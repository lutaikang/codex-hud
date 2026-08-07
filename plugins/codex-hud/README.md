# Codex HUD

`codex-hud` 是一个本地、可开关、可复用的 Codex CLI 8 行实时监控面板。macOS standalone Ghostty 使用原生 split 在 Codex 下方显示 HUD；已在 tmux 内或非 Ghostty 环境才使用 tmux pane，保留 Codex 原生单行状态栏。

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

面板严格占 8 行；渲染末尾不输出换行，因此不会滚出空白行。tmux fallback 创建 HUD pane 时使用 `split-window -l 8`；Ghostty 原生 split 启动后由 geometry monitor 动态校准到 8 行。窄终端通过 ANSI 与 CJK 字符宽度安全裁剪内容，不自动换行。

## 主题与显示

默认主题使用少量语义色：青色用于栏目和模型，紫色用于账号，绿色用于完成/低 Context 压力，黄色用于运行中或 65% 以上压力，红色用于 85% 以上压力。设置 `NO_COLOR=1` 可关闭颜色。

HUD 不依赖 Nerd Font，只使用常见 Unicode 字符；若终端字体显示异常，可配合 `NO_COLOR=1` 使用 UTF-8 等宽字体。所有行按终端列宽裁剪，中日韩全角字符计两列。

## 为什么采用终端侧车面板

Codex 官方 `tui.status_line` 只接受有序的原生标识符，适合保留单行状态；它没有插件可写的多行区域。HUD 因此采用独立的终端侧车 surface：Ghostty standalone 使用原生 split，其他环境使用 tmux pane。生命周期与 Codex 进程绑定，插件不用 patch TUI，升级失败时也可直接关闭。详细设计见 `docs/architecture.md`。

## 插件与启动 wrapper 的边界

Codex 插件负责安装和启用 manifest、生命周期 Hook 与 `codex-hud` Skill。Hook 只在 wrapper 预先注入 `CODEX_HUD_RUN_ID` 时采集当前会话，避免插件启用后误收普通 Codex 会话。

终端布局必须在 Codex 启动前决定：wrapper 需要注入 `--no-alt-screen`、设置运行 ID，并成为 Ghostty split 或 tmux pane 的生命周期 owner。当前 Codex 插件接口没有 pre-launch wrapper 或多行 TUI surface 扩展点，所以普通 `codex` 不会自动创建 HUD。请在外部终端运行 `codex-hud run`，不要从已运行的 Codex thread 内启动嵌套交互会话。完整分析见 `docs/plugin-startup-analysis.md`。

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
- 历史输出滚轮：Ghostty standalone 使用 Codex 官方 inline 模式保留 scrollback，由 Ghostty 原生滚动；tmux fallback 使用 tmux 能力范围内的历史滚动
- `on` / `off` / `toggle` 自定义开关

## 前置条件

- Codex CLI 0.146+（本次文档漂移审计使用 0.147.0）
- Python 3.10+
- tmux 3.x（仅已在 tmux 内或非 Ghostty/非 macOS fallback 需要）
- macOS standalone Ghostty 可使用原生 split，不要求 tmux 承载主 Codex surface
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

`codex-hud on` 只保存启用偏好，不会改写 `codex` 命令。新会话仍需从外部终端通过 `codex-hud run` 启动。

关闭 HUD 不会关闭原生单行状态栏。原生栏由用户自己的 `~/.codex/config.toml` 中 `tui.status_line` 管理，本项目不会重写该配置。

## 鼠标滚轮

Codex 0.146 默认使用 alternate screen；tmux 开启 mouse 后，默认 `WheelUpPane` 会把滚轮作为原始鼠标事件发给 Codex，但 Codex TUI 明确忽略 mouse event，因此不能形成历史滚动。`codex-hud run` 只在 HUD 包装路径注入 Codex 官方 `--no-alt-screen`，让主对话按官方 inline 模式写入承载终端的 scrollback；未启用 HUD 的普通 `codex` 不受影响。

**Ghostty/macOS standalone**：wrapper 不创建 tmux 主 session，而是通过 Ghostty 原生 split 创建 HUD surface；Codex 使用官方 `--no-alt-screen`，历史输出留在 Ghostty terminal scrollback，滚轮、拖选、释放保持和右键菜单由 Ghostty 原生处理。HUD surface 原生 read-only，且由 geometry monitor 动态保持 8 行。目标 terminal 通过唯一 OSC 7 working-directory marker 定位，避免多窗口时误选 front window。

**已在 tmux 内或非 Ghostty/非 macOS**：使用 tmux fallback。该路径只能在 tmux 能力范围内提供滚动和 pane 生命周期，不能宣称等同 Ghostty standalone 的原生选区和 context menu；退出时会恢复原有 session mouse 设置。

## 数据与隐私边界

状态仅保存在 `~/.codex-hud`，目录和文件使用私有权限。Hook 只处理 Codex 提供的当前 wrapper 会话事件；渲染器只读该会话的 transcript 路径，并通过同一进程环境下的本地 `codex app-server` 查询当前账号与限额。它不扫描其他 `CODEX_HOME`，不复制 `auth.json`，也不展示其他账号的历史上下文。

`≈$` 把观测到的输入、缓存输入和输出 token 套用内置参考单价，仅是 API 等价估算，不是 ChatGPT/Codex 订阅账单；可能不含长上下文附加费、cache write、工具费或未来价格变化。缓存比率只是 `cached_input_tokens / input_tokens`，不标注"健康/异常"，因为合理值取决于工作负载。

## 验证与故障排查

```bash
./scripts/verify.sh
codex-hud status
```

常见问题、tmux 内外启动差异及账号边界见插件包内的 `docs/troubleshooting.md`。指标口径见 `docs/metrics-semantics.md`，插件启动边界见 `docs/plugin-startup-analysis.md`。主题与终端显示规则见本 README 的“主题与显示”小节。
