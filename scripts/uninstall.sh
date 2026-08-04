#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="$ROOT/plugins/codex-hud/scripts/codex-hud"
LINK="$HOME/.local/bin/codex-hud"

codex plugin remove codex-hud@codex-hud-local
codex plugin marketplace remove codex-hud-local
if [[ -L "$LINK" && "$(readlink "$LINK")" == "$TARGET" ]]; then
  unlink "$LINK"
fi
echo "Plugin, owned command link, and marketplace registration removed."
echo "Local preferences under ~/.codex-hud were preserved."
