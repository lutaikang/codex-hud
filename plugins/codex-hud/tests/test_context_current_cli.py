from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import hud_render
import hud_state
import metrics


class CurrentContextTest(unittest.TestCase):
    def test_observed_codex_0162_context(self) -> None:
        # Real CLI 0.162.1 transcript and rendered native status line, 2026-10-14.
        usage = metrics.official_context_usage(35_007, 475_000)
        assert usage is not None
        self.assertEqual((usage.used_tokens, usage.window_tokens), (23_007, 463_000))
        self.assertEqual((usage.used_percent, usage.remaining_percent), (5, 95))

    def test_native_baseline_boundaries_and_remaining_rounding(self) -> None:
        for total, window, percentages in (
            (0, 12_000, (100, 0)),
            (12_000, 12_001, (0, 100)),
            (13_000, 12_001, (100, 0)),
            (12_062, 12_112, (55, 45)),
        ):
            with self.subTest(total=total, window=window):
                usage = metrics.official_context_usage(total, window)
                assert usage is not None
                self.assertEqual((usage.used_percent, usage.remaining_percent), percentages)

    def test_renderer_uses_effective_context_without_mutating_raw_usage(self) -> None:
        scanner = hud_render.TranscriptScanner()
        scanner.context_window = 475_000
        scanner.last = {"total_tokens": 35_007}
        scanner.total = {"input_tokens": 34_985, "cached_input_tokens": 13_056, "total_tokens": 35_007}
        state = hud_state.default_state("context-fixture")
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
            text = "\n".join(hud_render.render_lines(state, scanner, {}, {}))
        self.assertIn("5% used", text)
        self.assertIn("95% left", text)
        self.assertIn("23k/463k", text)
        self.assertEqual(scanner.last["total_tokens"], 35_007)


if __name__ == "__main__":
    unittest.main()
