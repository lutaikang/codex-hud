# Codex HUD

`codex-hud` 是一个本地、可开关、可复用的 Codex CLI 8 行实时监控面板。它在 tmux 下方显示当前会话状态，保留 Codex 原生单行状态栏。

```
HUD ● working  [gpt-5.6-sol · xhigh]  acct:me/plus  git:(feat/hud*)  ⏱ 1h 02m  ≈$7.22
CTX  ███░░░░░░░  27% used · 73% left · 68.6k/258.4k  │  7d 100% ↻163h
CACHE  97% · 16.12m/16.67m
TOOLS  ◐ Read 5s · ◐ Bash 3s · ◐ Write 1s  … +2 more
       ✓ Bash ×343 · ✓ Read ×23 · ✓ Write ×8  … +7 more
AGENTS ◐ reviewer 5m · ◐ explorer 2m · ✓ 5 complete
SKILLS ◐ brainstorming 30s · ✓ ×3
TODO   8/12  ◐ 当前任务名
```

面板严格占 8 行；渲染末尾不输出换行，因此不会滚出空白行。`codex-hud` 在 session 创建时用 `resize-pane` 锁定 HUD 面板高度。窄终端通过 ANSI 与 CJK 字符宽度安全裁剪内容，不自动换行。

## 为什么采用 tmux 侧车面板

Codex 官方 `tui.status_line` 只接受有序的原生标识符，适合保留单行状态；它没有插件可写的多行区域。HUD 因此采用 tmux 侧车 pane：生命周期与 Codex 进程绑定，插件不用 patch TUI，升级失败时也可直接关闭。详细设计见 `docs/architecture.md`。

## 功能清单

- 当前模型、推理强度、运行状态、项目、Git 分支和 Session 时长
- 仅当前 `CODEX_HOME` 的当前账号与套餐
- Context 已用/剩余比例（基于 Codex 原始值，与原生 TUI 口径一致）
- 当前账号的短周期和 7 天用量窗口（可用时）
- 工具运行状态（最近 3 个活跃工具，按启动时间降序）
- 工具累计调用统计（前 5 个，按调用次数降序 + 溢出提示）
- 子代理运行状态、耗时和完成数
- Skills 活跃状态与累计统计
- `update_plan` Todo 完成度及当前项
- 缓存输入占全部输入的中性比率
- 基于内置参考单价的 API 等价成本估算（不是订阅账单）
- 鼠标滚轮：主面板可滚动历史输出，HUD 面板不捕获滚轮
- `on` / `off` / `toggle` 自定义开关

## 前置条件

- Codex CLI 0.146 或兼容版本
- Python 3.10+
- tmux 3.x（macOS、Linux 或 WSL）
- 终端需支持 UTF-8，颜色可用 `NO_COLOR=1` 关闭

## 从源码仓库安装

以下命令只适用于完整源码仓库（不是 Codex 安装缓存）并从仓库根目录执行：

```bash
./scripts/install.sh
```

安装脚本会先拒绝覆盖不属于本项目的 `~/.local/bin/codex-hud`，再注册本地 marketplace、安装并启用插件，最后建立指向源码仓库的命令链接。因此移动源码仓库前应先卸载再重新安装。若该目录不在 `PATH`，可直接运行 `plugins/codex-hud/scripts/codex-hud`。

也可以手动安装：

```bash
codex plugin marketplace add /absolute/path/to/codex-hud
codex plugin add codex-hud@codex-hud-local
```

首次安装后，本地更新使用 `./scripts/update.sh`：它会写入单一 cachebuster 并重新安装。卸载见 `./scripts/uninstall.sh`；它不会删除 `~/.codex-hud` 中的本地偏好和临时状态。

## 使用

```bash
codex-hud on       # 启用多行 HUD
codex-hud off      # 关闭；现有 HUD 面板自动退出
codex-hud toggle   # 切换开关
codex-hud status   # 显示状态和依赖
codex-hud run      # 在 HUD 布局中启动 Codex
```

普通 Codex 参数放在 `run --` 后：

```text
codex-hud run -- -C /path/to/repo --search
```

关闭 HUD 不会关闭原生单行状态栏。原生栏由用户自己的 `~/.codex/config.toml` 中 `tui.status_line` 管理，本项目不会重写该配置。

## 鼠标滚轮

`codex-hud run` 创建的独立 tmux session 启用 `mouse on`。在主 Codex 面板上滚动鼠标滚轮可进入 tmux copy-mode 浏览历史输出。HUD 面板（底部 8 行）通过 `bind-key` 排除了滚轮捕获——滚轮事件落在 HUD 区域时被静默丢弃，不会进入无意义的 copy-mode。

如需在嵌入已有 tmux 的模式下也使用鼠标滚轮，请在当前 tmux 窗口中手动启用 `mouse on`。

## 数据与隐私边界

状态仅保存在 `~/.codex-hud`，目录和文件使用私有权限。Hook 只处理 Codex 提供的当前 wrapper 会话事件；渲染器只读该会话的 transcript 路径，并通过同一进程环境下的本地 `codex app-server` 查询当前账号与限额。它不扫描其他 `CODEX_HOME`，不复制 `auth.json`，也不展示其他账号的历史上下文。

`≈$` 把观测到的输入、缓存输入和输出 token 套用内置参考单价，仅是 API 等价估算，不是 ChatGPT/Codex 订阅账单；可能不含长上下文附加费、cache write、工具费或未来价格变化。缓存比率只是 `cached_input_tokens / input_tokens`，不标注"健康/异常"，因为合理值取决于工作负载。

## 验证与故障排查

```bash
./scripts/verify.sh
codex-hud status
```

常见问题、tmux 内外启动差异及账号边界见插件包内的 `docs/troubleshooting.md`。指标口径见 `docs/metrics-semantics.md`，主题与终端兼容策略见 `docs/themes.md`。
