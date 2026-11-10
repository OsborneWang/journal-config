# journal 配置（被 j 脚本 source 解析，值为 KEY=VALUE 形式）

# 数据目录（本地 git 仓库，不推远端）
JOURNAL_DIR="$HOME/journal"

# pi 可执行文件
J_PI="$HOME/.local/bin/pi"

# 速记后的后台整理用便宜快的模型
J_MODEL_CAPTURE="cn:glm-5.3-flash"

# 日报 / 月报用的模型
J_MODEL_REPORT="cn:deepseek-v4.1-flash"

# 1 = 每次速记后后台自动整理计划；0 = 关闭（也可临时用 j -n）
J_AI_CAPTURE=1

# 机器视角：在这些根目录下自动发现 git 仓库
J_REPO_ROOTS="/WorkSpace /ShareSpace"

# 机器视角里 git 提交按作者过滤（留空 = 不过滤）
# 本机自己的邮箱别写在这里（这是公开仓库），写进 ~/.config/journal/config.local.sh
J_GIT_AUTHOR=""

# 思考档位（off/low/medium/high）。速记整理用 off 求快，报告用 low
J_THINK_CAPTURE="off"
J_THINK_REPORT="low"

# 一天从几点开始算（凌晨写日志的人设成 04:00，避免熬夜的记录跑到第二天）
J_DAY_START="04:00"

# 备份：git bundle 存到哪、留几份；J_BACKUP_REMOTE 填了才能 --push
# 远端建议用私有仓库（日志含工作细节，别用公开仓库）
J_BACKUP_DIR="$HOME/journal-backups"
J_BACKUP_KEEP="7"
J_BACKUP_REMOTE=""
