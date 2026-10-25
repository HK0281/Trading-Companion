"""Real launcher directory loading separates identity vetoes from research freshness."""

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from scripts.verify import clean_environment, offline_network
from trading_companion import api_server
from tests.test_open_knowledge_api import EducationAndResearchModel, TEXT


class KnowledgeLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "directory.json"
        self.now = datetime.now(timezone.utc)
        self.write_directory(self.now)
        for guard in (
            clean_environment(), offline_network(),
            patch("requests.get", side_effect=AssertionError("No external data HTTP")),
            patch("requests.post", side_effect=AssertionError("No live model HTTP")),
            patch("trading_companion.local_env._read_local_values", side_effect=AssertionError("No .env reads")),
        ):
            guard.__enter__()
            self.addCleanup(guard.__exit__, None, None, None)

    def write_directory(self, timestamp):
        self.path.write_text(json.dumps({
            "source": "synthetic-launcher-test", "retrieved_at": timestamp.isoformat(), "is_fixture": False,
            "securities": [{"instrument_id": "000001.SZ", "name": "平安银行", "aliases": ["平安"]}],
        }, ensure_ascii=False), encoding="utf-8")

    def test_running_server_retains_expired_names_to_veto_education_without_authorizing_research(self):
        model = EducationAndResearchModel()
        observations = []

        def run(app, **options):
            self.assertEqual(options, {"host": "127.0.0.1", "port": 8765, "workers": 1, "access_log": False})
            # Exercise the loader closure installed by main, not a mock returning a stale object.
            self.write_directory(self.now - timedelta(days=8))
            with TestClient(app, base_url="http://localhost") as client:
                credentials = client.post("/v1/sessions", json={}).json()
                path = "/v1/sessions/" + credentials["session_id"] + "/messages"
                headers = {"Authorization": "Bearer " + credentials["access_token"]}
                for question in ("ROE如何判断平安银行估值", "PE低的平安银行值得买吗", "什么是PE，平安银行能买吗"):
                    response = client.post(path, headers=headers, json={"request_id": uuid4().hex, "question": question})
                    self.assertEqual(response.status_code, 200)
                    result = response.json()["result"]
                    self.assertEqual(result["code"], "product_directory_unusable")
                    self.assertEqual(result["model_calls_attempted"], 0)
                    self.assertEqual(model.calls, [])
                    observations.append(result["code"])
                response = client.post(path, headers=headers,
                    json={"request_id": uuid4().hex, "question": "怎么用PE/PB看估值"})
                self.assertEqual(response.status_code, 200)
                result = response.json()["result"]
                self.assertEqual((result["code"], result["stage"], result["answer"]),
                                 ("general_knowledge_answered", "knowledge", TEXT))
                self.assertEqual(len(model.calls), 1)
                observations.append(result["code"])

        with (patch("trading_companion.model_registry.configured_models", return_value={"deepseek": lambda: model}) as models,
              patch("uvicorn.run", side_effect=run) as server,
              redirect_stdout(StringIO()), redirect_stderr(StringIO())):
            status = api_server.main(["--directory", str(self.path)])
        self.assertEqual(status, 0)
        models.assert_called_once()
        server.assert_called_once()
        self.assertEqual(len(observations), 4)

    def test_already_expired_startup_directory_fails_before_model_configuration_or_server_start(self):
        self.write_directory(self.now - timedelta(days=8))
        with (patch("trading_companion.model_registry.configured_models") as models,
              patch("uvicorn.run") as server,
              redirect_stdout(StringIO()), redirect_stderr(StringIO()) as errors):
            status = api_server.main(["--directory", str(self.path)])
        self.assertEqual(status, 2)
        self.assertIn("configuration_error", errors.getvalue())
        models.assert_not_called()
        server.assert_not_called()


if __name__ == "__main__":
    unittest.main()
