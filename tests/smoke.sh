#!/usr/bin/env bash
# ==============================================================================
# tests/smoke.sh —— 离线回归测试（不调用模型、不碰真实数据、不依赖 tmux/fzf）
#
#   ./tests/smoke.sh          跑全部
#   ./tests/smoke.sh -v       失败项也打印上下文
#
# 覆盖：逻辑日 / 前缀语义 / 边界输入 / 结转 / 编辑 / 撤销 / 查询 / 渲染 / 备份 / 并发
# 目标：3 秒内跑完，改完代码随手跑一遍。
# ==============================================================================
set -uo pipefail

repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
export PATH="$HOME/.local/bin:$PATH"
JOURNAL_DIR=""
export JOURNAL_DIR
J="$HOME/.local/bin/j"
BASE=$(mktemp -d)
trap 'rm -rf "$BASE"' EXIT

pass=0 fail=0; failed=()
ok() { pass=$((pass+1)); printf '  \033[38;5;108m✓\033[0m %s\n' "$1"; }
no() { fail=$((fail+1)); failed+=("$1"); printf '  \033[38;5;203m✗\033[0m %s\n' "$1"; }
chk() { if eval "$2" >/dev/null 2>&1; then ok "$1"; else no "$1"; fi; }
grp() { printf '\n\033[1m%s\033[0m\n' "$1"; }
fresh() {  # 直接调用（不要用 $() 包，否则 export 留在子 shell 里）
	D=$(mktemp -d "$BASE/j.XXXXXX"); git -C "$D" init -q
	JOURNAL_DIR="$D"; export JOURNAL_DIR
}
f() { ls "$JOURNAL_DIR"/*/*/*.md 2>/dev/null | head -1; }

grp "1. 逻辑日（J_DAY_START，默认 04:00）"
fresh
env J_FAKE_NOW=2026-11-10T01:20:00 "$J" -n "熬夜写的" >/dev/null 2>&1
chk "01:20 记到前一天" "[ -f $JOURNAL_DIR/2026/11/2026-11-09.md ]"
chk "时间戳用挂钟时间 01:20" "grep -q '^- 01:20 熬夜写的' $JOURNAL_DIR/2026/11/2026-11-09.md"
fresh
env J_FAKE_NOW=2026-11-10T04:30:00 "$J" -n "早起" >/dev/null 2>&1
chk "04:30 属于新的一天" "[ -f $JOURNAL_DIR/2026/11/2026-11-10.md ]"
chk "非法 J_DAY_START 不崩" "D2=\$(mktemp -d); git -C \$D2 init -q; env JOURNAL_DIR=\$D2 J_DAY_START=xx J_FAKE_NOW=2026-11-10T01:00:00 $J -n x >/dev/null 2>&1; [ \$? -eq 0 ]"

grp "2. 采集前缀"
fresh
"$J" -n "普通速记一句" >/dev/null 2>&1
"$J" "+ 一条计划" >/dev/null 2>&1
"$J" "! 一条计划外" >/dev/null 2>&1
chk "普通进 ## 速记" "sed -n '/## 速记/,\$p' \$(f) | grep -q '普通速记一句'"
chk "+ 进 ## 计划（未勾选）" "sed -n '/## 计划/,/## 计划外/p' \$(f) | grep -q '^- \[ \] 一条计划'"
chk "! 进 ## 计划外" "sed -n '/## 计划外/,/## 速记/p' \$(f) | grep -q '一条计划外'"
"$J" "-1" >/dev/null 2>&1
chk "-1 勾选第 1 项" "sed -n '/## 计划/,/## 计划外/p' \$(f) | grep -q '^- \[x\] 一条计划'"
"$J" "- 一条计划" >/dev/null 2>&1
chk "- 关键字 取消勾选" "sed -n '/## 计划/,/## 计划外/p' \$(f) | grep -q '^- \[ \] 一条计划'"

grp "3. 边界输入"
rc=0; "$J" "" >/dev/null 2>&1 || rc=$?; chk "空串被拒绝" "[ $rc -eq 1 ]"
for p in "+" "!" "-"; do
	rc=0; "$J" "$p" >/dev/null 2>&1 || rc=$?
	chk "只有「$p」被拒绝" "[ $rc -eq 1 ]"
done
rc=0; "$J" "- 不存在的任务" >/dev/null 2>&1 || rc=$?; chk "找不到目标 rc=1" "[ $rc -eq 1 ]"
"$J" "$(printf '多行\t带tab\n换行')" >/dev/null 2>&1
chk "换行/tab 压成单行" "grep -q '多行 带tab 换行' \$(f)"
"$J" "emoji 🚀 和中文" >/dev/null 2>&1
chk "emoji 正常落盘" "grep -q '🚀' \$(f)"

grp "4. 跨天结转"
fresh; mkdir -p "$JOURNAL_DIR/2026/11"
printf '# d\n\n## 计划\n- [x] 做完的\n- [ ] 拖着的事 [↺2]\n\n## 速记\n' \
	> "$JOURNAL_DIR/2026/11/2026-11-09.md"
out=$(python3 -c "
import sys; sys.path.insert(0, '$repo'); import journal as J
from datetime import date
print(J.new_day_text(date(2026,11,10), J.carry_from_previous(date(2026,11,10))))")
chk "只带未完成的项" "echo \"\$out\" | grep -q '拖着的事' && ! echo \"\$out\" | grep -q '做完的'"
chk "↺2 结转后变 [↺3]" "echo \"\$out\" | grep -q '拖着的事 \[↺3\]'"
chk "正文末尾的 ↺3 不被吃掉" "python3 -c \"import sys; sys.path.insert(0,'$repo'); import journal as J; assert J.parse_task('- [ ] 修复 ↺3 问题')['carry']==0\"" 

grp "5. 编辑（rename / rm / promote）"
fresh
"$J" "+ 原名" >/dev/null 2>&1; "$J" "+ 第二条" >/dev/null 2>&1; "$J" "! 一条产出" >/dev/null 2>&1
chk "j list 是 类型+序号 三段式" "\"$J\" list | head -1 | grep -q \"^p$(printf '\t')1$(printf '\t')\""
"$J" rename 1 改过的名字 >/dev/null 2>&1
chk "rename 生效" "grep -q '改过的名字' \$(f) && ! grep -q '原名' \$(f)"
"$J" rm 2 >/dev/null 2>&1
chk "rm 生效" "! grep -q '第二条' \$(f)"
"$J" rm 1 --extra >/dev/null 2>&1
chk "rm --extra 生效" "! grep -q '一条产出' \$(f)"
"$J" "! 另一条产出" >/dev/null 2>&1
"$J" promote 1 衍生待办 >/dev/null 2>&1
chk "promote 加待办、保留产出" "grep -q '衍生待办' \$(f) && grep -q '另一条产出' \$(f)"

grp "6. 撤销"
fresh
"$J" -n "第一条" >/dev/null 2>&1; "$J" "+ 第二条" >/dev/null 2>&1; "$J" "! 第三条" >/dev/null 2>&1
"$J" undo >/dev/null 2>&1
chk "undo 撤掉最后一次" "! grep -q '第三条' \$(f)"
chk "undo 没动前面的" "grep -q '第二条' \$(f)"
chk "undo --dry 不改文件" "b=\$(md5sum \$(f)); \"$J\" undo --dry >/dev/null 2>&1; [ \"\$b\" = \"\$(md5sum \$(f))\" ]"

grp "7. 渲染"
fresh
for i in 1 2 3 4 5 6; do "$J" "+ 任务 $i：一条比较长的中文任务名用来测试截断行为" >/dev/null 2>&1; done
"$J" "! 计划外一件事" >/dev/null 2>&1; "$J" -n "最近一条" >/dev/null 2>&1
chk "35 种尺寸组合行数正确且不超宽" "python3 -c \"
import sys, re; sys.path.insert(0, '$repo'); import journal as J
for w in (24,30,42,54,80,120,200):
    for h in (5,6,10,24,51):
        fr = J.build_frame(w,h)
        assert len(fr)==h, (w,h)
        for ln in fr:
            vis = re.sub(r'\x1b\[[0-9;]*[A-Za-z]','', ln.replace('\x1b[K',''))
            assert J.dw(vis) <= w, (w,h,vis)
\""
chk "看板有 ◆ 计划外标记" "COLUMNS=54 LINES=20 \"$J\" board --once | grep -q '◆'"
chk "跨夜时段显示 ·跨夜" "env J_FAKE_NOW=2026-11-10T01:00:00 COLUMNS=54 LINES=12 \"$J\" board --once | head -1 | grep -q '跨夜'"

grp "8. 查询"
fresh
"$J" -n "甲记录：导出问题" >/dev/null 2>&1; "$J" -n "乙记录：权限问题" >/dev/null 2>&1
"$J" -n "丙记录：导出和权限都有" >/dev/null 2>&1
"$J" "+ 修导出" >/dev/null 2>&1; "$J" "! 顺手修了导出样式" >/dev/null 2>&1
chk "j log 只有速记" "[ \$(\"$J\" log | wc -l) -eq 3 ]"
chk "j search 覆盖速记/计划/计划外" "[ \$(\"$J\" search 导出 2>/dev/null | wc -l) -eq 4 ]"
chk "j search 多词是 AND" "[ \$(\"$J\" search 导出 权限 2>/dev/null | wc -l) -eq 1 ]"
chk "j day 打印指定日期" "\"$J\" day \$(date +%Y-%m-%d) | grep -q '# '"

grp "9. 备份"
fresh; export J_BACKUP_DIR="$BASE/bk"
"$J" -n "有内容才备份" >/dev/null 2>&1
chk "生成 bundle" "\"$J\" backup >/dev/null 2>&1 && [ \$(ls \"\$J_BACKUP_DIR\"/*.bundle | wc -l) -ge 1 ]"
chk "bundle 可校验" "b=\$(ls \"\$J_BACKUP_DIR\"/*.bundle | head -1); git -C \"\$JOURNAL_DIR\" bundle verify \"\$b\" >/dev/null 2>&1"
chk "空仓库备份给友好提示" "D2=\$(mktemp -d); git -C \$D2 init -q; JOURNAL_DIR=\$D2 J_BACKUP_DIR=\$BASE/bk2 \"$J\" backup | grep -q '还没有任何记录'"

grp "10. 并发与脏数据"
fresh
for i in $(seq 1 12); do "$J" -n "并发 $i" >/dev/null 2>&1 & done; wait
chk "12 路并发零丢失" "[ \$(sed -n '/## 速记/,\$p' \$(f) | grep -c '并发') -eq 12 ]"
printf '' > "$(f)"
chk "空文件可继续写入" "\"$J\" -n '写进空文件' >/dev/null 2>&1 && grep -q '写进空文件' \$(f)"
printf '# x\n\n## 速记\n- 09:00 旧\n\n## 计划\n- [ ] 顺序反了\n' > "$(f)"
chk "区块乱序也能读" "\"$J\" done | grep -q '顺序反了'"
printf '# y\r\n\r\n## 计划\r\n- [ ] CRLF 任务\r\n\r\n## 速记\r\n' > "$(f)"
chk "CRLF 能读且写回为 LF" "\"$J\" -n x >/dev/null 2>&1 && grep -q 'CRLF 任务' \$(f) && ! grep -q \$'\r' \$(f)"

printf '\n'
if [ "$fail" -eq 0 ]; then
	printf '\033[38;5;108m离线回归全部通过（%d 项）\033[0m\n' "$pass"
	exit 0
fi
printf '\033[38;5;203m%d 项失败 / 共 %d 项\033[0m\n' "$fail" "$((pass+fail))"
printf '  - %s\n' "${failed[@]}"
exit 1
