#!/usr/bin/env bash
# ==============================================================================
# check.sh —— 本地自检：不碰你的真实数据（用临时 JOURNAL_DIR 跑）
#
#   ./check.sh            结构 + 语法 + 渲染 + 增删改全流程
#   ./check.sh --model    再跑一次真实的模型调用（会产生费用）
# ==============================================================================
set -uo pipefail

repo_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
home_bin="$HOME/.local/bin"
conf="$HOME/.config/journal"
fail=0
with_model=0
[ "${1:-}" = "--model" ] && with_model=1

ok()   { printf '  \033[38;5;108m✓\033[0m %s\n' "$1"; }
bad()  { printf '  \033[38;5;203m✗\033[0m %s\n' "$1"; fail=$((fail+1)); }
head2() { printf '\n\033[1m%s\033[0m\n' "$1"; }

head2 "1. 文件"
for f in journal.py install.sh check.sh config.sh chat.md README.md AGENTS.md \
         prompts/distill.md prompts/report.md prompts/month.md prompts/week.md \
         bin/j bin/j-board bin/j-pane bin/j-window bin/j-toggle bin/j-act bin/j-start \
         bin/j-capture bin/j-chat bin/j-browse bin/j-report-ui bin/j-month-ui \
         eval/run.py tests/smoke.sh skills/journal/SKILL.md; do
	[ -e "$repo_dir/$f" ] && ok "$f" || bad "缺少 $f"
done

head2 "2. 语法"
python3 -m py_compile "$repo_dir/journal.py" 2>/dev/null && ok "journal.py 编译通过" || bad "journal.py 语法错误"
for f in bin/j bin/j-board; do
	python3 -m py_compile "$repo_dir/$f" 2>/dev/null && ok "$f 编译通过" || bad "$f 语法错误"
done
bash -n "$repo_dir/bin/j-capture" && ok "j-capture 语法通过" || bad "j-capture 语法错误"
bash -n "$repo_dir/bin/j-browse"  && ok "j-browse 语法通过"  || bad "j-browse 语法错误"
bash -n "$repo_dir/bin/j-pane"    && ok "j-pane 语法通过"    || bad "j-pane 语法错误"
bash -n "$repo_dir/bin/j-toggle"  && ok "j-toggle 语法通过"  || bad "j-toggle 语法错误"

head2 "3. 安装状态（软链指向本仓库）"
for f in "$repo_dir"/bin/*; do
	[ -f "$f" ] || continue
	dst="$home_bin/$(basename "$f")"
	if [ -L "$dst" ] && [ "$(readlink -f "$dst")" = "$(readlink -f "$f")" ]; then
		ok "~/.local/bin/$(basename "$f")"
	else
		bad "~/.local/bin/$(basename "$f") 未指向本仓库（跑 ./install.sh）"
	fi
done
for pair in "config.sh:$conf/config.sh" "prompts:$conf/prompts" \
            "skills/journal/SKILL.md:$HOME/.pi/agent/skills/journal/SKILL.md"; do
	src="${pair%%:*}"; dst="${pair##*:}"
	[ "$(readlink -f "$repo_dir/$src")" = "$(readlink -f "$dst" 2>/dev/null)" ] \
		&& ok "${dst/$HOME/~}" || bad "${dst/$HOME/~} 未指向本仓库"
done

head2 "4. 端到端（临时目录，不碰真实数据）"
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
git -C "$tmp" init -q
export JOURNAL_DIR="$tmp"
export REPO="$repo_dir"
J="$home_bin/j"

"$J" "+ 测试计划项" >/dev/null 2>&1 && ok "加计划项" || bad "加计划项"
"$J" -n "一条测试速记" >/dev/null 2>&1 && ok "记速记" || bad "记速记"
"$J" "! 一条计划外完成" >/dev/null 2>&1 && ok "记计划外完成" || bad "记计划外完成"
"$J" done 1 >/dev/null 2>&1 && ok "勾选计划项" || bad "勾选计划项"

day_file=$(find "$tmp" -name '2026-*.md' | head -1)
grep -q "^## 计划外" "$day_file" && ok "文件里有 ## 计划外 区块" || bad "缺 ## 计划外 区块"
grep -q "^## 速记"   "$day_file" && ok "文件里有 ## 速记 区块"   || bad "缺 ## 速记 区块"
[ "$(git -C "$tmp" log --oneline | wc -l)" -ge 4 ] && ok "每次写入都自动 commit" || bad "没有自动 commit"

out=$(COLUMNS=54 LINES=24 "$J" board --once 2>&1)
echo "$out" | grep -q "速记" && ok "看板渲染" || bad "看板渲染"
echo "$out" | grep -q "◆"    && ok "看板显示计划外 ◆" || bad "看板没显示 ◆"
if echo "$out" | grep -q "↺"; then ok "看板显示结转 ↺"; else echo "  · 跳过结转显示（这个临时场景里没有结转项）"; fi

head2 "4b. 结转标记（回归）"
python3 -c "import sys,os; sys.path.insert(0, os.environ['REPO']); import journal as J; assert J.parse_task('- [ ] 正文末尾 ↺3')['carry']==0; assert J.parse_task('- [ ] 任务 [↺4]')['carry']==4; assert J.render_task('任务',4,False)=='- [ ] 任务 [↺4]'; assert J.render_task('任务',0,True)=='- [x] 任务'" && ok "正文里的 ↺3 不吃正文、[↺n] 正常" || bad "结转标记解析"

head2 "4c. 离线回归（tests/smoke.sh）"
if "$repo_dir/tests/smoke.sh" >/tmp/check-smoke.$$ 2>&1; then
	ok "$(tail -1 /tmp/check-smoke.$$ | sed 's/\x1b\[[0-9;]*m//g')"
else
	bad "tests/smoke.sh 有失败项"
	sed 's/^/     /' /tmp/check-smoke.$$ | tail -20
fi
rm -f /tmp/check-smoke.$$

head2 "5. 模型（可选）"
if [ "$with_model" = 1 ]; then
	out=$("$J" distill 2>&1)
	grep -qE "\[[0-9:]+\]" <<<"$out" && ok "distill 有日志输出" || bad "distill 无日志"
	if "$repo_dir/eval/run.py" >/tmp/check-eval.$$ 2>&1; then
		ok "$(tail -1 /tmp/check-eval.$$)"
	else
		bad "提示词评测有失败"
		sed 's/^/     /' /tmp/check-eval.$$ | tail -20
	fi
	rm -f /tmp/check-eval.$$
else
	echo "  跳过（--model 开启）"
fi

echo
if [ "$fail" = 0 ]; then
	printf '\033[38;5;108m全部通过\033[0m\n'; exit 0
else
	printf '\033[38;5;203m%d 项失败\033[0m\n' "$fail"; exit 1
fi
