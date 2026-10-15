from __future__ import annotations

import importlib.machinery
import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
loader = importlib.machinery.SourceFileLoader("hud_daemon_cli", str(SCRIPTS / "codex-hud"))
spec = importlib.util.spec_from_loader(loader.name, loader)
assert spec is not None
cli = importlib.util.module_from_spec(spec)
loader.exec_module(cli)


class LauncherDaemonTest(unittest.TestCase):
    def test_capability_uses_installed_cli_help(self) -> None:
        for output, expected in (("Options:\n  --no-daemon\n", True), ("--no-alt-screen\n", False)):
            with self.subTest(output=output), mock.patch.object(
                cli.subprocess, "run", return_value=mock.Mock(returncode=0, stdout=output)
            ) as run:
                self.assertEqual(cli._codex_supports_no_daemon(), expected)
                self.assertEqual(run.call_args.args[0], ["codex", "--help"])
                self.assertEqual(run.call_args.kwargs["timeout"], 3.0)

    def test_failed_capability_probe_keeps_older_cli_compatible(self) -> None:
        for error in (OSError("missing"), subprocess.TimeoutExpired("codex", 3)):
            with self.subTest(error=error), mock.patch.object(cli.subprocess, "run", side_effect=error):
                self.assertFalse(cli._codex_supports_no_daemon())
        with mock.patch.object(cli.subprocess, "run", return_value=mock.Mock(returncode=1, stdout="--no-daemon")):
            self.assertFalse(cli._codex_supports_no_daemon())

    def test_modern_arguments_keep_hooks_in_the_launch_environment(self) -> None:
        self.assertEqual(
            cli._codex_arguments(["resume", "--last"], no_daemon=True),
            ["--no-alt-screen", "--no-daemon", "resume", "--last"],
        )
        arguments = ["--no-alt-screen", "--no-daemon", "resume", "--last"]
        self.assertEqual(cli._codex_arguments(arguments, no_daemon=True), arguments)
        self.assertEqual(cli._codex_arguments(["--", "--no-daemon"], no_daemon=True),
                         ["--no-alt-screen", "--no-daemon", "--", "--no-daemon"])

    def test_native_route_receives_daemon_isolation(self) -> None:
        with mock.patch.object(cli, "_require"), mock.patch.object(cli, "enabled", return_value=True), \
             mock.patch.object(cli, "_codex_supports_no_daemon", return_value=True), \
             mock.patch.object(cli, "_native_ghostty_hud", return_value=True), \
             mock.patch.object(cli, "_launch_native_ghostty", return_value=0) as launch:
            self.assertEqual(cli.launch([]), 0)
        self.assertEqual(launch.call_args.args[0], ["--no-alt-screen", "--no-daemon"])

    def test_remote_rejected_before_creating_a_surface(self) -> None:
        for arguments in (["--remote", "unix://"], ["--remote=ws://localhost:8080"]):
            with self.subTest(arguments=arguments), mock.patch.object(cli, "_require"), \
                 mock.patch.object(cli, "enabled", return_value=True), \
                 mock.patch.object(cli, "_native_ghostty_hud") as surface:
                with self.assertRaisesRegex(SystemExit, "本地 Hook"):
                    cli.launch(arguments)
                surface.assert_not_called()

    def test_hud_off_preserves_remote_arguments(self) -> None:
        arguments = ["--remote", "unix://"]
        with mock.patch.object(cli, "_require"), mock.patch.object(cli, "enabled", return_value=False), \
             mock.patch.object(cli.subprocess, "call", return_value=0) as call:
            self.assertEqual(cli.launch(arguments), 0)
        call.assert_called_once_with(["codex", *arguments])

    def test_remote_text_after_separator_is_a_prompt(self) -> None:
        with mock.patch.object(cli, "_require"), mock.patch.object(cli, "enabled", return_value=True), \
             mock.patch.object(cli, "_codex_supports_no_daemon", return_value=True), \
             mock.patch.object(cli, "_native_ghostty_hud", return_value=True), \
             mock.patch.object(cli, "_launch_native_ghostty", return_value=0):
            self.assertEqual(cli.launch(["--", "--remote"]), 0)


if __name__ == "__main__":
    unittest.main()
