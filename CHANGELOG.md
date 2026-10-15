# Changelog

## Unreleased - 2026-10-14

- Keep HUD hooks in the launch environment with capability-gated `--no-daemon`
  on current Codex CLI; reject unsupported remote HUD sessions.
- Restore the native TUI context baseline of 12,000 tokens, checked against
  Codex CLI 0.162.1 and a real transcript/status-line pair.
- Cancel and reap the owned account-query App Server on renderer shutdown,
  including TERM/HUP, without stopping the shared Codex service.
- Refuse plugin-cache updates from any Codex task or while Codex processes
  remain active; leave all existing sessions and hook trust untouched.

## 0.3.15 - 2026-08-10

- Preserve bounded per-session TODO history with stable internal task IDs,
  revision snapshots, transition timestamps, and recent completed-task rows.
- Enrich AGENTS with sanitized task names, models, current activity, source
  confidence, and detailed CLI inspection without storing prompt bodies or
  full agent output.
- Expand dynamic HUD geometry to at most 11 rows when current tasks and recent
  completions both need detail rows.

## 0.3.14 - 2026-08-08

- Keep TODO plans turn-aware: empty `update_plan` calls clear stale state, while
  unfinished plans at Stop are labeled unsynchronized instead of being guessed
  complete.
- Expand the HUD from its base 8 rows to at most 10 rows to show the current and
  next unfinished tasks, with stable overflow counts and dynamic Ghostty/tmux
  resizing.
- Preserve the TODO summary and first detail row while a surface is temporarily
  between its 8-row base and expanded height.
- Harden malformed-plan migration, same-turn state transitions, SessionEnd
  cleanup, narrow-surface rendering, observed tmux height reconciliation, and
  Ghostty renderer heartbeat expiry.
- Align root and packaged documentation with the current 0.3.14 implementation.
- Correct Context documentation to the raw-token baseline used by `metrics.py` and its regression fixtures.
- Document the boundary between Codex plugin installation/Hook loading and the external `codex-hud run` pre-launch wrapper.
- Remove duplicated fallback guidance and archive-only local E2E artifacts from the working tree.

## 0.3.13

- Use a native Ghostty split for macOS standalone launches so the Codex main
  surface keeps Ghostty-native scrollback, drag-selection, release retention,
  and right-click behavior. tmux remains only for nested/fallback launches.
- Retain Codex's official `--no-alt-screen` wrapper argument so transcript
  output remains in the owning terminal's scrollback.
- Locate the calling Ghostty surface with a unique OSC 7 working-directory
  marker instead of guessing the focused/front terminal in multi-window setups.
- Make native HUD startup, resize, signal handling, timeout recovery, and
  terminal cleanup deterministic; orphan splits are recovered by their unique
  per-run working directory.
- Keep fallback tmux mouse leases and legacy-binding migration isolated from
  native Ghostty behavior.

## 0.3.12

- Correct the 0.3.11 default-path regression: Codex 0.146 uses alternate screen
  by default, so tmux's native `WheelUpPane` forwarded a raw mouse event that
  Codex intentionally ignores. HUD launches now add Codex's official
  `--no-alt-screen` option, preserving transcript output in terminal scrollback.
- Wait for the HUD renderer's `mouse_any_flag=1` handshake before enabling tmux
  mouse routing, preventing the HUD pane from briefly entering copy-mode during
  startup.
- Install TERM/HUP cleanup before acquiring the embedded mouse lease and guard
  cleanup against a second termination signal; the standalone owner now uses
  the same cleanup path.
- Scope remains historical scrolling only. tmux mouse routing still changes
  drag selection and right-click behavior; those are separate unresolved bugs.

## 0.3.11

- Restore historical-output scrolling by enabling tmux mouse routing for the
  Codex pane; tmux's native `WheelUpPane` enters `copy-mode -e`, and scrolling
  back to the bottom automatically returns to the live Codex view.
- Keep the HUD pane out of copy-mode without installing server-global key
  bindings: the renderer advertises mouse capture and tmux disables pane input.
- Embedded launches keep the concurrent lease/restore behavior, but the leased
  state is now `mouse on`; standalone launches remain isolated on the dedicated
  `codex-hud` socket.
- Convert `SIGTERM`/`SIGHUP` into normal wrapper cleanup so embedded sessions
  restore the original mouse option instead of leaving a dead lease behind.
- Scope note: this release fixes historical scrolling. With tmux mouse routing
  enabled, drag selection and right-click still use tmux defaults rather than
  Ghostty's native selection/context-menu path.

## 0.3.10

- Restore exact inherited or explicit tmux `mouse` state after embedded HUD
  exits; concurrent HUD launches now use a process-safe lease set.
- Migrate only codex-hud's old marker bindings back to the installed tmux
  defaults, without touching user-defined bindings.
- Remove the HUD renderer's stdin drain; with tmux mouse capture disabled,
  Ghostty owns terminal mouse input and the renderer has no input stream to
  consume.

## 0.3.9

- Let Ghostty own Codex-area scrolling, selection, and right-click context
  actions by disabling tmux mouse capture for the HUD window.
- Run standalone HUD sessions on a dedicated `codex-hud` tmux socket so
  mouse bindings and terminal options cannot pollute ordinary tmux sessions.
- Keep tmux's `tmux-256color` capability inside the pane; do not pass the
  outer Ghostty `TERM` value into Codex.

## 0.3.8

- Fix stranded copy-mode: double/triple-click no longer enters copy-mode
  (Codex's full-screen redraw leaves almost nothing in scrollback, so
  copy-mode showed blanks — the "display abnormal" wheel bug). Wheel
  while stranded in copy-mode now cancels copy-mode first, then sends
  Up/Down to Codex so conversation scrolling always works.
- copy-mode drag-release now uses copy-selection-and-cancel (exits
  copy-mode after copying).

## 0.3.7

- Fix right-click: the MouseDown3Pane binding's triple-nested escaping
  failed to register ("if-shell: too many arguments" swallowed by
  check=False), so right-click fell through to the default menu.
  Simplified to a single gated if-shell + display-menu -O (verified
  15/15 bindings now register cleanly).

## 0.3.6

- Wheel sends bare Up / Down to the Codex pane — exactly what a
  terminal's alternate scroll sends (verified live: Up scrolls to
  earlier conversation with a clean render; Shift+Up behaves the same,
  so the Ghostty-equivalent unshifted sequence is used).
- Fix drag-selection: MouseDrag1Pane now uses `copy-mode -M -t =`
  (the -M flag starts the selection immediately; without it the pane
  entered copy-mode but never began selecting — "cannot select" bug).

## 0.3.5

- Wheel now sends Shift+Up / Shift+Down to the Codex pane — the keys
  Codex natively binds to conversation scrolling (verified live: a
  shifted-up scroll shows earlier conversation). Bare Up/Down and
  tmux copy-mode over scrollback were both proven wrong.
- MouseDragEnd1Pane: finish a drag with copy-selection-and-cancel
  (the previous empty else-branch left the pane stuck in copy-mode,
  which made selections persist after release).

## 0.3.4

- Rewrite mouse bindings with pane-identity gating (@is_hud on the HUD
  pane, @hud_main_pane session option). HUD pane drops all mouse events
  and cannot take focus; the Codex pane keeps tmux 3.6b default
  behaviour (alternate_on / pane_in_mode / mouse_any_flag conditions).
- Wheel: Codex pane → copy-mode -e over tmux scrollback (the faithful
  equivalent of Ghostty's terminal scrollback scrolling; the previous
  send-keys Up/Down approach was proven wrong — Codex ignores arrow keys).
- Selection: click elsewhere in copy-mode clears the selection and
  leaves copy-mode (copy-mode MouseDown1Pane → clear-selection; cancel).
- Right-click: display-menu -O (button-release no longer closes the
  menu — the "flash and disappear" bug), gated so the HUD shows no menu.
- hud_render.py drains stdin every second (stray wheel/typed bytes into
  the HUD pane no longer accumulate in the pty buffer).
- Removed the send-keys -N 5 Up/Down wheel bindings from 0.3.3.

## 0.3.3

- Fix wheel scrolling in the Codex pane: bind WheelUp/DownPane to send
  arrow keys, mimicking a terminal's alternate scroll. Codex does not
  enable mouse reporting (verified by pty capture), so Ghostty scrolls
  it by translating the wheel into Up/Down keys; tmux now does the same.
- Remove all other custom mouse bindings (residual server-level bind-keys
  from earlier versions were polluting every session).
- Remove --no-alt-screen: Codex now runs with its native full-screen TUI.
- Keep tmux defaults for click / selection / paste / right-click so the
  Codex pane retains original TUI behaviour.

## 0.3.1

- Fix test-session bind-key pollution overwriting codex-hud mouse bindings.
- Add @is_hud gates to copy-mode tables (MouseDown1Pane, WheelUp/DownPane).
- Fix copy-mode MouseDown1Pane: clear-selection on click elsewhere.
- Fix DoubleClick/TripleClick multi-command syntax (single-string with \;).
- Fix if-shell multi-word commands with tmux {} wrapping.
- Add --no-alt-screen for inline mode (preserves terminal scrollback).

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
