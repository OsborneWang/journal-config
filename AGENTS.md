# AGENTS.md —— 在这个仓库里干活前先读

这是用户的**个人日志 / 今日看板 / 日报 / 月报**工具。一个人用，跑在他的 Ubuntu 上，
tmux 里左边一块常驻看板，右边干活。

## 怎么改、怎么生效

仓库就是安装源：`~/.local/bin/j*`、`~/.config/journal/*`、pi 的 skill 全是**软链**指向本仓库。
**改仓库里的文件立即生效，不用重装**。只有两种情况例外：

```sh
./install.sh            # 换了文件名、新增了启动器、或者软链坏了
./check.sh              # 改完必须跑：结构 + 语法 + 端到端（用临时目录，不碰真实数据）
```

看板是一个长驻进程，改了 `journal.py` 的渲染逻辑后，已经开着的看板不会自己重启：

```sh
tmux respawn-pane -k -t <window>.0 'exec ~/.local/bin/j-board'
```

看板有两种出现方式：`prefix J`（专门窗口，`bin/j-window`）、`prefix B`（在当前窗口临时插一块，
由 `bin/j-pane` 实现，靠 pane 选项 `@journal=1` 识别自己插的那块）。
注意 `after-split-window` 钩子会把布局重排成 main-vertical，所以 `j-pane` 插完要
`swap-pane` 到最左边再 `select-layout`；临时改过的 `main-pane-width` 记在
`@journal-prev-mainw` 里、收起时还原。

## 目录

```
journal.py            全部实现，单文件、零第三方依赖（python3 标准库）
bin/                  启动器（软链到 ~/.local/bin/）
  j                   主命令：采集 / 计划 / 日报 / 月报 / 查询
  j-board             看板渲染器（长驻，只读）
  j-pane              在当前窗口左侧插入/收起一块看板 pane（用 @journal 标记识别）
  j-toggle            prefix X：fzf 多选切换计划项完成状态
  j-window            prefix J：跳到 journal 窗口并保证有看板
  j-toggle            prefix X：fzf 管理计划（切换/改名/删除/衍生）
  j-act               j-toggle 里 fzf 动作的分发器（按选中行调用 j 子命令）
  j-start             prefix R：开工视图
  j-week-ui           prefix W：周报弹窗
tests/smoke.sh        41 项离线回归。**改完代码必须跑**，2 秒
eval/run.py           提示词评测。换模型/改提示词必须跑
  j-capture           tmux 速记弹窗（bash + read -e）
  j-browse            fzf 浏览全部速记
  j-chat              临时 pi 会话，把话变成记录
  j-report-ui         日报弹窗包装
  j-month-ui          月报弹窗包装
prompts/              三个提示词，AI 的全部行为都靠它
  distill.md          速记 → 计划项/计划外完成的 JSON 操作
  report.md           当日材料 → 日报三段
  month.md            当月材料 + 统计 → 月报四段
config.sh             配置（模型、仓库根、开关）。改这里不用动代码
                      ★ 本机私有的值（自己的 git 邮箱、本机专有路径）不要写进 config.sh，
                        写 ~/.config/journal/config.local.sh（已 gitignore，会覆盖同名项）
chat.md               临时 pi 会话的系统提示
skills/journal/       给普通 pi 会话用的 skill（软链到 ~/.pi/agent/skills/）
README.md             给用户看的说明书（软链到 ~/.config/journal/）
check.sh / install.sh
```

**不在本仓库**的三块：数据在 `~/journal`；tmux 键位和窗口布局在 `~/tmux-config/tmux.conf`；
本机私有配置在 `~/.config/journal/config.local.sh`。

**这个仓库是公开的**（github.com/OsborneWang/journal-config）。提交前别把个人邮箱、
内网地址、公司项目名写进来；配置里需要的话走 `config.local.sh`。

## 数据

`~/journal/` 是本地 git 仓库（不推远端），一天一个 markdown：

```
~/journal/2026/11/2026-11-09.md
~/journal/reports/2026-11.md      月报
```

```markdown
# 2026-11-09 周一

## 计划            - [ ] / - [x]，没做完的次日自动带过来并 ↺n
## 计划外          - 14:20 干完了但本来没排的事（不结转、不进完成率）
## 速记            - 09:12 一句话流水，只追加
## 总结 · 18:32    日报，可以有很多条（一天生成多次）
```

## 不变量（改之前先看，破了就是 bug）

0. **「今天」是逻辑日，不是日历日。** 一天从 `J_DAY_START`（默认 04:00）算起，
   凌晨 1 点记的东西属于前一天。所有取日期的地方都用 `today()`，
   **不要直接写 `date.today()`**；需要挂钟日期用 `real_today()`，
   需要当前时刻用 `_now()`（它支持 `J_FAKE_NOW` 便于测试）。

1. **速记只追加，任何代码和 AI 都不许改写已有速记的文字。**
2. **AI 的输出只落进固定槽位**：`## 计划` 的勾选/新增、`## 计划外`、`## 总结`。
   它永远不写速记、不改已有计划项的文字。
3. **所有数字由脚本统计，模型只写叙述。** 完成数、结转天数、提交数都算在 python 里，
   提示词里明确要求模型「直接用，不要自己数」。
4. **写文件必须持锁**：`with Lock():`（`fcntl.flock` 在 `~/journal/.lock`）。
   每个写操作都是「整篇读 → 改 → 整篇写」，不加锁会互相覆盖。
5. **看板只读**，永不写数据，也不接收键盘输入——它崩了不能影响数据。
6. **采集永不阻塞**：速记先落盘，再后台 `pi -p` 提炼。API 挂了那一条的提炼丢掉，但原文还在。
7. **判定只看最新一条速记**。曾经把「最近 3 条」一起丢给模型重判，导致旧速记被反复判、
   凭空多出假产出（「开了个站会」被反复判成一条完成项）。不要再改回去。

## 结转标记的坑

只认 `[↺3]` 这种方括号形式（`CARRY_RE`）。早期用过 ` ↺3` 裸格式，后来废弃了：
正文如果以「↺3」结尾，会被误吃成结转次数（真实踩过）。
**改标记格式要同时改 `parse_task` / `render_task` / `tests/smoke.sh` 第 4 组。**

## 三种记录，别混

| | 计划项 | 计划外完成 | 速记 |
|---|---|---|---|
| 含义 | 打算做的事 | 干完但本来没排的事 | 流水 |
| 前缀 | `+` | `!` | 无 |
| 状态 | `[ ]`/`[x]`，会结转 | 永远已完成，不结转 | 无 |
| 改状态 | `j done` / `prefix X` / `prefix e` | 不用改（要改就删行） | 永不改 |
| 影响完成率 | 会 | 不会，单独统计 | 不会 |
| 看板 | `○` / `✔` | `◆` | 底部条数 + 最新一条 |

## 设计取向

- 采集路径的摩擦必须接近零：`prefix Space` 一行回车即走，不切窗口。
- 用户能自己判断的事不要交给 AI 判（`+/!` 前缀是确定性的），
  AI 只做「你没说清的那部分」。
- 宁可少判不可错判：提炼用便宜的模型 + `--thinking off`，判不准就留空。
- 数字要能解释：`↺n` 是结转天数，月报的拖延榜直接用它。

## 验证方式

```sh
./tests/smoke.sh                             # 41 项离线回归，2 秒，改完必跑
./eval/run.py                                # 提示词评测（调模型）
./check.sh                                   # 全流程自检，用临时目录
JOURNAL_DIR=$(mktemp -d) ~/.local/bin/j show  # 手动隔离试验
COLUMNS=54 LINES=24 ~/.local/bin/j board --once   # 看一眼看板（非交互）
cat ~/journal/.distill.log                   # 后台提炼的每次结果都在这
```

## 已知坑（踩过的）

- **tmux 弹窗里中文退格**：必须 `read -e`（走 readline 整行重绘）。用裸 `read` 时
  内核按字节擦除、汉字只退一列，用户会觉得「按两下才删掉一个字」。
- **CJK 宽度**：看板对齐必须用 `unicodedata.east_asian_width` 算显示宽度，
  不能 `len()`（一个汉字占两列）。
- **右上角的时钟**：`build_frame` 只在头部留出 `CLOCK_W` 个空格占位，
  真正的时间由主循环用 `\033[row;colH` 原地重写。**不要把时间塞进 frame**——
  frame 每秒变一次就会触发整屏重绘，肉眼能看到闪。改完记得把写满最后一列后的
  “待折行”状态清掉（末尾补 `\033[H`），否则下一次输出会顶到下一行。
- **并发 distill**：多个后台提炼同时写会互相覆盖，已用 `~/journal/.distill.lock`
  串行化（非阻塞排队，最多等 20 秒）。
- **git 证据重复**：`/WorkSpace` 和 `/ShareSpace` 下有同一份代码的多份克隆，
  采集时按 commit hash 全局去重。
- **别用工具自己的环境变量名当局部变量**：脚本里把变量叫 `TMUX` 会让 tmux CLI 连到
  `/usr/bin/tmux` 这个"socket"，叫 `LESS` 会被 less 当成默认选项解析。
  已经踩过两次（`T=`、`PAGER_BIN=`）。同理别用 `EDITOR`、`PAGER`、`HOME`。
- **pi 调用要隔离**：`pi -p --no-session --no-tools --no-skills --no-prompt-templates
  --no-context-files --no-extensions`，否则会加载用户全部扩展，慢且不可控。
  还要显式 `--thinking`（用户全局设的是 high，会把 2 秒的调用拖成 13 秒）。
