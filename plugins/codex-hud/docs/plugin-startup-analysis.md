# Codex HUD 启动方式设计记录

> 状态：设计记录，暂不实现
> 首次审计：2026-08-07
> 当前环境：Codex CLI 0.147.0，codex-hud 0.3.16（cachebuster 在安装时生成）
> 范围：分析完整 HUD（基础 8 行，随运行中子代理与计划/历史动态扩展至最多 24 行）的启动入口、降级形态与演进方向

## 1. 决策摘要

Codex HUD 已经能够作为 Codex 插件安装、启用并运行 Hook/Skill，但当前 Codex 插件接口没有观察到 pre-launch wrapper 或插件可写的多行 TUI surface。

完整 HUD 必须在 Codex 启动前完成参数注入和终端编排，因此现阶段仍应保持：

```text
Codex plugin
  ├─ marketplace / manifest / cache
  ├─ lifecycle hooks
  └─ skill instructions
            │
            ▼
pre-launch owner
  ├─ 注入 CODEX_HUD_RUN_ID
  ├─ 注入 --no-alt-screen
  ├─ 创建 Ghostty split 或 tmux pane
  ├─ 启动 Codex
  └─ 负责信号、退出与异常清理
```

`codex-hud run` 只是当前 pre-launch owner 的命令形式，不是唯一可能的用户入口。后续可以改变“如何触发 wrapper”，但不能轻易移除 wrapper 所承担的时序和生命周期职责。

当前推荐顺序：

1. 让 `codex-hud` 默认直接启动，`run` 保留为兼容别名。
2. 提供可选的 Shell function，让交互式 `codex` 转发到 HUD launcher。
3. 提供 Ghostty Profile、快捷键和 tmux new-window 模板。
4. 研究由插件 Skill 打开独立新终端，而不是在当前 thread 内嵌套启动。
5. 将 SessionStart 自动分屏限制为 tmux-only 实验，不作为跨终端主方案。
6. 不默认安装 PATH 同名 shim，不从当前 Codex thread 内启动嵌套交互会话。

## 2. 已确认的现状

| 证据 | 结果 | 含义 |
|---|---|---|
| `codex plugin list` | `codex-hud@codex-hud-local` installed, enabled | 插件安装链路成立 |
| 插件版本 | `0.3.16+codex.<timestamp>` | 当前源码与缓存刷新后使用的版本格式 |
| Codex 配置 | `[plugins."codex-hud@codex-hud-local"] enabled = true` | 插件在当前 `CODEX_HOME` 启用 |
| Hook trust state | SessionStart、Pre/PostToolUse、Subagent、Compact、Stop 等均有 trusted hash | 生命周期 Hook 已被 Codex 接纳 |
| 命令入口 | `~/.local/bin/codex-hud` 指向源码 wrapper | 当前 CLI 入口由安装脚本 symlink 提供 |
| 插件 manifest | 声明 Skill 和交互元数据，没有 pre-launch/bin/TUI surface 字段 | manifest 本身不负责替代 `codex` 启动 |
| `hud_hook.py` | 只有 HUD enabled 且存在 `CODEX_HUD_RUN_ID` 时才采集 | 普通 `codex` 会话被刻意排除 |
| wrapper | 启动 Codex 前注入 `--no-alt-screen` 与 run ID | 这些职责无法在事后的 SessionStart Hook 中补做 |
| Codex CLI | `--no-alt-screen` 是启动参数；plugin 子命令只有 add/list/marketplace/remove | 暂无插件 launch 子命令或 pre-launch 扩展点 |

## 3. 完整 HUD 的硬约束

任何声称可以替代 `codex-hud run` 的方案，都需要逐项满足以下条件。

### 3.1 启动时序

必须在 Codex TUI 创建前注入：

- `--no-alt-screen`，保留承载终端 scrollback；
- `CODEX_HUD_RUN_ID`，把 Hook 事件绑定到唯一 HUD run；
- 当前 `CODEX_HOME`、cwd 和 PATH。

SessionStart Hook 发生得太晚，不能修改已经启动的父进程参数。

### 3.2 终端识别

启动器必须识别：

- 是否为 macOS；
- `TERM_PROGRAM=ghostty`；
- stdin/stdout 是否为真实 TTY；
- 是否已在 tmux 内；
- 当前 `TMUX_PANE`；
- standalone Ghostty 中准确的调用 surface。

Ghostty 多窗口环境不能依赖“当前 front window”猜测目标 terminal。

### 3.3 surface 生命周期

启动器需要：

- 创建并校准基础 8 行、按运行中子代理、未完成计划和最近完成项动态扩展至最多 24 行的 HUD surface；
- 将 Ghostty HUD surface 设为 read-only；
- 防止 tmux HUD pane 抢焦点或错误进入 copy-mode；
- 处理 Codex 正常退出、detach、TERM/HUP、启动失败和超时；
- 只关闭属于当前 run 的 surface。

### 3.4 参数透传

需要正确处理：

- 全局选项，例如 `-C`、`--search`、`--remote`、模型和审批策略；
- 交互子命令，例如 `resume` 和 `fork`；
- 非交互子命令，例如 `exec`、`review`、`plugin`、`completion` 和 `app-server`；
- prompt、图片参数和未知未来参数。

### 3.5 可退出性

所有透明入口必须有明确 bypass，例如：

- `command codex`；
- `CODEX_HUD_DISABLE=1`；
- `codex --no-hud` 或等价设计；
- 卸载后恢复原始命令解析。

## 4. 启动目标的三种含义

### 目标 A：通过 Codex 插件安装和管理

已实现。marketplace、manifest、cache、enable、Hook 和 Skill 都正常。

### 目标 B：通过插件对话触发一个新的 HUD 会话

部分可行。Skill 可以管理 `on`、`off`、`status`，也可以在未来指导或请求打开新的外部终端。

关键限制是不能在当前 Codex TTY 内嵌套启动另一个交互式 Codex。

### 目标 C：用户只输入普通 `codex`，自动得到完整 HUD

Codex 插件本身当前不能直接实现。可以通过 Shell function、PATH shim 或终端 profile 获得近似体验，但底层仍然需要 pre-launch wrapper。

## 5. 方案总览

| 方案 | 完整 HUD | 透明度 | 跨终端 | 风险 | 推荐等级 |
|---|---:|---:|---:|---:|---|
| 当前 `codex-hud run` | 是 | 低 | 高 | 低 | 基线 |
| `codex-hud` 默认直接启动 | 是 | 中 | 高 | 低 | P0 |
| `codex-hud start/launch` | 是 | 中 | 高 | 低 | P1 |
| Shell function 包装 `codex` | 是 | 高 | 中 | 中 | P0，可选 |
| PATH shim 覆盖 `codex` | 是 | 最高 | 高 | 高 | P3 |
| Ghostty Profile / 快捷键 | 是 | 中高 | 低 | 低 | P1 |
| tmux new-window / 快捷键 | 是 | 中高 | 低 | 低 | P1 |
| 插件 Skill 打开外部终端 | 是 | 高 | 低至中 | 中 | P2 |
| SessionStart Hook 自动分屏 | 部分 | 最高 | 低 | 高 | P3，实验 |
| Codex 原生单行 status line | 否，降级 | 最高 | 高 | 低 | P2，compact |
| App Server / daemon dashboard | 否，独立 UI | 高 | 高 | 中高 | 独立方向 |
| `codex exec` 一次性摘要 | 否，headless | 中 | 高 | 低 | 独立方向 |

## 6. 方案详解

### 6.1 `codex-hud` 默认直接启动

目标体验：

```text
codex-hud
codex-hud -C /path/to/repo --search
codex-hud resume --last
codex-hud fork --last
```

设计要点：

- 无子命令时直接进入当前 launch 流程；
- `run` 继续作为兼容别名；
- `on`、`off`、`toggle`、`status` 保持管理子命令；
- 使用 `--` 解决 Codex 参数与 HUD 管理子命令的歧义。

优点：

- 复用全部现有启动、终端识别和 cleanup 逻辑；
- 不影响真实 `codex`；
- 安装、排障和卸载边界清晰；
- 实现和迁移风险最低。

缺点：

- 用户仍需记住 `codex-hud`；
- `on/off/status` 与未来 Codex 同名 prompt/参数需要稳定解析规则。

判断：首选入口优化。它改变的是 CLI UX，不改变架构。

### 6.2 `codex-hud start` 或 `codex-hud launch`

目标是把 `run` 换成更容易理解的动词。

优点：

- 语义明确；
- 可保留 `run` 兼容；
- 对文档和 shell completion 友好。

缺点：

- 仍然是两个词；
- 相比默认直接启动，用户体验提升有限。

判断：可以作为别名，但不应成为唯一的新入口。

### 6.3 Shell function 包装普通 `codex`

目标体验：

```text
codex
codex -C repo
codex resume --last
codex fork --last
```

Shell function 只拦截交互式 TUI 启动：

- 普通交互启动、`resume`、`fork` 转发到 HUD launcher；
- `exec`、`review`、`plugin`、`login`、`completion`、`mcp-server`、`app-server` 等直接调用真实 Codex；
- stdin/stdout 不是 TTY 时直接 bypass；
- 提供 `command codex` 和环境变量 bypass。

优点：

- 用户体验最接近原生 `codex`；
- 只影响交互 Shell，容易删除；
- function 不会自动导出给 wrapper 的子进程，递归风险低于 PATH shim；
- 可按 zsh/bash/fish 分别提供模板。

风险：

- Codex 全局选项可能出现在子命令前，不能只检查第一个参数；
- Codex 新增子命令后，分类表可能漂移；
- shell completion 需要继续调用 Codex completion；
- 脚本显式启动交互 Codex 时行为可能与预期不同。

判断：推荐作为显式 opt-in，不应由插件安装过程静默写入 shell 配置。

### 6.4 PATH shim 覆盖 `codex`

在 PATH 前部安装一个同名 `codex` 可执行文件，再定位真实 Codex。

优点：

- 对所有 Shell、IDE 和终端透明；
- 可以统一处理交互入口。

风险：

- 会拦截 `codex plugin`、`codex update`、`codex completion` 和自动化脚本；
- 必须可靠定位真实二进制并处理 Codex 升级或路径变化；
- 必须有递归保护、bypass 和卸载恢复；
- 一旦 shim 故障，用户可能无法直接运行 Codex；
- 插件 manifest 当前没有观察到安全声明或安装这种同名命令的字段。

判断：不建议默认采用。若研究，只能作为高级用户明确选择的模式。

### 6.5 Ghostty Profile、快捷键或 macOS Launcher

可设计以下入口：

- Ghostty “Codex HUD” Profile；
- Ghostty 快捷键打开新 window/tab 后执行 HUD launcher；
- Dock、Spotlight、Automator 或独立 .app；
- Finder/IDE 中“在 Codex HUD 打开当前目录”。

优点：

- 不污染 `codex` 命令；
- 新窗口拥有明确 TTY 和 Ghostty surface；
- 适合一键启动；
- 与 standalone native split 路线一致。

限制：

- macOS/Ghostty 专用；
- 传递任意 Codex 参数不如 Shell 灵活；
- cwd、prompt、resume/fork 需要设计 launcher 参数或 IPC；
- GUI 启动可能使用不同 PATH，需要明确环境加载方式。

判断：适合作为平台增强入口，与 CLI 默认启动并行存在。

### 6.6 tmux new-window、快捷键或项目命令

可设计：

- tmux new-window 启动 HUD；
- tmux key binding 创建专用 Codex window；
- `make codex`、`just codex`、`mise run codex` 等项目命令。

优点：

- TTY 和 `TMUX_PANE` 天然明确；
- 不覆盖 Codex 命令；
- 项目可以固定 cwd、模型、profile 或审批策略；
- 对远程机器和 SSH 更友好。

限制：

- 仅适合 tmux 或指定项目；
- 配置分散在用户或项目环境；
- 不提供纯插件体验。

判断：低风险、实用的辅助入口，适合提供模板而不是强制安装。

### 6.7 插件 Skill 打开独立外部终端

目标交互：

```text
用户：在新窗口启动 HUD
Codex Skill：
  1. 确认 cwd、是否 resume、需要的参数
  2. 请求 GUI/外部进程权限
  3. 打开新的 Ghostty window/tab
  4. 在新终端运行 HUD launcher
```

这不是在当前 TTY 内嵌套 Codex，而是启动新的独立终端会话。

优点：

- 可以通过自然语言触发；
- 当前 Codex thread 不被 wrapper 或新 TUI 接管；
- 可以携带 cwd、模型和 resume 目标；
- 适合 macOS/Ghostty 用户。

风险：

- 平台和终端相关；
- GUI 自动化需要用户授权；
- 启动新 thread 与 resume 当前 thread 的语义必须明确；
- 同一会话同时被两个 TUI 使用可能产生状态竞争；
- 失败时需要把可复制的手动命令返回给用户。

判断：可研究为插件辅助入口，但不替代通用 CLI。

### 6.8 SessionStart Hook 自动分屏

#### tmux 环境

Hook 可能继承 `TMUX` 和 `TMUX_PANE`，因此可以在 Codex 启动后创建 pane。理论上可以：

- 用 `session_id` 生成或绑定 run；
- 后台启动 renderer；
- split 当前 pane；
- 在 SessionEnd 时清理。

但仍存在：

- 无法为已经运行的 Codex 补注入 `--no-alt-screen`；
- Hook timeout 仅 2 秒，需要再派生后台 owner；
- 普通会话、嵌套 Codex 和 resume/clear 需要重新定义绑定规则；
- SessionEnd、进程崩溃、tmux detach 和 pane 被手动关闭会产生竞态；
- 插件 enabled 后可能影响每一个普通 Codex 会话。

#### Ghostty standalone

当前方案依赖 wrapper 向父 TTY 写 OSC 7 marker，再由 AppleScript 精确选择调用 surface。Hook stdin/stdout 通常是事件管道，不能可靠操作父 TTY。

若改用 focused terminal 或 cwd 猜测，多窗口环境可能分割错误窗口。

判断：

- tmux-only 可以做实验；
- Ghostty standalone 不适合作为默认自动启动路径；
- 不能替代完整跨终端 launcher。

### 6.9 Codex 原生单行 status line

使用 Codex `tui.status_line` 提供 compact 模式：

- 保留模型、上下文、Git 和时长等原生字段；
- 放弃或压缩 TOOLS、AGENTS、SKILLS、TODO 和成本；
- 不创建 split，不需要终端 owner。

优点：

- 真正使用普通 `codex`；
- 无额外进程和 pane；
- 兼容所有终端。

限制：

- 当前 status line 只接受 Codex 支持的原生标识符；
- 插件不能写入自定义多行内容；
- 无法完整展示当前 HUD 语义。

判断：适合独立的 compact 产品模式，不是完整 HUD 的替代启动方式。

### 6.10 App Server、daemon 或独立 dashboard

设计为：

```text
Codex Hooks / App Server
  → 本地 daemon
  → Web UI / 菜单栏 / 独立 Ghostty window
```

优点：

- 普通 `codex` 可以直接启动；
- 支持同时观察多个 session；
- UI 不依赖某个 terminal pane；
- 适合历史分析和常驻监控。

限制：

- 不再是 Codex 下方固定高度 HUD；
- 需要 daemon 生命周期、IPC、session 发现和安全边界；
- 多账号、多个 `CODEX_HOME` 和 transcript 访问需要重新设计；
- 与当前 local-only 单 run 模型差异很大。

判断：属于新的产品方向，不应作为当前 launcher 的小改动。

### 6.11 `codex exec` / CI 的 headless 启动

非交互环境可以考虑：

- Hook 写 JSON 状态；
- renderer `--once` 输出一次摘要；
- 命令结束后打印 token、cache、cost 和工具统计；
- 可选机器可读 JSON。

优点：

- 适合 CI、脚本和远程执行；
- 不需要 TTY 或 sidecar；
- 失败边界简单。

限制：

- 没有实时 HUD；
- 不适用于交互式历史滚动和 Ghostty surface。

判断：可作为独立的 headless telemetry 模式。

## 7. 推荐分级

### P0：优先设计

#### A. `codex-hud` 默认直接启动

原因：收益明确、架构不变、风险最低。

#### B. 可选 Shell function

原因：为希望继续输入 `codex` 的用户提供透明入口，同时保留清晰 bypass。

P0 设计阶段需要先确定：

- 交互/非交互子命令分类规则；
- 未知 Codex 新子命令的默认行为；
- bypass 语义；
- shell completion；
- 卸载和回滚方式。

### P1：提供平台模板

- Ghostty Profile；
- Ghostty 快捷键或 macOS Launcher；
- tmux new-window/key binding；
- 项目级 make/just/mise 示例。

这些方案应作为文档模板，不应静默修改用户配置。

### P2：独立能力研究

- Skill 打开独立新终端；
- compact 原生 status line；
- headless telemetry。

这些能力不应与完整 HUD 的启动语义混为一谈。

### P3：暂不作为主方案

- PATH shim 覆盖 `codex`；
- SessionStart 自动分屏；
- 常驻 daemon/dashboard。

只有明确的新需求和独立设计评审后再进入实现。

## 8. 明确不做

当前阶段不做以下事情：

- 不修改现有 launcher 或 Skill；
- 不自动编辑 `~/.zshrc`、`~/.bashrc`、Ghostty 或 tmux 配置；
- 不安装同名 `codex` shim；
- 不从当前 Codex thread 内嵌套启动交互式 Codex；
- 不把 SessionStart Hook 直接改造成终端布局 owner；
- 不把完整 HUD 降级为单行后仍宣称功能等价；
- 不启动 daemon、Web UI 或 GUI 自动化实验。

## 9. 后续设计验收条件

任何新入口进入实现前，需要有明确的验收项。

### 9.1 通用

- 所有 Codex 参数原样透传；
- `resume`、`fork` 和普通交互启动可用；
- `exec`、`plugin`、`completion` 等非交互命令不会被错误包装；
- 有明确、可靠、文档化的 bypass；
- launcher 失败时安全退回普通 Codex；
- 不采集不属于当前 run 的会话；
- 卸载后不残留命令覆盖或终端配置。

### 9.2 Ghostty

- 多窗口时只分割目标 terminal；
- 主 surface 保持原生滚轮、拖选和右键；
- HUD 始终 read-only 且保持基础 8 行；存在运行中子代理、未完成任务或最近完成项时动态扩展到最多 24 行；
- 启动失败、信号和 Codex 退出后无孤儿 split。

### 9.3 tmux

- 不污染 server-global binding；
- 精确恢复 session mouse 状态；
- 并发 HUD holder 行为确定；
- HUD pane 不抢焦点、不进入无意义 copy-mode；
- detach、重新 attach 和 Codex 退出均能正确清理。

### 9.4 Shell 透明入口

- zsh/bash/fish 至少有独立测试矩阵；
- completion 仍来自真实 Codex；
- 未知子命令默认 bypass，而不是冒险包装；
- `command codex` 或等价机制始终可用；
- 非 TTY、管道和 CI 自动 bypass。

## 10. 待决问题

在进入实现前需要回答：

1. 默认目标是减少一个 `run` 单词，还是让用户完全继续输入 `codex`？
2. Shell function 是否只支持 zsh，还是同时支持 bash/fish？
3. `resume` 和 `fork` 是否默认进入 HUD？
4. 未知 Codex 子命令应直接 bypass，还是按 TTY 状态判断？
5. 插件 Skill 打开新终端时，是创建新 thread 还是 resume 当前 thread？
6. 是否需要一个明确的 `compact` 单行模式？
7. 平台模板由安装脚本生成、仅文档提供，还是独立可选安装？
8. PATH shim 是否明确排除在产品范围之外？
9. tmux-only SessionStart 自动模式是否有足够独立价值？
10. 新入口是全局偏好、每项目偏好，还是每次显式选择？

## 11. 当前记录结论

- Codex 插件安装、Hook 和 Skill 集成已经成立。
- 完整 HUD 仍需要一个 pre-launch owner。
- `codex-hud run` 可以被更友好的入口触发或替代，但底层 wrapper 职责不能省略。
- 最优先的设计方向是“`codex-hud` 默认启动 + 可选 Shell function”。
- Ghostty/tmux 启动模板适合作为低风险补充。
- 纯 SessionStart 自动启动当前只能视为受限实验。
- 本文只记录设计，不授权或包含任何实现修改。
