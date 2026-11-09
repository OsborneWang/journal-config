---
name: journal
description: The human's personal daily journal and todo list in ~/journal. Use it when they say they plan to do something today, report what they did, ask to add/check off a todo, want a daily summary, or ask what they did today/this month.
---

# journal：个人日志与今日看板

数据在 `~/journal`（一天一个 markdown 文件，git 管理）。`j` 命令是唯一入口，
在 PATH 里；不在就用 `/home/osborne/.local/bin/j`。

    j "<一句话>"        记一条速记（流水、结论、感想、阻塞都行）
    j -n "<一句话>"     记速记但不触发后台整理
    j "+<任务>"         新增今日计划项（等同 j plan，最快）
    j done              列出今日计划（带编号）
    j done <编号|关键字>  切换完成状态
    j log               看今天的速记
    j show              打印今天整篇
    j report            生成今日总结（一天可以生成多次，每次追加一条带时间的总结）
    j month [YYYY-MM]   生成月报，写到 ~/journal/reports/
    j path              今日文件路径

## 什么时候用它

- 用户说「今天打算做 X、Y」→ 拆成 `j plan`，一句一条，不超过 20 字。
- 用户说「我把 X 搞定了」→ `j "<原话>"`，如果今天计划里有对应项，再 `j done <编号>`。
- 用户问「我今天干了什么」→ `j show`，然后按需 `j report`。
- 用户要月报/复盘 → `j month`。

## 规矩

- 不要在 `~/journal` 里用 write/edit 直接改文件，一律走 `j`。速记原文永不改写。
- 计划项是动作短句，不是议题。含糊不清时只记速记，不建计划项。
- 记完一句话确认即可，不要复述全文，不要打官腔。
