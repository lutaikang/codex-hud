#!/usr/bin/env python3
"""Give the plugin version one fresh Codex cachebuster suffix."""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    manifest_path = root / "plugins/codex-hud/.codex-plugin/plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    base_version = str(manifest["version"]).split("+", 1)[0]
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    token = re.sub(r"[^a-z0-9-]", "-", timestamp.lower())
    manifest["version"] = f"{base_version}+codex.{token}"
    descriptor, temporary_name = tempfile.mkstemp(
        dir=manifest_path.parent, prefix=f".{manifest_path.name}.", suffix=".tmp"
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        os.replace(temporary_path, manifest_path)
    finally:
        temporary_path.unlink(missing_ok=True)
    print(f"Plugin version: {manifest['version']}")


if __name__ == "__main__":
    main()
