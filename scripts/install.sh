#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="$ROOT/plugins/codex-hud/scripts/codex-hud"
LINK="$HOME/.local/bin/codex-hud"

if [[ -e "$LINK" || -L "$LINK" ]]; then
  if [[ ! -L "$LINK" || "$(readlink "$LINK")" != "$TARGET" ]]; then
    echo "Refusing to overwrite existing command: $LINK" >&2
    exit 1
  fi
fi

"$ROOT/scripts/verify.sh"
codex plugin marketplace add "$ROOT"
codex plugin add codex-hud@codex-hud-local
mkdir -p "$HOME/.local/bin"
if [[ ! -L "$LINK" ]]; then
  ln -s "$TARGET" "$LINK"
fi
echo "Installed. Run: codex-hud on && codex-hud run"
