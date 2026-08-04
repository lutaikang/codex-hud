from __future__ import annotations

import json
import io
import importlib.machinery
import importlib.util
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import hud_hook
import hud_render
import hud_state
import metrics


CLI_LOADER = importlib.machinery.SourceFileLoader("codex_hud_cli", str(SCRIPTS / "codex-hud"))
CLI_SPEC = importlib.util.spec_from_loader(CLI_LOADER.name, CLI_LOADER)
assert CLI_SPEC is not None
codex_hud_cli = importlib.util.module_from_spec(CLI_SPEC)
CLI_LOADER.exec_module(codex_hud_cli)


class HudTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
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
        self.assertEqual(len(lines), 8)
        for section in ("acct:me/plus", "CTX", "CACHE", "TOOLS", "AGENTS", "SKILLS", "TODO"):
            self.assertIn(section, output)

    def test_context_matches_codex_0146_native_tui(self) -> None:
        usage = metrics.official_context_usage(122_880, 258_400)
        self.assertIsNotNone(usage)
        assert usage is not None
        self.assertEqual(usage.used_tokens, 122_880)
        self.assertEqual(usage.window_tokens, 258_400)
        self.assertEqual(usage.used_percent, 48)
        self.assertEqual(usage.remaining_percent, 52)

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

    def test_embedded_tmux_is_killed_even_when_state_cleanup_fails(self) -> None:
        split = mock.Mock(stdout="%hud\n")
        run_calls: list[list[str]] = []

        def fake_run(arguments: list[str], **_kwargs: object) -> mock.Mock:
            run_calls.append(arguments)
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
        ), mock.patch.object(
            codex_hud_cli, "mark_ended", side_effect=OSError("state unavailable")
        ), mock.patch.object(
            codex_hud_cli, "remove_run", side_effect=OSError("state unavailable")
        ):
            self.assertEqual(codex_hud_cli.launch([]), 0)

        self.assertIn(["tmux", "kill-pane", "-t", "%hud"], run_calls)

    def test_detach_keeps_fresh_tmux_session_alive(self) -> None:
        run_calls: list[list[str]] = []

        def fake_run(arguments: list[str], **_kwargs: object) -> mock.Mock:
            run_calls.append(arguments)
            if arguments[1] == "new-session":
                return mock.Mock(stdout="%main\n", returncode=0)
            if arguments[1] == "has-session":
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
        ):
            self.assertEqual(codex_hud_cli.launch([]), 0)

        self.assertFalse(any(call[1] == "kill-session" for call in run_calls))
        ready_files = list((Path(self.temporary.name) / "runs").glob(".*.ready"))
        detached_files = list((Path(self.temporary.name) / "runs").glob(".*.detached"))
        self.assertEqual(len(ready_files), 1)
        self.assertEqual(len(detached_files), 1)


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

    def test_context_no_baseline_parity(self) -> None:
        """With baseline=0, used/window match the raw transcript values."""
        usage = metrics.official_context_usage(134_772, 258_400)
        self.assertIsNotNone(usage)
        assert usage is not None
        self.assertEqual(usage.used_tokens, 134_772)
        self.assertEqual(usage.window_tokens, 258_400)
        # 134772/258400 ≈ 52.16%, rounded remaining = 48, used = 52
        self.assertEqual(usage.used_percent, 52)
        self.assertEqual(usage.remaining_percent, 48)


if __name__ == "__main__":
    unittest.main()
