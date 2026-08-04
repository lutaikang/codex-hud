# Codex HUD

Codex HUD 是一个本地 tmux 侧车面板插件，在 Codex 主面板下方显示 8 行实时遥测信息：会话状态、上下文压力、缓存、工具、子代理、Skills、Todo 和成本估算。

核心原则：不修改 Codex TUI、不读取其他账号历史、可随时开关。

## 快速开始

```bash
./scripts/install.sh
codex-hud on
codex-hud run
```

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
docs/architecture.md                数据流、tmux 路线与设计取舍
docs/metrics-semantics.md           Context/cache/cost 等指标口径
docs/themes.md                      Hub 主题和终端显示规则
docs/troubleshooting.md             安装、账号、版本与 tmux 排障
scripts/install.sh                  校验并安装
scripts/update.sh                   重新安装更新
scripts/uninstall.sh                卸载插件，保留本地偏好
scripts/verify.sh                   单测和校验
```

完整功能、安装方式、隐私边界和显示示例见 [插件 README](plugins/codex-hud/README.md)。

## 设计路线

`codex-hud run` 创建独立的 tmux session：Codex 主面板 + 底部 8 行 HUD 渲染面板。Codex 生命周期 Hook 只在 wrapper 注入 `CODEX_HUD_RUN_ID` 时写本地状态；renderer 增量读取当前 transcript 的 token 事件，并用本地 `codex app-server` 读取当前账号与限额。

Context 百分比基于 Codex 写入 transcript 的原始 `model_context_window` 和 `total_tokens` 值，与 Codex 原生 TUI 口径一致。成本始终标为 API 等价估算。

独立 tmux session 启用 `mouse on`，滚轮在主面板上可进入 copy-mode 浏览历史输出；HUD 面板通过 `bind-key` 排除滚轮捕获（面板高度 == 8 行时不进入 copy-mode）。

### 路线图

- [x] v0.1.0 — 基础 5 行布局、Hook 事件采集、transcript 扫描
- [x] v0.2.0 — Codex 0.146 Context 基线对齐、marketplace 安装
- [x] v0.3.0 — 8 行布局、TOOLS/SKILLS 拆分、鼠标滚轮、高度自固定
- [ ] 未来 — 主题配置、多账号摘要、自定义行可见性、性能优化

## 开发验证

```bash
./scripts/verify.sh
```

发布或升级 Codex CLI 前，应重新核对 `docs/metrics-semantics.md` 中的版本敏感公式。

本地修改插件后运行 `./scripts/update.sh`。随后请新开 Codex thread，使 Hook 和 Skill 从新版本加载。

License: MIT
