# 指标语义

## Context

当前实现使用 Codex transcript 中的原始值，`OFFICIAL_BASELINE_TOKENS = 0`，不再扣除 12,000 token：

```text
effective_window = model_context_window
effective_used   = max(last.total_tokens, 0)
remaining        = max(effective_window - effective_used, 0)
remaining_pct    = round(remaining / effective_window * 100)
used_pct         = 100 - remaining_pct
```

只有 transcript 尚未提供 `model_context_window` 时才显示未知。当 `effective_window == 0` 时，`remaining_pct = 0`，因此显示 `100% used`。

实现按 Rust 非负数 `round()` 行为取整，并先计算剩余比例。当前回归 fixture `122880 / 258400` 必须保持原始 token 数，并得到 `48% used`、`52% left`。

这部分是版本敏感兼容层。本次审计环境为 Codex CLI 0.147.0；升级 Codex 后若 transcript 或原生 TUI 口径变化，应同时更新 `scripts/metrics.py`、测试 fixture 和本文件，不能只改文档中的常量。

## Cache

`cached_input_tokens / input_tokens`。它描述复用比例，不等同于性能健康度，也不包含输出 token。HUD 按复用比例高低着色：<40% 偏红、<70% 偏黄、否则偏绿；这只是复用程度的视觉提示，不是健康度判断。

## 成本

HUD 根据账号边界显示两套不同口径：

- ChatGPT 登录且 transcript 的 `model_provider=openai`：按 OpenAI 官方 token rate card 显示 `≈<credits>cr quota`。这是订阅限额的 token 部分估算；自定义 provider 不计入。
- API key 登录：累计输入扣除缓存输入后按 API 普通输入、缓存输入和输出分别计价，显示 `≈API equiv.$`。

credit rate card 校验日期为 2026-08-11。Fast mode、图像生成、工具专项收费和后续价格变化仍需与 OpenAI usage dashboard 对账。

## Context amplification risk

HUD 额外统计当前 transcript 中的工具调用/返回、图像块和工具返回序列化体积。这些是“上下文被反复重放”的代理信号，不是 OpenAI 账单字段：

- warning：输入 ≥ 5M、工具返回 ≥ 50、模型轮次 ≥ 50、估算 credits ≥ 100、连续至少 5 分钟时燃烧速率 ≥ 5 credits/min、活动子代理 ≥ 2、图像块 ≥ 20，或 payload ≥ 5MB；
- critical：输入 ≥ 20M、工具返回 ≥ 100、模型轮次 ≥ 100、估算 credits ≥ 250、连续至少 5 分钟时燃烧速率 ≥ 10 credits/min、活动子代理 ≥ 3、图像块 ≥ 100，或 payload ≥ 50MB。

这些是本地提前止损护栏，不是 OpenAI 官方限额。达到阈值时 HUD 在 CACHE 行追加 `IO <tool_outputs>t/<images>img/<bytes> · <model_rounds>r`、风险等级和首个原因。建议立即停止继续加工具，保存摘要后开新任务。

`codex-hud audit` 会扫描 `CODEX_HOME/sessions/**/*.jsonl`。根会话按 token 累计差分，子代理在首次 `inter_agent_communication_metadata` 之后开始计量，因而不会把 rollout 中复制的父历史误算两次。报告按 provider 分离原始 token，只对 `model_provider=openai` 计算订阅 credits；同时把根任务与全部后代聚合为 task family，输出模型轮次、工具返回、平均每轮输入、credits 和燃烧速率，避免单会话监控漏掉并行总量。

## Session、工具、子代理和 Todo

- Session 从当前 wrapper 收到首次 `SessionStart` 起计时。
- 工具次数在 `PostToolUse` 累加，进行中耗时从 `PreToolUse` 起算。
- 子代理由 `SubagentStart` / `SubagentStop` 跟踪生命周期；spawn Hook 和 transcript 只补充清洗后的任务名、模型、推理强度和最近动作。推理强度从 spawn 参数、`SubagentStart` 事件与 transcript 中容错探测 `effort`/`reasoning_effort`，缺失时降级为只显示模型。无法确认来源时只显示类型和耗时，不显示完整 prompt、推理或输出全文。
- Todo 只来自 `update_plan` 的 plan，不根据 Git 或最终回复猜测完成状态。TODO 汇总行显示完成度，最多追加两行未完成任务，并用空余行显示最近完成项；当前计划被替换或置空时，最近完成项保留在有上限的会话历史中。Stop 或 SessionEnd 时仍未完成的计划标记为“计划未同步”，clear/resume 切换到新 thread 时清空历史。非法任务项按 unknown 保留，不会被显示为全部完成。

## 账号边界

账号来自当前环境启动的 App Server `account/read`。插件不枚举其他账号目录，也不会把其他账号的历史上下文合并到当前指标。
