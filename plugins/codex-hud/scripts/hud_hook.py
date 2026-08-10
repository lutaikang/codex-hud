#!/usr/bin/env python3
"""Consume one Codex lifecycle hook event without affecting agent behavior."""

from __future__ import annotations

import json
import hashlib
import os
import sys
import time
from typing import Any

from hud_state import (
    enabled,
    normalize_plan,
    plan_state_for,
    reconcile_plan,
    sanitize_display_text,
    update_state,
)


JSON_OUTPUT_EVENTS = {"Stop", "SubagentStop"}


def _account_scope() -> str:
    """Fingerprint the active CODEX_HOME without storing its absolute path."""
    codex_home = os.environ.get("CODEX_HOME") or "~/.codex"
    normalized = str(os.path.realpath(os.path.expanduser(codex_home)))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _tool_key(event: dict[str, Any]) -> str:
    return str(
        event.get("tool_use_id")
        or f"{event.get('turn_id', 'turn')}:{event.get('tool_name', 'tool')}"
    )


def _normalize_plan(value: Any) -> list[dict[str, str]]:
    """Compatibility wrapper for callers that imported the old helper."""
    return normalize_plan(value)


def _record_plan(
    state: dict[str, Any],
    tool_input: Any,
    event: dict[str, Any],
    now: float,
) -> None:
    if not isinstance(tool_input, dict) or "plan" not in tool_input:
        return
    plan = _normalize_plan(tool_input.get("plan"))
    if state.get("plan") == plan:
        state["plan_state"] = plan_state_for(plan)
        state["plan_turn_id"] = event.get("turn_id") or state.get("current_turn_id")
        state["plan_updated_at"] = now
        return
    revision = int(state.get("plan_revision") or 0) + 1
    reconcile_plan(
        state,
        plan,
        revision=revision,
        turn_id=event.get("turn_id") or state.get("current_turn_id"),
        updated_at=now,
    )


def _is_spawn_tool(tool_name: Any) -> bool:
    normalized = str(tool_name or "").lower().replace("-", "_")
    return normalized in {"spawn_agent", "collaboration.spawn_agent"} or normalized.endswith(
        ".spawn_agent"
    )


def _spawn_record(event: dict[str, Any], now: float) -> dict[str, Any] | None:
    tool_input = event.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    task_name = sanitize_display_text(tool_input.get("task_name"), 80)
    description = sanitize_display_text(tool_input.get("description"), 120)
    if not description and task_name:
        description = sanitize_display_text(task_name.replace("_", " ").replace("-", " "), 120)
    model = sanitize_display_text(tool_input.get("model"), 80)
    if not task_name and not description and not model:
        return None
    return {
        "tool_use_id": str(event.get("tool_use_id") or ""),
        "task_name": task_name or None,
        "description": description or None,
        "model": model or None,
        "started_at": now,
        "updated_at": now,
        "current_activity": "starting",
    }


def _claim_spawn(
    state: dict[str, Any], event: dict[str, Any], now: float
) -> dict[str, Any] | None:
    pending = state.setdefault("pending_agent_spawns", [])
    if not isinstance(pending, list):
        state["pending_agent_spawns"] = pending = []
    pending[:] = [
        item
        for item in pending
        if isinstance(item, dict)
        and now - float(item.get("started_at") or now) <= 60
    ]
    task_name = sanitize_display_text(event.get("task_name"), 80)
    for index, item in enumerate(pending):
        if not isinstance(item, dict):
            continue
        if task_name and item.get("task_name") == task_name:
            return pending.pop(index)
    for index, item in enumerate(pending):
        if isinstance(item, dict):
            return pending.pop(index)
    return None


def _tool_failed(event: dict[str, Any]) -> bool:
    if event.get("is_error") is True or event.get("error"):
        return True
    for key in ("tool_output", "tool_response", "tool_result", "output"):
        value = event.get(key)
        if isinstance(value, str):
            normalized = value.lower()
            if "spawn failed" in normalized or normalized.startswith("error"):
                return True
        elif isinstance(value, dict) and (value.get("is_error") or value.get("error")):
            return True
    return False


def handle_event(event: dict[str, Any], run_id: str) -> None:
    event_name = str(event.get("hook_event_name") or "")
    now = time.time()
    event_scope = _account_scope()

    def mutate(state: dict[str, Any]) -> None:
        previous_session_id = state.get("session_id")
        event_session_id = event.get("session_id")
        bound_scope = state.get("account_scope")
        source = event.get("source")
        is_thread_transition = (
            event_name == "SessionStart"
            and source in {"clear", "resume"}
            and previous_session_id
            and event_session_id
            and event_session_id != previous_session_id
        )

        # The wrapper run id is necessary but not sufficient: child Codex
        # processes inherit environment variables. Bind the first SessionStart
        # to one account scope and one thread, then ignore nested sessions.
        if bound_scope and bound_scope != event_scope:
            return
        if (
            previous_session_id
            and event_session_id
            and event_session_id != previous_session_id
            and not is_thread_transition
        ):
            return
        if event_name == "SessionStart":
            state["account_scope"] = event_scope
            if is_thread_transition:
                state["active_tools"] = {}
                state["tool_stats"] = {}
                state["agents"] = {}
                state["pending_agent_spawns"] = []
                state["plan"] = []
                state["current_plan"] = []
                state["plan_history"] = []
                state["plan_revisions"] = []
                state["plan_state"] = "empty"
                state["plan_turn_id"] = None
                state["plan_updated_at"] = None
                state["plan_revision"] = 0
                state["task_sequence"] = 0
                state["current_turn_id"] = None
                state["compactions"] = 0
                state["started_at"] = now
        elif not previous_session_id:
            # Lifecycle events before SessionStart are not trusted to establish
            # the owner of a run.
            return

        for key in ("session_id", "cwd", "model", "permission_mode", "transcript_path"):
            value = event.get(key)
            if value is not None:
                state[key] = value
        if event_session_id:
            state["thread_id"] = event_session_id
        if event.get("turn_id"):
            state["current_turn_id"] = event["turn_id"]

        if event_name == "SessionStart":
            if source in {"startup", "resume", "clear"} and not previous_session_id:
                state["started_at"] = now
            if source != "compact":
                state["status"] = "ready"
        elif event_name == "UserPromptSubmit":
            incoming_turn_id = event.get("turn_id")
            plan_turn_id = state.get("plan_turn_id")
            same_plan_turn = bool(
                incoming_turn_id and plan_turn_id and incoming_turn_id == plan_turn_id
            )
            if (
                state.get("plan_state") == "active"
                and state.get("plan")
                and not same_plan_turn
            ):
                state["plan_state"] = "stale"
            state["status"] = "working"
        elif event_name == "PreToolUse":
            tool_name = str(event.get("tool_name") or "tool")
            key = _tool_key(event)
            state.setdefault("active_tools", {})[key] = {
                "name": tool_name,
                "started_at": now,
            }
            state["status"] = "working"
            if tool_name == "update_plan":
                _record_plan(state, event.get("tool_input"), event, now)
            elif _is_spawn_tool(tool_name):
                spawn = _spawn_record(event, now)
                if spawn:
                    pending = state.setdefault("pending_agent_spawns", [])
                    pending.append(spawn)
                    state["pending_agent_spawns"] = pending[-10:]
        elif event_name == "PostToolUse":
            tool_name = str(event.get("tool_name") or "tool")
            active = state.setdefault("active_tools", {}).pop(_tool_key(event), None)
            stats = state.setdefault("tool_stats", {}).setdefault(
                tool_name, {"count": 0, "last_duration_ms": None}
            )
            stats["count"] = int(stats.get("count") or 0) + 1
            if isinstance(active, dict) and isinstance(active.get("started_at"), (int, float)):
                stats["last_duration_ms"] = max(
                    0, int((now - float(active["started_at"])) * 1000)
                )
            if tool_name == "update_plan":
                _record_plan(state, event.get("tool_input"), event, now)
            elif _is_spawn_tool(tool_name):
                pending = state.setdefault("pending_agent_spawns", [])
                if _tool_failed(event):
                    state["pending_agent_spawns"] = [
                        item
                        for item in pending
                        if not isinstance(item, dict)
                        or item.get("tool_use_id") != _tool_key(event)
                    ]
                for item in state.setdefault("pending_agent_spawns", []):
                    if isinstance(item, dict) and item.get("tool_use_id") == _tool_key(event):
                        item["current_activity"] = "spawned"
                        item["updated_at"] = now
        elif event_name == "SubagentStart":
            agent_id = str(event.get("agent_id") or "agent")
            existing = state.setdefault("agents", {}).get(agent_id)
            if isinstance(existing, dict) and existing.get("status") == "completed":
                existing["updated_at"] = now
                return
            spawn = _claim_spawn(state, event, now)
            event_task_name = sanitize_display_text(event.get("task_name"), 80)
            task_name = event_task_name or (spawn or {}).get("task_name")
            description = sanitize_display_text(event.get("description"), 120)
            if not description:
                description = (spawn or {}).get("description")
            model = sanitize_display_text(event.get("model"), 80) or (spawn or {}).get("model")
            source_confidence = "hook"
            if spawn:
                source_confidence = "hook+spawn"
            agents = state.setdefault("agents", {})
            agents[agent_id] = {
                "type": sanitize_display_text(event.get("agent_type"), 80) or "agent",
                "status": "running",
                "started_at": now,
                "ended_at": None,
                "task_name": task_name,
                "description": description,
                "model": model or None,
                "current_activity": "working",
                "last_tool": None,
                "updated_at": now,
                "source_confidence": source_confidence,
            }
            if len(agents) > 20:
                ordered = sorted(
                    agents.items(),
                    key=lambda pair: float(pair[1].get("updated_at") or pair[1].get("started_at") or 0)
                    if isinstance(pair[1], dict)
                    else 0,
                )
                state["agents"] = dict(ordered[-20:])
        elif event_name == "SubagentStop":
            agent_id = str(event.get("agent_id") or "agent")
            agent = state.setdefault("agents", {}).setdefault(
                agent_id,
                {
                    "type": sanitize_display_text(event.get("agent_type"), 80) or "agent",
                    "started_at": now,
                },
            )
            agent["status"] = "completed"
            agent["ended_at"] = now
            agent["current_activity"] = "completed"
            agent["updated_at"] = now
        elif event_name == "PostCompact":
            state["compactions"] = int(state.get("compactions") or 0) + 1
        elif event_name == "Stop":
            state["status"] = "ready"
            state["active_tools"] = {}
            state["plan_state"] = plan_state_for(
                state.get("plan"), stale=bool(state.get("plan"))
            )
        elif event_name == "SessionEnd":
            state["status"] = "ended"
            state["active_tools"] = {}
            state["plan_state"] = plan_state_for(
                state.get("plan"), stale=bool(state.get("plan"))
            )

    update_state(run_id, mutate)


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return 0
    if not isinstance(event, dict):
        return 0

    event_name = str(event.get("hook_event_name") or "")
    # Only the opt-in wrapper may select a HUD run. Never collect ordinary Codex
    # sessions merely because the user's global HUD preference is enabled.
    run_id = os.environ.get("CODEX_HUD_RUN_ID") or ""
    if enabled() and run_id:
        try:
            handle_event(event, run_id)
        except Exception:
            # Observability must never block or alter Codex execution.
            pass

    if event_name in JSON_OUTPUT_EVENTS:
        print("{}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
