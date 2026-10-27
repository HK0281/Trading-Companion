"""Expanded-source integration with real session publication and launcher boundaries."""

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import datetime, timezone
from io import StringIO
import unittest
from unittest.mock import patch

from trading_companion import api_server
from trading_companion.api import create_app
from trading_companion.screening_source import TencentQuoteSnapshotSource
from trading_companion.turn_control import TurnControl, TurnLimits
from tests.test_screening_agent import Model
from tests.test_screening_coverage import large_directory
from tests.test_screening_source import NOW, TencentSession, Response
from tests.test_screening_followup_api import ScreeningJourneyFixture


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
                factory.assert_called_once_with(provider, "expanded")
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
