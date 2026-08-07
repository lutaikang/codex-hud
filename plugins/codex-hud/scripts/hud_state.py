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


STATE_VERSION = 4
HUD_MIN_LINES = 8
HUD_MAX_LINES = 10
# Backward-compatible name used by the launcher and existing callers.
HUD_LINES = HUD_MIN_LINES
HUD_VERSION = "0.3.13"
KNOWN_PLAN_STATUSES = {"pending", "in_progress", "completed"}


def normalize_plan(value: Any) -> list[dict[str, str]]:
    """Keep the Codex plan shape small without hiding malformed tasks."""
    if not isinstance(value, list):
        return []
    plan: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            plan.append({"step": "未命名任务", "status": "unknown"})
            continue
        step = item.get("step")
        status = item.get("status")
        if not isinstance(step, str) or not step.strip():
            step = "未命名任务"
        if not isinstance(status, str):
            status = "unknown"
        if status == "inProgress":
            status = "in_progress"
        if status not in KNOWN_PLAN_STATUSES:
            status = "unknown"
        plan.append({"step": step[:240], "status": status})
    return plan[:100]


def unfinished_plan_items(plan: Any) -> list[dict[str, str]]:
    """Return unfinished items with the active item shown before pending items."""
    normalized = normalize_plan(plan)
    indexed = list(enumerate(item for item in normalized if item["status"] != "completed"))
    indexed.sort(key=lambda entry: (0 if entry[1]["status"] == "in_progress" else 1, entry[0]))
    return [item for _, item in indexed]


def plan_projection(plan: Any) -> dict[str, Any]:
    """Return one canonical plan view for state, layout, and rendering."""
    normalized = normalize_plan(plan)
    unfinished = unfinished_plan_items(normalized)
    return {
        "items": normalized,
        "completed": sum(item["status"] == "completed" for item in normalized),
        "in_progress": sum(item["status"] == "in_progress" for item in normalized),
        "pending": sum(item["status"] == "pending" for item in normalized),
        "unknown": sum(item["status"] == "unknown" for item in normalized),
        "unfinished": unfinished,
    }


def plan_state_for(plan: Any, *, stale: bool = False) -> str:
    normalized = normalize_plan(plan)
    if not normalized:
        return "empty"
    if all(item["status"] == "completed" for item in normalized):
        return "completed"
    return "stale" if stale else "active"


def desired_hud_lines(state: dict[str, Any]) -> int:
    """Return the surface height needed for the TODO detail rows."""
    detail_rows = min(2, len(unfinished_plan_items(state.get("plan"))))
    return min(HUD_MAX_LINES, HUD_MIN_LINES + detail_rows)


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
        "plan_state": "empty",
        "plan_turn_id": None,
        "plan_updated_at": None,
        "plan_revision": 0,
        "current_turn_id": None,
        "compactions": 0,
    }


def load_state(run_id: str) -> dict[str, Any]:
    path = run_path(run_id)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default_state(run_id)
    if not isinstance(value, dict):
        return default_state(run_id)
    defaults = default_state(run_id)
    for key, default in defaults.items():
        value.setdefault(key, default)
    value["plan"] = normalize_plan(value.get("plan"))
    normalized_state = plan_state_for(value["plan"])
    if normalized_state in {"empty", "completed"}:
        value["plan_state"] = normalized_state
    elif value.get("plan_state") not in {"active", "stale"}:
        value["plan_state"] = normalized_state
    return value


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
