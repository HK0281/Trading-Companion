"""Offline contracts for trusted startup source selection; no fallback or credentials."""

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import date, datetime, timezone
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from trading_companion.agent_cli import ConversationData, main as cli_main, run_conversation
from trading_companion.agent_input import request_arguments
from trading_companion.api import create_app
from trading_companion.api_server import main as server_main
from trading_companion.api_service import DataModeDependencyError, ResearchApiService, ensure_data_mode_dependencies
from trading_companion.development_fixtures import ROOT, load_development_fixtures
from trading_companion.errors import DataSourceError, DataValidationError
from trading_companion.explanation_demo_model import ExplanationDemoModel
from trading_companion.model import ModelConfigurationError
from trading_companion.multi_contracts import IndicatorSpec, MultiIndicatorRequest
from trading_companion.research_intent import INTENT_TOOL_NAME
from trading_companion.research_tool import TOOL_NAME
from trading_companion.tool_model import ToolCall, ToolModelResponse
from fastapi.testclient import TestClient


NOW = datetime(2026, 9, 26, 2, tzinfo=timezone.utc)
QUESTION = "分析平安银行，2026-09-21 至 2026-09-25，只看SMA3和RSI2"
PRIVATE = "source-dependency-private-marker"


def scripted_model(source):
    request = MultiIndicatorRequest("000001.SZ", date(2026, 9, 21), date(2026, 9, 25),
        (IndicatorSpec("sma", window=3), IndicatorSpec("rsi", window=2)), source=source)
    intent = ToolModelResponse("tool_calls", tool_calls=(ToolCall("intent", INTENT_TOOL_NAME, {
        "intent": "research", "instrument_mentions": ["平安银行"], "date_expression": "2026-09-21 至 2026-09-25",
        "indicator_mentions": ["SMA3", "RSI2"], "clarification_reason": None,
    }),))
    proposal = ToolModelResponse("tool_calls", tool_calls=(ToolCall("analysis", TOOL_NAME, request_arguments(request)),))
    return ExplanationDemoModel([intent, proposal])


class SourceSelectionTests(unittest.TestCase):
    def setUp(self):
        local_file = patch("trading_companion.local_env._read_local_values", return_value={})
        local_file.start()
        self.addCleanup(local_file.stop)
        for guard in (patch("requests.get", side_effect=AssertionError("No network")),
                      patch("requests.post", side_effect=AssertionError("No paid provider")),
                      patch("trading_companion.deepseek_model.DeepSeekConfig.from_env", side_effect=AssertionError("No credentials"))):
            guard.start()
            self.addCleanup(guard.stop)
        self.csv, directory = load_development_fixtures()
        # Artificial records are explicitly relabeled only inside this isolated transport contract test.
        self.directory = replace(directory, is_fixture=False, retrieved_at=NOW)
        self.bars = self.csv.read_daily_bars()

    def service(self, source="akshare_eastmoney", **kwargs):
        with patch("trading_companion.api_service.find_spec", return_value=object()):
            return ResearchApiService(model_factory=lambda: scripted_model(source), directory_loader=lambda _: self.directory,
                provider="fake", data_mode=source, wall_clock=lambda: NOW, **kwargs)

    def test_package_presence_is_checked_only_for_explicit_akshare_mode(self):
        with patch("trading_companion.api_service.find_spec", return_value=None) as find:
            ensure_data_mode_dependencies("synthetic")
            ensure_data_mode_dependencies("tencent")
            find.assert_not_called()
            with self.assertRaises(DataModeDependencyError):
                ensure_data_mode_dependencies("akshare_eastmoney")
            find.assert_called_once_with("akshare")

    def test_broken_package_discovery_is_sanitized_and_never_falls_back(self):
        for error in (ImportError(PRIVATE), ValueError(PRIVATE), AttributeError(PRIVATE)):
            with self.subTest(error=type(error).__name__), patch("trading_companion.api_service.find_spec", side_effect=error):
                with self.assertRaises(DataModeDependencyError) as caught:
                    ensure_data_mode_dependencies("akshare_eastmoney")
                self.assertNotIn(PRIVATE, str(caught.exception))

    def test_unknown_modes_are_not_accepted_as_live_sources(self):
        for source in ("csv", "auto", "eastmoney", "https://provider.invalid"):
            with self.subTest(source=source), self.assertRaises(ModelConfigurationError):
                ensure_data_mode_dependencies(source)

    def test_service_rejects_missing_akshare_before_a_model_session_is_created(self):
        factory = Mock()
        with patch("trading_companion.api_service.find_spec", return_value=None), self.assertRaises(DataModeDependencyError):
            ResearchApiService(model_factory=factory, directory_loader=lambda _: self.directory,
                               data_mode="akshare_eastmoney")
        factory.assert_not_called()

    def test_akshare_cli_configuration_is_explicit_and_uses_the_same_local_directory_rules(self):
        with patch("trading_companion.api_service.find_spec", return_value=object()):
            self.assertEqual(ConversationData.from_options("akshare_eastmoney").directory_path,
                             ROOT / "data/security_directory_live.json")
            selected = ConversationData.from_options("akshare_eastmoney", "selected.json")
        self.assertEqual(selected.directory_path, Path("selected.json"))
        self.assertIsNone(selected.development_source)

    def test_online_modes_cannot_receive_development_csv_or_pinned_fixture_identity(self):
        with patch("trading_companion.api_service.find_spec", return_value=object()):
            with self.assertRaises(ModelConfigurationError):
                ConversationData("akshare_eastmoney", Path("selected.json"), self.csv)
            with self.assertRaises(ModelConfigurationError):
                ResearchApiService(model_factory=Mock(), directory_loader=Mock(), data_mode="akshare_eastmoney", development_source=self.csv)
            selected = ConversationData.from_options("akshare_eastmoney")
        with patch("trading_companion.agent_cli.load_security_directory", return_value=load_development_fixtures()[1]):
            with self.assertRaises(DataValidationError):
                selected.load_directory(NOW)

    def test_both_product_launchers_report_missing_dependency_without_keys_directory_or_network(self):
        for launcher in (cli_main, server_main):
            output, errors = StringIO(), StringIO()
            with self.subTest(launcher=launcher.__module__), redirect_stdout(output), redirect_stderr(errors), patch(
                "trading_companion.api_service.find_spec", return_value=None), patch(
                "trading_companion.agent_cli.load_security_directory", side_effect=AssertionError("No directory")), patch(
                "uvicorn.run", side_effect=AssertionError("No server")):
                self.assertEqual(launcher(["--provider", "deepseek", "--data", "akshare_eastmoney"]), 2)
            self.assertIn("data_dependency_missing", errors.getvalue())
            self.assertIn(".[data]", errors.getvalue())
            self.assertIn("不自动切换来源", errors.getvalue())
            self.assertNotIn(PRIVATE, output.getvalue() + errors.getvalue())

    def test_help_advertises_akshare_and_optional_extra_without_inspecting_dependencies(self):
        for launcher in (cli_main, server_main):
            with self.subTest(launcher=launcher.__module__), redirect_stdout(StringIO()) as output, patch(
                "trading_companion.api_service.find_spec", side_effect=AssertionError("Help does not discover packages")):
                self.assertEqual(launcher([]), 0)
            self.assertIn("akshare_eastmoney", output.getvalue())
            self.assertIn(".[data]", output.getvalue())

    def test_demo_cannot_silently_enable_a_live_source(self):
        for launcher in (cli_main, server_main):
            with self.subTest(launcher=launcher.__module__), redirect_stderr(StringIO()), self.assertRaises(SystemExit) as caught:
                launcher(["--demo", "--data", "akshare_eastmoney"])
            self.assertEqual(caught.exception.code, 2)

    def test_service_uses_one_selected_source_for_analysis_saved_recheck_chart_and_history(self):
        for mode in ("akshare_eastmoney", "tencent"):
            source = SimpleNamespace(source_name=mode, adjustment="unadjusted", read_daily_bars=Mock(return_value=self.bars))
            with self.subTest(mode=mode), patch("trading_companion.research_tool._build_source", return_value=source) as build:
                service = self.service(mode)
                credentials = service.create_session()
                sid, token = credentials["session_id"], credentials["access_token"]
                response = service.message(sid, token, uuid4().hex, QUESTION)
                result = response["result"]
                self.assertEqual(credentials["data_mode"], mode)
                self.assertEqual(result["status"], "completed")
                self.assertEqual(result["parameters"]["source"], mode)
                self.assertEqual(result["chart"]["status"], "available")
                self.assertEqual(result["chart"]["source"], mode)
                self.assertEqual(result["market_context"]["status"], "unavailable")
                history = service.history(sid, token)["result"]
                self.assertEqual(history["code"], "saved_result_shown")
                self.assertEqual(history["parameters"]["source"], mode)
                self.assertEqual(history["chart"]["source"], mode)
                self.assertEqual(history["chart"]["context"], "saved")
                self.assertEqual(history["model_calls_attempted"], 0)
                source.read_daily_bars.assert_called_once()
                self.assertEqual([call.args[0].source for call in build.call_args_list], [mode])
                service.close()

    def test_a_selected_source_failure_is_not_retried_or_replaced(self):
        service = self.service()
        credentials = service.create_session()
        with patch("trading_companion.research_tool._build_source", side_effect=DataSourceError(PRIVATE)) as build:
            result = service.message(credentials["session_id"], credentials["access_token"], uuid4().hex, QUESTION)["result"]
        self.assertNotEqual(result["status"], "completed")
        self.assertEqual(result["parameters"]["source"], "akshare_eastmoney")
        self.assertNotIn(PRIVATE, str(result))
        build.assert_called_once()
        self.assertEqual(build.call_args.args[0].source, "akshare_eastmoney")
        service.close()

    def test_http_health_and_session_publish_the_same_selected_mode_but_do_not_accept_per_request_source(self):
        service = self.service()
        with TestClient(create_app(service=service), base_url="http://localhost") as client:
            self.assertEqual(client.get("/health").json()["data_mode"], "akshare_eastmoney")
            credentials = client.post("/v1/sessions", json={}).json()
            self.assertEqual(credentials["data_mode"], "akshare_eastmoney")
            path = f"/v1/sessions/{credentials['session_id']}/messages"
            headers = {"Authorization": "Bearer " + credentials["access_token"]}
            response = client.post(path, headers=headers, json={"request_id": uuid4().hex, "question": QUESTION, "source": "tencent"})
            self.assertEqual(response.status_code, 422)
            self.assertEqual(client.get("/health").json()["data_mode"], "akshare_eastmoney")

    def test_cli_research_and_last_reuse_akshare_without_new_fetch(self):
        model = scripted_model("akshare_eastmoney")
        source = SimpleNamespace(source_name="akshare_eastmoney", adjustment="unadjusted", read_daily_bars=Mock(return_value=self.bars))
        with patch("trading_companion.api_service.find_spec", return_value=object()):
            data = ConversationData.from_options("akshare_eastmoney")
        replies = iter((QUESTION, "/last", "/exit"))
        output = []
        with patch("trading_companion.agent_cli.load_security_directory", return_value=self.directory), patch(
            "trading_companion.research_tool._build_source", return_value=source) as build:
            self.assertEqual(run_conversation(model=model, data=data, provider="fake", read_line=lambda _: next(replies),
                write_line=output.append, clock=lambda: NOW), 0)
        self.assertIn("AKShare / 东方财富不复权历史日线", "\n".join(output))
        self.assertIn("saved_result_shown", "\n".join(output))
        self.assertIn("不能切源", "\n".join(output))
        source.read_daily_bars.assert_called_once()
        self.assertEqual(build.call_args.args[0].source, "akshare_eastmoney")

    def test_server_startup_keeps_live_market_context_opt_in_and_does_not_load_it(self):
        from trading_companion.deepseek_model import DeepSeekConfig
        services = []
        with redirect_stdout(StringIO()) as output, patch("trading_companion.api_service.find_spec", return_value=object()), patch(
            "trading_companion.security_directory.load_security_directory", return_value=SimpleNamespace(assert_usable=Mock())), patch(
            "trading_companion.api.create_app", side_effect=lambda *, service: services.append(service) or "test-app"), patch(
            "trading_companion.deepseek_model.DeepSeekConfig.from_env",
            return_value=DeepSeekConfig("fixture-model", "artificial-test-key")), patch(
            "trading_companion.market_context.load_market_context", side_effect=AssertionError("No implicit snapshot")), patch("uvicorn.run") as run:
            self.assertEqual(server_main(["--provider", "deepseek", "--data", "akshare_eastmoney"]), 0)
        self.assertEqual(len(services), 1)
        self.assertEqual(services[0].data_mode, "akshare_eastmoney")
        self.assertIsNone(services[0]._development_source)
        self.assertIsNone(services[0]._market_context_loader)
        self.assertEqual(run.call_args.kwargs["host"], "127.0.0.1")
        self.assertIn("失败不自动换源", output.getvalue())
        services[0].close()


if __name__ == "__main__":
    unittest.main()
