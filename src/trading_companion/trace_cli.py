"""Bounded, read-only trace inspection. No service startup, environment or network access."""

import argparse
from collections import Counter
from datetime import datetime, timezone
import heapq
import json
from math import isfinite
import os
from pathlib import Path
import re
import stat
import sys
import unicodedata

from trading_companion.turn_trace import (
    _CODES, _EVENT_STATUSES, _KINDS, _OPERATIONS, _PHASES, _PROVIDERS, _SAFE_EVENT_CODES,
    _STOPS, _TOOLS, _USAGE_KEYS, _WORKFLOW_REASONS,
)


MAX_FILES = 4
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_INPUT_BYTES = MAX_FILES * MAX_FILE_BYTES
MAX_LINE_BYTES = 64 * 1024
MAX_LINES = 100_000
MAX_RUNS = 50_000
ROUTES = frozenset(("general_knowledge", "screening_edit", "screening", "screening_followup",
                    "company_research", "technical_research", "local_reply", "unrouted"))
CATEGORIES = frozenset(("completed", "clarification", "limited", "error", "rejected", "stopped", "interrupted", "replayed"))
MODES = frozenset(("normal", "development", "demo"))
SOURCES = frozenset(("tencent", "akshare_eastmoney", "csv", "research_snapshot", "eastmoney_quotes",
                     "exchange_metadata", "local_knowledge", "sina_news", "eastmoney_news", "eastmoney_company",
                     "eastmoney_financial", "eastmoney_announcements", "cninfo_announcements"))
SUMMARY_KEYS = ("seen", "entered", *_EVENT_STATUSES)
MODEL_COUNTS = ("attempted", "provider_requests_started", "responses_received",
                "responses_with_complete_usage", "attempts_with_unknown_transport")
DIAGNOSTICS = ("invalid_lines", "unknown_versions", "oversized_lines", "incomplete_lines", "duplicate_runs",
               "duplicate_files", "read_errors", "unsafe_files", "oversized_files", "changed_files", "input_limit_reached")
BUCKETS = ("0", "1", "2", "3", "4", "5", "6", "7-9", "10+")


class InvalidTrace(ValueError):
    pass


class UnknownVersion(ValueError):
    pass


class InvalidInput(ValueError):
    pass


def _count(value, *, nullable=False, positive=False):
    if nullable and value is None:
        return None
    if type(value) is not int or not (1 if positive else 0) <= value <= 10**12:
        raise InvalidTrace()
    return value


def _bool(value):
    if type(value) is not bool:
        raise InvalidTrace()
    return value


def _choice(value, choices, *, nullable=False):
    if nullable and value is None:
        return None
    if type(value) is not str or value not in choices:
        raise InvalidTrace()
    return value


def _identifier(value, *, nullable=False):
    if nullable and value is None:
        return None
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{32}", value) is None:
        raise InvalidTrace()
    return value


def _timestamp(value):
    if (type(value) is not str or len(value) > 40 or re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)", value) is None):
        raise InvalidTrace()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise InvalidTrace() from None


def _usage(value):
    if type(value) is not dict:
        raise InvalidTrace()
    return {key: _count(value[key], nullable=True) for key in _USAGE_KEYS}


def _pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise InvalidTrace()
        value[key] = item
    return value


def _integer(value):
    if len(value.lstrip("-")) > 13 or abs(int(value)) > 10**12:
        raise InvalidTrace()
    return int(value)


def _float(value):
    number = float(value)
    if not isfinite(number) or abs(number) > 10**12:
        raise InvalidTrace()
    return number


def _nonfinite(_):
    raise InvalidTrace()


def _shape(value, depth=0):
    if depth > 16:
        raise InvalidTrace()
    if type(value) is dict:
        if len(value) > 64:
            raise InvalidTrace()
        for item in value.values():
            _shape(item, depth + 1)
    elif type(value) is list:
        if len(value) > 64:
            raise InvalidTrace()
        for item in value:
            _shape(item, depth + 1)


def _safe_code(value, *, event=False, nullable=False):
    if nullable and value is None:
        return None
    if type(value) is not str:
        raise InvalidTrace()
    return value if value in (_SAFE_EVENT_CODES if event else _CODES) else "other"


def _control(value, executed):
    if not executed:
        if value is not None:
            raise InvalidTrace()
        return
    if type(value) is not dict:
        raise InvalidTrace()
    _choice(value["state"], ("active", "finished"))
    for key in ("elapsed_ms", "analyses_used", "model_calls_attempted", "responses_received", "observed_tokens",
                "responses_with_complete_consistent_usage"):
        _count(value[key], nullable=True)
    _bool(value["usage_complete_for_attempts"])
    _choice(value["stop_code"], _STOPS, nullable=True)
    _usage(value["reported_usage"])
    limits = value["limits"]
    if type(limits) is not dict:
        raise InvalidTrace()
    timeout = limits["timeout_seconds"]
    if type(timeout) not in (int, float) or not isfinite(timeout) or not 0 < timeout <= 10**12:
        raise InvalidTrace()
    for key in ("model_calls", "analyses", "observed_tokens"):
        _count(limits[key], nullable=True)


def _model_events(events, version, attempted):
    if type(events) is not list or len(events) > 64:
        raise InvalidTrace()
    links, sequences = [], set()
    for event in events:
        if type(event) is not dict:
            raise InvalidTrace()
        sequence = _count(event["sequence"], positive=True)
        if sequence in sequences or sequence > attempted:
            raise InvalidTrace()
        sequences.add(sequence)
        _count(event["start_ms"])
        _count(event["elapsed_ms"], nullable=True)
        _choice(event["provider"], _PROVIDERS, nullable=True)
        name = event["requested_model"]
        if name is not None and (type(name) is not str or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", name) is None):
            raise InvalidTrace()
        _choice(event["transport"], ("unknown", "mock", "http"))
        _count(event["provider_requests_started"])
        _bool(event["response_received"])
        _choice(event["response_status"], ("complete", "tool_calls", "refused", "incomplete"), nullable=True)
        _usage(event["usage"])
        _bool(event["usage_complete"])
        _safe_code(event["error_code"], nullable=True)
        if version == "trace-v2":
            links.append((_count(event["event_id"], positive=True), _count(event["parent_event_id"], nullable=True, positive=True)))
    return links


def _base_record(raw, version):
    if type(raw) is not dict:
        raise InvalidTrace()
    _choice(raw["entry"], ("api_message",))
    mode = _choice(raw["mode"], MODES)
    _choice(raw["provider"], _PROVIDERS, nullable=True)
    _bool(raw["history"])
    _choice(raw["phase"], _PHASES)
    _identifier(raw["original_run_id"], nullable=True)
    route = "unrouted"
    if raw["route"] is not None:
        if type(raw["route"]) is not dict:
            raise InvalidTrace()
        route = _choice(raw["route"]["kind"], ROUTES - {"unrouted"})
        for key in ("intent", "rule_id"):
            if type(raw["route"][key]) is not str or not 1 <= len(raw["route"][key]) <= 128:
                raise InvalidTrace()
    executed = _bool(raw["execution_started"])
    elapsed = _count(raw["execution_elapsed_ms"], nullable=True)
    if not executed and elapsed is not None:
        raise InvalidTrace()
    _control(raw["control"], executed)
    workflow = raw["workflow"]
    if workflow is not None:
        if type(workflow) is not dict:
            raise InvalidTrace()
        _choice(workflow["stop_reason"], _WORKFLOW_REASONS | {"other"})
        _count(workflow["tool_calls_used"], nullable=True)
    outcome = raw["outcome"]
    if type(outcome) is not dict:
        raise InvalidTrace()
    category = _choice(outcome["category"], CATEGORIES)
    models = raw["models"]
    if type(models) is not dict:
        raise InvalidTrace()
    counts = {key: _count(models[key]) for key in MODEL_COUNTS}
    complete = _bool(models["usage_complete_for_attempts"])
    if (counts["responses_received"] > counts["attempted"]
            or counts["responses_with_complete_usage"] > counts["responses_received"]
            or counts["attempts_with_unknown_transport"] > counts["attempted"]
            or complete != (counts["responses_with_complete_usage"] == counts["attempted"])):
        raise InvalidTrace()
    usage = _usage(models["reported_usage"])
    links = _model_events(models["events"], version, counts["attempted"])
    dropped = _count(raw["dropped_events"])
    truncated = _bool(raw["truncated"])
    if truncated != (dropped > 0):
        raise InvalidTrace()
    return {"run_id": _identifier(raw["run_id"]), "started_at": _timestamp(raw["started_at"]),
            "schema_version": version, "mode": mode, "route": route, "category": category,
            "code": _safe_code(outcome["code"]), "total_elapsed_ms": _count(raw["total_elapsed_ms"], nullable=True),
            "execution_started": executed, "execution_elapsed_ms": elapsed, "models": counts,
            "usage_complete": complete, "usage": usage, "summaries": None, "events": [],
            "retained": len(models["events"]), "observed": counts["attempted"], "truncated": truncated,
            "missing_parents": 0}, links


def _v1(raw):
    row, _ = _base_record(raw, "trace-v1")
    if row["observed"] != row["retained"] + raw["dropped_events"]:
        raise InvalidTrace()
    return row


def _v2(raw):
    row, links = _base_record(raw, "trace-v2")
    summaries = {}
    for name in _KINDS.values():
        summary = raw[name]
        if type(summary) is not dict:
            raise InvalidTrace()
        counters = {key: _count(summary[key]) for key in SUMMARY_KEYS}
        if sum(counters[key] for key in _EVENT_STATUSES) != counters["seen"] or counters["entered"] > counters["seen"] - counters["skipped"]:
            raise InvalidTrace()
        summaries[name] = counters
    events = raw["events"]
    if type(events) is not list or len(events) + row["retained"] > 64:
        raise InvalidTrace()
    normalized = []
    for event in events:
        if type(event) is not dict:
            raise InvalidTrace()
        kind = _choice(event["kind"], _KINDS)
        operation = _choice(event["operation"], _OPERATIONS | _TOOLS.keys())
        _choice(event["phase"], _PHASES)
        _count(event["start_ms"])
        _count(event["elapsed_ms"], nullable=True)
        _bool(event["entered"])
        _choice(event["status"], _EVENT_STATUSES)
        _choice(event["access"], ("network", "local", "snapshot", "unknown"))
        origin = event["source"]
        if origin is not None and type(origin) is not str:
            raise InvalidTrace()
        links.append((_count(event["event_id"], positive=True), _count(event["parent_event_id"], nullable=True, positive=True)))
        normalized.append((kind, operation, origin if origin in SOURCES else "unknown", _safe_code(event["code"], event=True)))
    identifiers = {identifier for identifier, _ in links}
    if len(identifiers) != len(links) or any(parent is not None and parent >= identifier for identifier, parent in links):
        raise InvalidTrace()
    row.update(summaries=summaries, events=normalized, retained=row["retained"] + len(events),
               observed=row["observed"] + sum(summary["seen"] for summary in summaries.values()),
               missing_parents=sum(parent is not None and parent not in identifiers for _, parent in links))
    if row["observed"] != row["retained"] + raw["dropped_events"]:
        raise InvalidTrace()
    for kind, name in _KINDS.items():
        retained = [event for event in events if event["kind"] == kind]
        if (len(retained) > summaries[name]["seen"]
                or sum(event["entered"] for event in retained) > summaries[name]["entered"]
                or any(sum(event["status"] == status for event in retained) > summaries[name][status]
                       for status in _EVENT_STATUSES)):
            raise InvalidTrace()
    return row


def parse_record(line):
    """Project two supported schemas to safe scalars; arbitrary input fields never reach output."""
    try:
        raw = json.loads(line.decode("utf-8"), object_pairs_hook=_pairs, parse_int=_integer,
                         parse_float=_float, parse_constant=_nonfinite)
        _shape(raw)
        if type(raw) is not dict:
            raise InvalidTrace()
        version = raw["schema_version"]
        if type(version) is not str:
            raise InvalidTrace()
        if version == "trace-v1":
            return _v1(raw)
        if version == "trace-v2":
            return _v2(raw)
        raise UnknownVersion()
    except (KeyError, TypeError, UnicodeError, RecursionError, OverflowError, ValueError) as error:
        if isinstance(error, UnknownVersion):
            raise
        raise InvalidTrace() from None


def _unsafe(path):
    # Do not follow file or ancestor symlinks/junctions, UNC paths or Windows device paths.
    if str(path).startswith(("\\\\", "//")):
        return True
    for item in (path, *path.parents):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
            return True
    return False


class TraceReader:
    def __init__(self):
        self.diagnostics = dict.fromkeys(DIAGNOSTICS, 0)
        self.bytes_read = self.lines_read = self.valid_turns = 0
        self._files, self._runs = set(), set()
        self._changed_paths = set()

    def _changed(self, path):
        if path not in self._changed_paths:
            self._changed_paths.add(path)
            self.diagnostics["changed_files"] += 1

    def directory(self, directory):
        path = Path(directory).absolute()
        try:
            if _unsafe(path):
                self.diagnostics["unsafe_files"] += 1
                return
            initial = path.stat()
            if not stat.S_ISDIR(initial.st_mode):
                raise OSError()
        except OSError:
            self.diagnostics["read_errors"] += 1
            return
        for name in ("turns.3.jsonl", "turns.2.jsonl", "turns.1.jsonl", "turns.jsonl"):
            selected = path / name
            try:
                selected.lstat()
            except FileNotFoundError:
                continue
            except OSError:
                self.diagnostics["read_errors"] += 1
                continue
            yield selected
        try:
            final = path.stat()
            if ((final.st_dev, final.st_ino) != (initial.st_dev, initial.st_ino)
                    or (final.st_mtime_ns, final.st_ctime_ns) != (initial.st_mtime_ns, initial.st_ctime_ns)):
                self._changed(path)
        except OSError:
            self._changed(path)

    def _lines(self, path):
        descriptor = None
        try:
            if _unsafe(path):
                self.diagnostics["unsafe_files"] += 1
                return
            initial = path.lstat()
            if not stat.S_ISREG(initial.st_mode):
                self.diagnostics["unsafe_files"] += 1
                return
            if initial.st_size > MAX_FILE_BYTES:
                self.diagnostics["oversized_files"] += 1
                return
            identity = (initial.st_dev, initial.st_ino)
            if identity in self._files:
                self.diagnostics["duplicate_files"] += 1
                return
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != identity:
                self.diagnostics["unsafe_files"] += 1
                return
            if (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) != (initial.st_size, initial.st_mtime_ns, initial.st_ctime_ns):
                self._changed(path)
            self._files.add(identity)
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = None
                remaining = initial.st_size
                while remaining:
                    if self.lines_read >= MAX_LINES or self.bytes_read >= MAX_INPUT_BYTES:
                        self.diagnostics["input_limit_reached"] += 1
                        return
                    limit = min(MAX_LINE_BYTES + 1, remaining, MAX_INPUT_BYTES - self.bytes_read)
                    line = stream.readline(limit)
                    if not line:
                        self._changed(path)
                        break
                    remaining -= len(line)
                    self.bytes_read += len(line)
                    self.lines_read += 1
                    if not line.endswith(b"\n") and remaining and self.bytes_read >= MAX_INPUT_BYTES:
                        self.diagnostics["input_limit_reached"] += 1
                        return
                    if len(line) > MAX_LINE_BYTES:
                        while not line.endswith(b"\n") and remaining > 0:
                            if self.bytes_read >= MAX_INPUT_BYTES:
                                self.diagnostics["input_limit_reached"] += 1
                                return
                            line = stream.readline(min(MAX_LINE_BYTES + 1, remaining, MAX_INPUT_BYTES - self.bytes_read))
                            if not line:
                                self._changed(path)
                                break
                            remaining -= len(line)
                            self.bytes_read += len(line)
                        self.diagnostics["oversized_lines"] += 1
                    elif not line.endswith(b"\n"):
                        self.diagnostics["incomplete_lines"] += 1
                    else:
                        yield line
                final = os.fstat(stream.fileno())
            current = path.lstat()
            expected = (initial.st_size, initial.st_mtime_ns, initial.st_ctime_ns)
            if ((current.st_dev, current.st_ino) != identity
                    or (final.st_size, final.st_mtime_ns, final.st_ctime_ns) != expected
                    or (current.st_size, current.st_mtime_ns, current.st_ctime_ns) != expected):
                self._changed(path)
        except OSError:
            self.diagnostics["read_errors"] += 1
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def read(self, paths):
        for filename in paths:
            if self.lines_read >= MAX_LINES or len(self._runs) >= MAX_RUNS:
                self.diagnostics["input_limit_reached"] += 1
                return
            for line in self._lines(Path(filename).absolute()):
                try:
                    row = parse_record(line)
                except UnknownVersion:
                    self.diagnostics["unknown_versions"] += 1
                    continue
                except InvalidTrace:
                    self.diagnostics["invalid_lines"] += 1
                    continue
                if row["run_id"] in self._runs:
                    self.diagnostics["duplicate_runs"] += 1
                    continue
                if len(self._runs) >= MAX_RUNS:
                    self.diagnostics["input_limit_reached"] += 1
                    return
                self._runs.add(row["run_id"])
                self.valid_turns += 1
                yield row


def _percentiles(values):
    ordered = sorted(values)
    return {"n": len(ordered), **{name: ordered[(percent * len(ordered) + 99) // 100 - 1] if ordered else None
                                  for name, percent in (("p50_ms", 50), ("p95_ms", 95))}}


class _PathStats:
    def __init__(self):
        self.n = self.attempted = self.sent = 0
        self.total, self.execution = [], []

    def add(self, row):
        self.n += 1
        if row["total_elapsed_ms"] is not None:
            self.total.append(row["total_elapsed_ms"])
        if row["execution_elapsed_ms"] is not None:
            self.execution.append(row["execution_elapsed_ms"])
        self.attempted += row["models"]["attempted"]
        self.sent += row["models"]["provider_requests_started"]

    def report(self):
        return {"turns": self.n, "total_elapsed_ms": _percentiles(self.total),
                "execution_elapsed_ms": _percentiles(self.execution), "model_attempted": self.attempted,
                "provider_requests_started": self.sent}


class TraceSummary:
    def __init__(self, *, list_limit=0):
        self.list_limit, self._listing = list_limit, []
        self.turns = self.v2_turns = self.truncated = self.retained = self.observed = self.missing_parents = 0
        self.paths, self.overall = {}, _PathStats()
        self.modes, self.versions, self.categories, self.codes = (Counter() for _ in range(4))
        self.models = dict.fromkeys(MODEL_COUNTS, 0)
        self.histograms = {key: Counter() for key in MODEL_COUNTS[:3]}
        self.usage, self.usage_turns = dict.fromkeys(_USAGE_KEYS), dict.fromkeys(_USAGE_KEYS, 0)
        self.complete_usage_turns = 0
        self.zero_attempt_turns = self.attempted_complete_usage_turns = 0
        self.summaries = {name: dict.fromkeys(SUMMARY_KEYS, 0) for name in _KINDS.values()}
        self.details = Counter()

    def add(self, row):
        self.turns += 1
        self.modes[row["mode"]] += 1
        self.versions[row["schema_version"]] += 1
        self.categories[row["category"]] += 1
        self.codes[row["code"]] += 1
        self.paths.setdefault(row["route"], _PathStats()).add(row)
        self.overall.add(row)
        self.truncated += int(row["truncated"])
        self.retained += row["retained"]
        self.observed += row["observed"]
        self.missing_parents += row["missing_parents"]
        self.complete_usage_turns += int(row["usage_complete"])
        self.zero_attempt_turns += int(row["models"]["attempted"] == 0)
        self.attempted_complete_usage_turns += int(row["usage_complete"] and row["models"]["attempted"] > 0)
        for key, value in row["models"].items():
            self.models[key] += value
            if key in self.histograms:
                bucket = str(value) if value <= 6 else "7-9" if value <= 9 else "10+"
                self.histograms[key][bucket] += 1
        for key, value in row["usage"].items():
            if value is not None:
                self.usage[key] = (self.usage[key] or 0) + value
                self.usage_turns[key] += 1
        if row["summaries"] is not None:
            self.v2_turns += 1
            for name, summary in row["summaries"].items():
                for key, value in summary.items():
                    self.summaries[name][key] += value
        self.details.update(row["events"])
        if self.list_limit:
            listing = {key: row[key] for key in ("run_id", "route", "category", "code", "total_elapsed_ms", "execution_elapsed_ms")}
            listing["started_at"] = row["started_at"].isoformat().replace("+00:00", "Z")
            item = (row["started_at"], row["run_id"], listing)
            if len(self._listing) < self.list_limit:
                heapq.heappush(self._listing, item)
            elif item[:2] > self._listing[0][:2]:
                heapq.heapreplace(self._listing, item)

    def report(self, reader):
        partial = any(reader.diagnostics.values())
        return {"status": "partial" if partial else "ok", "selected_turns": self.turns,
                "valid_unique_turns": reader.valid_turns, "bytes_read": reader.bytes_read, "lines_read": reader.lines_read,
                "modes": dict(self.modes), "versions": dict(self.versions), "categories": dict(self.categories), "outcome_codes": dict(self.codes),
                "event_summary_coverage": {"recorded_turns": self.v2_turns, "unrecorded_turns": self.turns - self.v2_turns},
                "details": {"retained": self.retained, "observed": self.observed, "truncated_turns": self.truncated, "missing_parents": self.missing_parents},
                "paths": {name: item.report() for name, item in sorted(self.paths.items())}, "overall": self.overall.report(),
                "models": {**self.models, "per_turn_distribution": {key: dict(value) for key, value in self.histograms.items()},
                           "reported_usage": self.usage, "usage_known_turns": self.usage_turns,
                           "complete_usage_turns": self.complete_usage_turns, "zero_attempt_turns": self.zero_attempt_turns,
                           "attempted_complete_usage_turns": self.attempted_complete_usage_turns},
                "event_summaries": self.summaries if self.v2_turns else None,
                "retained_event_distribution": [{"kind": kind, "operation": operation, "source": source, "code": value, "count": count}
                                                 for (kind, operation, source, value), count in sorted(self.details.items())],
                "listing": [item[2] for item in sorted(self._listing, reverse=True)], "diagnostics": dict(reader.diagnostics)}


def _matches(row, args):
    return ((args.since is None or row["started_at"] >= args.since)
            and (args.until is None or row["started_at"] < args.until)
            and (not args.route or row["route"] in args.route) and (not args.code or row["code"] in args.code)
            and (not args.mode or row["mode"] in args.mode) and (not args.category or row["category"] in args.category)
            and (not args.executed_only or row["execution_started"]))


def _table(headers, rows):
    def width(value):
        return sum(2 if unicodedata.east_asian_width(character) in ("W", "F") else 1 for character in value)
    data = [list(map(str, headers)), *[list(map(str, row)) for row in rows]]
    widths = [max(width(row[column]) for row in data) for column in range(len(headers))]
    lines = ["  ".join(value + " " * (size - width(value)) for value, size in zip(row, widths)).rstrip() for row in data]
    return "\n".join((lines[0], "  ".join("-" * size for size in widths), *lines[1:]))


def format_table(report):
    def counts(value):
        return ",".join(f"{key}:{count}" for key, count in sorted(value.items())) or "无"
    def duration(value):
        return f"{value['p50_ms']}/{value['p95_ms']} (n={value['n']})" if value["n"] else "未记录 (n=0)"
    diagnostics = report["diagnostics"]
    coverage, details = report["event_summary_coverage"], report["details"]
    lines = [f"样本 {report['selected_turns']}/{report['valid_unique_turns']} | 模式 {counts(report['modes'])} | 版本 {counts(report['versions'])} | "
             f"事件摘要覆盖 {coverage['recorded_turns']}/{report['selected_turns']} | 跳过项 {sum(value for key, value in diagnostics.items() if key != 'changed_files')} | 变动文件 {diagnostics['changed_files']}"]
    rows = [(name, item["turns"], duration(item["total_elapsed_ms"]), duration(item["execution_elapsed_ms"]),
             item["model_attempted"], item["provider_requests_started"]) for name, item in report["paths"].items()]
    overall = report["overall"]
    rows.append(("合计", overall["turns"], duration(overall["total_elapsed_ms"]), duration(overall["execution_elapsed_ms"]),
                 overall["model_attempted"], overall["provider_requests_started"]))
    lines.append(_table(("路径", "样本数", "总耗时 p50/p95 ms", "执行耗时 p50/p95 ms", "模型尝试总数", "发送总数"), rows))
    lines.extend((f"终态: {counts(report['categories'])}", f"结果码: {counts(report['outcome_codes'])}"))
    models = report["models"]
    lines.append(f"模型总计: 尝试 {models['attempted']}，开始发送 {models['provider_requests_started']}，有效回复 {models['responses_received']}。")
    for key, label in (("attempted", "尝试"), ("provider_requests_started", "发送"), ("responses_received", "有效回复")):
        histogram = models["per_turn_distribution"][key]
        lines.append(f"模型{label}每轮分布: " + (", ".join(f"{bucket}次={histogram[bucket]}轮" for bucket in BUCKETS if bucket in histogram) or "无"))
    lines.append(f"模型传输未知尝试: {models['attempts_with_unknown_transport']}；用量完整轮数: {models['complete_usage_turns']}/{report['selected_turns']}；未知用量不补零，已报告 token 不是账单。")
    lines.append(f"用量完整定义包含零尝试 {models['zero_attempt_turns']} 轮；有尝试且用量完整 {models['attempted_complete_usage_turns']}/{report['selected_turns'] - models['zero_attempt_turns']} 轮。")
    lines.append("已报告用量: " + "，".join(
        f"{key}={models['reported_usage'][key] if models['reported_usage'][key] is not None else '未记录'}"
        f" (已知 {models['usage_known_turns'][key]}/{report['selected_turns']} 轮)" for key in _USAGE_KEYS))
    lines.append(f"事件: 摘要覆盖 {coverage['recorded_turns']} 轮，未记录 {coverage['unrecorded_turns']} 轮；保留 {details['retained']}/{details['observed']} 条可观测明细；截断 {details['truncated_turns']} 轮；缺少父事件 {details['missing_parents']} 条。")
    if coverage["unrecorded_turns"]:
        lines.append("v1 的明细分母仅含模型；工具/来源/检查/修复没有记录，不能据此声称事件完整。")
    if report["event_summaries"] is None:
        lines.append("工具/来源/检查/修复: 未记录。")
    else:
        lines.append(_table(("事件类别", "观察入口", "实际进入", "正常", "受限", "拒绝", "失败", "停止", "跳过", "未结束"),
                            [(name, *[value[key] for key in SUMMARY_KEYS]) for name, value in report["event_summaries"].items()]))
    distribution = report["retained_event_distribution"]
    if distribution:
        lines.append("保留事件分布（仅代表保留明细；最多显示前20组）:")
        lines.append(_table(("类别", "操作", "来源", "结果码", "次数"),
                            [(item["kind"], item["operation"], item["source"], item["code"], item["count"])
                             for item in sorted(distribution, key=lambda item: (-item["count"], item["kind"], item["operation"], item["code"]))[:20]]))
        if len(distribution) > 20:
            lines.append(f"另有 {len(distribution) - 20} 组，可用 --format json 查看。")
    if report["listing"]:
        lines.append("最近运行:")
        lines.append(_table(("运行号", "UTC", "路径", "终态", "结果码", "总耗时 ms"),
                            [(item["run_id"], item["started_at"], item["route"], item["category"], item["code"],
                              item["total_elapsed_ms"] if item["total_elapsed_ms"] is not None else "未记录") for item in report["listing"]]))
    if any(diagnostics.values()):
        lines.append("读取不完整: " + counts({key: value for key, value in diagnostics.items() if value}))
    lines.append("本次为读取时可用的记录；活动文件可能轮换。demo/开发样本不能代表线上耗时；子调用耗时可能重叠。")
    return "\n".join(lines)


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise InvalidInput()


def main(argv=None):
    parser = _Parser(prog="trace_cli", allow_abbrev=False, description="只读查看本地运行记录；不请求模型或行情，不修改输入文件。")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--trace-dir", help="已有追踪目录，只读取四个固定文件名")
    source.add_argument("--file", action="append", help="已有 JSONL 文件，可重复，最多四个")
    parser.add_argument("--since", help="含起点、带时区的 ISO 时间")
    parser.add_argument("--until", help="不含终点、带时区的 ISO 时间")
    parser.add_argument("--route", action="append", choices=sorted(ROUTES))
    parser.add_argument("--code", action="append", help="整轮结果码，可重复；其他类别的细码不用于此筛选")
    parser.add_argument("--mode", action="append", choices=sorted(MODES))
    parser.add_argument("--category", action="append", choices=sorted(CATEGORIES))
    parser.add_argument("--executed-only", action="store_true", help="只统计 execution_started=true，仍保留执行失败")
    parser.add_argument("--list", type=int, default=0, dest="list_limit", help="列出最近 N 个运行号，0 到 1000，默认0")
    parser.add_argument("--format", choices=("table", "json"), default="table")
    try:
        args = parser.parse_args(argv)
        if (args.file and len(args.file) > MAX_FILES) or not 0 <= args.list_limit <= 1000:
            raise InvalidInput()
        if args.code and any(value not in _CODES for value in args.code):
            raise InvalidInput()
        if any("\0" in value for value in (args.file or [args.trace_dir])):
            raise InvalidInput()
        args.since = _timestamp(args.since) if args.since is not None else None
        args.until = _timestamp(args.until) if args.until is not None else None
        if args.since is not None and args.until is not None and args.since >= args.until:
            raise InvalidInput()
    except (InvalidInput, InvalidTrace):
        print("输入无效，请使用 --help 查看参数。", file=sys.stderr)
        return 2
    reader, summary = TraceReader(), TraceSummary(list_limit=args.list_limit)
    paths = reader.directory(args.trace_dir) if args.trace_dir is not None else args.file
    for row in reader.read(paths):
        if _matches(row, args):
            summary.add(row)
    report = summary.report(reader)
    print(json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2) if args.format == "json" else format_table(report))
    return 1 if report["status"] == "partial" else 0


if __name__ == "__main__":
    raise SystemExit(main())
