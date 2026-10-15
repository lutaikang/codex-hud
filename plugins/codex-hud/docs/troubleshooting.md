# 故障排查

## HUD 没出现

运行 `codex-hud status`，确认 HUD 已开启，然后必须从外部终端用 `codex-hud run` 启动新会话。macOS standalone Ghostty 不要求 tmux；已在 tmux 内或非 Ghostty 环境才需要 tmux fallback。

普通 `codex` 只会加载已安装的插件、Hook 和 Skill，不会自动创建 HUD surface。`codex-hud on` 也只保存启用偏好，不会改写 `codex` 命令。

## 不要在当前 Codex thread 内嵌套启动

插件 Skill 可以执行 `on`、`off` 和 `status`，但不应在已运行的 Codex thread 内再启动交互式 `codex-hud run`。完整 HUD 需要在父 Codex 启动前注入 `--no-alt-screen` 和 `CODEX_HUD_RUN_ID`，嵌套会话也可能继承环境并干扰当前 thread 绑定。

同样不要在任何活动 Codex 任务内运行 `scripts/update.sh`。Codex 会把 Hook 解析为版本化缓存中的绝对路径；插件管理器删除旧缓存后，在途任务不会自动更换路径。更新脚本检查 `CODEX_HUD_RUN_ID`、`CODEX_THREAD_ID` 和活动 Codex 进程，在任何修改前拒绝更新，不会结束这些进程。请自行关闭共享该安装的任务和 App Server，再从外部终端更新，期间不要新开 Codex。只换到外部终端而保留其他活动任务仍不安全。

## 鼠标滚轮、选区或右键异常

Codex 0.146 的兼容问题来自 alternate screen 与 tmux 鼠标事件路径。HUD wrapper 会添加官方 `--no-alt-screen`，使历史输出进入承载终端的 scrollback；普通无 HUD 的 Codex 启动保持原配置。

**Ghostty/macOS standalone（`codex-hud run` 创建）**：不创建 tmux 主 session，而是使用 Ghostty 原生下方 split。主 Codex surface 保留 terminal scrollback，滚轮、拖选、释放保持和右键由 Ghostty 原生处理；HUD surface 为 read-only。

**Ghostty/macOS 嵌入已有 tmux**：wrapper 会临时关闭当前 session 的 `mouse` 并保留兼容路径；由于外层 tmux 仍存在，该模式不能同时提供 standalone Ghostty 的完整滚轮、选区和右键语义。退出时恢复原来的显式或继承状态，同一 session 的其他窗口在运行期间也会暂时受 session 级 mouse 选项影响。需要完整原生行为时，请在 tmux 外启动。

**已在 tmux 内或非 Ghostty/非 macOS**：保留 tmux fallback；该 fallback 只保证 tmux 能力范围内的历史滚动和 pane 生命周期，不宣称等同于 Ghostty standalone 的原生选区和 context menu。

**升级后仍出现旧行为**：下一次嵌入式 `codex-hud run` 会仅迁移带 `@is_hud` / `@hud_main_pane` 标记的旧版绑定，恢复当前 tmux 默认绑定；用户自定义绑定不会被迁移。

## 高度不正确

tmux fallback 创建 HUD pane 时先用 `split-window -l 8` 分配基础高度；存在运行中子代理、未完成任务或最近完成项时 renderer 会根据实际 pane 高度动态扩展到最多 24 行。Ghostty 原生 split 会读取实际 geometry 并动态校准。窗口过小时，HUD 会优先保留 TODO 汇总，额外任务行可能无法显示，但不会滚屏或启动失败。Ghostty renderer heartbeat 失效后，geometry monitor 会停止继续 resize。

## 面板一直停在 starting

Codex CLI 0.162.1 默认复用共享服务，服务进程可能没有 wrapper 临时注入的运行 ID。新版 wrapper 在 CLI 支持时自动添加 `--no-daemon`，让 Hook 继承本次环境。请更新后在外部终端新开 `codex-hud run`。首次提交提示前还没有 SessionStart 状态时，`starting` 是正常的等待状态。

## Context 与文档或旧版本不一致

当前实现从窗口与最新使用量都扣除 12,000 token baseline，与原生 TUI 口径一致。原始 fixture `122880 / 258400` 显示有效值 `110880 / 246400`，期望 `45% used`、`55% left`。旧版 baseline 为 0 时会高估原生已用比例。

本次实际验证环境为 Codex CLI 0.162.1。运行 `./scripts/verify.sh` 检查 fixture；升级 Codex 后若口径变化，需要同步更新实现、测试和 `docs/metrics-semantics.md`。

## 账号错误

确认启动 wrapper 时的 `CODEX_HOME`。HUD 继承该变量并只读取该 home 的当前账号。切换账号后请新开 `codex-hud run` 会话。

当前 Hook 协议没有提供稳定的 Codex 进程身份。插件会拒绝不同账号和不同 thread 的普通 `startup`，并允许同账号的合法 `clear` / `resume`；因此不要从受监控会话的工具命令里再嵌套执行 `codex resume`。

## 嵌套 tmux

在已有 tmux 内运行时，HUD 只分割当前 pane，不关闭现有 tmux status。终端高度不足基础 8 行时，tmux 可能拒绝创建 pane；空间不足时，HUD 优先保留 TODO 汇总，子代理与 TODO 详情行会按实际空间降级折叠。高度暂时发生漂移时，renderer 会按实际 rows 再次校准。tmux 的 `mouse` 是 session 级选项，因此嵌入式 HUD 运行期间，同 session 的其他窗口可能暂时受到影响。

## 账号或限额暂时显示未知

HUD 首帧不会等待 `codex app-server`。renderer 在后台读取账号和 rate limits，并按约 60 秒刷新；App Server 暂时失败时，面板继续运行，账号或限额会显示 loading/unknown，而不会阻塞 Codex。

## 关闭

`codex-hud off` 会让活动 renderer 在下一次刷新退出，但不会终止 Codex，也不会修改原生 `tui.status_line`。
