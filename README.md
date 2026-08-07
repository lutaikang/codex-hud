# Codex HUD

Codex HUD 是一个本地 8 行实时遥测侧车面板插件，在 Codex 主面板下方显示会话状态、上下文压力、缓存、工具、子代理、Skills、Todo 和成本估算。macOS standalone Ghostty 使用 Ghostty 原生 split；已在 tmux 内或非 Ghostty 环境才使用 tmux fallback。

核心原则：不修改 Codex TUI、不读取其他账号历史、可随时开关。

## 快速开始

```bash
./scripts/install.sh
codex-hud on
codex-hud run
```

Codex 插件负责分发 Hook、Skill 和插件元数据；真正创建 Ghostty split 或 tmux pane 的仍是 `codex-hud run` 启动 wrapper。普通 `codex` 不会自动出现 HUD，也不要在已经运行的 Codex thread 内再启动嵌套的交互式 Codex。完整边界与可行性分析见 [Codex 插件启动分析](docs/plugin-startup-analysis.md)。

传递 Codex 参数：

```bash
codex-hud run -- -C /path/to/project --search
```

关闭或切换：

```bash
codex-hud off
codex-hud toggle
codex-hud status
```

## 面板布局（8 行）

```
L1  HUD ● working  [gpt-5.6-sol · xhigh]  acct:user  project git:(branch*)  ⏱ 1h 02m  ≈$7.22
L2  CTX  ███░░░░░░░  27% used · 73% left · 68.6k/258.4k  │  7d 100% ↻163h
L3  CACHE  97% · 16.12m/16.67m
L4  TOOLS  ◐ Read 5s · ◐ Bash 3s · ◐ Write 1s  … +2 more
L5         ✓ Bash ×343 · ✓ Read ×23 · ✓ Write ×8  … +7 more
L6  AGENTS ◐ reviewer 5m · ◐ explorer 2m · ✓ 5 complete
L7  SKILLS ◐ brainstorming 30s · ✓ ×3
L8  TODO   8/12  ◐ 当前任务名
```

## 项目结构

```text
.agents/plugins/marketplace.json    本地 marketplace
plugins/codex-hud/                  插件本体、Hook、渲染器和测试
docs/architecture.md                数据流、终端路线与设计取舍
docs/metrics-semantics.md           Context/cache/cost 等指标口径
docs/plugin-startup-analysis.md     Codex 插件原生启动可行性分析
docs/themes.md                      Hub 主题和终端显示规则
docs/troubleshooting.md             安装、账号、版本与 tmux 排障
scripts/install.sh                  校验并安装
scripts/update.sh                   重新安装更新
scripts/uninstall.sh                卸载插件，保留本地偏好
scripts/verify.sh                   单测和校验
```

完整功能、安装方式、隐私边界和显示示例见 [插件 README](plugins/codex-hud/README.md)。

## 设计路线

`codex-hud run` 根据承载终端选择布局：macOS standalone Ghostty 通过 Ghostty 原生下方 split 创建 Codex 主面板和 HUD surface；已在 tmux 内或非 Ghostty 环境才创建 tmux session/pane。Codex 生命周期 Hook 只在 wrapper 注入 `CODEX_HUD_RUN_ID` 时写本地状态；renderer 增量读取当前 transcript 的 token 事件，并用本地 `codex app-server` 读取当前账号与限额。

插件安装本身已经成立：`codex plugin add` 会启用 manifest、Hook 和 Skill；但当前插件接口没有“在 Codex 父进程启动前包裹命令”或“向 TUI 注入多行区域”的入口。因此完整 8 行 HUD 采用“插件负责集成，wrapper 负责终端编排”的混合架构。

Context 百分比基于 Codex 写入 transcript 的原始 `model_context_window` 和 `total_tokens` 值，与 Codex 原生 TUI 口径一致。成本始终标为 API 等价估算。

HUD 包装路径为 Codex 注入官方 `--no-alt-screen`，让对话输出保留在承载终端的 scrollback；普通无 HUD 的 `codex` 不受影响。在 standalone Ghostty 中，主 Codex surface 不经过 tmux，滚轮、左键选区、释放保持和右键菜单全部由 Ghostty/Codex 原生处理；HUD surface 原生 read-only，并由 geometry monitor 动态保持 8 行。已在 tmux 内时无法绕过外层 tmux，wrapper 保留兼容的 tmux mouse lease；非 Ghostty 或非 macOS 环境使用 tmux fallback。插件不安装 server-global 自定义鼠标绑定。

### 路线图

- [x] v0.1.0 — 基础 5 行布局、Hook 事件采集、transcript 扫描
- [x] v0.2.0 — Codex Context fixture、账号边界、marketplace 安装
- [x] v0.3.0 — 8 行布局、TOOLS/SKILLS 拆分、鼠标滚轮、高度自固定
- [x] v0.3.13 — Ghostty 原生 split、inline scrollback、read-only HUD 与动态高度校准
- [ ] 未来 — 主题配置、多账号摘要、自定义行可见性、性能优化

## 开发验证

```bash
./scripts/verify.sh
```

发布或升级 Codex CLI 前，应重新核对 `docs/metrics-semantics.md` 中的版本敏感公式。

本地修改插件后运行 `./scripts/update.sh`。随后请新开 Codex thread，使 Hook 和 Skill 从新版本加载。

License: MIT
