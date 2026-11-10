#!/usr/bin/env python3
"""j — 个人日志 / 日报 / 月报。

单文件、无第三方依赖。数据是 ~/journal 下的 markdown，一天一个文件。
速记只追加；AI 只准写「计划」的勾选/新增和「总结」区块，永不改写速记原文。
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path

HOME = Path.home()
CFG_DIR = HOME / ".config" / "journal"
PROMPT_DIR = CFG_DIR / "prompts"
WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

DEFAULTS = {
    "JOURNAL_DIR": str(HOME / "journal"),
    "J_PI": str(HOME / ".local/bin/pi"),
    "J_MODEL_CAPTURE": "cn:glm-5.3-flash",
    "J_MODEL_REPORT": "cn:deepseek-v4.1-flash",
    "J_AI_CAPTURE": "1",
    "J_REPO_ROOTS": "/WorkSpace /ShareSpace",
    "J_GIT_AUTHOR": "",
    "J_DAY_START": "04:00",
    "J_BACKUP_DIR": str(HOME / "journal-backups"),
    "J_BACKUP_REMOTE": "",
    "J_BACKUP_KEEP": "7",
}


def load_config() -> dict:
    """config.sh（仓库里的默认值）→ config.local.sh（本机私有，不进仓库）→ 环境变量。"""
    cfg = dict(DEFAULTS)
    for name in ("config.sh", "config.local.sh"):
        p = CFG_DIR / name
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            cfg[k.strip()] = os.path.expandvars(os.path.expanduser(v))
    for k in DEFAULTS:  # 环境变量可覆盖，便于测试
        if k in os.environ:
            cfg[k] = os.path.expandvars(os.path.expanduser(os.environ[k]))
    return cfg


CFG = load_config()
ROOT = Path(CFG["JOURNAL_DIR"])


# ------------------------------------------------------------------ 时间与「今天」
# 一天从 J_DAY_START（默认 04:00）算起：凌晨 1 点记的东西仍属于前一天，
# 这样熬夜写完的日志不会莫名其妙跑到第二天去。
# 测试用 J_FAKE_NOW=2026-11-10T01:20:00 可以把「现在」钉死。


def _now() -> datetime:
    fake = os.environ.get("J_FAKE_NOW")
    if fake:
        try:
            return datetime.fromisoformat(fake)
        except ValueError:
            pass
    return datetime.now()


def day_start_offset() -> timedelta:
    raw = str(CFG.get("J_DAY_START", "00:00")).strip()
    try:
        h, m = (int(x) for x in raw.split(":")[:2])
    except Exception:
        return timedelta(0)
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return timedelta(0)
    return timedelta(hours=h, minutes=m)


def today() -> date:
    """逻辑上的「今天」。"""
    return (_now() - day_start_offset()).date()


def real_today() -> date:
    """挂钟上的今天，用来判断是否处在跨夜时段。"""
    return _now().date()


def is_late_night() -> bool:
    return real_today() != today()


# ---------------------------------------------------------------- 文件与行解析

SECTION_RE = re.compile(r"^##\s+(.*?)\s*$")
TASK_RE = re.compile(r"^\s*- \[([ xX])\]\s*(.*?)\s*$")
# 结转标记只认 "[↺3]" 这一种（方括号包住，正文不可能撞上）。
# 早期用过的 " ↺3" 裸格式已废弃：它会把正文末尾的「↺3」误当成结转次数。
CARRY_RE = re.compile(r"\s*\[↺(\d+)\]\s*$")
NOTE_RE = re.compile(r"^\s*- (\d{1,2}:\d{2})\s+(.*)$")


def day_path(d: date) -> Path:
    return ROOT / f"{d:%Y}" / f"{d:%m}" / f"{d:%Y-%m-%d}.md"


def all_days():
    out = []
    for p in ROOT.glob("*/*/*-*-??.md"):
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})\.md$", p.name)
        if not m:
            continue
        try:
            out.append((date(*map(int, m.groups())), p))
        except ValueError:
            pass
    return sorted(out)


def split_sections(text: str):
    pre, sections, cur = [], [], None
    for ln in text.splitlines():
        m = SECTION_RE.match(ln)
        if m:
            cur = [m.group(1), []]
            sections.append(cur)
        elif cur is None:
            pre.append(ln)
        else:
            cur[1].append(ln)
    for _, body in sections:  # 去掉区块首尾空行，避免追加时插进空行
        while body and not body[0].strip():
            body.pop(0)
        while body and not body[-1].strip():
            body.pop()
    return pre, sections


ORDER = {"计划": 0, "计划外": 1, "速记": 2}


def join_sections(pre, sections) -> str:
    out = list(pre)
    while out and not out[-1].strip():
        out.pop()
    ordered = sorted(enumerate(sections), key=lambda p: (ORDER.get(p[1][0], 2), p[0]))
    for _, (name, body) in ordered:
        b = list(body)
        while b and not b[0].strip():
            b.pop(0)
        while b and not b[-1].strip():
            b.pop()
        if out:
            out.append("")
        out.append("## " + name)
        out.extend(b)
    return "\n".join(out) + "\n"


def get_section(sections, name, create=False):
    for n, body in sections:
        if n == name:
            return body
    if not create:
        return None
    body = []
    sections.append([name, body])
    return body


def render_task(text, carry, done) -> str:
    return "- [%s] %s%s" % ("x" if done else " ", text, (" [↺%d]" % carry if carry else ""))


def parse_task(line):
    m = TASK_RE.match(line)
    if not m:
        return None
    text, carry = m.group(2), 0
    mm = CARRY_RE.search(text)
    if mm:
        carry = int(mm.group(1))
        text = text[: mm.start()].rstrip()
    return {"done": m.group(1).lower() == "x", "text": text, "carry": carry}


def plan_items(body):
    out = []
    for i, ln in enumerate(body or []):
        t = parse_task(ln)
        if t:
            out.append(dict(t, line=i))
    return out


EXTRA_RE = re.compile(r"^\s*- (\d{1,2}:\d{2})?\s*(.+)$")


def extra_items(body):
    """## 计划外 区块：干完的事，不进计划、不参与结转和完成率。"""
    out = []
    for i, ln in enumerate(body or []):
        m = EXTRA_RE.match(ln)
        if m:
            out.append({"time": m.group(1) or "", "text": m.group(2).strip(), "line": i})
    return out


def note_items(body):
    out = []
    for ln in body or []:
        m = NOTE_RE.match(ln)
        if m:
            out.append({"time": m.group(1), "text": m.group(2)})
    return out


def summary_sections(sections):
    return [(n, b) for n, b in sections if n.startswith("总结")]


def subsection(body, title):
    out, on = [], False
    for ln in body or []:
        if ln.startswith("### "):
            on = ln[4:].strip() == title
            continue
        if on and ln.strip().startswith("- "):
            out.append(ln.strip()[2:].strip())
    return out


# --------------------------------------------------------------------- 读写锁


class Lock:
    def __enter__(self):
        ROOT.mkdir(parents=True, exist_ok=True)
        self.f = open(ROOT / ".lock", "w")
        fcntl.flock(self.f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *a):
        fcntl.flock(self.f, fcntl.LOCK_UN)
        self.f.close()


def git_commit(msg: str) -> None:
    try:
        subprocess.run(["git", "-C", str(ROOT), "add", "-A"], capture_output=True, check=False)
        subprocess.run(
            ["git", "-C", str(ROOT), "commit", "-qm", msg[:200]],
            capture_output=True,
            check=False,
        )
    except FileNotFoundError:
        pass


def load_day(d: date):
    p = day_path(d)
    if not p.exists():
        return None, None, None
    return p, *split_sections(p.read_text(encoding="utf-8"))


def save_day(d: date, pre, sections, msg: str) -> None:
    p = day_path(d)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(join_sections(pre, sections), encoding="utf-8")
    git_commit(msg)


def carry_from_previous(d: date):
    prev = [(dd, p) for dd, p in all_days() if dd < d]
    if not prev:
        return []
    p = prev[-1][1]
    _, secs = split_sections(p.read_text(encoding="utf-8"))
    return [t for t in plan_items(get_section(secs, "计划")) if not t["done"]]


def new_day_text(d: date, carry) -> str:
    pre = [f"# {d:%Y-%m-%d} {WEEKDAYS[d.weekday()]}"]
    body = [render_task(t["text"], t["carry"] + 1, False) for t in carry]
    return join_sections(pre, [["计划", body], ["速记", []]])


def ensure_today(d: date) -> Path:
    p = day_path(d)
    if p.exists():
        return p
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(new_day_text(d, carry_from_previous(d)), encoding="utf-8")
    git_commit(f"day: {d} 开新一天")
    return p


# ------------------------------------------------------------------- AI 调用


def pi_call(prompt_file: str, material: str, model: str, timeout: int = 240,
            thinking: str = "low") -> str:
    sysp = (PROMPT_DIR / prompt_file).read_text(encoding="utf-8")
    cmd = [
        CFG["J_PI"], "-p",
        "--no-session", "--no-tools", "--no-skills",
        "--no-prompt-templates", "--no-context-files", "--no-extensions",
        "--mode", "text",
        "--model", model,
        "--thinking", thinking,
        "--system-prompt", sysp,
        material,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"pi 调用失败：{(r.stderr or r.stdout).strip()[:400]}")
    return r.stdout.strip()


def extract_json(s: str) -> dict:
    if not s:
        return {}
    i, j = s.find("{"), s.rfind("}")
    if i < 0 or j <= i:
        return {}
    try:
        v = json.loads(s[i : j + 1])
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def norm(t: str) -> str:
    return re.sub(r"[\s\u3000，。,.、；;：:！!？?（）()\[\]【】]+", "", t).lower()


# ---------------------------------------------------------------- 机器视角


SKIP_DIRS = {".git", "node_modules", "dist", "target", ".venv", "venv", ".idea", ".next", "build"}


def repo_names(repos):
    """给每个仓库取最短的唯一后缀名，例如 pmCloud_New/pmCloud_frontend。"""
    parts = [Path(r).parts for r in repos]
    out = []
    for p in parts:
        k = 1
        while k < len(p) and sum(1 for q in parts if q[-k:] == p[-k:]) > 1:
            k += 1
        out.append("/".join(p[-k:]))
    return out


def find_repos():
    out, seen = [], set()
    for root in CFG["J_REPO_ROOTS"].split():
        rp = Path(root)
        if not rp.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(rp):
            rel = Path(dirpath).relative_to(rp)
            if len(rel.parts) >= 4:
                dirnames[:] = []
                continue
            if ".git" in dirnames or ".git" in filenames:
                if dirpath not in seen:
                    seen.add(dirpath)
                    out.append(dirpath)
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
    # 同一个仓库可能被多个路径（软链/多份克隆）发现，按真实路径去重
    uniq, real = [], set()
    for r in sorted(out):
        rp = os.path.realpath(r)
        if rp in real:
            continue
        real.add(rp)
        uniq.append(r)
    return uniq


def git_commits(since: str, until: str, max_per_repo: int = 25, max_total: int = 90):
    """采集区间内本人（J_GIT_AUTHOR）的提交，按仓库分组。同一份代码的多份克隆只报一次。"""
    repos = find_repos()
    names = repo_names(repos)

    def run(author):
        lines, total, seen = [], 0, set()
        for repo, name in zip(repos, names):
            cmd = ["git", "-C", repo, "log", f"--since={since}", f"--until={until}",
                   "--pretty=format:%h\t%ad\t%s", "--date=format:%m-%d %H:%M"]
            if author:
                cmd.insert(4, f"--author={author}")
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
            except Exception:
                continue
            fresh = []
            for line in (r.stdout or "").splitlines():
                parts = line.split("\t")
                if len(parts) < 3:
                    continue
                h, when, subj = parts[0], parts[1], "\t".join(parts[2:])
                if h in seen:
                    continue
                seen.add(h)
                fresh.append(f"{when}  {subj}")
            if not fresh:
                continue
            lines.append(f"{name}（{len(fresh)} 次提交）")
            lines += ["  - " + x for x in fresh[:max_per_repo]]
            total += len(fresh)
            if total >= max_total:
                break
        return lines

    author = CFG.get("J_GIT_AUTHOR", "").strip()
    out = run(author)
    if not out and author:
        out = run("")
    return out


def herdr_done(limit=8):
    exe = shutil.which("herdr-projects") or str(HOME / ".herdr-projects/data/bin/herdr-projects")
    if not Path(exe).exists():
        return []
    try:
        r = subprocess.run([exe, "list", "--json", "--all"], capture_output=True,
                           text=True, timeout=25)
        items = json.loads(r.stdout or "[]")
    except Exception:
        return []
    out = []
    for t in items if isinstance(items, list) else []:
        if isinstance(t, dict) and t.get("state") == "done":
            title = (t.get("title") or "").strip()
            msg = (t.get("msg") or "").strip()
            out.append(f"- [{t.get('id')}] {title}" + (f" — {msg}" if msg else ""))
    return out[:limit]


def machine_view(since: str, until: str) -> str:
    blocks = []
    commits = git_commits(since, until)
    if commits:
        blocks.append("git 提交（脚本采集）:\n" + "\n".join(commits))
    board = herdr_done()
    if board:
        blocks.append("任务板上已完成（当前快照，没有日期，不一定是今天的）:\n" + "\n".join(board))
    return "\n\n".join(blocks)


# ------------------------------------------------------------------- 采集


STATE_FILE = "distill.state"


def save_distill_state(**kw) -> None:
    try:
        data = {"ts": _now().isoformat(timespec="seconds")}
        data.update(kw)
        (ROOT / STATE_FILE).write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )
    except Exception:
        pass


def load_distill_state() -> dict:
    try:
        return json.loads((ROOT / STATE_FILE).read_text(encoding="utf-8"))
    except Exception:
        return {}


def distill_status_text():
    """给看板用的一句话状态，返回 (文本, 颜色)。"""
    st = load_distill_state()
    if not st.get("ts"):
        return "", ""
    try:
        ago = (_now() - datetime.fromisoformat(st["ts"])).total_seconds()
    except Exception:
        return "", ""
    if st.get("ok") is False:
        return "AI 失败", C_YEL
    if ago < 90:
        when = "刚刚"
    elif ago < 3600:
        when = f"{int(ago // 60)} 分钟前"
    else:
        when = f"{int(ago // 3600)} 小时前"
    return f"AI {when}", C_FAINT


def spawn_distill() -> None:
    try:
        log = open(ROOT / ".distill.log", "a")
    except Exception:
        return
    subprocess.Popen(
        [sys.executable, os.path.realpath(__file__), "distill"],
        stdout=log, stderr=log, stdin=subprocess.DEVNULL,
        start_new_session=True,
    )


def cmd_capture(text: str, ai: bool) -> int:
    text = " ".join(text.split())
    if not text:
        print("空内容，什么都没记。", file=sys.stderr)
        return 1
    d = today()
    # 开头的符号决定去向，决定权永远在你手里：
    #   +  → 计划项（待办，会结转）
    #   -  → 切换某一项计划项的完成状态（后面跟编号或关键字）
    #   !  → 计划外完成（干完了、但本来没排的事）
    #   其他 → 速记，后台 AI 再判断是否影响计划
    mode = "note"
    if text.startswith(("+", "＋")):
        mode, text = "plan", text.lstrip("+＋ ").strip()
    elif text.startswith(("-", "－", "—")):
        mode, text = "toggle", text.lstrip("-－— ").strip()
    elif text.startswith(("!", "！")):
        mode, text = "extra", text.lstrip("!！ ").strip()
    if not text:
        print("内容为空。", file=sys.stderr)
        return 1
    with Lock():
        ensure_today(d)
        p, pre, secs = load_day(d)
        if mode == "plan":
            get_section(secs, "计划", create=True).append(render_task(text, 0, False))
            save_day(d, pre, secs, f"plan {d}: + {text[:50]}")
        elif mode == "toggle":
            body = get_section(secs, "计划")
            items = plan_items(body)
            hits = match_tasks(items, text)
            if not hits:
                print(f"计划里没找到「{text}」。j list 看看编号。", file=sys.stderr)
                return 1
            if len(hits) > 1:
                print(f"「{text}」匹配到 {len(hits)} 项，用编号：", file=sys.stderr)
                for i in hits:
                    print(f"  {i + 1}. {items[i]['text']}", file=sys.stderr)
                return 1
            t = items[hits[0]]
            body[t["line"]] = render_task(t["text"], t["carry"], not t["done"])
            save_day(d, pre, secs, f"plan {d}: toggle {t['text'][:50]}")
        elif mode == "extra":
            get_section(secs, "计划外", create=True).append(f"- {_now():%H:%M} {text}")
            save_day(d, pre, secs, f"extra {d}: {text[:50]}")
        else:
            get_section(secs, "速记", create=True).append(f"- {_now():%H:%M} {text}")
            save_day(d, pre, secs, f"note {d}: {text[:50]}")
    if ai and mode == "note" and CFG.get("J_AI_CAPTURE", "1") == "1":
        spawn_distill()
    return 0


def cmd_distill() -> int:
    # 一次只跑一个：并发跑会互相覆盖整篇文件
    lockfile = ROOT / ".distill.lock"
    try:
        ROOT.mkdir(parents=True, exist_ok=True)
        fh = open(lockfile, "w")
    except Exception:
        return 0
    got = False
    for _ in range(40):  # 最多排 20 秒
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            got = True
            break
        except OSError:
            time.sleep(0.5)
    if not got:
        print("[distill] 前一个还在跑，放弃", file=sys.stderr)
        return 0
    try:
        return _distill_locked()
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


def _distill_locked() -> int:
    d = today()
    p, pre, secs = load_day(d)
    if not p:
        return 0
    plan = plan_items(get_section(secs, "计划"))
    notes = note_items(get_section(secs, "速记"))
    if not plan and not notes:
        return 0

    if not notes:
        return 0
    target, ctx = notes[-1], notes[-4:-1]  # 只判断最新这一条，旧的只当背景

    mat = ["今天的计划（编号从 1 开始，✔ 表示已完成）:"]
    for i, t in enumerate(plan, 1):
        mat.append(f"{i}. [{'x' if t['done'] else ' '}] {t['text']}")
    if not plan:
        mat.append("（空）")
    mat.append("")
    mat.append("刚记的这条（只判断这一条）:")
    mat.append(f"- {target['time']} {target['text']}")
    if ctx:
        mat.append("")
        mat.append("更早的几条（只作背景，不要对它们做任何判断）:")
        mat += [f"- {n['time']} {n['text']}" for n in ctx]

    try:
        out = pi_call("distill.md", "\n".join(mat), CFG["J_MODEL_CAPTURE"], timeout=120,
                      thinking=CFG.get("J_THINK_CAPTURE", "off"))
    except Exception as e:
        print(f"[distill] 跳过：{e}", file=sys.stderr)
        save_distill_state(ok=False, error=str(e)[:300])
        return 0
    stamp = f"{_now():%H:%M:%S}"
    ops = extract_json(out)
    if not ops:
        print(f"[{stamp}] 无法解析，忽略：{out[:200]}", file=sys.stderr)
        return 0

    changed = []
    with Lock():
        p, pre, secs = load_day(d)
        body = get_section(secs, "计划", create=True)
        items = plan_items(body)
        for key in ops.get("done") or []:
            k = None
            if isinstance(key, int) or (isinstance(key, str) and key.strip().isdigit()):
                i = int(key) - 1
                k = i if 0 <= i < len(items) else None
            else:
                q = norm(str(key))
                if q:
                    hits = [i for i, t in enumerate(items) if norm(t["text"]) == q]
                    if not hits:
                        hits = [i for i, t in enumerate(items)
                                if q in norm(t["text"]) or norm(t["text"]) in q]
                    if len(hits) == 1:
                        k = hits[0]
            if k is None or items[k]["done"]:
                continue
            t = items[k]
            body[t["line"]] = render_task(t["text"], t["carry"], True)
            changed.append(f"✔ {t['text']}")
        existing = {norm(t["text"]) for t in items}
        for add in ops.get("add") or []:
            s = " ".join(str(add).split())
            if not s or len(s) > 200 or norm(s) in existing:
                continue
            existing.add(norm(s))
            body.append(render_task(s, 0, False))
            changed.append(f"+ {s}")
        # 计划外完成：进「## 计划外」，不污染计划、不影响完成率
        extra_body = get_section(secs, "计划外", create=True) if ops.get("done_add") else None
        extra_seen = {norm(t["text"]) for t in extra_items(get_section(secs, "计划外"))} | existing
        for add in ops.get("done_add") or []:
            s = " ".join(str(add).split())
            if not s or len(s) > 200 or norm(s) in extra_seen:
                continue
            extra_seen.add(norm(s))
            stamp_hhmm = target["time"]
            extra_body = extra_body if extra_body is not None else get_section(secs, "计划外", create=True)
            extra_body.append(f"- {stamp_hhmm} {s}")
            changed.append(f"◆ {s}")
        if changed:
            save_day(d, pre, secs, f"day {d}: " + "; ".join(changed)[:80])
    if changed:
        for c in changed:
            print(f"[{stamp}] {c}", file=sys.stderr)
        save_distill_state(ok=True, applied=changed)
    else:
        raw = " ".join(out.split())[:120]
        print(f"[{stamp}] 无变化 · 模型返回 {raw}", file=sys.stderr)
        save_distill_state(ok=True, applied=[])
    return 0


# --------------------------------------------------------------- 计划项编辑


def match_tasks(items, key: str):
    if key.isdigit():
        k = int(key) - 1
        return [k] if 0 <= k < len(items) else []
    key = key.lower()
    hits = [i for i, t in enumerate(items) if key in t["text"].lower()]
    return hits


def cmd_plan(text: str) -> int:
    text = " ".join(text.split())
    if not text:
        print("用法: j plan <任务内容>", file=sys.stderr)
        return 1
    d = today()
    with Lock():
        ensure_today(d)
        p, pre, secs = load_day(d)
        get_section(secs, "计划", create=True).append(render_task(text, 0, False))
        save_day(d, pre, secs, f"plan {d}: + {text[:50]}")
    print(f"+ {text}")
    return 0


def _git(*args, timeout=20):
    return subprocess.run(["git", "-C", str(ROOT), *args],
                          capture_output=True, text=True, timeout=timeout)


def cmd_undo(rest) -> int:
    """撤销最近 n 次写入（默认 1 次）。每次写入都是独立 commit，所以撤得很干净。"""
    dry = "--dry" in rest
    nums = [a for a in rest if a.isdigit()]
    n = max(1, int(nums[0])) if nums else 1
    if not (ROOT / ".git").exists():
        print("数据目录不是 git 仓库，无法撤销。", file=sys.stderr)
        return 1
    if _git("status", "--porcelain").stdout.strip():
        print("工作区有未提交的改动，先处理掉再撤销。", file=sys.stderr)
        return 1
    log = _git("log", "-n", str(n), "--pretty=%h %ad %s", "--date=format:%m-%d %H:%M")
    commits = [ln for ln in log.stdout.splitlines() if ln.strip()]
    if not commits:
        print("没有可撤销的提交。", file=sys.stderr)
        return 1
    print(f"将撤销最近 {len(commits)} 次写入:" if not dry else f"将要撤销（dry-run）:")
    for c in commits:
        print("  " + c)
    if dry:
        return 0
    rng = "HEAD" if n == 1 else f"HEAD~{n}..HEAD"
    r = _git("revert", "--no-edit", rng, timeout=60)
    if r.returncode != 0:
        _git("revert", "--abort")
        print(f"撤销失败（可能有冲突），已回滚到撤销前：\n{(r.stderr or '')[:400]}",
              file=sys.stderr)
        return 1
    print("已撤销。用 j show 确认一下。")
    return 0


def cmd_backup(rest) -> int:
    """给 ~/journal 做 git bundle 快照；配了 J_BACKUP_REMOTE 还能顺便推远端。"""
    keep = 7
    try:
        keep = max(1, int(CFG.get("J_BACKUP_KEEP", "7")))
    except Exception:
        pass
    bdir = Path(CFG["J_BACKUP_DIR"])
    push = "--push" in rest
    remote = CFG.get("J_BACKUP_REMOTE", "").strip()

    if not (ROOT / ".git").exists():
        print("数据目录不是 git 仓库，没法备份。", file=sys.stderr)
        return 1
    if not _git("rev-list", "--all", "--max-count=1").stdout.strip():
        print("还没有任何记录，不用备份。")
        return 0
    bdir.mkdir(parents=True, exist_ok=True)
    stamp = _now().strftime("%Y%m%d-%H%M%S")
    name = bdir / f"journal-{stamp}.bundle"
    if name.exists():  # 同一秒内多次备份，别互相覆盖
        name = bdir / f"journal-{stamp}-{_now().strftime('%f')[:3]}.bundle"
    r = subprocess.run(["git", "-C", str(ROOT), "bundle", "create", str(name), "--all"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"bundle 失败：{(r.stderr or '')[:300]}", file=sys.stderr)
        return 1

    # 校验一遍，别存个坏的
    v = subprocess.run(["git", "-C", str(ROOT), "bundle", "verify", str(name)],
                       capture_output=True, text=True)
    ok = v.returncode == 0
    size = name.stat().st_size
    print(f"{'✓' if ok else '✗'} {name}（{size / 1024:.0f} KB）"
          + ("" if ok else f"  校验失败：{v.stderr[:200]}"))
    if not ok:
        return 1

    bundles = sorted(bdir.glob("journal-*.bundle"), key=lambda f: f.stat().st_mtime)
    for old in bundles[:-keep]:
        old.unlink()
        print(f"  清掉旧备份 {old.name}")
    print(f"  保留最近 {min(len(bundles), keep)} 份")

    if push:
        if not remote:
            print("  没配 J_BACKUP_REMOTE，跳过推远端"
                  "（在 config.local.sh 里设一个私有仓库地址即可）", file=sys.stderr)
        else:
            pr = subprocess.run(["git", "-C", str(ROOT), "push", remote, "--all"],
                                capture_output=True, text=True, timeout=120)
            if pr.returncode == 0:
                print(f"  已推到 {remote}")
            else:
                print(f"  推远端失败：{(pr.stderr or '')[:300]}", file=sys.stderr)
                return 1
    return 0


def cmd_start(rest) -> int:
    """开工视图：上次没做完的 + 上次定下的明日计划 + 今天的现状。"""
    d = today()
    prev = [(dd, p) for dd, p in all_days() if dd < d]
    print(f"{C_BOLD}今天 {d:%Y-%m-%d} {WEEKDAYS[d.weekday()]}{C_R}")
    if is_late_night():
        print(f"{C_YEL}（挂钟已过 0 点，仍记在前一天）{C_R}")

    if prev:
        dd, pp = prev[-1]
        _, secs = split_sections(pp.read_text(encoding="utf-8"))
        left = [t for t in plan_items(get_section(secs, "计划")) if not t["done"]]
        print(f"\n{C_DIM}上次记录 {dd:%m-%d} {WEEKDAYS[dd.weekday()]}，没做完 {len(left)} 项{C_R}"
              if left else f"\n{C_DIM}上次记录 {dd:%m-%d}，没有遗留{C_R}")
        for t in left[:12]:
            carry = f" {C_YEL}[↺{t['carry'] + 1}]{C_R}" if True else ""
            print(f"  {C_CYAN}○{C_R} {t['text']}{carry}")
        ss = summary_sections(secs)
        if ss:
            todo = subsection(ss[-1][1], "明日计划")
            if todo:
                print(f"{C_DIM}上次总结里定下的明日计划:{C_R}")
                for t in todo[:6]:
                    print(f"  {C_FAINT}·{C_R} {C_DIM}{t}{C_R}")
    else:
        print(f"\n{C_DIM}这是第一天，还没有历史记录{C_R}")

    p, pre, secs = load_day(d)
    plan = plan_items(get_section(secs, "计划")) if p else []
    notes = note_items(get_section(secs, "速记")) if p else []
    extra = extra_items(get_section(secs, "计划外")) if p else []
    done = sum(1 for t in plan if t["done"])
    print(f"\n今天已记: 计划 {len(plan)} 项（完成 {done}）· 计划外 {len(extra)} · 速记 {len(notes)}")
    ai_text, _ = distill_status_text()
    if ai_text:
        print(f"{C_FAINT}{ai_text}{C_R}")
    if not plan:
        print(f"\n{C_DIM}还没排今天的计划。{C_R}prefix Space 打 {C_GREEN}+ 任务{C_R}{C_DIM} 就能加。{C_R}")
    return 0


def cmd_status(rest) -> int:
    d = today()
    p, pre, secs = load_day(d)
    plan = plan_items(get_section(secs, "计划")) if p else []
    extra = extra_items(get_section(secs, "计划外")) if p else []
    notes = note_items(get_section(secs, "速记")) if p else []
    done = sum(1 for t in plan if t["done"])
    print(f"今天 {d:%Y-%m-%d} {WEEKDAYS[d.weekday()]}"
          + ("  ·跨夜（挂钟已过 0 点，仍记在前一天）" if is_late_night() else ""))
    print(f"  计划 {len(plan)} 项（完成 {done}）· 计划外 {len(extra)} · 速记 {len(notes)}")
    st = load_distill_state()
    if st.get("ts"):
        flag = "失败" if st.get("ok") is False else "正常"
        n = len(st.get("applied") or [])
        print(f"  AI 整理: {st['ts'].replace('T', ' ')} {flag}"
              + (f"，补了 {n} 条" if n else "，无变化")
              + (f"（{st['error'][:60]}）" if st.get("error") else ""))
    else:
        print("  AI 整理: 还没有运行过")
    if (ROOT / ".git").exists():
        last = _git("log", "-1", "--pretty=%ad %s", "--date=format:%m-%d %H:%M").stdout.strip()
        print(f"  最近写入: {last}")
    return 0


def cmd_list(rest) -> int:
    """给 fzf 用：每行 `类型<TAB>序号<TAB>标记 文本`。p=计划 e=计划外。--open 只看未完成。"""
    only_open = "--open" in rest
    d = today()
    p, pre, secs = load_day(d)
    if not p:
        print("今天还没有文件。", file=sys.stderr)
        return 1
    n = 0
    for i, t in enumerate(plan_items(get_section(secs, "计划")), 1):
        if only_open and t["done"]:
            continue
        mark = "✔" if t["done"] else "○"
        tail = f"  [↺{t['carry']}]" if (t["carry"] and not t["done"]) else ""
        print(f"p\t{i}\t{mark} {t['text']}{tail}")
        n += 1
    for i, e in enumerate(extra_items(get_section(secs, "计划外")), 1):
        print(f"e\t{i}\t◆ {e['text']}")
        n += 1
    return 0 if n else 1


def _edit_target(rest):
    """解析 <序号> [--extra] [其他文本]"""
    extra = "--extra" in rest
    args = [a for a in rest if a != "--extra"]
    if not args or not args[0].isdigit():
        return None, None, extra
    return int(args[0]), " ".join(args[1:]).strip(), extra


def cmd_rm(rest) -> int:
    idx, _, extra = _edit_target(rest)
    if not idx:
        print("用法: j rm <序号> [--extra]", file=sys.stderr)
        return 1
    d = today()
    with Lock():
        p, pre, secs = load_day(d)
        if not p:
            print("今天还没有文件。", file=sys.stderr)
            return 1
        name = "计划外" if extra else "计划"
        body = get_section(secs, name)
        items = extra_items(body) if extra else plan_items(body)
        if not (1 <= idx <= len(items)):
            print(f"{name}里没有第 {idx} 项。", file=sys.stderr)
            return 1
        if extra:
            removed = items[idx - 1]["text"]
            body.pop(items[idx - 1]["line"])
        else:
            t = items[idx - 1]
            removed = t["text"]
            body.pop(t["line"])
        save_day(d, pre, secs, f"rm {d}: {name} {removed[:50]}")
    print(f"删除: {removed}")
    return 0


def cmd_rename(rest) -> int:
    idx, newtext, extra = _edit_target(rest)
    if not idx:
        print("用法: j rename <序号> [新名字] [--extra]", file=sys.stderr)
        return 1
    d = today()
    p, pre, secs = load_day(d)
    if not p:
        print("今天还没有文件。", file=sys.stderr)
        return 1
    name = "计划外" if extra else "计划"
    body = get_section(secs, name) or []
    items = extra_items(body) if extra else plan_items(body)
    if not (1 <= idx <= len(items)):
        print(f"{name}里没有第 {idx} 项。", file=sys.stderr)
        return 1
    old = items[idx - 1]["text"]
    if not newtext:
        try:
            newtext = input(f"新名字（原「{old}」）: ").strip()
        except (EOFError, KeyboardInterrupt):
            return 1
    if not newtext:
        print("没输入名字，什么都没改。", file=sys.stderr)
        return 1
    with Lock():
        p, pre, secs = load_day(d)
        body = get_section(secs, name)
        items = extra_items(body) if extra else plan_items(body)
        if extra:
            body[items[idx - 1]["line"]] = f"- {items[idx-1]['time']} {newtext}".replace("  ", " ")
        else:
            t = items[idx - 1]
            body[t["line"]] = render_task(newtext, t["carry"], t["done"])
        save_day(d, pre, secs, f"rename {d}: {old[:30]} -> {newtext[:30]}")
    print(f"{old} → {newtext}")
    return 0


def cmd_promote(rest) -> int:
    idx, newtext, _ = _edit_target(rest)
    if not idx:
        print("用法: j promote <计划外序号> [新任务文本]", file=sys.stderr)
        return 1
    d = today()
    p, pre, secs = load_day(d)
    if not p:
        print("今天还没有文件。", file=sys.stderr)
        return 1
    body = get_section(secs, "计划外") or []
    items = extra_items(body)
    if not (1 <= idx <= len(items)):
        print(f"计划外没有第 {idx} 项。", file=sys.stderr)
        return 1
    src = items[idx - 1]["text"]
    if not newtext:
        try:
            newtext = input(f"衍生出的待办（回车沿用「{src}」）: ").strip() or src
        except (EOFError, KeyboardInterrupt):
            return 1
    with Lock():
        p, pre, secs = load_day(d)
        get_section(secs, "计划", create=True).append(render_task(newtext, 0, False))
        save_day(d, pre, secs, f"promote {d}: {newtext[:50]}")
    print(f"+ {newtext}（来自计划外: {src}）")
    return 0


def cmd_done(key: str) -> int:
    d = today()
    p, pre, secs = load_day(d)
    if not p:
        print("今天还没有文件。", file=sys.stderr)
        return 1
    items = plan_items(get_section(secs, "计划"))
    if not key:
        if not items:
            print("今日计划为空。")
            return 0
        for i, t in enumerate(items, 1):
            print(f"{i}. [{'x' if t['done'] else ' '}] {t['text']}")
        print("\n用法: j done <编号|关键字>  切换完成状态")
        return 0
    hits = match_tasks(items, key)
    if not hits:
        print(f"没找到: {key}", file=sys.stderr)
        return 1
    if len(hits) > 1:
        print(f"「{key}」匹配到 {len(hits)} 项，请用编号：", file=sys.stderr)
        for i in hits:
            print(f"  {i + 1}. {items[i]['text']}", file=sys.stderr)
        return 1
    k = hits[0]
    t = items[k]
    with Lock():
        p, pre, secs = load_day(d)
        body = get_section(secs, "计划")
        t = plan_items(body)[k]
        body[t["line"]] = render_task(t["text"], t["carry"], not t["done"])
        save_day(d, pre, secs, f"plan {d}: toggle {t['text'][:50]}")
    print(f"[{' ' if t['done'] else 'x'}] {t['text']}")
    return 0


# ------------------------------------------------------------------- 日报


def daily_material(d: date, plan, notes, secs) -> str:
    done = sum(1 for t in plan if t["done"])
    carried = [t for t in plan if not t["done"] and t["carry"] >= 2]
    extra = extra_items(get_section(secs, "计划外"))
    out = [f"日期: {d:%Y-%m-%d} {WEEKDAYS[d.weekday()]}", ""]
    out.append(
        f"统计（脚本算出）: 计划 {len(plan)} 项，已完成 {done} 项，未完成 {len(plan) - done} 项"
        + (f"（其中结转 ≥2 天的 {len(carried)} 项）" if carried else "")
        + f"；计划外完成 {len(extra)} 项；速记 {len(notes)} 条。"
    )
    out.append("")
    out.append("计划:")
    for i, t in enumerate(plan, 1):
        c = f"（已结转 {t['carry']} 天）" if t["carry"] else ""
        out.append(f"{i}. [{'x' if t['done'] else ' '}] {t['text']}{c}")
    if not plan:
        out.append("（空）")
    if extra:
        out.append("")
        out.append("计划外完成（不在计划里、但今天干了的）:")
        out += [f"- {e['time']} {e['text']}".rstrip() for e in extra]
    out.append("")
    out.append("速记:")
    out += [f"- {n['time']} {n['text']}" for n in notes] or ["（空）"]
    mv = machine_view(f"{d} 00:00", f"{d} 23:59")
    if mv:
        out += ["", "机器证据:", mv]
    return "\n".join(out)


def cmd_report(dry: bool) -> int:
    d = today()
    with Lock():
        ensure_today(d)
    p, pre, secs = load_day(d)
    plan = plan_items(get_section(secs, "计划"))
    notes = note_items(get_section(secs, "速记"))
    extra = extra_items(get_section(secs, "计划外"))
    if not plan and not notes and not extra:
        print("今天还没有任何记录，先记一笔再来。", file=sys.stderr)
        return 1
    print("生成中…", file=sys.stderr)
    try:
        out = pi_call("report.md", daily_material(d, plan, notes, secs), CFG["J_MODEL_REPORT"],
                      thinking=CFG.get("J_THINK_REPORT", "low"))
    except Exception as e:
        print(f"失败：{e}", file=sys.stderr)
        return 1
    out = out.strip()
    if not out:
        print("模型返回空。", file=sys.stderr)
        return 1
    if dry:
        print(out)
        return 0
    stamp = f"{_now():%H:%M}"
    with Lock():
        p, pre, secs = load_day(d)
        secs = [(n, b) for n, b in secs if n != f"总结 · {stamp}"]
        secs.append([f"总结 · {stamp}", out.splitlines()])
        save_day(d, pre, secs, f"report {d} {stamp}")
    print(out)
    return 0


# ------------------------------------------------------------------- 月报


def collect_month(ym: str):
    """ym = 'YYYY-MM'"""
    y, m = map(int, ym.split("-"))
    start = date(y, m, 1)
    end = date(y + (m == 12), (m % 12) + 1, 1) - timedelta(days=1)
    days = [(dd, p) for dd, p in all_days() if start <= dd <= end]
    return start, end, days


def period_material(days, title: str, since: str, until: str) -> str:
    """把一段时间（月 / 周）的材料拼给模型。days: [(date, path)]"""
    total = done = nsum = nextra = 0
    per_day_done, delay, body = [], {}, []
    for dd, p in days:
        _, secs = split_sections(p.read_text(encoding="utf-8"))
        plan = plan_items(get_section(secs, "计划"))
        notes = note_items(get_section(secs, "速记"))
        extra = extra_items(get_section(secs, "计划外"))
        nextra += len(extra)
        total += len(plan)
        dn = sum(1 for t in plan if t["done"])
        done += dn
        per_day_done.append(f"{dd:%m-%d}:{dn}")
        for t in plan:
            if not t["done"] and t["carry"]:
                delay[t["text"]] = max(delay.get(t["text"], 0), t["carry"])
        ss = summary_sections(secs)
        nsum += len(ss)
        body.append(f"== {dd:%m-%d} {WEEKDAYS[dd.weekday()]} ==")
        for t in plan:
            c = f"（结转 {t['carry']} 天）" if t["carry"] else ""
            body.append(f"- [{'x' if t['done'] else ' '}] {t['text']}{c}")
        for n in notes[:12]:
            body.append(f"· {n['time']} {n['text']}")
        for e in extra[:10]:
            body.append(f"◆ 计划外完成: {e['text']}")
        if ss:
            body.append(f"（当日总结 {len(ss)} 次，最后一次 {ss[-1][0].split('· ')[-1]}）")
        body.append("")

    top_delay = sorted(delay.items(), key=lambda kv: -kv[1])[:6]
    head = [
        title,
        "",
        "统计（脚本算出，直接引用）:",
        f"- 有记录的天数: {len(days)}",
        f"- 计划项合计: {total}，完成 {done}，完成率 {round(done * 100 / total) if total else 0}%",
        f"- 计划外完成: {nextra} 项（不在计划里、实际干了的事）",
        f"- 生成总结次数: {nsum}",
        f"- 每日完成数: {' '.join(per_day_done) if per_day_done else '（无）'}",
    ]
    if top_delay:
        head.append("- 拖延最久（结转天数）:")
        head += [f"  - {t} → {n} 天" for t, n in top_delay]
    head += ["", "每日记录:", ""]
    mv = machine_view(since, until)
    tail = ["", "机器证据:", mv] if mv else []
    return "\n".join(head) + "\n".join(body) + "\n".join(tail)


def month_material(ym: str) -> str:
    start, end, days = collect_month(ym)
    return period_material(days, f"月份: {ym}", f"{start} 00:00", f"{end} 23:59")


def collect_week(arg: str | None):
    """arg: 2026-W45 或空（取逻辑今天所在的那一周，周一开始）"""
    if arg:
        m = re.match(r"^(\d{4})-?W(\d{1,2})$", arg.strip(), re.I)
        if not m:
            raise ValueError("周报参数要写成 2026-W45")
        d = date.fromisocalendar(int(m.group(1)), int(m.group(2)), 1)
    else:
        t = today()
        d = t - timedelta(days=t.weekday())
    start = d
    end = d + timedelta(days=6)
    days = [(dd, p) for dd, p in all_days() if start <= dd <= end]
    return start, end, days


def week_material(arg: str | None) -> str:
    start, end, days = collect_week(arg)
    iso = start.isocalendar()
    return period_material(
        days, f"周: {iso[0]}-W{iso[1]:02d}（{start:%m-%d} ~ {end:%m-%d}）",
        f"{start} 00:00", f"{end} 23:59",
    )


def day_notes(d: date):
    _, _, secs = load_day(d)
    if secs is None:
        return []
    return note_items(get_section(secs, "速记"))


def cmd_log(rest) -> int:
    """j log [-n N] [-a|--all] [YYYY-MM-DD|YYYY-MM]"""
    limit, all_days_flag, target = 10, False, None
    i = 0
    while i < len(rest):
        a = rest[i]
        if a in ("-n", "--lines"):
            try:
                limit = int(rest[i + 1])
                i += 1
            except Exception:
                pass
        elif a in ("-a", "--all"):
            all_days_flag = True
        elif not a.startswith("-"):
            target = a
        i += 1

    if all_days_flag or target:
        rows = []
        for dd, _ in all_days():
            if target:
                if re.match(r"^\d{4}-\d{2}-\d{2}$", target) and f"{dd:%Y-%m-%d}" != target:
                    continue
                if re.match(r"^\d{4}-\d{2}$", target) and f"{dd:%Y-%m}" != target:
                    continue
            rows += [(dd, n) for n in day_notes(dd)]
        if not rows:
            print("没有匹配的速记。", file=sys.stderr)
            return 0
        for dd, n in rows[-limit:]:
            print(f"{C_DIM}{dd:%m-%d}{C_R} {C_DIM}{n['time']}{C_R}  {n['text']}" if sys.stdout.isatty()
                  else f"{dd:%Y-%m-%d} {n['time']}  {n['text']}")
        return 0

    items = day_notes(today())
    if not items:
        print("今天还没有速记。j \"<一句话>\" 记一笔。", file=sys.stderr)
        return 0
    for n in items[-limit:]:
        print(f"{n['time']}  {n['text']}")
    return 0


def cmd_search(words: str) -> int:
    """跨天搜索：速记、计划项、计划外都搜。多个词是 AND。"""
    if not words.strip():
        print("用法: j search <关键词> [关键词...]", file=sys.stderr)
        return 1
    keys = [w.lower() for w in words.split()]
    hit = 0
    for dd, _ in all_days():
        _, _, secs = load_day(dd)
        if secs is None:
            continue
        rows = []
        for n in note_items(get_section(secs, "速记")):
            rows.append((f"{n['time']}", "速记  ", n["text"]))
        for t in plan_items(get_section(secs, "计划")):
            rows.append(("", "计划  ", f"[{'x' if t['done'] else ' '}] {t['text']}"))
        for e in extra_items(get_section(secs, "计划外")):
            rows.append((e["time"], "计划外", e["text"]))
        for when, kind, text in rows:
            low = text.lower()
            if all(k in low for k in keys):
                hit += 1
                print(f"{dd:%Y-%m-%d} {kind} {when:>5}  {text}")
    if not hit:
        print(f"没找到含「{words}」的记录。", file=sys.stderr)
        return 1
    print(f"\n—— {hit} 条", file=sys.stderr)
    return 0


def cmd_notes() -> int:
    """给 fzf 用的平铺列表：YYYY-MM-DD HH:MM 内容"""
    for dd, _ in all_days():
        for n in day_notes(dd):
            print(f"{dd:%Y-%m-%d} {n['time']}  {n['text']}")
    return 0


def cmd_day(arg: str | None) -> int:
    if not arg:
        arg = f"{today():%Y-%m-%d}"
    try:
        d = datetime.strptime(arg, "%Y-%m-%d").date()
    except ValueError:
        print("用法: j day YYYY-MM-DD", file=sys.stderr)
        return 1
    p, _, _ = load_day(d)
    if not p:
        print(f"{d} 没有记录。", file=sys.stderr)
        return 1
    print(p.read_text(encoding="utf-8"), end="")
    return 0


def cmd_week(arg: str | None, dry: bool) -> int:
    try:
        start, end, days = collect_week(arg)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1
    iso = start.isocalendar()
    key = f"{iso[0]}-W{iso[1]:02d}"
    if not days:
        print(f"{key}（{start:%m-%d} ~ {end:%m-%d}）没有任何记录。", file=sys.stderr)
        return 1
    print(f"生成 {key} 周报中（{len(days)} 天记录）…", file=sys.stderr)
    try:
        out = pi_call("week.md", week_material(arg), CFG["J_MODEL_REPORT"], timeout=600,
                      thinking=CFG.get("J_THINK_REPORT", "low"))
    except Exception as e:
        print(f"失败：{e}", file=sys.stderr)
        return 1
    out = out.strip()
    if not out:
        print("模型返回空。", file=sys.stderr)
        return 1
    if not dry:
        rp = ROOT / "reports" / f"{key}.md"
        rp.parent.mkdir(parents=True, exist_ok=True)
        rp.write_text(f"# {key} 周报\n\n> {start:%Y-%m-%d} ~ {end:%Y-%m-%d}"
                      f" · 生成于 {_now():%Y-%m-%d %H:%M}\n\n{out}\n", encoding="utf-8")
        git_commit(f"week report {key}")
        print(f"已写入 {rp}", file=sys.stderr)
    print(out)
    return 0


def cmd_month(ym: str | None, dry: bool) -> int:
    ym = ym or f"{today():%Y-%m}"
    if not re.match(r"^\d{4}-\d{2}$", ym):
        print("用法: j month [YYYY-MM]", file=sys.stderr)
        return 1
    start, end, days = collect_month(ym)
    if not days:
        print(f"{ym} 没有任何记录。", file=sys.stderr)
        return 1
    print(f"生成 {ym} 月报中（{len(days)} 天记录）…", file=sys.stderr)
    try:
        out = pi_call("month.md", month_material(ym), CFG["J_MODEL_REPORT"], timeout=600,
                      thinking=CFG.get("J_THINK_REPORT", "low"))
    except Exception as e:
        print(f"失败：{e}", file=sys.stderr)
        return 1
    out = out.strip()
    if not out:
        print("模型返回空。", file=sys.stderr)
        return 1
    if not dry:
        rp = ROOT / "reports" / f"{ym}.md"
        rp.parent.mkdir(parents=True, exist_ok=True)
        rp.write_text(
            f"# {ym} 月报\n\n> 生成于 {_now():%Y-%m-%d %H:%M}\n\n{out}\n",
            encoding="utf-8",
        )
        git_commit(f"month report {ym}")
        print(f"已写入 {rp}", file=sys.stderr)
    print(out)
    return 0


# ------------------------------------------------------------------ 看板渲染

C_R = "\033[0m"
C_DIM = "\033[38;5;242m"
C_FAINT = "\033[38;5;238m"
C_BOLD = "\033[1m"
C_GREEN = "\033[38;5;108m"
C_YEL = "\033[38;5;179m"
C_CYAN = "\033[38;5;73m"
C_VIOLET = "\033[38;5;141m"
C_WHITE = "\033[38;5;252m"


def dw(s: str) -> int:
    w = 0
    for c in s:
        if unicodedata.combining(c):
            continue
        w += 2 if unicodedata.east_asian_width(c) in "WF" else 1
    return w


def cut(s: str, width: int) -> str:
    if dw(s) <= width:
        return s
    out, w = [], 0
    for c in s:
        cw = 2 if unicodedata.east_asian_width(c) in "WF" else 1
        if w + cw > width - 1:
            break
        out.append(c)
        w += cw
    return "".join(out) + "…"


def pad(s: str, width: int) -> str:
    return s + " " * max(0, width - dw(s))


def board_state(d: date | None = None):
    d = d or today()
    st = {
        "date": d,
        "exists": False,
        "plan": [],
        "extra": [],
        "notes": 0,
        "last_note": "",
        "summaries": [],
        "tomorrow": [],
        "week": [],
    }
    p = day_path(d)
    if p.exists():
        st["exists"] = True
        _, secs = split_sections(p.read_text(encoding="utf-8"))
        st["plan"] = plan_items(get_section(secs, "计划"))
        st["extra"] = extra_items(get_section(secs, "计划外"))
        notes = note_items(get_section(secs, "速记"))
        st["notes"] = len(notes)
        if notes:
            st["last_note"] = f"{notes[-1]['time']} {notes[-1]['text']}"
        ss = summary_sections(secs)
        st["summaries"] = [n.split("·")[-1].strip() for n, _ in ss]
        if ss:
            st["tomorrow"] = subsection(ss[-1][1], "明日计划")

    today_text = {norm(t["text"]) for t in st["plan"]}
    seen = {}
    for dd, p in all_days():
        if dd > d or dd < d - timedelta(days=7) or dd == d:
            continue
        _, secs = split_sections(p.read_text(encoding="utf-8"))
        for t in plan_items(get_section(secs, "计划")):
            if t["done"] or norm(t["text"]) in today_text:
                continue
            k = norm(t["text"])
            if k not in seen:
                seen[k] = {"text": t["text"], "n": 0}
            seen[k]["n"] += 1
    st["week"] = sorted(seen.values(), key=lambda x: -x["n"])[:6]
    return st


CLOCK_FMT = "%H:%M:%S"
CLOCK_W = len("00:00:00")  # 8，纯 ASCII，正好和显示宽度一致


def clock_anchor(w: int):
    """时钟在贴图上的位置（1 基：行, 列）。太窄就不显示。"""
    if w < CLOCK_W + 12:
        return None
    return 1, w - CLOCK_W + 1


def clock_text() -> str:
    return _now().strftime(CLOCK_FMT)


def rule(w: int) -> str:
    return C_FAINT + " " + "─" * max(1, w - 2) + C_R


def build_frame(w: int, h: int):
    st = board_state()
    d = st["date"]
    lines = []

    done = sum(1 for t in st["plan"] if t["done"])
    total = len(st["plan"])
    head = f"{d:%m-%d} {WEEKDAYS[d.weekday()]}"
    if d == today() and is_late_night():
        head += " ·跨夜"   # 挂钟已经过了 0 点，但记录仍算在前一天
    cnt = f"{done}/{total}" if total else ""
    if st["extra"]:
        cnt = f"{cnt} +{len(st['extra'])}" if cnt else f"+{len(st['extra'])}"
    # 右端 8 列留给时钟（由主循环每秒原地重画，不进 frame，避免整屏重绘）
    left_txt = head + (("  " + cnt) if cnt else "")
    pad_n = max(1, w - 1 - dw(left_txt) - CLOCK_W)
    lines.append(" " + C_BOLD + head + C_R + ((("  " + C_DIM + cnt + C_R) if cnt else ""))
                 + " " * pad_n + " " * CLOCK_W)
    lines.append(rule(w))

    if not st["exists"] or (not st["plan"] and not st["extra"] and not st["notes"]):
        lines.append("")
        lines.append(f"  {C_DIM}今天还没有记录{C_R}")
        lines.append(f"  {C_DIM}prefix + Space 记一笔{C_R}")
        return _finish(lines, w, h)

    inner = w - 3  # 行首留 1 空格 + 标记 1 + 空格 1
    tomorrow = st["tomorrow"][:3]
    tail_rows = 2 + (len(st["week"]) + 3 if st["week"] else 0)
    budget = max(1, h - len(lines) - tail_rows - (len(tomorrow) + 2 if tomorrow else 0))

    rows = [("plan", t) for t in st["plan"]] + [("extra", t) for t in st["extra"]]
    for i, (kind, t) in enumerate(rows):
        if i >= budget:
            rest = len(rows) - i
            lines.append(f"  {C_DIM}… 还有 {rest} 项{C_R}")
            break
        if kind == "extra":
            mark = f"{C_VIOLET}◆{C_R}"
            col, tail, tw = C_DIM, "", 0
        else:
            mark = f"{C_GREEN}✔{C_R}" if t["done"] else f"{C_CYAN}○{C_R}"
            col = C_DIM if t["done"] else C_WHITE
            tail = f"{C_YEL}↺{t['carry']}{C_R}" if (not t["done"] and t["carry"]) else ""
            tw = dw(f"↺{t['carry']}") + 1 if tail else 0
        text = cut(t["text"], max(4, inner - tw))
        fill = max(0, inner - dw(text) - tw)
        lines.append(f" {mark} {col}{text}{C_R}" + " " * fill + ((" " + tail) if tail else ""))

    if tomorrow:
        lines.append("")
        lines.append(f" {C_DIM}明日{C_R}")
        for t in tomorrow:
            lines.append(f" {C_FAINT}·{C_R} {C_DIM}{cut(t, inner)}{C_R}")

    lines.append(rule(w))
    meta = f"{st['notes']} 条速记"
    if st["summaries"]:
        meta += f" · 总结 {len(st['summaries'])} 次 ({st['summaries'][-1]})"
    ai_text, ai_color = distill_status_text()
    if ai_text and w - 2 - dw(meta) - dw(ai_text) >= 2:
        gap = w - 2 - dw(meta) - dw(ai_text)
        lines.append(f" {C_DIM}{meta}{C_R}{' ' * gap}{ai_color}{ai_text}{C_R}")
    else:
        lines.append(f" {C_DIM}{cut(meta, w - 2)}{C_R}")
    if st["last_note"]:
        lines.append(f" {C_FAINT}{cut(st['last_note'], w - 2)}{C_R}")

    if st["week"]:
        lines.append(rule(w))
        lines.append(f" {C_DIM}本周在办{C_R}")
        room = max(0, h - len(lines) - 1)
        for it in st["week"][:room]:
            tail = f"{C_YEL}▸{it['n']}{C_R}"
            text = cut(it["text"], max(4, inner - 3))
            fill = max(1, inner - dw(text) - 2)
            lines.append(f" {C_FAINT}·{C_R} {C_DIM}{text}{C_R}" + " " * fill + tail)

    return _finish(lines, w, h)


def _finish(lines, w, h):
    out = []
    for ln in lines[:h]:
        vis = re.sub(r"\033\[[0-9;]*m", "", ln)
        if dw(vis) > w:
            ln = cut(vis, w)
        out.append(ln + "\033[K")
    while len(out) < h:
        out.append("\033[K")
    return out


def term_size():
    try:
        sz = shutil.get_terminal_size()
        return max(20, sz.columns), max(6, sz.lines)
    except Exception:
        return 42, 40


def _print_frame() -> int:
    """--once：打一帧到 stdout（非交互，调试用）。"""
    w, h = term_size()
    lines = [l.replace("\033[K", "") for l in build_frame(w, h)]
    if clock_anchor(w) and lines:
        lines[0] = lines[0][: len(lines[0]) - CLOCK_W] + clock_text()
    print("\n".join(lines))
    return 0


def cmd_board(once: bool) -> int:
    """只读看板：不接收键盘输入。

    两层刷新：
      · 内容层 —— 文件 mtime 变、窗口尺寸变，才整帧重画（避免闪烁）
      · 时钟层 —— 每秒只把右上角那 8 列原地重写，不动其它像素
    """
    if once:
        return _print_frame()

    state = {"resize": True}

    def on_winch(signum, frame):
        state["resize"] = True

    signal.signal(signal.SIGWINCH, on_winch)
    out = sys.stdout
    out.write("\033[?1049h\033[?25l")
    out.flush()

    last, last_sig = None, None
    size = (0, 0)
    drawn = {"sec": None}

    def paint_clock(force=False):
        nonlocal size
        anchor = clock_anchor(size[0])
        now = clock_text()
        if not anchor or (now == drawn["sec"] and not force):
            return
        drawn["sec"] = now
        row, col = anchor
        color = C_YEL if is_late_night() else C_DIM
        # 写满最后一列会让光标进入“待折行”状态，末尾回一次原点清掉
        out.write("\033[%d;%dH%s%s%s\033[H" % (row, col, color, now, C_R))
        out.flush()

    try:
        while True:
            d = today()
            p = day_path(d)
            try:
                sig = (p.stat().st_mtime_ns, p.parent.stat().st_mtime_ns)
            except OSError:
                sig = None

            if state["resize"] or sig != last_sig:
                state["resize"] = False
                last_sig = sig
                size = term_size()
                try:
                    frame = build_frame(*size)
                except Exception:
                    import traceback
                    frame = [" \033[38;5;203m看板渲染出错\033[0m "] + [
                        " " + ln for ln in traceback.format_exc().splitlines()[-6:]
                    ]
                if frame != last:
                    last = frame
                    out.write("\033[H" + "\r\n".join(frame))
                    out.flush()
                    paint_clock(force=True)

            paint_clock()
            time.sleep(0.25)
    finally:
        out.write("\033[?25h\033[?1049l")
        out.flush()
    return 0


# --------------------------------------------------------------------- 主入口


HELP = f"""j — 个人日志 / 日报

  j <一句话>              记一笔速记（后台自动整理计划）
  j +<任务>               加进 todolist（待办，会结转）
  j !<一句话>             记一条「计划外完成」（干完但没排的事）
  j -<编号|关键字>        切换某项 todo 的完成状态
  j -n <一句话>           记一笔，不动计划
  j plan <内容>           加一条计划项
  j done                  列出今日计划（带编号）
  j done <编号|关键字>    切换完成状态（就是改 todo 状态的方式）
  j list [--open]         列成 fzf 能吃的格式（给 j-toggle 用）
  j rm <序号> [--extra]   删掉一项
  j rename <序号> [新名]  改一项的文字
  j promote <序号> [文本] 从「计划外」衍生一条待办
  j log [-n N]            今天的速记（默认最近 10 条）
  j log -a [YYYY-MM]      跨天看速记
  j search <词>...        跨天搜速记（多个词是 AND）
  j day [YYYY-MM-DD]      打印某一天整篇
  j undo [n] [--dry]      撤销最近 n 次写入（默认 1 次）
  j status                今天概况 + AI 整理状态
  j backup [--push]       给数据做个 git bundle 快照（可顺带推私有远端）
  j start                 开工视图：上次没做完的 + 今天的现状
  j show                  打印今天整篇
  j edit                  用 $EDITOR 打开今天
  j report [--dry]        生成今日总结（可一天多次）
  j month [YYYY-MM]       生成月报
  j week [2026-W45]       生成周报（默认本周）
  j board                 渲染看板（j-board 调用）
  j path                  今天文件路径
  j distill               立刻跑一次计划整理
"""


def main(argv) -> int:
    if not argv:
        print(HELP)
        return 0
    cmd, rest = argv[0], argv[1:]

    if cmd in ("-h", "--help", "help"):
        print(HELP)
        return 0
    if cmd == "distill":
        return cmd_distill()
    if cmd == "plan":
        return cmd_plan(" ".join(rest))
    if cmd == "done":
        return cmd_done(" ".join(rest))
    if cmd in ("list", "ls"):
        return cmd_list(rest)
    if cmd == "undo":
        return cmd_undo(rest)
    if cmd in ("status", "st"):
        return cmd_status(rest)
    if cmd in ("start", "begin"):
        return cmd_start(rest)
    if cmd == "backup":
        return cmd_backup(rest)
    if cmd == "rm":
        return cmd_rm(rest)
    if cmd == "rename":
        return cmd_rename(rest)
    if cmd == "promote":
        return cmd_promote(rest)
    if cmd == "report":
        return cmd_report(dry="--dry" in rest)
    if cmd == "month":
        args = [a for a in rest if not a.startswith("-")]
        return cmd_month(args[0] if args else None, dry="--dry" in rest)
    if cmd == "week":
        args = [a for a in rest if not a.startswith("-")]
        return cmd_week(args[0] if args else None, dry="--dry" in rest)
    if cmd == "board":
        return cmd_board(once="--once" in rest)
    if cmd == "path":
        ensure_today(today())
        print(day_path(today()))
        return 0
    if cmd == "show":
        p, _, _ = load_day(today())
        if p:
            print(p.read_text(encoding="utf-8"), end="")
        else:
            print("今天还没有记录。", file=sys.stderr)
        return 0
    if cmd == "edit":
        p = ensure_today(today())
        return subprocess.call([os.environ.get("EDITOR", "nvim"), str(p)])
    if cmd == "log":
        return cmd_log(rest)
    if cmd in ("search", "s"):
        return cmd_search(" ".join(rest))
    if cmd == "notes":
        return cmd_notes()
    if cmd == "day":
        return cmd_day(rest[0] if rest else None)
    if cmd in ("rollover", "new"):
        with Lock():
            p = ensure_today(today())
        print(p)
        return 0

    ai, text = True, list(argv)
    if cmd == "-n":
        ai, text = False, rest
    return cmd_capture(" ".join(text), ai)


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        sys.exit(130)
