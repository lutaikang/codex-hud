---
name: codex-hud
description: Manage the local Codex HUD when the user asks to enable, disable, toggle, inspect, or start it.
---

# Codex HUD

Use the installed plugin command at `$PLUGIN_ROOT/scripts/codex-hud`.

- To enable the multi-line HUD, run `"$PLUGIN_ROOT/scripts/codex-hud" on`.
- To disable it, run `"$PLUGIN_ROOT/scripts/codex-hud" off`.
- To inspect dependencies and current state, run `"$PLUGIN_ROOT/scripts/codex-hud" status`.
- Do not start a nested interactive Codex session from inside an existing Codex thread. Tell the user to run `codex-hud run` in a terminal for the next session.

The HUD is local-only and scoped to the current `CODEX_HOME` account. On macOS standalone Ghostty it uses a native split; nested tmux and non-Ghostty terminals use the tmux fallback. Cost is an API-equivalent estimate, not a ChatGPT subscription bill. Cache hit rate is derived from cached input tokens divided by input tokens.
