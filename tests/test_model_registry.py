from dataclasses import FrozenInstanceError
import importlib.util
import os
import unittest
from unittest.mock import Mock, patch

from trading_companion.deepseek_tool_model import DeepSeekToolModel
from trading_companion.model import ModelConfigurationError
from trading_companion.model_registry import configured_models
from trading_companion.openai_tool_model import OpenAIToolModel


DEEPSEEK_KEY = "synthetic-registry-deepseek-key-not-real"
OPENAI_KEY = "synthetic-registry-openai-key-not-real"
DEEPSEEK = {"DEEPSEEK_MODEL": "synthetic-deepseek-model", "DEEPSEEK_API_KEY": DEEPSEEK_KEY}
OPENAI = {"OPENAI_MODEL": "synthetic-openai-model", "OPENAI_API_KEY": OPENAI_KEY}


class ModelRegistryTests(unittest.TestCase):
    def setUp(self):
        for guard in (
            patch.dict(os.environ, {}, clear=True),
            patch("socket.socket.connect", side_effect=AssertionError("No real network")),
            patch("requests.post", side_effect=AssertionError("Factories must not request a model")),
            patch("trading_companion.local_env._read_local_values", side_effect=AssertionError("No real .env reads")),
        ):
            guard.start()
            self.addCleanup(guard.stop)

    def loader(self, **providers):
        return Mock(side_effect=lambda *, provider: providers.get(provider, {}))

    def test_registry_import_does_not_load_environment_or_create_models(self):
        import trading_companion.model_registry as module
        spec = importlib.util.spec_from_file_location("_registry_import_without_io", module.__file__)
        with patch("trading_companion.local_env.load_model_environment", side_effect=AssertionError("No import config")), \
                patch.object(DeepSeekToolModel, "__post_init__", side_effect=AssertionError("No import models")), \
                patch.object(OpenAIToolModel, "__post_init__", side_effect=AssertionError("No import models")):
            spec.loader.exec_module(importlib.util.module_from_spec(spec))

    def test_default_deepseek_is_required_and_only_it_is_exposed_when_optional_absent(self):
        loader = self.loader(deepseek=DEEPSEEK)
        models = configured_models(loader=loader)
        self.assertEqual(list(models), ["deepseek"])
        self.assertEqual([call.kwargs["provider"] for call in loader.call_args_list], ["deepseek", "openai"])
        model = models["deepseek"]()
        self.assertIs(type(model), DeepSeekToolModel)
        self.assertEqual(model.config.api_key, DEEPSEEK_KEY)
        self.assertEqual(model.config.max_output_tokens, 1500)

    def test_both_valid_providers_expose_fresh_stateless_factories_without_public_credentials(self):
        models = configured_models(loader=self.loader(deepseek=DEEPSEEK, openai=OPENAI))
        self.assertEqual(list(models), ["deepseek", "openai"])
        for provider, model_type, key in (("deepseek", DeepSeekToolModel, DEEPSEEK_KEY),
                                         ("openai", OpenAIToolModel, OPENAI_KEY)):
            with self.subTest(provider=provider):
                first, second = models[provider](), models[provider]()
                self.assertIs(type(first), model_type)
                self.assertIsNot(first, second)
                self.assertEqual(first.config.api_key, key)
                self.assertNotIn(key, repr(first))
                self.assertNotIn(key, repr(models))
                with self.assertRaises(FrozenInstanceError):
                    first.config.api_key = "changed"
        self.assertEqual(models["openai"]().config.max_output_tokens, 2048)

    def test_default_loader_filters_provider_settings_without_mutating_environment(self):
        values = {**DEEPSEEK, **OPENAI, "OPENAI_BASE_URL": "https://untrusted.invalid", "OTHER_SECRET": "not-used"}
        with patch("trading_companion.local_env._read_local_values", return_value=values) as local:
            models = configured_models()
        self.assertEqual(set(models), {"deepseek", "openai"})
        self.assertEqual(models["deepseek"]().config.model, DEEPSEEK["DEEPSEEK_MODEL"])
        self.assertEqual(models["openai"]().config.model, OPENAI["OPENAI_MODEL"])
        self.assertEqual(local.call_count, 2)
        self.assertEqual(dict(os.environ), {})

    def test_default_openai_is_first_and_does_not_require_deepseek(self):
        loader = self.loader(openai=OPENAI)
        models = configured_models(default_provider="openai", loader=loader)
        self.assertEqual(list(models), ["openai"])
        self.assertIs(type(models["openai"]()), OpenAIToolModel)
        self.assertEqual([call.kwargs["provider"] for call in loader.call_args_list], ["openai", "deepseek"])

    def test_missing_or_invalid_default_never_falls_back_to_valid_other_provider(self):
        for default, other_values in (("deepseek", OPENAI), ("openai", DEEPSEEK)):
            other = "openai" if default == "deepseek" else "deepseek"
            for invalid in ({}, {f"{default.upper()}_MODEL": "only-a-model"},
                            {f"{default.upper()}_MODEL": "bad model", f"{default.upper()}_API_KEY": DEEPSEEK_KEY}):
                with self.subTest(default=default, invalid=list(invalid)):
                    loader = self.loader(**{default: invalid, other: other_values})
                    with self.assertRaises(ModelConfigurationError) as caught:
                        configured_models(default_provider=default, loader=loader)
                    self.assertEqual(str(caught.exception), "The default model provider is not configured correctly")
                    self.assertNotIn(DEEPSEEK_KEY, str(caught.exception))
                    self.assertNotIn(OPENAI_KEY, str(caught.exception))
                    loader.assert_called_once_with(provider=default)

    def test_incomplete_invalid_optional_config_is_not_exposed_or_used_as_fallback(self):
        for default, default_values, optional in (("deepseek", DEEPSEEK, "openai"),
                                                  ("openai", OPENAI, "deepseek")):
            prefix = optional.upper()
            for values in ({}, {f"{prefix}_MODEL": "only-model"}, {f"{prefix}_API_KEY": "only-key"},
                           {f"{prefix}_MODEL": "bad model", f"{prefix}_API_KEY": OPENAI_KEY},
                           {f"{prefix}_MODEL": "x" * 129, f"{prefix}_API_KEY": OPENAI_KEY},
                           {f"{prefix}_MODEL": "test", f"{prefix}_API_KEY": OPENAI_KEY + "\x16"}):
                with self.subTest(default=default, fields=list(values)):
                    models = configured_models(default_provider=default,
                        loader=self.loader(**{default: default_values, optional: values}))
                    self.assertEqual(list(models), [default])

    def test_optional_loader_error_is_ignored_but_default_loader_error_is_sanitized(self):
        def optional_error(*, provider):
            if provider == "deepseek":
                return DEEPSEEK
            raise ModelConfigurationError(OPENAI_KEY)
        self.assertEqual(list(configured_models(loader=optional_error)), ["deepseek"])
        with self.assertRaises(ModelConfigurationError) as caught:
            configured_models(loader=Mock(side_effect=ModelConfigurationError(DEEPSEEK_KEY)))
        self.assertNotIn(DEEPSEEK_KEY, str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)

    def test_invalid_provider_or_loader_fails_before_configuration_access(self):
        loader = Mock(side_effect=AssertionError("No settings read"))
        for provider in ("fake", "", "OPENAI", "deepseek:model", None, [], OPENAI_KEY):
            with self.subTest(provider=provider), self.assertRaises(ModelConfigurationError) as caught:
                configured_models(default_provider=provider, loader=loader)
            self.assertNotIn(OPENAI_KEY, str(caught.exception))
        loader.assert_not_called()
        with self.assertRaises(ModelConfigurationError):
            configured_models(loader=None)

    def test_invalid_loader_shape_is_not_advertised_as_a_provider(self):
        for values in (None, [], "not-a-mapping"):
            with self.subTest(values=values), self.assertRaises(ModelConfigurationError):
                configured_models(loader=self.loader(deepseek=values, openai=OPENAI))


if __name__ == "__main__":
    unittest.main()
