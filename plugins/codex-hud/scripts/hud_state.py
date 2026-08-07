"""Shared private state helpers for Codex HUD."""

from __future__ import annotations

import json
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback for future ports
    fcntl = None


STATE_VERSION = 2
HUD_LINES = 8
HUD_VERSION = "0.3.13"


def hud_home() -> Path:
    return Path(os.environ.get("CODEX_HUD_HOME", "~/.codex-hud")).expanduser()


def config_path() -> Path:
    return hud_home() / "config.json"


def enabled() -> bool:
    try:
        value = json.loads(config_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return False
    return value.get("enabled") is True


def set_enabled(value: bool) -> None:
    home = hud_home()
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    _atomic_json_write(config_path(), {"enabled": value})


def run_path(run_id: str) -> Path:
    safe_id = "".join(char for char in run_id if char.isalnum() or char in "-_")
    if not safe_id:
        raise ValueError("run_id must contain at least one safe character")
    return hud_home() / "runs" / f"{safe_id}.json"


def default_state(run_id: str) -> dict[str, Any]:
    now = time.time()
    return {
        "version": STATE_VERSION,
        "run_id": run_id,
        "session_id": None,
        "thread_id": None,
        "account_scope": None,
        "cwd": None,
        "model": None,
        "permission_mode": None,
        "transcript_path": None,
        "started_at": now,
        "last_event_at": now,
        "status": "starting",
        "active_tools": {},
        "tool_stats": {},
        "agents": {},
        "plan": [],
        "compactions": 0,
    }


def load_state(run_id: str) -> dict[str, Any]:
    path = run_path(run_id)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default_state(run_id)
    return value if isinstance(value, dict) else default_state(run_id)


def update_state(
    run_id: str, mutator: Callable[[dict[str, Any]], None]
) -> dict[str, Any]:
    path = run_path(run_id)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = path.with_suffix(".lock")
    with _locked(lock_path):
        state = load_state(run_id)
        mutator(state)
        state["version"] = STATE_VERSION
        state["last_event_at"] = time.time()
        _atomic_json_write(path, state)
        return state


def mark_ended(run_id: str) -> None:
    update_state(run_id, lambda state: state.update(status="ended"))


def remove_run(run_id: str) -> None:
    """Remove session telemetry after the owning Codex process exits."""
    path = run_path(run_id)
    for candidate in (path, path.with_suffix(".lock")):
        try:
            candidate.unlink()
        except FileNotFoundError:
            pass


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if fcntl is not None:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        if fcntl is not None:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _atomic_json_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
        os.replace(temporary_path, path)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
