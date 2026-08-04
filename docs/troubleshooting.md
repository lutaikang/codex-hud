# 故障排查

## HUD 没出现

运行 `codex-hud status`，确认 HUD 已开启、`tmux` 可执行，然后必须用 `codex-hud run` 启动新会话。普通 `codex` 不会自动创建 pane。

## Context 与原生栏不一致

先确认 Codex CLI 版本。当前公式对齐 0.146；新版可能调整基线或 token 语义。运行 `./scripts/verify.sh` 检查已知 fixture，再按 `docs/metrics-semantics.md` 更新兼容层。

## 账号错误

确认启动 wrapper 时的 `CODEX_HOME`。HUD 继承该变量并只读取该 home 的当前账号。切换账号后请新开 `codex-hud run` 会话。

当前 Hook 协议没有提供稳定的 Codex 进程身份。插件会拒绝不同账号和不同 thread 的普通 `startup`，并允许同账号的合法 `clear` / `resume`；因此不要从受监控会话的工具命令里再嵌套执行 `codex resume`，它可能被解释为当前 TUI 的合法恢复切换。

## 嵌套 tmux

在已有 tmux 内运行时，HUD 只分割当前 pane，不关闭现有 tmux status。终端高度不足 10 行时，tmux 可能拒绝创建 5 行 pane。

## 关闭

`codex-hud off` 会让活动 renderer 在下一次刷新退出，但不会终止 Codex，也不会修改原生 `tui.status_line`。
