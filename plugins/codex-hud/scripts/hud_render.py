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

from hud_state import enabled, load_state, HUD_LINES, HUD_VERSION
from metrics import official_context_usage


ESC = "\033["
RESET = f"{ESC}0m"
COLORS = {
    "cyan": f"{ESC}36m",
    "green": f"{ESC}32m",
    "yellow": f"{ESC}33m",
    "red": f"{ESC}31m",
    "magenta": f"{ESC}35m",
    "dim": f"{ESC}2m",
}
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
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


def color(name: str, text: str) -> str:
    if os.environ.get("NO_COLOR"):
        return text
    return f"{COLORS[name]}{text}{RESET}"


def human_tokens(value: int | float | None) -> str:
    number = float(value or 0)
    if number >= 1_000_000:
        return f"{number / 1_000_000:.2f}".rstrip("0").rstrip(".") + "m"
    if number >= 1_000:
        return f"{number / 1_000:.1f}".rstrip("0").rstrip(".") + "k"
    return str(int(number))


def human_duration(seconds: float | int | None) -> str:
    seconds = max(0, int(seconds or 0))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def progress_bar(percent: float | int | None, width: int = 10) -> str:
    value = max(0.0, min(100.0, float(percent or 0)))
    filled = round(width * value / 100)
    name = "green" if value < 65 else "yellow" if value < 85 else "red"
    return color(name, "█" * filled + "░" * (width - filled))


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


class TranscriptScanner:
    def __init__(self) -> None:
        self.path: Path | None = None
        self.offset = 0
        self.last: dict[str, Any] = {}
        self.total: dict[str, Any] = {}
        self.context_window: int | None = None
        self.model: str | None = None
        self.effort: str | None = None
        self.rate_limits: dict[str, Any] = {}

    def _reset_content(self) -> None:
        self.offset = 0
        self.last = {}
        self.total = {}
        self.context_window = None
        self.model = None
        self.effort = None
        self.rate_limits = {}

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
        model = payload.get("model")
        if isinstance(model, str):
            self.model = model
        effort = payload.get("effort")
        if isinstance(effort, str):
            self.effort = effort
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
            window = info.get("model_context_window")
            if isinstance(window, int):
                self.context_window = window
        rate_limits = payload.get("rate_limits")
        if isinstance(rate_limits, dict):
            self.rate_limits = rate_limits


class AccountReader:
    def read(
        self, timeout: float = ACCOUNT_READ_TIMEOUT
    ) -> tuple[dict[str, Any], dict[str, Any]]:
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
        messages: queue.Queue[dict[str, Any]] = queue.Queue()

        def reader() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    messages.put(value)

        threading.Thread(target=reader, daemon=True).start()

        def send(value: dict[str, Any]) -> None:
            assert process.stdin is not None
            process.stdin.write(json.dumps(value, separators=(",", ":")) + "\n")
            process.stdin.flush()

        deadline = time.monotonic() + max(0.05, timeout)
        account: dict[str, Any] = {}
        limits: dict[str, Any] = {}

        def receive(expected_id: int) -> dict[str, Any] | None:
            """Return the first response for an id, including JSON-RPC errors."""
            while time.monotonic() < deadline:
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
        except (BrokenPipeError, OSError, queue.Empty):
            pass
        finally:
            try:
                if process.stdin:
                    process.stdin.close()
            except OSError:
                pass
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
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
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=0.2)


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
    cost_label = (
        f"API equiv.${cost:.2f}" if cost is not None else "API equiv.$--"
    )
    status = str(state.get("status") or "starting")
    status_mark = color("yellow", "●") if status == "working" else color("green", "●")
    first = "  ".join(
        part
        for part in (
            f"{color('cyan', 'HUD')} v{HUD_VERSION} {status_mark} {status}",
            color("cyan", f"[{model}]"),
            color(
                "magenta",
                f"acct:{account_label(account, loading=not account_loaded)}",
            ),
            f"{project} {branch}".strip(),
            f"⏱ {duration}",
            f"≈{cost_label}",
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
    if context:
        second = (
            f"{color('cyan', 'CTX')}  {progress_bar(used_percent)}  "
            f"{used_percent}% used · {context.remaining_percent}% left · "
            f"{human_tokens(context.used_tokens)}/{human_tokens(context.window_tokens)}"
        )
    else:
        second = f"{color('cyan', 'CTX')}  {progress_bar(0)}  --"
    if primary:
        second += (
            f"  │  {format_window(_field(primary, 'windowDurationMins', 'window_minutes'))} "
            f"{float(_field(primary, 'usedPercent', 'used_percent') or 0):.0f}% "
            f"↻{format_reset(_field(primary, 'resetsAt', 'resets_at'))}"
        )
    if secondary:
        second += (
            f"  │  {format_window(_field(secondary, 'windowDurationMins', 'window_minutes'))} "
            f"{float(_field(secondary, 'usedPercent', 'used_percent') or 0):.0f}% "
            f"↻{format_reset(_field(secondary, 'resetsAt', 'resets_at'))}"
        )

    input_tokens = int(scanner.total.get("input_tokens") or 0)
    cached_tokens = int(scanner.total.get("cached_input_tokens") or 0)
    cache_percent = cached_tokens * 100 / input_tokens if input_tokens else 0
    third = (
        f"{color('cyan', 'CACHE')} {cache_percent:.0f}% · "
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
            color("yellow", f"◐ {item.get('name', 'tool')} {human_duration(elapsed)}")
        )
    active_overflow = len(sorted_active_tools) - 3
    if active_overflow > 0:
        active_tool_parts.append(f"… +{active_overflow} more")
    fourth = f"{color('cyan', 'TOOLS')}  " + (
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
    running_agent_entries: list[tuple[float, str]] = []
    completed_agents = 0
    for agent in agents.values():
        if not isinstance(agent, dict):
            continue
        if agent.get("status") == "running":
            started = float(agent.get("started_at") or now)
            label = (
                f"◐ {agent.get('type', 'agent')} "
                f"{human_duration(now - started)}"
            )
            running_agent_entries.append((started, label))
        else:
            completed_agents += 1
    running_agent_entries.sort(key=lambda entry: entry[0], reverse=True)
    active_agents = [label for _, label in running_agent_entries]
    agent_summary = " · ".join(active_agents[:3]) if active_agents else "0 running"
    agent_overflow = len(active_agents) - 3
    if agent_overflow > 0:
        agent_summary += f"  … +{agent_overflow} more"
    sixth = (
        f"{color('cyan', 'AGENTS')} {agent_summary} · ✓ {completed_agents} complete"
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
            color("yellow", f"◐ {item.get('name', 'skill')} {human_duration(elapsed)}")
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
    seventh = f"{color('cyan', 'SKILLS')} " + (
        " · ".join(seventh_parts) if seventh_parts else "--"
    )

    plan = state.get("plan") if isinstance(state.get("plan"), list) else []
    completed = sum(1 for item in plan if isinstance(item, dict) and item.get("status") == "completed")
    current = next(
        (
            str(item.get("step"))
            for item in plan
            if isinstance(item, dict) and item.get("status") in {"in_progress", "inProgress"}
        ),
        None,
    )
    if current is None:
        current = next(
            (
                str(item.get("step"))
                for item in plan
                if isinstance(item, dict) and item.get("status") == "pending"
            ),
            None,
        )
    eighth = f"{color('cyan', 'TODO')} {completed}/{len(plan)}"
    if current:
        eighth += f"  {color('yellow', '◐')} {current}"
    elif plan:
        eighth += f"  {color('green', '✓')} 全部完成"
    else:
        eighth += "  · --"

    return [first, second, third, fourth, fifth, sixth, seventh, eighth]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--ready-file")
    parser.add_argument("--geometry-file")
    args = parser.parse_args()

    scanner = TranscriptScanner()
    account_poller = None if args.once else AccountPoller()
    if account_poller is not None:
        account_poller.start()
    last_geometry: dict[str, int] | None = None
    ready_written = False
    first_frame = True

    try:
        while enabled():
            state = load_state(args.run_id)
            # The launcher needs only a minimal first frame to open the split.
            # Defer a potentially large transcript scan until the HUD is
            # visible; --once remains a complete synchronous snapshot mode.
            if args.once or not first_frame:
                scanner.scan(state.get("transcript_path"))
            if account_poller is not None:
                account, limits, account_loaded = account_poller.snapshot()
            else:
                account, limits, account_loaded = {}, {}, False
            lines = render_lines(
                state,
                scanner,
                account,
                limits,
                account_loaded=account_loaded,
            )
            try:
                geometry = terminal_geometry(sys.stdout.fileno())
            except (OSError, ValueError):
                geometry = {
                    "rows": HUD_LINES,
                    "columns": shutil.get_terminal_size((120, HUD_LINES)).columns,
                    "width_pixels": 0,
                    "height_pixels": 0,
                }
            if args.geometry_file and geometry != last_geometry:
                _write_private_json(Path(args.geometry_file), geometry)
                last_geometry = geometry
            width = max(1, geometry["columns"])
            clipped = [visible_clip(line, width) for line in lines]
            if args.once:
                print("\n".join(clipped))
                if args.ready_file:
                    _touch_private(Path(args.ready_file))
                return 0
            # Exactly HUD_LINES rows and no trailing newline: the HUD does not scroll.
            sys.stdout.write("\033[H\033[2J" + "\n".join(clipped))
            sys.stdout.flush()
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
        if account_poller is not None:
            account_poller.stop()


if __name__ == "__main__":
    raise SystemExit(main())
