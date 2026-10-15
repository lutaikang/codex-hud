#!/usr/bin/env python3
"""Refuse shared plugin-cache updates while a Codex process may still use it."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


CODEX_PROCESS_NAMES = frozenset(
    {
        "codex",
        "codex-app-server",
        "codex-app-server-service",
        "codex-exec-server",
        "code-mode-host",
        "codex-code-mode-host",
        "codex-code-mode",  # Linux may truncate comm to 15 characters.
    }
)


def codex_processes(output: str) -> list[tuple[int, int, str]]:
    """Parse ps's PID, PPID and command columns without exposing full arguments."""
    lines = output.splitlines()
    if not lines or not any(line.strip() for line in lines):
        raise ValueError("ps returned an empty process table")
    found = []
    for line in lines:
        if not line.strip():
            continue
        fields = line.split(maxsplit=2)
        if len(fields) != 3:
            raise ValueError("ps returned an invalid process table")
        try:
            pid, ppid = int(fields[0]), int(fields[1])
        except ValueError as error:
            raise ValueError("ps returned an invalid process table") from error
        if pid <= 0 or ppid < 0:
            raise ValueError("ps returned invalid process IDs")
        name = Path(fields[2]).name
        normalized = name.lower()
        if normalized in CODEX_PROCESS_NAMES or normalized.startswith("codex helper ("):
            found.append((pid, ppid, name))
    return found


def main() -> int:
    try:
        result = subprocess.run(
            ["ps", "-axww", "-o", "pid=,ppid=,comm="],
            capture_output=True,
            text=True,
            timeout=3,
            check=True,
        )
        processes = codex_processes(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        print("Refusing to update: unable to inspect the running process table.", file=sys.stderr)
        return 2

    if processes:
        print("Refusing to update: Codex processes may still use the plugin cache:", file=sys.stderr)
        for pid, ppid, name in processes:
            print(f"  PID {pid}, PPID {ppid}: {name}", file=sys.stderr)
        print("Close Codex tasks and app servers yourself; no processes were stopped.", file=sys.stderr)
        return 2

    # A process-table snapshot cannot prevent another Codex process starting later.
    print("Idle preflight passed. Keep all Codex tasks and app servers closed until update finishes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
