"""Local UI routing and packaging boundaries; no model or market network."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
except ImportError:
    raise unittest.SkipTest("Install .[api,dev] to test optional web routes")

from trading_companion.api import create_app
from trading_companion.web_ui import add_web_routes, _asset


class WebRoutesTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        # Temporary test assets are fixtures, not project sources.
        (self.root / "assets").mkdir()
        (self.root / "index.html").write_text('<!doctype html><html lang="zh-CN">test workspace</html>', encoding="utf-8")
        (self.root / "assets/app-test.js").write_text('"use strict";', encoding="utf-8")
        (self.root / "assets/index-test.css").write_text('body { color: black; }', encoding="utf-8")
        app = FastAPI()
        add_web_routes(app, asset_root=self.root)
        self.client = TestClient(app, base_url="http://localhost")

    def test_root_serves_only_compiled_index(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
        self.assertIn("test workspace", response.text)

    def test_web_policy_disallows_external_scripts_frames_inline_code_and_api_hosts(self):
        response = self.client.get("/")
        policy = response.headers["content-security-policy"]
        for rule in ("default-src 'none'", "script-src 'self'", "connect-src 'self'", "frame-ancestors 'none'", "base-uri 'none'"):
            self.assertIn(rule, policy)
        self.assertNotIn("unsafe-inline", policy)
        self.assertNotIn("unsafe-eval", policy)
        self.assertEqual(response.headers["x-frame-options"], "DENY")

    def test_script_and_style_have_explicit_mime_types(self):
        for name, expected in (("app-test.js", "text/javascript"), ("index-test.css", "text/css")):
            response = self.client.get("/assets/" + name)
            self.assertEqual(response.status_code, 200)
            self.assertIn(expected, response.headers["content-type"])

    def test_missing_build_has_actionable_503_but_no_server_paths(self):
        with patch("trading_companion.web_ui._asset", return_value=None):
            response = self.client.get("/")
        self.assertEqual(response.status_code, 503)
        self.assertIn("尚未构建", response.text)
        self.assertIn("frontend build", response.text)
        self.assertNotIn(str(self.root), response.text)

    def test_missing_asset_does_not_fall_back_to_html(self):
        response = self.client.get("/assets/app-missing.js")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "asset_not_found")

    def test_does_not_expose_source_maps_environment_or_python_files(self):
        for path in ("/assets/app-test.js.map", "/assets/.env", "/assets/api.py", "/src/App.tsx", "/frontend/package.json"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)

    def test_asset_resolver_rejects_escape_and_unresolvable_paths(self):
        self.assertIsNone(_asset(self.root, "../index.html"))
        self.assertIsNone(_asset(self.root, "assets"))
        self.assertIsNone(_asset(self.root, "missing"))

    def test_resolver_rejects_resolved_symlink_escape_without_requiring_windows_symlink_privileges(self):
        outside = self.root.parent / "outside.js"
        with patch.object(Path, "resolve", side_effect=[outside, self.root]):
            self.assertIsNone(_asset(self.root, "assets/app-test.js"))

    def test_html_routes_do_not_replace_api_404_or_require_credentials(self):
        with TestClient(create_app(), base_url="http://localhost") as client:
            self.assertIn(client.get("/").status_code, (200, 503))
            self.assertEqual(client.get("/v1/no-such-api").status_code, 404)
            self.assertEqual(client.get("/health").status_code, 200)

    def test_existing_host_origin_and_cache_boundaries_cover_web_routes(self):
        with TestClient(create_app(), base_url="http://localhost") as client:
            self.assertEqual(client.get("/", headers={"Host": "untrusted.test"}).status_code, 400)
            self.assertEqual(client.get("/", headers={"Origin": "https://untrusted.test"}).status_code, 403)
            response = client.get("/")
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(response.headers["x-content-type-options"], "nosniff")

    def test_web_routes_are_not_advertised_as_research_api_operations(self):
        with TestClient(create_app(), base_url="http://localhost") as client:
            paths = client.get("/openapi.json").json()["paths"]
            self.assertNotIn("/", paths)
            self.assertNotIn("/assets/{filename}", paths)
            self.assertIn("/v1/sessions", paths)


if __name__ == "__main__":
    unittest.main()
