import json
import os
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
from unittest.mock import Mock, patch

import requests

from trading_companion.deepseek_model import (
    DEEPSEEK_CHAT_URL,
    DeepSeekConfig,
    DeepSeekTextModel,
    parse_chat_response,
)
from trading_companion.model import (
    ChatMessage,
    FakeTextModel,
    ModelCallError,
    ModelConfigurationError,
    ModelRequestError,
    TokenUsage,
    validate_messages,
)
from trading_companion.model_cli import main


DUMMY_KEY = "synthetic-test-credential-not-a-real-key"
DUMMY_MODEL = "synthetic-test-model"


def response_payload() -> dict:
    return {
        "model": "synthetic-served-model",
        "choices": [{
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": "Synthetic answer."},
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


class ModelLayerTests(unittest.TestCase):
    def setUp(self) -> None:
        local_file = patch("trading_companion.local_env._read_local_values", return_value={})
        local_file.start()
        self.addCleanup(local_file.stop)
        # An accidental network call fails rather than using real credentials.
        env_patch = patch.dict(os.environ, {}, clear=True)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        post_patch = patch("requests.post", side_effect=AssertionError("Unexpected HTTP call"))
        self.post = post_patch.start()
        self.addCleanup(post_patch.stop)
        self.messages = [ChatMessage("user", "Explain a moving average.")]
        self.config = DeepSeekConfig(DUMMY_MODEL, DUMMY_KEY)

    def prepare_response(self, status: int = 200, payload: object = None) -> Mock:
        response = Mock()
        response.status_code = status
        response.text = DUMMY_KEY
        response.json.return_value = response_payload() if payload is None else payload
        self.post.side_effect = None
        self.post.return_value = response
        return response

    def assert_model_error(self, code: str) -> ModelCallError:
        with self.assertRaises(ModelCallError) as caught:
            DeepSeekTextModel(self.config).complete(self.messages)
        self.assertEqual(caught.exception.code, code)
        self.assertNotIn(DUMMY_KEY, str(caught.exception))
        self.post.assert_called_once()
        return caught.exception

    def test_fake_model_is_deterministic_offline_and_explicitly_labelled(self) -> None:
        first = FakeTextModel().complete(self.messages)
        second = FakeTextModel().complete(self.messages)
        self.assertEqual(first, second)
        self.assertTrue(first.is_mock)
        self.assertEqual(first.provider, "fake")
        self.assertIsNone(first.usage)
        self.assertIn("mock", first.text)
        self.assertEqual(json.loads(json.dumps(first.to_dict())), first.to_dict())
        self.post.assert_not_called()

    def test_fake_response_must_be_nonempty_text(self) -> None:
        for text in (None, "", "   ", 123):
            with self.subTest(text=text), self.assertRaises(ModelConfigurationError):
                FakeTextModel(response_text=text).complete(self.messages)
        self.post.assert_not_called()

    def test_from_env_reads_only_explicit_configuration(self) -> None:
        values = {"DEEPSEEK_MODEL": DUMMY_MODEL, "DEEPSEEK_API_KEY": DUMMY_KEY}
        self.assertEqual(DeepSeekConfig.from_env(values), self.config)
        with patch.dict(os.environ, values, clear=True):
            self.assertEqual(DeepSeekConfig.from_env(), self.config)
            # An explicit empty mapping must not fall back to the process environment.
            with self.assertRaises(ModelConfigurationError):
                DeepSeekConfig.from_env({})
        self.post.assert_not_called()

    def test_missing_configuration_fails_without_http(self) -> None:
        for values in ({}, {"DEEPSEEK_MODEL": DUMMY_MODEL}, {"DEEPSEEK_API_KEY": DUMMY_KEY}):
            with self.subTest(values=tuple(values)), self.assertRaises(ModelConfigurationError):
                DeepSeekConfig.from_env(values)
        self.post.assert_not_called()

    def test_configuration_rejects_invalid_values_before_http(self) -> None:
        cases = {
            "model": (None, "", " ", "model name", "model\nname"),
            "api_key": (None, "", " ", "key value", "key\nvalue", "key\x00value", "key\x7fvalue", "密钥"),
            "timeout_seconds": (None, True, 0, -1, 121, 10**400, float("nan"), float("inf"), "30"),
            "max_output_tokens": (None, True, 0, -1, 8193, 1.5, "512"),
        }
        for field, values in cases.items():
            for value in values:
                config = {"model": DUMMY_MODEL, "api_key": DUMMY_KEY, field: value}
                with self.subTest(field=field, value=value), self.assertRaises(ModelConfigurationError):
                    DeepSeekConfig(**config)
        self.post.assert_not_called()

    def test_configuration_and_messages_hide_sensitive_text_in_repr(self) -> None:
        self.assertNotIn(DUMMY_KEY, repr(self.config))
        self.assertNotIn(DUMMY_KEY, repr(DeepSeekTextModel(self.config)))
        self.assertNotIn(DUMMY_KEY, repr(ChatMessage("user", DUMMY_KEY)))
        payload = response_payload()
        payload["choices"][0]["message"]["content"] = DUMMY_KEY
        self.assertNotIn(DUMMY_KEY, repr(parse_chat_response(payload, DUMMY_MODEL)))

    def test_chat_message_rejects_unsupported_role_and_empty_content(self) -> None:
        for role in ("tool", "developer", "USER", "", None):
            with self.subTest(role=role), self.assertRaises(ModelRequestError):
                ChatMessage(role, "text")
        for content in (None, "", " \n ", 1, ["text"]):
            with self.subTest(content=content), self.assertRaises(ModelRequestError):
                ChatMessage("user", content)

    def test_message_validation_rejects_bad_sequences_before_http(self) -> None:
        cases = (
            None, "text", [], [dict(role="user", content="text")],
            [ChatMessage("assistant", "answer")],
            [ChatMessage("system", "instructions")],
            [ChatMessage("user", "text")] * 21,
            [ChatMessage("user", "x" * 2001)],
            [ChatMessage("system", "x" * 20000), ChatMessage("user", "x")],
        )
        for messages in cases:
            for provider in (FakeTextModel(), DeepSeekTextModel(self.config)):
                with self.subTest(provider=type(provider).__name__, messages_type=type(messages).__name__):
                    with self.assertRaises(ModelRequestError):
                        provider.complete(messages)
        self.post.assert_not_called()

    def test_message_limits_allow_exact_boundaries(self) -> None:
        cases = (
            [ChatMessage("user", "x")] * 20,
            [ChatMessage("user", "x" * 2000)],
            [ChatMessage("system", "x" * 18000), ChatMessage("user", "x" * 2000)],
            (ChatMessage("user", "text"),),
        )
        for messages in cases:
            with self.subTest(count=len(messages)):
                self.assertEqual(validate_messages(messages), tuple(messages))

    def test_http_uses_fixed_endpoint_explicit_limits_and_single_call(self) -> None:
        response = self.prepare_response()
        config = DeepSeekConfig(DUMMY_MODEL, DUMMY_KEY, timeout_seconds=7.5, max_output_tokens=128)
        result = DeepSeekTextModel(config).complete(self.messages)
        self.post.assert_called_once_with(
            DEEPSEEK_CHAT_URL,
            headers={"Authorization": f"Bearer {DUMMY_KEY}"},
            json={
                "model": DUMMY_MODEL,
                "messages": [{"role": "user", "content": "Explain a moving average."}],
                "max_tokens": 128,
                "stream": False,
                "thinking": {"type": "disabled"},
            },
            timeout=7.5,
            allow_redirects=False,
        )
        self.assertEqual(DEEPSEEK_CHAT_URL, "https://api.deepseek.com/chat/completions")
        self.assertEqual(result.provider, "deepseek")
        self.assertFalse(result.is_mock)
        self.assertEqual(result.requested_model, DUMMY_MODEL)
        self.assertEqual(result.model, "synthetic-served-model")
        self.assertEqual(result.usage, TokenUsage(10, 5, 15))
        response.close.assert_called_once_with()

    def test_http_failures_are_classified_sanitized_and_not_retried(self) -> None:
        for status, code in ((401, "authentication_error"), (403, "authentication_error"),
                             (429, "rate_limited"), (302, "provider_error"),
                             (400, "provider_error"), (500, "provider_error"), (503, "provider_error")):
            with self.subTest(status=status):
                self.post.reset_mock()
                response = self.prepare_response(status=status)
                self.assert_model_error(code)
                response.json.assert_not_called()
                response.close.assert_called_once_with()

    def test_network_errors_are_sanitized_and_not_retried(self) -> None:
        for exception, code in (
            (requests.exceptions.Timeout(DUMMY_KEY), "timeout"),
            (requests.exceptions.ConnectTimeout(DUMMY_KEY), "timeout"),
            (requests.exceptions.ReadTimeout(DUMMY_KEY), "timeout"),
            (requests.exceptions.ConnectionError(DUMMY_KEY), "connection_error"),
            (requests.exceptions.RequestException(DUMMY_KEY), "connection_error"),
        ):
            with self.subTest(exception=type(exception).__name__):
                self.post.reset_mock()
                self.post.side_effect = exception
                error = self.assert_model_error(code)
                self.assertTrue(error.__suppress_context__)

    def test_invalid_json_is_sanitized_and_response_is_closed(self) -> None:
        response = self.prepare_response()
        response.json.side_effect = requests.exceptions.JSONDecodeError(DUMMY_KEY, "invalid", 0)
        self.assert_model_error("invalid_response")
        response.close.assert_called_once_with()

    def test_response_schema_requires_one_complete_assistant_text(self) -> None:
        invalid_payloads = [None, [], "text", {}, {"choices": []}]
        for path, value in (
            (("choices",), [response_payload()["choices"][0]] * 2),
            (("choices",), [None]),
            (("choices", 0, "message"), None),
            (("choices", 0, "message", "role"), "user"),
            (("choices", 0, "message", "content"), None),
            (("choices", 0, "message", "content"), " "),
            (("choices", 0, "message", "content"), ["answer"]),
            (("model",), None),
            (("model",), ""),
        ):
            payload = response_payload()
            target = payload
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            invalid_payloads.append(payload)
        for payload in invalid_payloads:
            with self.subTest(payload=payload), self.assertRaises(ModelCallError) as caught:
                parse_chat_response(payload, DUMMY_MODEL)
            self.assertEqual(caught.exception.code, "invalid_response")

    def test_finish_reasons_do_not_turn_partial_or_refused_text_into_success(self) -> None:
        for finish_reason, code in (("length", "incomplete_response"), ("content_filter", "refused"),
                                    ("tool_calls", "unsupported_response"), (None, "unsupported_response"),
                                    ("unknown", "unsupported_response")):
            payload = response_payload()
            payload["choices"][0]["finish_reason"] = finish_reason
            with self.subTest(finish_reason=finish_reason), self.assertRaises(ModelCallError) as caught:
                parse_chat_response(payload, DUMMY_MODEL)
            self.assertEqual(caught.exception.code, code)

    def test_tool_calls_and_legacy_function_calls_are_never_executed_or_accepted(self) -> None:
        for field, value in (("tool_calls", [{"id": "call", "function": {"name": "buy_stock"}}]),
                             ("function_call", {"name": "buy_stock"})):
            payload = response_payload()
            payload["choices"][0]["message"][field] = value
            with self.subTest(field=field), self.assertRaises(ModelCallError) as caught:
                parse_chat_response(payload, DUMMY_MODEL)
            self.assertEqual(caught.exception.code, "unsupported_response")
        self.post.assert_not_called()

    def test_refusal_text_is_not_returned_as_a_normal_answer(self) -> None:
        payload = response_payload()
        payload["choices"][0]["message"]["refusal"] = DUMMY_KEY
        with self.assertRaises(ModelCallError) as caught:
            parse_chat_response(payload, DUMMY_MODEL)
        self.assertEqual(caught.exception.code, "refused")
        self.assertNotIn(DUMMY_KEY, str(caught.exception))

    def test_null_optional_fields_and_unknown_usage_counts_are_preserved(self) -> None:
        payload = response_payload()
        payload["choices"][0]["message"].update(tool_calls=None, function_call=None, refusal=None)
        for usage in (None, {}, {"prompt_tokens": 0, "completion_tokens": None}):
            payload["usage"] = usage
            result = parse_chat_response(payload, DUMMY_MODEL)
            if usage is None:
                self.assertIsNone(result.usage)
            else:
                self.assertIsNone(result.usage.completion_tokens)
                self.assertIsNone(result.usage.total_tokens)
            self.assertIsNone(result.to_dict()["usage"] if usage is None else result.to_dict()["usage"]["total_tokens"])
        del payload["usage"]
        self.assertIsNone(parse_chat_response(payload, DUMMY_MODEL).usage)

    def test_usage_rejects_invalid_types_negative_values_and_nonfinite_numbers(self) -> None:
        for usage in ([], "usage", {"prompt_tokens": -1}, {"completion_tokens": True},
                      {"total_tokens": 1.5}, {"total_tokens": "3"}, {"total_tokens": float("nan")}):
            payload = response_payload()
            payload["usage"] = usage
            with self.subTest(usage=usage), self.assertRaises(ModelCallError) as caught:
                parse_chat_response(payload, DUMMY_MODEL)
            self.assertEqual(caught.exception.code, "invalid_response")

    def test_hidden_reasoning_and_provider_extras_are_not_exposed(self) -> None:
        payload = response_payload()
        payload["choices"][0]["message"]["reasoning_content"] = DUMMY_KEY
        payload["internal_debug"] = DUMMY_KEY
        payload["usage"]["completion_tokens_details"] = {"reasoning_tokens": 2}
        original = deepcopy(payload)
        result = parse_chat_response(payload, DUMMY_MODEL)
        self.assertNotIn(DUMMY_KEY, json.dumps(result.to_dict()))
        self.assertEqual(result.text, "Synthetic answer.")
        self.assertEqual(payload, original)

    def test_cli_defaults_to_fake_without_reading_configuration_or_network(self) -> None:
        output = StringIO()
        with patch("trading_companion.model_cli.DeepSeekConfig.from_env", side_effect=AssertionError("Must remain offline")) as config:
            with redirect_stdout(output):
                code = main(["Hello"])
        self.assertEqual(code, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["provider"], "fake")
        self.assertTrue(payload["is_mock"])
        config.assert_not_called()
        self.post.assert_not_called()

    def test_cli_invalid_configuration_and_prompt_are_safe_before_http(self) -> None:
        cases = (
            (["Hello", "--provider", "deepseek"], {}),
            (["Hello", "--provider", "deepseek"], {"DEEPSEEK_API_KEY": DUMMY_KEY, "DEEPSEEK_MODEL": "bad model"}),
            (["x" * 2001], {}),
            ([" "], {}),
        )
        for argv, env in cases:
            output = StringIO()
            with self.subTest(argv_length=len(argv)), patch.dict(os.environ, env, clear=True):
                with redirect_stdout(output):
                    code = main(argv)
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(output.getvalue())["status"], "invalid_request")
            self.assertNotIn(DUMMY_KEY, output.getvalue())
        self.post.assert_not_called()

    def test_cli_provider_failure_never_prints_credentials_or_raw_response(self) -> None:
        env = {"DEEPSEEK_API_KEY": DUMMY_KEY, "DEEPSEEK_MODEL": DUMMY_MODEL}
        for exception in (None, requests.exceptions.ConnectionError(DUMMY_KEY)):
            self.post.reset_mock()
            self.prepare_response(status=500)
            self.post.side_effect = exception
            output = StringIO()
            with self.subTest(exception=type(exception).__name__), patch.dict(os.environ, env, clear=True):
                with redirect_stdout(output):
                    code = main(["Hello", "--provider", "deepseek"])
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(output.getvalue())["status"], "error")
            self.assertNotIn(DUMMY_KEY, output.getvalue())
            self.post.assert_called_once()

    def test_cli_deepseek_success_is_json_without_hidden_provider_fields(self) -> None:
        payload = response_payload()
        payload["choices"][0]["message"]["reasoning_content"] = DUMMY_KEY
        self.prepare_response(payload=payload)
        output = StringIO()
        with patch.dict(os.environ, {"DEEPSEEK_MODEL": DUMMY_MODEL, "DEEPSEEK_API_KEY": DUMMY_KEY}, clear=True):
            with redirect_stdout(output):
                code = main(["Hello", "--provider", "deepseek"])
        self.assertEqual(code, 0)
        parsed = json.loads(output.getvalue())
        self.assertEqual(parsed["status"], "ok")
        self.assertEqual(parsed["text"], "Synthetic answer.")
        self.assertFalse(parsed["is_mock"])
        self.assertNotIn(DUMMY_KEY, output.getvalue())
        self.post.assert_called_once()


if __name__ == "__main__":
    unittest.main()
