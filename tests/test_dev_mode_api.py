"""Development routing uses independent artificial services, never live providers or data."""

from contextlib import ExitStack
import json
import os
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from fastapi.testclient import TestClient

from trading_companion.api import MAX_BODY_BYTES, create_app, create_demo_service
from trading_companion.api_service import ApiProblem, ResearchApiService
from trading_companion.development_fixtures import REFERENCE_DATE, load_development_fixtures
from trading_companion.followup_demo import QUESTION


PRIVATE = "dev-routing-private-marker-never-export"


class DevelopmentModeApiTests(unittest.TestCase):
    def setUp(self):
        for guard in (
            patch.dict(os.environ, {}, clear=True),
            patch("requests.get", side_effect=AssertionError("No market network")),
            patch("requests.post", side_effect=AssertionError("No model network")),
            patch("trading_companion.deepseek_model.DeepSeekConfig.from_env",
                  side_effect=AssertionError("No credentials")),
        ):
            guard.start()
            self.addCleanup(guard.stop)
        self.normal_model = Mock()
        self.normal_model.complete.side_effect = AssertionError("Development requests must not call the normal model")
        self.normal_factory = Mock(return_value=self.normal_model)
        self.normal_directory = Mock(side_effect=AssertionError("Development must not load the normal directory"))
        self.normal = ResearchApiService(model_factory=self.normal_factory, directory_loader=self.normal_directory,
                                         provider="deepseek", data_mode="tencent")
        self.development = create_demo_service()
        self.client = TestClient(create_app(service=self.normal, development_service=self.development),
                                 base_url="http://localhost")
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.credentials = self.client.post("/dev-api/v1/sessions", json={}).json()
        self.sid, self.token = self.credentials["session_id"], self.credentials["access_token"]
        self.path = "/dev-api/v1/sessions/" + self.sid
        self.headers = {"Authorization": "Bearer " + self.token}

    def send(self, *, request_id=None, **extra):
        return self.client.post(self.path + "/messages", headers=self.headers,
                                json={"request_id": request_id or uuid4().hex, "question": QUESTION, **extra})

    def assert_normal_untouched(self):
        self.normal_factory.assert_not_called()
        self.normal_model.complete.assert_not_called()
        self.normal_directory.assert_not_called()

    def test_health_advertises_offline_mode_without_switching_the_normal_service(self):
        root = self.client.get("/health").json()
        development = self.client.get("/dev-api/health").json()
        self.assertEqual((root["provider"], root["data_mode"]), ("deepseek", "tencent"))
        self.assertTrue(root["development_mode_available"])
        self.assertEqual((development["provider"], development["data_mode"]), ("fake", "synthetic"))
        self.assertFalse(development["development_mode_available"])
        self.assertFalse(root["trading_enabled"])
        self.assertFalse(development["trading_enabled"])
        self.assertEqual(self.client.get("/health").json(), root)
        self.assert_normal_untouched()

    def test_development_research_is_artificial_and_cannot_call_normal_dependencies(self):
        self.assertEqual((self.credentials["provider"], self.credentials["data_mode"]), ("fake", "synthetic"))
        response = self.send()
        self.assertEqual(response.status_code, 200)
        turn = response.json()["result"]
        self.assertEqual(turn["code"], "company_research_completed")
        self.assertEqual(turn["chart"]["data_authenticity"], "synthetic")
        self.assertEqual(self.client.get("/health").json()["data_mode"], "tencent")
        self.assert_normal_untouched()

    def test_all_session_lifecycle_routes_are_available_inside_the_prefix(self):
        self.assertEqual(self.client.get(self.path, headers=self.headers).status_code, 200)
        key = uuid4().hex
        self.assertEqual(self.send(request_id=key).status_code, 200)
        self.assertTrue(self.send(request_id=key).json()["replayed"])
        saved = self.client.get(self.path + "/result", headers=self.headers)
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.json()["result"]["code"], "product_saved_result")
        idle = self.client.post(self.path + "/cancel", headers=self.headers, json={"request_id": uuid4().hex})
        self.assertEqual(idle.status_code, 409)
        reset = self.client.post(self.path + "/reset", headers=self.headers, json={})
        self.assertEqual(reset.status_code, 200)
        self.assertEqual(self.send(request_id=key).status_code, 409)
        self.assertEqual(self.client.delete(self.path, headers=self.headers).status_code, 204)
        self.assertEqual(self.client.get(self.path, headers=self.headers).status_code, 404)
        self.assert_normal_untouched()

    def test_tokens_and_session_ids_are_not_interchangeable_between_services(self):
        normal = self.client.post("/v1/sessions", json={}).json()
        normal_path = "/v1/sessions/" + normal["session_id"]
        normal_headers = {"Authorization": "Bearer " + normal["access_token"]}
        pairs = ((self.path, normal_headers), (normal_path, self.headers),
                 ("/v1/sessions/" + self.sid, self.headers),
                 ("/dev-api/v1/sessions/" + normal["session_id"], normal_headers))
        for path, headers in pairs:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path, headers=headers).status_code, 404)
                self.assertEqual(self.client.post(path + "/messages", headers=headers,
                    json={"request_id": uuid4().hex, "question": QUESTION}).status_code, 404)
                self.assertEqual(self.client.post(path + "/cancel", headers=headers,
                    json={"request_id": uuid4().hex}).status_code, 404)
                self.assertEqual(self.client.post(path + "/reset", headers=headers, json={}).status_code, 404)
                self.assertEqual(self.client.delete(path, headers=headers).status_code, 404)
        self.assertEqual(self.client.get(normal_path, headers=normal_headers).status_code, 200)
        self.assertEqual(self.client.get(self.path, headers=self.headers).status_code, 200)
        self.normal_model.complete.assert_not_called()
        self.normal_directory.assert_not_called()

    def test_development_body_cannot_override_sources_models_credentials_or_paths(self):
        for field in ("source", "data_mode", "model", "provider", "api_key", "path", "directory", "development_mode"):
            with self.subTest(field=field):
                response = self.send(**{field: PRIVATE})
                self.assertEqual(response.status_code, 422)
                self.assertNotIn(PRIVATE, response.text)
                created = self.client.post("/dev-api/v1/sessions", json={field: PRIVATE})
                self.assertEqual(created.status_code, 422)
                self.assertNotIn(PRIVATE, created.text)
        self.assertEqual(self.client.get(self.path, headers=self.headers).json()["messages_used"], 0)
        self.assert_normal_untouched()

    def test_development_keeps_host_origin_and_json_boundaries(self):
        for headers, status in (({"Host": "attacker.invalid"}, 400),
                                ({"Origin": "https://attacker.invalid"}, 403),
                                ({"Origin": "null"}, 403), ({"Sec-Fetch-Site": "cross-site"}, 403)):
            with self.subTest(headers=headers):
                response = self.client.post("/dev-api/v1/sessions", json={}, headers=headers)
                self.assertEqual(response.status_code, status)
        allowed = self.client.post("/dev-api/v1/sessions", json={}, headers={"Origin": "http://localhost"})
        self.assertEqual(allowed.status_code, 201)
        self.assertNotIn("access-control-allow-origin", allowed.headers)
        bad = self.client.post(self.path + "/messages", content=PRIVATE,
                               headers={**self.headers, "Content-Type": "text/plain"})
        self.assertEqual(bad.status_code, 415)
        self.assert_normal_untouched()

    def test_development_keeps_bearer_authentication(self):
        self.assertEqual(self.client.get(self.path).status_code, 401)
        for token in (PRIVATE, "x" * 129):
            response = self.client.get(self.path, headers={"Authorization": "Bearer " + token})
            self.assertIn(response.status_code, (401, 404))
            self.assertNotIn(token, response.text)
        self.assert_normal_untouched()

    def test_development_bounds_declared_and_streamed_uploads_without_echoing_them(self):
        huge = json.dumps({"request_id": uuid4().hex, "question": PRIVATE * 1000}).encode()
        self.assertGreater(len(huge), MAX_BODY_BYTES)
        variants = ((huge, {}), ((part for part in (huge[:10000], huge[10000:])), {"Content-Length": "1"}))
        for body, extra in variants:
            response = self.client.post(self.path + "/messages", content=body,
                headers={**self.headers, "Content-Type": "application/json", **extra})
            self.assertEqual(response.status_code, 413)
            self.assertNotIn(PRIVATE, response.text)
        self.assert_normal_untouched()

    def test_development_responses_disable_caching_and_hide_credentials(self):
        response = self.client.get("/dev-api/health")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        schema = self.client.get("/dev-api/openapi.json")
        self.assertEqual(schema.status_code, 200)
        self.assertNotIn(self.token, schema.text)
        self.assert_normal_untouched()

    def test_development_prefix_is_not_enabled_without_a_trusted_development_service(self):
        service = create_demo_service()
        with TestClient(create_app(service=service), base_url="http://localhost") as client:
            self.assertFalse(client.get("/health").json()["development_mode_available"])
            self.assertEqual(client.get("/dev-api/health").status_code, 404)
            self.assertEqual(client.post("/dev-api/v1/sessions", json={}).status_code, 404)

    def test_only_a_separate_fake_synthetic_service_can_be_mounted(self):
        source, directory = load_development_fixtures()
        paid_synthetic = ResearchApiService(model_factory=Mock(), directory_loader=lambda _: directory,
            provider="deepseek", data_mode="synthetic", development_source=source, reference_date=REFERENCE_DATE)
        fake_live = ResearchApiService(model_factory=Mock(), directory_loader=Mock(), provider="fake", data_mode="tencent")
        self.addCleanup(paid_synthetic.close)
        self.addCleanup(fake_live.close)
        for invalid in (self.normal, paid_synthetic, fake_live, Mock(spec=ResearchApiService)):
            with self.subTest(service_type=type(invalid).__name__), self.assertRaises(ValueError):
                create_app(service=self.normal, development_service=invalid)
        with self.assertRaises(ValueError):
            create_app(service=self.development, development_service=self.development)

    def test_parent_shutdown_closes_both_services_even_without_child_lifespan(self):
        normal = ResearchApiService(model_factory=Mock(), directory_loader=Mock(), provider="deepseek", data_mode="tencent")
        development = create_demo_service()
        with ExitStack() as stack:
            normal_close = stack.enter_context(patch.object(normal, "close", wraps=normal.close))
            development_close = stack.enter_context(patch.object(development, "close", wraps=development.close))
            with TestClient(create_app(service=normal, development_service=development), base_url="http://localhost") as client:
                self.assertEqual(client.get("/dev-api/health").status_code, 200)
            normal_close.assert_called_once()
            development_close.assert_called_once()
        for service in (normal, development):
            with self.assertRaises(ApiProblem) as caught:
                service.create_session()
            self.assertEqual((caught.exception.status, caught.exception.code), (503, "service_closed"))


if __name__ == "__main__":
    unittest.main()
