# 架构设计

## 路线

Codex 原生 `tui.status_line` 继续负责单行信息；本插件用 tmux 创建一个严格 8 行的侧车 pane。外层 wrapper 为当前进程设置随机 `CODEX_HUD_RUN_ID`，只有带该 ID 的 Hook 才采集事件，避免全局启用 Hook 后误收普通会话。首次 `SessionStart` 还会绑定当前 `CODEX_HOME` 指纹和 thread；不同 thread 的普通 `startup` 被拒绝，只有同账号下明确的 `clear` / `resume` 合法切换会迁移绑定。

```text
codex-hud run
  ├─ Codex CLI ── lifecycle hooks ──> ~/.codex-hud/runs/<id>.json
  │              └─ current transcript JSONL ───────────────────────┐
  └─ tmux HUD renderer <─ state file + transcript + app-server account/read
```

## 设计取舍

- 不 patch Codex TUI：降低升级耦合，关闭开关即可回退。
- 不运行第二个会话读取历史：transcript 路径由当前会话 Hook 提供。
- App Server 只读当前 `CODEX_HOME` 的账号和 rate limits；不用于猜测另一个进程的 thread。
- 状态文件采用文件锁和原子替换；Hook 异常被吞掉，监控不能阻塞 Codex。
- 独立 tmux session 关闭自己的 status bar、启用 `mouse on`；嵌入已有 tmux 时不修改用户 tmux 设置（除当前窗口启用 mouse）。
- HUD 面板高度通过创建时 `resize-pane -y 8` 固定，不依赖运行时循环修正。
- HUD 面板通过 `bind-key WheelUpPane` + 高度条件判断（height == 8）排除滚轮捕获。
- 用户从独立 session detach 时保留后台 Codex；只有 Codex 真正退出或布局启动失败才清理 session。
- 最终渲染无尾随换行，pane 高度等于内容行数。
- `HUD_LINES` 在 `hud_state.py` 中定义为单一真源。

## Context 口径

Context 百分比基于 Codex 写入 transcript 的原始 `model_context_window` 与 `last_token_usage.total_tokens` 值，直接计算使用比例。与 Codex 原生 TUI 的百分比口径一致（分母均为 Codex 模型目录的当前有效上下文窗口）。

## 兼容层

`scripts/metrics.py` 隔离版本敏感指标。Codex 升级后先用官方源码/协议重新核对 Context 公式，再更新常量和 fixture。
