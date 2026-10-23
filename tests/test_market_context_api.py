"""Offline service contracts for frozen, optional, non-model market context."""

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import os
from threading import Event, Thread
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from scripts.verify import offline_network
from trading_companion.api_service import ApiLimits, ApiProblem, ResearchApiService
from trading_companion.data_source import CsvDailyBarSource
from trading_companion.development_fixtures import (
    REFERENCE_DATE, load_development_fixtures, load_development_market_context,
)
from trading_companion.errors import DataSourceError
from trading_companion.followup_demo import ConversationDemoModel, FOLLOWUP, QUESTION
from trading_companion.turn_control import TurnControl, TurnLimits


NOW = datetime(2026, 9, 26, 2, tzinfo=timezone.utc)
PRIVATE = "private-context-error-never-export"
EDIT = "把SMA3改成5日，其他不变"


class MarketContextServiceTests(unittest.TestCase):
    def setUp(self):
        for guard in (
            patch.dict(os.environ, {}, clear=True),
            patch("requests.get", side_effect=AssertionError("No real market or directory HTTP")),
            patch("requests.post", side_effect=AssertionError("No real model HTTP")),
            patch("trading_companion.deepseek_model.DeepSeekConfig.from_env",
                  side_effect=AssertionError("No credential reads")),
        ):
            guard.start()
            self.addCleanup(guard.stop)
        network = offline_network()
        network.__enter__()
        self.addCleanup(network.__exit__, None, None, None)
        self.source, self.directory = load_development_fixtures()
        self.snapshot = load_development_market_context()
        self.context_loader = Mock(return_value=self.snapshot)
        self.directory_loader = Mock(return_value=self.directory)
        self.models = []
        self.seconds = 100.0
        self.install_service()

    def model_factory(self):
        delegate = ConversationDemoModel()
        model = Mock()
        model.complete.side_effect = delegate.complete
        self.models.append(model)
        return model

    def install_service(self, **overrides):
        options = dict(
            model_factory=self.model_factory, directory_loader=self.directory_loader,
            development_source=self.source, reference_date=REFERENCE_DATE,
            market_context_loader=self.context_loader,
            clock=lambda: self.seconds, wall_clock=lambda: NOW,
        )
        options.update(overrides)
        self.service = ResearchApiService(**options)
        credentials = self.service.create_session()
        self.sid, self.token = credentials["session_id"], credentials["access_token"]
        self.entry = self.service._entries[self.sid]
        self.model = self.entry.model.delegate

    def send(self, question=QUESTION, request_id=None):
        return self.service.message(self.sid, self.token, request_id or uuid4().hex, question)

    def history(self):
        return self.service.history(self.sid, self.token)["result"]

    def assert_unavailable(self, context, code):
        self.assertEqual(context, {
            "version": "research-market-context-v1", "status": "unavailable", "code": code,
        })

    def test_new_research_loads_context_once_without_extra_model_or_directory_calls(self):
        response = self.send()
        turn, context = response["result"], response["result"]["market_context"]
        self.assertEqual(turn["code"], "research_completed")
        self.assertEqual(context["status"], "available")
        self.assertEqual(context["request_id"], turn["report"]["request_id"])
        self.assertNotEqual(context["request_id"], response["request_id"])
        self.assertEqual(context["snapshot_id"], self.snapshot.snapshot_id)
        self.assertEqual(context["data_authenticity"], "synthetic")
        self.assertEqual((turn["model_calls_attempted"], turn["analyses_used"]), (3, 1))
        self.assertEqual(self.model.complete.call_count, 3)
        self.context_loader.assert_called_once_with(NOW)
        self.directory_loader.assert_called_once_with(NOW)
        self.assertEqual(self.entry.market_contexts, {context["request_id"]: context})

    def test_context_is_not_added_to_any_model_message_or_tool(self):
        self.send()
        model_inputs = repr(self.model.complete.call_args_list)
        for marker in (self.snapshot.snapshot_id, "security_states", "calendar_context_unknown_or_partial",
                       self.snapshot.security_states[0].reference, self.snapshot.calendars[0].reference):
            self.assertNotIn(marker, model_inputs)
        self.assertEqual(self.model.complete.call_count, 3)

    def test_initial_response_replay_and_context_cache_are_deeply_detached(self):
        key = uuid4().hex
        original = self.send(request_id=key)
        expected = deepcopy(original["result"]["market_context"])
        original["result"]["market_context"]["security"]["st_status"] = "changed-by-consumer"
        original["result"]["market_context"]["calendar"]["open_dates"].clear()
        original["result"]["market_context"]["warnings"].append(PRIVATE)
        replay = self.send(request_id=key)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["result"]["market_context"], expected)
        replay["result"]["market_context"]["calendar"]["open_dates"].clear()
        self.assertEqual(self.send(request_id=key)["result"]["market_context"], expected)
        self.assertEqual(self.entry.market_contexts[expected["request_id"]], expected)
        self.assertEqual(self.model.complete.call_count, 3)
        self.context_loader.assert_called_once_with(NOW)

    def test_followup_and_history_preserve_original_context_without_reload_or_price_reads(self):
        original = self.send()["result"]["market_context"]
        self.context_loader.return_value = replace(self.snapshot, snapshot_id="different-current-snapshot")
        with patch.object(CsvDailyBarSource, "read_daily_bars", side_effect=AssertionError("No price reload")):
            followup = self.send(FOLLOWUP)["result"]
            history = self.history()
        self.assertEqual(followup["code"], "saved_question_answered")
        self.assertEqual(followup["market_context"], original)
        self.assertEqual(history["market_context"], original)
        self.assertEqual(history["model_calls_attempted"], 0)
        self.assertEqual(self.model.complete.call_count, 4)
        self.context_loader.assert_called_once_with(NOW)

    def test_missing_cached_context_is_not_reconstructed_from_current_snapshot(self):
        self.send()
        self.entry.market_contexts.clear()
        self.context_loader.side_effect = AssertionError("A cache miss must not reload context")
        followup = self.send(FOLLOWUP)["result"]
        history = self.history()
        for turn in (followup, history):
            self.assertEqual(turn["status"], "completed")
            self.assert_unavailable(turn["market_context"], "context_cache_miss")
        self.assertEqual(self.entry.market_contexts, {})
        self.assertEqual(self.context_loader.call_count, 1)

    def test_unavailable_projection_is_frozen_even_if_later_snapshot_becomes_usable(self):
        self.context_loader.return_value = replace(self.snapshot, retrieved_at=NOW + timedelta(days=1))
        original = self.send()["result"]
        self.assertEqual(original["code"], "research_completed")
        self.assert_unavailable(original["market_context"], "context_future_snapshot")
        self.context_loader.return_value = self.snapshot
        self.assertEqual(self.send(FOLLOWUP)["result"]["market_context"], original["market_context"])
        self.assertEqual(self.history()["market_context"], original["market_context"])
        self.assertEqual(self.context_loader.call_count, 1)

    def test_loader_exception_is_sanitized_frozen_and_does_not_replace_text_or_trigger_retry(self):
        self.context_loader.side_effect = DataSourceError(PRIVATE)
        key = uuid4().hex
        original = self.send(request_id=key)
        self.assertEqual(original["result"]["code"], "research_completed")
        self.assertIn("10.166667", original["result"]["answer"])
        self.assert_unavailable(original["result"]["market_context"], "context_unusable")
        self.assertNotIn(PRIVATE, json.dumps(original))
        self.context_loader.side_effect = None
        self.assertEqual(self.send(request_id=key)["result"], original["result"])
        self.assertEqual(self.history()["market_context"], original["result"]["market_context"])
        self.context_loader.assert_called_once_with(NOW)
        self.assertEqual(self.model.complete.call_count, 3)

    def test_no_configured_loader_keeps_research_working_with_explicit_unavailable_context(self):
        self.install_service(market_context_loader=None)
        original = self.send()["result"]
        self.assertEqual(original["code"], "research_completed")
        self.assert_unavailable(original["market_context"], "context_not_configured")
        self.assertEqual(self.history()["market_context"], original["market_context"])
        self.context_loader.assert_not_called()
        self.assertEqual(self.model.complete.call_count, 3)

    def test_failed_new_research_cannot_attach_old_context_but_explicit_history_can(self):
        original = self.send()["result"]["market_context"]
        self.send(FOLLOWUP)
        with patch.object(CsvDailyBarSource, "read_daily_bars", side_effect=DataSourceError(PRIVATE)):
            failed = self.send(EDIT)["result"]
        self.assertEqual(failed["code"], "research_data_failed")
        self.assertIsNone(failed["market_context"])
        implicit = self.send("这里RSI多少")["result"]
        self.assertEqual(implicit["code"], "no_current_result")
        self.assertIsNone(implicit["market_context"])
        self.assertEqual(self.history()["market_context"], original)
        self.context_loader.assert_called_once_with(NOW)

    def test_stopped_turn_retaining_facts_does_not_load_context_or_commit_a_snapshot(self):
        self.install_service(control_factory=lambda: TurnControl(TurnLimits(model_calls=2)))
        turn = self.send()["result"]
        self.assertEqual(turn["code"], "model_call_budget_exhausted")
        self.assertEqual(turn["runtime"]["stop_code"], "model_call_budget_exhausted")
        self.assertIsNotNone(turn["report"])
        self.assertTrue(turn["report"]["fields"])
        self.assertEqual((turn["model_calls_attempted"], turn["analyses_used"]), (2, 1))
        self.assertIsNone(turn["market_context"])
        self.assertEqual(self.entry.market_contexts, {})
        self.assertIsNone(self.service.inspect(self.sid, self.token)["research"]["saved_result"])
        self.assertEqual(self.history()["code"], "no_saved_result")
        self.context_loader.assert_not_called()

    def test_cancelled_inflight_turn_never_loads_context_or_publishes_a_cache_entry(self):
        entered, release = Event(), Event()
        delegate = self.model.complete.side_effect

        def blocked(messages, *, tools):
            entered.set()
            if not release.wait(3):
                raise AssertionError("Test did not release the fake model")
            return delegate(messages, tools=tools)

        self.model.complete.side_effect = blocked
        key = uuid4().hex
        responses, errors = [], []

        def execute():
            try:
                responses.append(self.send(request_id=key))
            except Exception as error:
                errors.append(error)

        thread = Thread(target=execute, daemon=True)
        thread.start()
        try:
            self.assertTrue(entered.wait(3))
            self.assertEqual(self.service.cancel(self.sid, self.token, key)["status"], "cancellation_requested")
        finally:
            release.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(responses[0]["result"]["code"], "turn_cancelled")
        self.assertIsNone(responses[0]["result"]["market_context"])
        self.assertEqual(self.entry.market_contexts, {})
        self.context_loader.assert_not_called()

    def test_knowledge_and_plain_cancel_do_not_load_or_attach_an_old_context(self):
        original = self.send()["result"]["market_context"]
        for question in ("RSI是什么", "取消"):
            with self.subTest(question=question):
                self.assertIsNone(self.send(question)["result"]["market_context"])
        self.assertEqual(self.history()["market_context"], original)
        self.context_loader.assert_called_once_with(NOW)

    def test_expired_saved_result_does_not_expose_cached_context_or_reload_it(self):
        self.send()
        self.service._wall_clock = lambda: NOW + timedelta(minutes=15)
        history = self.history()
        self.assertEqual(history["code"], "saved_result_expired")
        self.assertIsNone(history["market_context"])
        self.context_loader.assert_called_once_with(NOW)

    def test_http_reset_clears_context_and_retires_old_idempotent_response(self):
        key = uuid4().hex
        self.send(request_id=key)
        self.service.reset(self.sid, self.token)
        self.assertEqual(self.entry.market_contexts, {})
        self.assertIsNone(self.history()["market_context"])
        with self.assertRaises(ApiProblem) as caught:
            self.send(request_id=key)
        self.assertEqual(caught.exception.code, "request_id_retired")
        self.context_loader.assert_called_once_with(NOW)

    def test_natural_language_session_reset_clears_context(self):
        self.send()
        reset = self.send("新对话")["result"]
        self.assertEqual(reset["code"], "session_reset")
        self.assertIsNone(reset["market_context"])
        self.assertEqual(self.entry.market_contexts, {})
        self.assertEqual(self.history()["code"], "no_saved_result")
        self.context_loader.assert_called_once_with(NOW)

    def test_delete_clears_context_even_when_a_test_retains_the_entry_reference(self):
        self.send()
        self.service.delete(self.sid, self.token)
        self.assertEqual(self.entry.market_contexts, {})
        self.assertNotIn(self.sid, self.service._entries)

    def test_session_ttl_pruning_clears_context_and_removes_the_entry(self):
        self.send()
        self.seconds += self.service.limits.lifetime_seconds
        with self.assertRaises(ApiProblem) as caught:
            self.service.inspect(self.sid, self.token)
        self.assertEqual(caught.exception.code, "session_not_found")
        self.assertEqual(self.entry.market_contexts, {})
        self.assertNotIn(self.sid, self.service._entries)

    def test_close_clears_context_and_all_idle_sessions(self):
        self.send()
        self.service.close()
        self.assertEqual(self.entry.market_contexts, {})
        self.assertEqual(self.service._entries, {})

    def test_separate_sessions_never_share_or_refresh_one_anothers_context(self):
        first = self.send()["result"]["market_context"]
        other = self.service.create_session()
        empty = self.service.history(other["session_id"], other["access_token"])["result"]
        self.assertIsNone(empty["market_context"])
        self.context_loader.return_value = replace(self.snapshot, snapshot_id="second-session-snapshot")
        second = self.service.message(other["session_id"], other["access_token"], uuid4().hex, QUESTION)["result"]["market_context"]
        self.assertNotEqual(first["request_id"], second["request_id"])
        self.assertNotEqual(first["snapshot_id"], second["snapshot_id"])
        self.assertEqual(self.history()["market_context"], first)
        self.assertEqual(self.context_loader.call_count, 2)
        self.assertEqual(len(self.entry.market_contexts), 1)
        self.assertEqual(len(self.service._entries[other["session_id"]].market_contexts), 1)

    def test_message_limit_also_bounds_the_number_of_cached_contexts(self):
        self.install_service(limits=ApiLimits(messages=1))
        self.send()
        with self.assertRaises(ApiProblem) as caught:
            self.send("RSI是什么")
        self.assertEqual(caught.exception.code, "session_message_limit")
        self.assertEqual(len(self.entry.market_contexts), 1)
        self.assertIsNotNone(self.history()["market_context"])
        self.context_loader.assert_called_once_with(NOW)

    def test_defensive_full_cache_bound_does_not_evict_or_load_another_context(self):
        # Populate the internal bound artificially; normal message quotas already cap growth.
        existing = {f"occupied-{index}": {
            "version": "research-market-context-v1", "status": "unavailable", "code": "context_not_configured",
        } for index in range(self.service.limits.messages)}
        self.entry.market_contexts.update(deepcopy(existing))
        turn = self.send()["result"]
        self.assertEqual(turn["code"], "research_completed")
        self.assert_unavailable(turn["market_context"], "context_cache_limit")
        self.assertEqual(self.entry.market_contexts, existing)
        self.context_loader.assert_not_called()
        self.assert_unavailable(self.history()["market_context"], "context_cache_miss")
        self.context_loader.assert_not_called()

    def test_a_new_sma5_research_freezes_a_new_snapshot_without_rewriting_the_old_one(self):
        first_key = uuid4().hex
        first = self.send(request_id=first_key)["result"]["market_context"]
        self.send(FOLLOWUP)
        states = (replace(self.snapshot.security_states[0], st_status="st"), *self.snapshot.security_states[1:])
        self.context_loader.return_value = replace(
            self.snapshot, snapshot_id="new-sma5-snapshot", security_states=states,
        )
        edited = self.send(EDIT)["result"]
        second = edited["market_context"]
        self.assertEqual(edited["code"], "research_completed")
        self.assertEqual(second["snapshot_id"], "new-sma5-snapshot")
        self.assertNotEqual(first["request_id"], second["request_id"])
        self.assertEqual(second["security"]["st_status"], "st")
        self.assertEqual(first["security"]["st_status"], "not_st")
        self.assertEqual(self.entry.market_contexts[first["request_id"]], first)
        self.assertEqual(self.entry.market_contexts[second["request_id"]], second)
        self.assertEqual(self.history()["market_context"], second)
        self.assertEqual(self.send(request_id=first_key)["result"]["market_context"], first)
        self.assertEqual((self.context_loader.call_count, self.model.complete.call_count), (2, 6))


if __name__ == "__main__":
    unittest.main()
