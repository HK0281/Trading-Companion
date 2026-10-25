"""Provider choice selects an existing server factory, never credentials or tool permissions."""

import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from fastapi.testclient import TestClient

from trading_companion.api import create_app, create_demo_service
from trading_companion.api_service import ResearchApiService
from trading_companion.development_fixtures import REFERENCE_DATE, load_development_fixtures
from trading_companion.followup_demo import ConversationDemoModel
from trading_companion.model import ModelConfigurationError


class ProviderSelectionApiTests(unittest.TestCase):
    def setUp(self):
        source, directory = load_development_fixtures()
        self.deepseek = Mock(side_effect=ConversationDemoModel)
        self.openai = Mock(side_effect=ConversationDemoModel)
        self.service = ResearchApiService(model_factory=self.deepseek,
            model_factories={"deepseek": self.deepseek, "openai": self.openai},
            provider="deepseek", data_mode="synthetic", development_source=source,
            reference_date=REFERENCE_DATE, directory_loader=lambda _: directory)
        self.client = TestClient(create_app(service=self.service, development_service=create_demo_service()),
                                 base_url="http://localhost")
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        for name in ("requests.get", "requests.post"):
            guard = patch(name, side_effect=AssertionError("No external network"))
            guard.start()
            self.addCleanup(guard.stop)

    def test_health_advertises_names_without_constructing_or_calling_models(self):
        response = self.client.get("/health").json()
        self.assertEqual(response["provider"], "deepseek")
        self.assertEqual(response["available_providers"], ["deepseek", "openai"])
        self.deepseek.assert_not_called()
        self.openai.assert_not_called()

    def test_default_and_explicit_provider_are_independent_sessions(self):
        default = self.client.post("/v1/sessions", json={}).json()
        selected = self.client.post("/v1/sessions", json={"provider": "openai"}).json()
        self.assertEqual(default["provider"], "deepseek")
        self.assertEqual(selected["provider"], "openai")
        self.assertNotEqual(default["session_id"], selected["session_id"])
        self.assertNotEqual(default["access_token"], selected["access_token"])
        self.assertEqual(default["data_mode"], selected["data_mode"])
        self.deepseek.assert_called_once_with()
        self.openai.assert_called_once_with()
        self.assertEqual(self.client.get("/health").json()["provider"], "deepseek")

    def test_selected_provider_is_fixed_for_messages_reset_and_history(self):
        auth = self.client.post("/v1/sessions", json={"provider": "openai"}).json()
        path = "/v1/sessions/" + auth["session_id"]
        headers = {"Authorization": "Bearer " + auth["access_token"]}
        self.assertEqual(self.client.get(path, headers=headers).json()["provider"], "openai")
        self.assertEqual(self.client.post(path + "/messages", headers=headers,
            json={"request_id": uuid4().hex, "question": "RSI是什么"}).status_code, 200)
        reset = self.client.post(path + "/reset", headers=headers, json={})
        self.assertEqual(reset.json()["session"]["provider"], "openai")
        self.deepseek.assert_not_called()
        self.openai.assert_called_once_with()

    def test_bad_provider_or_extra_configuration_is_rejected_before_factory(self):
        for body in ({"provider": "unknown"}, {"provider": 1}, {"provider": "fake"},
                     {"provider": "openai", "api_key": "private-marker"},
                     {"provider": "openai", "base_url": "https://invalid.example"}):
            with self.subTest(body=body):
                response = self.client.post("/v1/sessions", json=body)
                self.assertEqual(response.status_code, 422)
                self.assertNotIn("private-marker", response.text)
        self.deepseek.assert_not_called()
        self.openai.assert_not_called()

    def test_unconfigured_provider_does_not_fall_back(self):
        single = create_demo_service()
        with TestClient(create_app(service=single), base_url="http://localhost") as client:
            response = client.post("/v1/sessions", json={"provider": "openai"})
            self.assertEqual((response.status_code, response.json()["error"]["code"]), (422, "provider_unavailable"))
            self.assertEqual(client.get("/health").json()["available_providers"], ["fake"])

    def test_development_never_accepts_a_live_provider(self):
        for provider in ("deepseek", "openai"):
            self.assertEqual(self.client.post("/dev-api/v1/sessions", json={"provider": provider}).status_code, 422)
        self.assertEqual(self.client.post("/dev-api/v1/sessions", json={}).status_code, 201)
        self.deepseek.assert_not_called()
        self.openai.assert_not_called()

    def test_factory_failure_does_not_try_other_models_or_leak_error(self):
        self.openai.side_effect = RuntimeError("private-marker")
        response = self.client.post("/v1/sessions", json={"provider": "openai"})
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private-marker", response.text)
        self.deepseek.assert_not_called()
        self.assertEqual(self.client.post("/v1/sessions", json={}).status_code, 201)

    def test_demo_factories_cannot_be_mixed_with_live_factories(self):
        with self.assertRaises(ModelConfigurationError):
            ResearchApiService(model_factory=self.deepseek, provider="deepseek", data_mode="tencent",
                directory_loader=lambda _: None,
                model_factories={"deepseek": self.deepseek, "fake": self.openai})

    def test_openai_can_be_explicit_server_default(self):
        service = ResearchApiService(model_factory=self.openai, provider="openai", data_mode="tencent",
            directory_loader=lambda _: None, model_factories={"openai": self.openai, "deepseek": self.deepseek})
        self.addCleanup(service.close)
        self.assertEqual(service.create_session()["provider"], "openai")
        self.deepseek.assert_not_called()


if __name__ == "__main__":
    unittest.main()
