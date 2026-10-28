"""Expanded-source integration with real session publication and launcher boundaries."""

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Event
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from trading_companion import api_server
from trading_companion.api import create_app
from trading_companion.collection_execution import CollectionExecutor
from trading_companion.screening_source import TencentQuoteSnapshotSource
from trading_companion.turn_control import TurnControl, TurnLimits
from trading_companion.turn_trace import JsonlTraceSink
from tests.test_collection_execution import BackgroundCall, MutableClock
from tests.test_screening_collection_concurrency import QuoteTransport
from tests.test_screening_agent import Model
from tests.test_screening_coverage import large_directory
from tests.test_screening_source import NOW, TencentSession, Response
from tests.test_screening_followup_api import ScreeningJourneyFixture
from tests.test_turn_trace import read_records


class ExpandedCoverageApiTests(ScreeningJourneyFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.directory = large_directory()
        self.model.directory = self.directory
        self.model.screening = Model(limit=1)
        self.wall = NOW
        self.transport = TencentSession(self.directory, self.prices)
        self.quotes = TencentQuoteSnapshotSource(session=self.transport, clock=lambda: self.wall, coverage="expanded")
        self.install()

    @staticmethod
    def prices(lines):
        for symbol, fields in lines:
            fields[3] = "99" if symbol == "sh600398" else "1"
        return lines

    def test_edit_rereads_expanded_scope_and_publishes_only_displayed_candidates(self):
        first = self.send("帮我选1只股票")["result"]
        self.assertEqual(first["screening"]["coverage"]["scanned_records"], 1000)
        self.assertEqual(len(self.transport.calls), 15)
        self.assertEqual(len(self.session.screening_saved.securities), 1)
        self.assertNotEqual(first["screening"]["rows"][0]["instrument_id"], "600398.SH")
        result = self.send("再加价格大于50")["result"]
        self.assertEqual(result["screening"]["coverage"]["scanned_records"], 1000)
        self.assertEqual(result["screening"]["rows"][0]["instrument_id"], "600398.SH")
        self.assertEqual(tuple(item.instrument_id for item in self.session.screening_saved.securities), ("600398.SH",))
        self.assertEqual((len(self.transport.calls), len(self.model.calls)), (30, 1))

    def test_source_failure_after_one_batch_never_publishes_partial_or_old_candidates(self):
        self.send("帮我选1只股票")
        original = self.transport.request
        calls = 0
        def fail(*args, **kwargs):
            nonlocal calls
            calls += 1
            return original(*args, **kwargs) if calls == 1 else Response(status=429)
        with patch.object(self.transport, "request", side_effect=fail):
            result = self.send("再加价格大于50")["result"]
        self.assertEqual((result["code"], calls), ("screening_data_unavailable", 2))
        self.assertIsNone(result.get("screening"))
        self.assertIsNone(self.session.screening_saved)
        self.assertEqual(len(self.model.calls), 1)

    def test_cancel_or_deadline_during_middle_batch_cannot_publish_any_candidates(self):
        for stop in ("cancel", "deadline"):
            with self.subTest(stop=stop):
                elapsed = [0.0]
                control = TurnControl(TurnLimits(), clock=lambda: elapsed[0])
                self.install(control_factory=lambda: control)
                original = self.transport.request
                calls = 0
                def stopped(*args, **kwargs):
                    nonlocal calls
                    response = original(*args, **kwargs)
                    calls += 1
                    if calls == 2:
                        if stop == "cancel": control.cancel()
                        else: elapsed[0] = 91.0
                    return response
                with patch.object(self.transport, "request", side_effect=stopped):
                    result = self.send("帮我选1只股票")["result"]
                self.assertEqual(calls, 2)
                self.assertIsNone(result.get("screening"))
                self.assertIsNone(self.session.screening_saved)
                self.assertEqual(result["status"] if stop == "cancel" else result["code"],
                    "cancelled" if stop == "cancel" else "turn_deadline_exceeded")
                self.assertTrue(all(item.closed for item in self.transport.responses))


class ConcurrentCoverageApiTests(ScreeningJourneyFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.pool = CollectionExecutor()
        self.addCleanup(self.pool.close, wait=True)
        self.directory = large_directory(3)
        self.model.directory = self.directory
        self.model.screening = Model(limit=1)
        self.wall = NOW
        self.http = QuoteTransport(self.directory)
        self.quotes = TencentQuoteSnapshotSource(session_factory=self.http.factory,
            executor=self.pool, clock=lambda: self.wall)
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path, self.archive = Path(self.temporary.name), Mock()

    def test_failure_discards_old_and_partial_list_and_late_success_cannot_overwrite_new_turn(self):
        self.install(trace_sink=JsonlTraceSink(self.path), snapshot_store=self.archive)
        self.send("帮我选1只股票")
        self.assertIsNotNone(self.session.screening_saved)
        entered, release, barrier = Event(), Event(), Barrier(3)
        base = len(self.http.calls)
        def hook(index, url, kwargs):
            if index < base + 3:
                barrier.wait(5)
            if index == base:
                entered.set()
                if not release.wait(5): raise AssertionError("Unreleased failed-turn late batch")
            elif index == base + 1:
                return Response(status=429)
        self.http.hook = hook
        call = BackgroundCall(lambda: self.send("再加价格大于50"))
        try:
            self.assertTrue(entered.wait(5))
            result = call.result()["result"]
            self.assertEqual(result["code"], "screening_data_unavailable")
            self.assertIsNone(result.get("screening"))
            self.assertIsNone(self.session.screening_saved)
            record = deepcopy(read_records(self.path)[-1])
            self.http.hook = None
            next_result = self.send("帮我选1只股票")["result"]
            self.assertIsNotNone(next_result.get("screening"))
            saved = deepcopy(self.session.to_dict())
            frozen = deepcopy(self.service._entries[self.sid].records)
            release.set()
            self.pool.close(wait=True)
            self.assertEqual(self.session.to_dict(), saved)
            self.assertEqual(self.service._entries[self.sid].records, frozen)
            self.assertEqual(read_records(self.path)[-2], record)
            self.assertEqual(self.archive.mock_calls, [])
            self.assertTrue(all(client.closed == 1 for client in self.http.clients))
            self.assertTrue(all(response.closed for response in self.http.responses))
        finally:
            release.set()

    def test_cancel_and_turn_deadline_return_before_late_batch_and_freeze_session_cache_archive_trace(self):
        for stop in ("cancel", "deadline", "source_deadline"):
            with self.subTest(stop=stop):
                timer, controls = MutableClock(), []
                source_timer = MutableClock()
                self.quotes._timer = source_timer
                def control_factory():
                    control = TurnControl(TurnLimits(), clock=timer)
                    controls.append(control)
                    return control
                # Each subcase has its own service/session, with a real preexisting list.
                self.http.hook = None
                self.install(control_factory=control_factory, trace_sink=JsonlTraceSink(self.path), snapshot_store=self.archive)
                self.send("帮我选1只股票")
                entered, release = Event(), Event()
                base = len(self.http.calls)
                def hook(index, url, kwargs):
                    if index == base:
                        entered.set()
                        if not release.wait(5): raise AssertionError("Unreleased cancelled batch")
                self.http.hook = hook
                key = uuid4().hex
                call = BackgroundCall(lambda: self.send("再加价格大于50", request_id=key))
                try:
                    self.assertTrue(entered.wait(5))
                    if stop == "cancel": self.service.cancel(self.sid, self.token, key)
                    elif stop == "deadline": timer.set(91)
                    else: source_timer.set(25)
                    result = call.result()["result"]
                    self.assertEqual(result["code"], {"cancel": "turn_cancelled", "deadline": "turn_deadline_exceeded",
                                                      "source_deadline": "screening_data_unavailable"}[stop])
                    self.assertIsNone(result.get("screening"))
                    self.assertIsNone(self.session.screening_saved)
                    stopped = deepcopy(read_records(self.path)[-1])
                    self.http.hook = None
                    self.assertIsNotNone(self.send("帮我选1只股票")["result"].get("screening"))
                    saved = deepcopy(self.session.to_dict())
                    records = deepcopy(self.service._entries[self.sid].records)
                    release.set()
                    # Owned-client closure signals that each old worker has left its network boundary.
                    for client in self.http.clients:
                        self.assertTrue(client.finished.wait(5))
                    self.assertEqual(self.session.to_dict(), saved)
                    self.assertEqual(self.service._entries[self.sid].records, records)
                    self.assertEqual(read_records(self.path)[-2], stopped)
                    self.assertEqual(self.archive.mock_calls, [])
                finally:
                    release.set()


class CoverageLauncherTests(unittest.TestCase):
    def test_expanded_configuration_reaches_normal_service_without_leaking_into_dev_mode(self):
        from trading_companion.screening_source import configured_quote_source
        trusted = replace(large_directory(5), retrieved_at=datetime.now(timezone.utc))
        for provider in ("tencent", "eastmoney"):
            with (self.subTest(provider=provider), patch("trading_companion.local_env._read_local_values",
                    side_effect=AssertionError("No credentials")), patch("requests.request",
                    side_effect=AssertionError("No live quote data")), patch("trading_companion.security_directory.load_security_directory",
                    return_value=trusted), patch("trading_companion.model_registry.configured_models",
                    return_value={"deepseek": lambda: Model()}), patch("trading_companion.api.create_app", wraps=create_app) as app,
                    patch("trading_companion.screening_source.configured_quote_source", wraps=configured_quote_source) as factory,
                    patch("uvicorn.run"), redirect_stdout(StringIO())):
                self.assertEqual(api_server.main(["--screening-data", provider, "--screening-coverage", "expanded"]), 0)
                factory.assert_called_once_with(provider, "expanded", max_workers=3)
                options = next(call.kwargs for call in app.call_args_list if "development_service" in call.kwargs)
                service, dev = options["service"], options["development_service"]
                self.assertIsNone(dev._screening_source)
                if provider == "tencent":
                    self.assertEqual((service._screening_source._per_stratum, service._screening_source._coverage), (200, "expanded"))
                else:
                    self.assertEqual(service._screening_source._pages, 2)

    def test_synthetic_launches_reject_expanded_configuration_before_loading_models(self):
        with patch("trading_companion.model_registry.configured_models") as models, patch("uvicorn.run") as server, redirect_stderr(StringIO()):
            for flags in (["--demo"], ["--data", "synthetic"]):
                with self.subTest(flags=flags), self.assertRaises(SystemExit):
                    api_server.main([*flags, "--screening-coverage", "expanded"])
            models.assert_not_called()
            server.assert_not_called()
