"""Startup-only directory refresh uses real local saves and simulated exchange results."""

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from scripts.verify import clean_environment, offline_network
from trading_companion import api_server
from trading_companion.api import create_app
from trading_companion.development_fixtures import FIXTURES, MARKET_CONTEXT_FIXTURE, ROOT
from trading_companion.errors import DataSourceError, DataValidationError
from trading_companion.model import ModelConfigurationError
from trading_companion.security_directory import (
    Security, SecurityDirectory, load_security_directory, save_security_directory,
)


# This UTC date differs from the Shanghai date, exercising the freshness boundary.
NOW = datetime(2026, 10, 28, 16, 30, tzinfo=timezone.utc)
PRIVATE = "artificial-secret-never-display"
REFRESH = ["--refresh-stale-directory"]


def directory(*, age=0, fixture=False):
    return SecurityDirectory((Security("000001.SZ", "平安银行"),),
                             "synthetic-startup-test", NOW - timedelta(days=age), fixture)


class StartupDirectoryTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "data/security_directory_live.json"
        self.path.parent.mkdir()
        # Keep the genuine pinned offline fixtures available to normal/demo startup.
        for name in [*(name for name, _ in FIXTURES), MARKET_CONTEXT_FIXTURE[0]]:
            shutil.copyfile(ROOT / "data" / name, self.path.parent / name)
        for guard in (clean_environment(), offline_network()):
            guard.__enter__()
            self.addCleanup(guard.__exit__, None, None, None)
        self.enter_patch("trading_companion.development_fixtures.ROOT", self.root)
        self.enter_patch("trading_companion.api_server.datetime", wraps=datetime).now.side_effect = (
            lambda zone: NOW.astimezone(zone))
        self.env_read = self.enter_patch("trading_companion.local_env._read_local_values",
                                        side_effect=AssertionError("No real .env reads"))
        self.models = self.enter_patch("trading_companion.model_registry.configured_models",
            return_value={"deepseek": lambda: Mock(), "openai": lambda: Mock()})
        self.server = self.enter_patch("uvicorn.run")
        self.browser = self.enter_patch("trading_companion.api_server._run_browser_server")
        self.source = self.enter_patch("trading_companion.exchange_directory.ExchangeDirectorySource.read_directory",
                                       return_value=directory())

    def enter_patch(self, *args, **kwargs):
        context = patch(*args, **kwargs)
        result = context.start()
        self.addCleanup(context.stop)
        return result

    def invoke(self, args=()):
        output, errors = StringIO(), StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            status = api_server.main(list(args))
        self.assertNotIn(PRIVATE, output.getvalue() + errors.getvalue())
        return status, output.getvalue(), errors.getvalue()

    def assert_stopped(self, errors, code):
        self.assertIn(f"[{code}]", errors)
        self.models.assert_not_called()
        self.env_read.assert_not_called()
        self.server.assert_not_called()
        self.browser.assert_not_called()

    def test_direct_start_is_strict_for_missing_stale_and_future_snapshots(self):
        for age, code in ((None, "directory_missing"), (8, "directory_stale"), (-1, "directory_stale")):
            with self.subTest(age=age):
                if age is not None:
                    save_security_directory(directory(age=age), self.path)
                status, _, errors = self.invoke()
                self.assertEqual(status, 2)
                self.assert_stopped(errors, code)
                self.assertIn("security_directory_refresh --live", errors)
                self.source.assert_not_called()

    def test_missing_stale_and_future_default_snapshots_refresh_before_models(self):
        for age in (None, 8, -1):
            with self.subTest(age=age):
                self.path.unlink(missing_ok=True)
                if age is not None:
                    save_security_directory(directory(age=age), self.path)
                self.source.reset_mock()
                self.server.reset_mock()
                self.models.reset_mock()

                def models_after_refresh(**kwargs):
                    self.source.assert_called_once_with()
                    self.assertEqual(load_security_directory(self.path), directory())
                    self.assertIs(kwargs["loader"], api_server.load_model_environment)
                    return {"deepseek": lambda: Mock()}

                self.models.side_effect = models_after_refresh
                status, output, errors = self.invoke(REFRESH)
                self.assertEqual((status, errors), (0, ""))
                self.assertIn("正在刷新证券名单", output)
                self.assertIn("证券名单刷新成功：1 条，取于 2026-10-29", output)
                if age is not None:
                    self.assertIn(str((NOW - timedelta(days=age)).astimezone(api_server._SHANGHAI).date()), output)
                self.models.assert_called_once()
                self.server.assert_called_once()
                self.assertEqual(load_security_directory(self.path), directory())

    def test_current_and_seven_day_old_snapshots_never_refresh(self):
        for age in (0, 7):
            with self.subTest(age=age):
                save_security_directory(directory(age=age), self.path)
                original = self.path.read_bytes()
                status, output, errors = self.invoke(REFRESH)
                self.assertEqual((status, errors), (0, ""))
                self.assertNotIn("正在刷新", output)
                self.assertEqual(self.path.read_bytes(), original)
                self.source.assert_not_called()

    def test_exchange_failure_preserves_old_snapshot_and_does_not_read_env(self):
        save_security_directory(directory(age=8), self.path)
        original = self.path.read_bytes()
        self.source.side_effect = DataSourceError(PRIVATE)
        status, _, errors = self.invoke([*REFRESH, "--open-browser"])
        self.assertEqual(status, 2)
        self.assert_stopped(errors, "directory_refresh_failed")
        self.assertIn("请稍后重试", errors)
        self.assertIn("security_directory_refresh --live", errors)
        self.assertEqual(self.path.read_bytes(), original)
        self.source.assert_called_once_with()

    def test_failed_first_refresh_does_not_create_a_snapshot(self):
        self.source.side_effect = DataSourceError(PRIVATE)
        status, _, errors = self.invoke(REFRESH)
        self.assertEqual(status, 2)
        self.assert_stopped(errors, "directory_refresh_failed")
        self.assertFalse(self.path.exists())

    def test_atomic_replace_failure_keeps_old_bytes_and_cleans_temporary(self):
        save_security_directory(directory(age=8), self.path)
        original = self.path.read_bytes()
        with patch("trading_companion.security_directory.os.replace", side_effect=OSError(PRIVATE)):
            status, _, errors = self.invoke(REFRESH)
        self.assertEqual(status, 2)
        self.assert_stopped(errors, "directory_refresh_failed")
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.glob(".directory-*")), [])

    def test_unusable_fetched_snapshots_are_rejected_before_replacing_old_file(self):
        for snapshot in (directory(age=8), directory(age=-1), directory(fixture=True)):
            with self.subTest(snapshot=snapshot.metadata()):
                save_security_directory(directory(age=8), self.path)
                original = self.path.read_bytes()
                self.source.return_value = snapshot
                status, _, errors = self.invoke(REFRESH)
                self.assertEqual(status, 2)
                self.assert_stopped(errors, "directory_refresh_failed")
                self.assertEqual(self.path.read_bytes(), original)

    def test_invalid_json_is_reported_without_attempting_refresh(self):
        self.path.write_text(PRIVATE, encoding="utf-8")
        status, _, errors = self.invoke(REFRESH)
        self.assertEqual(status, 2)
        self.assert_stopped(errors, "directory_invalid")
        self.assertEqual(self.path.read_text(encoding="utf-8"), PRIVATE)
        self.source.assert_not_called()

    def test_fixture_is_invalid_even_when_its_date_is_also_stale(self):
        save_security_directory(directory(age=8, fixture=True), self.path)
        status, _, errors = self.invoke(REFRESH)
        self.assertEqual(status, 2)
        self.assert_stopped(errors, "directory_invalid")
        self.source.assert_not_called()

    def test_read_failure_on_an_existing_file_is_not_treated_as_missing(self):
        save_security_directory(directory(age=8), self.path)
        with patch("trading_companion.security_directory.load_security_directory", side_effect=DataSourceError(PRIVATE)):
            status, _, errors = self.invoke(REFRESH)
        self.assertEqual(status, 2)
        self.assert_stopped(errors, "directory_unavailable")
        self.source.assert_not_called()

    def test_refresh_return_value_cannot_replace_readback_and_freshness_check(self):
        save_security_directory(directory(age=8), self.path)
        original = self.path.read_bytes()
        with patch("trading_companion.security_directory_refresh.refresh_security_directory",
                   return_value=directory()) as refresh:
            status, _, errors = self.invoke(REFRESH)
        self.assertEqual(status, 2)
        self.assert_stopped(errors, "directory_refresh_failed")
        refresh.assert_called_once_with(self.path, reference_date=NOW.astimezone(api_server._SHANGHAI).date())
        self.assertEqual(self.path.read_bytes(), original)

    def test_demo_and_synthetic_do_not_refresh_a_missing_default_snapshot(self):
        for flags in (["--demo"], ["--data", "synthetic"]):
            with self.subTest(flags=flags):
                status, output, errors = self.invoke([*REFRESH, *flags])
                self.assertEqual((status, errors), (0, ""))
                self.assertNotIn("正在刷新", output)
                self.assertFalse(self.path.exists())
                self.source.assert_not_called()
        self.env_read.assert_not_called()

    def test_explicit_directory_including_default_path_never_refreshes(self):
        for path in (self.path.parent / "custom.json", self.path):
            with self.subTest(path=path):
                save_security_directory(directory(age=8), path)
                original = path.read_bytes()
                status, _, errors = self.invoke([*REFRESH, "--directory", str(path)])
                self.assertEqual(status, 2)
                self.assert_stopped(errors, "directory_stale")
                self.assertEqual(path.read_bytes(), original)
                self.source.assert_not_called()

    def test_explicit_valid_directory_still_starts_without_refreshing(self):
        custom = self.path.parent / "custom.json"
        save_security_directory(directory(), custom)
        status, _, errors = self.invoke([*REFRESH, "--directory", str(custom)])
        self.assertEqual((status, errors), (0, ""))
        self.source.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_empty_explicit_directory_does_not_enable_default_refresh(self):
        status, _, errors = self.invoke([*REFRESH, "--directory", ""])
        self.assertEqual(status, 2)
        self.assert_stopped(errors, "directory_unavailable")
        self.source.assert_not_called()

    def test_model_configuration_errors_name_fields_but_never_values(self):
        save_security_directory(directory(), self.path)
        self.models.side_effect = ModelConfigurationError(PRIVATE)
        for provider, prefix in (("deepseek", "DEEPSEEK"), ("openai", "OPENAI")):
            with self.subTest(provider=provider):
                status, _, errors = self.invoke(["--provider", provider])
                self.assertEqual(status, 2)
                self.assertIn("[model_configuration_error]", errors)
                self.assertIn(prefix + "_API_KEY", errors)
                self.assertIn(prefix + "_MODEL", errors)
                self.assertNotIn("[configuration_error]", errors)
        self.server.assert_not_called()
        self.source.assert_not_called()

    def test_unusable_snapshot_store_has_its_own_error(self):
        save_security_directory(directory(), self.path)
        archive = self.root / "invalid.sqlite"
        archive.write_bytes(PRIVATE.encode("ascii"))
        status, _, errors = self.invoke(["--snapshot-db", str(archive)])
        self.assertEqual(status, 2)
        self.assertIn("[snapshot_store_unavailable]", errors)
        self.assertIn("--snapshot-db", errors)
        self.assertNotIn("[model_configuration_error]", errors)
        self.server.assert_not_called()

    def test_other_validation_errors_keep_a_sanitized_fallback_and_close_resources(self):
        save_security_directory(directory(), self.path)
        with patch("trading_companion.snapshot_store.ResearchSnapshotStore") as store, \
                patch("trading_companion.api_service.ResearchApiService") as service, \
                patch("trading_companion.api.create_demo_service", side_effect=DataValidationError(PRIVATE)):
            status, _, errors = self.invoke(["--snapshot-db", str(self.root / "archive.sqlite")])
        self.assertEqual(status, 2)
        self.assertIn("[configuration_error]", errors)
        service.return_value.close.assert_called_once_with()
        store.return_value.close.assert_called_once_with()
        self.server.assert_not_called()

    def test_help_does_not_refresh_or_configure_models(self):
        for flag in ("--help", "-h"):
            with self.subTest(flag=flag), redirect_stdout(StringIO()) as output, \
                    self.assertRaises(SystemExit) as caught:
                api_server.main([*REFRESH, flag])
            self.assertEqual(caught.exception.code, 0)
            self.assertIn("--refresh-stale-directory", output.getvalue())
        self.models.assert_not_called()
        self.env_read.assert_not_called()
        self.source.assert_not_called()

    def test_runtime_loader_does_not_refresh_after_startup_snapshot_expires(self):
        save_security_directory(directory(age=8), self.path)
        with patch("trading_companion.api.create_app", wraps=create_app) as app:
            def expire_during_run(*args, **kwargs):
                loader = app.call_args.kwargs["service"]._directory_loader
                save_security_directory(directory(age=8), self.path)
                expired = loader(NOW)
                with self.assertRaises(DataValidationError):
                    expired.assert_usable(NOW.astimezone(api_server._SHANGHAI).date(), development=False)
                self.source.assert_called_once_with()
            self.server.side_effect = expire_during_run
            status, _, errors = self.invoke(REFRESH)
        self.assertEqual((status, errors), (0, ""))
        self.assertEqual(load_security_directory(self.path), directory(age=8))


if __name__ == "__main__":
    unittest.main()
