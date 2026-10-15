# 架构设计

## 插件层与启动层

Codex HUD 已经是一个 Codex 插件，但插件和启动 wrapper 承担不同职责：

- 插件 manifest 负责被 marketplace 发现、安装和启用。
- `hooks/hooks.json` 负责接收 Codex 生命周期事件。
- `skills/codex-hud/SKILL.md` 负责让 Codex 管理开关、状态和使用说明。
- `scripts/codex-hud run` 必须在 Codex 启动前创建终端布局、注入运行环境并成为生命周期 owner。

普通 `codex` 启动时，插件 Hook 虽然已加载，但没有 `CODEX_HUD_RUN_ID`，因此不会采集会话，也不会自动创建 HUD surface。这是避免全局插件误收普通会话的安全边界。

## 路线

Codex 原生 `tui.status_line` 继续负责单行信息；本插件按承载终端选择基础 8 行、随运行中子代理与计划/历史动态扩展到最多 24 行的侧车 surface：macOS standalone Ghostty 使用原生 split，已在 tmux 内或非 Ghostty 环境使用 tmux pane。外层 wrapper 为当前进程设置随机 `CODEX_HUD_RUN_ID`，只有带该 ID 的 Hook 才采集事件。首次 `SessionStart` 还会绑定当前 `CODEX_HOME` 指纹和 thread；不同 thread 的普通 `startup` 被拒绝，只有同账号下明确的 `clear` / `resume` 合法切换会迁移绑定。

```text
codex plugin add
  └─ manifest + Hook + Skill ──> Codex 插件运行时

外部终端: codex-hud run
  ├─ 注入 CODEX_HUD_RUN_ID + --no-alt-screen
  ├─ Ghostty/macOS standalone ── native split ──┐
  ├─ tmux/non-Ghostty fallback ── tmux pane ────┤
  └─ Codex CLI ── lifecycle hooks ──> ~/.codex-hud/runs/<id>.json
                  └─ transcript JSONL + app-server ──> HUD renderer
```

## 设计取舍

- 不 patch Codex TUI：降低升级耦合，关闭开关即可回退。
- 不从 Hook 内启动嵌套 Codex：Hook 发生在当前 Codex 已启动之后，无法反向注入 `--no-alt-screen` 或安全接管父终端。
- 不运行第二个会话读取历史：transcript 路径由当前会话 Hook 提供。
- App Server 只读当前 `CODEX_HOME` 的账号和 rate limits；不用于猜测另一个进程的 thread。
- 状态文件采用文件锁和原子替换；Hook 异常被吞掉，监控不能阻塞 Codex。
- Ghostty/macOS standalone 不创建 tmux 主 session；通过 Ghostty 原生 split 保持主 Codex surface 的滚轮、拖选、释放保持和右键原生语义。已在 tmux 内、非 Ghostty 或无法准确定位当前 Ghostty surface 时才使用 tmux fallback。
- 原生 split 的 HUD surface 由 Ghostty 设置为 read-only；唯一的 OSC 7 working-directory marker 用于精确选择调用 wrapper 的 terminal，marker 不可用时安全回退，不猜测 front window。
- 原生 split 的高度由 geometry monitor 按运行中子代理、计划摘要和最近完成项动态校准，最多 24 行；tmux fallback 使用创建时的 `split-window -l 8`，renderer 在内容变化时调整自身 pane。
- 不安装新的 server-global 鼠标绑定；升级时只识别带 `@is_hud` / `@hud_main_pane` 标记的旧版绑定，并恢复当前 tmux 默认命令。
- pane 内的终端能力保持 `tmux-256color`，不把外层 Ghostty 的 `TERM=xterm-ghostty` 伪装传入 Codex。
- HUD 包装路径使用 Codex 官方 `--no-alt-screen`，让 transcript 写入承载终端的 scrollback；普通无 HUD 启动不受影响。
- wrapper 检测 CLI 的 `--no-daemon` 能力，支持时使用独立服务，避免既有共享服务缺少本次 `CODEX_HUD_RUN_ID`。旧 CLI 保持原路径；本地 HUD 拒绝无法绑定 Hook 的显式 `--remote` 会话。
- renderer 退出时取消账号读取，并对自己创建的 App Server 执行 terminate/wait，必要时 kill/wait；TERM/HUP 也经过此清理路径，不操作共享服务。
- Ghostty/macOS standalone 由 Ghostty 原生滚轮浏览历史；HUD renderer 不声明 mouse capture，也不注入鼠标协议。已在 tmux 内或非 Ghostty/非 macOS 才使用 tmux fallback。
- 用户从独立 tmux session detach 时保留后台 Codex；只有 Codex 真正退出或布局启动失败才清理 session。
- 最终渲染无尾随换行，surface 高度按运行中子代理、计划摘要、最近完成项与实际 geometry 动态校准；空间不足时优先保留 TODO 汇总，避免滚屏。
- `HUD_MIN_LINES`、`HUD_MAX_LINES`、`HUD_DETAIL_ROWS` 和 `desired_hud_lines()` 在 `hud_state.py` 中定义为单一真源；高度随运行中子代理与 TODO 详情行动态增长。
- `update_plan` 是 TODO 的唯一事实来源；当前计划与有上限的 `plan_history` 分离保存，任务按文本/顺序 best-effort 维持内部 ID，并记录状态转换和 revision。Stop/SessionEnd 不会自动完成未收尾任务，旧计划会显示为 stale/“计划未同步”。非法计划项保留为 unknown，不会伪装成完成。
- `SubagentStart`/`SubagentStop` 决定 agent 生命周期；spawn Hook 和 transcript 只补充任务名、模型、推理强度和最近动作。推理强度容错探测 `effort`/`reasoning_effort`。Hook 与 transcript 冲突时不改变生命周期，无法确认时降级显示类型和耗时。
- 主题使用语义槽位（`DEFAULT_THEME`），`color()` 按槽位解析到 `BASE_COLORS` 的 ANSI 码；`~/.codex-hud/config.json` 的 `theme` 区块可覆盖槽位，`NO_COLOR` 总开关保留。

## Context 口径

Context 百分比从 Codex transcript 中的 `model_context_window` 与 `last_token_usage.total_tokens` 都扣除 12,000 token。先计算剩余比例并按 Rust 非负数 `round()` 取整，再用 `100 - remaining_percent` 得到已用比例。CTX token 比显示有效值；scanner 的原始值和累计 token 不改变。

## 兼容层

`scripts/metrics.py` 隔离版本敏感指标。本次实际运行和官方协议核验环境为 Codex CLI 0.162.1；Codex 升级后应同时核对 transcript 协议、原生 TUI 口径、测试 fixture 和 `docs/metrics-semantics.md`。

完整的插件原生启动可行性分析见 `docs/plugin-startup-analysis.md`。
