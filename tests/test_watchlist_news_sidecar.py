"""The optional local sink cannot change frozen research requests, evidence or answers."""

from contextlib import ExitStack
from dataclasses import replace
from itertools import count
from threading import Event, Thread
from time import monotonic
import unittest
from unittest.mock import patch
from uuid import UUID

from scripts.verify import offline_network
from trading_companion.api_service import ResearchApiService
from trading_companion.personal_memory_store import SqlitePersonalMemoryStore
from trading_companion.turn_control import TurnControl, TurnLimits, TurnStopped, current_control
from trading_companion.watchlist_news import SOURCES, WatchlistNewsError
from trading_companion.watchlist_news_sidecar import bind_research_inbox, prepare_research_inbox
from tests import routing_baseline_support as routing
from tests import intent_integration_support as integration
from tests.test_product_agent import ProductModelFixture
from tests.test_product_research import CompanyDataFixture, MemorySourceFactory, NOW, directory, plan


def public_title_batch(batch):
    sections = []
    for section in batch.source_results:
        if section.kind not in ("news", "announcements"):
            sections.append(section)
            continue
        source = SOURCES[section.kind == "announcements"]
        url = ("https://finance.sina.com.cn/stock/doc-sidecartest.shtml" if source == SOURCES[0] else
               "https://data.eastmoney.com/notices/detail/" + batch.request.instrument_id[:6] + "/AN123456789012.html")
        items = tuple(replace(item, source=source, url=url) for item in section.items)
        sections.append(replace(section, source=source, items=items))
    return replace(batch, source_results=tuple(sections))


class SidecarFrozenParityTests(unittest.TestCase):
    def observe(self, scenario, observer, *, enabled):
        requests, snapshots = [], []
        original = routing.Journey.__init__
        request_normalizer = routing.Normalizer()

        def initialize(journey, stack, config):
            original(journey, stack, config)
            forwarded = journey.model.complete
            def capture(messages, *, tools):
                requests.append(request_normalizer({"messages": [message.to_dict() for message in messages],
                    "tools": [tool.to_dict() for tool in tools]}))
                return forwarded(messages, tools=tools)
            stack.enter_context(patch.object(journey.model, "complete", capture))
            if enabled:
                store = SqlitePersonalMemoryStore(":memory:", clock=lambda: NOW)
                stack.callback(store.close)
                for security in journey.directory.securities[:50]:
                    store.add(instrument_id=security.instrument_id, name=security.name)
                original_snapshot = store.inbox_snapshot
                def snapshot(*args, **kwargs):
                    snapshots.append(kwargs.get("deadline"))
                    return original_snapshot(*args, **kwargs)
                stack.enter_context(patch.object(store, "inbox_snapshot", snapshot))
                # The historical observer intentionally uses a fake model. Inject only this
                # isolated test sink after construction, leaving its frozen source untouched.
                journey.service._personal_memory_store = store

        with ExitStack() as stack:
            # Index plan hashes include the random request ID itself. Fix the input
            # factories rather than normalizing away those meaningful output hashes.
            sequence = count(1)
            for module in ("api_service", "index_agent", "index_contracts", "multi_contracts", "workflow_tools"):
                stack.enter_context(patch("trading_companion." + module + ".uuid4", lambda: UUID(int=next(sequence))))
            stack.enter_context(patch.object(routing.Journey, "__init__", initialize))
            observations = observer(scenario)
        return observations, requests, snapshots

    def test_every_frozen_routing_scenario_keeps_exact_requests_evidence_and_answers(self):
        scenarios = routing.read_baseline(routing.ROOT / "tests/fixtures/request-routing-v2.json")["scenarios"]
        exercised = 0
        for scenario in scenarios:
            with self.subTest(scenario=scenario["id"]):
                off, off_requests, _ = self.observe(scenario, routing.observe_scenario, enabled=False)
                on, on_requests, snapshots = self.observe(scenario, routing.observe_scenario, enabled=True)
                self.assertEqual(routing.canonical_json(on_requests), routing.canonical_json(off_requests))
                self.assertEqual(on, off)
                exercised += len(snapshots)
        self.assertGreater(exercised, 0, "The optional sink must actually execute in company scenarios")

    def test_complete_existing_intent_integration_corpus_keeps_exact_requests_and_business_results(self):
        exercised = 0
        for scenario in integration.scenarios():
            with self.subTest(scenario=scenario["id"]):
                off, off_requests, _ = self.observe(scenario, integration.observe, enabled=False)
                on, on_requests, snapshots = self.observe(scenario, integration.observe, enabled=True)
                self.assertEqual(routing.canonical_json(on_requests), routing.canonical_json(off_requests))
                self.assertEqual(on, off)
                exercised += len(snapshots)
        self.assertGreater(exercised, 0)


class ResearchSidecarTests(unittest.TestCase):
    def setUp(self):
        guard = offline_network()
        guard.__enter__()
        self.addCleanup(guard.__exit__, None, None, None)
        denied = patch("trading_companion.local_env._read_local_values", side_effect=AssertionError("No credentials"))
        denied.start()
        self.addCleanup(denied.stop)

    def service(self, *, enabled=True, on_collect=None, model=None):
        store = SqlitePersonalMemoryStore(":memory:", clock=lambda: NOW)
        self.addCleanup(store.close)
        store.add(instrument_id="000001.SZ", name="平安银行")
        model = model or ProductModelFixture()
        data = CompanyDataFixture(transform=public_title_batch, on_collect=on_collect)
        bars = MemorySourceFactory()
        def control():
            return TurnControl(TurnLimits(model_calls=6, analyses=3, profile="company_research"), clock=lambda: 10.0)
        service = ResearchApiService(model_factory=lambda: model, directory_loader=lambda _: directory(),
            provider="openai", data_mode="tencent", product_data_service=data, wall_clock=lambda: NOW,
            clock=lambda: 100.0, control_factory=control, personal_memory_store=store if enabled else None)
        self.addCleanup(service.close)
        credentials = service.create_session()
        return store, service, credentials, model, data, bars

    def run_message(self, setup, question="分析平安银行的基本面和新闻公告"):
        store, service, credentials, model, data, bars = setup
        with patch("trading_companion.product_research._build_source", side_effect=bars):
            result = service.message(credentials["session_id"], credentials["access_token"], "sidecar-test", question)
        return result

    def comparable(self, setup, response):
        normalizer = routing.Normalizer()
        model = setup[3]
        return normalizer({"response": response, "requests": [
            {"messages": [message.to_dict() for message in messages], "tools": [tool.to_dict() for tool in tools]}
            for messages, tools in model.calls], "data_calls": setup[4].calls, "bar_reads": setup[5].reads})

    def test_real_store_saves_before_synthesis_without_extra_source_or_model_requests(self):
        off, on = self.service(enabled=False), self.service()
        saved_before_answer = []
        on[3].on_synthesis = lambda: saved_before_answer.append(on[0]._connection.execute("SELECT count(*) FROM inbox_messages").fetchone()[0])
        off_result, on_result = self.run_message(off), self.run_message(on)
        self.assertEqual(self.comparable(off, off_result), self.comparable(on, on_result))
        self.assertTrue(saved_before_answer)
        self.assertGreater(saved_before_answer[0], 0)
        inbox = on[0].list_inbox()
        self.assertEqual({item["source"] for item in inbox["items"]}, set(SOURCES))
        self.assertTrue(all(item["discovery"] == "research" for item in inbox["items"]))
        self.assertTrue(all(state["last_success_at"] is None for state in inbox["source_states"]))

    def test_store_failure_is_isolated_from_exact_model_requests_evidence_and_answer(self):
        off, failed = self.service(enabled=False), self.service()
        with patch.object(failed[0], "ingest_inbox", side_effect=WatchlistNewsError("inbox_store_unavailable")):
            failed_result = self.run_message(failed)
        off_result = self.run_message(off)
        self.assertEqual(self.comparable(off, off_result), self.comparable(failed, failed_result))
        self.assertEqual(failed[0].list_inbox()["items"], [])

    def test_prepare_lock_abandons_within_50ms_and_capture_lock_within_150ms(self):
        store = self.service()[0]
        entered, release = Event(), Event()
        def hold():
            with store._lock:
                entered.set()
                release.wait(2)
        thread = Thread(target=hold)
        thread.start()
        try:
            self.assertTrue(entered.wait(1))
            started = monotonic()
            with bind_research_inbox(store):
                self.assertIsNone(prepare_research_inbox(plan()))
            self.assertLess(monotonic() - started, .2)
        finally:
            release.set()
            thread.join(2)
        # Prepare a real sink first, then contend only during the capture stage.
        from trading_companion.product_research import collect_product_research
        with bind_research_inbox(store):
            sink = prepare_research_inbox(plan(kinds=("news", "announcements")))
        bundle = collect_product_research(plan(kinds=("news", "announcements")),
            data_service=CompanyDataFixture(transform=public_title_batch), now=NOW, source_factory=MemorySourceFactory())
        entered.clear()
        release.clear()
        thread = Thread(target=hold)
        thread.start()
        try:
            self.assertTrue(entered.wait(1))
            started = monotonic()
            sink.capture(bundle)
            self.assertLess(monotonic() - started, .3)
        finally:
            release.set()
            thread.join(2)
        self.assertEqual(store.list_inbox()["items"], [])

    def test_remove_and_readd_during_data_read_cannot_resurrect_captured_membership(self):
        setup = self.service()
        store = setup[0]
        def readd():
            store.remove(instrument_id="000001.SZ")
            store.add(instrument_id="000001.SZ", name="平安银行")
        setup[4].on_collect = readd
        result = self.run_message(setup)
        self.assertEqual(result["result"]["code"], "company_research_completed")
        self.assertEqual(store.list_inbox()["items"], [])

    def test_true_turn_cancellation_is_not_swallowed_by_optional_capture(self):
        setup = self.service()
        def cancel(*args, **kwargs):
            current_control().cancel()
            raise TurnStopped("turn_cancelled")
        with patch.object(setup[0], "ingest_inbox", side_effect=cancel):
            result = self.run_message(setup)
        self.assertEqual(result["result"]["code"], "turn_cancelled")
        self.assertEqual(len(setup[3].calls), 1, "No synthesis after a genuine research cancellation")
        self.assertEqual(setup[0].list_inbox()["items"], [])

    def test_cancellation_after_staged_sql_rolls_back_messages_seen_and_source_revision(self):
        setup = self.service()
        store = setup[0]
        before = store.inbox_snapshot()
        ingest = store.ingest_inbox
        staged_counts = []

        def cancel_before_commit(*args, **kwargs):
            checkpoint = kwargs["before_commit"]
            self.assertTrue(callable(checkpoint))
            def cancel():
                staged_counts.append(store._connection.execute("SELECT count(*) FROM inbox_messages").fetchone()[0])
                self.assertGreater(store._connection.execute("SELECT count(*) FROM inbox_seen").fetchone()[0], 0)
                current_control().cancel()
                checkpoint()
            kwargs["before_commit"] = cancel
            return ingest(*args, **kwargs)

        with patch.object(store, "ingest_inbox", side_effect=cancel_before_commit):
            result = self.run_message(setup)
        self.assertTrue(staged_counts)
        self.assertGreater(staged_counts[0], 0, "Exercise cancellation after real INSERTs, before commit")
        self.assertEqual(result["result"]["code"], "turn_cancelled")
        self.assertEqual(len(setup[3].calls), 1)
        self.assertEqual(len(setup[4].calls), 1)
        self.assertEqual(store.inbox_snapshot(), before)
        for table in ("inbox_messages", "inbox_seen"):
            self.assertEqual(store._connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0)

    def test_historical_replay_and_explanation_do_not_read_or_write_the_sink(self):
        historical = self.service(model=ProductModelFixture(date_expression="2026-09-01至2026-09-20"))
        with patch.object(historical[0], "inbox_snapshot", side_effect=AssertionError("No historical local read")) as snapshot:
            result = self.run_message(historical, "分析平安银行2026-09-01至2026-09-20的基本面和新闻公告")
            self.assertTrue(result["result"]["company_research"]["explicit_historical"])
            snapshot.assert_not_called()
        current = self.service()
        self.run_message(current)
        credentials = current[2]
        with patch.object(current[0], "inbox_snapshot", side_effect=AssertionError("No replay local read")) as snapshot:
            replay = self.run_message(current)
            self.assertTrue(replay["replayed"])
            current[1].history(credentials["session_id"], credentials["access_token"])
            current[1].message(credentials["session_id"], credentials["access_token"], "explain-test", "为什么这样判断？")
            snapshot.assert_not_called()


if __name__ == "__main__":
    unittest.main()
