#!/usr/bin/env python3
"""Consume one Codex lifecycle hook event without affecting agent behavior."""

from __future__ import annotations

import json
import hashlib
import os
import sys
import time
from typing import Any

from hud_state import enabled, update_state


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
    if not isinstance(value, list):
        return []
    plan: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        step = item.get("step")
        status = item.get("status")
        if isinstance(step, str) and isinstance(status, str):
            plan.append({"step": step[:240], "status": status})
    return plan[:100]


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
                state["plan"] = []
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

        if event_name == "SessionStart":
            if source in {"startup", "resume", "clear"} and not previous_session_id:
                state["started_at"] = now
            if source != "compact":
                state["status"] = "ready"
        elif event_name == "UserPromptSubmit":
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
                tool_input = event.get("tool_input")
                if isinstance(tool_input, dict):
                    plan = _normalize_plan(tool_input.get("plan"))
                    if plan:
                        state["plan"] = plan
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
                tool_input = event.get("tool_input")
                if isinstance(tool_input, dict):
                    plan = _normalize_plan(tool_input.get("plan"))
                    if plan:
                        state["plan"] = plan
        elif event_name == "SubagentStart":
            agent_id = str(event.get("agent_id") or "agent")
            state.setdefault("agents", {})[agent_id] = {
                "type": str(event.get("agent_type") or "agent"),
                "status": "running",
                "started_at": now,
                "ended_at": None,
            }
        elif event_name == "SubagentStop":
            agent_id = str(event.get("agent_id") or "agent")
            agent = state.setdefault("agents", {}).setdefault(
                agent_id,
                {
                    "type": str(event.get("agent_type") or "agent"),
                    "started_at": now,
                },
            )
            agent["status"] = "completed"
            agent["ended_at"] = now
        elif event_name == "PostCompact":
            state["compactions"] = int(state.get("compactions") or 0) + 1
        elif event_name == "Stop":
            state["status"] = "ready"
            state["active_tools"] = {}
        elif event_name == "SessionEnd":
            state["status"] = "ended"
            state["active_tools"] = {}

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
