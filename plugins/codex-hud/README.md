# Codex HUD

`codex-hud` 是一个本地、可开关、可复用的 Codex CLI 实时监控面板。默认显示 8 行；有运行中子代理或未完成任务时，面板随内容动态扩展，最多 24 行。macOS standalone Ghostty 使用原生 split 在 Codex 下方显示 HUD；已在 tmux 内或非 Ghostty 环境才使用 tmux pane，保留 Codex 原生单行状态栏。

```
HUD ● working  [gpt-5.6-sol · xhigh]  acct:me/plus  git:(feat/hud*)  ⏱ 1h 02m  ≈387.0cr quota
CTX  ███░░░░░░░  27% used · 73% left · 68.6k/258.4k  │  7d 100% ↻163h
CACHE  97% · 16.12m/16.67m
       │  IO 343t/12img/4.8MB
TOOLS  ◐ Read 5s · ◐ Bash 3s · ◐ Write 1s  … +2 more
       ✓ Bash ×343 · ✓ Read ×23 · ✓ Write ×8  … +7 more
AGENTS 2 running · ✓ 5 complete
       ◐ reviewer [sol · high] 检查测试 · running tests 5m
       ◐ explorer [terra · low] 映射仓库 2m
SKILLS ◐ brainstorming 30s · ✓ ×3
TODO   8/12 · 4 未完成 · ◐ 执行中 · ✓ 最近完成 2
       ◐ 当前任务名
       ○ 下一任务  … +2
       ✓ 最近完成任务
```

面板基础占 8 行；每个运行中子代理各占一行，未完成计划最多显示两行未完成任务并用空余行显示最近完成项，整体随内容动态扩展到最多 24 行；没有活动计划但本会话有历史完成项时仍会显示最近完成项。渲染末尾不输出换行，因此不会滚出空白行。tmux fallback 先使用 `split-window -l 8`，随后由 renderer 按实际 pane 高度动态调整；Ghostty 原生 split 启动后由 geometry monitor 动态校准。窄终端通过 ANSI、控制字符与 CJK 字符宽度安全裁剪内容，并优先保留 TODO 汇总，不自动换行。

## 主题与显示

默认主题使用语义色槽位：各栏目标题分色，模型青色、账号紫色、完成绿色、运行中黄色，推理强度按档位着色（low 暗、medium 绿、high 黄、xhigh/max 红），缓存命中率与 Context 压力按阈值变色。可在 `~/.codex-hud/config.json` 的 `theme` 区块覆盖任意槽位，值为 `black/red/green/yellow/blue/magenta/cyan/white` 或 `bright_*`、`dim`、`bold` 之一，例如 `{"theme": {"section_ctx": "red"}}`。设置 `NO_COLOR=1` 可关闭全部颜色。

HUD 不依赖 Nerd Font，只使用常见 Unicode 字符；若终端字体显示异常，可配合 `NO_COLOR=1` 使用 UTF-8 等宽字体。所有行按终端列宽裁剪，中日韩全角字符计两列。

## 为什么采用终端侧车面板

Codex 官方 `tui.status_line` 只接受有序的原生标识符，适合保留单行状态；它没有插件可写的多行区域。HUD 因此采用独立的终端侧车 surface：Ghostty standalone 使用原生 split，其他环境使用 tmux pane。生命周期与 Codex 进程绑定，插件不用 patch TUI，升级失败时也可直接关闭。详细设计见 `docs/architecture.md`。

## 插件与启动 wrapper 的边界

Codex 插件负责安装和启用 manifest、生命周期 Hook 与 `codex-hud` Skill。Hook 只在 wrapper 预先注入 `CODEX_HUD_RUN_ID` 时采集当前会话，避免插件启用后误收普通 Codex 会话。

终端布局必须在 Codex 启动前决定：wrapper 需要注入 `--no-alt-screen`、设置运行 ID，并成为 Ghostty split 或 tmux pane 的生命周期 owner。当前 Codex 插件接口没有 pre-launch wrapper 或多行 TUI surface 扩展点，所以普通 `codex` 不会自动创建 HUD。请在外部终端运行 `codex-hud run`，不要从已运行的 Codex thread 内启动嵌套交互会话。完整分析见 `docs/plugin-startup-analysis.md`。

新版 CLI 支持 `--no-daemon` 时，wrapper 会自动注入该参数，让 Hook 使用当前启动环境中的运行 ID。旧 CLI 不添加此参数。启用 HUD 时不能使用 `--remote`，远端会话请用普通 `codex --remote ...`。

## 功能清单

- 当前模型、推理强度、运行状态、项目、Git 分支和 Session 时长
- 仅当前 `CODEX_HOME` 的当前账号与套餐
- Context 已用/剩余比例和有效 token 比（窗口与使用量都扣除 12,000 token，与原生 TUI 口径一致）
- 当前账号的短周期和 7 天用量窗口（可用时）
- 工具运行状态（最近 3 个活跃工具，按启动时间降序）
- 工具累计调用统计（前 5 个，按调用次数降序 + 溢出提示）
- 子代理逐行显示运行状态、耗时、类型、模型、推理强度和受控任务描述、最近动作摘要
- Skills 活跃状态与累计统计
- `update_plan` Todo 完成度、当前/待执行项和本会话最近完成项
- 缓存输入占全部输入的中性比率
- 当前会话的工具返回、图像块和序列化体积；达到阈值时显示 warning/critical 风险
- audit 只读扫描全部本地 rollout，按 provider 区分配额、按子代理交接边界去除父历史重放，并按 task family 汇总父子模型轮次、credits、工具循环和燃烧速率
- 基于内置参考单价的 API 等价成本估算（不是订阅账单）
- 历史输出滚轮：Ghostty standalone 使用 Codex 官方 inline 模式保留 scrollback，由 Ghostty 原生滚动；tmux fallback 使用 tmux 能力范围内的历史滚动
- `on` / `off` / `toggle` 自定义开关

## 前置条件

- Codex CLI 0.146+（本次实际运行验证使用 0.162.1）
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

首次安装后，本地更新使用 `./scripts/update.sh`：先关闭 Codex 任务和 App Server，在外部终端执行，更新期间不要新开会话。脚本在任何修改前检查任务环境和活动 Codex 进程；不能确认空闲时拒绝，不会终止其他会话。通过后才写入 cachebuster 并重新安装。日常检查可独立运行 `./scripts/verify.sh`。卸载见 `./scripts/uninstall.sh`；它不会删除 `~/.codex-hud` 中的本地偏好和临时状态。

## 使用

```bash
codex-hud on       # 启用多行 HUD
codex-hud off      # 关闭；现有 HUD 面板自动退出
codex-hud toggle   # 切换开关
codex-hud status   # 显示状态和依赖
codex-hud status --run-id <run-id> --details  # 查看当前/历史计划与 agent 详情
codex-hud audit    # 审计最近 7 天 token、工具 payload、图片和子代理扇出
codex-hud audit --since 2026-08-03 --until 2026-08-11 --json
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

**Ghostty/macOS standalone**：wrapper 不创建 tmux 主 session，而是通过 Ghostty 原生 split 创建 HUD surface；Codex 使用官方 `--no-alt-screen`，历史输出留在 Ghostty terminal scrollback，滚轮、拖选、释放保持和右键菜单由 Ghostty 原生处理。HUD surface 原生 read-only，且由 geometry monitor 动态校准到最多 24 行。目标 terminal 通过唯一 OSC 7 working-directory marker 定位，避免多窗口时误选 front window。

**已在 tmux 内或非 Ghostty/非 macOS**：使用 tmux fallback。该路径只能在 tmux 能力范围内提供滚动和 pane 生命周期，不能宣称等同 Ghostty standalone 的原生选区和 context menu；退出时会恢复原有 session mouse 设置。

## 数据与隐私边界

状态仅保存在 `~/.codex-hud`，目录和文件使用私有权限。Hook 只处理 Codex 提供的当前 wrapper 会话事件；渲染器只读该会话的 transcript 路径，并通过同一进程环境下的本地 `codex app-server` 查询当前账号与限额。TODO 使用当前计划加有上限的会话历史；AGENTS 只保存任务名、模型、推理强度和清洗后的动作摘要，不保存完整 prompt、推理过程或 agent 输出全文。它不扫描其他 `CODEX_HOME`，不复制 `auth.json`，也不展示其他账号的历史上下文。

ChatGPT 登录时，HUD 只对 transcript 中的 `model_provider=openai` 按官方 rate card 显示 token credits；自定义 provider 不并入订阅配额。API key 登录时显示 `≈API equiv.$`。两种估算都可能不含 Fast mode、图像生成、工具专项费用或未来价格变化。缓存比率是 `cached_input_tokens / input_tokens`；HUD 按复用比例高低着色（低命中偏红、高命中偏绿），但它不是性能健康度——高缓存仍会持续消耗 credits。

`codex-hud audit` 是只读分析，不会读取认证字段、不修改 rollout、不删除历史。子代理 rollout 会复制父历史；审计器只把 `inter_agent_communication_metadata` 之后的增量归入子代理，避免把 rollout 重放误算两次。随后按根任务聚合全部后代，暴露单个用户任务的真实总 credits 和模型轮次。报告最后会明确提示需与 OpenAI usage dashboard 对账。

## 验证与故障排查

```bash
./scripts/verify.sh
codex-hud status
```

常见问题、tmux 内外启动差异及账号边界见插件包内的 `docs/troubleshooting.md`。指标口径见 `docs/metrics-semantics.md`，插件启动边界见 `docs/plugin-startup-analysis.md`。主题、终端显示和动态 TODO 规则见本 README 的“主题与显示”小节及上方布局说明。
