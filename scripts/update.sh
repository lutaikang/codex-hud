#!/usr/bin/env bash
set -euo pipefail

if [[ -n "${CODEX_HUD_RUN_ID:-}" || -n "${CODEX_THREAD_ID:-}" ]]; then
  echo "Refusing to update codex-hud from an active Codex task." >&2
  echo "Use an external terminal after closing all Codex tasks and app servers." >&2
  exit 2
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python3 "$ROOT/scripts/check_update_idle.py"
MANIFEST="$ROOT/plugins/codex-hud/.codex-plugin/plugin.json"
BACKUP="$(mktemp)"
cp "$MANIFEST" "$BACKUP"
UPDATE_OK=0

cleanup() {
  if [[ "$UPDATE_OK" -ne 1 ]]; then
    cp "$BACKUP" "$MANIFEST"
  fi
  rm -f "$BACKUP"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP

python3 "$ROOT/scripts/cachebuster.py"
"$ROOT/scripts/verify.sh"
codex plugin add codex-hud@codex-hud-local
UPDATE_OK=1
echo "Updated. Start a new Codex thread to load the refreshed plugin."
