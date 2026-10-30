"""Trace real offline execution boundaries without owning retries, budgets or publication."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import copy_context
from dataclasses import replace
import ast
from datetime import date
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Event
import unittest
from unittest.mock import Mock, patch

from scripts.verify import offline_network
from trading_companion.company_research import ResearchDataService
from trading_companion.company_research_contracts import ResearchDataRequest
from trading_companion.data_source import CsvDailyBarSource
from trading_companion.development_fixtures import REFERENCE_DATE, load_development_fixtures
from trading_companion.errors import DataSourceError
from trading_companion.knowledge_retrieval import default_knowledge_retriever
from trading_companion.model import ModelCallError, TokenUsage
from trading_companion.open_knowledge import answer_general_knowledge
from trading_companion.product_answer import synthesize_legacy_product_answer as synthesize_product_answer
from trading_companion.research_intent import prepare_research_question
from trading_companion.research_tool import TOOL_NAME
from trading_companion.tool_model import FakeToolModel, ToolModelResponse
from trading_companion.turn_control import ControlledToolModel, TurnControl, TurnLimits, TurnStopped, bind_turn
from trading_companion.turn_trace import (
    JsonlTraceSink, TraceLimits, TurnTrace, _CODES, _EVENT_CODES, _CURRENT, trace_call, trace_event, trace_message,
    trace_model_attempt, trace_model_response, trace_phase,
)
from trading_companion.workflow_tools import ADDITIONAL_TOOL_NAME, KNOWLEDGE_TOOL_NAME, SNAPSHOT_TOOL_NAME
from tests.test_company_research import CUTOFF, NOW, financial, response as source_response
from tests.test_product_answer import QUESTION, make_bundle, response
from tests.test_product_answer_repair import BAD_ASSOCIATION, GOOD_ASSOCIATION, ScriptedModel
from tests.test_research_intent import QUESTION as INTENT_QUESTION, interpretation, intent_arguments
from tests.test_turn_trace import read_records
from tests.test_workflow_tools import append, bars, call, fixture_executor, history, request
from trading_companion.multi_contracts import IndicatorSpec


PRIVATE = "trace-v2-private-sentinel"


class TraceEventTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "traces"
        self.sink = JsonlTraceSink(self.path)
        network = offline_network()
        network.__enter__()
        self.addCleanup(network.__exit__, None, None, None)

    @contextmanager
    def scope(self, sink=None):
        with trace_message(self.sink if sink is None else sink, mode="demo", provider="fake", history=False):
            trace_phase("execution")
            yield

    def record(self):
        return read_records(self.path)[-1]

    def test_local_date_repair_records_only_its_fixed_source_and_outcome(self):
        _, directory = load_development_fixtures()
        question = "分析平安银行近三个月的走势，只看3日均线"
        values = intent_arguments(expression=None, indicators=["3日均线"])
        with self.scope():
            result = prepare_research_question(
                question, model=FakeToolModel([interpretation(values)]), directory=directory,
                source="csv", reference_date=REFERENCE_DATE, development=True)
        self.assertEqual(result.status, "ready", result.message)
        events = self.events("repair", "local_date_expression")
        self.assertEqual([(event["status"], event["code"]) for event in events],
                         [("ok", "local_date_recognized")])
        encoded = json.dumps(self.record(), ensure_ascii=False)
        for raw in (question, "平安银行", "近三个月", "3日均线"):
            self.assertNotIn(raw, encoded)

    def events(self, kind=None, operation=None):
        return [event for event in self.record()["events"]
                if (kind is None or event["kind"] == kind) and (operation is None or event["operation"] == operation)]

    def primary(self, executor=None):
        executor = executor or fixture_executor(request(indicators=(IndicatorSpec("sma", window=3),)))
        proposal = call(confirmed=executor._request)
        with patch.object(CsvDailyBarSource, "read_daily_bars", autospec=True, return_value=bars()) as read:
            output = executor.execute(proposal, messages=history())
        self.assertEqual(read.call_count, 1)
        return executor, append(history(), proposal, output)

    def test_nested_primary_counts_one_tool_and_snapshot_supplement_does_not_refetch(self):
        with self.scope():
            executor, messages = self.primary()
            for index, (name, arguments) in enumerate((
                (SNAPSHOT_TOOL_NAME, {"section": "overview"}),
                (KNOWLEDGE_TOOL_NAME, {"topics": ["sma"]}),
                (ADDITIONAL_TOOL_NAME, {"indicators": [{"kind": "sma", "window": 5}]}),
            )):
                proposal = call(name, arguments, call_id=f"helper_{index}")
                output = executor.execute(proposal, messages=messages)
                self.assertNotEqual(output.result.get("status"), "rejected")
                messages = append(messages, proposal, output)
        record = self.record()
        self.assertEqual((record["tools"]["seen"], record["tools"]["entered"]), (4, 4))
        self.assertEqual({event["operation"] for event in self.events("tool")},
                         {TOOL_NAME, SNAPSHOT_TOOL_NAME, KNOWLEDGE_TOOL_NAME, ADDITIONAL_TOOL_NAME})
        reads = self.events("source")
        self.assertEqual([(event["source"], event["access"]) for event in reads],
                         [("csv", "local"), ("research_snapshot", "snapshot")])
        tools = {event["event_id"]: event["operation"] for event in self.events("tool")}
        self.assertEqual([tools[event["parent_event_id"]] for event in reads], [TOOL_NAME, ADDITIONAL_TOOL_NAME])
        self.assertGreater(record["checks"]["seen"], 4)
        self.assertEqual(executor.tool_calls_used, 4)

    def test_scope_and_duplicate_rejections_never_count_as_entered_tools_or_source_reads(self):
        with self.scope():
            executor = fixture_executor()
            proposal = call(arguments={"private": PRIVATE})
            rejected = executor.execute(proposal, messages=history())
        self.assertEqual(rejected.result["code"], "proposal_request_mismatch")
        self.assertEqual((self.record()["tools"]["seen"], self.record()["tools"]["entered"],
                          self.record()["sources"]["seen"]), (1, 0, 0))
        self.assertEqual(self.events("check", "tool_scope")[0]["status"], "rejected")
        with self.scope():
            executor, messages = self.primary()
            proposal = call(confirmed=executor._request, call_id="duplicate")
            output = executor.execute(proposal, messages=messages)
        self.assertEqual(output.result["code"], "duplicate_request")
        self.assertEqual((self.record()["tools"]["seen"], self.record()["tools"]["entered"]), (2, 1))
        self.assertEqual(self.record()["sources"]["seen"], 1)

    def test_tool_budget_stop_happens_without_execution_or_read(self):
        control = TurnControl(TurnLimits(analyses=0))
        control.start()
        with self.scope(), bind_turn(control):
            with self.assertRaises(TurnStopped):
                fixture_executor().execute(call(), messages=history())
        record = self.record()
        self.assertEqual((record["tools"]["entered"], record["sources"]["seen"]), (0, 0))
        self.assertEqual(self.events("tool")[0]["code"], "analysis_budget_exhausted")

    def test_source_failure_and_late_cancel_keep_the_read_without_changing_tool_semantics(self):
        with self.scope(), patch.object(CsvDailyBarSource, "read_daily_bars", autospec=True,
                                      side_effect=DataSourceError(PRIVATE)) as read:
            executor = fixture_executor()
            executor.execute(call(), messages=history())
        self.assertEqual(read.call_count, 1)
        self.assertEqual(executor.primary_execution.full_result.data_status, "source_error")
        self.assertEqual(self.events("source")[0]["code"], "source_unavailable")
        control = TurnControl()
        control.start()
        def late(_):
            control.cancel()
            return bars()
        with self.scope(), bind_turn(control), patch.object(CsvDailyBarSource, "read_daily_bars", autospec=True, side_effect=late):
            with self.assertRaises(TurnStopped):
                fixture_executor().execute(call(), messages=history())
        self.assertEqual(self.events("source")[0]["status"], "ok")
        self.assertEqual(self.events("tool")[0]["code"], "turn_cancelled")

    def test_company_sections_keep_source_identity_skip_and_failure_without_double_counting_batch(self):
        session = Mock()
        session.request.side_effect = [source_response(financial()), source_response({}, status=503)]
        service = ResearchDataService(session=session, clock=lambda: NOW, news_provider="eastmoney")
        selected = ResearchDataRequest("300308.SZ", "中际旭创", CUTOFF, date(2026, 7, 1), allow_current_background=False)
        with self.scope():
            batch = trace_call("source", "company_collect", service.collect, selected, kinds=("profile", "financial", "announcements"))
        self.assertEqual(session.request.call_count, 2)
        self.assertEqual([section.status for section in batch.source_results], ["unavailable", "available", "unavailable"])
        events = self.events("source")
        self.assertEqual([event["source"] for event in events], ["eastmoney_company", "eastmoney_financial", "eastmoney_announcements"])
        self.assertEqual([event["code"] for event in events], ["current_background_not_allowed", "bounded_evidence_available", "source_http_error"])
        self.assertEqual((self.record()["sources"]["seen"], self.record()["sources"]["entered"], self.record()["sources"]["skipped"]), (3, 2, 1))

    def test_unknown_source_is_not_guessed_from_object_attributes_or_response_text(self):
        class Unknown:
            source_name = PRIVATE
            def read(self):
                return [PRIVATE]
        with self.scope():
            result = trace_call("source", "daily_bars", Unknown().read)
        event = self.events("source")[0]
        self.assertEqual(result, [PRIVATE])
        self.assertEqual((event["source"], event["access"]), (None, "unknown"))
        self.assertNotIn(PRIVATE, json.dumps(self.record()))

    def test_event_source_private_exception_reasons_are_all_explicitly_registered(self):
        root = Path(__file__).resolve().parents[1] / "src" / "trading_companion"
        reasons = set()
        for filename, exception in (("company_research.py", "_Rejected"), ("company_news_sina.py", "_Unavailable"),
                                    ("product_answer.py", "_Invalid"), ("research_response.py", "_Invalid"),
                                    ("collection_execution.py", "CollectionUnavailable")):
            tree = ast.parse((root / filename).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == exception
                        and node.args and isinstance(node.args[0], ast.Constant) and type(node.args[0].value) is str):
                    reasons.add(node.args[0].value)
        self.assertGreater(len(reasons), 35)
        self.assertFalse(reasons - _CODES - _EVENT_CODES)

    def test_actual_parallel_company_collector_keeps_parent_and_source_counts(self):
        from trading_companion.collection_execution import CollectionExecutor
        from tests.test_company_collection_concurrency import FakeTransport
        pool = CollectionExecutor()
        self.addCleanup(pool.close, wait=True)
        barrier = Barrier(3)
        def wait_for_peers(kind):
            barrier.wait(5)
        http = FakeTransport(wait_for_peers)
        service = ResearchDataService(session_factory=http.factory, executor=pool,
                                     clock=lambda: NOW, news_provider="eastmoney")
        request = ResearchDataRequest("300308.SZ", "中际旭创", CUTOFF, allow_current_background=True)
        with self.scope(), trace_event("tool", "research"):
            batch = service.collect(request, ("profile", "financial", "announcements"))
        self.assertEqual(batch.status, "ok")
        sources, parent = self.events("source"), self.events("tool")[0]
        self.assertEqual(len(sources), 3)
        self.assertEqual({item["parent_event_id"] for item in sources}, {parent["event_id"]})
        self.assertEqual({item["source"] for item in sources},
                         {"eastmoney_company", "eastmoney_financial", "eastmoney_announcements"})
        self.assertEqual(self.record()["sources"]["seen"], 3)
        self.assertEqual(http.maximum, 3)

    def test_independent_turns_share_pool_but_not_controls_or_trace_parents(self):
        from trading_companion.collection_execution import CollectionExecutor
        from trading_companion.turn_control import _ACTIVE
        from tests.test_collection_execution import BackgroundCall
        from tests.test_company_collection_concurrency import FakeTransport
        pool, barrier = CollectionExecutor(), Barrier(3)
        self.addCleanup(pool.close, wait=True)
        def run():
            control = TurnControl()
            control.start()
            def handler(kind):
                self.assertIs(_ACTIVE.get(), control)
                barrier.wait(5)
            http = FakeTransport(handler)
            service = ResearchDataService(session_factory=http.factory, executor=pool,
                                         clock=lambda: NOW, news_provider="eastmoney")
            with self.scope(), bind_turn(control), trace_event("repair", "model_rewrite"):
                result = service.collect(ResearchDataRequest("300308.SZ", "中际旭创", CUTOFF,
                    allow_current_background=True), ("profile", "financial", "announcements"))
            self.assertEqual(result.status, "ok")
            control.finish()
        first, second = BackgroundCall(run), BackgroundCall(run)
        first.result()
        second.result()
        records = read_records(self.path)
        self.assertEqual(len({record["run_id"] for record in records}), 2)
        for record in records:
            reads = [item for item in record["events"] if item["kind"] == "source"]
            parent = next(item for item in record["events"] if item["kind"] == "repair")
            self.assertEqual(len(reads), 3)
            self.assertEqual({item["parent_event_id"] for item in reads}, {parent["event_id"]})
            self.assertEqual(len({item["event_id"] for item in record["events"]}), 4)

    def test_multi_company_source_deadline_is_stopped_separately_from_failed_and_freezes_late_work(self):
        from copy import deepcopy
        import requests
        from trading_companion.collection_execution import CollectionExecutor
        from tests.test_collection_execution import BackgroundCall, MutableClock
        from tests.test_company_collection_concurrency import FakeTransport
        from tests.test_multi_company_collection import REQUESTS
        pool, clock, entered, release = CollectionExecutor(), MutableClock(), Event(), Event()
        self.addCleanup(pool.close, wait=True)
        def handler(symbol, kind, kwargs):
            if symbol == REQUESTS[1].instrument_id and kind == "financial":
                raise requests.Timeout(PRIVATE)
            if symbol == REQUESTS[2].instrument_id and kind == "news":
                entered.set()
                if not release.wait(5):
                    raise AssertionError("Unreleased trace deadline source")
        http = FakeTransport(company_handler=handler)
        service = ResearchDataService(session_factory=http.factory, executor=pool, max_workers=1,
            clock=lambda: NOW, news_provider="eastmoney", monotonic_clock=clock, deadline_seconds=1)
        def invoke():
            with self.scope(), trace_event("tool", "research"):
                return trace_call("source", "company_collect", service.collect_many, REQUESTS, kinds=("financial", "news"))
        call = BackgroundCall(invoke)
        try:
            self.assertTrue(entered.wait(5))
            clock.set(2)
            batches = call.result()
            self.assertEqual(batches[2].source_results[1].code, "research_data_deadline_exceeded")
            self.assertTrue(batches[2].source_results[0].items)
            record = deepcopy(self.record())
            summary = record["sources"]
            self.assertEqual((summary["seen"], summary["ok"], summary["failed"], summary["stopped"], summary["unfinished"]),
                             (6, 4, 1, 1, 0))
            sources, parent = self.events("source"), self.events("tool")[0]
            self.assertEqual({item["parent_event_id"] for item in sources}, {parent["event_id"]})
            stopped = [item for item in sources if item["status"] == "stopped"]
            self.assertEqual([(item["source"], item["code"]) for item in stopped],
                             [("eastmoney_news", "research_data_deadline_exceeded")])
            encoded = json.dumps(record, ensure_ascii=False)
            for private in (PRIVATE, "300308.SZ", "000001.SZ", "600000.SH", "中际旭创"):
                self.assertNotIn(private, encoded)
            release.set()
            pool.close(wait=True)
            self.assertEqual(self.record(), record)
        finally:
            release.set()

    def test_three_company_four_section_trace_has_twelve_sources_with_one_parent(self):
        from trading_companion.collection_execution import CollectionExecutor
        from tests.test_company_collection_concurrency import FakeTransport
        from tests.test_multi_company_collection import REQUESTS
        pool, barrier = CollectionExecutor(), Barrier(3)
        self.addCleanup(pool.close, wait=True)
        def wait_for_peers(kind):
            barrier.wait(5)
        http = FakeTransport(wait_for_peers)
        service = ResearchDataService(session_factory=http.factory, executor=pool, clock=lambda: NOW, news_provider="eastmoney")
        with self.scope(), trace_event("tool", "research"):
            batches = trace_call("source", "company_collect", service.collect_many, REQUESTS)
        self.assertTrue(all(batch.status == "ok" for batch in batches))
        self.assertEqual(self.record()["sources"]["seen"], 12)
        self.assertEqual(len(self.events("source")), 12)
        parent = self.events("tool")[0]
        self.assertEqual({event["parent_event_id"] for event in self.events("source")}, {parent["event_id"]})

    def test_tencent_parallel_batches_keep_one_logical_source_and_copy_trace_parent_per_task(self):
        from trading_companion.collection_execution import CollectionExecutor
        from trading_companion.screening_source import TencentQuoteSnapshotSource
        from trading_companion.turn_trace import _PARENT
        from tests.test_screening_collection_concurrency import QuoteTransport
        from tests.test_screening_source import directory, NOW as QUOTE_NOW
        pool, observed, barrier = CollectionExecutor(), [], Barrier(3)
        self.addCleanup(pool.close, wait=True)
        def hook(index, url, kwargs):
            trace, parent = _CURRENT.get(), _PARENT.get()
            observed.append((trace, parent))
            _PARENT.set(None)  # A worker's mutation must not affect the next task or caller.
            if index < 3: barrier.wait(5)
        http = QuoteTransport(directory(), hook)
        source = TencentQuoteSnapshotSource(session_factory=http.factory, executor=pool, clock=lambda: QUOTE_NOW)
        with self.scope(), trace_event("tool", "research"):
            caller_trace, caller_parent = _CURRENT.get(), _PARENT.get()
            result = trace_call("source", "screening_snapshot", source.read_snapshot, directory())
            self.assertEqual((_CURRENT.get(), _PARENT.get()), (caller_trace, caller_parent))
        self.assertEqual(len(result.quotes), 5)
        self.assertEqual(len(http.calls), 5)
        reads = self.events("source")
        self.assertEqual(len(reads), 1)
        self.assertEqual((reads[0]["operation"], reads[0]["status"]), ("screening_snapshot", "ok"))
        self.assertEqual(reads[0]["parent_event_id"], self.events("tool")[0]["event_id"])
        self.assertEqual(len(observed), 5)
        self.assertTrue(all(trace is caller_trace and parent[0] is caller_trace
                            and parent[1]["event_id"] == reads[0]["event_id"]
                            for trace, parent in observed))
        self.assertEqual(self.record()["sources"]["seen"], 1)

    def test_invalid_source_result_is_not_inspected_or_serialized_as_arbitrary_metadata(self):
        class Untrusted:
            @property
            def hits(self):
                raise AssertionError("Untrusted property must not run for tracing")
        value = Untrusted()
        with self.scope():
            self.assertIs(trace_call("source", "knowledge_search", lambda: value), value)
        self.assertEqual(self.events("source")[0]["code"], "invalid_source_shape")

    def test_local_knowledge_search_and_output_rejection_use_existing_calls(self):
        model = FakeToolModel([ToolModelResponse("complete", "{}")])
        with self.scope():
            result = answer_general_knowledge("怎么用PE/PB看估值", model=model)
        self.assertEqual(result.answer.code, "general_knowledge_output_invalid")
        event = self.events("source")[0]
        self.assertEqual((event["source"], event["access"]), ("local_knowledge", "local"))
        self.assertEqual(self.events("check", "knowledge_answer")[0]["status"], "rejected")

    def run_repair(self, *values, repair=True, budget=2, callback=None):
        delegate = ScriptedModel(*values, callback=callback)
        control = TurnControl(TurnLimits(model_calls=budget))
        control.start()
        with self.scope(), bind_turn(control):
            result = synthesize_product_answer(QUESTION, make_bundle(), model=ControlledToolModel(delegate, control), repair_invalid=repair)
        return result, delegate

    def test_rewrite_links_only_second_model_and_check_and_preserves_rejected_first_check(self):
        result, delegate = self.run_repair(response(BAD_ASSOCIATION), response(GOOD_ASSOCIATION))
        self.assertEqual((result.answer.status, len(delegate.calls)), ("accepted", 2))
        record = self.record()
        repair = self.events("repair", "model_rewrite")[0]
        checks = self.events("check", "product_answer")
        self.assertEqual([event["status"] for event in checks], ["rejected", "ok"])
        self.assertEqual(checks[0]["code"], "numeric_citation_association")
        self.assertEqual(checks[1]["parent_event_id"], repair["event_id"])
        models = record["models"]["events"]
        self.assertIsNone(models[0]["parent_event_id"])
        self.assertEqual(models[1]["parent_event_id"], repair["event_id"])
        self.assertEqual((record["repairs"]["entered"], record["repairs"]["ok"]), (1, 1))
        encoded = json.dumps(record, ensure_ascii=False)
        for private in (QUESTION, BAD_ASSOCIATION[0]["text"], "revenue-real-id", "E1", "000001.SZ", "平安银行"):
            self.assertNotIn(private, encoded)

    def test_disabled_or_unsafe_rewrite_has_fixed_reason_without_repair_attempt(self):
        cases = ((response(BAD_ASSOCIATION), False, "rewrite_disabled"),
                 (ToolModelResponse("complete", PRIVATE), True, "rewrite_not_repairable"))
        for first, allowed, reason in cases:
            with self.subTest(reason=reason):
                result, delegate = self.run_repair(first, response(GOOD_ASSOCIATION), repair=allowed)
                self.assertEqual((result.answer.status, len(delegate.calls)), ("fallback", 1))
                self.assertEqual(self.record()["repairs"]["seen"], 0)
                self.assertEqual(self.events("check", "rewrite_decision")[0]["code"], reason)

    def test_rewrite_failure_is_recorded_and_never_starts_third_model(self):
        for second, status, code in ((response(BAD_ASSOCIATION), "rejected", "numeric_citation_association"),
                                     (ModelCallError("timeout", PRIVATE), "failed", "product_answer_model_unavailable")):
            with self.subTest(status=status):
                result, delegate = self.run_repair(response(BAD_ASSOCIATION), second, response(GOOD_ASSOCIATION))
                self.assertEqual((result.answer.status, len(delegate.calls)), ("fallback", 2))
                repair = self.events("repair", "model_rewrite")[0]
                self.assertEqual((repair["status"], repair["code"]), (status, code))

    def test_rewrite_budget_and_late_cancellation_keep_actual_model_counts(self):
        with self.assertRaises(TurnStopped):
            self.run_repair(response(BAD_ASSOCIATION), response(GOOD_ASSOCIATION), budget=1)
        self.assertEqual(self.record()["models"]["attempted"], 1)
        self.assertEqual(self.events("repair", "model_rewrite")[0]["code"], "model_call_budget_exhausted")
        control = TurnControl()
        control.start()
        delegate = ScriptedModel(response(BAD_ASSOCIATION), response(GOOD_ASSOCIATION),
                                 callback=lambda number: control.cancel() if number == 2 else None)
        with self.scope(), bind_turn(control), self.assertRaises(TurnStopped):
            synthesize_product_answer(QUESTION, make_bundle(), model=ControlledToolModel(delegate, control), repair_invalid=True)
        self.assertEqual((self.record()["models"]["attempted"], self.record()["models"]["responses_received"]), (2, 2))
        self.assertEqual(self.events("repair", "model_rewrite")[0]["code"], "turn_cancelled")

    def test_rule_correction_is_separate_from_model_rewrite_and_does_not_invent_model_attempts(self):
        _, directory = load_development_fixtures()
        for task, expected in (("research", "skipped"), ("clarify", "ok")):
            with self.subTest(task=task), self.scope():
                model = FakeToolModel([interpretation(intent_arguments(task=task, reason="request" if task == "clarify" else None))])
                result = prepare_research_question(INTENT_QUESTION, model=model, directory=directory,
                                                   source="csv", development=True, reference_date=REFERENCE_DATE)
            self.assertEqual(result.status, "ready")
            self.assertEqual(self.events("repair")[0]["status"], expected)
            self.assertEqual(result.model_calls_attempted, 1)
            self.assertEqual(self.record()["models"]["attempted"], 0)  # No controlled wrapper was injected here.
            self.assertEqual(self.events("repair")[0]["operation"], "intent_rule")

    def test_shared_detail_cap_and_minimum_bytes_preserve_all_category_counters(self):
        for limit in (0, 1, 64):
            path = self.path / str(limit)
            sink = JsonlTraceSink(path, limits=TraceLimits(max_events=limit, record_bytes=2048))
            with self.scope(sink):
                for _ in range(100):
                    with trace_event("tool", "research"):
                        with trace_event("tool", "execute"):
                            trace_call("source", "knowledge_search", default_knowledge_retriever().search, "PE")
                        trace_call("check", "tool_scope", lambda: True)
                        with trace_event("repair", "model_rewrite"), trace_model_attempt():
                            trace_model_response(ToolModelResponse("complete", PRIVATE, usage=TokenUsage(2, 3, 5)))
            record = read_records(path)[0]
            retained = len(record["events"]) + len(record["models"]["events"])
            self.assertLessEqual(retained, limit)
            self.assertEqual((record["sources"]["seen"], record["sources"]["ok"], record["models"]["attempted"]), (100, 100, 100))
            for category in ("tools", "checks", "repairs"):
                self.assertEqual((record[category]["seen"], record[category]["entered"], record[category]["ok"]), (100, 100, 100))
            self.assertEqual(record["dropped_events"], 500 - retained)
            self.assertLessEqual((path / "turns.jsonl").stat().st_size, 2048)

    def test_event_timing_uses_trace_clock_and_original_business_exception_propagates(self):
        seconds = [0.0]
        sink = JsonlTraceSink(self.path, clock=lambda: seconds[0])
        error = RuntimeError(PRIVATE)
        def read():
            seconds[0] += 0.025
            raise error
        with self.scope(sink), self.assertRaises(RuntimeError) as caught:
            trace_call("source", "directory", read)
        self.assertIs(caught.exception, error)
        event = self.events("source")[0]
        self.assertEqual((event["elapsed_ms"], event["code"]), (25, "other"))
        self.assertNotIn(PRIVATE, json.dumps(self.record()))

    def test_recording_failures_and_disabled_mode_do_not_change_call_count_or_return_value(self):
        for name in ("event_start", "event_end"):
            work = Mock(return_value=PRIVATE)
            with self.scope(), patch.object(TurnTrace, name, side_effect=RuntimeError(PRIVATE)):
                self.assertEqual(trace_call("source", "directory", work), PRIVATE)
            work.assert_called_once_with()
        for hook in ("_source_identity", "_known"):
            work = Mock(return_value=PRIVATE)
            with self.scope(), patch("trading_companion.turn_trace." + hook, side_effect=RuntimeError(PRIVATE)):
                self.assertEqual(trace_call("source", "company_collect", work), PRIVATE)
            work.assert_called_once_with()
        work = Mock(return_value=PRIVATE)
        with trace_message(None, mode="demo", provider="fake", history=False), patch(
                "trading_companion.turn_trace._source_identity", side_effect=AssertionError("disabled metadata inspected")):
            self.assertEqual(trace_call("source", "directory", work), PRIVATE)
        work.assert_called_once_with()

    def test_each_task_needs_its_own_context_and_parent_contexts_do_not_mix(self):
        barrier = Barrier(3)
        def task():
            barrier.wait(timeout=5)
            return trace_call("source", "knowledge_search", default_knowledge_retriever().search, "PE")
        with ThreadPoolExecutor(max_workers=3) as pool, self.scope():
            with trace_event("repair", "model_rewrite"):
                futures = [pool.submit(copy_context().run, task) for _ in range(2)]
                futures.append(pool.submit(task))
                for future in futures:
                    future.result(timeout=10)
        repair = self.events("repair")[0]
        reads = self.events("source")
        self.assertEqual(len(reads), 2)
        self.assertEqual({event["parent_event_id"] for event in reads}, {repair["event_id"]})
        self.assertEqual(len({event["event_id"] for event in self.record()["events"]}), 3)

    def test_finish_freezes_active_events_and_all_late_updates_without_second_write(self):
        with self.scope():
            trace = _CURRENT.get()
            with trace_event("source", "directory"):
                trace.finish()
                original = (self.path / "turns.jsonl").read_bytes()
                trace_call("source", "knowledge_search", default_knowledge_retriever().search, "PE")
                with trace_model_attempt():
                    trace_model_response(ToolModelResponse("complete", PRIVATE))
                trace_phase("projection")
            trace.finish()
        self.assertEqual((self.path / "turns.jsonl").read_bytes(), original)
        self.assertEqual(len(read_records(self.path)), 1)
        self.assertEqual((self.record()["sources"]["seen"], self.record()["sources"]["unfinished"]), (1, 1))
        self.assertEqual(self.record()["models"]["attempted"], 0)

    def test_concurrent_independent_turns_do_not_share_sources_or_parent_events(self):
        barrier = Barrier(2)
        def turn(operation):
            with self.scope():
                with trace_event("repair", "model_rewrite"):
                    barrier.wait(timeout=5)
                    trace_call("source", operation, lambda: [])
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(turn, operation) for operation in ("daily_bars", "directory")]
            for future in futures:
                future.result(timeout=10)
        records = read_records(self.path)
        self.assertEqual(len({record["run_id"] for record in records}), 2)
        self.assertEqual({next(event["operation"] for event in record["events"] if event["kind"] == "source")
                          for record in records}, {"daily_bars", "directory"})
        for record in records:
            self.assertEqual((record["sources"]["seen"], record["repairs"]["seen"]), (1, 1))
            root = next(event for event in record["events"] if event["kind"] == "repair")
            read = next(event for event in record["events"] if event["kind"] == "source")
            self.assertEqual(read["parent_event_id"], root["event_id"])


if __name__ == "__main__":
    unittest.main()
