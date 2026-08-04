#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONPYCACHEPREFIX="$ROOT/.pycache"
python3 -m unittest discover -s "$ROOT/plugins/codex-hud/tests" -v
python3 -m py_compile "$ROOT/plugins/codex-hud/scripts/"*.py
python3 -m json.tool "$ROOT/.agents/plugins/marketplace.json" >/dev/null
python3 -m json.tool "$ROOT/plugins/codex-hud/.codex-plugin/plugin.json" >/dev/null
python3 -m json.tool "$ROOT/plugins/codex-hud/hooks/hooks.json" >/dev/null
echo "Codex HUD verification passed."
