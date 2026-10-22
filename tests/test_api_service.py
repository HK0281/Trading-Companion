"""Framework-independent, offline API session/service contracts."""

from datetime import datetime, timezone
import json
import os
from threading import Event, Thread
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from trading_companion.agent import AgentTurnResult
from trading_companion.api_service import ApiLimits, ApiProblem, ResearchApiService, public_turn
from trading_companion.development_fixtures import REFERENCE_DATE, load_development_fixtures
from trading_companion.followup_demo import ConversationDemoModel, QUESTION, FOLLOWUP
from trading_companion.model import ModelConfigurationError
from trading_companion.errors import DataSourceError


NOW = datetime(2026, 9, 26, 2, tzinfo=timezone.utc)
PRIVATE = "private-api-marker-never-export"


class ServiceTests(unittest.TestCase):
    def setUp(self):
        for guard in (patch.dict(os.environ, {}, clear=True),
                      patch("requests.get", side_effect=AssertionError("No market HTTP")),
                      patch("requests.post", side_effect=AssertionError("No model HTTP")),
                      patch("trading_companion.deepseek_model.DeepSeekConfig.from_env",
                            side_effect=AssertionError("No real credential reads"))):
            guard.start()
            self.addCleanup(guard.stop)
        self.source, self.directory = load_development_fixtures()
        self.seconds = 100.0
        self.factory = Mock(side_effect=ConversationDemoModel)
        self.loader = Mock(return_value=self.directory)
        self.service = self.build()
        self.credentials = self.service.create_session()
        self.sid, self.token = self.credentials["session_id"], self.credentials["access_token"]

    def build(self, **kwargs):
        options = dict(model_factory=self.factory, directory_loader=self.loader,
                       development_source=self.source, reference_date=REFERENCE_DATE,
                       clock=lambda: self.seconds, wall_clock=lambda: NOW)
        options.update(kwargs)
        return ResearchApiService(**options)

    def send(self, question=QUESTION, request_id=None):
        return self.service.message(self.sid, self.token, request_id or uuid4().hex, question)

    def problem(self, code, callback, status=None):
        with self.assertRaises(ApiProblem) as caught:
            callback()
        self.assertEqual(caught.exception.code, code)
        if status is not None:
            self.assertEqual(caught.exception.status, status)

    def test_default_limits_and_invalid_expansion(self):
        self.assertEqual(ApiLimits().messages, 20)
        for values in ({"sessions": 0}, {"sessions": 33}, {"messages": 21}, {"model_calls": True},
                       {"concurrent_turns": 3}, {"lifetime_seconds": float("nan")}, {"lifetime_seconds": 1801}):
            with self.subTest(values=values), self.assertRaises(ModelConfigurationError):
                ApiLimits(**values)

    def test_creation_has_independent_tokens_and_no_model_request_or_directory_io(self):
        second = self.service.create_session()
        self.assertNotEqual(second["session_id"], self.sid)
        self.assertNotEqual(second["access_token"], self.token)
        self.assertEqual(len(self.sid), 32)
        self.loader.assert_not_called()
        self.assertEqual(self.service.inspect(self.sid, self.token)["model_calls_forwarded"], 0)

    def test_bad_or_cross_session_tokens_cannot_read_modify_delete_or_query(self):
        other = self.service.create_session()
        for token in (other["access_token"], "wrong", "中文", None):
            for operation in (lambda: self.service.inspect(self.sid, token),
                              lambda: self.service.reset(self.sid, token),
                              lambda: self.service.delete(self.sid, token),
                              lambda: self.service.history(self.sid, token),
                              lambda: self.service.message(self.sid, token, uuid4().hex, QUESTION)):
                self.problem("session_not_found", operation, 404)
        self.loader.assert_not_called()

    def test_research_followup_and_history_reuse_real_agent(self):
        initial = self.send()
        self.assertEqual(initial["result"]["code"], "research_completed")
        answer = self.send(FOLLOWUP)
        self.assertEqual(answer["result"]["code"], "saved_question_answered")
        self.assertIn("79.545455", answer["result"]["answer"])
        history = self.service.history(self.sid, self.token)
        self.assertEqual(history["result"]["model_calls_attempted"], 0)
        self.assertEqual(history["result"]["runtime"]["limits"]["model_calls"], 0)
        self.assertEqual(self.service.inspect(self.sid, self.token)["model_calls_forwarded"], 4)

    def test_repeated_request_is_replayed_without_extra_model_or_directory_reads(self):
        key = uuid4().hex
        original = self.send(request_id=key)
        reads = self.loader.call_count
        replay = self.send(request_id=key)
        self.assertTrue(replay["replayed"])
        self.assertEqual(original["result"], replay["result"])
        self.assertEqual(self.loader.call_count, reads)
        self.assertEqual(self.service.inspect(self.sid, self.token)["messages_used"], 1)
        replay["result"]["runtime"]["stop_code"] = PRIVATE
        self.assertIsNone(self.send(request_id=key)["result"]["runtime"]["stop_code"])

    def test_reuse_id_for_different_question_is_conflict(self):
        key = uuid4().hex
        self.send("RSI是什么", key)
        self.problem("request_id_conflict", lambda: self.send("ATR是什么", key), 409)

    def test_reset_clears_results_but_does_not_refund_quotas_or_allow_old_ids(self):
        key = uuid4().hex
        self.send(request_id=key)
        state = self.service.reset(self.sid, self.token)["session"]
        self.assertEqual((state["messages_used"], state["model_calls_forwarded"]), (1, 3))
        self.assertIsNone(state["research"]["saved_result"])
        self.problem("request_id_retired", lambda: self.send(request_id=key), 409)
        self.assertEqual(self.service.history(self.sid, self.token)["result"]["code"], "no_saved_result")
        self.assertEqual(self.factory.call_count, 1)

    def test_delete_removes_session(self):
        self.service.delete(self.sid, self.token)
        self.problem("session_not_found", lambda: self.service.inspect(self.sid, self.token), 404)

    def test_expiration_is_absolute_and_access_does_not_renew_it(self):
        self.seconds += 1799
        self.service.inspect(self.sid, self.token)
        self.seconds += 1
        self.problem("session_not_found", lambda: self.service.inspect(self.sid, self.token), 404)
        self.assertIsNotNone(self.service.create_session())

    def test_backwards_clock_invalidates_session(self):
        self.seconds -= 1
        self.problem("session_not_found", lambda: self.service.inspect(self.sid, self.token))

    def test_capacity_is_bounded_and_expired_sessions_are_pruned(self):
        self.service = self.build(limits=ApiLimits(sessions=1))
        self.service.create_session()
        self.problem("session_capacity", self.service.create_session, 429)
        self.seconds += 1800
        self.assertIsNotNone(self.service.create_session())

    def test_message_allowance_does_not_block_history_or_reset(self):
        self.service = self.build(limits=ApiLimits(messages=1))
        credentials = self.service.create_session()
        self.sid, self.token = credentials["session_id"], credentials["access_token"]
        self.send()
        self.problem("session_message_limit", lambda: self.send("RSI是什么"), 429)
        self.assertIn("历史查看", self.service.history(self.sid, self.token)["result"]["answer"])
        self.service.reset(self.sid, self.token)
        self.problem("session_message_limit", lambda: self.send("RSI是什么"), 429)

    def test_session_model_allowance_is_separate_from_turn_limit(self):
        self.service = self.build(limits=ApiLimits(model_calls=1))
        credentials = self.service.create_session()
        self.sid, self.token = credentials["session_id"], credentials["access_token"]
        response = self.send()
        self.assertEqual(response["result"]["code"], "model_unavailable")
        state = self.service.inspect(self.sid, self.token)
        self.assertEqual(state["model_calls_forwarded"], 1)
        self.assertEqual(response["result"]["model_calls_attempted"], 2)
        self.service.reset(self.sid, self.token)
        self.send()
        self.assertEqual(self.service.inspect(self.sid, self.token)["model_calls_forwarded"], 1)

    def test_directory_failure_is_sanitized_and_idempotently_cached(self):
        self.loader.side_effect = DataSourceError(PRIVATE)
        key = uuid4().hex
        self.problem("directory_unusable", lambda: self.send(request_id=key), 503)
        self.problem("directory_unusable", lambda: self.send(request_id=key), 503)
        self.assertEqual(self.loader.call_count, 1)
        self.assertEqual(self.service.inspect(self.sid, self.token)["model_calls_forwarded"], 0)

    def test_unexpected_failure_is_sanitized_replayable_and_releases_busy_slot(self):
        self.loader.side_effect = RuntimeError(PRIVATE)
        key = uuid4().hex
        self.problem("research_internal_error", lambda: self.send(request_id=key), 500)
        self.problem("research_internal_error", lambda: self.send(request_id=key), 500)
        self.assertNotEqual(self.service.inspect(self.sid, self.token)["state"], "busy")
        self.assertEqual(self.loader.call_count, 1)

    def test_public_projection_never_calls_debug_export_or_exposes_draft(self):
        result = AgentTurnResult(model_draft=PRIVATE, failure_code=PRIVATE)
        with patch.object(AgentTurnResult, "to_dict", side_effect=AssertionError("No debug export")):
            projected = public_turn(result)
        self.assertNotIn(PRIVATE, json.dumps(projected))
        self.assertNotIn("research_result", projected)
        self.assertNotIn("model_responses", projected)

    def test_close_revokes_sessions_and_new_work(self):
        self.service.close()
        self.problem("service_closed", self.service.create_session, 503)
        self.problem("service_closed", lambda: self.send(), 503)

    def test_bad_model_factory_does_not_leak_configuration(self):
        service = self.build(model_factory=Mock(side_effect=RuntimeError(PRIVATE)))
        self.problem("model_configuration_error", service.create_session, 503)

    def test_session_results_are_isolated(self):
        self.send()
        second = self.service.create_session()
        response = self.service.history(second["session_id"], second["access_token"])
        self.assertEqual(response["result"]["code"], "no_saved_result")

    def test_matching_cancel_is_cooperative_and_busy_slots_are_not_released_early(self):
        entered, release = Event(), Event()
        key = uuid4().hex
        delegate = ConversationDemoModel()
        def blocked(messages, *, tools):
            entered.set()
            if not release.wait(3):
                raise AssertionError("No test release signal")
            return delegate.complete(messages, tools=tools)
        self.service = self.build(model_factory=lambda: Mock(complete=Mock(side_effect=blocked)))
        credentials = self.service.create_session()
        self.sid, self.token = credentials["session_id"], credentials["access_token"]
        results = []
        thread = Thread(target=lambda: results.append(self.send(request_id=key)), daemon=True)
        thread.start()
        try:
            self.assertTrue(entered.wait(3))
            self.problem("request_in_progress", lambda: self.send(request_id=key), 409)
            self.problem("session_busy", lambda: self.send("RSI是什么"), 409)
            self.problem("session_busy", lambda: self.service.reset(self.sid, self.token), 409)
            self.problem("session_busy", lambda: self.service.delete(self.sid, self.token), 409)
            self.problem("request_not_cancellable", lambda: self.service.cancel(self.sid, self.token, uuid4().hex), 409)
            self.assertEqual(self.service.cancel(self.sid, self.token, key)["status"], "cancellation_requested")
            self.assertEqual(self.service.inspect(self.sid, self.token)["state"], "busy")
        finally:
            release.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results[0]["result"]["code"], "turn_cancelled")
        self.assertIsNone(self.service.inspect(self.sid, self.token)["research"]["saved_result"])
        self.assertTrue(self.send(request_id=key)["replayed"])
        self.problem("request_not_cancellable", lambda: self.service.cancel(self.sid, self.token, key), 409)

    def _blocked_service(self, **options):
        entered, release = Event(), Event()
        delegate = ConversationDemoModel()
        def block(messages, *, tools):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("No test release")
            return delegate.complete(messages, tools=tools)
        self.service = self.build(model_factory=lambda: Mock(complete=Mock(side_effect=block)), **options)
        credentials = self.service.create_session()
        self.sid, self.token = credentials["session_id"], credentials["access_token"]
        results = []
        thread = Thread(target=lambda: results.append(self.send()), daemon=True)
        thread.start()
        self.assertTrue(entered.wait(3))
        self.addCleanup(release.set)
        return release, thread, results

    def test_global_concurrency_rejects_before_consuming_other_session_message(self):
        release, thread, results = self._blocked_service(limits=ApiLimits(concurrent_turns=1))
        other = self.service.create_session()
        key = uuid4().hex
        def retry():
            return self.service.message(other["session_id"], other["access_token"], key, "RSI是什么")
        try:
            self.problem("service_busy", retry, 429)
            self.assertEqual(self.service.inspect(other["session_id"], other["access_token"])["messages_used"], 0)
        finally:
            release.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(retry()["result"]["code"], "indicator_knowledge_answered")

    def test_expiring_inflight_session_signals_cancel_and_cleans_after_return(self):
        release, thread, results = self._blocked_service()
        try:
            self.seconds += 1800
            self.problem("session_not_found", lambda: self.service.inspect(self.sid, self.token), 404)
        finally:
            release.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results[0]["result"]["code"], "turn_cancelled")
        self.assertEqual(self.service._entries, {})

    def test_shutdown_cancels_inflight_turn_and_does_not_retain_completed_state(self):
        release, thread, results = self._blocked_service()
        try:
            self.service.close()
        finally:
            release.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results[0]["result"]["code"], "turn_cancelled")
        self.assertEqual(self.service._entries, {})


if __name__ == "__main__":
    unittest.main()
