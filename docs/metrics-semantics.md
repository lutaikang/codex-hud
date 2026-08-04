# 指标语义

## Context

Codex 0.146 原生 TUI 使用 12,000 token 基线：

```text
effective_window = model_context_window - 12000
effective_used   = max(last.total_tokens - 12000, 0)
remaining        = max(effective_window - effective_used, 0)
remaining_pct    = round(remaining / effective_window * 100)
used_pct         = 100 - remaining_pct
```

当 `effective_window == 0` 时，官方返回 `remaining_pct = 0`，所以显示 `100% used`；只有 transcript 尚无 `model_context_window` 时才显示未知。

实现按 Rust 非负数 `round()` 行为取整，并先算剩余比例。fixture `122880 / 258400` 必须得到 `110880 / 246400`、`45% used`、`55% left`。

## Cache

`cached_input_tokens / input_tokens`。它描述复用比例，不等同于性能健康度，也不包含输出 token。

## 成本

累计输入中扣除缓存输入后按普通输入价计算，缓存输入和输出分别计价。该值标记为 `≈$`，是内置参考价格下的 API 等价估算，不是用户实际账单。

## Session、工具、子代理和 Todo

- Session 从当前 wrapper 收到首次 `SessionStart` 起计时。
- 工具次数在 `PostToolUse` 累加，进行中耗时从 `PreToolUse` 起算。
- 子代理由 `SubagentStart` / `SubagentStop` 跟踪。
- Todo 来自 `update_plan` 的 plan；只显示当前进行项或首个 pending 项。

## 账号边界

账号来自当前环境启动的 App Server `account/read`。插件不枚举其他账号目录，也不会把其他账号的历史上下文合并到当前指标。
