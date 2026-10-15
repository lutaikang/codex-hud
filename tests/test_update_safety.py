from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class UpdateSafetyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="codex-hud-update-safety-")
        self.addCleanup(self.temporary.cleanup)
        self.fixture = Path(self.temporary.name)
        self.repo = self.fixture / "repo"
        self.scripts = self.repo / "scripts"
        self.scripts.mkdir(parents=True)
        for name in ("update.sh", "cachebuster.py", "check_update_idle.py"):
            source = ROOT / "scripts" / name
            if source.exists():
                shutil.copy2(source, self.scripts / name)
        self.manifest = self.repo / "plugins/codex-hud/.codex-plugin/plugin.json"
        self.manifest.parent.mkdir(parents=True)
        self.original_manifest = b'{"name":"codex-hud","version":"0.3.17+codex.20000101000000"}\n'
        self.manifest.write_bytes(self.original_manifest)
        self.codex_home = self.fixture / "codex-home"
        self.codex_home.mkdir()
        self.config = self.codex_home / "config.toml"
        self.original_config = b'[plugins."codex-hud@codex-hud-local"]\nenabled = true\n'
        self.config.write_bytes(self.original_config)
        self.bin_dir = self.fixture / "bin"
        self.bin_dir.mkdir()
        self.trace = self.fixture / "trace"
        self.ps_output = self.fixture / "ps-output"
        self.ps_output.write_text("  100 1 /sbin/launchd\n  200 100 /bin/zsh\n", encoding="utf-8")
        self._executable(
            self.bin_dir / "ps",
            '#!/bin/sh\nprintf "ps %s\\n" "$*" >> "$HUD_TEST_TRACE"\n'
            'if [ "${HUD_TEST_PS_FAIL:-0}" = 1 ]; then exit 1; fi\n'
            'cat "$HUD_TEST_PS_OUTPUT"\n',
        )
        self._executable(
            self.bin_dir / "codex",
            '#!/bin/sh\nprintf "codex %s\\n" "$*" >> "$HUD_TEST_TRACE"\n',
        )
        self._executable(
            self.scripts / "verify.sh",
            '#!/bin/sh\nprintf "verify\\n" >> "$HUD_TEST_TRACE"\n',
        )
        self.env = dict(os.environ)
        for name in ("CODEX_HUD_RUN_ID", "CODEX_THREAD_ID"):
            self.env.pop(name, None)
        self.env.update(
            PATH=f"{self.bin_dir}{os.pathsep}{self.env['PATH']}",
            CODEX_HOME=str(self.codex_home),
            HUD_TEST_TRACE=str(self.trace),
            HUD_TEST_PS_OUTPUT=str(self.ps_output),
        )

    @staticmethod
    def _executable(path: Path, content: str) -> None:
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)

    def _update(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(self.scripts / "update.sh")],
            cwd=self.repo,
            env=self.env,
            text=True,
            capture_output=True,
            timeout=10,
        )

    def _assert_unchanged(self) -> None:
        self.assertEqual(self.manifest.read_bytes(), self.original_manifest)
        self.assertEqual(self.config.read_bytes(), self.original_config)
        trace = self.trace.read_text(encoding="utf-8") if self.trace.exists() else ""
        self.assertNotIn("verify", trace)
        self.assertNotIn("codex ", trace)

    def test_plain_codex_task_refused_before_process_check_or_writes(self) -> None:
        self.env["CODEX_THREAD_ID"] = "ordinary-task"
        result = self._update()
        self.assertEqual(result.returncode, 2, result.stderr)
        self._assert_unchanged()
        self.assertFalse(self.trace.exists())

    def test_hud_task_refused_before_process_check_or_writes(self) -> None:
        self.env["CODEX_HUD_RUN_ID"] = "hud-task"
        result = self._update()
        self.assertEqual(result.returncode, 2, result.stderr)
        self._assert_unchanged()
        self.assertFalse(self.trace.exists())

    def test_external_update_with_codex_processes_refused(self) -> None:
        processes = (
            "/opt/homebrew/bin/codex",
            "/Applications/Codex.app/Contents/MacOS/Codex",
            "/Applications/Codex.app/Contents/Resources/code-mode-host",
            "codex-app-server",
            "/Users/example/.codex/packages/app-server-daemon/releases/fixture/bin/codex-code-mode-host",
            "codex-code-mode",
            "/Applications/Codex.app/Contents/Frameworks/Codex Helper (Renderer).app/Contents/MacOS/Codex Helper (Renderer)",
        )
        for process in processes:
            with self.subTest(process=process):
                self.ps_output.write_text(f"  300 1 {process}\n", encoding="utf-8")
                result = self._update()
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("300", result.stderr)
                self.assertNotIn("/Users/example", result.stderr)
                self._assert_unchanged()

    def test_failed_process_inspection_refused(self) -> None:
        self.env["HUD_TEST_PS_FAIL"] = "1"
        result = self._update()
        self.assertEqual(result.returncode, 2, result.stderr)
        self._assert_unchanged()

    def test_malformed_or_empty_process_output_refused(self) -> None:
        for output in ("", "not a process table\n", "300 missing command\n"):
            with self.subTest(output=output):
                self.ps_output.write_text(output, encoding="utf-8")
                result = self._update()
                self.assertEqual(result.returncode, 2, result.stderr)
                self._assert_unchanged()

    def test_idle_external_fixture_keeps_normal_update_sequence(self) -> None:
        self.ps_output.write_text("100 1 /sbin/launchd\n200 100 /bin/zsh\n300 200 /tmp/codex-hud-monitor\n", encoding="utf-8")
        result = self._update()
        self.assertEqual(result.returncode, 0, result.stderr)
        version = json.loads(self.manifest.read_text(encoding="utf-8"))["version"]
        self.assertRegex(version, r"^0\.3\.17\+codex\.\d{14}$")
        self.assertNotEqual(self.manifest.read_bytes(), self.original_manifest)
        self.assertEqual(self.config.read_bytes(), self.original_config)
        lines = self.trace.read_text(encoding="utf-8").splitlines()
        self.assertTrue(lines[0].startswith("ps "), lines)
        self.assertEqual(lines[1:], ["verify", "codex plugin add codex-hud@codex-hud-local"])


if __name__ == "__main__":
    unittest.main()
