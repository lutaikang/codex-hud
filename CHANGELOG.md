# Changelog

## 0.3.0

- Expand to 8-line layout: CACHE standalone line, TOOLS split into active/stats lines, new SKILLS line.
- Drop 12k-token baseline subtraction — Context now shows raw Codex values.
- Active tools sorted by most-recently-started (descending) instead of insertion order.
- Add overflow indicators (`… +N more`) for tools, agents, and skills lists.
- Enable `mouse on` in standalone sessions — scroll wheel enters copy-mode in the Codex pane.
- HUD pane excludes scroll capture via `bind-key` (height-based conditional).
- Pin HUD pane height at creation time with `resize-pane` (remove render-loop auto-pin).
- Shared `HUD_LINES` constant replaces scattered magic numbers.
- Skills display (active + cumulative stats) filtered from tool events.

## 0.2.0

- Adopt a strict five-line Hub layout.
- Match Codex 0.146 native Context semantics with a versioned regression fixture.
- Keep account scope restricted to the active `CODEX_HOME`.
- Add reusable marketplace, installer, verification scripts, and design documentation.
