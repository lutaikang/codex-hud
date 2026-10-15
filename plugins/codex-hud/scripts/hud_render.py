#!/usr/bin/env python3
"""Render the active Codex session into a small HUD surface."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import queue
import re
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any

from hud_state import (
    BASE_COLORS,
    DEFAULT_THEME,
    HUD_DETAIL_ROWS,
    HUD_MAX_LINES,
    HUD_MIN_LINES,
    HUD_VERSION,
    desired_hud_lines,
    enabled,
    load_state,
    load_theme,
    plan_detail_items,
    plan_projection,
    recent_completed_items,
    running_agent_count,
    sanitize_display_text,
    todo_detail_rows,
)
from metrics import estimate_chatgpt_credits, official_context_usage, session_risk


ESC = "\033["
RESET = f"{ESC}0m"
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

_theme: dict[str, str] = dict(DEFAULT_THEME)


def refresh_theme() -> None:
    """Reload config.json theme overrides at the start of each render frame."""
    global _theme
    _theme = load_theme()
GIT_CACHE_SECONDS = 5.0
_GIT_CACHE: dict[str, tuple[float, str]] = {}
ACCOUNT_REFRESH_SECONDS = 60.0
ACCOUNT_READ_TIMEOUT = 4.0

PRICES_PER_MILLION = {
    "gpt-5.6-sol": (5.0, 0.5, 30.0),
    "gpt-5.6": (5.0, 0.5, 30.0),
    "gpt-5.6-terra": (2.5, 0.25, 15.0),
    "gpt-5.6-luna": (1.0, 0.1, 6.0),
}


def terminal_geometry(file_descriptor: int) -> dict[str, int]:
    packed = fcntl.ioctl(file_descriptor, termios.TIOCGWINSZ, b"\0" * 8)
    rows, columns, width_pixels, height_pixels = struct.unpack("HHHH", packed)
    return {
        "rows": rows,
        "columns": columns,
        "width_pixels": width_pixels,
        "height_pixels": height_pixels,
    }


def resize_tmux_surface(target_lines: int) -> bool:
    """Resize the renderer's own tmux pane, if this HUD uses tmux fallback."""
    pane = os.environ.get("TMUX_PANE")
    if not pane:
        return True
    target = max(HUD_MIN_LINES, min(HUD_MAX_LINES, int(target_lines)))
    try:
        result = subprocess.run(
            ["tmux", "resize-pane", "-t", pane, "-y", str(target)],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return False
    return result.returncode == 0


def tmux_surface_needs_resize(target_lines: int, observed_rows: int) -> bool:
    """Compare the requested height with the pane's observed height."""
    return bool(os.environ.get("TMUX_PANE")) and int(target_lines) != int(observed_rows)


def _write_private_json(path: Path, value: dict[str, int]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary = Path(name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, separators=(",", ":"))
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _touch_private(path: Path) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)
    try:
        os.utime(path, None)
    except FileNotFoundError:
        pass


def color(slot: str, text: str) -> str:
    if os.environ.get("NO_COLOR"):
        return text
    name = _theme.get(slot, slot)
    return f"{ESC}{BASE_COLORS.get(name, name)}m{text}{RESET}"


def human_tokens(value: int | float | None) -> str:
    number = float(value or 0)
    if number >= 1_000_000:
        return f"{number / 1_000_000:.2f}".rstrip("0").rstrip(".") + "m"
    if number >= 1_000:
        return f"{number / 1_000:.1f}".rstrip("0").rstrip(".") + "k"
    return str(int(number))


def human_bytes(value: int | float | None) -> str:
    number = float(value or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if number < 1024 or unit == "GB":
            return f"{number:.1f}{unit}" if unit != "B" else f"{int(number)}B"
        number /= 1024
    return f"{int(value or 0)}B"


def human_duration(seconds: float | int | None) -> str:
    seconds = max(0, int(seconds or 0))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def _percent_slot(value: float | int | None) -> str:
    number = float(value or 0)
    if number < 65:
        return "progress_low"
    if number < 85:
        return "progress_mid"
    return "progress_high"


def progress_bar(percent: float | int | None, width: int = 10) -> str:
    value = max(0.0, min(100.0, float(percent or 0)))
    filled = round(width * value / 100)
    return color(_percent_slot(value), "█" * filled + "░" * (width - filled))


def visible_clip(text: str, width: int) -> str:
    """Clip ANSI text by terminal columns so HUD rows never wrap."""
    output: list[str] = []
    columns = 0
    index = 0
    while index < len(text) and columns < width:
        match = ANSI_RE.match(text, index)
        if match:
            output.append(match.group(0))
            index = match.end()
            continue
        character = text[index]
        character_width = 2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1
        if columns + character_width > width:
            break
        output.append(character)
        columns += character_width
        index += 1
    if not os.environ.get("NO_COLOR"):
        output.append(RESET)
    return "".join(output)


def _visible_width(text: str) -> int:
    columns = 0
    index = 0
    while index < len(text):
        match = ANSI_RE.match(text, index)
        if match:
            index = match.end()
            continue
        character = text[index]
        columns += 2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1
        index += 1
    return columns


def plan_detail_line(
    item: dict[str, Any], index: int, total: int, width: int
) -> str:
    overflow = max(0, total - 2) if index == 1 else 0
    return task_detail_line(item, index, total, width, overflow=overflow)


def task_detail_line(
    item: dict[str, Any],
    index: int,
    total: int,
    width: int,
    *,
    overflow: int = 0,
) -> str:
    status = item.get("status")
    marker = "◐" if status == "in_progress" else "?" if status == "unknown" else "✓" if status == "completed" else "○"
    marker_color = "task_completed" if status == "completed" else "task_active"
    prefix = f"       {color(marker_color, marker)} "
    suffix = f"  … +{overflow}" if overflow > 0 else ""
    available = max(1, width - _visible_width(prefix) - _visible_width(suffix))
    step = sanitize_display_text(item.get("step"), 240) or "未命名任务"
    return prefix + visible_clip(step, available) + suffix


def effort_slot(effort: Any) -> str:
    """Map a reasoning-effort value to a theme slot for coloring."""
    normalized = str(effort or "").strip().lower()
    if normalized in {"low", "minimal", "0", "1"}:
        return "effort_low"
    if normalized in {"high", "2", "3"}:
        return "effort_high"
    if normalized in {"xhigh", "extreme", "max", "4", "5"}:
        return "effort_max"
    return "effort_medium"


def _agent_detail_line(agent: dict[str, Any], started: float, now: float) -> str:
    """Render one running agent on its own line."""
    agent_type = sanitize_display_text(agent.get("type"), 40) or "agent"
    model = sanitize_display_text(agent.get("model"), 80)
    effort = sanitize_display_text(agent.get("effort"), 20)
    description = sanitize_display_text(agent.get("description"), 80)
    activity = sanitize_display_text(agent.get("current_activity"), 40)
    parts = [color("agent_running", f"◐ {agent_type}")]
    model_effort = model or ""
    if effort:
        model_effort += f" · {color(effort_slot(effort), effort)}"
    if model_effort:
        parts.append(color("model", f"[{model_effort}]"))
    if description:
        parts.append(description)
    if activity and activity not in {"working", "starting", "spawned"}:
        parts.append(color("task_active", f"· {activity}"))
    parts.append(human_duration(now - started))
    return "  " + " ".join(parts)


def fit_lines_for_geometry(
    lines: list[str],
    rows: int,
    *,
    agent_rows: int = 0,
    detail_rows: int = 0,
) -> list[str]:
    """Clip a full frame to a short surface without hiding summary rows.

    The six fixed header rows always survive. When space is scarce, the TODO
    summary and SKILLS rows are kept first, then running-agent rows, and finally
    TODO detail rows — so TODO detail rows drop before running-agent rows.
    """
    target = max(1, int(rows))
    if len(lines) <= target:
        return lines
    if target <= 6:
        return lines[:6][:target]

    skills_index = 6 + agent_rows
    todo_index = skills_index + 1
    detail_start = todo_index + 1

    priority: list[int] = []
    if todo_index < len(lines):
        priority.append(todo_index)
    if skills_index < len(lines):
        priority.append(skills_index)
    priority.extend(range(6, min(6 + agent_rows, len(lines))))
    priority.extend(range(detail_start, min(detail_start + detail_rows, len(lines))))

    kept = set(range(6))
    for index in priority:
        if len(kept) >= target:
            break
        kept.add(index)
    return [lines[index] for index in sorted(kept)]


def format_reset(timestamp: Any) -> str:
    if not isinstance(timestamp, (int, float)):
        return "--"
    delta = max(0, int(timestamp - time.time()))
    return human_duration(delta)


def format_window(minutes: Any) -> str:
    value = int(minutes or 0)
    if value >= 1440 and value % 1440 == 0:
        return f"{value // 1440}d"
    if value >= 60 and value % 60 == 0:
        return f"{value // 60}h"
    return f"{value}m" if value else "quota"


def _json_size(value: Any) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    except (TypeError, ValueError):
        return 0


def _count_image_blocks(value: Any) -> int:
    if isinstance(value, dict):
        count = 1 if value.get("type") in {"image", "input_image"} else 0
        return count + sum(_count_image_blocks(item) for item in value.values())
    if isinstance(value, list):
        return sum(_count_image_blocks(item) for item in value)
    return 0


class TranscriptScanner:
    def __init__(self) -> None:
        self.path: Path | None = None
        self.offset = 0
        self.last: dict[str, Any] = {}
        self.total: dict[str, Any] = {}
        self.context_window: int | None = None
        self.model_provider: str | None = None
        self.model: str | None = None
        self.effort: str | None = None
        self.rate_limits: dict[str, Any] = {}
        self.token_updates = 0
        self.tool_calls = 0
        self.tool_outputs = 0
        self.tool_output_bytes = 0
        self.image_blocks = 0
        self.compactions = 0
        self.agent_metadata: dict[str, dict[str, Any]] = {}
        self._pending_agent_spawns: dict[str, dict[str, Any]] = {}

    def _reset_content(self) -> None:
        self.offset = 0
        self.last = {}
        self.total = {}
        self.context_window = None
        self.model_provider = None
        self.model = None
        self.effort = None
        self.rate_limits = {}
        self.token_updates = 0
        self.tool_calls = 0
        self.tool_outputs = 0
        self.tool_output_bytes = 0
        self.image_blocks = 0
        self.compactions = 0
        self.agent_metadata = {}
        self._pending_agent_spawns = {}

    def scan(self, raw_path: str | None) -> None:
        if not raw_path:
            return
        path = Path(raw_path).expanduser()
        if path != self.path:
            self.path = path
            self._reset_content()
        try:
            size = path.stat().st_size
            if size < self.offset:
                self._reset_content()
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(self.offset)
                while True:
                    line_start = handle.tell()
                    line = handle.readline()
                    if not line:
                        break
                    if not line.endswith("\n"):
                        self.offset = line_start
                        break
                    self._consume(line)
                    self.offset = handle.tell()
        except OSError:
            return

    def _consume(self, line: str) -> None:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            return
        if not isinstance(record, dict):
            return
        payload = record.get("payload")
        if not isinstance(payload, dict):
            return
        if record.get("type") == "compacted":
            self.compactions += 1
        elif record.get("type") == "response_item":
            item_type = payload.get("type")
            if item_type in {"function_call", "custom_tool_call"}:
                self.tool_calls += 1
            elif item_type in {"function_call_output", "custom_tool_call_output"}:
                self.tool_outputs += 1
                output = payload.get("output")
                self.tool_output_bytes += _json_size(output)
                self.image_blocks += _count_image_blocks(output)
        model_provider = payload.get("model_provider")
        if isinstance(model_provider, str):
            self.model_provider = model_provider
        model = payload.get("model")
        if isinstance(model, str):
            self.model = model
        effort = payload.get("effort")
        if isinstance(effort, str):
            self.effort = effort
        self._consume_agent_metadata(record, payload)
        if record.get("type") != "event_msg" or payload.get("type") != "token_count":
            return
        info = payload.get("info")
        if isinstance(info, dict):
            last = info.get("last_token_usage")
            total = info.get("total_token_usage")
            if isinstance(last, dict):
                self.last = last
            if isinstance(total, dict):
                self.total = total
                self.token_updates += 1
            window = info.get("model_context_window")
            if isinstance(window, int):
                self.context_window = window
        rate_limits = payload.get("rate_limits")
        if isinstance(rate_limits, dict):
            self.rate_limits = rate_limits

    def _consume_agent_metadata(
        self, record: dict[str, Any], payload: dict[str, Any]
    ) -> None:
        """Extract only bounded, non-prompt agent metadata from the transcript."""
        timestamp = record.get("timestamp")
        event_type = payload.get("type")
        if event_type == "function_call" and payload.get("name") == "spawn_agent":
            arguments = payload.get("arguments")
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    arguments = {}
            if not isinstance(arguments, dict):
                return
            task_name = sanitize_display_text(arguments.get("task_name"), 80)
            if not task_name:
                return
            model = sanitize_display_text(arguments.get("model"), 80)
            effort = sanitize_display_text(
                arguments.get("effort") or arguments.get("reasoning_effort"), 20
            )
            self._pending_agent_spawns[str(payload.get("call_id") or "")] = {
                "task_name": task_name,
                "description": sanitize_display_text(
                    task_name.replace("_", " ").replace("-", " "), 120
                ),
                "model": model or None,
                "effort": effort or None,
                "updated_at": timestamp,
                "current_activity": "starting",
            }
            return

        if event_type == "function_call_output":
            call_id = str(payload.get("call_id") or "")
            pending = self._pending_agent_spawns.pop(call_id, None)
            output = payload.get("output")
            if pending is None or not isinstance(output, str):
                return
            try:
                parsed = json.loads(output)
            except json.JSONDecodeError:
                return
            if not isinstance(parsed, dict):
                return
            task_path = sanitize_display_text(parsed.get("task_name"), 120)
            if not task_path.startswith("/"):
                return
            pending["current_activity"] = "spawned"
            pending["updated_at"] = timestamp
            self.agent_metadata[task_path] = pending
            return

        if event_type == "sub_agent_activity":
            task_path = sanitize_display_text(payload.get("agent_path"), 120)
            if not task_path:
                return
            metadata = self.agent_metadata.setdefault(
                task_path,
                {
                    "task_name": task_path.rsplit("/", 1)[-1],
                    "description": sanitize_display_text(
                        task_path.rsplit("/", 1)[-1].replace("_", " "), 120
                    ),
                    "model": None,
                },
            )
            metadata["current_activity"] = sanitize_display_text(payload.get("kind"), 40) or "active"
            metadata["updated_at"] = payload.get("occurred_at_ms") or timestamp
            effort = sanitize_display_text(
                payload.get("effort") or payload.get("reasoning_effort"), 20
            )
            if effort:
                metadata["effort"] = effort


class AccountReader:
    def __init__(self) -> None:
        self._cancelled = threading.Event()
        self._process_lock = threading.Lock()
        self._cleanup_lock = threading.Lock()
        self._read_lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None

    def cancel(self) -> None:
        """Stop this reader and reap only the app-server process it created."""
        self._cancelled.set()
        with self._process_lock:
            process = self._process
        if process is not None:
            self._reap_process(process)

    def _reap_process(self, process: subprocess.Popen[str]) -> None:
        # Cancellation and read's finally can race. Serialize the full reap so
        # neither caller returns while the other still owns a live child.
        with self._cleanup_lock:
            with self._process_lock:
                if self._process is not process:
                    return
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                process.wait(timeout=1)
            with self._process_lock:
                if self._process is process:
                    self._process = None

    def read(
        self, timeout: float = ACCOUNT_READ_TIMEOUT
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        with self._read_lock:
            return self._read(timeout)

    def _read(self, timeout: float) -> tuple[dict[str, Any], dict[str, Any]]:
        with self._process_lock:
            if self._cancelled.is_set():
                return {}, {}
            try:
                process = subprocess.Popen(
                    ["codex", "app-server"],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    bufsize=1,
                )
            except OSError:
                return {}, {}
            self._process = process
        messages: queue.Queue[dict[str, Any]] = queue.Queue()

        def reader() -> None:
            assert process.stdout is not None
            try:
                for line in process.stdout:
                    try:
                        value = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(value, dict):
                        messages.put(value)
            finally:
                # Only this thread closes its stream. Another process can keep
                # an inherited stdout pipe open after the owned server exits.
                close = getattr(process.stdout, "close", None)
                if callable(close):
                    try:
                        close()
                    except OSError:
                        pass

        reader_thread = threading.Thread(target=reader, daemon=True)
        reader_thread.start()

        def send(value: dict[str, Any]) -> None:
            if self._cancelled.is_set():
                return
            assert process.stdin is not None
            process.stdin.write(json.dumps(value, separators=(",", ":")) + "\n")
            process.stdin.flush()

        deadline = time.monotonic() + max(0.05, timeout)
        account: dict[str, Any] = {}
        limits: dict[str, Any] = {}

        def receive(expected_id: int) -> dict[str, Any] | None:
            """Return the first response for an id, including JSON-RPC errors."""
            while not self._cancelled.is_set() and time.monotonic() < deadline:
                remaining = max(0.01, deadline - time.monotonic())
                try:
                    message = messages.get(timeout=min(0.05, remaining))
                except queue.Empty:
                    # A failed app-server can exit without writing a response.
                    # Do not turn that into a full timeout on every refresh.
                    if process.poll() is not None:
                        return None
                    continue
                if message.get("id") == expected_id:
                    return message
            return None

        try:
            send(
                {
                    "method": "initialize",
                    "id": 0,
                    "params": {
                        "clientInfo": {
                            "name": "codex_hud",
                            "title": "Codex HUD",
                            "version": "0.1.0",
                        }
                    },
                }
            )
            initialize = receive(0)
            if initialize is None or "error" in initialize:
                return account, limits
            send({"method": "initialized", "params": {}})
            send(
                {
                    "method": "account/read",
                    "id": 1,
                    "params": {"refreshToken": False},
                }
            )
            account_response = receive(1)
            if account_response is not None:
                result = account_response.get("result")
                if isinstance(result, dict):
                    value = result.get("account")
                    account = value if isinstance(value, dict) else {}

            # API-key sessions do not expose Codex subscription rate limits;
            # avoid issuing a request that the server will reject.  The
            # transcript remains the source of any rolling limit data.
            if str(account.get("type") or "").lower() not in {
                "apikey",
                "api_key",
            }:
                send({"method": "account/rateLimits/read", "id": 2})
                limits_response = receive(2)
                if limits_response is not None:
                    result = limits_response.get("result")
                    if isinstance(result, dict):
                        limits = result
        except (BrokenPipeError, OSError, ValueError, queue.Empty):
            pass
        finally:
            self._reap_process(process)
            reader_thread.join(timeout=0.2)
            if process.stdin:
                try:
                    process.stdin.close()
                except OSError:
                    pass
        return account, limits


class AccountPoller:
    """Refresh account metadata without delaying the first HUD frame."""

    def __init__(
        self,
        reader: AccountReader | None = None,
        refresh_seconds: float = ACCOUNT_REFRESH_SECONDS,
    ) -> None:
        self.reader = reader or AccountReader()
        self.refresh_seconds = max(1.0, float(refresh_seconds))
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._account: dict[str, Any] = {}
        self._limits: dict[str, Any] = {}
        self._loaded = False

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run,
            name="codex-hud-account-refresh",
            daemon=True,
        )
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                account, limits = self.reader.read()
            except Exception:
                account, limits = {}, {}
            with self._lock:
                self._account = account if isinstance(account, dict) else {}
                self._limits = limits if isinstance(limits, dict) else {}
                self._loaded = True
            if self._stop.wait(self.refresh_seconds):
                return

    def snapshot(self) -> tuple[dict[str, Any], dict[str, Any], bool]:
        with self._lock:
            return dict(self._account), dict(self._limits), self._loaded

    def stop(self) -> None:
        self._stop.set()
        try:
            cancel = getattr(self.reader, "cancel", None)
            if callable(cancel):
                cancel()
        finally:
            thread = self._thread
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=ACCOUNT_READ_TIMEOUT + 2)


def account_label(account: dict[str, Any], loading: bool = False) -> str:
    if loading and not account:
        return "loading"
    account_type = str(account.get("type") or "unknown")
    normalized_type = account_type.lower().replace("_", "")
    if normalized_type == "apikey":
        return "API key"
    if normalized_type == "amazonbedrock":
        return "Bedrock"
    email = account.get("email")
    plan = account.get("planType")
    if isinstance(email, str) and email:
        identity = email.split("@", 1)[0]
    else:
        identity = account_type
    return f"{identity}/{plan}" if plan else identity


def is_api_account(account: dict[str, Any]) -> bool:
    return str(account.get("type") or "").lower().replace("_", "") == "apikey"


def git_label(cwd: str | None) -> str:
    if not cwd:
        return ""
    cached = _GIT_CACHE.get(cwd)
    now = time.monotonic()
    if cached and now - cached[0] < GIT_CACHE_SECONDS:
        return cached[1]
    try:
        result = subprocess.run(
            ["git", "-C", cwd, "status", "--porcelain=v1", "--branch"],
            capture_output=True,
            text=True,
            timeout=0.8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        label = ""
        _GIT_CACHE[cwd] = (now, label)
        return label
    if result.returncode or not result.stdout:
        label = ""
        _GIT_CACHE[cwd] = (now, label)
        return label
    lines = result.stdout.splitlines()
    branch = lines[0].removeprefix("## ").split("...", 1)[0]
    dirty = "*" if len(lines) > 1 else ""
    label = f"git:({branch}{dirty})"
    _GIT_CACHE[cwd] = (now, label)
    return label


def estimated_cost(model: str | None, total: dict[str, Any]) -> float | None:
    if not model:
        return None
    normalized = model.lower()
    prices = PRICES_PER_MILLION.get(normalized)
    if prices is None:
        for name in sorted(PRICES_PER_MILLION, key=len, reverse=True):
            if normalized.startswith(name):
                prices = PRICES_PER_MILLION[name]
                break
    if prices is None:
        return None
    input_tokens = int(total.get("input_tokens") or 0)
    cached_tokens = int(total.get("cached_input_tokens") or 0)
    output_tokens = int(total.get("output_tokens") or 0)
    uncached_tokens = max(0, input_tokens - cached_tokens)
    input_price, cached_price, output_price = prices
    return (
        uncached_tokens * input_price
        + cached_tokens * cached_price
        + output_tokens * output_price
    ) / 1_000_000


def _quota_window(limits: dict[str, Any], key: str) -> dict[str, Any]:
    rate_limits = limits.get("rateLimits", limits)
    if isinstance(rate_limits, dict):
        window = rate_limits.get(key)
        if isinstance(window, dict):
            return window
    return {}


def _field(value: dict[str, Any], camel: str, snake: str) -> Any:
    return value.get(camel) if camel in value else value.get(snake)


def render_lines(
    state: dict[str, Any],
    scanner: TranscriptScanner,
    account: dict[str, Any],
    limits: dict[str, Any],
    account_loaded: bool = True,
    width: int | None = None,
) -> list[str]:
    now = time.time()
    model = scanner.model or state.get("model") or "model --"
    if scanner.effort:
        model = f"{model} · {scanner.effort}"
    cwd = state.get("cwd")
    project = Path(cwd).name if isinstance(cwd, str) and cwd else "project --"
    branch = git_label(cwd if isinstance(cwd, str) else None)
    duration = human_duration(now - float(state.get("started_at") or now))
    api_account = is_api_account(account)
    cost = estimated_cost(str(model).split(" · ", 1)[0], scanner.total)
    credit_estimate = (
        estimate_chatgpt_credits(str(model).split(" · ", 1)[0], scanner.total)
        if not api_account and scanner.model_provider == "openai"
        else None
    )
    if api_account:
        usage_label = f"API equiv.${cost:.2f}" if cost is not None else "API equiv.$--"
    elif credit_estimate is not None:
        usage_label = f"{credit_estimate.total:.1f}cr quota"
    else:
        usage_label = "quota --"
    status = str(state.get("status") or "starting")
    status_mark = (
        color("status_working", "●") if status == "working"
        else color("status_ready", "●")
    )
    first = "  ".join(
        part
        for part in (
            f"{color('section_hud', 'HUD')} v{HUD_VERSION} {status_mark} {status}",
            color("model", f"[{model}]"),
            color(
                "account",
                f"acct:{account_label(account, loading=not account_loaded)}",
            ),
            f"{project} {branch}".strip(),
            f"⏱ {duration}",
            f"≈{usage_label}",
        )
        if part
    )

    context = official_context_usage(
        int(scanner.last.get("total_tokens") or 0), scanner.context_window
    )
    used_percent = context.used_percent if context else 0
    quota_source = limits if limits else scanner.rate_limits
    primary = _quota_window(quota_source, "primary")
    secondary = _quota_window(quota_source, "secondary")
    ctx_slot = _percent_slot(used_percent)
    if context:
        second = (
            f"{color('section_ctx', 'CTX')}  {progress_bar(used_percent)}  "
            f"{color(ctx_slot, f'{used_percent}%')} used · "
            f"{context.remaining_percent}% left · "
            f"{human_tokens(context.used_tokens)}/{human_tokens(context.window_tokens)}"
        )
    else:
        second = f"{color('section_ctx', 'CTX')}  {progress_bar(0)}  --"
    if primary:
        primary_used = float(_field(primary, 'usedPercent', 'used_percent') or 0)
        second += (
            f"  │  {format_window(_field(primary, 'windowDurationMins', 'window_minutes'))} "
            f"{color(_percent_slot(primary_used), f'{primary_used:.0f}%')} "
            f"↻{format_reset(_field(primary, 'resetsAt', 'resets_at'))}"
        )
    if secondary:
        secondary_used = float(_field(secondary, 'usedPercent', 'used_percent') or 0)
        second += (
            f"  │  {format_window(_field(secondary, 'windowDurationMins', 'window_minutes'))} "
            f"{color(_percent_slot(secondary_used), f'{secondary_used:.0f}%')} "
            f"↻{format_reset(_field(secondary, 'resetsAt', 'resets_at'))}"
        )

    input_tokens = int(scanner.total.get("input_tokens") or 0)
    cached_tokens = int(scanner.total.get("cached_input_tokens") or 0)
    cache_percent = cached_tokens * 100 / input_tokens if input_tokens else 0
    cache_slot = (
        "metric_bad" if cache_percent < 40
        else "metric_warn" if cache_percent < 70
        else "metric_good"
    )
    third = (
        f"{color('section_cache', 'CACHE')} "
        f"{color(cache_slot, f'{cache_percent:.0f}%')} · "
        f"{human_tokens(cached_tokens)}/{human_tokens(input_tokens)}"
    )
    if api_account:
        if input_tokens or cached_tokens or scanner.total.get("output_tokens"):
            uncached_tokens = max(0, input_tokens - cached_tokens)
            output_tokens = int(scanner.total.get("output_tokens") or 0)
            reasoning_tokens = int(
                scanner.total.get("reasoning_output_tokens") or 0
            )
            third += (
                f"  │  API in {human_tokens(uncached_tokens)} + "
                f"cache {human_tokens(cached_tokens)} · "
                f"out {human_tokens(output_tokens)}"
            )
            if reasoning_tokens:
                third += f" · reasoning {human_tokens(reasoning_tokens)}"
        else:
            third += "  │  API usage --"

    active_tools = state.get("active_tools") if isinstance(state.get("active_tools"), dict) else {}
    tool_stats = state.get("tool_stats") if isinstance(state.get("tool_stats"), dict) else {}

    # Sort active tools by started_at descending (most recent first).
    sorted_active_tools = sorted(
        [t for t in active_tools.values() if isinstance(t, dict)],
        key=lambda t: float(t.get("started_at", 0) or 0),
        reverse=True,
    )
    active_tool_parts: list[str] = []
    for item in sorted_active_tools[:3]:
        elapsed = now - float(item.get("started_at") or now)
        active_tool_parts.append(
            color("task_active", f"◐ {item.get('name', 'tool')} {human_duration(elapsed)}")
        )
    active_overflow = len(sorted_active_tools) - 3
    if active_overflow > 0:
        active_tool_parts.append(f"… +{active_overflow} more")
    fourth = f"{color('section_tools', 'TOOLS')}  " + (
        " · ".join(active_tool_parts) if active_tool_parts else "--"
    )

    ranked_tools = sorted(
        tool_stats.items(),
        key=lambda pair: int(pair[1].get("count") or 0),
        reverse=True,
    ) if tool_stats else []
    stat_tool_parts: list[str] = []
    for name, stats in ranked_tools[:5]:
        stat_tool_parts.append(f"✓ {name} ×{int(stats.get('count') or 0)}")
    stat_overflow = len(ranked_tools) - 5
    if stat_overflow > 0:
        stat_tool_parts.append(f"… +{stat_overflow} more")
    fifth = "       " + (
        " · ".join(stat_tool_parts) if stat_tool_parts else "--"
    )

    agents = state.get("agents") if isinstance(state.get("agents"), dict) else {}
    running_agents = sum(
        isinstance(agent, dict) and agent.get("status") == "running"
        for agent in agents.values()
    )
    risk = session_risk(
        input_tokens,
        scanner.tool_outputs,
        scanner.image_blocks,
        scanner.tool_output_bytes,
        running_agents,
        scanner.token_updates,
        credit_estimate.total if credit_estimate is not None else None,
        now - float(state.get("started_at") or now),
    )
    if (
        scanner.token_updates
        or scanner.tool_outputs
        or scanner.image_blocks
        or scanner.tool_output_bytes
    ):
        third += (
            f"  │  IO {scanner.tool_outputs}t/{scanner.image_blocks}img/"
            f"{human_bytes(scanner.tool_output_bytes)} · {scanner.token_updates}r"
        )
    if risk.level != "normal":
        risk_color = "risk_critical" if risk.level == "critical" else "risk_warning"
        reason = risk.reasons[0] if risk.reasons else ""
        label = f"⚠ {risk.level}:{reason}" if reason else f"⚠ {risk.level}"
        third += f"  │  {color(risk_color, label)}"
    transcript_agents = scanner.agent_metadata
    running_agent_entries: list[tuple[float, str]] = []
    completed_agents = 0
    for agent_id, raw_agent in agents.items():
        agent = dict(raw_agent) if isinstance(raw_agent, dict) else {}
        metadata = transcript_agents.get(str(agent_id))
        if metadata is None:
            task_name = str(agent.get("task_name") or "")
            for candidate in transcript_agents.values():
                if task_name and candidate.get("task_name") == task_name:
                    metadata = candidate
                    break
        if metadata:
            for key in ("description", "model", "effort", "current_activity", "updated_at"):
                if metadata.get(key) is not None:
                    agent[key] = metadata[key]
        if not isinstance(agent, dict):
            continue
        if agent.get("status") == "running":
            started = float(agent.get("started_at") or now)
            running_agent_entries.append(
                (started, _agent_detail_line(agent, started, now))
            )
        else:
            completed_agents += 1
    running_agent_entries.sort(key=lambda entry: entry[0], reverse=True)
    agent_detail_lines = [line for _, line in running_agent_entries]
    sixth = (
        f"{color('section_agents', 'AGENTS')} {running_agents} running"
        f" · {color('agent_completed', f'✓ {completed_agents} complete')}"
    )

    # SKILLS: filter active_tools and tool_stats for skill-related entries.
    active_skills = [
        t for t in sorted_active_tools
        if "skill" in str(t.get("name", "")).lower()
    ]
    skill_active_parts: list[str] = []
    for item in active_skills[:3]:
        elapsed = now - float(item.get("started_at") or now)
        skill_active_parts.append(
            color("task_active", f"◐ {item.get('name', 'skill')} {human_duration(elapsed)}")
        )
    skill_active_overflow = len(active_skills) - 3
    if skill_active_overflow > 0:
        skill_active_parts.append(f"… +{skill_active_overflow} more")
    skill_stats = {
        k: v for k, v in tool_stats.items() if "skill" in str(k).lower()
    }
    ranked_skills = sorted(
        skill_stats.items(),
        key=lambda pair: int(pair[1].get("count") or 0),
        reverse=True,
    ) if skill_stats else []
    skill_stat_parts: list[str] = []
    for name, stats in ranked_skills[:3]:
        skill_stat_parts.append(f"✓ {name} ×{int(stats.get('count') or 0)}")
    skill_stat_overflow = len(ranked_skills) - 3
    if skill_stat_overflow > 0:
        skill_stat_parts.append(f"… +{skill_stat_overflow} more")
    seventh_parts = skill_active_parts + skill_stat_parts
    seventh = f"{color('section_skills', 'SKILLS')} " + (
        " · ".join(seventh_parts) if seventh_parts else "--"
    )

    projection = plan_projection(state.get("plan"))
    plan = projection["items"]
    completed = projection["completed"]
    unfinished = projection["unfinished"]
    recent_completed = recent_completed_items(state, limit=2)
    plan_state = str(state.get("plan_state") or "active")
    eighth = f"{color('section_todo', 'TODO')} {completed}/{len(plan)}"
    if unfinished:
        eighth += f" · {len(unfinished)} 未完成"
        if plan_state == "stale":
            eighth += f" · {color('task_active', '⚠ 计划未同步')}"
        elif projection["in_progress"]:
            eighth += f" · {color('task_active', '◐ 执行中')}"
        else:
            eighth += f" · {color('task_active', '○ 待执行')}"
        if recent_completed:
            eighth += f" · {color('task_completed', f'✓ 最近完成 {len(recent_completed)}')}"
    elif plan:
        eighth += f"  {color('task_completed', '✓ 全部完成')}"
        if recent_completed:
            eighth += f" · 最近完成 {len(recent_completed)}"
    elif recent_completed:
        eighth += f" · {color('task_completed', f'✓ 最近完成 {len(recent_completed)}')}"
    else:
        eighth += "  · --"

    detail_lines: list[str] = []
    detail_items = plan_detail_items(state, limit=HUD_DETAIL_ROWS)
    unfinished_shown = min(2, len(unfinished))
    for index, item in enumerate(detail_items):
        overflow = (
            max(0, len(unfinished) - unfinished_shown)
            if unfinished_shown and index == unfinished_shown - 1
            else 0
        )
        detail_lines.append(
            task_detail_line(
                item,
                index,
                len(detail_items),
                width or 10_000,
                overflow=overflow,
            )
        )

    return [
        first, second, third, fourth, fifth, sixth,
        *agent_detail_lines, seventh, eighth, *detail_lines,
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--ready-file")
    parser.add_argument("--geometry-file")
    parser.add_argument("--heartbeat-file")
    args = parser.parse_args()

    scanner = TranscriptScanner()
    account_poller = None if args.once else AccountPoller()
    previous_handlers: dict[int, Any] = {}
    shutting_down = False

    def terminate(signum: int, _frame: Any) -> None:
        if not shutting_down:
            raise SystemExit(128 + signum)

    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGTERM, signal.SIGHUP):
            previous_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, terminate)
    last_geometry: dict[str, int] | None = None
    ready_written = False
    first_frame = True

    try:
        if account_poller is not None:
            account_poller.start()
        while enabled():
            state = load_state(args.run_id)
            refresh_theme()
            # The launcher needs only a minimal first frame to open the split.
            # Defer a potentially large transcript scan until the HUD is
            # visible; --once remains a complete synchronous snapshot mode.
            if args.once or not first_frame:
                scanner.scan(state.get("transcript_path"))
            if account_poller is not None:
                account, limits, account_loaded = account_poller.snapshot()
            else:
                account, limits, account_loaded = {}, {}, False
            try:
                geometry = terminal_geometry(sys.stdout.fileno())
            except (OSError, ValueError):
                geometry = {
                    "rows": HUD_MIN_LINES,
                    "columns": shutil.get_terminal_size((120, HUD_MIN_LINES)).columns,
                    "width_pixels": 0,
                    "height_pixels": 0,
                }
            if not args.once and os.environ.get("TMUX_PANE"):
                target_lines = desired_hud_lines(state)
                if tmux_surface_needs_resize(target_lines, geometry["rows"]):
                    resize_tmux_surface(target_lines)
                    try:
                        geometry = terminal_geometry(sys.stdout.fileno())
                    except (OSError, ValueError):
                        pass
            width = max(1, geometry["columns"])
            lines = render_lines(
                state,
                scanner,
                account,
                limits,
                account_loaded=account_loaded,
                width=width,
            )
            if args.geometry_file and geometry != last_geometry:
                _write_private_json(Path(args.geometry_file), geometry)
                last_geometry = geometry
            visible_lines = (
                lines
                if args.once
                else fit_lines_for_geometry(
                    lines,
                    geometry["rows"],
                    agent_rows=running_agent_count(state),
                    detail_rows=todo_detail_rows(state),
                )
            )
            clipped = [visible_clip(line, width) for line in visible_lines]
            if args.once:
                print("\n".join(clipped))
                if args.ready_file:
                    _touch_private(Path(args.ready_file))
                return 0
            # The renderer never writes beyond the current surface while a dynamic
            # resize is in flight, so the HUD does not scroll.
            sys.stdout.write("\033[H\033[2J" + "\n".join(clipped))
            sys.stdout.flush()
            if args.heartbeat_file:
                _touch_private(Path(args.heartbeat_file))
            if args.ready_file and not ready_written:
                _touch_private(Path(args.ready_file))
                ready_written = True
            if state.get("status") == "ended":
                return 0
            delay = 0.1 if first_frame else 1.0
            first_frame = False
            time.sleep(delay)
        return 0
    finally:
        shutting_down = True
        for signum in previous_handlers:
            signal.signal(signum, signal.SIG_IGN)
        try:
            if account_poller is not None:
                account_poller.stop()
        finally:
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)


if __name__ == "__main__":
    raise SystemExit(main())
