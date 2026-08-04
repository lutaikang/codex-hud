# 故障排查

## HUD 没出现

运行 `codex-hud status`，确认 HUD 已开启、`tmux` 可执行，然后必须用 `codex-hud run` 启动新会话。普通 `codex` 不会自动创建 pane。

## 鼠标滚轮无法滚动历史

**独立 session（`codex-hud run` 创建）**：已自动启用 `mouse on`，滚轮在主 Codex 面板上可进入 copy-mode 浏览历史。HUD 面板区域（底部 8 行）通过 `bind-key` 排除滚轮捕获。

**嵌入已有 tmux**：需在当前窗口手动启用鼠标：`tmux set-option -w mouse on`。

**指针位置**：滚轮事件按指针所在 pane 路由。确保鼠标在主 Codex 面板区域而非 HUD 面板区域。

## 高度不正确

`codex-hud` 在创建 session 时用 `resize-pane -y 8` 固定 HUD 高度。终端窗口高度应 >= 10 行（主面板至少 2 行 + HUD 8 行）。窗口过小时 tmux 可能拒绝分配足够行数。

## Context 与原生栏不一致

当前 HUD 使用 Codex transcript 中的原始 `model_context_window` 和 `total_tokens` 值，与 Codex 原生 TUI 的百分比口径一致。确认 Codex CLI 版本 >= 0.146。运行 `./scripts/verify.sh` 检查 fixture。

## 账号错误

确认启动 wrapper 时的 `CODEX_HOME`。HUD 继承该变量并只读取该 home 的当前账号。切换账号后请新开 `codex-hud run` 会话。

当前 Hook 协议没有提供稳定的 Codex 进程身份。插件会拒绝不同账号和不同 thread 的普通 `startup`，并允许同账号的合法 `clear` / `resume`；因此不要从受监控会话的工具命令里再嵌套执行 `codex resume`，它可能被解释为当前 TUI 的合法恢复切换。

## 嵌套 tmux

在已有 tmux 内运行时，HUD 只分割当前 pane，不关闭现有 tmux status。终端高度不足 10 行时，tmux 可能拒绝创建 8 行 pane。嵌入模式下鼠标滚轮需手动启用（见上方）。

## 性能

渲染器每秒轮询 transcript 和状态，正常 CPU < 1%。修改源码后删除 `__pycache__` 确保新代码生效。

## 关闭

`codex-hud off` 会让活动 renderer 在下一次刷新退出，但不会终止 Codex，也不会修改原生 `tui.status_line`。

## 重置

```bash
codex-hud off
rm -rf ~/.codex-hud   # 可选：清理本地状态
./scripts/install.sh
```
