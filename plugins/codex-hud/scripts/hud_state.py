"""Shared private state helpers for Codex HUD."""

from __future__ import annotations

import json
import hashlib
import os
import re
import tempfile
import time
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback for future ports
    fcntl = None


STATE_VERSION = 5
HUD_MIN_LINES = 8
HUD_MAX_LINES = 11
# Backward-compatible name used by the launcher and existing callers.
HUD_LINES = HUD_MIN_LINES
HUD_VERSION = "0.3.15"
KNOWN_PLAN_STATUSES = {"pending", "in_progress", "completed"}
PLAN_HISTORY_LIMIT = 20
PLAN_REVISION_LIMIT = 20
TRANSITION_LIMIT = 20
DISPLAY_TEXT_LIMIT = 240
AGENT_LIMIT = 20
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_BIDI_CHARS = {
    "\u061c", "\u200e", "\u200f", "\u202a", "\u202b", "\u202c",
    "\u202d", "\u202e", "\u2066", "\u2067", "\u2068", "\u2069",
}


def sanitize_display_text(value: Any, limit: int = DISPLAY_TEXT_LIMIT) -> str:
    """Remove terminal/control payloads before text reaches state or the HUD."""
    if not isinstance(value, str):
        return ""
    cleaned = _OSC_RE.sub("", _ANSI_RE.sub("", value))
    output: list[str] = []
    for character in cleaned:
        if character in _BIDI_CHARS:
            continue
        if unicodedata.category(character) == "Cc":
            if character in {"\t", "\n", "\r"}:
                output.append(" ")
            continue
        output.append(character)
    return " ".join("".join(output).split())[: max(1, int(limit))]


def normalize_agents(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        return {}
    entries: list[tuple[str, dict[str, Any]]] = []
    for raw_id, raw_agent in value.items():
        if not isinstance(raw_agent, dict):
            continue
        agent_id = sanitize_display_text(str(raw_id), 120) or "agent"
        status = str(raw_agent.get("status") or "running")
        if status not in {"running", "completed"}:
            status = "running"
        entries.append(
            (
                agent_id,
                {
                    "type": sanitize_display_text(raw_agent.get("type"), 80) or "agent",
                    "status": status,
                    "started_at": raw_agent.get("started_at"),
                    "ended_at": raw_agent.get("ended_at"),
                    "task_name": sanitize_display_text(raw_agent.get("task_name"), 80) or None,
                    "description": sanitize_display_text(raw_agent.get("description"), 120) or None,
                    "model": sanitize_display_text(raw_agent.get("model"), 80) or None,
                    "current_activity": sanitize_display_text(raw_agent.get("current_activity"), 80) or None,
                    "last_tool": sanitize_display_text(raw_agent.get("last_tool"), 80) or None,
                    "updated_at": raw_agent.get("updated_at"),
                    "source_confidence": sanitize_display_text(
                        raw_agent.get("source_confidence"), 40
                    )
                    or "hook",
                },
            )
        )
    entries.sort(key=lambda pair: _numeric_time(pair[1].get("updated_at") or pair[1].get("started_at")))
    return dict(entries[-AGENT_LIMIT:])


def _numeric_time(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) else 0.0


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
        step = sanitize_display_text(step)
        if not step:
            step = "未命名任务"
        if not isinstance(status, str):
            status = "unknown"
        if status == "inProgress":
            status = "in_progress"
        if status not in KNOWN_PLAN_STATUSES:
            status = "unknown"
        plan.append({"step": step, "status": status})
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


def _legacy_task_id(step: str, index: int) -> str:
    digest = hashlib.sha256(f"{index}:{step}".encode("utf-8")).hexdigest()[:12]
    return f"legacy-{digest}"


def _new_task_id(state: dict[str, Any]) -> str:
    sequence = int(state.get("task_sequence") or 0) + 1
    state["task_sequence"] = sequence
    return f"task-{sequence}"


def _task_copy(item: dict[str, Any]) -> dict[str, Any]:
    status = str(item.get("status") or "unknown")
    if status not in KNOWN_PLAN_STATUSES:
        status = "unknown"
    return {
        "id": sanitize_display_text(str(item.get("id") or "task"), 120) or "task",
        "step": sanitize_display_text(item.get("step")) or "未命名任务",
        "status": status,
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
        "completed_at": item.get("completed_at"),
        "first_revision": item.get("first_revision"),
        "last_revision": item.get("last_revision"),
        "turn_id": item.get("turn_id"),
        "transitions": [
            transition
            for transition in item.get("transitions", [])
            if isinstance(transition, dict)
        ][-TRANSITION_LIMIT:],
    }


def _current_tasks(state: dict[str, Any]) -> list[dict[str, Any]]:
    current = state.get("current_plan")
    if isinstance(current, list):
        return [_task_copy(item) for item in current if isinstance(item, dict)]
    legacy = normalize_plan(state.get("plan"))
    return [
        {
            "id": _legacy_task_id(item["step"], index),
            "step": item["step"],
            "status": item["status"],
            "created_at": None,
            "updated_at": None,
            "completed_at": None,
            "first_revision": state.get("plan_revision"),
            "last_revision": state.get("plan_revision"),
            "turn_id": state.get("plan_turn_id"),
            "transitions": [],
        }
        for index, item in enumerate(legacy)
    ]


def reconcile_plan(
    state: dict[str, Any],
    plan: Any,
    *,
    revision: int,
    turn_id: Any,
    updated_at: float,
) -> list[dict[str, Any]]:
    """Merge a complete update_plan snapshot into current and bounded history."""
    normalized = normalize_plan(plan)
    previous = _current_tasks(state)
    available: dict[str, list[dict[str, Any]]] = {}
    for item in previous:
        available.setdefault(item["step"], []).append(item)

    current: list[dict[str, Any]] = []
    for item in normalized:
        matches = available.get(item["step"], [])
        task = matches.pop(0) if matches else None
        if task is None:
            task = {
                "id": _new_task_id(state),
                "step": item["step"],
                "status": item["status"],
                "created_at": updated_at,
                "updated_at": updated_at,
                "completed_at": updated_at if item["status"] == "completed" else None,
                "first_revision": revision,
                "last_revision": revision,
                "turn_id": turn_id,
                "transitions": [
                    {"from": None, "to": item["status"], "at": updated_at, "revision": revision}
                ],
            }
        else:
            old_status = task["status"]
            if old_status != item["status"]:
                task.setdefault("transitions", []).append(
                    {
                        "from": old_status,
                        "to": item["status"],
                        "at": updated_at,
                        "revision": revision,
                    }
                )
                task["transitions"] = task["transitions"][-TRANSITION_LIMIT:]
            task["step"] = item["step"]
            task["status"] = item["status"]
            task["updated_at"] = updated_at
            task["last_revision"] = revision
            task["turn_id"] = turn_id
            if item["status"] == "completed" and old_status != "completed":
                task["completed_at"] = updated_at
        current.append(task)

    state["current_plan"] = current
    state["plan"] = [{"step": item["step"], "status": item["status"]} for item in current]
    state["plan_revision"] = revision
    state["plan_turn_id"] = turn_id
    state["plan_updated_at"] = updated_at

    history = [
        _task_copy(item)
        for item in state.get("plan_history", [])
        if isinstance(item, dict)
    ]
    by_id = {item["id"]: item for item in history}
    for item in previous:
        if item["status"] == "completed":
            by_id[item["id"]] = _task_copy(item)
        elif item["id"] in by_id:
            reopened = _task_copy(item)
            by_id[item["id"]].update(
                {
                    "step": reopened["step"],
                    "status": reopened["status"],
                    "updated_at": reopened["updated_at"],
                    "last_revision": reopened["last_revision"],
                    "turn_id": reopened["turn_id"],
                    "transitions": reopened["transitions"],
                }
            )
    for item in current:
        if item["status"] != "completed":
            continue
        copy = _task_copy(item)
        copy["status"] = "completed"
        by_id[copy["id"]] = copy
    history = sorted(
        by_id.values(),
        key=lambda item: _numeric_time(item.get("completed_at") or item.get("updated_at")),
        reverse=True,
    )[:PLAN_HISTORY_LIMIT]
    state["plan_history"] = history
    revisions = state.get("plan_revisions")
    if not isinstance(revisions, list):
        revisions = []
    revisions.append(
        {
            "revision": revision,
            "turn_id": turn_id,
            "updated_at": updated_at,
            "items": [
                {"id": item["id"], "step": item["step"], "status": item["status"]}
                for item in current
            ],
        }
    )
    state["plan_revisions"] = revisions[-PLAN_REVISION_LIMIT:]
    state["plan_state"] = plan_state_for(state["plan"])
    return current


def recent_completed_items(state: dict[str, Any], limit: int = 2) -> list[dict[str, Any]]:
    """Return completed tasks whose latest known state is not reopened."""
    current = {item["id"]: item for item in _current_tasks(state)}
    result: list[dict[str, Any]] = []
    for item in state.get("plan_history", []):
        if not isinstance(item, dict) or item.get("status") != "completed":
            continue
        latest = current.get(str(item.get("id") or ""))
        if latest is not None and latest.get("status") != "completed":
            continue
        result.append(_task_copy(item))
    return result[: max(0, int(limit))]


def plan_detail_items(state: dict[str, Any], limit: int = 3) -> list[dict[str, Any]]:
    """Prioritize unfinished tasks, then fill spare rows with recent completions."""
    unfinished = unfinished_plan_items(state.get("plan"))
    recent = recent_completed_items(state, limit=limit)
    selected = unfinished[:2]
    remaining = max(0, int(limit) - len(selected))
    selected.extend(recent[:remaining])
    return selected


def plan_state_for(plan: Any, *, stale: bool = False) -> str:
    normalized = normalize_plan(plan)
    if not normalized:
        return "empty"
    if all(item["status"] == "completed" for item in normalized):
        return "completed"
    return "stale" if stale else "active"


def desired_hud_lines(state: dict[str, Any]) -> int:
    """Return the surface height needed for the TODO detail rows."""
    detail_rows = len(plan_detail_items(state, limit=HUD_MAX_LINES - HUD_MIN_LINES))
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
        "pending_agent_spawns": [],
        "plan": [],
        "current_plan": [],
        "plan_history": [],
        "plan_revisions": [],
        "plan_state": "empty",
        "plan_turn_id": None,
        "plan_updated_at": None,
        "plan_revision": 0,
        "task_sequence": 0,
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
    legacy_only = "current_plan" not in value
    defaults = default_state(run_id)
    for key, default in defaults.items():
        value.setdefault(key, default)
    had_current_plan = not legacy_only and isinstance(value.get("current_plan"), list)
    legacy_plan = normalize_plan(value.get("plan"))
    value["version"] = STATE_VERSION
    if had_current_plan:
        value["current_plan"] = _current_tasks(value)
        current_simple = [
            {"step": item["step"], "status": item["status"]}
            for item in value["current_plan"]
        ]
        if current_simple != legacy_plan:
            value.pop("current_plan", None)
            value["plan"] = legacy_plan
            value["current_plan"] = _current_tasks(value)
    else:
        value.pop("current_plan", None)
        value["plan"] = legacy_plan
        value["current_plan"] = _current_tasks(value)
    value["plan"] = [
        {"step": item["step"], "status": item["status"]}
        for item in value["current_plan"]
    ]
    value["plan_history"] = sorted(
        [
            _task_copy(item)
            for item in value.get("plan_history", [])
            if isinstance(item, dict)
        ],
        key=lambda item: _numeric_time(item.get("completed_at") or item.get("updated_at")),
        reverse=True,
    )[:PLAN_HISTORY_LIMIT]
    revisions = value.get("plan_revisions")
    value["plan_revisions"] = revisions[-PLAN_REVISION_LIMIT:] if isinstance(revisions, list) else []
    spawns = value.get("pending_agent_spawns")
    value["pending_agent_spawns"] = spawns[-10:] if isinstance(spawns, list) else []
    value["agents"] = normalize_agents(value.get("agents"))
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
