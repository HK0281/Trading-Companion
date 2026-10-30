"""Explicit startup-only provider configuration; factories and credentials stay on the server."""

from dataclasses import replace
from collections.abc import Callable, Mapping
from functools import partial

from trading_companion.deepseek_model import ANSWER_OUTPUT_TOKENS, DeepSeekConfig
from trading_companion.deepseek_tool_model import DeepSeekToolModel
from trading_companion.local_env import load_model_environment
from trading_companion.model import ModelConfigurationError
from trading_companion.openai_tool_model import OpenAIConfig, OpenAIToolModel
from trading_companion.tool_model import ToolCapableModel


_PROVIDERS = ("deepseek", "openai")


def _factory(provider, values):
    if not isinstance(values, Mapping):
        raise ModelConfigurationError("Model configuration must use the documented settings mapping")
    if provider == "deepseek":
        config = replace(DeepSeekConfig.from_env(values), max_output_tokens=ANSWER_OUTPUT_TOKENS)
        factory = partial(DeepSeekToolModel, config)
    else:
        config = OpenAIConfig.from_env(values)
        factory = partial(OpenAIToolModel, config)
    # Constructors are pure. Validate adapter metadata before advertising the provider.
    factory()
    return factory


def configured_models(
    *, default_provider: str = "deepseek", loader=load_model_environment,
) -> dict[str, Callable[[], ToolCapableModel]]:
    """Return fresh model factories without ever substituting a different provider.

    The selected default must be configured correctly. Other providers are
    exposed only when their own explicit settings validate. Factories share
    immutable server configuration, not history or execution permissions.
    This function performs no provider requests; importing the module does
    not load environment values or the local .env file.
    """
    if type(default_provider) is not str or default_provider not in _PROVIDERS:
        raise ModelConfigurationError("Choose DeepSeek or OpenAI as the default model provider")
    if not callable(loader):
        raise ModelConfigurationError("Use a trusted model configuration loader")
    providers = (default_provider, *(provider for provider in _PROVIDERS if provider != default_provider))
    models = {}
    for provider in providers:
        try:
            values = loader(provider=provider)
            models[provider] = _factory(provider, values)
        except ModelConfigurationError:
            if provider == default_provider:
                raise ModelConfigurationError("The default model provider is not configured correctly") from None
            # A missing or invalid optional provider is unavailable, never a fallback.
    return models
