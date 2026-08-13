#!/usr/bin/env python3
"""Read-only Codex transcript token audit.

The Codex rollout JSONL files contain cumulative token counters.  Subagent
rollouts also contain a replay of the parent transcript, so a naive sum of the
last counter in every file overstates usage.  This module attributes only the
counter deltas after the subagent hand-off boundary and reports the expensive
tool payloads that commonly amplify context.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from metrics import CreditEstimate, estimate_chatgpt_credits, session_risk


UTC = dt.timezone.utc
LOCAL = dt.datetime.now().astimezone().tzinfo or UTC
TOKEN_FIELDS = (
    "total_tokens",
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)
IMAGE_BLOCK_TYPES = {"image", "input_image"}
TOOL_CALL_TYPES = {"function_call", "custom_tool_call"}
TOOL_OUTPUT_TYPES = {"function_call_output", "custom_tool_call_output"}


def _zero_usage() -> dict[str, int]:
    return {field: 0 for field in TOKEN_FIELDS}


def _usage(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return _zero_usage()
    return {field: max(0, int(value.get(field) or 0)) for field in TOKEN_FIELDS}


def _subtract(current: dict[str, int], previous: dict[str, int]) -> dict[str, int]:
    return {
        field: max(0, int(current.get(field, 0)) - int(previous.get(field, 0)))
        for field in TOKEN_FIELDS
    }


def _add(target: dict[str, int], delta: dict[str, int]) -> None:
    for field in TOKEN_FIELDS:
        target[field] = int(target.get(field, 0)) + int(delta.get(field, 0))


def _parse_timestamp(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def _parse_bound(value: str | None, *, default: dt.datetime) -> dt.datetime:
    if not value:
        return default
    if len(value) == 10:
        try:
            parsed_date = dt.date.fromisoformat(value)
        except ValueError:
            pass
        else:
            return dt.datetime.combine(parsed_date, dt.time.min, tzinfo=LOCAL).astimezone(UTC)
    parsed = _parse_timestamp(value)
    if parsed is not None:
        return parsed
    try:
        parsed_date = dt.date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid date/time: {value}") from exc
    return dt.datetime.combine(parsed_date, dt.time.min, tzinfo=LOCAL).astimezone(UTC)


def _in_window(timestamp: dt.datetime | None, start: dt.datetime, end: dt.datetime) -> bool:
    return timestamp is not None and start <= timestamp < end


def _json_size(value: Any) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    except (TypeError, ValueError):
        return 0


def _count_image_blocks(value: Any) -> int:
    if isinstance(value, dict):
        count = 1 if value.get("type") in IMAGE_BLOCK_TYPES else 0
        return count + sum(_count_image_blocks(item) for item in value.values())
    if isinstance(value, list):
        return sum(_count_image_blocks(item) for item in value)
    return 0


def _records(path: Path) -> Iterable[dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    yield record
    except OSError:
        return


def _first_session_meta(path: Path) -> dict[str, Any] | None:
    for record in _records(path):
        if record.get("type") == "session_meta" and isinstance(record.get("payload"), dict):
            return record["payload"]
    return None


@dataclass
class SessionAudit:
    session_id: str
    path: str
    started_at: str | None
    cwd: str | None
    model_provider: str | None
    model: str | None
    reasoning_effort: str | None
    thread_source: str | None
    parent_thread_id: str | None
    subagent: bool
    usage: dict[str, int] = field(default_factory=_zero_usage)
    token_updates: int = 0
    tool_calls: int = 0
    tool_outputs: int = 0
    tool_output_bytes: int = 0
    image_blocks: int = 0
    compactions: int = 0
    tool_breakdown: dict[str, int] = field(default_factory=dict)
    usage_by_day: dict[str, dict[str, int]] = field(default_factory=dict)
    first_model_at: str | None = None
    last_model_at: str | None = None

    @property
    def total_tokens(self) -> int:
        return int(self.usage.get("total_tokens", 0))

    @property
    def credit_estimate(self) -> CreditEstimate | None:
        if self.model_provider != "openai":
            return None
        return estimate_chatgpt_credits(self.model, self.usage)

    @property
    def quota_credits(self) -> float | None:
        estimate = self.credit_estimate
        return estimate.total if estimate is not None else None

    @property
    def observed_seconds(self) -> float:
        first = _parse_timestamp(self.first_model_at)
        last = _parse_timestamp(self.last_model_at)
        if first is None or last is None:
            return 0.0
        return max(0.0, (last - first).total_seconds())

    @property
    def average_input_per_round(self) -> float:
        return (
            self.usage.get("input_tokens", 0) / self.token_updates
            if self.token_updates
            else 0.0
        )

    @property
    def risk_level(self) -> str:
        return session_risk(
            self.usage.get("input_tokens", 0),
            self.tool_outputs,
            self.image_blocks,
            self.tool_output_bytes,
            0,
            self.token_updates,
            self.quota_credits,
            self.observed_seconds,
        ).level

    @property
    def risk_reasons(self) -> list[str]:
        return list(
            session_risk(
                self.usage.get("input_tokens", 0),
                self.tool_outputs,
                self.image_blocks,
                self.tool_output_bytes,
                0,
                self.token_updates,
                self.quota_credits,
                self.observed_seconds,
            ).reasons
        )


@dataclass
class AuditReport:
    window_start: str
    window_end: str
    codex_home: str
    sessions_scanned: int
    sessions_with_usage: int
    subagent_sessions: int
    root_sessions: int
    usage: dict[str, int]
    by_day: dict[str, dict[str, int]]
    by_provider: list[dict[str, Any]]
    by_model: list[dict[str, Any]]
    task_families: list[dict[str, Any]]
    openai_quota: dict[str, Any]
    top_sessions: list[SessionAudit]
    parent_fanout: list[dict[str, Any]]
    warnings: list[str]


def scan_session(
    path: Path,
    *,
    start: dt.datetime,
    end: dt.datetime,
) -> SessionAudit | None:
    own_meta = _first_session_meta(path)
    if not own_meta:
        return None

    source = own_meta.get("source") if isinstance(own_meta.get("source"), dict) else {}
    subagent = source.get("subagent") if isinstance(source.get("subagent"), dict) else {}
    spawn = subagent.get("thread_spawn") if isinstance(subagent.get("thread_spawn"), dict) else {}
    is_subagent = bool(spawn.get("parent_thread_id"))
    report = SessionAudit(
        session_id=str(own_meta.get("id") or own_meta.get("session_id") or path.stem),
        path=str(path),
        started_at=own_meta.get("timestamp") if isinstance(own_meta.get("timestamp"), str) else None,
        cwd=own_meta.get("cwd") if isinstance(own_meta.get("cwd"), str) else None,
        model_provider=own_meta.get("model_provider") if isinstance(own_meta.get("model_provider"), str) else None,
        model=None,
        reasoning_effort=None,
        thread_source=own_meta.get("thread_source") if isinstance(own_meta.get("thread_source"), str) else None,
        parent_thread_id=spawn.get("parent_thread_id") if isinstance(spawn.get("parent_thread_id"), str) else None,
        subagent=is_subagent,
    )
    previous = _zero_usage()
    call_names: dict[str, str] = {}
    in_scope = not is_subagent

    for record in _records(path):
        timestamp = _parse_timestamp(record.get("timestamp"))
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        record_type = record.get("type")
        payload_type = payload.get("type")
        if isinstance(payload.get("model"), str):
            report.model = payload["model"]
        if isinstance(payload.get("effort"), str):
            report.reasoning_effort = payload["effort"]
        if is_subagent and record_type == "inter_agent_communication_metadata":
            in_scope = True
            continue
        in_window = _in_window(timestamp, start, end)

        if record_type == "event_msg" and payload_type == "token_count":
            info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
            current = _usage(info.get("total_token_usage"))
            if current["total_tokens"] < previous["total_tokens"]:
                previous = _zero_usage()
            delta = _subtract(current, previous)
            if in_scope and in_window and delta["total_tokens"]:
                _add(report.usage, delta)
                timestamp_text = timestamp.astimezone(UTC).isoformat()
                if report.first_model_at is None:
                    report.first_model_at = timestamp_text
                report.last_model_at = timestamp_text
                day = timestamp.astimezone(LOCAL).date().isoformat()
                daily = report.usage_by_day.setdefault(day, _zero_usage())
                _add(daily, delta)
                report.token_updates += 1
            previous = current
            continue

        if not in_scope or not in_window:
            continue
        if record_type == "compacted":
            report.compactions += 1
            continue
        if record_type != "response_item":
            continue

        if payload_type in TOOL_CALL_TYPES:
            call_id = str(payload.get("call_id") or payload.get("id") or "")
            call_names[call_id] = str(payload.get("name") or payload_type)
            report.tool_calls += 1
        elif payload_type in TOOL_OUTPUT_TYPES:
            call_id = str(payload.get("call_id") or "")
            name = call_names.get(call_id, payload_type)
            report.tool_outputs += 1
            report.tool_output_bytes += _json_size(payload.get("output"))
            report.image_blocks += _count_image_blocks(payload.get("output"))
            report.tool_breakdown[name] = report.tool_breakdown.get(name, 0) + 1

    return report if report.total_tokens or report.tool_outputs else None


def _human_tokens(value: int) -> str:
    number = float(value)
    if number >= 1_000_000_000:
        return f"{number / 1_000_000_000:.2f}B"
    if number >= 1_000_000:
        return f"{number / 1_000_000:.2f}M"
    if number >= 1_000:
        return f"{number / 1_000:.1f}K"
    return str(int(number))


def _human_bytes(value: int) -> str:
    number = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if number < 1024 or unit == "GB":
            return f"{number:.1f}{unit}" if unit != "B" else f"{int(number)}B"
        number /= 1024
    return f"{int(value)}B"


def _credit_totals(sessions: Iterable[SessionAudit]) -> dict[str, float]:
    totals = {
        "uncached_input": 0.0,
        "cached_input": 0.0,
        "output": 0.0,
        "total": 0.0,
    }
    for session in sessions:
        estimate = session.credit_estimate
        if estimate is None:
            continue
        totals["uncached_input"] += estimate.uncached_input
        totals["cached_input"] += estimate.cached_input
        totals["output"] += estimate.output
        totals["total"] += estimate.total
    return totals


def _aggregate_sessions(
    sessions: list[SessionAudit],
    *,
    label_key: str,
    label: str,
) -> dict[str, Any]:
    usage = _zero_usage()
    for session in sessions:
        _add(usage, session.usage)
    credits = _credit_totals(sessions)
    return {
        label_key: label,
        "sessions": len(sessions),
        "root_sessions": sum(not session.subagent for session in sessions),
        "subagent_sessions": sum(session.subagent for session in sessions),
        "usage": usage,
        "model_rounds": sum(session.token_updates for session in sessions),
        "tool_calls": sum(session.tool_calls for session in sessions),
        "tool_outputs": sum(session.tool_outputs for session in sessions),
        "tool_output_bytes": sum(session.tool_output_bytes for session in sessions),
        "image_blocks": sum(session.image_blocks for session in sessions),
        "credit_estimate": credits if credits["total"] else None,
    }


def _group_by_provider(sessions: list[SessionAudit]) -> list[dict[str, Any]]:
    grouped: dict[str, list[SessionAudit]] = defaultdict(list)
    for session in sessions:
        grouped[session.model_provider or "unknown"].append(session)
    values = [
        _aggregate_sessions(items, label_key="provider", label=provider)
        for provider, items in grouped.items()
    ]
    return sorted(
        values,
        key=lambda item: item["usage"]["total_tokens"],
        reverse=True,
    )


def _group_by_model(sessions: list[SessionAudit]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], list[SessionAudit]] = defaultdict(list)
    for session in sessions:
        key = (
            session.model_provider or "unknown",
            session.model or "unknown",
            session.reasoning_effort or "unknown",
        )
        grouped[key].append(session)
    values: list[dict[str, Any]] = []
    for (provider, model, effort), items in grouped.items():
        value = _aggregate_sessions(
            items,
            label_key="model_config",
            label=f"{provider}/{model}/{effort}",
        )
        value.update({"provider": provider, "model": model, "reasoning_effort": effort})
        values.append(value)
    return sorted(
        values,
        key=lambda item: item["usage"]["total_tokens"],
        reverse=True,
    )


def _task_families(sessions: list[SessionAudit]) -> list[dict[str, Any]]:
    by_id = {session.session_id: session for session in sessions}

    def root_id(session: SessionAudit) -> str:
        current = session
        seen = {current.session_id}
        while current.parent_thread_id and current.parent_thread_id not in seen:
            parent_id = current.parent_thread_id
            parent = by_id.get(parent_id)
            if parent is None:
                return parent_id
            seen.add(parent_id)
            current = parent
        return current.session_id

    grouped: dict[str, list[SessionAudit]] = defaultdict(list)
    for session in sessions:
        grouped[root_id(session)].append(session)

    families: list[dict[str, Any]] = []
    for root, items in grouped.items():
        aggregate = _aggregate_sessions(
            items,
            label_key="root_session_id",
            label=root,
        )
        first_values = [
            parsed
            for parsed in (_parse_timestamp(item.first_model_at) for item in items)
            if parsed is not None
        ]
        last_values = [
            parsed
            for parsed in (_parse_timestamp(item.last_model_at) for item in items)
            if parsed is not None
        ]
        elapsed_seconds = (
            max(0.0, (max(last_values) - min(first_values)).total_seconds())
            if first_values and last_values
            else 0.0
        )
        credits = aggregate.get("credit_estimate")
        quota_credits = float(credits["total"]) if isinstance(credits, dict) else 0.0
        child_count = sum(item.subagent for item in items)
        risk = session_risk(
            aggregate["usage"]["input_tokens"],
            aggregate["tool_outputs"],
            aggregate["image_blocks"],
            aggregate["tool_output_bytes"],
            child_count,
            aggregate["model_rounds"],
            quota_credits,
            elapsed_seconds,
        )
        configs = Counter(
            f"{item.model_provider or 'unknown'}/{item.model or 'unknown'}/{item.reasoning_effort or 'unknown'}"
            for item in items
        )
        aggregate.update(
            {
                "session_ids": [item.session_id for item in items],
                "child_sessions": child_count,
                "elapsed_seconds": elapsed_seconds,
                "credits_per_minute": (
                    quota_credits / (elapsed_seconds / 60.0)
                    if quota_credits and elapsed_seconds >= 1
                    else None
                ),
                "average_input_per_round": (
                    aggregate["usage"]["input_tokens"] / aggregate["model_rounds"]
                    if aggregate["model_rounds"]
                    else 0.0
                ),
                "model_configs": dict(configs),
                "risk_level": risk.level,
                "risk_reasons": list(risk.reasons),
            }
        )
        families.append(aggregate)
    return sorted(
        families,
        key=lambda item: (
            float((item.get("credit_estimate") or {}).get("total") or 0),
            item["usage"]["total_tokens"],
        ),
        reverse=True,
    )


def build_report(
    codex_home: Path,
    *,
    start: dt.datetime,
    end: dt.datetime,
    top: int = 10,
) -> AuditReport:
    files = sorted((codex_home / "sessions").glob("**/*.jsonl"))
    sessions: list[SessionAudit] = []
    for path in files:
        session = scan_session(path, start=start, end=end)
        if session is not None:
            sessions.append(session)

    usage = _zero_usage()
    by_day: dict[str, dict[str, int]] = defaultdict(_zero_usage)
    for session in sessions:
        _add(usage, session.usage)
        for day, daily_usage in session.usage_by_day.items():
            _add(by_day[day], daily_usage)

    fanout: Counter[str] = Counter(
        session.parent_thread_id for session in sessions if session.parent_thread_id
    )
    parent_fanout = [
        {"parent_thread_id": parent, "children_in_window": count}
        for parent, count in fanout.most_common()
    ]

    by_provider = _group_by_provider(sessions)
    by_model = _group_by_model(sessions)
    task_families = _task_families(sessions)
    openai_sessions = [session for session in sessions if session.model_provider == "openai"]
    openai_aggregate = _aggregate_sessions(
        openai_sessions,
        label_key="provider",
        label="openai",
    )
    openai_credits = _credit_totals(openai_sessions)
    openai_quota = {
        **openai_aggregate,
        "credit_estimate": openai_credits,
        "unpriced_sessions": sum(
            session.credit_estimate is None for session in openai_sessions
        ),
    }

    warnings: list[str] = []
    if openai_credits["total"] >= 250:
        warnings.append(
            f"OpenAI 会话估算已消耗 {openai_credits['total']:.2f} credits；达到本地 critical 护栏。"
        )
    elif openai_credits["total"] >= 100:
        warnings.append(
            f"OpenAI 会话估算已消耗 {openai_credits['total']:.2f} credits；达到本地 warning 护栏。"
        )
    if openai_aggregate["model_rounds"] >= 100:
        warnings.append(
            f"OpenAI 会话产生 {openai_aggregate['model_rounds']} 个模型轮次；检查工具返回后自动续跑循环。"
        )
    image_count = sum(session.image_blocks for session in sessions)
    payload_bytes = sum(session.tool_output_bytes for session in sessions)
    if image_count >= 20:
        warnings.append(f"工具返回包含 {image_count} 个图像块；避免 original/high 图片回传。")
    if payload_bytes >= 5_000_000:
        warnings.append(f"工具返回序列化体积约 {_human_bytes(payload_bytes)}；请限制输出和截图。")
    if parent_fanout and parent_fanout[0]["children_in_window"] >= 3:
        warnings.append("检测到至少 3 个同源子代理；请使用最小 fork_turns、显式子模型和 task-family 总预算。")
    if task_families:
        top_family_credits = float(
            (task_families[0].get("credit_estimate") or {}).get("total") or 0
        )
        if top_family_credits >= 100:
            warnings.append(
                f"最大 task family 估算消耗 {top_family_credits:.2f} credits；单会话监控会漏掉父子并行总量。"
            )

    return AuditReport(
        window_start=start.isoformat(),
        window_end=end.isoformat(),
        codex_home=str(codex_home),
        sessions_scanned=len(files),
        sessions_with_usage=len(sessions),
        subagent_sessions=sum(session.subagent for session in sessions),
        root_sessions=sum(not session.subagent for session in sessions),
        usage=usage,
        by_day=dict(sorted(by_day.items())),
        by_provider=by_provider,
        by_model=by_model,
        task_families=task_families[:max(1, top)],
        openai_quota=openai_quota,
        top_sessions=sorted(sessions, key=lambda item: item.total_tokens, reverse=True)[:max(1, top)],
        parent_fanout=parent_fanout[:10],
        warnings=warnings,
    )


def _session_json(session: SessionAudit) -> dict[str, Any]:
    value = asdict(session)
    estimate = session.credit_estimate
    value["credit_estimate"] = asdict(estimate) if estimate is not None else None
    value["observed_seconds"] = session.observed_seconds
    value["average_input_per_round"] = session.average_input_per_round
    value["risk_level"] = session.risk_level
    value["risk_reasons"] = session.risk_reasons
    return value


def report_json(report: AuditReport) -> dict[str, Any]:
    value = asdict(report)
    value["top_sessions"] = [_session_json(session) for session in report.top_sessions]
    return value


def render_report(report: AuditReport) -> str:
    usage = report.usage
    input_tokens = usage["input_tokens"]
    cached = usage["cached_input_tokens"]
    cache_percent = cached * 100 / input_tokens if input_tokens else 0
    quota = report.openai_quota
    quota_usage = quota["usage"]
    credits = quota["credit_estimate"]
    lines = [
        "Codex token audit (read-only)",
        f"window: {report.window_start} → {report.window_end}",
        f"sessions: {report.sessions_with_usage}/{report.sessions_scanned} "
        f"with usage · roots {report.root_sessions} · subagents {report.subagent_sessions}",
        f"total: {_human_tokens(usage['total_tokens'])} · input {_human_tokens(input_tokens)} "
        f"· cached {_human_tokens(cached)} ({cache_percent:.0f}%) · "
        f"output {_human_tokens(usage['output_tokens'])} · reasoning {_human_tokens(usage['reasoning_output_tokens'])}",
        f"OpenAI quota: {credits['total']:.2f}cr "
        f"(uncached {credits['uncached_input']:.2f} + cached {credits['cached_input']:.2f} + output {credits['output']:.2f}) "
        f"· {_human_tokens(quota_usage['input_tokens'])} input · {quota['model_rounds']} model rounds · {quota['tool_outputs']} tool outputs",
        "",
        "Providers:",
    ]
    for provider in report.by_provider:
        provider_credits = provider.get("credit_estimate")
        credit_label = (
            f" · {provider_credits['total']:.2f}cr"
            if isinstance(provider_credits, dict)
            else " · quota excluded"
        )
        lines.append(
            f"- {provider['provider']}: {_human_tokens(provider['usage']['input_tokens'])} input "
            f"· {provider['model_rounds']} rounds · {provider['tool_outputs']} tools{credit_label}"
        )
    lines.extend(
        [
        "",
        "Top task families:",
        ]
    )
    for index, family in enumerate(report.task_families[:5], 1):
        family_credits = family.get("credit_estimate")
        credit_label = (
            f"{family_credits['total']:.2f}cr"
            if isinstance(family_credits, dict)
            else "quota excluded"
        )
        burn = family.get("credits_per_minute")
        burn_label = f" · {burn:.2f}cr/min" if isinstance(burn, (int, float)) else ""
        lines.append(
            f"{index:>2}. {credit_label} · {_human_tokens(family['usage']['input_tokens'])} input "
            f"· {family['model_rounds']} rounds · {family['tool_outputs']} tools "
            f"· {family['child_sessions']} children{burn_label} [{family['risk_level']}] "
            f"{family['root_session_id']}"
        )
    lines.extend(
        [
        "",
        "Top sessions:",
        ]
    )
    for index, session in enumerate(report.top_sessions, 1):
        risk = f" [{session.risk_level}]" if session.risk_level != "normal" else ""
        lines.append(
            f"{index:>2}. {_human_tokens(session.total_tokens):>8} "
            f"in {_human_tokens(session.usage['input_tokens']):>8} "
            f"rounds {session.token_updates:>4} "
            f"tools {session.tool_outputs:>4} "
            f"avg {_human_tokens(int(session.average_input_per_round)):>7}/r "
            f"credits {(session.quota_credits if session.quota_credits is not None else 0):>7.2f} "
            f"images {session.image_blocks:>3} "
            f"payload {_human_bytes(session.tool_output_bytes):>8}{risk} "
            f"{session.model_provider or 'unknown'}/{session.model or 'unknown'}/{session.reasoning_effort or 'unknown'} "
            f"{Path(session.path).name}"
        )
    if report.parent_fanout:
        lines.extend(["", "Largest subagent fan-out:"])
        for item in report.parent_fanout[:5]:
            lines.append(f"- {item['parent_thread_id']}: {item['children_in_window']} child sessions")
    if report.warnings:
        lines.extend(["", "Warnings:"])
        lines.extend(f"- {warning}" for warning in report.warnings)
    lines.extend(
        [
            "",
            "Note: credit estimates use the 2026-08-11 official token rate card for model_provider=openai only. Custom providers are excluded; fast mode, image generation, tool-specific charges, and future rate changes still require reconciliation with the OpenAI usage dashboard.",
        ]
    )
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only Codex session token audit")
    parser.add_argument("--codex-home", default=os.environ.get("CODEX_HOME") or "~/.codex")
    parser.add_argument("--days", type=float, default=7.0, help="trailing local-time window (default: 7)")
    parser.add_argument("--since", help="ISO timestamp or local YYYY-MM-DD")
    parser.add_argument("--until", help="ISO timestamp or local YYYY-MM-DD")
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    now = dt.datetime.now(tz=UTC)
    end = _parse_bound(args.until, default=now)
    start = _parse_bound(args.since, default=end - dt.timedelta(days=max(0.01, args.days)))
    if start >= end:
        print("since must be earlier than until", file=sys.stderr)
        return 2
    report = build_report(
        Path(args.codex_home).expanduser(),
        start=start,
        end=end,
        top=args.top,
    )
    if args.as_json:
        print(json.dumps(report_json(report), ensure_ascii=False, indent=2))
    else:
        print(render_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
