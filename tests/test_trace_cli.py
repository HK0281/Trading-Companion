"""Read-only viewer contracts, with fixed legacy input and real offline launcher output."""

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from scripts.verify import offline_network
from trading_companion import trace_cli as cli


LEGACY = Path(__file__).parent / "fixtures" / "turn_trace_v1.jsonl"
PRIVATE = "private-viewer-sentinel"


def record(number=1, *, version="trace-v2", executed=True, category="completed", outcome="research_completed",
           route="technical_research", total=100, execution=80):
    raw = json.loads(LEGACY.read_bytes())
    raw.update(schema_version=version, run_id=f"{number:032x}", started_at="2026-10-27T00:00:00Z",
               total_elapsed_ms=total, execution_started=executed, execution_elapsed_ms=execution if executed else None)
    raw["outcome"] = {"category": category, "code": outcome}
    raw["route"] = {**raw["route"], "kind": route} if route else None
    if not executed:
        raw["control"] = None
    if version == "trace-v2":
        raw["events"] = []
        for name in cli._KINDS.values():
            raw[name] = dict.fromkeys(cli.SUMMARY_KEYS, 0)
        for event in raw["models"]["events"]:
            event.update(event_id=event["sequence"], parent_event_id=None)
    return raw


def event(raw, *, kind="source", operation="daily_bars", status="ok", entered=True, parent=None, source="csv"):
    item = dict(event_id=10 + len(raw["events"]), parent_event_id=parent, kind=kind, operation=operation,
                phase="execution", start_ms=1, elapsed_ms=2, entered=entered, status=status, access="local",
                source=source, code="ok")
    raw["events"].append(item)
    summary = raw[cli._KINDS[kind]]
    summary["seen"] += 1
    summary["entered"] += int(entered)
    summary[status] += 1
    return item


def encoded(raw):
    return (json.dumps(raw, ensure_ascii=False) + "\n").encode("utf-8")


class TraceCliTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name)
        guard = offline_network()
        guard.__enter__()
        self.addCleanup(guard.__exit__, None, None, None)

    def write(self, *rows, name="turns.jsonl"):
        path = self.path / name
        path.write_bytes(b"".join(row if isinstance(row, bytes) else encoded(row) for row in rows))
        return path

    def invoke(self, *arguments, table=False):
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = cli.main(["--trace-dir", str(self.path), *([] if table else ["--format", "json"]), *arguments])
        self.assertEqual(stderr.getvalue(), "")
        return status, stdout.getvalue() if table else json.loads(stdout.getvalue())

    def test_fixed_v1_and_v2_mixture_does_not_invent_legacy_event_counts(self):
        new = record(2)
        event(new)
        self.write(LEGACY.read_bytes(), new)
        status, report = self.invoke()
        self.assertEqual(status, 0)
        self.assertEqual(report["versions"], {"trace-v1": 1, "trace-v2": 1})
        self.assertEqual(report["event_summary_coverage"], {"recorded_turns": 1, "unrecorded_turns": 1})
        self.assertEqual(report["event_summaries"]["sources"]["seen"], 1)
        self.assertEqual(report["models"]["attempted"], 6)
        self.assertEqual(report["models"]["reported_usage"]["total_tokens"], None)

    def test_evaluation_projection_keeps_only_safe_intent_and_event_metadata(self):
        raw = record()
        item = event(raw, status="stopped", source="private-viewer-source")
        item["code"] = "research_data_deadline_exceeded"
        item["private_payload"] = PRIVATE
        row = cli.parse_record(encoded(raw))
        self.assertEqual(row["intent"], raw["route"]["intent"])
        self.assertEqual(row["event_records"], [{"kind": "source", "operation": "daily_bars", "source": "unknown",
            "code": "research_data_deadline_exceeded", "status": "stopped", "entered": True,
            "event_id": item["event_id"], "parent_event_id": None, "elapsed_ms": 2}])
        self.assertNotIn(PRIVATE, json.dumps(row, default=str))
        raw["route"]["intent"] = PRIVATE
        self.assertEqual(cli.parse_record(encoded(raw))["intent"], "unknown")
        old = cli.parse_record(LEGACY.read_bytes())
        self.assertEqual(old["event_records"], [])

    def test_v1_only_table_and_json_explicitly_say_unrecorded(self):
        self.write(LEGACY.read_bytes())
        self.assertIsNone(self.invoke()[1]["event_summaries"])
        status, table = self.invoke(table=True)
        self.assertEqual(status, 0)
        self.assertIn("工具/来源/检查/修复: 未记录", table)
        self.assertIn("未记录 1 轮", table)
        self.assertIn("事件摘要覆盖 0/1", table)

    def test_default_includes_failure_cancellation_admission_and_replay(self):
        self.write(record(1), record(2, category="error", outcome="internal_error"),
                   record(3, category="stopped", outcome="turn_cancelled"),
                   record(4, category="rejected", outcome="session_busy", route=None, executed=False),
                   record(5, category="replayed", executed=False))
        report = self.invoke()[1]
        self.assertEqual(report["selected_turns"], 5)
        self.assertEqual(report["paths"]["unrouted"]["turns"], 1)
        self.assertEqual(set(report["categories"]), {"completed", "error", "stopped", "rejected", "replayed"})
        status, executed = self.invoke("--executed-only")
        self.assertEqual(status, 0)
        self.assertEqual(executed["selected_turns"], 3)
        self.assertEqual(executed["categories"]["error"], 1)

    def test_table_distinguishes_inapplicable_source_from_unknown_without_changing_json(self):
        raw = record()
        for kind, operation in (("tool", "analyze_daily_indicators"), ("check", "daily_records"),
                                ("repair", "intent_rule")):
            event(raw, kind=kind, operation=operation, source=None)
        event(raw, source=PRIVATE)
        event(raw, source="tencent")
        self.write(raw)

        status, table = self.invoke(table=True)
        self.assertEqual(status, 0)
        rows = [line.split() for line in table.splitlines()
                if line.startswith(("tool ", "check ", "repair ", "source "))]
        self.assertCountEqual([(row[0], row[2]) for row in rows],
                              [("tool", "—"), ("check", "—"), ("repair", "—"),
                               ("source", "unknown"), ("source", "tencent")])
        self.assertNotIn(PRIVATE, table)
        status, report = self.invoke()
        self.assertEqual(status, 0)
        self.assertCountEqual([(row["kind"], row["source"]) for row in report["retained_event_distribution"]],
                              [("tool", "unknown"), ("check", "unknown"), ("repair", "unknown"),
                               ("source", "unknown"), ("source", "tencent")])
        self.assertNotIn(PRIVATE, json.dumps(report))

    def test_table_labels_mixed_file_and_line_diagnostics_without_counting_them_as_skipped_turns(self):
        path = self.write(record(), b"{broken}\n")
        outputs = {}
        for mode in ("table", "json"):
            stdout, stderr = StringIO(), StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                status = cli.main(["--file", str(path), "--file", str(path), "--format", mode])
            self.assertEqual(status, 1)
            self.assertEqual(stderr.getvalue(), "")
            outputs[mode] = stdout.getvalue()
        first_line = outputs["table"].splitlines()[0]
        self.assertIn("样本 1/1", first_line)
        self.assertIn("读取诊断 2 | 变动文件 0", first_line)
        self.assertNotIn("跳过项", first_line)
        diagnostics = json.loads(outputs["json"])["diagnostics"]
        self.assertEqual(diagnostics["invalid_lines"], 1)
        self.assertEqual(diagnostics["duplicate_files"], 1)
        self.assertEqual(diagnostics["changed_files"], 0)

    def test_half_open_utc_time_route_and_turn_code_filters(self):
        first, middle, last = (record(n) for n in range(1, 4))
        first["started_at"] = "2026-10-27T00:00:00+08:00"
        middle["started_at"] = "2026-10-27T00:01:00+08:00"
        last["started_at"] = "2026-10-27T00:02:00+08:00"
        event(middle)["code"] = "source_unavailable"
        self.write(first, middle, last, record(4, route="screening"))
        report = self.invoke("--since", "2026-10-26T16:01:00Z", "--until", "2026-10-26T16:02:00Z",
                             "--route", "technical_research", "--code", "research_completed", "--list", "10")[1]
        self.assertEqual(report["selected_turns"], 1)
        self.assertEqual(report["listing"][0]["run_id"], middle["run_id"])
        self.assertEqual(report["listing"][0]["started_at"], "2026-10-26T16:01:00Z")
        self.assertEqual(self.invoke("--code", "research_data_failed")[1]["selected_turns"], 0)

    def test_repeated_route_mode_category_filters_and_latest_list_limit(self):
        rows = [record(1), record(2, route="screening"), record(3, route="company_research"), record(4)]
        rows[-1]["mode"] = "normal"
        self.write(*rows)
        report = self.invoke("--route", "technical_research", "--route", "screening", "--mode", "demo",
                             "--category", "completed", "--list", "1")[1]
        self.assertEqual(report["selected_turns"], 2)
        self.assertEqual(report["listing"][0]["run_id"], rows[1]["run_id"])

    def test_nearest_rank_and_missing_durations_have_separate_valid_n(self):
        rows = [record(n, total=n, execution=n * 2) for n in range(1, 21)]
        rows.append(record(21, total=None, execution=None))
        self.write(*rows)
        stats = self.invoke()[1]["paths"]["technical_research"]
        self.assertEqual(stats["turns"], 21)
        self.assertEqual(stats["total_elapsed_ms"], {"n": 20, "p50_ms": 10, "p95_ms": 19})
        self.assertEqual(stats["execution_elapsed_ms"], {"n": 20, "p50_ms": 20, "p95_ms": 38})
        self.write(record(total=0, execution=None))
        report = self.invoke()[1]
        self.assertEqual(report["overall"]["total_elapsed_ms"], {"n": 1, "p50_ms": 0, "p95_ms": 0})
        self.assertEqual(report["overall"]["execution_elapsed_ms"], {"n": 0, "p50_ms": None, "p95_ms": None})

    def test_model_totals_distributions_unknown_transport_and_partial_usage(self):
        first, second = record(1), record(2)
        first["models"].update(provider_requests_started=2, attempts_with_unknown_transport=1)
        first["models"]["reported_usage"] = dict(prompt_tokens=10, completion_tokens=None, total_tokens=None)
        second["models"].update(attempted=10, responses_received=10, responses_with_complete_usage=10,
                                 usage_complete_for_attempts=True)
        second["models"]["reported_usage"] = dict(prompt_tokens=20, completion_tokens=30, total_tokens=50)
        second.update(dropped_events=7, truncated=True)
        self.write(first, second)
        models = self.invoke()[1]["models"]
        self.assertEqual(models["attempted"], 13)
        self.assertEqual(models["provider_requests_started"], 2)
        self.assertEqual(models["responses_received"], 13)
        self.assertEqual(models["per_turn_distribution"]["attempted"], {"3": 1, "10+": 1})
        self.assertEqual(models["reported_usage"], dict(prompt_tokens=30, completion_tokens=30, total_tokens=50))
        self.assertEqual(models["usage_known_turns"], dict(prompt_tokens=2, completion_tokens=1, total_tokens=1))
        self.assertEqual(models["complete_usage_turns"], 1)
        self.assertEqual(models["attempts_with_unknown_transport"], 1)

    def test_summary_survives_truncation_and_details_do_not_claim_full_coverage(self):
        raw = record()
        event(raw, parent=8)
        raw["sources"]["seen"] += 6
        raw["sources"]["entered"] += 6
        raw["sources"]["ok"] += 6
        raw.update(dropped_events=6, truncated=True)
        self.write(raw)
        report = self.invoke()[1]
        self.assertEqual(report["event_summaries"]["sources"]["seen"], 7)
        self.assertEqual(report["retained_event_distribution"][0]["count"], 1)
        self.assertEqual(report["details"], dict(retained=4, observed=10, truncated_turns=1, missing_parents=1))
        table = self.invoke(table=True)[1]
        self.assertIn("保留 4/10", table)
        self.assertIn("仅代表保留明细", table)
        self.assertIn("缺少父事件 1", table)

    def test_unsafe_input_fields_and_errors_never_reach_output(self):
        raw = record()
        raw.update(question=PRIVATE, exception=PRIVATE, path=PRIVATE, secret=PRIVATE)
        raw["route"]["intent"] = PRIVATE
        raw["route"]["rule_id"] = PRIVATE
        raw["outcome"]["code"] = PRIVATE
        raw["models"]["events"][0]["requested_model"] = PRIVATE
        item = event(raw, source=PRIVATE)
        item.update(code=PRIVATE, draft=PRIVATE)
        self.write(raw, b'{"secret":"' + PRIVATE.encode() + b'"}\n')
        status, report = self.invoke("--list", "5")
        self.assertEqual(status, 1)
        self.assertNotIn(PRIVATE, json.dumps(report))
        self.assertEqual(report["outcome_codes"], {"other": 1})
        self.assertEqual(report["retained_event_distribution"][0]["source"], "unknown")
        self.assertNotIn(PRIVATE, self.invoke("--list", "5", table=True)[1])

    def test_invalid_types_counts_required_fields_and_links_are_rejected(self):
        mutations = [
            lambda raw: raw.update(total_elapsed_ms=True),
            lambda raw: raw.update(total_elapsed_ms=-1),
            lambda raw: raw.update(total_elapsed_ms=10**13),
            lambda raw: raw.update(total_elapsed_ms=1.5),
            lambda raw: raw.update(run_id=PRIVATE),
            lambda raw: raw.pop("sources"),
            lambda raw: raw["checks"].update(seen=1),
            lambda raw: raw["models"].update(attempted=False),
            lambda raw: raw["models"].update(responses_received=4),
            lambda raw: raw["models"].update(usage_complete_for_attempts=True),
            lambda raw: raw["models"]["events"][0].update(event_id=2),
            lambda raw: raw["models"]["events"][0].update(parent_event_id=2),
            lambda raw: raw.update(events=[{}] * 65),
            lambda raw: raw.update(extra=[0] * 65),
            lambda raw: raw.update(truncated=True),
            lambda raw: raw.update(extra={str(n): n for n in range(65)}),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                raw = record()
                mutate(raw)
                with self.assertRaises(cli.InvalidTrace):
                    cli.parse_record(encoded(raw))
        nested = record()
        nested["extra"] = [[[[[[[[[[[[[[[[[[0]]]]]]]]]]]]]]]]]]
        with self.assertRaises(cli.InvalidTrace):
            cli.parse_record(encoded(nested))

    def test_corrupt_unknown_duplicate_nonfinite_and_incomplete_lines_are_distinct(self):
        good = encoded(record())
        self.write(b"\xff\n", b"[]\n", b'{"schema_version":"future"}\n',
                   good.replace(b'"total_elapsed_ms": 100', b'"total_elapsed_ms": NaN'),
                   good.replace(b'"total_elapsed_ms": 100', b'"total_elapsed_ms": 1e999'),
                   good.replace(b'"total_elapsed_ms": 100', b'"total_elapsed_ms": 100, "total_elapsed_ms": 1'),
                   good, good, b'{"unfinished":')
        status, report = self.invoke()
        self.assertEqual(status, 1)
        self.assertEqual(report["selected_turns"], 1)
        diagnostics = report["diagnostics"]
        self.assertEqual(diagnostics["invalid_lines"], 5)
        self.assertEqual(diagnostics["unknown_versions"], 1)
        self.assertEqual(diagnostics["duplicate_runs"], 1)
        self.assertEqual(diagnostics["incomplete_lines"], 1)

    def test_long_lines_are_drained_and_exact_limit_is_accepted(self):
        good = encoded(record())
        exact = good[:-1] + b" " * (cli.MAX_LINE_BYTES - len(good)) + b"\n"
        self.write(b"x" * (cli.MAX_LINE_BYTES * 2) + b"\n", exact)
        status, report = self.invoke()
        self.assertEqual(status, 1)
        self.assertEqual(report["selected_turns"], 1)
        self.assertEqual(report["lines_read"], 2)
        self.assertEqual(report["diagnostics"]["oversized_lines"], 1)

    def test_only_four_fixed_names_are_read_and_inputs_are_unchanged(self):
        for number, name in enumerate(("turns.3.jsonl", "turns.2.jsonl", "turns.1.jsonl", "turns.jsonl",
                                       "turns.4.jsonl", "other.jsonl"), 1):
            self.write(record(number), name=name)
        nested = self.path / "nested"
        nested.mkdir()
        (nested / "turns.jsonl").write_bytes(encoded(record(99)))
        before = {path.relative_to(self.path): path.read_bytes() for path in self.path.rglob("*") if path.is_file()}
        self.assertEqual(self.invoke()[1]["selected_turns"], 4)
        after = {path.relative_to(self.path): path.read_bytes() for path in self.path.rglob("*") if path.is_file()}
        self.assertEqual(before, after)

    def test_deduplication_precedes_filter_and_oldest_valid_record_wins(self):
        self.write(record(1), name="turns.1.jsonl")
        self.write(record(1, route="screening"))
        status, report = self.invoke("--route", "screening")
        self.assertEqual(status, 1)
        self.assertEqual(report["selected_turns"], 0)
        self.assertEqual(report["diagnostics"]["duplicate_runs"], 1)

    def test_duplicate_explicit_files_are_detected(self):
        path = self.write(record())
        output = StringIO()
        with redirect_stdout(output):
            status = cli.main(["--file", str(path), "--file", str(path), "--format", "json"])
        self.assertEqual(status, 1)
        self.assertEqual(json.loads(output.getvalue())["diagnostics"]["duplicate_files"], 1)

    def test_empty_and_missing_directories_are_distinguished(self):
        self.assertEqual(self.invoke()[0], 0)
        self.assertEqual(self.invoke()[1]["selected_turns"], 0)
        missing = self.path / "missing"
        output = StringIO()
        with redirect_stdout(output):
            status = cli.main(["--trace-dir", str(missing), "--format", "json"])
        self.assertEqual(status, 1)
        self.assertEqual(json.loads(output.getvalue())["diagnostics"]["read_errors"], 1)
        self.assertFalse(missing.exists())

    def test_file_limits_and_global_resource_limits(self):
        oversized = self.path / "turns.1.jsonl"
        with oversized.open("wb") as stream:
            stream.seek(cli.MAX_FILE_BYTES)
            stream.write(b"x")
        self.write(record())
        status, report = self.invoke()
        self.assertEqual(status, 1)
        self.assertEqual(report["selected_turns"], 1)
        self.assertEqual(report["diagnostics"]["oversized_files"], 1)
        oversized.unlink()
        self.write(record(1), record(2))
        for limit, value in (("MAX_RUNS", 1), ("MAX_LINES", 1), ("MAX_INPUT_BYTES", 10)):
            with self.subTest(limit=limit), patch.object(cli, limit, value):
                status, report = self.invoke()
                self.assertEqual(status, 1)
                self.assertGreater(report["diagnostics"]["input_limit_reached"], 0)
                self.assertLessEqual(report["bytes_read"], value if limit == "MAX_INPUT_BYTES" else cli.MAX_INPUT_BYTES)

    def test_read_failure_is_fixed_diagnostic_without_exception_text(self):
        self.write(record())
        with patch.object(cli.os, "open", side_effect=OSError(PRIVATE)):
            status, report = self.invoke()
        self.assertEqual(status, 1)
        self.assertEqual(report["diagnostics"]["read_errors"], 1)
        self.assertNotIn(PRIVATE, json.dumps(report))

    def test_file_and_ancestor_symlink_or_junction_are_refused_before_open(self):
        self.write(record())
        original = Path.lstat
        for target in (self.path, self.path / "turns.jsonl"):
            for link_type in ("symlink", "junction"):
                def fake(path, *args, **kwargs):
                    info = original(path, *args, **kwargs)
                    if path == target:
                        return SimpleNamespace(st_mode=stat.S_IFLNK if link_type == "symlink" else info.st_mode,
                                               st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
                    return info
                with self.subTest(target=target, link_type=link_type), patch.object(Path, "lstat", fake), \
                        patch.object(cli.os, "open", side_effect=AssertionError("No follow")):
                    status, report = self.invoke()
                    self.assertEqual(status, 1)
                    self.assertEqual(report["diagnostics"]["unsafe_files"], 1)
        self.assertTrue(cli._unsafe(Path(r"\\server\share\trace.jsonl")))

    def test_nonregular_input_and_identity_change_during_open_are_refused(self):
        (self.path / "turns.jsonl").mkdir()
        self.assertEqual(self.invoke()[1]["diagnostics"]["unsafe_files"], 1)
        (self.path / "turns.jsonl").rmdir()
        self.write(record())
        info = SimpleNamespace(st_mode=stat.S_IFREG, st_dev=-1, st_ino=-1)
        with patch.object(cli.os, "fstat", return_value=info):
            self.assertEqual(self.invoke()[1]["diagnostics"]["unsafe_files"], 1)

    def test_concurrent_growth_and_short_read_are_reported_without_reading_new_data(self):
        path = self.write(record())
        parse = cli.parse_record
        def append(line):
            with path.open("ab") as stream:
                stream.write(encoded(record(2)))
            return parse(line)
        with patch.object(cli, "parse_record", side_effect=append):
            status, report = self.invoke()
        self.assertEqual(status, 1)
        self.assertEqual(report["selected_turns"], 1)
        self.assertEqual(report["diagnostics"]["changed_files"], 1)
        path = self.write(record(1), record(2))
        real_fdopen = os.fdopen
        def shortened(descriptor, mode):
            stream = real_fdopen(descriptor, mode)
            original = stream.readline
            count = [0]
            def readline(limit):
                count[0] += 1
                return original(limit) if count[0] == 1 else b""
            stream.readline = readline
            return stream
        with patch.object(cli.os, "fdopen", side_effect=shortened):
            status, report = self.invoke()
        self.assertEqual(status, 1)
        self.assertEqual(report["selected_turns"], 1)
        self.assertGreater(report["diagnostics"]["changed_files"], 0)

    def test_rotation_directory_replacement_and_same_size_edits_are_reported(self):
        path = self.write(record())
        original = Path.lstat
        calls = [0]
        def replaced(selected, *args, **kwargs):
            info = original(selected, *args, **kwargs)
            if selected == path:
                calls[0] += 1
                if calls[0] == 3:
                    return SimpleNamespace(st_dev=-1, st_ino=-1)
            return info
        reader = cli.TraceReader()
        with patch.object(Path, "lstat", replaced):
            self.assertEqual(len(list(reader.read([path]))), 1)
        self.assertEqual(reader.diagnostics["changed_files"], 1)
        original_stat = Path.stat
        count = [0]
        def changed_directory(selected, *args, **kwargs):
            info = original_stat(selected, *args, **kwargs)
            if selected == self.path and kwargs.get("follow_symlinks", True):
                count[0] += 1
                if count[0] == 2:
                    return SimpleNamespace(st_dev=-1, st_ino=-1)
            return info
        with patch.object(Path, "stat", changed_directory):
            self.assertEqual(self.invoke()[1]["diagnostics"]["changed_files"], 1)
        original_fstat = os.fstat
        count = [0]
        def modified(descriptor):
            info = original_fstat(descriptor)
            count[0] += 1
            if count[0] == 2:
                return SimpleNamespace(st_size=info.st_size, st_mtime_ns=info.st_mtime_ns + 1,
                                       st_ctime_ns=info.st_ctime_ns)
            return info
        with patch.object(cli.os, "fstat", side_effect=modified):
            self.assertEqual(self.invoke()[1]["diagnostics"]["changed_files"], 1)

    def test_invalid_arguments_return_two_without_echoing_values(self):
        cases = (["--unknown", PRIVATE], ["--since", PRIVATE], ["--since", "2026-10-27"],
                 ["--since", "2026-10-27T00:00:00+00:99"], ["--route", PRIVATE], ["--code", "source_unavailable"],
                 ["--since", "2026-10-28T00:00:00Z", "--until", "2026-10-27T00:00:00Z"],
                 ["--list", "1001"], ["--list", "-1"], ["--list", PRIVATE], ["--for", "json"],
                 ["--trace-dir", "bad\0path"])
        for arguments in cases:
            with self.subTest(arguments=arguments):
                output, error = StringIO(), StringIO()
                with redirect_stdout(output), redirect_stderr(error):
                    status = cli.main(["--trace-dir", str(self.path), *arguments])
                self.assertEqual(status, 2)
                self.assertEqual(output.getvalue(), "")
                self.assertEqual(error.getvalue(), "输入无效，请使用 --help 查看参数。\n")
        with redirect_stderr(StringIO()):
            self.assertEqual(cli.main([part for _ in range(5) for part in ("--file", "unused")]), 2)

    def test_subprocess_module_entry_and_stdlib_only_imports(self):
        self.write(record())
        command = [sys.executable, "-X", "utf8", "-B", "-m", "trading_companion.trace_cli",
                   "--trace-dir", str(self.path), "--format", "json"]
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["selected_turns"], 1)
        code = """
import builtins, runpy, sys
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name.split('.')[0] in ('socket', 'requests', 'dotenv', 'fastapi', 'uvicorn') or name.endswith(('local_env', 'api_service', 'api_server')):
        raise AssertionError('Service or network dependency imported')
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
sys.argv = ['trace_cli', '--trace-dir', sys.argv[1]]
runpy.run_module('trading_companion.trace_cli', run_name='__main__')
"""
        result = subprocess.run([sys.executable, "-X", "utf8", "-B", "-c", code, str(self.path)],
                                capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("路径", result.stdout)

    def test_actual_offline_demo_launcher_multiple_turns_parse_without_loss(self):
        from fastapi.testclient import TestClient
        from trading_companion.api_server import main as server_main
        from trading_companion.followup_demo import QUESTION
        destination = self.path / "demo"
        def serve(app, **kwargs):
            with TestClient(app, base_url="http://localhost") as client:
                session = client.post("/v1/sessions", json={}).json()
                url = "/v1/sessions/" + session["session_id"] + "/messages"
                headers = {"Authorization": "Bearer " + session["access_token"]}
                key = uuid4().hex
                for question, request in (("MACD是什么", uuid4().hex), (QUESTION, key), (QUESTION, key),
                                          ("重置", uuid4().hex)):
                    response = client.post(url, headers=headers, json={"question": question, "request_id": request})
                    self.assertEqual(response.status_code, 200)
                response = client.post(url, headers={"Authorization": "Bearer invalid"},
                                       json={"question": QUESTION, "request_id": uuid4().hex})
                self.assertEqual(response.status_code, 404)
        with patch.dict(os.environ, {}, clear=True), patch("uvicorn.run", side_effect=serve), \
                patch("trading_companion.local_env._read_local_values", side_effect=AssertionError("No credentials")), \
                redirect_stdout(StringIO()):
            self.assertEqual(server_main(["--demo", "--trace-dir", str(destination)]), 0)
        reader = cli.TraceReader()
        rows = list(reader.read(reader.directory(destination)))
        self.assertEqual(len(rows), 5)
        self.assertFalse(any(reader.diagnostics.values()))
        self.assertIn("replayed", {row["category"] for row in rows})
        self.assertIn("rejected", {row["category"] for row in rows})
        self.assertIn("unrouted", {row["route"] for row in rows})
        summary = cli.TraceSummary()
        for row in rows:
            summary.add(row)
        report = summary.report(reader)
        self.assertEqual(report["models"]["zero_attempt_turns"], 3)
        self.assertEqual(report["models"]["complete_usage_turns"], 3)
        self.assertEqual(report["models"]["attempted_complete_usage_turns"], 0)
        self.assertIn("有尝试且用量完整 0/2 轮", cli.format_table(report))


if __name__ == "__main__":
    unittest.main()
