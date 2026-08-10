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

`cached_input_tokens / input_tokens`。它描述复用比例，不等同于性能健康度，也不包含输出 token。

## 成本

累计输入中扣除缓存输入后按普通输入价计算，缓存输入和输出分别计价。该值标记为 `≈API equiv.$`，是内置参考价格下的 API 等价估算，不是用户实际账单。

## Session、工具、子代理和 Todo

- Session 从当前 wrapper 收到首次 `SessionStart` 起计时。
- 工具次数在 `PostToolUse` 累加，进行中耗时从 `PreToolUse` 起算。
- 子代理由 `SubagentStart` / `SubagentStop` 跟踪生命周期；spawn Hook 和 transcript 只补充清洗后的任务名、模型和最近动作。无法确认来源时只显示类型和耗时，不显示完整 prompt、推理或输出全文。
- Todo 只来自 `update_plan` 的 plan，不根据 Git 或最终回复猜测完成状态。第 8 行显示汇总，最多追加两行未完成任务，并用空余行显示最近完成项；当前计划被替换或置空时，最近完成项保留在有上限的会话历史中。Stop 或 SessionEnd 时仍未完成的计划标记为“计划未同步”，clear/resume 切换到新 thread 时清空历史。非法任务项按 unknown 保留，不会被显示为全部完成。

## 账号边界

账号来自当前环境启动的 App Server `account/read`。插件不枚举其他账号目录，也不会把其他账号的历史上下文合并到当前指标。
