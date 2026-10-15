from __future__ import annotations

import json
import io
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import hud_render


class AccountLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.bin_directory = self.directory / "bin"
        self.bin_directory.mkdir()
        self.record = self.directory / "app-server.json"
        self.fake_codex = self.bin_directory / "codex"
        self.fake_codex.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys, time\n"
            "from pathlib import Path\n"
            "Path(os.environ['HUD_TEST_CHILD_RECORD']).write_text(json.dumps({\n"
            "  'pid': os.getpid(), 'ppid': os.getppid(), 'argv': sys.argv\n"
            "}))\n"
            "while True: time.sleep(1)\n",
            encoding="utf-8",
        )
        self.fake_codex.chmod(0o700)
        self.environment = os.environ.copy()
        self.environment.update(
            CODEX_HUD_HOME=str(self.directory),
            HUD_TEST_CHILD_RECORD=str(self.record),
            PATH=str(self.bin_directory) + os.pathsep + self.environment.get("PATH", ""),
            PYTHONDONTWRITEBYTECODE="1",
        )
        self.config = self.directory / "config.json"
        self.config.write_text('{"enabled": true}', encoding="utf-8")
        self.child_pid: int | None = None

    def tearDown(self) -> None:
        if self.child_pid is not None:
            identity = self._identity(self.child_pid)
            if identity and str(self.fake_codex) in identity:
                os.kill(self.child_pid, signal.SIGTERM)
                self._wait_until(lambda: not self._identity(self.child_pid), timeout=2)
        self.temporary.cleanup()

    @staticmethod
    def _identity(pid: int) -> str:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "pid=", "-o", "ppid=", "-o", "command="],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.stdout.strip()

    @staticmethod
    def _wait_until(predicate: object, timeout: float = 3) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return bool(predicate())

    def _wait_for_child(self) -> int:
        self.assertTrue(self._wait_until(self.record.exists), "app-server did not start")
        value = json.loads(self.record.read_text(encoding="utf-8"))
        self.child_pid = int(value["pid"])
        self.assertIn(str(self.fake_codex), self._identity(self.child_pid))
        return self.child_pid

    def _renderer_exit_reaps_child(self, signum: int | None) -> None:
        renderer = subprocess.Popen(
            [sys.executable, str(SCRIPTS / "hud_render.py"), "--run-id", "lifecycle-test"],
            env=self.environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            pid = self._wait_for_child()
            if signum is None:
                self.config.write_text('{"enabled": false}', encoding="utf-8")
            else:
                renderer.send_signal(signum)
            exit_code = renderer.wait(timeout=5)
            self.assertEqual(exit_code, 0 if signum is None else 128 + signum)
            self.assertFalse(
                self._identity(pid),
                "renderer exited while its owned app-server remained alive",
            )
        finally:
            if renderer.poll() is None:
                renderer.terminate()
            try:
                renderer.wait(timeout=2)
            except subprocess.TimeoutExpired:
                renderer.kill()
                renderer.wait(timeout=2)

    def test_hud_off_reaps_blocked_app_server(self) -> None:
        self._renderer_exit_reaps_child(None)

    def test_renderer_sigterm_reaps_blocked_app_server(self) -> None:
        self._renderer_exit_reaps_child(signal.SIGTERM)

    def test_renderer_sighup_reaps_blocked_app_server(self) -> None:
        self._renderer_exit_reaps_child(signal.SIGHUP)

    def test_poller_stop_cancels_blocked_account_read(self) -> None:
        with mock.patch.dict(os.environ, self.environment, clear=True):
            poller = hud_render.AccountPoller()
            try:
                poller.start()
                pid = self._wait_for_child()
                started = time.monotonic()
                poller.stop()
                self.assertLess(time.monotonic() - started, 2)
                self.assertFalse(
                    self._identity(pid),
                    "poller.stop returned while its owned app-server remained alive",
                )
            finally:
                poller.stop()

    def test_account_reader_reaps_after_forced_kill(self) -> None:
        process = mock.Mock()
        process.stdin = mock.Mock()
        process.stdout = iter(())
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired("codex", 1), 0]
        with mock.patch.object(hud_render.subprocess, "Popen", return_value=process):
            self.assertEqual(hud_render.AccountReader().read(timeout=0.05), ({}, {}))
        process.terminate.assert_called_once()
        process.kill.assert_called_once()
        self.assertEqual(process.wait.call_count, 2)

    def test_cancel_during_process_creation_reaps_registered_child(self) -> None:
        real_popen = subprocess.Popen
        release_spawn = threading.Event()
        cancel_started = threading.Event()
        errors: list[BaseException] = []
        reader = hud_render.AccountReader()

        def pending_spawn(*arguments: object, **keywords: object) -> object:
            process = real_popen(*arguments, **keywords)
            command = arguments[0]
            if isinstance(command, (list, tuple)) and command[0] == "codex":
                release_spawn.wait(timeout=3)
            return process

        def read() -> None:
            try:
                reader.read(timeout=30)
            except BaseException as error:
                errors.append(error)

        def cancel() -> None:
            cancel_started.set()
            try:
                reader.cancel()
            except BaseException as error:
                errors.append(error)

        with mock.patch.dict(os.environ, self.environment, clear=True), mock.patch.object(
            hud_render.subprocess, "Popen", side_effect=pending_spawn
        ):
            read_thread = threading.Thread(target=read)
            cancel_thread = threading.Thread(target=cancel)
            try:
                read_thread.start()
                pid = self._wait_for_child()
                cancel_thread.start()
                self.assertTrue(cancel_started.wait(timeout=1))
                self.assertTrue(reader._cancelled.wait(timeout=1))
                release_spawn.set()
                cancel_thread.join(timeout=2)
                read_thread.join(timeout=2)
                self.assertFalse(cancel_thread.is_alive())
                self.assertFalse(read_thread.is_alive())
                self.assertEqual(errors, [])
                self.assertFalse(self._identity(pid))
            finally:
                release_spawn.set()
                reader.cancel()
                read_thread.join(timeout=3)
                if cancel_thread.ident is not None:
                    cancel_thread.join(timeout=3)

    def test_cancelled_reader_never_starts_another_process(self) -> None:
        reader = hud_render.AccountReader()
        reader.cancel()
        with mock.patch.object(hud_render.subprocess, "Popen") as popen:
            self.assertEqual(reader.read(), ({}, {}))
        popen.assert_not_called()

    def test_reader_does_not_close_blocked_stdout_from_another_thread(self) -> None:
        release_stdout = threading.Event()
        stdout_started = threading.Event()
        stdout_closed = threading.Event()
        stdout_lock = threading.Lock()

        class BlockedOutput:
            def __iter__(self) -> BlockedOutput:
                return self

            def __next__(self) -> str:
                with stdout_lock:
                    stdout_started.set()
                    release_stdout.wait(timeout=3)
                    raise StopIteration

            def close(self) -> None:
                with stdout_lock:
                    stdout_closed.set()

        process = mock.Mock()
        process.stdin = io.StringIO()
        process.stdout = BlockedOutput()
        process.poll.return_value = None
        process.wait.return_value = 0
        result: list[object] = []
        reader = hud_render.AccountReader()
        with mock.patch.object(hud_render.subprocess, "Popen", return_value=process):
            read_thread = threading.Thread(target=lambda: result.append(reader.read(timeout=0.05)))
            try:
                read_thread.start()
                self.assertTrue(stdout_started.wait(timeout=1))
                read_thread.join(timeout=0.7)
                self.assertFalse(
                    read_thread.is_alive(),
                    "finished read blocked closing stdout owned by its reader thread",
                )
                self.assertEqual(result, [({}, {})])
            finally:
                release_stdout.set()
                read_thread.join(timeout=2)
        self.assertTrue(stdout_closed.wait(timeout=1))


if __name__ == "__main__":
    unittest.main()
