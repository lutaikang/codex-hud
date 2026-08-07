# Codex 插件启动可行性分析

> 审计日期：2026-08-07  
> 审计环境：Codex CLI 0.147.0，codex-hud 0.3.13+codex.20260807054520  
> 范围：只分析启动架构，不修改实现代码

## 结论

Codex HUD **已经能够作为 Codex 插件安装、启用和运行 Hook/Skill**；当前本地 marketplace、插件缓存、启用配置与 Hook trust state 都已生效。

但完整 8 行 HUD **不能只靠“普通 `codex` + 插件自动启动”实现**。当前 Codex 插件接口发生在 Codex 进程内部或会话启动之后，没有观察到以下扩展点：

- 在父 Codex 进程启动前包裹命令；
- 向当前 TUI 注入插件自定义的多行区域；
- 从 `SessionStart` Hook 反向修改父进程参数；
- 让 Hook 安全接管调用者的 Ghostty terminal/split 生命周期。

因此当前最合理的架构是：

```text
Codex plugin
  ├─ manifest / marketplace / cache
  ├─ lifecycle hooks
  └─ skill instructions
            │
            ▼
外部启动 wrapper: codex-hud run
  ├─ 先设置 CODEX_HUD_RUN_ID
  ├─ 先注入 --no-alt-screen
  ├─ 创建 Ghostty split 或 tmux pane
  └─ 再启动 Codex
```

也就是说：**插件负责集成与分发，wrapper 负责 pre-launch 和终端编排。**

## 已确认的现状

| 证据 | 结果 | 含义 |
|---|---|---|
| `codex plugin list` | `codex-hud@codex-hud-local` installed, enabled | 插件安装链路成立 |
| 插件版本 | `0.3.13+codex.20260807054520` | 当前源码已进入 Codex 插件缓存 |
| Codex 配置 | `[plugins."codex-hud@codex-hud-local"] enabled = true` | 插件在当前 `CODEX_HOME` 启用 |
| Hook trust state | SessionStart、Pre/PostToolUse、Subagent、Compact、Stop 等均有 trusted hash | 生命周期 Hook 已被 Codex 接纳 |
| 命令入口 | `~/.local/bin/codex-hud` 指向源码 wrapper | CLI 入口由安装脚本的 symlink 提供 |
| 插件 manifest | 声明 Skill 和交互元数据，没有 pre-launch/bin/TUI surface 字段 | manifest 本身不负责替代 `codex` 启动 |
| `hud_hook.py` | 只有同时满足 HUD enabled 和 `CODEX_HUD_RUN_ID` 才采集 | 普通 `codex` 会话被刻意排除 |
| wrapper | 在启动 Codex 前注入 `--no-alt-screen` 与 run ID | 这些职责无法在事后的 SessionStart Hook 中补做 |

## 为什么 SessionStart Hook 不能替代 wrapper

### 1. 时序不对

`SessionStart` 发生时 Codex 已经启动。此时 Hook 可以记录事件或启动辅助进程，但不能重新为父 Codex 注入 `--no-alt-screen`。没有 inline mode，历史输出和滚轮路径就会与当前 HUD 设计不同。

### 2. 缺少可靠的父终端所有权

Ghostty standalone 路径需要：

- 找到发起 wrapper 的准确 terminal；
- 创建下方 native split；
- 把 HUD surface 设为 read-only；
- 动态校准到 8 行；
- 在 Codex 退出、信号或启动失败时关闭正确的 split。

这些动作需要一个从 Codex 启动前就存在、并持续持有生命周期的 owner。短时 Hook 命令不适合承担这个角色。

### 3. 环境绑定必须先建立

Hook 的隔离边界依赖随机 `CODEX_HUD_RUN_ID`。如果普通插件 Hook 自行创建 run ID，前面的事件、renderer 与父进程并没有共同的启动上下文；子 Codex 还可能继承环境，增加 thread 误绑定风险。

### 4. Hook trust 与失败策略不适合做主启动器

当前 Hook timeout 为 2 秒，设计目标是“遥测失败不能影响 Codex”。终端编排却需要更长的 ready/geometry handshake、信号处理和确定性 cleanup。把两者合并会破坏现有的 fail-open 观测原则。

## 可选方案比较

| 方案 | 可行性 | 用户体验 | 主要问题 |
|---|---:|---|---|
| 当前混合架构：插件 + `codex-hud run` | 高 | 明确、稳定 | 需要使用专用启动命令 |
| Shell alias/function 把 `codex` 转发到 wrapper | 中高 | 看起来像直接运行 `codex` | 不属于插件能力；要处理递归、升级和绕过方式 |
| SessionStart Hook 自动创建 HUD | 低 | 表面自动 | 无法补注入父进程参数，缺少终端 owner，cleanup 脆弱 |
| Skill 在当前 thread 内执行 `codex-hud run` | 不建议 | 对话式 | 会启动嵌套交互 Codex，环境和 thread 可能互相干扰 |
| 只启动 headless renderer/写状态 | 中 | 无可视 HUD | 可用于遥测采集，但不满足 8 行侧车目标 |
| Codex 原生多行插件 surface / pre-launch hook | 理论上最佳 | 真正插件原生 | 当前 CLI 0.147.0 暂未观察到该扩展点，需要 Codex 核心支持 |

## “以插件方式启动”可以分成三种目标

### 目标 A：通过插件安装和管理

已实现。marketplace、manifest、cache、enable、Hook、Skill 都正常。

### 目标 B：用户对 Codex 说“启动 HUD”

部分实现。Skill 可以安全执行 `on`、`off`、`status`，并指导用户在下一个外部终端会话运行 `codex-hud run`。它不应在当前 thread 内启动嵌套 Codex。

### 目标 C：用户只输入普通 `codex`，插件自动出现 8 行 HUD

当前插件接口下不可直接实现。可以在未来增加 shell 层 shim/alias 获得“一条 `codex` 命令”的体验，但本质仍是 wrapper，不是纯插件自启动。

## 建议

1. 保持当前“插件负责 Hook/Skill，wrapper 负责 pre-launch/终端 surface”的架构。
2. 文档明确区分 `codex plugin add`、`codex-hud on` 和 `codex-hud run`：安装、偏好、实际启动是三个不同动作。
3. 如果后续只优化入口体验，优先评估可显式退出的 shell function 或独立 shim，并保留 `command codex` 作为 bypass；这属于新功能，应另开设计与实现任务。
4. 若 Codex 未来提供 pre-launch hook 或可写的多行 TUI plugin surface，再重新评估去掉 wrapper。
5. 不把 `SessionStart` Hook 扩展成主布局启动器，也不从当前 Codex thread 内启动嵌套交互会话。

## 最终判断

- **能否作为 Codex 插件存在和安装：能，而且已经如此。**
- **能否由插件 Hook 在普通 `codex` 启动后自动补出完整 HUD：当前不能可靠实现。**
- **能否让用户获得近似“直接运行 codex”的体验：可以通过 shell/wrapper 层实现，但仍不是纯插件能力。**
- **当前推荐方案：保留混合架构，不修改代码。**

