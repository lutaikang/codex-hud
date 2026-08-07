#!/usr/bin/env python3
"""Manage tmux mouse routing and legacy HUD bindings."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import os
import re
import secrets
import subprocess
import tempfile
from pathlib import Path


LEGACY_MOUSE_KEYS: tuple[tuple[str, str], ...] = (
    ("root", "MouseDown1Pane"),
    ("root", "MouseDown2Pane"),
    ("root", "MouseDown3Pane"),
    ("root", "MouseDrag1Pane"),
    ("root", "MouseDragEnd1Pane"),
    ("root", "DoubleClick1Pane"),
    ("root", "TripleClick1Pane"),
    ("root", "WheelUpPane"),
    ("root", "WheelDownPane"),
    ("copy-mode", "MouseDown1Pane"),
    ("copy-mode", "MouseDrag1Pane"),
    ("copy-mode", "MouseDragEnd1Pane"),
    ("copy-mode", "WheelUpPane"),
    ("copy-mode", "WheelDownPane"),
    ("copy-mode-vi", "MouseDown1Pane"),
    ("copy-mode-vi", "MouseDrag1Pane"),
    ("copy-mode-vi", "MouseDragEnd1Pane"),
    ("copy-mode-vi", "WheelUpPane"),
    ("copy-mode-vi", "WheelDownPane"),
)
LEGACY_MOUSE_MARKERS = ("#{@is_hud}", "#{@hud_main_pane}")
MOUSE_HOLDERS_OPTION = "@codex_hud_mouse_holders"
MOUSE_ORIGINAL_SCOPE_OPTION = "@codex_hud_mouse_original_scope"
MOUSE_ORIGINAL_VALUE_OPTION = "@codex_hud_mouse_original_value"


def _best_effort(action: object, *arguments: object, **keywords: object) -> None:
    try:
        action(*arguments, **keywords)  # type: ignore[operator]
    except Exception:
        pass


def _tmux(arguments: list[str], socket_name: str | None = None) -> list[str]:
    command = ["tmux"]
    if socket_name:
        command.extend(["-L", socket_name])
    command.extend(arguments)
    return command


def list_key_command(
    table: str, key: str, socket_name: str | None = None
) -> str | None:
    result = subprocess.run(
        _tmux(["list-keys", "-T", table, key], socket_name),
        capture_output=True,
        text=True,
        check=False,
    )
    lines = result.stdout.strip().splitlines()
    if not lines:
        return None
    match = re.match(r"^bind-key(?:\s+-r)?\s+-T\s+\S+\s+\S+\s+(.*)$", lines[0])
    return match.group(1) if match else None


def default_key_commands(table: str) -> dict[str, str] | None:
    probe_socket = f"codex-hud-defaults-{os.getpid()}-{secrets.token_hex(3)}"
    probe_environment = os.environ.copy()
    probe_environment.pop("TMUX", None)
    probe_environment.pop("TMUX_PANE", None)
    result = subprocess.run(
        ["tmux", "-L", probe_socket, "-f", "/dev/null", "list-keys", "-T", table],
        capture_output=True,
        text=True,
        check=False,
        env=probe_environment,
    )
    if result.returncode != 0:
        return None
    commands: dict[str, str] = {}
    for line in result.stdout.splitlines():
        match = re.match(r"^bind-key(?:\s+-r)?\s+-T\s+\S+\s+(\S+)\s+(.*)$", line)
        if match:
            commands[match.group(1)] = match.group(2)
    return commands


def restore_legacy_mouse_bindings(socket_name: str | None = None) -> bool:
    """Restore tmux defaults only for bindings written by codex-hud <=0.3.8."""
    contaminated = [
        (table, key)
        for table, key in LEGACY_MOUSE_KEYS
        if (command := list_key_command(table, key, socket_name))
        and any(marker in command for marker in LEGACY_MOUSE_MARKERS)
    ]
    if not contaminated:
        return False

    defaults: dict[str, dict[str, str]] = {}
    for table in {table for table, _ in contaminated}:
        commands = default_key_commands(table)
        if commands is None:
            return False
        defaults[table] = commands

    for table, key in contaminated:
        command = defaults[table].get(key)
        arguments = (
            _tmux(
                [
                    "bind-key",
                    "-T",
                    table,
                    key,
                    command.replace(r"\;", ";"),
                ],
                socket_name,
            )
            if command is not None
            else _tmux(["unbind-key", "-T", table, key], socket_name)
        )
        result = subprocess.run(
            arguments,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            return False
    return True


def session_id(pane: str, socket_name: str | None = None) -> str:
    result = subprocess.run(
        _tmux(["display-message", "-p", "-t", pane, "#{session_id}"], socket_name),
        capture_output=True,
        text=True,
        check=True,
    )
    value = result.stdout.strip()
    if not value:
        raise RuntimeError(f"无法确定 tmux session：{pane}")
    return value


def session_option(
    session: str,
    name: str,
    effective: bool = False,
    socket_name: str | None = None,
) -> str:
    arguments = ["show-options"]
    if effective:
        arguments.append("-A")
    arguments.extend(["-v", "-t", session, name])
    result = subprocess.run(
        _tmux(arguments, socket_name),
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip()


def set_session_option(
    session: str,
    name: str,
    value: str,
    socket_name: str | None = None,
) -> None:
    subprocess.run(
        _tmux(["set-option", "-t", session, name, value], socket_name),
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def unset_session_option(
    session: str, name: str, socket_name: str | None = None
) -> None:
    subprocess.run(
        _tmux(["set-option", "-u", "-t", session, name], socket_name),
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def mouse_lock_path(session: str, socket_name: str | None = None) -> Path:
    tmux_identity = socket_name or os.environ.get("TMUX", "default")
    digest = hashlib.sha256(f"{tmux_identity}\0{session}".encode()).hexdigest()[:20]
    uid = getattr(os, "getuid", lambda: 0)()
    return Path(tempfile.gettempdir()) / f"codex-hud-mouse-{uid}-{digest}.lock"


@contextmanager
def mouse_state_lock(session: str, socket_name: str | None = None):
    descriptor = os.open(
        mouse_lock_path(session, socket_name), os.O_RDWR | os.O_CREAT, 0o600
    )
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def live_mouse_holders(raw: str) -> list[str]:
    holders: list[str] = []
    for holder in raw.split(",") if raw else []:
        try:
            pid = int(holder.split(":", 1)[0])
            os.kill(pid, 0)
        except (ValueError, ProcessLookupError):
            continue
        except PermissionError:
            pass
        holders.append(holder)
    return holders


def holder_mouse_value(holder: str) -> str | None:
    value = holder.rsplit(":", 1)[-1]
    return value if value in {"on", "off"} else None


def required_mouse_value(holders: list[str], fallback: str) -> str:
    """Resolve incompatible session-wide requests deterministically.

    tmux cannot expose mouse reporting to one client while hiding it from
    another client attached to the same session. Prefer native terminal input
    whenever any live holder requires it; otherwise retain the fallback mode.
    """
    values = [holder_mouse_value(holder) for holder in holders]
    if "off" in values:
        return "off"
    if "on" in values:
        return "on"
    return fallback


def restore_mouse_snapshot(session: str, socket_name: str | None = None) -> None:
    scope = session_option(session, MOUSE_ORIGINAL_SCOPE_OPTION, socket_name=socket_name)
    value = session_option(session, MOUSE_ORIGINAL_VALUE_OPTION, socket_name=socket_name)
    if scope == "explicit" and value in {"on", "off"}:
        set_session_option(session, "mouse", value, socket_name)
    elif scope == "inherited":
        unset_session_option(session, "mouse", socket_name)
    unset_session_option(session, MOUSE_HOLDERS_OPTION, socket_name)
    unset_session_option(session, MOUSE_ORIGINAL_SCOPE_OPTION, socket_name)
    unset_session_option(session, MOUSE_ORIGINAL_VALUE_OPTION, socket_name)


def _acquire_mouse(
    pane: str, socket_name: str | None, value: str
) -> tuple[str, str, str | None]:
    session = session_id(pane, socket_name)
    with mouse_state_lock(session, socket_name):
        restore_legacy_mouse_bindings(socket_name)
        holders = live_mouse_holders(
            session_option(session, MOUSE_HOLDERS_OPTION, socket_name=socket_name)
        )
        fresh_snapshot = not holders
        try:
            if fresh_snapshot:
                if session_option(
                    session, MOUSE_ORIGINAL_SCOPE_OPTION, socket_name=socket_name
                ):
                    restore_mouse_snapshot(session, socket_name)
                explicit = session_option(session, "mouse", socket_name=socket_name)
                effective = session_option(
                    session, "mouse", effective=True, socket_name=socket_name
                )
                if explicit not in {"", "on", "off"} or effective not in {"on", "off"}:
                    raise RuntimeError("无法读取 tmux mouse 选项")
                set_session_option(
                    session,
                    MOUSE_ORIGINAL_SCOPE_OPTION,
                    "explicit" if explicit else "inherited",
                    socket_name,
                )
                set_session_option(
                    session,
                    MOUSE_ORIGINAL_VALUE_OPTION,
                    explicit or effective,
                    socket_name,
                )
            holder = f"{os.getpid()}:{secrets.token_hex(4)}:{value}"
            set_session_option(
                session,
                "mouse",
                required_mouse_value([*holders, holder], value),
                socket_name,
            )
            set_session_option(
                session,
                MOUSE_HOLDERS_OPTION,
                ",".join([*holders, holder]),
                socket_name,
            )
        except Exception:
            if fresh_snapshot:
                _best_effort(restore_mouse_snapshot, session, socket_name)
            raise
    return session, holder, socket_name


def acquire_native_mouse(
    pane: str, socket_name: str | None = None
) -> tuple[str, str, str | None]:
    """Temporarily disable tmux mouse so the outer terminal owns mouse input."""
    return _acquire_mouse(pane, socket_name, "off")


def acquire_tmux_mouse(
    pane: str, socket_name: str | None = None
) -> tuple[str, str, str | None]:
    """Temporarily enable tmux mouse for non-Ghostty fallback terminals."""
    return _acquire_mouse(pane, socket_name, "on")


def release_native_mouse(lease: tuple[str, str] | tuple[str, str, str | None]) -> None:
    session, holder, *socket_values = lease
    socket_name = socket_values[0] if socket_values else None
    try:
        with mouse_state_lock(session, socket_name):
            holders = [
                item
                for item in live_mouse_holders(
                    session_option(
                        session, MOUSE_HOLDERS_OPTION, socket_name=socket_name
                    )
                )
                if item != holder
            ]
            if holders:
                current = session_option(
                    session, "mouse", effective=True, socket_name=socket_name
                )
                if current in {"on", "off"}:
                    set_session_option(
                        session,
                        "mouse",
                        required_mouse_value(holders, current),
                        socket_name,
                    )
                set_session_option(
                    session, MOUSE_HOLDERS_OPTION, ",".join(holders), socket_name
                )
            else:
                restore_mouse_snapshot(session, socket_name)
    except Exception:
        # Codex cleanup must not mask the process exit code.
        pass


# Compatibility alias for callers from the previous native lease.
release_tmux_mouse = release_native_mouse
