#!/usr/bin/env python3
"""提示词回归评测。

换模型、改提示词、厂商更新模型之后跑一遍，防止 AI 行为悄悄退化。

    ./eval/run.py              跑全部用例
    ./eval/run.py -v           打印模型原始输出
    ./eval/run.py 03-done      只跑名字里含 03-done 的用例

用例文件格式（eval/cases/*.md）：

    # 说明
    prompt: distill.md      # prompts/ 下的哪个提示词
    expect: 必须出现的子串   # 可多行
    expect_not: 不许出现    # 可多行，可选
    ---
    喂给模型的材料（原样）
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import journal as J  # noqa: E402


def parse_case(path: Path):
    meta, material, in_body = {"expect": [], "expect_not": []}, [], False
    for line in path.read_text(encoding="utf-8").splitlines():
        if not in_body:
            if line.strip() == "---":
                in_body = True
                continue
            if ":" in line and not line.startswith("#"):
                k, v = line.split(":", 1)
                k, v = k.strip(), v.strip()
                if k in ("expect", "expect_not"):
                    meta[k].append(v)
                else:
                    meta[k] = v
        else:
            material.append(line)
    return meta, "\n".join(material)


def main(argv):
    verbose = "-v" in argv or "--verbose" in argv
    needle = next((a for a in argv if not a.startswith("-")), "")
    cases = sorted((HERE / "cases").glob("*.md"))
    if needle:
        cases = [c for c in cases if needle in c.name]
    if not cases:
        print("没找到用例")
        return 1

    passed = failed = 0
    for path in cases:
        meta, material = parse_case(path)
        prompt = meta.get("prompt", "distill.md")
        name = path.stem
        try:
            out = J.pi_call(prompt, material, J.CFG["J_MODEL_CAPTURE"], timeout=180,
                            thinking=J.CFG.get("J_THINK_CAPTURE", "off"))
        except Exception as e:
            print(f"  \033[38;5;203mERROR\033[0m {name}: {e}")
            failed += 1
            continue

        missing = [s for s in meta["expect"] if s not in out]
        dirty = [s for s in meta["expect_not"] if s in out]
        if missing or dirty:
            failed += 1
            print(f"  \033[38;5;203mFAIL\033[0m {name}")
            for s in missing:
                print(f"        缺少: {s}")
            for s in dirty:
                print(f"        不该出现: {s}")
        else:
            passed += 1
            print(f"  \033[38;5;108mPASS\033[0m {name}")
        if verbose or missing or dirty:
            print("        ── 模型输出 ──")
            for ln in (out or "(空)").splitlines()[:8]:
                print("        " + ln)

    print(f"\n提示词评测: {passed} 通过 / {failed} 失败")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
