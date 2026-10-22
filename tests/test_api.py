"""HTTP contracts use ASGI/TestClient, not paid models or real market requests."""

from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timezone
from io import StringIO
import json
import os
from threading import Event, Thread
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

try:
    from fastapi.testclient import TestClient
except ImportError:
    raise unittest.SkipTest("Install .[api,dev] to run optional HTTP tests")

from trading_companion.api import MAX_BODY_BYTES, create_app
from trading_companion.api_server import main as server_main
from trading_companion.api_service import ApiLimits, ResearchApiService
from trading_companion.development_fixtures import REFERENCE_DATE, load_development_fixtures
from trading_companion.followup_demo import ConversationDemoModel, QUESTION, FOLLOWUP
from trading_companion.errors import DataSourceError


NOW = datetime(2026, 9, 26, 2, tzinfo=timezone.utc)
PRIVATE = "private-http-marker-never-export"


class HttpTests(unittest.TestCase):
    def setUp(self):
        for guard in (patch.dict(os.environ, {}, clear=True),
                      patch("requests.get", side_effect=AssertionError("No market HTTP")),
                      patch("requests.post", side_effect=AssertionError("No model HTTP")),
                      patch("trading_companion.deepseek_model.DeepSeekConfig.from_env",
                            side_effect=AssertionError("No credential reads"))):
            guard.start()
            self.addCleanup(guard.stop)
        self.source, self.directory = load_development_fixtures()
        self.loader = Mock(return_value=self.directory)
        self.factory = Mock(side_effect=ConversationDemoModel)
        self.service = self.build()
        self.client = TestClient(create_app(service=self.service), base_url="http://localhost")
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.credentials = self.client.post("/v1/sessions", json={}).json()
        self.sid = self.credentials["session_id"]
        self.token = self.credentials["access_token"]
        self.headers = {"Authorization": "Bearer " + self.token}
        self.path = "/v1/sessions/" + self.sid

    def build(self, **kwargs):
        options = dict(model_factory=self.factory, directory_loader=self.loader, development_source=self.source,
                       reference_date=REFERENCE_DATE, wall_clock=lambda: NOW)
        options.update(kwargs)
        return ResearchApiService(**options)

    def send(self, question=QUESTION, request_id=None, **kwargs):
        return self.client.post(self.path + "/messages", headers=self.headers,
            json={"question": question, "request_id": request_id or uuid4().hex}, **kwargs)

    def test_health_is_local_readiness_not_provider_connectivity(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["trading_enabled"])
        self.loader.assert_not_called()
        self.assertEqual(self.factory.call_count, 1)

    def test_docs_and_openapi_have_routes_but_no_credentials(self):
        self.assertEqual(self.client.get("/docs").status_code, 200)
        schema = self.client.get("/openapi.json").json()
        self.assertIn("/v1/sessions/{session_id}/cancel", schema["paths"])
        self.assertIn("HTTPBearer", schema["components"]["securitySchemes"])
        self.assertNotIn(self.token, json.dumps(schema))

    def test_json_responses_disable_caching(self):
        for response in (self.client.get("/health"), self.send("RSI是什么"), self.client.post("/v1/sessions", json={})):
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(response.headers["x-content-type-options"], "nosniff")

    def test_missing_bearer_is_401_and_wrong_token_is_404(self):
        self.assertEqual(self.client.get(self.path).status_code, 401)
        self.assertEqual(self.client.get(self.path, headers={"Authorization": "Basic aaaa"}).status_code, 401)
        response = self.client.get(self.path, headers={"Authorization": "Bearer " + PRIVATE})
        self.assertEqual(response.status_code, 404)
        self.assertNotIn(PRIVATE, response.text)

    def test_cross_session_bearer_cannot_read_or_cancel(self):
        other = self.client.post("/v1/sessions", json={}).json()
        headers = {"Authorization": "Bearer " + other["access_token"]}
        self.assertEqual(self.client.get(self.path, headers=headers).status_code, 404)
        response = self.client.post(self.path + "/cancel", headers=headers, json={"request_id": uuid4().hex})
        self.assertEqual(response.status_code, 404)

    def test_invalid_ids_are_sanitized(self):
        response = self.client.get("/v1/sessions/" + PRIVATE, headers=self.headers)
        self.assertEqual(response.status_code, 422)
        self.assertNotIn(PRIVATE, response.text)

    def test_blank_long_or_wrong_type_questions_fail_without_model_calls(self):
        for question in ("", " \n ", "x" * 2001, 12, True, ["RSI"]):
            with self.subTest(question_type=type(question).__name__):
                self.assertEqual(self.send(question).status_code, 422)
        self.loader.assert_not_called()
        self.assertEqual(self.service.inspect(self.sid, self.token)["messages_used"], 0)

    def test_unknown_fields_cannot_configure_source_model_key_or_paths(self):
        for field in ("model", "provider", "source", "api_key", "path", "timeout_seconds", "session"):
            payload = {"request_id": uuid4().hex, "question": "RSI是什么", field: PRIVATE}
            response = self.client.post(self.path + "/messages", headers=self.headers, json=payload)
            self.assertEqual(response.status_code, 422)
            self.assertNotIn(PRIVATE, response.text)
        self.loader.assert_not_called()

    def test_request_id_is_required_and_strict(self):
        for key in ("", "A" * 32, "x" * 32, 123, None):
            response = self.client.post(self.path + "/messages", headers=self.headers,
                                        json={"question": QUESTION, "request_id": key})
            self.assertEqual(response.status_code, 422)
        response = self.client.post(self.path + "/messages", headers=self.headers, json={"question": QUESTION})
        self.assertEqual(response.status_code, 422)

    def test_create_and_reset_do_not_accept_hidden_configuration(self):
        self.assertEqual(self.client.post("/v1/sessions", json={"provider": PRIVATE}).status_code, 422)
        self.assertEqual(self.client.post(self.path + "/reset", headers=self.headers, json={"api_key": PRIVATE}).status_code, 422)

    def test_non_json_and_malformed_json_do_not_echo_request(self):
        response = self.client.post(self.path + "/messages", content=PRIVATE, headers={**self.headers, "Content-Type": "text/plain"})
        self.assertEqual(response.status_code, 415)
        response = self.client.post(self.path + "/messages", content='{"question": "' + PRIVATE,
                                    headers={**self.headers, "Content-Type": "application/json"})
        self.assertEqual(response.status_code, 422)
        self.assertNotIn(PRIVATE, response.text)

    def test_declared_and_streamed_body_size_are_bounded(self):
        huge = json.dumps({"question": PRIVATE * 1000, "request_id": uuid4().hex}).encode()
        self.assertGreater(len(huge), MAX_BODY_BYTES)
        for content, extra in ((huge, {}), ((part for part in (huge[:10000], huge[10000:])), {"Content-Length": "1"})):
            response = self.client.post(self.path + "/messages", content=content,
                headers={**self.headers, "Content-Type": "application/json", **extra})
            self.assertEqual(response.status_code, 413)
            self.assertNotIn(PRIVATE, response.text)
        self.loader.assert_not_called()

    def test_host_and_cross_origin_guards_prevent_browser_triggered_paid_work(self):
        response = self.client.post("/v1/sessions", json={}, headers={"Host": "attacker.invalid"})
        self.assertEqual(response.status_code, 400)
        for origin in ("https://attacker.invalid", "null", "http://localhost.attacker.invalid"):
            response = self.client.post("/v1/sessions", json={}, headers={"Origin": origin})
            self.assertEqual(response.status_code, 403)
        response = self.client.post("/v1/sessions", json={}, headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.factory.call_count, 1)

    def test_same_origin_and_nonbrowser_clients_are_allowed_without_wildcard_cors(self):
        response = self.client.post("/v1/sessions", json={}, headers={"Origin": "http://localhost"})
        self.assertEqual(response.status_code, 201)
        self.assertNotIn("access-control-allow-origin", response.headers)

    def test_complete_research_response_has_only_public_projection(self):
        response = self.send()
        self.assertEqual(response.status_code, 200)
        result = response.json()["result"]
        self.assertEqual(result["code"], "research_completed")
        self.assertIn("10.166667", result["answer"])
        self.assertEqual((result["model_calls_attempted"], result["analyses_used"]), (3, 1))
        for forbidden in ("model_draft", "model_responses", "research_result", str(self.source.path), self.token):
            self.assertNotIn(forbidden, response.text)

    def test_rejected_model_prose_is_not_exposed(self):
        delegate = ConversationDemoModel()
        def bad_explanation(messages, *, tools):
            response = delegate.complete(messages, tools=tools)
            if delegate.calls == 3:
                from trading_companion.tool_model import ToolModelResponse
                return ToolModelResponse("complete", PRIVATE)
            return response
        service = self.build(model_factory=lambda: Mock(complete=Mock(side_effect=bad_explanation)))
        with TestClient(create_app(service=service), base_url="http://localhost") as client:
            credentials = client.post("/v1/sessions", json={}).json()
            response = client.post("/v1/sessions/" + credentials["session_id"] + "/messages",
                headers={"Authorization": "Bearer " + credentials["access_token"]},
                json={"question": QUESTION, "request_id": uuid4().hex})
        self.assertEqual(response.json()["result"]["code"], "explanation_rejected")
        self.assertNotIn(PRIVATE, response.text)

    def test_business_clarification_is_200_not_transport_failure(self):
        from trading_companion.session_agent_demo import build_demo_model
        service = self.build(model_factory=build_demo_model)
        with TestClient(create_app(service=service), base_url="http://localhost") as client:
            credentials = client.post("/v1/sessions", json={}).json()
            response = client.post("/v1/sessions/" + credentials["session_id"] + "/messages",
                headers={"Authorization": "Bearer " + credentials["access_token"]}, json={"request_id": uuid4().hex,
                "question": "看看平安最近一个月的走势，只看SMA3和RSI2"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["result"]["status"], "needs_clarification")
        self.assertEqual(len(response.json()["result"]["candidates"]), 2)

    def test_duplicate_request_replays_and_conflicting_body_is_409(self):
        key = uuid4().hex
        self.send(request_id=key)
        response = self.send(request_id=key)
        self.assertTrue(response.json()["replayed"])
        self.assertEqual(self.service.inspect(self.sid, self.token)["model_calls_forwarded"], 3)
        self.assertEqual(self.send("ATR是什么", request_id=key).status_code, 409)

    def test_followup_and_get_result_are_safe_and_do_not_refetch_market_data(self):
        self.send()
        answer = self.send(FOLLOWUP).json()
        self.assertIn("Wilder", answer["result"]["answer"])
        response = self.client.get(self.path + "/result", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertIn("历史查看", response.json()["result"]["answer"])
        self.assertEqual(response.json()["result"]["model_calls_attempted"], 0)

    def test_reset_delete_and_replay_retirement(self):
        key = uuid4().hex
        self.send(request_id=key)
        response = self.client.post(self.path + "/reset", headers=self.headers, json={})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["session"]["messages_used"], 1)
        self.assertEqual(self.send(request_id=key).status_code, 409)
        self.assertEqual(self.client.get(self.path + "/result", headers=self.headers).json()["result"]["code"], "no_saved_result")
        self.assertEqual(self.client.delete(self.path, headers=self.headers).status_code, 204)
        self.assertEqual(self.client.get(self.path, headers=self.headers).status_code, 404)

    def test_local_directory_failure_is_503_without_details_or_retry(self):
        self.loader.side_effect = DataSourceError(PRIVATE)
        response = self.send()
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(PRIVATE, response.text)

    def test_http_cancel_remains_responsive_during_sync_model_work(self):
        entered, release = Event(), Event()
        delegate = ConversationDemoModel()
        def blocked(messages, *, tools):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("Test release not received")
            return delegate.complete(messages, tools=tools)
        service = self.build(model_factory=lambda: Mock(complete=Mock(side_effect=blocked)))
        with TestClient(create_app(service=service), base_url="http://localhost") as client:
            credentials = client.post("/v1/sessions", json={}).json()
            path = "/v1/sessions/" + credentials["session_id"]
            headers = {"Authorization": "Bearer " + credentials["access_token"]}
            key, results = uuid4().hex, []
            thread = Thread(target=lambda: results.append(client.post(path + "/messages", headers=headers,
                json={"question": QUESTION, "request_id": key})), daemon=True)
            thread.start()
            try:
                self.assertTrue(entered.wait(3))
                self.assertEqual(client.get("/health").status_code, 200)
                self.assertEqual(client.post(path + "/cancel", headers=headers, json={"request_id": uuid4().hex}).status_code, 409)
                self.assertEqual(client.post(path + "/cancel", headers=headers, json={"request_id": key}).status_code, 202)
                self.assertEqual(client.get(path, headers=headers).json()["state"], "busy")
            finally:
                release.set()
                thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(results[0].json()["result"]["code"], "turn_cancelled")
            self.assertIsNone(client.get(path, headers=headers).json()["research"]["saved_result"])


class LauncherTests(unittest.TestCase):
    def test_fixed_inprocess_demo_completes_without_printing_tokens(self):
        from trading_companion.api_demo import main
        with redirect_stdout(StringIO()) as output, patch.dict(os.environ, {}, clear=True), patch(
            "trading_companion.deepseek_model.DeepSeekConfig.from_env", side_effect=AssertionError("No keys")):
            self.assertEqual(main(), 0)
        report = json.loads(output.getvalue())
        self.assertEqual(len(report["checks"]), 8)
        self.assertTrue(all(report["checks"].values()))
        self.assertNotIn("access_token", output.getvalue())

    def test_no_arguments_only_show_help_without_importing_app_or_reading_keys(self):
        with redirect_stdout(StringIO()) as output, patch("trading_companion.deepseek_model.DeepSeekConfig.from_env",
            side_effect=AssertionError("No credentials")), patch("uvicorn.run", side_effect=AssertionError("No server")):
            self.assertEqual(server_main([]), 0)
        self.assertIn("--demo", output.getvalue())

    def test_invalid_combinations_fail_before_server_or_credentials(self):
        for args in (["--provider", "deepseek"], ["--data", "synthetic"], ["--demo", "--data", "synthetic"],
                     ["--demo", "--port", "0"], ["--provider", "deepseek", "--data", "synthetic", "--directory", PRIVATE]):
            with self.subTest(args=args), redirect_stderr(StringIO()), self.assertRaises(SystemExit) as caught:
                server_main(args)
            self.assertEqual(caught.exception.code, 2)

    def test_demo_binds_one_loopback_worker_and_disables_access_log(self):
        with redirect_stdout(StringIO()), patch("uvicorn.run") as run, patch(
                "trading_companion.deepseek_model.DeepSeekConfig.from_env", side_effect=AssertionError("No keys")):
            self.assertEqual(server_main(["--demo", "--port", "8123"]), 0)
        self.assertEqual(run.call_args.kwargs, {"host": "127.0.0.1", "port": 8123, "workers": 1, "access_log": False})

    def test_live_invalid_configuration_is_sanitized_without_starting_server(self):
        from trading_companion.model import ModelConfigurationError
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()) as output, patch("uvicorn.run") as run, patch(
                "trading_companion.deepseek_model.DeepSeekConfig.from_env", side_effect=ModelConfigurationError(PRIVATE)):
            self.assertEqual(server_main(["--provider", "deepseek", "--data", "synthetic"]), 2)
        self.assertNotIn(PRIVATE, output.getvalue())
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
