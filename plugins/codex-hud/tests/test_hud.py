from __future__ import annotations

import json
import io
import importlib.machinery
import importlib.util
import os
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import nullcontext, redirect_stdout
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import hud_hook
import hud_render
import hud_state
import metrics
import tmux_mouse
import token_audit


CLI_LOADER = importlib.machinery.SourceFileLoader("codex_hud_cli", str(SCRIPTS / "codex-hud"))
CLI_SPEC = importlib.util.spec_from_loader(CLI_LOADER.name, CLI_LOADER)
assert CLI_SPEC is not None
codex_hud_cli = importlib.util.module_from_spec(CLI_SPEC)
CLI_LOADER.exec_module(codex_hud_cli)


class HudTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        # Existing launcher fixtures model the pre-daemon CLI. Modern CLI
        # capability and argument routing are covered in test_launcher_daemon.
        capability = mock.patch.object(
            codex_hud_cli, "_codex_supports_no_daemon", return_value=False
        )
        capability.start()
        self.addCleanup(capability.stop)
        os.environ["CODEX_HUD_HOME"] = self.temporary.name
        os.environ["NO_COLOR"] = "1"
        hud_state.set_enabled(True)
        self.run_id = "test-run"

    def tearDown(self) -> None:
        os.environ.pop("CODEX_HUD_HOME", None)
        os.environ.pop("CODEX_HOME", None)
        os.environ.pop("NO_COLOR", None)
        self.temporary.cleanup()

    def test_hook_tracks_tools_agents_and_plan(self) -> None:
        hud_hook.handle_event(
            {
                "hook_event_name": "SessionStart",
                "session_id": "thread-1",
                "cwd": "/tmp/project",
                "model": "gpt-5.6-sol",
                "transcript_path": "/tmp/transcript.jsonl",
                "source": "startup",
            },
            self.run_id,
        )
        hud_hook.handle_event(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "update_plan",
                "tool_use_id": "tool-1",
                "tool_input": {
                    "plan": [
                        {"step": "Build", "status": "in_progress"},
                        {"step": "Test", "status": "pending"},
                    ]
                },
            },
            self.run_id,
        )
        hud_hook.handle_event(
            {
                "hook_event_name": "PostToolUse",
                "tool_name": "update_plan",
                "tool_use_id": "tool-1",
                "tool_input": {
                    "plan": [
                        {"step": "Build", "status": "completed"},
                        {"step": "Test", "status": "in_progress"},
                    ]
                },
            },
            self.run_id,
        )
        hud_hook.handle_event(
            {
                "hook_event_name": "SubagentStart",
                "agent_id": "agent-1",
                "agent_type": "reviewer",
            },
            self.run_id,
        )
        state = hud_state.load_state(self.run_id)
        self.assertEqual(state["session_id"], "thread-1")
        self.assertEqual(state["tool_stats"]["update_plan"]["count"], 1)
        self.assertEqual(state["plan"][1]["status"], "in_progress")
        self.assertEqual(state["agents"]["agent-1"]["status"], "running")

    def test_empty_update_plan_clears_stale_plan(self) -> None:
        hud_hook.handle_event(
            {
                "hook_event_name": "SessionStart",
                "session_id": "thread-1",
                "source": "startup",
            },
            self.run_id,
        )
        hud_hook.handle_event(
            {
                "hook_event_name": "PostToolUse",
                "session_id": "thread-1",
                "turn_id": "turn-1",
                "tool_name": "update_plan",
                "tool_use_id": "tool-1",
                "tool_input": {
                    "plan": [{"step": "Old task", "status": "in_progress"}]
                },
            },
            self.run_id,
        )
        hud_hook.handle_event(
            {
                "hook_event_name": "PostToolUse",
                "session_id": "thread-1",
                "turn_id": "turn-2",
                "tool_name": "update_plan",
                "tool_use_id": "tool-2",
                "tool_input": {"plan": []},
            },
            self.run_id,
        )

        state = hud_state.load_state(self.run_id)
        self.assertEqual(state["plan"], [])
        self.assertEqual(state["plan_state"], "empty")

    def test_completed_plan_history_survives_replacement_and_empty_plan(self) -> None:
        hud_hook.handle_event(
            {
                "hook_event_name": "SessionStart",
                "session_id": "thread-1",
                "source": "startup",
            },
            self.run_id,
        )
        for turn_id, plan in (
            ("turn-1", [{"step": "Finished work", "status": "in_progress"}]),
            ("turn-1", [{"step": "Finished work", "status": "completed"}]),
            ("turn-2", [{"step": "Replacement", "status": "in_progress"}]),
            ("turn-3", []),
        ):
            hud_hook.handle_event(
                {
                    "hook_event_name": "PostToolUse",
                    "session_id": "thread-1",
                    "turn_id": turn_id,
                    "tool_name": "update_plan",
                    "tool_input": {"plan": plan},
                },
                self.run_id,
            )

        state = hud_state.load_state(self.run_id)
        self.assertEqual(state["plan"], [])
        self.assertEqual(state["current_plan"], [])
        self.assertEqual(state["plan_history"][0]["step"], "Finished work")
        self.assertEqual(state["plan_history"][0]["status"], "completed")
        self.assertEqual(
            hud_state.recent_completed_items(state)[0]["step"], "Finished work"
        )

    def test_duplicate_plan_items_keep_distinct_stable_ids(self) -> None:
        state = hud_state.default_state(self.run_id)
        hud_state.reconcile_plan(
            state,
            [
                {"step": "Duplicate", "status": "in_progress"},
                {"step": "Duplicate", "status": "pending"},
            ],
            revision=1,
            turn_id="turn-1",
            updated_at=10,
        )
        original_ids = [item["id"] for item in state["current_plan"]]

        hud_state.reconcile_plan(
            state,
            [
                {"step": "Duplicate", "status": "completed"},
                {"step": "Duplicate", "status": "in_progress"},
            ],
            revision=2,
            turn_id="turn-2",
            updated_at=20,
        )

        self.assertEqual(
            [item["id"] for item in state["current_plan"]], original_ids
        )
        self.assertNotEqual(original_ids[0], original_ids[1])
        self.assertEqual(state["current_plan"][0]["completed_at"], 20)

    def test_reopened_task_is_not_reported_as_recently_completed(self) -> None:
        state = hud_state.default_state(self.run_id)
        hud_state.reconcile_plan(
            state,
            [{"step": "Reopen me", "status": "completed"}],
            revision=1,
            turn_id="turn-1",
            updated_at=10,
        )
        hud_state.reconcile_plan(
            state,
            [{"step": "Reopen me", "status": "in_progress"}],
            revision=2,
            turn_id="turn-2",
            updated_at=20,
        )

        self.assertEqual(hud_state.recent_completed_items(state), [])
        self.assertEqual(
            [event["to"] for event in state["current_plan"][0]["transitions"]],
            ["completed", "in_progress"],
        )

        hud_state.reconcile_plan(
            state,
            [],
            revision=3,
            turn_id="turn-3",
            updated_at=30,
        )
        self.assertEqual(hud_state.recent_completed_items(state), [])
        self.assertEqual(state["plan_history"][0]["status"], "in_progress")

    def test_plan_history_is_bounded_and_duplicate_hook_frames_are_idempotent(self) -> None:
        hud_hook.handle_event(
            {
                "hook_event_name": "SessionStart",
                "session_id": "thread-1",
                "source": "startup",
            },
            self.run_id,
        )
        for index in range(hud_state.PLAN_HISTORY_LIMIT + 5):
            event = {
                "hook_event_name": "PostToolUse",
                "session_id": "thread-1",
                "turn_id": f"turn-{index}",
                "tool_name": "update_plan",
                "tool_use_id": f"tool-{index}",
                "tool_input": {
                    "plan": [{"step": f"Done {index}", "status": "completed"}]
                },
            }
            hud_hook.handle_event(event, self.run_id)
            hud_hook.handle_event(event, self.run_id)

        state = hud_state.load_state(self.run_id)
        self.assertEqual(len(state["plan_history"]), hud_state.PLAN_HISTORY_LIMIT)
        self.assertEqual(state["plan_revision"], hud_state.PLAN_HISTORY_LIMIT + 5)
        self.assertEqual(len(state["plan_revisions"]), hud_state.PLAN_REVISION_LIMIT)
        self.assertEqual(state["plan_history"][0]["step"], "Done 24")

    def test_clear_to_new_thread_resets_plan_history(self) -> None:
        hud_hook.handle_event(
            {
                "hook_event_name": "SessionStart",
                "session_id": "thread-1",
                "source": "startup",
            },
            self.run_id,
        )
        hud_hook.handle_event(
            {
                "hook_event_name": "PostToolUse",
                "session_id": "thread-1",
                "tool_name": "update_plan",
                "tool_input": {
                    "plan": [{"step": "Old completion", "status": "completed"}]
                },
            },
            self.run_id,
        )
        hud_hook.handle_event(
            {
                "hook_event_name": "SessionStart",
                "session_id": "thread-2",
                "source": "clear",
            },
            self.run_id,
        )

        state = hud_state.load_state(self.run_id)
        self.assertEqual(state["current_plan"], [])
        self.assertEqual(state["plan_history"], [])
        self.assertEqual(state["plan_revisions"], [])

    def test_stop_marks_incomplete_plan_stale_without_completing(self) -> None:
        hud_hook.handle_event(
            {
                "hook_event_name": "SessionStart",
                "session_id": "thread-1",
                "source": "startup",
            },
            self.run_id,
        )
        hud_hook.handle_event(
            {
                "hook_event_name": "PostToolUse",
                "session_id": "thread-1",
                "turn_id": "turn-1",
                "tool_name": "update_plan",
                "tool_use_id": "tool-1",
                "tool_input": {
                    "plan": [
                        {"step": "Done", "status": "completed"},
                        {"step": "Pending", "status": "pending"},
                    ]
                },
            },
            self.run_id,
        )
        hud_hook.handle_event(
            {
                "hook_event_name": "Stop",
                "session_id": "thread-1",
                "turn_id": "turn-1",
            },
            self.run_id,
        )

        state = hud_state.load_state(self.run_id)
        self.assertEqual(state["plan"][1]["status"], "pending")
        self.assertEqual(state["plan_state"], "stale")

    def test_same_turn_user_prompt_does_not_stale_active_plan(self) -> None:
        hud_hook.handle_event(
            {
                "hook_event_name": "SessionStart",
                "session_id": "thread-1",
                "turn_id": "turn-1",
                "source": "startup",
            },
            self.run_id,
        )
        hud_hook.handle_event(
            {
                "hook_event_name": "PostToolUse",
                "session_id": "thread-1",
                "turn_id": "turn-1",
                "tool_name": "update_plan",
                "tool_input": {
                    "plan": [{"step": "Continue", "status": "in_progress"}]
                },
            },
            self.run_id,
        )
        hud_hook.handle_event(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "thread-1",
                "turn_id": "turn-1",
            },
            self.run_id,
        )

        self.assertEqual(hud_state.load_state(self.run_id)["plan_state"], "active")

    def test_session_end_marks_incomplete_plan_stale(self) -> None:
        hud_hook.handle_event(
            {
                "hook_event_name": "SessionStart",
                "session_id": "thread-1",
                "source": "startup",
            },
            self.run_id,
        )
        hud_hook.handle_event(
            {
                "hook_event_name": "PostToolUse",
                "session_id": "thread-1",
                "tool_name": "update_plan",
                "tool_input": {
                    "plan": [{"step": "Interrupted", "status": "pending"}]
                },
            },
            self.run_id,
        )
        hud_hook.handle_event(
            {"hook_event_name": "SessionEnd", "session_id": "thread-1"},
            self.run_id,
        )

        state = hud_state.load_state(self.run_id)
        self.assertEqual(state["status"], "ended")
        self.assertEqual(state["plan_state"], "stale")

    def test_new_plan_update_reactivates_stale_plan(self) -> None:
        hud_hook.handle_event(
            {
                "hook_event_name": "SessionStart",
                "session_id": "thread-1",
                "source": "startup",
            },
            self.run_id,
        )
        hud_hook.handle_event(
            {
                "hook_event_name": "PostToolUse",
                "session_id": "thread-1",
                "turn_id": "turn-1",
                "tool_name": "update_plan",
                "tool_use_id": "tool-1",
                "tool_input": {
                    "plan": [{"step": "Pending", "status": "pending"}]
                },
            },
            self.run_id,
        )
        hud_hook.handle_event(
            {
                "hook_event_name": "Stop",
                "session_id": "thread-1",
                "turn_id": "turn-1",
            },
            self.run_id,
        )
        hud_hook.handle_event(
            {
                "hook_event_name": "PostToolUse",
                "session_id": "thread-1",
                "turn_id": "turn-2",
                "tool_name": "update_plan",
                "tool_use_id": "tool-2",
                "tool_input": {
                    "plan": [{"step": "Now", "status": "in_progress"}]
                },
            },
            self.run_id,
        )

        state = hud_state.load_state(self.run_id)
        self.assertEqual(state["plan_state"], "active")
        self.assertEqual(state["plan_turn_id"], "turn-2")

    def test_spawn_hook_enriches_subagent_without_storing_prompt_body(self) -> None:
        hud_hook.handle_event(
            {
                "hook_event_name": "SessionStart",
                "session_id": "thread-1",
                "source": "startup",
            },
            self.run_id,
        )
        hud_hook.handle_event(
            {
                "hook_event_name": "PreToolUse",
                "session_id": "thread-1",
                "tool_name": "spawn_agent",
                "tool_use_id": "spawn-1",
                "tool_input": {
                    "task_name": "todo_history_review",
                    "message": "secret prompt body that must not be persisted",
                    "model": "gpt-5.6-sol\x1b]8;;bad\x07",
                },
            },
            self.run_id,
        )
        hud_hook.handle_event(
            {
                "hook_event_name": "SubagentStart",
                "session_id": "thread-1",
                "agent_id": "agent-1",
                "agent_type": "reviewer",
            },
            self.run_id,
        )

        state = hud_state.load_state(self.run_id)
        agent = state["agents"]["agent-1"]
        self.assertEqual(agent["task_name"], "todo_history_review")
        self.assertEqual(agent["description"], "todo history review")
        self.assertEqual(agent["model"], "gpt-5.6-sol")
        self.assertEqual(agent["current_activity"], "working")
        self.assertEqual(agent["source_confidence"], "hook+spawn")
        self.assertNotIn("secret prompt", json.dumps(state))

    def test_late_duplicate_subagent_start_does_not_reopen_completed_agent(self) -> None:
        hud_hook.handle_event(
            {
                "hook_event_name": "SessionStart",
                "session_id": "thread-1",
                "source": "startup",
            },
            self.run_id,
        )
        start = {
            "hook_event_name": "SubagentStart",
            "session_id": "thread-1",
            "agent_id": "agent-1",
            "agent_type": "reviewer",
        }
        hud_hook.handle_event(start, self.run_id)
        hud_hook.handle_event(
            {
                "hook_event_name": "SubagentStop",
                "session_id": "thread-1",
                "agent_id": "agent-1",
            },
            self.run_id,
        )
        hud_hook.handle_event(start, self.run_id)

        agent = hud_state.load_state(self.run_id)["agents"]["agent-1"]
        self.assertEqual(agent["status"], "completed")
        self.assertIsNotNone(agent["ended_at"])

    def test_failed_spawn_is_not_claimed_by_next_subagent(self) -> None:
        hud_hook.handle_event(
            {
                "hook_event_name": "SessionStart",
                "session_id": "thread-1",
                "source": "startup",
            },
            self.run_id,
        )
        hud_hook.handle_event(
            {
                "hook_event_name": "PreToolUse",
                "session_id": "thread-1",
                "tool_name": "collaboration.spawn_agent",
                "tool_use_id": "spawn-failed",
                "tool_input": {"task_name": "failed_task", "model": "gpt-5.6-sol"},
            },
            self.run_id,
        )
        hud_hook.handle_event(
            {
                "hook_event_name": "PostToolUse",
                "session_id": "thread-1",
                "tool_name": "collaboration.spawn_agent",
                "tool_use_id": "spawn-failed",
                "tool_output": "collab spawn failed: thread limit reached",
            },
            self.run_id,
        )
        hud_hook.handle_event(
            {
                "hook_event_name": "SubagentStart",
                "session_id": "thread-1",
                "agent_id": "agent-1",
                "agent_type": "reviewer",
            },
            self.run_id,
        )

        agent = hud_state.load_state(self.run_id)["agents"]["agent-1"]
        self.assertIsNone(agent["task_name"])
        self.assertIsNone(agent["model"])
        self.assertEqual(agent["source_confidence"], "hook")

    def test_session_start_resets_stale_launcher_timestamp(self) -> None:
        hud_state.update_state(
            self.run_id,
            lambda state: state.update(started_at=1, session_id=None),
        )
        hud_hook.handle_event(
            {
                "hook_event_name": "SessionStart",
                "session_id": "thread-new",
                "source": "startup",
            },
            self.run_id,
        )
        self.assertGreater(hud_state.load_state(self.run_id)["started_at"], 1)

    def test_transcript_tokens_cache_context_and_cost(self) -> None:
        transcript = Path(self.temporary.name) / "rollout.jsonl"
        transcript.write_text(
            json.dumps(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "last_token_usage": {"total_tokens": 210000},
                            "total_token_usage": {
                                "input_tokens": 1000000,
                                "cached_input_tokens": 800000,
                                "output_tokens": 100000,
                            },
                            "model_context_window": 1050000,
                        },
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        scanner = hud_render.TranscriptScanner()
        scanner.scan(str(transcript))
        self.assertEqual(scanner.context_window, 1050000)
        self.assertEqual(scanner.total["cached_input_tokens"], 800000)
        self.assertAlmostEqual(
            hud_render.estimated_cost("gpt-5.6-sol", scanner.total), 4.4
        )

    def test_token_audit_excludes_replayed_parent_history_from_subagent(self) -> None:
        codex_home = Path(self.temporary.name) / "codex-home"
        session_dir = codex_home / "sessions" / "2026" / "08" / "10"
        session_dir.mkdir(parents=True)
        root = session_dir / "root.jsonl"
        root_records = [
            {
                "timestamp": "2026-08-10T01:00:00Z",
                "type": "session_meta",
                "payload": {
                    "id": "root",
                    "timestamp": "2026-08-10T01:00:00Z",
                    "thread_source": "user",
                    "model_provider": "openai",
                    "source": "cli",
                    "cwd": "/tmp/project",
                },
            },
            {
                "timestamp": "2026-08-10T01:00:00.500Z",
                "type": "turn_context",
                "payload": {"model": "gpt-5.6-sol", "effort": "high"},
            },
            {
                "timestamp": "2026-08-10T01:00:01Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "total_tokens": 100,
                            "input_tokens": 90,
                            "cached_input_tokens": 40,
                            "output_tokens": 10,
                        }
                    },
                },
            },
            {
                "timestamp": "2026-08-10T01:00:02Z",
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "name": "view_image",
                    "call_id": "call-1",
                },
            },
            {
                "timestamp": "2026-08-10T01:00:03Z",
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": "call-1",
                    "output": [
                        {"type": "input_image", "image_url": "data:image/png;base64,AAAA"}
                    ],
                },
            },
            {
                "timestamp": "2026-08-10T01:00:04Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "total_tokens": 250,
                            "input_tokens": 230,
                            "cached_input_tokens": 100,
                            "output_tokens": 20,
                        }
                    },
                },
            },
        ]
        root.write_text(
            "".join(json.dumps(record) + "\n" for record in root_records),
            encoding="utf-8",
        )

        child = session_dir / "child.jsonl"
        child_records = [
            {
                "timestamp": "2026-08-10T02:00:00Z",
                "type": "session_meta",
                "payload": {
                    "id": "child",
                    "timestamp": "2026-08-10T02:00:00Z",
                    "thread_source": "subagent",
                    "model_provider": "openai",
                    "source": {
                        "subagent": {
                            "thread_spawn": {
                                "parent_thread_id": "root",
                                "depth": 1,
                                "agent_path": "/root/audit",
                            }
                        }
                    },
                    "cwd": "/tmp/project",
                },
            },
            *root_records,
            {
                "timestamp": "2026-08-10T02:00:01Z",
                "type": "inter_agent_communication_metadata",
                "payload": {"trigger_turn": True},
            },
            {
                "timestamp": "2026-08-10T02:00:02Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "total_tokens": 300,
                            "input_tokens": 275,
                            "cached_input_tokens": 120,
                            "output_tokens": 25,
                        }
                    },
                },
            },
        ]
        child.write_text(
            "".join(json.dumps(record) + "\n" for record in child_records),
            encoding="utf-8",
        )

        start = token_audit._parse_timestamp("2026-08-10T00:00:00Z")
        end = token_audit._parse_timestamp("2026-08-11T00:00:00Z")
        assert start is not None and end is not None
        report = token_audit.build_report(codex_home, start=start, end=end)

        self.assertEqual(report.usage["total_tokens"], 300)
        self.assertEqual(report.usage["input_tokens"], 275)
        self.assertEqual(report.sessions_with_usage, 2)
        self.assertEqual(report.subagent_sessions, 1)
        self.assertEqual(report.openai_quota["model_rounds"], 3)
        self.assertEqual(report.task_families[0]["child_sessions"], 1)
        self.assertEqual(report.task_families[0]["usage"]["total_tokens"], 300)
        root_audit = next(item for item in report.top_sessions if item.session_id == "root")
        child_audit = next(item for item in report.top_sessions if item.session_id == "child")
        self.assertEqual(root_audit.image_blocks, 1)
        self.assertEqual(root_audit.tool_breakdown["view_image"], 1)
        self.assertEqual(child_audit.total_tokens, 50)

    def test_transcript_waits_for_complete_jsonl_line_and_resets_on_truncate(self) -> None:
        transcript = Path(self.temporary.name) / "partial.jsonl"
        record = json.dumps(
            {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {"total_token_usage": {"input_tokens": 123}},
                },
            }
        )
        transcript.write_text(record[:-2], encoding="utf-8")
        scanner = hud_render.TranscriptScanner()
        scanner.scan(str(transcript))
        self.assertEqual(scanner.total, {})
        with transcript.open("a", encoding="utf-8") as handle:
            handle.write(record[-2:] + "\n")
        scanner.scan(str(transcript))
        self.assertEqual(scanner.total["input_tokens"], 123)
        transcript.write_text("", encoding="utf-8")
        scanner.scan(str(transcript))
        self.assertEqual(scanner.total, {})

    def test_transcript_tracks_tool_payload_and_image_amplifiers(self) -> None:
        transcript = Path(self.temporary.name) / "amplifiers.jsonl"
        records = [
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "name": "view_image",
                    "call_id": "call-1",
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": "call-1",
                    "output": [
                        {"type": "input_image", "image_url": "data:image/png;base64,AAAA"}
                    ],
                },
            },
            {
                "type": "compacted",
                "payload": {"window_number": 2},
            },
        ]
        transcript.write_text(
            "".join(json.dumps(record) + "\n" for record in records),
            encoding="utf-8",
        )
        scanner = hud_render.TranscriptScanner()
        scanner.scan(str(transcript))
        self.assertEqual(scanner.tool_calls, 1)
        self.assertEqual(scanner.tool_outputs, 1)
        self.assertEqual(scanner.image_blocks, 1)
        self.assertGreater(scanner.tool_output_bytes, 0)
        self.assertEqual(scanner.compactions, 1)

    def test_session_risk_escalates_for_context_amplifiers(self) -> None:
        self.assertEqual(
            metrics.session_risk(
                4_999_999,
                49,
                19,
                4_999_999,
                1,
                49,
                99.9,
                3600,
            ).level,
            "normal",
        )
        warning = metrics.session_risk(
            5_000_000,
            50,
            20,
            5_000_000,
            2,
            50,
            100,
            3600,
        )
        self.assertEqual(warning.level, "warning")
        critical = metrics.session_risk(
            20_000_000,
            100,
            100,
            50_000_000,
            3,
            100,
            250,
            600,
        )
        self.assertEqual(critical.level, "critical")
        self.assertIn("input>=20m", critical.reasons)
        self.assertIn("credits>=250", critical.reasons)

    def test_chatgpt_credit_estimate_matches_official_rate_card(self) -> None:
        estimate = metrics.estimate_chatgpt_credits(
            "gpt-5.6-sol",
            {
                "input_tokens": 23_264_350,
                "cached_input_tokens": 22_866_688,
                "output_tokens": 68_670,
            },
        )
        self.assertIsNotNone(estimate)
        assert estimate is not None
        self.assertAlmostEqual(estimate.uncached_input, 49.70775, places=5)
        self.assertAlmostEqual(estimate.cached_input, 285.8336, places=4)
        self.assertAlmostEqual(estimate.output, 51.5025, places=4)
        self.assertAlmostEqual(estimate.total, 387.04385, places=5)
        self.assertIsNone(
            metrics.estimate_chatgpt_credits("unknown-model", {"input_tokens": 1})
        )

    def test_update_refuses_active_hud_managed_task_before_mutation(self) -> None:
        update_script = SCRIPTS.parents[2] / "scripts" / "update.sh"
        result = subprocess.run(
            ["bash", str(update_script)],
            env={**os.environ, "CODEX_HUD_RUN_ID": "active-test-run"},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("Refusing to update", result.stderr)

    def test_transcript_extracts_agent_spawn_and_activity_metadata(self) -> None:
        transcript = Path(self.temporary.name) / "agents.jsonl"
        records = [
            {
                "timestamp": "2026-08-08T10:00:00Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "spawn_agent",
                    "call_id": "call-1",
                    "arguments": json.dumps(
                        {
                            "task_name": "history_audit",
                            "model": "gpt-5.6-sol",
                            "message": "encrypted-or-sensitive",
                        }
                    ),
                },
            },
            {
                "timestamp": "2026-08-08T10:00:01Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call-1",
                    "output": json.dumps({"task_name": "/root/history_audit"}),
                },
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "sub_agent_activity",
                    "agent_path": "/root/history_audit",
                    "kind": "interacted",
                    "occurred_at_ms": 1_786_129_202_000,
                },
            },
        ]
        transcript.write_text(
            "".join(json.dumps(record) + "\n" for record in records),
            encoding="utf-8",
        )

        scanner = hud_render.TranscriptScanner()
        scanner.scan(str(transcript))

        metadata = scanner.agent_metadata["/root/history_audit"]
        self.assertEqual(metadata["task_name"], "history_audit")
        self.assertEqual(metadata["description"], "history audit")
        self.assertEqual(metadata["model"], "gpt-5.6-sol")
        self.assertEqual(metadata["current_activity"], "interacted")
        self.assertNotIn("encrypted-or-sensitive", json.dumps(metadata))

        transcript.write_text("", encoding="utf-8")
        scanner.scan(str(transcript))
        self.assertEqual(scanner.agent_metadata, {})

    def test_specific_model_price_wins_over_generic_prefix(self) -> None:
        self.assertAlmostEqual(
            hud_render.estimated_cost(
                "gpt-5.6-terra-2026-08-01", {"input_tokens": 1_000_000}
            ),
            2.5,
        )

    def test_hook_main_without_wrapper_run_id_does_not_collect(self) -> None:
        os.environ.pop("CODEX_HUD_RUN_ID", None)
        event = {"hook_event_name": "Stop", "session_id": "ordinary-session"}
        output = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(event))):
            with redirect_stdout(output):
                self.assertEqual(hud_hook.main(), 0)
        self.assertEqual(output.getvalue(), "{}\n")
        self.assertFalse(hud_state.run_path("ordinary-session").exists())

    def test_account_reader_handles_missing_codex(self) -> None:
        with mock.patch.object(hud_render.subprocess, "Popen", side_effect=OSError):
            self.assertEqual(hud_render.AccountReader().read(), ({}, {}))

    def test_account_reader_treats_json_rpc_error_as_completed(self) -> None:
        class FakeProcess:
            def __init__(self) -> None:
                self.stdin = io.StringIO()
                self.stdout = io.StringIO(
                    "\n".join(
                        [
                            json.dumps({"id": 0, "result": {}}),
                            json.dumps(
                                {
                                    "id": 1,
                                    "result": {
                                        "account": {
                                            "type": "chatgpt",
                                            "email": "me@example.com",
                                            "planType": "plus",
                                        }
                                    },
                                }
                            ),
                            json.dumps(
                                {
                                    "id": 2,
                                    "error": {"code": -32600, "message": "unsupported"},
                                }
                            ),
                            "",
                        ]
                    )
                )

            def poll(self) -> int | None:
                return None

            def terminate(self) -> None:
                return None

            def wait(self, timeout: float) -> int:
                del timeout
                return 0

        process = FakeProcess()
        with mock.patch.object(hud_render.subprocess, "Popen", return_value=process):
            started = time.perf_counter()
            account, limits = hud_render.AccountReader().read(timeout=0.5)
        self.assertLess(time.perf_counter() - started, 0.3)
        self.assertEqual(account["type"], "chatgpt")
        self.assertEqual(limits, {})

    def test_account_poller_does_not_block_first_snapshot(self) -> None:
        class BlockingReader:
            def __init__(self) -> None:
                self.started = threading.Event()
                self.release = threading.Event()

            def read(self) -> tuple[dict[str, object], dict[str, object]]:
                self.started.set()
                self.release.wait(timeout=2)
                return {"type": "apiKey"}, {}

        reader = BlockingReader()
        poller = hud_render.AccountPoller(reader, refresh_seconds=60)
        try:
            poller.start()
            self.assertTrue(reader.started.wait(timeout=1))
            started = time.perf_counter()
            account, limits, loaded = poller.snapshot()
            self.assertLess(time.perf_counter() - started, 0.1)
            self.assertEqual((account, limits, loaded), ({}, {}, False))
            reader.release.set()
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                account, limits, loaded = poller.snapshot()
                if loaded:
                    break
                time.sleep(0.01)
            self.assertEqual(account, {"type": "apiKey"})
            self.assertEqual(limits, {})
            self.assertTrue(loaded)
        finally:
            reader.release.set()
            poller.stop()

    def test_render_contains_requested_sections(self) -> None:
        state = hud_state.default_state(self.run_id)
        state.update(
            {
                "cwd": self.temporary.name,
                "model": "gpt-5.6-terra",
                "plan": [{"step": "Verify", "status": "in_progress"}],
            }
        )
        scanner = hud_render.TranscriptScanner()
        scanner.last = {"total_tokens": 112_000}
        scanner.total = {
            "input_tokens": 1000,
            "cached_input_tokens": 800,
            "output_tokens": 100,
        }
        scanner.context_window = 212_000
        lines = hud_render.render_lines(
            state,
            scanner,
            {"type": "chatgpt", "email": "me@example.com", "planType": "plus"},
            {},
        )
        output = "\n".join(lines)
        self.assertEqual(len(lines), 9)
        for section in ("acct:me/plus", "CTX", "CACHE", "TOOLS", "AGENTS", "SKILLS", "TODO"):
            self.assertIn(section, output)

    def test_render_shows_unfinished_plan_items_and_overflow(self) -> None:
        state = hud_state.default_state(self.run_id)
        state.update(
            {
                "cwd": self.temporary.name,
                "plan": [
                    {"step": "Already done", "status": "completed"},
                    {"step": "Current task", "status": "in_progress"},
                    {"step": "Next task", "status": "pending"},
                    {"step": "Later task", "status": "pending"},
                ],
                "plan_state": "stale",
            }
        )
        lines = hud_render.render_lines(
            state,
            hud_render.TranscriptScanner(),
            {},
            {},
        )

        self.assertEqual(len(lines), 10)
        output = "\n".join(lines)
        self.assertIn("3 未完成", output)
        self.assertIn("计划未同步", output)
        self.assertIn("Current task", output)
        self.assertIn("Next task", output)
        self.assertIn("+1", output)

    def test_render_shows_current_tasks_and_recent_completion(self) -> None:
        state = hud_state.default_state(self.run_id)
        hud_state.reconcile_plan(
            state,
            [{"step": "Recently done", "status": "completed"}],
            revision=1,
            turn_id="turn-1",
            updated_at=10,
        )
        hud_state.reconcile_plan(
            state,
            [
                {"step": "Current task", "status": "in_progress"},
                {"step": "Next task", "status": "pending"},
            ],
            revision=2,
            turn_id="turn-2",
            updated_at=20,
        )

        lines = hud_render.render_lines(
            state,
            hud_render.TranscriptScanner(),
            {},
            {},
        )

        self.assertEqual(len(lines), 11)
        output = "\n".join(lines)
        self.assertIn("Current task", output)
        self.assertIn("Next task", output)
        self.assertIn("Recently done", output)
        self.assertIn("最近完成", lines[7])

    def test_render_agents_shows_description_model_and_current_activity(self) -> None:
        state = hud_state.default_state(self.run_id)
        state["agents"] = {
            "agent-1": {
                "type": "reviewer",
                "status": "running",
                "started_at": time.time() - 5,
                "ended_at": None,
                "description": "TODO history audit\x1b]8;;bad\x07",
                "model": "gpt-5.6-sol",
                "current_activity": "running tests",
                "source_confidence": "hook+spawn",
            }
        }

        lines = hud_render.render_lines(
            state,
            hud_render.TranscriptScanner(),
            {},
            {},
        )

        self.assertIn("AGENTS 1 running", lines[5])
        self.assertIn("reviewer [gpt-5.6-sol]", lines[6])
        self.assertIn("TODO history audit", lines[6])
        self.assertIn("running tests", lines[6])
        self.assertNotIn("\x1b]", lines[6])

    def test_render_agents_keeps_recent_completed_agent_summary(self) -> None:
        state = hud_state.default_state(self.run_id)
        state["agents"] = {
            "agent-1": {
                "type": "reviewer",
                "status": "completed",
                "started_at": 1,
                "ended_at": 2,
                "description": "history review",
                "model": "gpt-5.6-sol",
                "current_activity": "completed",
                "source_confidence": "hook+spawn",
            }
        }

        lines = hud_render.render_lines(
            state,
            hud_render.TranscriptScanner(),
            {},
            {},
        )

        self.assertIn("✓ 1 complete", lines[5])

    def test_completed_plan_renders_recent_completed_details(self) -> None:
        state = hud_state.default_state(self.run_id)
        hud_state.reconcile_plan(
            state,
            [
                {"step": "First", "status": "completed"},
                {"step": "Second", "status": "completed"},
            ],
            revision=1,
            turn_id="turn-1",
            updated_at=10,
        )

        lines = hud_render.render_lines(
            state,
            hud_render.TranscriptScanner(),
            {},
            {},
        )

        self.assertEqual(len(lines), 10)
        self.assertIn("全部完成", lines[7])
        self.assertIn("First", "\n".join(lines[8:]))
        self.assertIn("Second", "\n".join(lines[8:]))

    def test_invalid_plan_item_never_reports_all_completed(self) -> None:
        state = hud_state.default_state(self.run_id)
        state.update(
            {
                "cwd": self.temporary.name,
                "plan": [{"step": 123, "status": "pending"}],
                "plan_state": "active",
            }
        )

        lines = hud_render.render_lines(
            state,
            hud_render.TranscriptScanner(),
            {},
            {},
        )

        self.assertNotIn("全部完成", lines[7])
        self.assertIn("1 未完成", lines[7])

    def test_load_state_migrates_legacy_plan_and_version(self) -> None:
        path = hud_state.run_path(self.run_id)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "version": 2,
                    "run_id": self.run_id,
                    "plan": [{"step": 123, "status": "pending"}],
                    "plan_state": "empty",
                }
            ),
            encoding="utf-8",
        )

        state = hud_state.load_state(self.run_id)
        self.assertEqual(state["version"], hud_state.STATE_VERSION)
        self.assertEqual(state["plan"][0]["status"], "pending")
        self.assertEqual(state["plan"][0]["step"], "未命名任务")
        self.assertEqual(state["plan_state"], "active")
        self.assertEqual(state["current_plan"][0]["step"], "未命名任务")

    def test_status_details_prints_plan_history_and_agent_sources(self) -> None:
        state = hud_state.default_state(self.run_id)
        hud_state.reconcile_plan(
            state,
            [{"step": "Historical", "status": "completed"}],
            revision=1,
            turn_id="turn-1",
            updated_at=10,
        )
        state["agents"] = {
            "agent-1": {
                "type": "reviewer",
                "status": "running",
                "started_at": 1,
                "ended_at": None,
                "description": "history review",
                "model": "gpt-5.6-sol",
                "current_activity": "testing",
                "source_confidence": "hook+spawn",
            }
        }
        hud_state.update_state(self.run_id, lambda current: current.update(state))

        output = io.StringIO()
        with redirect_stdout(output):
            codex_hud_cli._print_run_status(self.run_id, details=True)

        rendered = output.getvalue()
        self.assertIn("history:", rendered)
        self.assertIn("Historical", rendered)
        self.assertIn("agents:", rendered)
        self.assertIn("history review", rendered)
        self.assertIn("source=hook+spawn", rendered)

    def test_small_geometry_preserves_todo_summary(self) -> None:
        state = hud_state.default_state(self.run_id)
        state.update(
            {
                "cwd": self.temporary.name,
                "plan": [{"step": "Keep visible", "status": "pending"}],
                "plan_state": "active",
            }
        )
        lines = hud_render.render_lines(
            state,
            hud_render.TranscriptScanner(),
            {},
            {},
        )

        visible = hud_render.fit_lines_for_geometry(
            lines, 7, agent_rows=0, detail_rows=1
        )
        self.assertEqual(len(visible), 7)
        self.assertIn("TODO", "\n".join(visible))
        self.assertNotIn("Keep visible", "\n".join(visible))

    def test_partial_dynamic_geometry_keeps_summary_and_first_detail(self) -> None:
        lines = [f"line-{index}" for index in range(6)]
        lines += ["agent-1", "agent-2"]
        lines += ["skills", "todo"]
        lines += ["detail-1", "detail-2"]

        visible = hud_render.fit_lines_for_geometry(
            lines, 9, agent_rows=2, detail_rows=2
        )

        self.assertEqual(len(visible), 9)
        self.assertIn("todo", visible)
        self.assertIn("skills", visible)
        self.assertIn("agent-1", visible)
        self.assertNotIn("agent-2", visible)
        self.assertNotIn("detail-1", visible)

    def test_plan_detail_preserves_overflow_suffix_when_clipped(self) -> None:
        item = {"step": "B" * 240, "status": "pending"}
        line = hud_render.plan_detail_line(item, 1, 3, 80)

        self.assertIn("… +1", line)

    def test_render_width_passes_through_to_plan_detail(self) -> None:
        state = hud_state.default_state(self.run_id)
        state.update(
            {
                "cwd": self.temporary.name,
                "plan": [
                    {"step": "Current", "status": "in_progress"},
                    {"step": "B" * 240, "status": "pending"},
                    {"step": "Later", "status": "pending"},
                ],
            }
        )
        lines = hud_render.render_lines(
            state,
            hud_render.TranscriptScanner(),
            {},
            {},
            width=80,
        )

        self.assertIn("… +1", lines[9])

    def test_render_stays_eight_lines_when_plan_is_complete(self) -> None:
        state = hud_state.default_state(self.run_id)
        state.update(
            {
                "cwd": self.temporary.name,
                "plan": [
                    {"step": "First", "status": "completed"},
                    {"step": "Second", "status": "completed"},
                ],
                "plan_state": "completed",
            }
        )

        lines = hud_render.render_lines(
            state,
            hud_render.TranscriptScanner(),
            {},
            {},
        )
        self.assertEqual(len(lines), 8)
        self.assertIn("2/2", lines[7])
        self.assertIn("全部完成", lines[7])

    def test_desired_hud_lines_tracks_unfinished_task_count(self) -> None:
        state = hud_state.default_state(self.run_id)
        self.assertEqual(hud_state.desired_hud_lines(state), 8)
        state["plan"] = [{"step": "One", "status": "pending"}]
        self.assertEqual(hud_state.desired_hud_lines(state), 9)
        state["plan"] = [
            {"step": "One", "status": "in_progress"},
            {"step": "Two", "status": "pending"},
            {"step": "Three", "status": "pending"},
        ]
        self.assertEqual(hud_state.desired_hud_lines(state), 10)

    def test_renderer_resizes_its_tmux_pane_to_dynamic_target(self) -> None:
        os.environ["TMUX_PANE"] = "%hud"
        try:
            with mock.patch.object(hud_render.subprocess, "run") as run:
                run.return_value.returncode = 0
                self.assertTrue(hud_render.resize_tmux_surface(10))
        finally:
            os.environ.pop("TMUX_PANE", None)

        run.assert_called_once_with(
            ["tmux", "resize-pane", "-t", "%hud", "-y", "10"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def test_tmux_resize_decision_uses_observed_rows(self) -> None:
        os.environ["TMUX_PANE"] = "%hud"
        try:
            self.assertTrue(hud_render.tmux_surface_needs_resize(10, 8))
            self.assertFalse(hud_render.tmux_surface_needs_resize(10, 10))
        finally:
            os.environ.pop("TMUX_PANE", None)

    def test_api_render_exposes_usage_and_marks_estimate(self) -> None:
        state = hud_state.default_state(self.run_id)
        state.update({"cwd": self.temporary.name, "model": "gpt-5.6-terra"})
        scanner = hud_render.TranscriptScanner()
        scanner.total = {
            "input_tokens": 1_000_000,
            "cached_input_tokens": 800_000,
            "output_tokens": 100_000,
            "reasoning_output_tokens": 25_000,
        }
        lines = hud_render.render_lines(
            state,
            scanner,
            {"type": "apiKey"},
            {},
        )
        output = "\n".join(lines)
        self.assertIn("acct:API key", output)
        self.assertIn("API equiv.$2.20", output)
        self.assertIn("API in 200k + cache 800k · out 100k · reasoning 25k", output)
        self.assertNotIn("primary", output)

    def test_render_marks_live_context_amplification_risk(self) -> None:
        state = hud_state.default_state(self.run_id)
        state.update({"cwd": self.temporary.name, "model": "gpt-5.6-sol"})
        scanner = hud_render.TranscriptScanner()
        scanner.total = {
            "input_tokens": 120_000_000,
            "cached_input_tokens": 110_000_000,
            "output_tokens": 250_000,
        }
        scanner.tool_outputs = 1_100
        scanner.image_blocks = 120
        scanner.tool_output_bytes = 60_000_000
        lines = hud_render.render_lines(state, scanner, {"type": "chatgpt"}, {})
        output = "\n".join(lines)
        self.assertIn("IO 1100t/120img", output)
        self.assertIn("critical", output)

    def test_render_separates_openai_chatgpt_quota_from_custom_provider(self) -> None:
        state = hud_state.default_state(self.run_id)
        state.update({"cwd": self.temporary.name, "model": "gpt-5.6-sol"})
        scanner = hud_render.TranscriptScanner()
        scanner.model = "gpt-5.6-sol"
        scanner.total = {
            "input_tokens": 23_264_350,
            "cached_input_tokens": 22_866_688,
            "output_tokens": 68_670,
        }

        scanner.model_provider = "openai"
        openai_output = "\n".join(
            hud_render.render_lines(state, scanner, {"type": "chatgpt"}, {})
        )
        self.assertIn("≈387.0cr quota", openai_output)

        scanner.model_provider = "custom"
        custom_output = "\n".join(
            hud_render.render_lines(state, scanner, {"type": "chatgpt"}, {})
        )
        self.assertIn("≈quota --", custom_output)
        self.assertNotIn("cr quota", custom_output)

    def test_context_matches_codex_0162_native_tui(self) -> None:
        usage = metrics.official_context_usage(122_880, 258_400)
        self.assertIsNotNone(usage)
        assert usage is not None
        self.assertEqual(usage.used_tokens, 110_880)
        self.assertEqual(usage.window_tokens, 246_400)
        self.assertEqual(usage.used_percent, 45)
        self.assertEqual(usage.remaining_percent, 55)

    def test_context_rounds_remaining_like_rust(self) -> None:
        usage = metrics.official_context_usage(62, 112, baseline_tokens=0)
        self.assertIsNotNone(usage)
        assert usage is not None
        self.assertEqual((usage.used_percent, usage.remaining_percent), (55, 45))

    def test_context_zero_effective_window_matches_native_tui(self) -> None:
        usage = metrics.official_context_usage(100, 0)
        self.assertIsNotNone(usage)
        assert usage is not None
        self.assertEqual((usage.used_percent, usage.remaining_percent), (100, 0))

    def test_context_is_unknown_without_a_window_event(self) -> None:
        self.assertIsNone(metrics.official_context_usage(100, None))

    def test_nested_session_cannot_replace_bound_thread(self) -> None:
        hud_hook.handle_event(
            {
                "hook_event_name": "SessionStart",
                "session_id": "owner-thread",
                "transcript_path": "/tmp/owner.jsonl",
                "source": "startup",
            },
            self.run_id,
        )
        hud_hook.handle_event(
            {
                "hook_event_name": "SessionStart",
                "session_id": "nested-thread",
                "transcript_path": "/tmp/nested.jsonl",
                "source": "startup",
            },
            self.run_id,
        )
        state = hud_state.load_state(self.run_id)
        self.assertEqual(state["session_id"], "owner-thread")
        self.assertEqual(state["transcript_path"], "/tmp/owner.jsonl")

    def test_clear_and_resume_can_transition_to_a_new_thread(self) -> None:
        for source in ("clear", "resume"):
            with self.subTest(source=source):
                run_id = f"transition-{source}"
                hud_hook.handle_event(
                    {
                        "hook_event_name": "SessionStart",
                        "session_id": "owner-thread",
                        "transcript_path": "/tmp/owner.jsonl",
                        "source": "startup",
                    },
                    run_id,
                )
                hud_state.update_state(
                    run_id,
                    lambda state: state.update(
                        plan=[{"step": "old", "status": "in_progress"}]
                    ),
                )
                hud_hook.handle_event(
                    {
                        "hook_event_name": "SessionStart",
                        "session_id": f"new-{source}-thread",
                        "transcript_path": f"/tmp/{source}.jsonl",
                        "source": source,
                    },
                    run_id,
                )
                state = hud_state.load_state(run_id)
                self.assertEqual(state["session_id"], f"new-{source}-thread")
                self.assertEqual(state["transcript_path"], f"/tmp/{source}.jsonl")
                self.assertEqual(state["plan"], [])

    def test_different_codex_home_cannot_update_bound_session(self) -> None:
        os.environ["CODEX_HOME"] = str(Path(self.temporary.name) / "owner")
        hud_hook.handle_event(
            {
                "hook_event_name": "SessionStart",
                "session_id": "owner-thread",
                "source": "startup",
            },
            self.run_id,
        )
        os.environ["CODEX_HOME"] = str(Path(self.temporary.name) / "nested")
        hud_hook.handle_event(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "owner-thread",
            },
            self.run_id,
        )
        self.assertEqual(hud_state.load_state(self.run_id)["status"], "ready")
        os.environ.pop("CODEX_HOME", None)

    def test_transcript_rate_limit_is_used_when_app_server_is_unavailable(self) -> None:
        state = hud_state.default_state(self.run_id)
        scanner = hud_render.TranscriptScanner()
        scanner.rate_limits = {
            "primary": {
                "used_percent": 64,
                "window_minutes": 10080,
                "resets_at": 9_999_999_999,
            }
        }
        output = "\n".join(hud_render.render_lines(state, scanner, {}, {}))
        self.assertIn("7d 64%", output)

    def test_visible_clip_counts_wide_characters_and_removes_overflow(self) -> None:
        self.assertEqual(hud_render.visible_clip("缓存abcdef", 6), "缓存ab")

    def test_tmux_command_leaves_term_owned_by_tmux(self) -> None:
        command = codex_hud_cli._tmux_command(
            ["codex"],
            {
                "PATH": "/opt/bin",
                "TERM": "xterm-ghostty",
                "CODEX_HOME": "/tmp/codex-home",
            },
        )
        self.assertIn("PATH=/opt/bin", command)
        self.assertIn("CODEX_HOME=/tmp/codex-home", command)
        self.assertNotIn("TERM=", command)

    def test_status_can_inspect_plan_details(self) -> None:
        hud_state.update_state(
            self.run_id,
            lambda state: state.update(
                plan=[
                    {"step": "Done", "status": "completed"},
                    {"step": "Pending", "status": "pending"},
                ],
                plan_state="stale",
            ),
        )
        output = io.StringIO()
        with redirect_stdout(output):
            codex_hud_cli._print_run_status(self.run_id, details=True)

        rendered = output.getvalue()
        self.assertIn("plan: stale 1/2", rendered)
        self.assertIn("Pending [pending]", rendered)

    def test_hud_forces_codex_native_inline_scrollback_mode(self) -> None:
        self.assertEqual(
            codex_hud_cli._codex_arguments(["-C", "/tmp/project"]),
            ["--no-alt-screen", "-C", "/tmp/project"],
        )
        self.assertEqual(
            codex_hud_cli._codex_arguments(["--no-alt-screen", "resume", "--last"]),
            ["--no-alt-screen", "resume", "--last"],
        )

    def test_standalone_ghostty_routes_to_native_split_without_tmux(self) -> None:
        environment = {
            "CODEX_HUD_HOME": self.temporary.name,
            "PATH": os.environ.get("PATH", ""),
            "TERM_PROGRAM": "ghostty",
        }
        with mock.patch.object(
            codex_hud_cli, "enabled", return_value=True
        ), mock.patch.object(
            codex_hud_cli, "_require"
        ) as require, mock.patch.object(
            codex_hud_cli.os, "environ", environment
        ), mock.patch.object(
            codex_hud_cli, "_native_ghostty_hud", return_value=True
        ), mock.patch.object(
            codex_hud_cli, "_launch_native_ghostty", return_value=17
        ) as native, mock.patch.object(
            codex_hud_cli.subprocess, "run"
        ) as tmux_run:
            self.assertEqual(codex_hud_cli.launch(["resume", "--last"]), 17)

        require.assert_called_once_with("codex")
        native.assert_called_once_with(
            ["--no-alt-screen", "resume", "--last"],
            environment,
            mock.ANY,
            mock.ANY,
        )
        tmux_run.assert_not_called()

    def test_native_ghostty_launch_closes_only_its_hud_surface(self) -> None:
        environment = {
            "CODEX_HUD_HOME": self.temporary.name,
            "PATH": os.environ.get("PATH", ""),
        }
        lease = ("main-terminal", "hud-terminal", 2.0)
        resize_stop = mock.Mock()
        resize_thread = mock.Mock()
        cleanup_order: list[str] = []
        resize_thread.join.side_effect = lambda **_kwargs: cleanup_order.append("join")
        with mock.patch.object(
            codex_hud_cli, "_ghostty_open_hud", return_value=lease
        ), mock.patch.object(
            codex_hud_cli, "_ghostty_make_readonly"
        ) as make_readonly, mock.patch.object(
            codex_hud_cli, "_ghostty_focus_terminal"
        ) as focus_terminal, mock.patch.object(
            codex_hud_cli,
            "_ghostty_set_reported_working_directory",
            return_value=True,
        ), mock.patch.object(
            codex_hud_cli, "_ghostty_resize_hud", return_value=True
        ) as resize, mock.patch.object(
            codex_hud_cli, "_wait_for_path", return_value=True
        ), mock.patch.object(
            codex_hud_cli,
            "_start_ghostty_size_monitor",
            return_value=(resize_stop, resize_thread),
        ) as start_monitor, mock.patch.object(
            codex_hud_cli.subprocess, "call", return_value=0
        ) as codex_call, mock.patch.object(
            codex_hud_cli,
            "_ghostty_close_terminal",
            side_effect=lambda _terminal: cleanup_order.append("close"),
        ) as close_terminal, mock.patch.object(
            codex_hud_cli, "_mark_ended"
        ) as mark_ended, mock.patch.object(
            codex_hud_cli, "_remove_run"
        ) as remove_run, mock.patch.object(
            codex_hud_cli, "_install_termination_handlers", return_value={}
        ), mock.patch.object(
            codex_hud_cli, "_suspend_termination_handlers"
        ), mock.patch.object(
            codex_hud_cli, "_restore_termination_handlers"
        ):
            self.assertEqual(
                codex_hud_cli._launch_native_ghostty(
                    ["--no-alt-screen"], environment, "native-run", "hud command"
                ),
                0,
            )

        resize.assert_called_once_with(
            "main-terminal",
            "hud-terminal",
            mock.ANY,
            2.0,
            hud_state.HUD_MIN_LINES,
        )
        make_readonly.assert_called_once_with("hud-terminal")
        focus_terminal.assert_called_once_with("main-terminal")
        start_monitor.assert_called_once_with(
            "main-terminal",
            "hud-terminal",
            mock.ANY,
            mock.ANY,
            2.0,
            "native-run",
        )
        resize_stop.set.assert_called_once_with()
        resize_thread.join.assert_called_once_with(timeout=12.0)
        codex_call.assert_called_once_with(
            ["codex", "--no-alt-screen"],
            env={**environment, "CODEX_HUD_RUN_ID": "native-run"},
        )
        close_terminal.assert_called_once_with("hud-terminal")
        self.assertEqual(cleanup_order, ["join", "close"])
        mark_ended.assert_called_once_with("native-run")
        remove_run.assert_called_once_with("native-run")

    def test_ghostty_open_hud_uses_unambiguous_literal_separator(self) -> None:
        target = Path(self.temporary.name) / "literal-separator-target"
        with mock.patch.object(
            codex_hud_cli,
            "_run_applescript",
            return_value="main-terminal|hud-terminal|2.0",
        ) as run_applescript:
            self.assertEqual(
                codex_hud_cli._ghostty_open_hud(
                    "hud command",
                    {"PATH": "/opt/bin"},
                    "/tmp/project",
                    target,
                ),
                ("main-terminal", "hud-terminal", 2.0),
            )

        script = run_applescript.call_args.args[0]
        self.assertIn('& "|" &', script)
        self.assertNotIn(" & tab & ", script)
        self.assertNotIn("toggle_readonly", script)

    def test_ghostty_open_hud_targets_the_calling_surface_marker(self) -> None:
        target = Path(self.temporary.name) / "surface target"
        with mock.patch.object(
            codex_hud_cli,
            "_run_applescript",
            return_value="main-terminal|hud-terminal|not-a-number",
        ) as run_applescript:
            self.assertEqual(
                codex_hud_cli._ghostty_open_hud(
                    "hud command",
                    {"PATH": "/opt/bin"},
                    "/tmp/project",
                    target,
                ),
                ("main-terminal", "hud-terminal", 1.0),
            )

        script = run_applescript.call_args.args[0]
        self.assertIn(
            f'first terminal whose working directory is "{target.resolve()}"',
            script,
        )
        self.assertIn("NSScreen's screens()", script)

    def test_native_startup_failure_closes_split_before_fallback(self) -> None:
        environment = {
            "CODEX_HUD_HOME": self.temporary.name,
            "PATH": os.environ.get("PATH", ""),
        }
        with mock.patch.object(
            codex_hud_cli,
            "_ghostty_open_hud",
            return_value=("main-terminal", "hud-terminal", 2.0),
        ), mock.patch.object(
            codex_hud_cli, "_ghostty_make_readonly", side_effect=RuntimeError("readonly")
        ), mock.patch.object(
            codex_hud_cli, "_ghostty_close_terminal"
        ) as close_terminal, mock.patch.object(
            codex_hud_cli, "_ghostty_set_reported_working_directory", return_value=True
        ), mock.patch.object(
            codex_hud_cli, "_remove_run"
        ), mock.patch.object(
            codex_hud_cli, "_install_termination_handlers", return_value={}
        ), mock.patch.object(
            codex_hud_cli, "_suspend_termination_handlers"
        ), mock.patch.object(
            codex_hud_cli, "_restore_termination_handlers"
        ), mock.patch.object(
            codex_hud_cli.subprocess, "call", return_value=23
        ) as codex_call:
            self.assertEqual(
                codex_hud_cli._launch_native_ghostty(
                    ["--no-alt-screen"], environment, "failed-native", "hud command"
                ),
                23,
            )

        close_terminal.assert_called_once_with("hud-terminal")
        codex_call.assert_called_once_with(
            ["codex", "--no-alt-screen"], env=environment
        )

    def test_native_marker_failure_falls_back_without_opening_wrong_surface(self) -> None:
        environment = {
            "CODEX_HUD_HOME": self.temporary.name,
            "PATH": os.environ.get("PATH", ""),
        }
        with mock.patch.object(
            codex_hud_cli,
            "_ghostty_set_reported_working_directory",
            return_value=False,
        ), mock.patch.object(codex_hud_cli, "_ghostty_open_hud") as open_hud, mock.patch.object(
            codex_hud_cli, "_ghostty_close_terminal_by_working_directory"
        ) as close_by_directory, mock.patch.object(
            codex_hud_cli, "_remove_run"
        ), mock.patch.object(
            codex_hud_cli, "_install_termination_handlers", return_value={}
        ), mock.patch.object(
            codex_hud_cli, "_suspend_termination_handlers"
        ), mock.patch.object(
            codex_hud_cli, "_restore_termination_handlers"
        ), mock.patch.object(
            codex_hud_cli.subprocess, "call", return_value=23
        ) as codex_call:
            self.assertEqual(
                codex_hud_cli._launch_native_ghostty(
                    ["--no-alt-screen"], environment, "marker-failure", "hud command"
                ),
                23,
            )

        open_hud.assert_not_called()
        close_by_directory.assert_not_called()
        codex_call.assert_called_once_with(
            ["codex", "--no-alt-screen"], env=environment
        )

    def test_native_split_timeout_closes_orphan_by_marker_directory(self) -> None:
        environment = {
            "CODEX_HUD_HOME": self.temporary.name,
            "PATH": os.environ.get("PATH", ""),
        }
        timeout = subprocess.TimeoutExpired(["osascript"], 15)
        with mock.patch.object(
            codex_hud_cli,
            "_ghostty_set_reported_working_directory",
            return_value=True,
        ), mock.patch.object(
            codex_hud_cli, "_ghostty_open_hud", side_effect=timeout
        ), mock.patch.object(
            codex_hud_cli, "_ghostty_close_terminal_by_working_directory"
        ) as close_by_directory, mock.patch.object(
            codex_hud_cli, "_remove_run"
        ), mock.patch.object(
            codex_hud_cli, "_install_termination_handlers", return_value={}
        ), mock.patch.object(
            codex_hud_cli, "_suspend_termination_handlers"
        ), mock.patch.object(
            codex_hud_cli, "_restore_termination_handlers"
        ), mock.patch.object(
            codex_hud_cli.subprocess, "call", return_value=23
        ):
            self.assertEqual(
                codex_hud_cli._launch_native_ghostty(
                    ["--no-alt-screen"], environment, "split-timeout", "hud command"
                ),
                23,
            )

        close_by_directory.assert_called_once()
        marker_path = close_by_directory.call_args.args[0]
        self.assertIn(".split-timeout.ghostty-hud", str(marker_path))

    def test_native_signal_during_split_closes_orphan_by_marker_directory(self) -> None:
        environment = {
            "CODEX_HUD_HOME": self.temporary.name,
            "PATH": os.environ.get("PATH", ""),
        }
        with mock.patch.object(
            codex_hud_cli,
            "_ghostty_set_reported_working_directory",
            return_value=True,
        ), mock.patch.object(
            codex_hud_cli, "_ghostty_open_hud", side_effect=SystemExit(143)
        ), mock.patch.object(
            codex_hud_cli, "_ghostty_close_terminal_by_working_directory"
        ) as close_by_directory, mock.patch.object(
            codex_hud_cli, "_remove_run"
        ), mock.patch.object(
            codex_hud_cli, "_install_termination_handlers", return_value={}
        ), mock.patch.object(
            codex_hud_cli, "_suspend_termination_handlers"
        ), mock.patch.object(
            codex_hud_cli, "_restore_termination_handlers"
        ):
            with self.assertRaises(SystemExit):
                codex_hud_cli._launch_native_ghostty(
                    ["--no-alt-screen"], environment, "split-signal", "hud command"
                )

        close_by_directory.assert_called_once()
        marker_path = close_by_directory.call_args.args[0]
        self.assertIn(".split-signal.ghostty-hud", str(marker_path))

    def test_native_cleanup_recovers_when_terminal_id_close_fails(self) -> None:
        marker = Path(self.temporary.name) / "unique-hud-marker"
        with mock.patch.object(
            codex_hud_cli,
            "_ghostty_close_terminal",
            side_effect=RuntimeError("terminal disappeared"),
        ) as close_terminal, mock.patch.object(
            codex_hud_cli, "_ghostty_close_terminal_by_working_directory"
        ) as close_by_directory:
            codex_hud_cli._close_native_hud("hud-terminal", marker, True)

        close_terminal.assert_called_once_with("hud-terminal")
        close_by_directory.assert_called_once_with(marker)

    def test_resize_calibrates_unknown_display_scale_from_geometry_delta(self) -> None:
        geometries = [
            {"rows": 22, "columns": 120, "width_pixels": 1928, "height_pixels": 748},
            {"rows": 15, "columns": 120, "width_pixels": 1928, "height_pixels": 510},
            {"rows": 8, "columns": 120, "width_pixels": 1928, "height_pixels": 272},
        ]
        applied: list[str] = []
        with mock.patch.object(
            codex_hud_cli, "_wait_for_geometry", side_effect=geometries
        ), mock.patch.object(
            codex_hud_cli, "_ghostty_apply_resize", side_effect=lambda _m, _h, action: applied.append(action)
        ):
            self.assertTrue(
                codex_hud_cli._ghostty_resize_hud(
                    "main", "hud", Path("/tmp/geometry.json"), 2.0
                )
            )

        self.assertEqual(applied, ["resize_split:down,238", "resize_split:down,238"])

    def test_resize_does_not_start_action_after_deadline(self) -> None:
        with mock.patch.object(
            codex_hud_cli.time,
            "monotonic",
            side_effect=[100.0, 109.0],
        ), mock.patch.object(
            codex_hud_cli, "_wait_for_geometry", return_value={
                "rows": 22,
                "columns": 120,
                "width_pixels": 1928,
                "height_pixels": 748,
            }
        ), mock.patch.object(codex_hud_cli, "_ghostty_apply_resize") as apply_resize:
            self.assertFalse(
                codex_hud_cli._ghostty_resize_hud(
                    "main", "hud", Path("/tmp/geometry.json"), 2.0
                )
            )
        apply_resize.assert_not_called()

    def test_ghostty_monitor_stops_when_renderer_heartbeat_is_stale(self) -> None:
        heartbeat = Path(self.temporary.name) / "heartbeat"
        heartbeat.touch()
        os.utime(heartbeat, (1, 1))
        stop_event = mock.Mock()
        stop_event.wait.return_value = False

        with mock.patch.object(codex_hud_cli, "enabled", return_value=True), mock.patch.object(
            codex_hud_cli, "_read_terminal_geometry"
        ) as read_geometry, mock.patch.object(
            codex_hud_cli, "_ghostty_resize_hud"
        ) as resize:
            codex_hud_cli._maintain_ghostty_hud_size(
                "main",
                "hud",
                Path(self.temporary.name) / "geometry.json",
                heartbeat,
                2.0,
                "run",
                stop_event,
            )

        read_geometry.assert_not_called()
        resize.assert_not_called()

    def test_ghostty_working_directory_marker_is_encoded_and_invisible(self) -> None:
        output = mock.Mock()
        output.fileno.return_value = 19
        with mock.patch.object(codex_hud_cli.sys, "stdout", output), mock.patch.object(
            codex_hud_cli.os, "isatty", return_value=True
        ):
            self.assertTrue(
                codex_hud_cli._ghostty_set_reported_working_directory(
                    Path(self.temporary.name) / "surface target"
                )
            )

        written = output.write.call_args.args[0]
        self.assertTrue(written.startswith("\033]7;file://localhost/"))
        self.assertIn("surface%20target", written)
        self.assertTrue(written.endswith("\a"))
        output.flush.assert_called_once_with()

    def test_native_hud_resize_uses_terminal_pixels_and_retina_scale(self) -> None:
        self.assertEqual(
            codex_hud_cli._ghostty_resize_action(
                {"rows": 22, "height_pixels": 748}, 2.0
            ),
            "resize_split:down,238",
        )
        self.assertEqual(
            codex_hud_cli._ghostty_resize_action(
                {"rows": 4, "height_pixels": 136}, 2.0
            ),
            "resize_split:up,68",
        )
        self.assertIsNone(
            codex_hud_cli._ghostty_resize_action(
                {"rows": hud_state.HUD_LINES, "height_pixels": 272}, 2.0
            )
        )
        self.assertEqual(
            codex_hud_cli._ghostty_resize_action(
                {"rows": 8, "height_pixels": 272}, 2.0, target_lines=10
            ),
            "resize_split:up,34",
        )

    def test_renderer_reports_terminal_rows_and_pixels_for_native_resize(self) -> None:
        packed = struct.pack("HHHH", 8, 120, 1928, 272)
        with mock.patch.object(hud_render.fcntl, "ioctl", return_value=packed):
            self.assertEqual(
                hud_render.terminal_geometry(1),
                {
                    "rows": 8,
                    "columns": 120,
                    "width_pixels": 1928,
                    "height_pixels": 272,
                },
            )

    def test_standalone_tmux_socket_can_be_isolated_for_e2e(self) -> None:
        self.assertEqual(codex_hud_cli.TMUX_SOCKET, "codex-hud")

    def test_native_terminal_mouse_is_scoped_to_macos_ghostty(self) -> None:
        with mock.patch.object(codex_hud_cli.sys, "platform", "darwin"):
            self.assertTrue(codex_hud_cli._native_terminal_mouse({"TERM_PROGRAM": "ghostty"}))
            self.assertFalse(codex_hud_cli._native_terminal_mouse({"TERM_PROGRAM": "iTerm.app"}))
        with mock.patch.object(codex_hud_cli.sys, "platform", "linux"):
            self.assertFalse(codex_hud_cli._native_terminal_mouse({"TERM_PROGRAM": "ghostty"}))

    def test_termination_signal_uses_python_cleanup_path(self) -> None:
        installed: dict[int, object] = {}
        restored: list[tuple[int, object]] = []

        def set_handler(signum: int, handler: object) -> None:
            if callable(handler):
                installed[signum] = handler
            else:
                restored.append((signum, handler))

        with mock.patch.object(
            codex_hud_cli.signal, "getsignal", return_value="previous"
        ), mock.patch.object(
            codex_hud_cli.signal, "signal", side_effect=set_handler
        ):
            previous = codex_hud_cli._install_termination_handlers()
            handler = installed[codex_hud_cli.signal.SIGTERM]
            assert callable(handler)
            with self.assertRaisesRegex(SystemExit, "143"):
                handler(codex_hud_cli.signal.SIGTERM, None)
            codex_hud_cli._suspend_termination_handlers()
            codex_hud_cli._restore_termination_handlers(previous)

        self.assertIn((codex_hud_cli.signal.SIGTERM, codex_hud_cli.signal.SIG_IGN), restored)
        self.assertIn((codex_hud_cli.signal.SIGHUP, codex_hud_cli.signal.SIG_IGN), restored)
        self.assertIn((codex_hud_cli.signal.SIGTERM, "previous"), restored)
        self.assertIn((codex_hud_cli.signal.SIGHUP, "previous"), restored)

    def test_embedded_tmux_is_killed_even_when_state_cleanup_fails(self) -> None:
        split = mock.Mock(stdout="%hud\n")
        run_calls: list[list[str]] = []
        option_state: dict[str, str] = {}

        def fake_run(arguments: list[str], **_kwargs: object) -> mock.Mock:
            run_calls.append(arguments)
            if "display-message" in arguments:
                return mock.Mock(stdout="$1\n", returncode=0)
            if "show-options" in arguments:
                name = arguments[-1]
                if name == "mouse" and "-A" in arguments:
                    return mock.Mock(
                        stdout=f"{option_state.get('mouse', 'off')}\n", returncode=0
                    )
                return mock.Mock(stdout=f"{option_state.get(name, '')}\n", returncode=0)
            if "set-option" in arguments and "-u" not in arguments:
                option_state[arguments[-2]] = arguments[-1]
            if "set-option" in arguments and "-u" in arguments:
                option_state.pop(arguments[-1], None)
            if arguments[1] == "split-window":
                return split
            return mock.Mock(returncode=0)

        environment = {
            "TMUX": "socket",
            "TMUX_PANE": "%main",
            "CODEX_HUD_HOME": self.temporary.name,
            "PATH": os.environ.get("PATH", ""),
        }
        with mock.patch.object(codex_hud_cli, "enabled", return_value=True), mock.patch.object(
            codex_hud_cli, "_require"
        ), mock.patch.object(codex_hud_cli.os, "environ", environment), mock.patch.object(
            codex_hud_cli.subprocess, "run", side_effect=fake_run
        ), mock.patch.object(
            codex_hud_cli.subprocess, "call", return_value=0
        ) as codex_call, mock.patch.object(
            codex_hud_cli, "mark_ended", side_effect=OSError("state unavailable")
        ), mock.patch.object(
            codex_hud_cli, "remove_run", side_effect=OSError("state unavailable")
        ), mock.patch.object(
            codex_hud_cli, "_native_terminal_mouse", return_value=True
        ), mock.patch.object(
            codex_hud_cli, "_acquire_native_mouse", return_value=("$1", "holder", None)
        ) as acquire, mock.patch.object(
            codex_hud_cli, "_release_native_mouse"
        ) as release:
            self.assertEqual(codex_hud_cli.launch([]), 0)

        self.assertIn(["tmux", "kill-pane", "-t", "%hud"], run_calls)
        embedded_split = next(call for call in run_calls if "split-window" in call)
        self.assertLess(embedded_split.index("-d"), embedded_split.index("-v"))
        self.assertIn(
            ["tmux", "select-pane", "-d", "-t", "%hud"], run_calls
        )
        self.assertIn(
            [
                "tmux",
                "set-hook",
                "-p",
                "-t",
                "%hud",
                "after-select-pane",
                "select-pane -t %main",
            ],
            run_calls,
        )
        self.assertFalse(any(call[1] == "bind-key" for call in run_calls))
        codex_call.assert_called_once()
        self.assertEqual(codex_call.call_args.args[0], ["codex", "--no-alt-screen"])
        self.assertEqual(codex_call.call_args.kwargs["env"]["TMUX_PANE"], "%main")
        self.assertIn("CODEX_HUD_RUN_ID", codex_call.call_args.kwargs["env"])
        acquire.assert_called_once_with("%main")
        release.assert_called_once_with(("$1", "holder", None))

    def test_native_mouse_lease_preserves_inheritance_and_concurrency(self) -> None:
        options: dict[str, str] = {}

        def read_option(
            _session: str,
            name: str,
            effective: bool = False,
            socket_name: str | None = None,
        ) -> str:
            del socket_name
            if name == "mouse" and effective:
                return options.get(name, "on")
            return options.get(name, "")

        def set_option(
            _session: str, name: str, value: str, socket_name: str | None = None
        ) -> None:
            del socket_name
            options[name] = value

        def unset_option(
            _session: str, name: str, socket_name: str | None = None
        ) -> None:
            del socket_name
            options.pop(name, None)

        with mock.patch.object(
            tmux_mouse, "session_id", return_value="$1"
        ), mock.patch.object(
            tmux_mouse, "mouse_state_lock", side_effect=lambda *_args: nullcontext()
        ), mock.patch.object(
            tmux_mouse, "restore_legacy_mouse_bindings", return_value=False
        ), mock.patch.object(
            tmux_mouse, "session_option", side_effect=read_option
        ), mock.patch.object(
            tmux_mouse, "set_session_option", side_effect=set_option
        ), mock.patch.object(
            tmux_mouse, "unset_session_option", side_effect=unset_option
        ), mock.patch.object(
            tmux_mouse.secrets, "token_hex", side_effect=["one", "two"]
        ):
            first = tmux_mouse.acquire_native_mouse("%one")
            second = tmux_mouse.acquire_native_mouse("%two")
            self.assertEqual(options["mouse"], "off")
            self.assertEqual(len(options[tmux_mouse.MOUSE_HOLDERS_OPTION].split(",")), 2)

            tmux_mouse.release_native_mouse(first)
            self.assertEqual(options["mouse"], "off")
            self.assertEqual(len(options[tmux_mouse.MOUSE_HOLDERS_OPTION].split(",")), 1)

            tmux_mouse.release_native_mouse(second)
            self.assertNotIn("mouse", options)
            self.assertNotIn(tmux_mouse.MOUSE_HOLDERS_OPTION, options)

    def test_native_mouse_lease_wins_mixed_mode_then_restores_fallback(self) -> None:
        options: dict[str, str] = {}

        def read_option(
            _session: str,
            name: str,
            effective: bool = False,
            socket_name: str | None = None,
        ) -> str:
            del socket_name
            if name == "mouse" and effective:
                return options.get(name, "off")
            return options.get(name, "")

        with mock.patch.object(
            tmux_mouse, "session_id", return_value="$1"
        ), mock.patch.object(
            tmux_mouse, "mouse_state_lock", side_effect=lambda *_args: nullcontext()
        ), mock.patch.object(
            tmux_mouse, "restore_legacy_mouse_bindings", return_value=False
        ), mock.patch.object(
            tmux_mouse, "session_option", side_effect=read_option
        ), mock.patch.object(
            tmux_mouse,
            "set_session_option",
            side_effect=lambda _session, name, value, _socket=None: options.__setitem__(name, value),
        ), mock.patch.object(
            tmux_mouse,
            "unset_session_option",
            side_effect=lambda _session, name, _socket=None: options.pop(name, None),
        ), mock.patch.object(
            tmux_mouse.secrets, "token_hex", side_effect=["native", "fallback"]
        ):
            native = tmux_mouse.acquire_native_mouse("%native")
            fallback = tmux_mouse.acquire_tmux_mouse("%fallback")
            self.assertEqual(options["mouse"], "off")

            tmux_mouse.release_native_mouse(native)
            self.assertEqual(options["mouse"], "on")

            tmux_mouse.release_native_mouse(fallback)
            self.assertNotIn("mouse", options)

    def test_non_ghostty_fallback_still_enables_tmux_mouse(self) -> None:
        calls: list[tuple[str, str]] = []
        with mock.patch.object(
            tmux_mouse, "_acquire_mouse", side_effect=lambda _pane, _socket, value: calls.append(("mouse", value)) or ("$1", "holder", None)
        ):
            self.assertEqual(
                tmux_mouse.acquire_tmux_mouse("%main"), ("$1", "holder", None)
            )
        self.assertEqual(calls, [("mouse", "on")])

    def test_embedded_split_failure_never_acquires_mouse_lease(self) -> None:
        environment = {
            "TMUX": "socket",
            "TMUX_PANE": "%main",
            "CODEX_HUD_HOME": self.temporary.name,
            "PATH": os.environ.get("PATH", ""),
        }
        split_error = subprocess.CalledProcessError(1, ["tmux", "split-window"])
        with mock.patch.object(
            codex_hud_cli, "enabled", return_value=True
        ), mock.patch.object(
            codex_hud_cli, "_require"
        ), mock.patch.object(
            codex_hud_cli.os, "environ", environment
        ), mock.patch.object(
            codex_hud_cli, "_acquire_native_mouse"
        ) as acquire, mock.patch.object(
            codex_hud_cli, "_release_native_mouse"
        ) as release, mock.patch.object(
            codex_hud_cli.subprocess, "run", side_effect=split_error
        ), mock.patch.object(
            codex_hud_cli, "_mark_ended"
        ), mock.patch.object(
            codex_hud_cli, "_remove_run"
        ):
            with self.assertRaises(subprocess.CalledProcessError):
                codex_hud_cli.launch([])
        acquire.assert_not_called()
        release.assert_not_called()

    def test_legacy_mouse_binding_migration_restores_defaults(self) -> None:
        run_calls: list[list[str]] = []

        def current_binding(
            table: str, key: str, _socket_name: str | None = None
        ) -> str | None:
            if (table, key) == ("root", "WheelUpPane"):
                return 'if-shell -F "#{@is_hud}" "" "send-keys -M"'
            if (table, key) == ("root", "WheelDownPane"):
                return 'if-shell -F "#{@hud_main_pane}" "" "send-keys -M"'
            return None

        def defaults(table: str) -> dict[str, str]:
            if table == "root":
                return {"WheelUpPane": r"select-pane \; copy-mode -e"}
            return {}

        def fake_run(arguments: list[str], **_kwargs: object) -> mock.Mock:
            run_calls.append(arguments)
            return mock.Mock(returncode=0)

        with mock.patch.object(
            tmux_mouse, "list_key_command", side_effect=current_binding
        ), mock.patch.object(
            tmux_mouse, "default_key_commands", side_effect=defaults
        ), mock.patch.object(
            tmux_mouse.subprocess, "run", side_effect=fake_run
        ):
            self.assertTrue(tmux_mouse.restore_legacy_mouse_bindings())

        self.assertIn(
            [
                "tmux",
                "bind-key",
                "-T",
                "root",
                "WheelUpPane",
                "select-pane ; copy-mode -e",
            ],
            run_calls,
        )
        self.assertIn(
            ["tmux", "unbind-key", "-T", "root", "WheelDownPane"], run_calls
        )

    def test_detach_keeps_fresh_tmux_session_alive(self) -> None:
        run_calls: list[list[str]] = []

        def fake_run(arguments: list[str], **_kwargs: object) -> mock.Mock:
            run_calls.append(arguments)
            if "new-session" in arguments:
                return mock.Mock(stdout="%main\n", returncode=0)
            if "split-window" in arguments:
                return mock.Mock(stdout="%hud\n", returncode=0)
            if "has-session" in arguments:
                return mock.Mock(returncode=0)
            return mock.Mock(returncode=0, stdout="")

        environment = {
            "CODEX_HUD_HOME": self.temporary.name,
            "PATH": os.environ.get("PATH", ""),
        }
        with mock.patch.object(codex_hud_cli, "enabled", return_value=True), mock.patch.object(
            codex_hud_cli, "_require"
        ), mock.patch.object(codex_hud_cli.os, "environ", environment), mock.patch.object(
            codex_hud_cli.os, "getcwd", return_value=self.temporary.name
        ), mock.patch.object(
            codex_hud_cli.subprocess, "run", side_effect=fake_run
        ), mock.patch.object(
            codex_hud_cli.subprocess, "call", return_value=0
        ), mock.patch.object(
            codex_hud_cli, "_native_terminal_mouse", return_value=True
        ):
            self.assertEqual(codex_hud_cli.launch([]), 0)

        self.assertFalse(any("kill-session" in call for call in run_calls))
        self.assertIn(
            ["tmux", "-L", "codex-hud", "select-pane", "-d", "-t", "%hud"],
            run_calls,
        )
        self.assertIn(
            [
                "tmux",
                "-L",
                "codex-hud",
                "set-hook",
                "-p",
                "-t",
                "%hud",
                "after-select-pane",
                "select-pane -t %main",
            ],
            run_calls,
        )
        self.assertFalse(any("bind-key" in call for call in run_calls))
        create_call = next(call for call in run_calls if "new-session" in call)
        self.assertEqual(create_call[:3], ["tmux", "-L", "codex-hud"])
        self.assertIn("/dev/null", create_call)
        self.assertIn("default-terminal", create_call)
        self.assertIn("tmux-256color", create_call)
        split_call = next(call for call in run_calls if "split-window" in call)
        self.assertLess(split_call.index("-d"), split_call.index("-v"))
        self.assertIn(
            [
                "tmux",
                "-L",
                "codex-hud",
                "set-option",
                "-t",
                mock.ANY,
                "mouse",
                "off",
            ],
            run_calls,
        )
        ready_files = list((Path(self.temporary.name) / "runs").glob(".*.ready"))
        detached_files = list((Path(self.temporary.name) / "runs").glob(".*.detached"))
        self.assertEqual(len(ready_files), 1)
        self.assertEqual(len(detached_files), 1)

    def test_standalone_fallback_mouse_survives_detach_and_guards_hud(self) -> None:
        run_calls: list[list[str]] = []

        def fake_run(arguments: list[str], **_kwargs: object) -> mock.Mock:
            run_calls.append(arguments)
            if "new-session" in arguments:
                return mock.Mock(stdout="%main\n", returncode=0)
            if "split-window" in arguments:
                return mock.Mock(stdout="%hud\n", returncode=0)
            if "has-session" in arguments:
                return mock.Mock(returncode=0)
            return mock.Mock(returncode=0, stdout="")

        environment = {
            "CODEX_HUD_HOME": self.temporary.name,
            "PATH": os.environ.get("PATH", ""),
        }
        with mock.patch.object(
            codex_hud_cli, "enabled", return_value=True
        ), mock.patch.object(
            codex_hud_cli, "_require"
        ), mock.patch.object(
            codex_hud_cli.os, "environ", environment
        ), mock.patch.object(
            codex_hud_cli.os, "getcwd", return_value=self.temporary.name
        ), mock.patch.object(
            codex_hud_cli.subprocess, "run", side_effect=fake_run
        ), mock.patch.object(
            codex_hud_cli.subprocess, "call", return_value=0
        ), mock.patch.object(
            codex_hud_cli, "_native_terminal_mouse", return_value=False
        ):
            self.assertEqual(codex_hud_cli.launch([]), 0)

        self.assertIn(
            [
                "tmux", "-L", "codex-hud", "set-option", "-t",
                mock.ANY, "mouse", "on",
            ],
            run_calls,
        )
        self.assertIn(
            [
                "tmux", "-L", "codex-hud", "set-option", "-p", "-t",
                "%hud", "@is_hud", "1",
            ],
            run_calls,
        )
        self.assertIn(
            [
                "tmux", "-L", "codex-hud", "set-hook", "-p", "-t",
                "%hud", "after-copy-mode",
                'if-shell -F "#{@is_hud}" "copy-mode -q"',
            ],
            run_calls,
        )


    def test_tools_active_sorted_by_recent(self) -> None:
        """Active tools should be displayed with the most recently started first."""
        state = hud_state.default_state(self.run_id)
        state.update(
            {
                "cwd": self.temporary.name,
                "active_tools": {
                    "old": {"name": "Bash", "started_at": 100},
                    "mid": {"name": "Read", "started_at": 200},
                    "new": {"name": "Write", "started_at": 300},
                },
                "tool_stats": {},
            }
        )
        scanner = hud_render.TranscriptScanner()
        lines = hud_render.render_lines(state, scanner, {}, {})
        self.assertEqual(len(lines), 8)
        tools_line = lines[3]  # L4: TOOLS active
        # "Write" (most recent) should appear before "Bash" (oldest).
        self.assertIn("Write", tools_line)
        write_pos = tools_line.index("Write")
        bash_pos = tools_line.index("Bash")
        self.assertLess(write_pos, bash_pos)

    def test_explicit_zero_baseline_preserves_raw_values(self) -> None:
        """With baseline=0, used/window match the raw transcript values."""
        usage = metrics.official_context_usage(134_772, 258_400, baseline_tokens=0)
        self.assertIsNotNone(usage)
        assert usage is not None
        self.assertEqual(usage.used_tokens, 134_772)
        self.assertEqual(usage.window_tokens, 258_400)
        # 134772/258400 ≈ 52.16%, rounded remaining = 48, used = 52
        self.assertEqual(usage.used_percent, 52)
        self.assertEqual(usage.remaining_percent, 48)


    def test_spawn_hook_captures_effort_without_prompt_body(self) -> None:
        hud_hook.handle_event(
            {
                "hook_event_name": "SessionStart",
                "session_id": "thread-1",
                "source": "startup",
            },
            self.run_id,
        )
        hud_hook.handle_event(
            {
                "hook_event_name": "PreToolUse",
                "session_id": "thread-1",
                "tool_name": "spawn_agent",
                "tool_use_id": "spawn-1",
                "tool_input": {
                    "task_name": "review",
                    "model": "gpt-5.6-sol",
                    "effort": "high",
                    "message": "secret prompt must not be stored",
                },
            },
            self.run_id,
        )
        hud_hook.handle_event(
            {
                "hook_event_name": "SubagentStart",
                "session_id": "thread-1",
                "agent_id": "agent-1",
                "agent_type": "reviewer",
            },
            self.run_id,
        )

        agent = hud_state.load_state(self.run_id)["agents"]["agent-1"]
        self.assertEqual(agent["effort"], "high")
        self.assertNotIn("secret prompt", json.dumps(hud_state.load_state(self.run_id)))

    def test_subagent_start_captures_reasoning_effort_alias(self) -> None:
        hud_hook.handle_event(
            {
                "hook_event_name": "SessionStart",
                "session_id": "thread-1",
                "source": "startup",
            },
            self.run_id,
        )
        hud_hook.handle_event(
            {
                "hook_event_name": "SubagentStart",
                "session_id": "thread-1",
                "agent_id": "agent-1",
                "agent_type": "explorer",
                "reasoning_effort": "medium",
            },
            self.run_id,
        )

        agent = hud_state.load_state(self.run_id)["agents"]["agent-1"]
        self.assertEqual(agent["effort"], "medium")

    def test_transcript_extracts_agent_effort(self) -> None:
        transcript = Path(self.temporary.name) / "effort.jsonl"
        transcript.write_text(
            "".join(
                json.dumps(record) + "\n"
                for record in [
                    {
                        "timestamp": "2026-08-08T10:00:00Z",
                        "type": "response_item",
                        "payload": {
                            "type": "function_call",
                            "name": "spawn_agent",
                            "call_id": "call-1",
                            "arguments": json.dumps(
                                {
                                    "task_name": "audit",
                                    "model": "gpt-5.6-sol",
                                    "effort": "high",
                                }
                            ),
                        },
                    },
                    {
                        "timestamp": "2026-08-08T10:00:01Z",
                        "type": "response_item",
                        "payload": {
                            "type": "function_call_output",
                            "call_id": "call-1",
                            "output": json.dumps({"task_name": "/root/audit"}),
                        },
                    },
                ]
            ),
            encoding="utf-8",
        )

        scanner = hud_render.TranscriptScanner()
        scanner.scan(str(transcript))

        self.assertEqual(scanner.agent_metadata["/root/audit"]["effort"], "high")

    def test_render_agents_expands_one_row_per_running_agent(self) -> None:
        state = hud_state.default_state(self.run_id)
        state["agents"] = {
            "a": {
                "type": "reviewer",
                "status": "running",
                "started_at": time.time() - 10,
                "model": "gpt-5.6-sol",
                "effort": "high",
                "description": "review PR",
            },
            "b": {
                "type": "explorer",
                "status": "running",
                "started_at": time.time() - 5,
                "model": "gpt-5.6-terra",
                "effort": "low",
                "description": "map repo",
            },
        }

        lines = hud_render.render_lines(
            state, hud_render.TranscriptScanner(), {}, {}
        )

        self.assertIn("AGENTS 2 running", lines[5])
        self.assertIn("explorer [gpt-5.6-terra · low]", lines[6])
        self.assertIn("reviewer [gpt-5.6-sol · high]", lines[7])

    def test_desired_hud_lines_grows_with_running_agents(self) -> None:
        state = hud_state.default_state(self.run_id)
        self.assertEqual(hud_state.desired_hud_lines(state), 8)
        state["agents"] = {
            str(index): {"status": "running", "started_at": index}
            for index in range(3)
        }
        self.assertEqual(hud_state.desired_hud_lines(state), 11)
        state["agents"] = {
            str(index): {"status": "running", "started_at": index}
            for index in range(20)
        }
        self.assertEqual(hud_state.desired_hud_lines(state), hud_state.HUD_MAX_LINES)

    def test_theme_overrides_from_config(self) -> None:
        self.assertEqual(hud_state.load_theme()["section_ctx"], "blue")
        config = hud_state.config_path()
        config.write_text(
            json.dumps(
                {
                    "enabled": True,
                    "theme": {"section_ctx": "red", "effort_high": "magenta"},
                }
            ),
            encoding="utf-8",
        )
        theme = hud_state.load_theme()
        self.assertEqual(theme["section_ctx"], "red")
        self.assertEqual(theme["effort_high"], "magenta")
        self.assertEqual(theme["section_hud"], "cyan")

        config.write_text(
            json.dumps({"enabled": True, "theme": {"section_ctx": "not-a-color"}}),
            encoding="utf-8",
        )
        self.assertEqual(hud_state.load_theme()["section_ctx"], "blue")

    def test_color_uses_theme_slot_and_honors_no_color(self) -> None:
        hud_render.refresh_theme()
        os.environ.pop("NO_COLOR", None)
        try:
            self.assertIn("\x1b[34m", hud_render.color("section_ctx", "X"))
            self.assertIn("\x1b[31m", hud_render.color("effort_max", "X"))
        finally:
            os.environ["NO_COLOR"] = "1"
        self.assertEqual(hud_render.color("section_ctx", "X"), "X")

    def test_effort_slot_maps_levels(self) -> None:
        self.assertEqual(hud_render.effort_slot("low"), "effort_low")
        self.assertEqual(hud_render.effort_slot("high"), "effort_high")
        self.assertEqual(hud_render.effort_slot("max"), "effort_max")
        self.assertEqual(hud_render.effort_slot("unrecognized"), "effort_medium")


if __name__ == "__main__":
    unittest.main()
