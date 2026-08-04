"""Metric semantics shared by the renderer and regression tests."""

from __future__ import annotations

import math
from dataclasses import dataclass


# HUD uses raw Codex values (baseline = 0) for a display that matches the
# model's internal context budget. Versioned for test fixture compatibility.
OFFICIAL_BASELINE_TOKENS = 0


@dataclass(frozen=True)
class ContextUsage:
    used_tokens: int
    window_tokens: int
    used_percent: int
    remaining_percent: int


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
