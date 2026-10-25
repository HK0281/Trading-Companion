"""Local config tests use disposable files and artificial credentials only."""

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from trading_companion.deepseek_model import DeepSeekConfig
from trading_companion import local_env
from trading_companion.model import ModelConfigurationError
from trading_companion.openai_tool_model import OpenAIConfig


ARTIFICIAL_KEY = "synthetic-local-env-key-not-a-credential"
ARTIFICIAL_MODEL = "synthetic-local-env-model"
FILE_CONTENT = f"DEEPSEEK_API_KEY={ARTIFICIAL_KEY}\nDEEPSEEK_MODEL={ARTIFICIAL_MODEL}\n"


class LocalEnvironmentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="trading-companion-env-")
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / ".env"
        for guard in (
            patch.dict(os.environ, {}, clear=True),
            patch.object(local_env, "LOCAL_ENV_FILE", self.path),
            patch("requests.post", side_effect=AssertionError("No real model calls")),
            patch("requests.get", side_effect=AssertionError("No real data requests")),
        ):
            guard.start()
            self.addCleanup(guard.stop)

    def write_file(self, content=FILE_CONTENT, *, encoding="utf-8"):
        self.path.write_text(content, encoding=encoding)

    def test_reads_local_values_without_mutating_process_environment(self):
        self.write_file()
        values = local_env.load_model_environment()
        self.assertEqual(values, {"DEEPSEEK_MODEL": ARTIFICIAL_MODEL, "DEEPSEEK_API_KEY": ARTIFICIAL_KEY})
        self.assertEqual(dict(os.environ), {})
        config = DeepSeekConfig.from_env(values)
        self.assertEqual(config.api_key, ARTIFICIAL_KEY)
        self.assertNotIn(ARTIFICIAL_KEY, repr(config))

    def test_environment_overrides_one_file_value(self):
        self.write_file()
        os.environ["DEEPSEEK_MODEL"] = "terminal-model"
        self.assertEqual(local_env.load_model_environment(), {
            "DEEPSEEK_MODEL": "terminal-model", "DEEPSEEK_API_KEY": ARTIFICIAL_KEY,
        })

    def test_complete_environment_does_not_open_local_file(self):
        os.environ.update(DEEPSEEK_MODEL="terminal-model", DEEPSEEK_API_KEY="terminal-key")
        with patch.object(local_env, "_read_local_values", side_effect=AssertionError("Do not open .env")):
            self.assertEqual(local_env.load_model_environment(), {
                "DEEPSEEK_MODEL": "terminal-model", "DEEPSEEK_API_KEY": "terminal-key",
            })

    def test_empty_environment_value_never_falls_back_to_file(self):
        self.write_file()
        for name in local_env.MODEL_VARIABLES:
            with self.subTest(name=name), patch.dict(os.environ, {name: ""}, clear=True):
                values = local_env.load_model_environment()
                self.assertEqual(values[name], "")
                with self.assertRaises(ModelConfigurationError):
                    DeepSeekConfig.from_env(values)

    def test_missing_file_leaves_missing_configuration_for_existing_validation(self):
        self.assertEqual(local_env.load_model_environment(), {})
        with self.assertRaises(ModelConfigurationError):
            DeepSeekConfig.from_env(local_env.load_model_environment())

    def test_partial_environment_is_preserved_when_file_is_missing(self):
        os.environ["DEEPSEEK_MODEL"] = ARTIFICIAL_MODEL
        self.assertEqual(local_env.load_model_environment(), {"DEEPSEEK_MODEL": ARTIFICIAL_MODEL})

    def test_ignores_unrelated_file_and_process_settings(self):
        self.write_file(FILE_CONTENT + "HTTPS_PROXY=http://must-not-be-loaded.invalid\nPYTHONPATH=untrusted\n")
        os.environ["CUSTOM_SECRET"] = "must-not-be-returned"
        values = local_env.load_model_environment()
        self.assertEqual(set(values), set(local_env.MODEL_VARIABLES))
        self.assertNotIn("HTTPS_PROXY", os.environ)
        self.assertNotIn("PYTHONPATH", os.environ)

    def test_provider_selection_reads_only_its_own_documented_settings(self):
        self.write_file(FILE_CONTENT + 'OPENAI_MODEL="synthetic-openai-model"\nOPENAI_API_KEY="synthetic-openai-key"\n'
                        'OPENAI_BASE_URL=https://untrusted.invalid\nCUSTOM_SECRET=not-returned\n')
        openai = local_env.load_model_environment(provider="openai")
        self.assertEqual(openai, {"OPENAI_MODEL": "synthetic-openai-model", "OPENAI_API_KEY": "synthetic-openai-key"})
        self.assertEqual(local_env.load_model_environment(),
                         {"DEEPSEEK_MODEL": ARTIFICIAL_MODEL, "DEEPSEEK_API_KEY": ARTIFICIAL_KEY})
        self.assertEqual(OpenAIConfig.from_env(openai).model, "synthetic-openai-model")
        self.assertEqual(dict(os.environ), {})

    def test_openai_environment_overrides_file_including_empty_values(self):
        self.write_file("OPENAI_MODEL=file-model\nOPENAI_API_KEY=file-key\n")
        os.environ["OPENAI_MODEL"] = "terminal-model"
        self.assertEqual(local_env.load_model_environment(provider="openai"),
                         {"OPENAI_MODEL": "terminal-model", "OPENAI_API_KEY": "file-key"})
        os.environ["OPENAI_API_KEY"] = ""
        with patch.object(local_env, "_read_local_values", side_effect=AssertionError("Complete env must not read file")):
            values = local_env.load_model_environment(provider="openai")
        self.assertEqual(values["OPENAI_API_KEY"], "")
        with self.assertRaises(ModelConfigurationError):
            OpenAIConfig.from_env(values)

    def test_openai_does_not_interpolate_other_variables_or_load_a_custom_endpoint(self):
        self.write_file('OPENAI_MODEL=test-model\nOPENAI_API_KEY=${OTHER_SECRET}\nOPENAI_BASE_URL=https://untrusted.invalid\n')
        os.environ["OTHER_SECRET"] = ARTIFICIAL_KEY
        values = local_env.load_model_environment(provider="openai")
        self.assertEqual(values, {"OPENAI_MODEL": "test-model", "OPENAI_API_KEY": "${OTHER_SECRET}"})
        self.assertNotIn("OPENAI_BASE_URL", os.environ)

    def test_unknown_provider_is_rejected_before_file_or_environment_reads(self):
        for provider in ("fake", "", "OPENAI", None, [], ARTIFICIAL_KEY):
            with self.subTest(provider=provider), patch.object(local_env, "_read_local_values",
                    side_effect=AssertionError("Unknown provider must not read file")), \
                    self.assertRaises(ModelConfigurationError) as caught:
                local_env.load_model_environment(provider=provider)
            self.assertNotIn(ARTIFICIAL_KEY, str(caught.exception))

    def test_openai_honors_existing_dotenv_opt_out(self):
        with patch.dict(os.environ, {"PYTHON_DOTENV_DISABLED": "true"}), patch.object(
                local_env, "_read_local_values", side_effect=AssertionError("No opted-out file read")):
            self.assertEqual(local_env.load_model_environment(provider="openai"), {})

    def test_supports_quotes_comments_and_export(self):
        self.write_file(f'# Local test\nexport DEEPSEEK_MODEL="{ARTIFICIAL_MODEL}"\n'
                        f"DEEPSEEK_API_KEY='{ARTIFICIAL_KEY}' # Artificial key\n")
        self.assertEqual(local_env.load_model_environment()["DEEPSEEK_API_KEY"], ARTIFICIAL_KEY)
        self.assertEqual(local_env.load_model_environment()["DEEPSEEK_MODEL"], ARTIFICIAL_MODEL)

    def test_supports_utf8_bom(self):
        self.write_file(encoding="utf-8-sig")
        self.assertEqual(local_env.load_model_environment()["DEEPSEEK_API_KEY"], ARTIFICIAL_KEY)

    def test_does_not_expand_other_environment_variables(self):
        os.environ["OTHER_SECRET"] = ARTIFICIAL_KEY
        self.write_file("DEEPSEEK_MODEL=test-model\nDEEPSEEK_API_KEY=${OTHER_SECRET}\n")
        self.assertEqual(local_env.load_model_environment()["DEEPSEEK_API_KEY"], "${OTHER_SECRET}")

    def test_unassigned_or_empty_key_still_fails_validation(self):
        for line in ("DEEPSEEK_API_KEY", "DEEPSEEK_API_KEY="):
            with self.subTest(line=line):
                self.write_file(f"DEEPSEEK_MODEL={ARTIFICIAL_MODEL}\n{line}\n")
                with self.assertRaises(ModelConfigurationError):
                    DeepSeekConfig.from_env(local_env.load_model_environment())

    def test_invalid_key_is_not_in_validation_error(self):
        self.write_file(f'DEEPSEEK_MODEL={ARTIFICIAL_MODEL}\nDEEPSEEK_API_KEY="{ARTIFICIAL_KEY} bad"\n')
        with self.assertRaises(ModelConfigurationError) as caught:
            DeepSeekConfig.from_env(local_env.load_model_environment())
        self.assertNotIn(ARTIFICIAL_KEY, str(caught.exception))

    def test_read_errors_never_include_path_or_raw_exception(self):
        for error in (PermissionError(ARTIFICIAL_KEY), UnicodeError(ARTIFICIAL_KEY)):
            with self.subTest(error=type(error).__name__), patch.object(Path, "open", side_effect=error):
                with self.assertRaises(ModelConfigurationError) as caught:
                    local_env.load_model_environment()
                self.assertNotIn(ARTIFICIAL_KEY, str(caught.exception))
                self.assertNotIn(str(self.path), str(caught.exception))

    def test_missing_dotenv_dependency_has_safe_install_hint(self):
        self.write_file()
        with patch.dict(sys.modules, {"dotenv": None}):
            with self.assertRaises(ModelConfigurationError) as caught:
                local_env.load_model_environment()
        self.assertIn(".[model]", str(caught.exception))
        self.assertNotIn(ARTIFICIAL_KEY, str(caught.exception))

    def test_explicit_opt_out_prevents_file_reading(self):
        for value in ("1", "true", "TRUE", "yes", "on"):
            with self.subTest(value=value), patch.dict(os.environ, {"PYTHON_DOTENV_DISABLED": value}), patch.object(
                    local_env, "_read_local_values", side_effect=AssertionError("Do not read .env")):
                self.assertEqual(local_env.load_model_environment(), {})

    def test_config_from_env_remains_file_free(self):
        with patch.object(local_env, "_read_local_values", side_effect=AssertionError("Pure config only")):
            self.assertEqual(DeepSeekConfig.from_env({
                "DEEPSEEK_MODEL": ARTIFICIAL_MODEL, "DEEPSEEK_API_KEY": ARTIFICIAL_KEY,
            }).model, ARTIFICIAL_MODEL)

    def test_reloads_file_between_startup_attempts(self):
        self.write_file()
        self.assertEqual(local_env.load_model_environment()["DEEPSEEK_MODEL"], ARTIFICIAL_MODEL)
        self.write_file(FILE_CONTENT.replace(ARTIFICIAL_MODEL, "changed-model"))
        self.assertEqual(local_env.load_model_environment()["DEEPSEEK_MODEL"], "changed-model")

    def test_uses_fixed_project_file_not_working_directory_or_parent_search(self):
        self.write_file()
        with patch.object(Path, "cwd", side_effect=AssertionError("No current-directory search")):
            self.assertEqual(local_env.load_model_environment()["DEEPSEEK_API_KEY"], ARTIFICIAL_KEY)

    def test_source_layout_requires_checkout_marker(self):
        package = self.path.parent / "src" / "trading_companion"
        package.mkdir(parents=True)
        module = package / "local_env.py"
        self.assertIsNone(local_env._source_env_file(module))
        (self.path.parent / "pyproject.toml").write_text("# Artificial checkout marker\n", encoding="utf-8")
        # Match the resolver's canonical path, including Windows short-name expansion.
        self.assertEqual(local_env._source_env_file(module), self.path.resolve())

    def test_source_layout_resolves_parent_directory_segments(self):
        package = self.path.parent / "src" / "trading_companion"
        package.mkdir(parents=True)
        (self.path.parent / "pyproject.toml").write_text("# Artificial checkout marker\n", encoding="utf-8")
        module = package / ".." / "trading_companion" / "local_env.py"
        self.assertEqual(local_env._source_env_file(module), self.path.resolve())

    def test_wheel_layout_never_uses_virtual_environment_ancestors(self):
        module = self.path.parent / ".venv" / "Lib" / "site-packages" / "trading_companion" / "local_env.py"
        self.assertIsNone(local_env._source_env_file(module))
        with patch.object(local_env, "LOCAL_ENV_FILE", None), patch.object(
                Path, "open", side_effect=AssertionError("Wheel installs must not open local files")):
            self.assertEqual(local_env.load_model_environment(), {})

    def test_local_file_can_complete_text_entry_with_mocked_http_only(self):
        from trading_companion.model_cli import main
        self.write_file()
        response = Mock(status_code=200)
        response.json.return_value = {
            "model": ARTIFICIAL_MODEL,
            "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "OK"}}],
        }
        with patch("requests.post", return_value=response) as post, redirect_stdout(StringIO()) as output:
            self.assertEqual(main(["Reply with OK", "--provider", "deepseek"]), 0)
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer " + ARTIFICIAL_KEY)
        self.assertEqual(post.call_args.kwargs["json"]["model"], ARTIFICIAL_MODEL)
        self.assertNotIn(ARTIFICIAL_KEY, output.getvalue())
        self.assertNotIn("DEEPSEEK_API_KEY", os.environ)

    def test_offline_entries_never_load_local_environment(self):
        from trading_companion import agent_cli, agent_evaluation, api_server, model_cli, mvp_evaluation, tool_smoke
        entries = (
            lambda: model_cli.main(["Hello"]), lambda: tool_smoke.run_smoke_test(),
            lambda: agent_evaluation.run_evaluation(), lambda: mvp_evaluation.run_mvp_evaluation(case="single_sma"),
            lambda: agent_cli.main([]), lambda: agent_cli.main(["--demo"]),
            lambda: api_server.main(["--demo"]),
        )
        with patch.object(local_env, "_read_local_values", side_effect=AssertionError("Offline must not read .env")), \
                patch("uvicorn.run"), redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            for entry in entries:
                with self.subTest(entry=entries.index(entry)):
                    result = entry()
                    self.assertTrue(result == 0 or isinstance(result, dict) and result["status"] in ("ok", "passed"))

        with patch.object(local_env, "_read_local_values", side_effect=AssertionError("Help must not read .env")), \
                patch("uvicorn.run", side_effect=AssertionError("Help must not start a server")), \
                redirect_stdout(StringIO()), self.assertRaises(SystemExit) as caught:
            api_server.main(["--help"])
        self.assertEqual(caught.exception.code, 0)

    def test_all_six_live_entries_pass_merged_values_to_validation_before_network(self):
        from trading_companion import agent_cli, agent_evaluation, api_server, model_cli, mvp_evaluation, tool_smoke
        self.write_file()
        entries = (
            lambda: model_cli.main(["Hello", "--provider", "deepseek"]),
            lambda: tool_smoke.run_smoke_test(provider="deepseek"),
            lambda: agent_evaluation.run_evaluation(provider="deepseek"),
            lambda: mvp_evaluation.run_mvp_evaluation(provider="deepseek", case="single_sma"),
            lambda: agent_cli.main(["--provider", "deepseek", "--data", "synthetic"]),
            lambda: api_server.main(["--provider", "deepseek", "--data", "synthetic"]),
        )
        for index, entry in enumerate(entries):
            with self.subTest(entry=index), redirect_stdout(StringIO()), redirect_stderr(StringIO()), patch.object(
                    DeepSeekConfig, "from_env", side_effect=ModelConfigurationError("Configuration probe")) as validate:
                entry()
                validate.assert_called_once_with({"DEEPSEEK_MODEL": ARTIFICIAL_MODEL, "DEEPSEEK_API_KEY": ARTIFICIAL_KEY})

    def test_default_server_loads_merged_local_configuration_without_demo_fallback(self):
        from trading_companion import api_server
        self.write_file()
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()), patch(
                "trading_companion.security_directory.load_security_directory",
                return_value=Mock(assert_usable=Mock())), patch.object(
                DeepSeekConfig, "from_env", side_effect=ModelConfigurationError("Configuration probe")) as validate, patch(
                "uvicorn.run", side_effect=AssertionError("Invalid configuration must not start a server")), patch(
                "trading_companion.api.create_demo_service", side_effect=AssertionError("No demo fallback")):
            self.assertEqual(api_server.main([]), 2)
        validate.assert_called_once_with({"DEEPSEEK_MODEL": ARTIFICIAL_MODEL, "DEEPSEEK_API_KEY": ARTIFICIAL_KEY})

    def test_invalid_local_directory_stops_before_reading_credentials(self):
        from trading_companion import agent_cli, api_server
        from trading_companion.errors import DataSourceError
        for module in (agent_cli, api_server):
            target = ("trading_companion.agent_cli.load_security_directory" if module is agent_cli
                      else "trading_companion.security_directory.load_security_directory")
            with self.subTest(module=module.__name__), patch(target, side_effect=DataSourceError("Invalid directory")), \
                    patch.object(local_env, "_read_local_values", side_effect=AssertionError("No credentials")), \
                    redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                self.assertIn(module.main(["--provider", "deepseek", "--data", "tencent"]), (1, 2))

    def test_missing_data_dependency_stops_before_reading_credentials(self):
        from trading_companion import agent_cli, api_server
        from trading_companion.api_service import DataModeDependencyError
        for module in (agent_cli, api_server):
            with self.subTest(module=module.__name__), patch.object(
                    module, "ensure_data_mode_dependencies", side_effect=DataModeDependencyError()), patch.object(
                    local_env, "_read_local_values", side_effect=AssertionError("No credentials")), \
                    redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                self.assertEqual(module.main(["--provider", "deepseek", "--data", "akshare_eastmoney"]), 2)


if __name__ == "__main__":
    unittest.main()
