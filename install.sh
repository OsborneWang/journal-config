#!/usr/bin/env bash
# ==============================================================================
# install.sh —— 把本仓库的 journal 工具装到当前用户
#
# 默认（软链模式，推荐）：改完仓库里的文件立即生效，无需重装
#   ~/.local/bin/<脚本>              -> <repo>/bin/<脚本>
#   ~/.config/journal/config.sh      -> <repo>/config.sh
#   ~/.config/journal/chat.md        -> <repo>/chat.md
#   ~/.config/journal/prompts/       -> <repo>/prompts/
#   ~/.config/journal/README.md      -> <repo>/README.md
#   ~/.pi/agent/skills/journal/SKILL.md -> <repo>/skills/journal/SKILL.md
#
# 数据目录 ~/journal 不在这里管，脚本第一次运行时会自己建。
#
# 用法：
#   ./install.sh             软链安装（已存在的真实文件先备份）
#   ./install.sh --copy      复制安装（不建软链）
#   ./install.sh --dry-run   只打印动作
#   ./install.sh --force     已有真实文件直接覆盖，不备份
# ==============================================================================
set -euo pipefail

repo_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
mode="link"
dry_run=0
force=0

for arg in "$@"; do
	case "$arg" in
		--copy)    mode="copy" ;;
		--link)    mode="link" ;;
		--dry-run) dry_run=1 ;;
		--force)   force=1 ;;
		*) echo "未知参数: $arg" >&2; exit 2 ;;
	esac
done

run() {
	if [ "$dry_run" = 1 ]; then
		printf '  [dry] %s\n' "$*"
	else
		"$@"
	fi
}

# install_one <源> <目标>
install_one() {
	local src=$1 dst=$2
	mkdir -p "$(dirname "$dst")"
	if [ -L "$dst" ]; then
		run rm -f "$dst"
	elif [ -e "$dst" ]; then
		if [ "$force" = 1 ]; then
			run rm -rf "$dst"
		else
			local bak="$dst.bak-$(date +%Y%m%d-%H%M%S)"
			echo "  备份: $dst -> $bak"
			run mv "$dst" "$bak"
		fi
	fi
	if [ "$mode" = "link" ]; then
		run ln -s "$src" "$dst"
	else
		run cp -r "$src" "$dst"
	fi
	printf '  %-46s -> %s\n' "${dst/$HOME/~}" "$( [ "$mode" = link ] && echo "${src/$HOME/~}" || echo '(copy)')"
}

echo "仓库: $repo_dir"
echo "模式: $mode$([ "$dry_run" = 1 ] && echo ' (dry-run)')"
echo

echo "[1/3] 命令"
for f in "$repo_dir"/bin/*; do
	[ -f "$f" ] || continue
	install_one "$f" "$HOME/.local/bin/$(basename "$f")"
done

echo "[2/3] 配置与提示词"
install_one "$repo_dir/config.sh"      "$HOME/.config/journal/config.sh"
install_one "$repo_dir/chat.md"        "$HOME/.config/journal/chat.md"
install_one "$repo_dir/prompts"        "$HOME/.config/journal/prompts"
install_one "$repo_dir/README.md"      "$HOME/.config/journal/README.md"

echo "[3/3] pi skill"
install_one "$repo_dir/skills/journal/SKILL.md" "$HOME/.pi/agent/skills/journal/SKILL.md"

if [ "$mode" = "link" ]; then
	for f in "$repo_dir"/bin/*; do [ -f "$f" ] && chmod +x "$f"; done
fi

echo
echo "完成。自检: $repo_dir/check.sh"
[ "$dry_run" = 1 ] || echo "tmux 里要生效的话: tmux source-file ~/tmux-config/tmux.conf  （看板面板需 respawn）"
