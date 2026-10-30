"""Tracing through real message/launcher boundaries, with synthetic sources and mocked HTTP."""

from contextlib import nullcontext, redirect_stdout
from datetime import datetime, timezone
from io import StringIO
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

import requests
from fastapi.testclient import TestClient
from scripts.verify import offline_network

from trading_companion.api_server import main as server_main
from trading_companion.api_service import ApiLimits, ApiProblem, ResearchApiService, _SessionModel
from trading_companion.deepseek_model import DeepSeekConfig
from trading_companion.deepseek_tool_model import DeepSeekToolModel
from trading_companion.development_fixtures import REFERENCE_DATE, load_development_fixtures
from trading_companion.errors import DataSourceError
from trading_companion.followup_demo import ConversationDemoModel, QUESTION
from trading_companion.model import ModelCallError, ModelRequestError
from trading_companion.openai_tool_model import OpenAIConfig, OpenAIToolModel
from trading_companion.tool_model import ToolMessage, ToolModelResponse
from trading_companion.turn_control import ControlledToolModel, TurnControl, TurnLimits, TurnStopped, bind_turn
from trading_companion.turn_trace import JsonlTraceSink, TraceLimits, trace_control, trace_message
from tests.test_deepseek_tool_model import provider_payload
from tests.test_turn_trace import read_records


NOW = datetime(2026, 9, 26, 2, tzinfo=timezone.utc)
PRIVATE = "private-trace-api-sentinel"


class TurnTraceApiTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "traces"
        self.sink = JsonlTraceSink(self.path)
        self.source, self.directory = load_development_fixtures()
        for guard in (patch.dict(os.environ, {}, clear=True),
                      patch("requests.get", side_effect=AssertionError("No market HTTP")),
                      patch("requests.post", side_effect=AssertionError("No model HTTP")),
                      patch("trading_companion.local_env._read_local_values", side_effect=AssertionError("No credentials"))):
            guard.start()
            self.addCleanup(guard.stop)
        network = offline_network()
        network.__enter__()
        self.addCleanup(network.__exit__, None, None, None)

    def build(self, **changes):
        options = dict(model_factory=ConversationDemoModel, directory_loader=lambda _: self.directory,
                       development_source=self.source, reference_date=REFERENCE_DATE, wall_clock=lambda: NOW,
                       clock=lambda: 100.0, trace_sink=self.sink)
        options.update(changes)
        service = ResearchApiService(**options)
        self.addCleanup(service.close)
        credentials = service.create_session()
        return service, credentials

    def send(self, service, credentials, question=QUESTION, request_id=None):
        return service.message(credentials["session_id"], credentials["access_token"], request_id or uuid4().hex, question)

    def test_off_and_on_fixed_knowledge_have_identical_public_output(self):
        factory = lambda: TurnControl(clock=lambda: 100.0)
        plain, first = self.build(trace_sink=None, control_factory=factory)
        traced, second = self.build(control_factory=factory)
        key = uuid4().hex
        expected = self.send(plain, first, "MACD是什么", key)
        self.assertFalse(self.path.exists())
        actual = self.send(traced, second, "MACD是什么", key)
        self.assertEqual(actual, expected)
        record = read_records(self.path)[0]
        self.assertEqual(record["route"], {"kind": "technical_research", "intent": "indicator_knowledge",
                                          "rule_id": "knowledge.indicator"})
        self.assertEqual(record["models"]["attempted"], 0)
        self.assertEqual(record["outcome"], {"category": "completed", "code": "indicator_knowledge_answered"})
        self.assertNotIn("run_id", json.dumps(actual))

    def test_v2_actual_execution_and_projection_checks_are_counted_while_replay_has_no_new_events(self):
        service, credentials = self.build()
        key = uuid4().hex
        actual = self.send(service, credentials, request_id=key)
        replay = self.send(service, credentials, request_id=key)
        self.assertEqual(replay["result"], actual["result"])
        first, second = read_records(self.path)
        self.assertEqual(first["schema_version"], "trace-v2")
        self.assertEqual(first["tools"]["seen"], 1)
        self.assertEqual(first["tools"]["entered"], 1)
        self.assertEqual(first["sources"]["seen"], 2)  # Directory load and one synthetic daily read.
        checks = [event for event in first["events"] if event["kind"] == "check"]
        self.assertEqual({event["phase"] for event in checks}, {"execution", "projection"})
        self.assertGreater(sum(event["operation"] == "workflow_response" for event in checks), 1)
        self.assertEqual(second["events"], [])
        for category in ("tools", "sources", "checks", "repairs"):
            self.assertEqual(second[category]["seen"], 0)
        serialized = json.dumps([first, second], ensure_ascii=False)
        for private in (QUESTION, "平安银行", "000001.SZ", key, credentials["session_id"], credentials["access_token"]):
            self.assertNotIn(private, serialized)

    def test_trace_event_instrumentation_failure_preserves_reply_and_replay(self):
        from trading_companion.turn_trace import TurnTrace
        service, credentials = self.build()
        with patch.object(TurnTrace, "event_start", side_effect=RuntimeError(PRIVATE)):
            actual = self.send(service, credentials)
        replay = self.send(service, credentials, request_id=actual["request_id"])
        self.assertEqual(actual["result"], replay["result"])
        self.assertEqual(actual["result"]["code"], "research_completed")

    def test_knowledge_failure_and_noncomplete_codes_are_preserved_in_real_message_trace(self):
        cases = (
            ("general_knowledge_model_unavailable", ModelCallError("timeout", PRIVATE)),
            ("general_knowledge_output_invalid", ToolModelResponse("complete", "{}")),
            ("general_knowledge_refused", ToolModelResponse("refused", PRIVATE)),
            ("general_knowledge_incomplete", ToolModelResponse("incomplete")),
            ("general_knowledge_retrieval_unavailable", None),
        )
        for expected, response in cases:
            with self.subTest(code=expected):
                delegate = Mock()
                if isinstance(response, Exception):
                    delegate.complete.side_effect = response
                else:
                    delegate.complete.return_value = response
                service, credentials = self.build(model_factory=lambda: delegate)
                retrieval = (patch("trading_companion.open_knowledge.default_knowledge_retriever",
                    return_value=Mock(search=Mock(side_effect=ValueError(PRIVATE)))) if response is None else nullcontext())
                with retrieval:
                    result = self.send(service, credentials, "怎么用PE/PB看估值")
                self.assertEqual(result["result"]["code"], expected)
                record = read_records(self.path)[-1]
                self.assertEqual(record["route"]["intent"], "general_knowledge")
                self.assertEqual(record["outcome"], {"category": "error", "code": expected})
                self.assertNotIn(PRIVATE, json.dumps(record))
                if response is None:
                    delegate.complete.assert_not_called()

    def test_directory_failure_and_failed_replay_have_no_execution_or_double_counting(self):
        loader = Mock(side_effect=DataSourceError(PRIVATE))
        service, credentials = self.build(directory_loader=loader)
        key = uuid4().hex
        for _ in range(2):
            with self.assertRaises(ApiProblem) as caught:
                self.send(service, credentials, QUESTION + PRIVATE, key)
            self.assertEqual(caught.exception.code, "directory_unusable")
        loader.assert_called_once()
        first, replay = read_records(self.path)
        self.assertEqual(first["phase"], "directory")
        self.assertEqual(first["outcome"]["code"], "directory_unusable")
        self.assertFalse(first["execution_started"])
        self.assertIsNone(first["execution_elapsed_ms"])
        self.assertIsNone(first["route"])
        self.assertEqual(replay["original_run_id"], first["run_id"])
        self.assertEqual(replay["outcome"], {"category": "replayed", "code": "directory_unusable"})
        self.assertEqual(replay["models"]["attempted"], 0)
        encoded = json.dumps([first, replay])
        for private in (PRIVATE, key, credentials["session_id"], credentials["access_token"]):
            self.assertNotIn(private, encoded)

    def test_successful_replay_history_and_auth_rejections_are_separate_visits(self):
        service, credentials = self.build()
        key = uuid4().hex
        first = self.send(service, credentials, request_id=key)
        replay = self.send(service, credentials, request_id=key)
        self.assertEqual(first["result"], replay["result"])
        self.assertTrue(replay["replayed"])
        with self.assertRaises(ApiProblem):
            self.send(service, credentials, "MACD是什么", key)
        bad = {**credentials, "access_token": PRIVATE}
        with self.assertRaises(ApiProblem):
            self.send(service, bad, request_id=key)
        service.history(credentials["session_id"], credentials["access_token"])
        original, repeated, conflict, rejected, history = read_records(self.path)
        self.assertGreater(original["models"]["attempted"], 0)
        self.assertEqual(repeated["original_run_id"], original["run_id"])
        self.assertEqual(repeated["models"]["attempted"], 0)
        self.assertEqual(conflict["outcome"]["code"], "request_id_conflict")
        self.assertIsNone(rejected["original_run_id"])
        self.assertIsNone(rejected["provider"])
        self.assertEqual(rejected["outcome"]["code"], "session_not_found")
        self.assertEqual(rejected["outcome"]["category"], "rejected")
        self.assertTrue(history["history"])
        self.assertEqual(history["models"]["attempted"], 0)

    def test_message_budget_rejection_is_recorded_before_directory_work(self):
        loader = Mock(return_value=self.directory)
        service, credentials = self.build(limits=ApiLimits(messages=1), directory_loader=loader)
        self.send(service, credentials, "MACD是什么")
        with self.assertRaises(ApiProblem):
            self.send(service, credentials)
        loader.assert_called_once()
        record = read_records(self.path)[1]
        self.assertEqual(record["outcome"]["code"], "session_message_limit")
        self.assertEqual(record["phase"], "admission")
        self.assertIsNone(record["control"])

    def test_whole_message_time_includes_directory_but_execution_time_does_not(self):
        seconds = [100.0]
        sink = JsonlTraceSink(self.path, clock=lambda: seconds[0])
        def directory(_):
            seconds[0] += 4
            return self.directory
        service, credentials = self.build(trace_sink=sink, directory_loader=directory,
            control_factory=lambda: TurnControl(clock=lambda: 50.0))
        self.send(service, credentials, "MACD是什么")
        record = read_records(self.path)[0]
        self.assertEqual(record["total_elapsed_ms"], 4000)
        self.assertEqual(record["execution_elapsed_ms"], 0)
        self.assertTrue(record["execution_started"])

    def test_unexpected_error_and_interrupt_keep_their_original_semantics(self):
        for error in (RuntimeError(PRIVATE), KeyboardInterrupt(PRIVATE)):
            with self.subTest(error=type(error).__name__):
                service, credentials = self.build(directory_loader=Mock(side_effect=error))
                with self.assertRaises(KeyboardInterrupt if isinstance(error, KeyboardInterrupt) else ApiProblem):
                    self.send(service, credentials)
                self.assertNotEqual(service.inspect(credentials["session_id"], credentials["access_token"])["state"], "busy")
        records = read_records(self.path)
        self.assertEqual(records[0]["outcome"]["code"], "research_internal_error")
        self.assertEqual(records[1]["outcome"]["category"], "interrupted")
        self.assertNotIn(PRIVATE, json.dumps(records))

    def test_budget_stop_has_terminal_record_without_a_reserved_model_attempt(self):
        service, credentials = self.build(control_factory=lambda: TurnControl(TurnLimits(model_calls=0, analyses=0)))
        result = self.send(service, credentials)
        self.assertEqual(result["result"]["code"], "model_call_budget_exhausted")
        record = read_records(self.path)[0]
        self.assertEqual(record["outcome"]["category"], "stopped")
        self.assertEqual(record["models"]["attempted"], 0)
        self.assertEqual(record["control"]["state"], "finished")

    def test_cancelled_late_response_is_metered_without_publication_and_busy_is_traced(self):
        entered, release, replies, errors = Event(), Event(), [], []
        delegate = ConversationDemoModel()
        def complete(messages, *, tools):
            entered.set()
            if not release.wait(3):
                raise AssertionError("Test did not release blocked model")
            return delegate.complete(messages, tools=tools)
        service, credentials = self.build(model_factory=lambda: Mock(complete=complete))
        key = uuid4().hex
        def work():
            try:
                replies.append(self.send(service, credentials, request_id=key))
            except BaseException as error:
                errors.append(error)
        thread = Thread(target=work)
        thread.start()
        try:
            self.assertTrue(entered.wait(3))
            with self.assertRaises(ApiProblem):
                self.send(service, credentials)
            service.cancel(credentials["session_id"], credentials["access_token"], key)
        finally:
            release.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(replies[0]["result"]["code"], "turn_cancelled")
        self.assertIsNone(service._entries[credentials["session_id"]].session.confirmed)
        records = read_records(self.path)
        cancelled = next(item for item in records if item["outcome"]["category"] == "stopped")
        busy = next(item for item in records if item["outcome"]["code"] == "session_busy")
        self.assertEqual(busy["models"]["attempted"], 0)
        self.assertEqual(cancelled["models"]["responses_received"], 1)
        self.assertEqual(cancelled["models"]["events"][0]["error_code"], "turn_cancelled")

    def test_write_failure_does_not_change_response_cache_or_leave_locks_held(self):
        service, credentials = self.build()
        acquired = []
        def fail(record):
            entry = service._entries[credentials["session_id"]]
            for lock in (service._lock, entry.session._turn_lock):
                free = lock.acquire(blocking=False)
                acquired.append(free)
                if free:
                    lock.release()
            raise OSError(PRIVATE)
        with patch.object(self.sink, "write", side_effect=fail):
            result = self.send(service, credentials, "MACD是什么")
        self.assertEqual(result["result"]["code"], "indicator_knowledge_answered")
        self.assertEqual(acquired, [True, True])
        replay = self.send(service, credentials, "MACD是什么", result["request_id"])
        self.assertTrue(replay["replayed"])

    def test_smallest_record_limit_keeps_control_and_terminal_state(self):
        sink = JsonlTraceSink(self.path, limits=TraceLimits(record_bytes=2048))
        service, credentials = self.build(trace_sink=sink)
        result = self.send(service, credentials)
        record = read_records(self.path)[0]
        self.assertEqual(record["outcome"]["code"], result["result"]["code"])
        self.assertEqual(record["control"]["state"], "finished")
        self.assertLessEqual((self.path / "turns.jsonl").stat().st_size, 2048)
        self.assertTrue(record["truncated"])

    def test_selected_session_provider_is_recorded_instead_of_service_default(self):
        deepseek = lambda: DeepSeekToolModel(DeepSeekConfig("trace-deepseek", PRIVATE))
        openai = lambda: OpenAIToolModel(OpenAIConfig("trace-openai", PRIVATE))
        service, _ = self.build(provider="deepseek", model_factory=deepseek,
                                model_factories={"deepseek": deepseek, "openai": openai})
        credentials = service.create_session(provider="openai")
        self.send(service, credentials, "MACD是什么")
        self.assertEqual(read_records(self.path)[0]["provider"], "openai")

    def test_all_105_routing_observations_match_frozen_targets_with_tracing_enabled(self):
        from tests.routing_baseline_support import compare_observations, read_baseline
        from tests.routing_targets_support import TARGET, observe_target_scenario, validate_contracts
        from tests.routing_horizon_support import overlay
        from tests.routing_technical_support import validate_current_contracts
        def traced_service(**kwargs):
            return ResearchApiService(**kwargs, trace_sink=self.sink)
        with patch("tests.routing_baseline_support.ResearchApiService", side_effect=traced_service):
            for scenario in read_baseline(TARGET)["scenarios"]:
                with self.subTest(scenario=scenario["id"]):
                    observed, decisions = observe_target_scenario(scenario)
                    validate_current_contracts(scenario, observed, decisions)
                    compare_observations({**scenario, "observed": overlay(scenario["id"], scenario["observed"])}, observed)
        records = read_records(self.path)
        self.assertEqual(len(records), 105)
        self.assertNotIn("other", {item["outcome"]["code"] for item in records})
        intents = {item["route"]["intent"] for item in records if item["route"] is not None}
        self.assertTrue({"screening", "screening_edit", "screening_followup", "company_research",
                           "technical_research", "general_knowledge", "company_clarification"} <= intents)

    def test_demo_launcher_explicitly_injects_trace_without_loading_environment(self):
        def serve(app, **kwargs):
            with TestClient(app, base_url="http://localhost") as client:
                credentials = client.post("/v1/sessions", json={}).json()
                response = client.post("/v1/sessions/" + credentials["session_id"] + "/messages",
                    headers={"Authorization": "Bearer " + credentials["access_token"]},
                    json={"request_id": uuid4().hex, "question": QUESTION})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["result"]["code"], "company_research_completed")
        with redirect_stdout(StringIO()) as output, patch("uvicorn.run", side_effect=serve):
            self.assertEqual(server_main(["--demo", "--trace-dir", str(self.path)]), 0)
        self.assertIn("运行记录已开启", output.getvalue())
        self.assertNotIn(str(self.path), output.getvalue())
        record = read_records(self.path)[0]
        self.assertEqual(record["mode"], "demo")
        self.assertGreater(record["models"]["attempted"], 0)
        self.assertEqual(record["models"]["provider_requests_started"], 0)

    def test_normal_and_dev_launch_share_only_writer_and_have_distinct_labels(self):
        from trading_companion.api import create_app as actual_app
        def app_factory(*, service, development_service=None):
            if development_service is None:
                return actual_app(service=service)
            self.assertIs(service._trace_sink, development_service._trace_sink)
            self.assertIsNot(service._entries, development_service._entries)
            for current in (service, development_service):
                credentials = current.create_session()
                self.send(current, credentials, "MACD是什么")
            return actual_app(service=service, development_service=development_service)
        with redirect_stdout(StringIO()), patch("uvicorn.run"), patch(
            "trading_companion.api.create_app", side_effect=app_factory), patch(
            "trading_companion.model_registry.configured_models",
            return_value={"deepseek": lambda: DeepSeekToolModel(DeepSeekConfig("trace-model", PRIVATE))}):
            self.assertEqual(server_main(["--data", "synthetic", "--trace-dir", str(self.path)]), 0)
        self.assertEqual([item["mode"] for item in read_records(self.path)], ["normal", "development"])


class ModelTraceBoundaryTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name)
        self.sink = JsonlTraceSink(self.path)
        self.messages = [ToolMessage("user", PRIVATE)]
        self.adapters = (("deepseek", DeepSeekToolModel(DeepSeekConfig("trace-deepseek", PRIVATE))),
                         ("openai", OpenAIToolModel(OpenAIConfig("trace-openai", PRIVATE))))

    def invoke(self, provider, model, *, messages=None, session_limit=60):
        control = TurnControl()
        with trace_message(self.sink, mode="normal", provider=provider, history=False):
            trace_control(control, provider)
            control.start()
            try:
                with bind_turn(control):
                    return ControlledToolModel(_SessionModel(model, session_limit), control).complete(
                        self.messages if messages is None else messages, tools=())
            finally:
                control.finish()

    def test_success_counts_http_and_response_separately_without_payload_or_key(self):
        for provider, model in self.adapters:
            response = Mock(status_code=200)
            payload = provider_payload(content=PRIVATE)
            payload["model"] = PRIVATE
            response.json.return_value = payload
            with patch("requests.post", return_value=response) as post:
                self.invoke(provider, model)
            post.assert_called_once()
        for record in read_records(self.path):
            models = record["models"]
            self.assertEqual((models["attempted"], models["provider_requests_started"], models["responses_received"]), (1, 1, 1))
            self.assertEqual(models["reported_usage"]["total_tokens"], 15)
            self.assertTrue(models["usage_complete_for_attempts"])
            self.assertNotIn(PRIVATE, json.dumps(record))

    def test_local_validation_and_session_limit_are_attempts_without_http(self):
        for provider, model in self.adapters:
            for options, expected in (({"messages": []}, ModelRequestError), ({"session_limit": 0}, ModelCallError)):
                with self.subTest(provider=provider, options=options), patch("requests.post") as post:
                    with self.assertRaises(expected):
                        self.invoke(provider, model, **options)
                    post.assert_not_called()
        for record in read_records(self.path):
            models = record["models"]
            self.assertEqual((models["attempted"], models["provider_requests_started"], models["responses_received"]), (1, 0, 0))
            self.assertFalse(models["usage_complete_for_attempts"])

    def test_timeout_and_invalid_provider_body_are_sends_without_valid_response(self):
        expected_attempts = []
        for provider, model in self.adapters:
            for effect in (requests.exceptions.Timeout(PRIVATE), Mock(status_code=200, json=Mock(return_value={PRIVATE: PRIVATE}))):
                with self.subTest(provider=provider, effect=type(effect).__name__):
                    options = {"side_effect": effect} if isinstance(effect, Exception) else {"return_value": effect}
                    with patch("requests.post", **options), self.assertRaises(ModelCallError):
                        self.invoke(provider, model)
                    expected_attempts.append(2 if isinstance(effect, requests.exceptions.Timeout) else 1)
        records = read_records(self.path)
        self.assertEqual(len(records), len(expected_attempts))
        for record, attempts in zip(records, expected_attempts):
            models = record["models"]
            self.assertEqual((models["attempted"], models["provider_requests_started"], models["responses_received"]), (attempts, attempts, 0))
            self.assertIsNone(models["reported_usage"]["total_tokens"])
            self.assertNotIn(PRIVATE, json.dumps(record))

    def test_timeout_resolution_precedes_send_marker_for_both_adapters(self):
        for provider, model in self.adapters:
            module = "deepseek_model" if provider == "deepseek" else "openai_tool_model"
            with patch("trading_companion." + module + ".request_timeout", side_effect=TurnStopped("turn_deadline_exceeded")), \
                    patch("requests.post") as post, self.assertRaises(TurnStopped):
                self.invoke(provider, model)
            post.assert_not_called()
        self.assertTrue(all(item["models"]["provider_requests_started"] == 0 for item in read_records(self.path)))

    def test_late_http_response_usage_is_recorded_before_cancel_or_deadline_rejection(self):
        for provider, model in self.adapters:
            for stop in ("cancel", "deadline"):
                seconds = [100.0]
                control = TurnControl(TurnLimits(timeout_seconds=1), clock=lambda: seconds[0])
                response = Mock(status_code=200)
                response.json.return_value = provider_payload(content=PRIVATE)
                def post(*args, **kwargs):
                    if stop == "cancel":
                        control.cancel()
                    else:
                        seconds[0] += 1
                    return response
                with patch("requests.post", side_effect=post), self.assertRaises(TurnStopped), trace_message(
                        self.sink, mode="normal", provider=provider, history=False):
                    trace_control(control, provider)
                    control.start()
                    try:
                        with bind_turn(control):
                            ControlledToolModel(_SessionModel(model, 60), control).complete(self.messages, tools=())
                    finally:
                        control.finish()
        for record in read_records(self.path):
            self.assertEqual(record["outcome"]["category"], "stopped")
            self.assertEqual(record["models"]["responses_received"], 1)
            self.assertEqual(record["models"]["reported_usage"]["total_tokens"], 15)
            self.assertEqual(record["control"]["reported_usage"]["total_tokens"], 15)
            self.assertEqual(record["models"]["events"][0]["error_code"], record["control"]["stop_code"])


if __name__ == "__main__":
    unittest.main()
