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


# ---------------------------------------------------------------- 文件与行解析

SECTION_RE = re.compile(r"^##\s+(.*?)\s*$")
TASK_RE = re.compile(r"^\s*- \[([ xX])\]\s*(.*?)\s*$")
CARRY_RE = re.compile(r"\s*↺(\d+)\s*$")
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
    return "- [%s] %s%s" % ("x" if done else " ", text, (" ↺%d" % carry if carry else ""))


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
    for ln in body or []:
        m = EXTRA_RE.match(ln)
        if m:
            out.append({"time": m.group(1) or "", "text": m.group(2).strip()})
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
    d = date.today()
    # 开头的符号决定去向，决定权永远在你手里：
    #   +  → 计划项（待办，会结转）
    #   !  → 计划外完成（干完了、但本来没排的事）
    #   其他 → 速记，后台 AI 再判断是否影响计划
    mode = "note"
    if text.startswith(("+", "＋")):
        mode, text = "plan", text.lstrip("+＋ ").strip()
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
        elif mode == "extra":
            get_section(secs, "计划外", create=True).append(f"- {datetime.now():%H:%M} {text}")
            save_day(d, pre, secs, f"extra {d}: {text[:50]}")
        else:
            get_section(secs, "速记", create=True).append(f"- {datetime.now():%H:%M} {text}")
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
    d = date.today()
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
        return 0
    stamp = f"{datetime.now():%H:%M:%S}"
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
    else:
        raw = " ".join(out.split())[:120]
        print(f"[{stamp}] 无变化 · 模型返回 {raw}", file=sys.stderr)
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
    d = date.today()
    with Lock():
        ensure_today(d)
        p, pre, secs = load_day(d)
        get_section(secs, "计划", create=True).append(render_task(text, 0, False))
        save_day(d, pre, secs, f"plan {d}: + {text[:50]}")
    print(f"+ {text}")
    return 0


def cmd_done(key: str) -> int:
    d = date.today()
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
    d = date.today()
    with Lock():
        ensure_today(d)
    p, pre, secs = load_day(d)
    plan = plan_items(get_section(secs, "计划"))
    notes = note_items(get_section(secs, "速记"))
    if not plan and not notes:
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
    stamp = f"{datetime.now():%H:%M}"
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


def month_material(ym: str) -> str:
    start, end, days = collect_month(ym)
    total = done = nsum = nextra = 0
    per_day_done = []
    delay = {}
    body = []
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
        f"月份: {ym}",
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
    mv = machine_view(f"{start} 00:00", f"{end} 23:59")
    tail = ["", "机器证据:", mv] if mv else []
    return "\n".join(head) + "\n".join(body) + "\n".join(tail)


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

    items = day_notes(date.today())
    if not items:
        print("今天还没有速记。j \"<一句话>\" 记一笔。", file=sys.stderr)
        return 0
    for n in items[-limit:]:
        print(f"{n['time']}  {n['text']}")
    return 0


def cmd_search(words: str) -> int:
    """跨天搜速记内容，多个词是 AND。"""
    if not words.strip():
        print("用法: j search <关键词> [关键词...]", file=sys.stderr)
        return 1
    keys = [w.lower() for w in words.split()]
    hit = 0
    for dd, _ in all_days():
        for n in day_notes(dd):
            low = n["text"].lower()
            if all(k in low for k in keys):
                hit += 1
                print(f"{dd:%Y-%m-%d} {n['time']}  {n['text']}")
    if not hit:
        print(f"没找到含「{words}」的速记。", file=sys.stderr)
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
        arg = f"{date.today():%Y-%m-%d}"
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


def cmd_month(ym: str | None, dry: bool) -> int:
    ym = ym or f"{date.today():%Y-%m}"
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
            f"# {ym} 月报\n\n> 生成于 {datetime.now():%Y-%m-%d %H:%M}\n\n{out}\n",
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
    d = d or date.today()
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


def rule(w: int) -> str:
    return C_FAINT + " " + "─" * max(1, w - 2) + C_R


def build_frame(w: int, h: int):
    st = board_state()
    d = st["date"]
    lines = []

    done = sum(1 for t in st["plan"] if t["done"])
    total = len(st["plan"])
    head = f"{d:%m-%d} {WEEKDAYS[d.weekday()]}"
    cnt = f"{done}/{total}" if total else ""
    if st["extra"]:
        cnt = f"{cnt} +{len(st['extra'])}" if cnt else f"+{len(st['extra'])}"
    gap = max(1, w - 2 - dw(head) - dw(cnt))
    lines.append(" " + C_BOLD + head + C_R + " " * gap + C_DIM + cnt + C_R)
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
    w, h = term_size()
    print("\n".join(l.replace("\033[K", "") for l in build_frame(w, h)))
    return 0


def cmd_board(once: bool) -> int:
    """只读看板：不接收键盘输入，只在文件变化或窗口尺寸变化时重绘。"""
    if once:
        return _print_frame()

    def on_winch(signum, frame):
        state["resize"] = True

    state = {"resize": True}
    signal.signal(signal.SIGWINCH, on_winch)
    out = sys.stdout
    out.write("\033[?1049h\033[?25l")
    out.flush()
    last, last_sig = None, None
    try:
        while True:
            d = date.today()
            p = day_path(d)
            try:
                sig = (p.stat().st_mtime_ns, p.parent.stat().st_mtime_ns)
            except OSError:
                sig = None
            if state["resize"] or sig != last_sig:
                state["resize"] = False
                last_sig = sig
                w, h = term_size()
                try:
                    frame = build_frame(w, h)
                except Exception:
                    import traceback
                    frame = [" [38;5;203m看板渲染出错[0m "] + \
                            [" " + l for l in traceback.format_exc().splitlines()[-6:]]
                if frame != last:
                    last = frame
                    out.write("\033[H" + "\r\n".join(frame))
                    out.flush()
            time.sleep(0.5)
    finally:
        out.write("\033[?25h\033[?1049l")
        out.flush()
    return 0


# --------------------------------------------------------------------- 主入口


HELP = f"""j — 个人日志 / 日报

  j <一句话>              记一笔速记（后台自动整理计划）
  j +<任务>               加进 todolist（待办，会结转）
  j !<一句话>             记一条「计划外完成」（干完但没排的事）
  j -n <一句话>           记一笔，不动计划
  j plan <内容>           加一条计划项
  j done                  列出今日计划（带编号）
  j done <编号|关键字>    切换完成状态
  j log [-n N]            今天的速记（默认最近 10 条）
  j log -a [YYYY-MM]      跨天看速记
  j search <词>...        跨天搜速记（多个词是 AND）
  j day [YYYY-MM-DD]      打印某一天整篇
  j show                  打印今天整篇
  j edit                  用 $EDITOR 打开今天
  j report [--dry]        生成今日总结（可一天多次）
  j month [YYYY-MM]       生成月报
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
    if cmd == "report":
        return cmd_report(dry="--dry" in rest)
    if cmd == "month":
        args = [a for a in rest if not a.startswith("-")]
        return cmd_month(args[0] if args else None, dry="--dry" in rest)
    if cmd == "board":
        return cmd_board(once="--once" in rest)
    if cmd == "path":
        ensure_today(date.today())
        print(day_path(date.today()))
        return 0
    if cmd == "show":
        p, _, _ = load_day(date.today())
        if p:
            print(p.read_text(encoding="utf-8"), end="")
        else:
            print("今天还没有记录。", file=sys.stderr)
        return 0
    if cmd == "edit":
        p = ensure_today(date.today())
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
            p = ensure_today(date.today())
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
