"""Metric semantics shared by the renderer and regression tests."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


# HUD uses raw Codex values (baseline = 0) for a display that matches the
# model's internal context budget. Versioned for test fixture compatibility.
OFFICIAL_BASELINE_TOKENS = 0

# ChatGPT/Codex credits per 1M tokens. This is intentionally separate from
# API-equivalent dollar pricing: subscription limits are consumed in credits.
# Source (checked 2026-08-11): https://learn.chatgpt.com/docs/pricing
CHATGPT_CREDIT_RATES_PER_MILLION = {
    "gpt-5.6-sol": (125.0, 12.5, 750.0),
    "gpt-5.6": (125.0, 12.5, 750.0),
    "daybreak-blue": (125.0, 12.5, 750.0),
    "daybreak-red": (312.5, 31.25, 1875.0),
    "gpt-5.6-terra": (50.0, 5.0, 300.0),
    "gpt-5.6-luna": (5.0, 0.5, 30.0),
    "gpt-5.5": (125.0, 12.5, 750.0),
    "gpt-5.4": (62.5, 6.25, 375.0),
    "gpt-5.4-mini": (18.75, 1.875, 113.0),
}


@dataclass(frozen=True)
class ContextUsage:
    used_tokens: int
    window_tokens: int
    used_percent: int
    remaining_percent: int


@dataclass(frozen=True)
class SessionRisk:
    level: str
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class CreditEstimate:
    uncached_input: float
    cached_input: float
    output: float
    total: float


def estimate_chatgpt_credits(
    model: str | None,
    usage: Mapping[str, Any],
) -> CreditEstimate | None:
    """Estimate ChatGPT/Codex credits from token usage for a known model."""
    if not model:
        return None
    normalized = model.lower().strip()
    rates = CHATGPT_CREDIT_RATES_PER_MILLION.get(normalized)
    if rates is None:
        for name in sorted(CHATGPT_CREDIT_RATES_PER_MILLION, key=len, reverse=True):
            if normalized.startswith(name):
                rates = CHATGPT_CREDIT_RATES_PER_MILLION[name]
                break
    if rates is None:
        return None

    input_tokens = max(0, int(usage.get("input_tokens") or 0))
    cached_tokens = min(
        input_tokens,
        max(0, int(usage.get("cached_input_tokens") or 0)),
    )
    output_tokens = max(0, int(usage.get("output_tokens") or 0))
    uncached_tokens = input_tokens - cached_tokens
    input_rate, cached_rate, output_rate = rates
    uncached_cost = uncached_tokens * input_rate / 1_000_000
    cached_cost = cached_tokens * cached_rate / 1_000_000
    output_cost = output_tokens * output_rate / 1_000_000
    return CreditEstimate(
        uncached_input=uncached_cost,
        cached_input=cached_cost,
        output=output_cost,
        total=uncached_cost + cached_cost + output_cost,
    )


def session_risk(
    input_tokens: int | None,
    tool_outputs: int | None,
    image_blocks: int | None,
    tool_output_bytes: int | None,
    active_agents: int | None = 0,
    model_rounds: int | None = 0,
    estimated_credits: float | None = None,
    elapsed_seconds: float | None = None,
) -> SessionRisk:
    """Classify local quota-amplification risk.

    Thresholds are operational guardrails, not provider limits. They are set
    below the observed 2026-08-10 incident so the HUD warns before a single
    task family can consume a weekly allowance.
    """
    input_value = max(0, int(input_tokens or 0))
    tool_value = max(0, int(tool_outputs or 0))
    image_value = max(0, int(image_blocks or 0))
    bytes_value = max(0, int(tool_output_bytes or 0))
    agents_value = max(0, int(active_agents or 0))
    rounds_value = max(0, int(model_rounds or 0))
    credits_value = max(0.0, float(estimated_credits or 0.0))
    elapsed_value = max(0.0, float(elapsed_seconds or 0.0))
    credits_per_minute = (
        credits_value / (elapsed_value / 60.0)
        if credits_value and elapsed_value >= 300
        else 0.0
    )
    reasons: list[str] = []
    critical = False

    if input_value >= 20_000_000:
        reasons.append("input>=20m")
        critical = True
    elif input_value >= 5_000_000:
        reasons.append("input>=5m")
    if tool_value >= 100:
        reasons.append("tool_outputs>=100")
        critical = True
    elif tool_value >= 50:
        reasons.append("tool_outputs>=50")
    if image_value >= 100:
        reasons.append("images>100")
        critical = True
    elif image_value >= 20:
        reasons.append("images>20")
    if bytes_value >= 50_000_000:
        reasons.append("payload>50MB")
        critical = True
    elif bytes_value >= 5_000_000:
        reasons.append("payload>5MB")
    if agents_value >= 3:
        reasons.append("agents>=3")
        critical = True
    elif agents_value >= 2:
        reasons.append("agents>=2")
    if rounds_value >= 100:
        reasons.append("model_rounds>=100")
        critical = True
    elif rounds_value >= 50:
        reasons.append("model_rounds>=50")
    if credits_value >= 250:
        reasons.append("credits>=250")
        critical = True
    elif credits_value >= 100:
        reasons.append("credits>=100")
    if credits_per_minute >= 10:
        reasons.append("burn>=10cr/min")
        critical = True
    elif credits_per_minute >= 5:
        reasons.append("burn>=5cr/min")

    return SessionRisk(
        level="critical" if critical else "warning" if reasons else "normal",
        reasons=tuple(reasons),
    )


def _rust_round_nonnegative(value: float) -> int:
    """Match Rust f64::round for the non-negative percentages used here."""
    return int(math.floor(value + 0.5))


def official_context_usage(
    last_total_tokens: int | None,
    model_context_window: int | None,
    baseline_tokens: int = OFFICIAL_BASELINE_TOKENS,
) -> ContextUsage | None:
    """Reproduce Codex TUI's effective context-window calculation.

    Codex subtracts the same baseline from both the latest turn total and the
    model window, then rounds the *remaining* percentage. Used percentage is
    `100 - remaining`, which is important at rounding boundaries.
    """
    total = max(0, int(last_total_tokens or 0))
    window = max(0, int(model_context_window or 0))
    baseline = max(0, int(baseline_tokens))
    if model_context_window is None:
        return None
    effective_window = max(window - baseline, 0)
    effective_used = max(total - baseline, 0)
    remaining = max(effective_window - effective_used, 0)
    remaining_percent = (
        min(100, _rust_round_nonnegative(remaining / effective_window * 100))
        if effective_window > 0
        else 0
    )
    return ContextUsage(
        used_tokens=effective_used,
        window_tokens=effective_window,
        used_percent=100 - remaining_percent,
        remaining_percent=remaining_percent,
    )
