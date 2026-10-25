from collections.abc import Mapping
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import requests

from trading_companion.deepseek_model import DeepSeekConfig
from trading_companion.data_source import CsvDailyBarSource
from trading_companion.model import (
    ChatMessage, ModelCallError, ModelConfigurationError, ModelRequestError, TokenUsage,
)
from trading_companion.openai_tool_model import OPENAI_CHAT_URL, OpenAIConfig, OpenAIToolModel
from trading_companion.research_tool import RESEARCH_TOOL_DEFINITION, ResearchToolExecutor
from trading_companion.tool_model import ToolCall, ToolDefinition, ToolMessage, ToolModelResponse
from trading_companion.turn_control import (
    ControlledToolModel, TurnControl, TurnLimits, TurnStopped, bind_turn,
)


KEY = "synthetic-openai-test-credential-not-a-real-key"
MODEL = "synthetic-openai-tool-model"
_UNSET = object()


def provider_call(*, identifier="call_1", name="inspect_fixture", arguments='{"window":3}'):
    return {"id": identifier, "type": "function", "function": {"name": name, "arguments": arguments}}


def payload(*, finish="stop", content="Synthetic answer.", calls=None):
    message = {"role": "assistant", "content": content}
    if calls is not None:
        message["tool_calls"] = calls
    return {"model": "synthetic-served-openai-model", "choices": [{"message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}


class _RecordedMapping(Mapping):
    def __init__(self, values):
        self.values, self.requested = values, []

    def __getitem__(self, key):
        self.requested.append(key)
        return self.values[key]

    def __iter__(self):
        raise AssertionError("Configuration must not enumerate unrelated variables")

    def __len__(self):
        return len(self.values)


class OpenAIToolModelTests(unittest.TestCase):
    def setUp(self):
        for guard in (
            patch.dict(os.environ, {}, clear=True),
            patch("trading_companion.local_env._read_local_values", side_effect=AssertionError("No .env reads")),
            patch("socket.socket.connect", side_effect=AssertionError("No real network")),
            patch("trading_companion.deepseek_model._post_chat", side_effect=AssertionError("No DeepSeek transport")),
        ):
            guard.start()
            self.addCleanup(guard.stop)
        post = patch("requests.post", side_effect=AssertionError("Unexpected HTTP request"))
        self.post = post.start()
        self.addCleanup(post.stop)
        self.config = OpenAIConfig(MODEL, KEY)
        self.model = OpenAIToolModel(self.config)
        self.messages = [ToolMessage("system", "Only use declared fixture evidence."),
                         ToolMessage("user", "Inspect the synthetic fixture.")]
        self.tool = ToolDefinition("inspect_fixture", "Inspect synthetic data.",
            {"type": "object", "properties": {"window": {"type": "integer"}},
             "required": ["window"], "additionalProperties": False})
        self.tools = (self.tool,)

    def prepare(self, value=_UNSET, *, status=200):
        response = Mock()
        response.status_code = status
        response.text = KEY
        response.json.return_value = payload() if value is _UNSET else value
        self.post.side_effect = None
        self.post.return_value = response
        return response

    def complete(self):
        return self.model.complete(self.messages, tools=self.tools)

    def assert_bad(self, value, *, code="invalid_response"):
        self.post.reset_mock()
        response = self.prepare(value)
        with self.assertRaises(ModelCallError) as caught:
            self.complete()
        self.assertEqual(caught.exception.code, code)
        self.assertNotIn(KEY, str(caught.exception))
        self.post.assert_called_once()
        response.close.assert_called_once_with()

    def test_explicit_config_and_import_do_not_read_environment_or_contact_provider(self):
        import trading_companion.openai_tool_model as module
        spec = importlib.util.spec_from_file_location("_openai_import_without_io", module.__file__)
        loaded = importlib.util.module_from_spec(spec)
        forbidden_env = Mock(spec=Mapping)
        forbidden_env.get.side_effect = AssertionError("Import and explicit construction must not read env")
        with patch("os.environ", forbidden_env), patch.object(OpenAIConfig, "from_env", side_effect=AssertionError("No env read")):
            spec.loader.exec_module(loaded)
            model = OpenAIToolModel(OpenAIConfig(MODEL, KEY))
        self.assertNotIn(KEY, repr(model))
        self.post.assert_not_called()

    def test_from_env_reads_only_openai_model_and_credential_without_a_model_default(self):
        values = _RecordedMapping({"OPENAI_MODEL": MODEL, "OPENAI_API_KEY": KEY,
            "DEEPSEEK_API_KEY": "unused", "OPENAI_BASE_URL": "https://untrusted.invalid"})
        config = OpenAIConfig.from_env(values)
        self.assertEqual((config.model, config.api_key), (MODEL, KEY))
        self.assertEqual(values.requested, ["OPENAI_MODEL", "OPENAI_API_KEY"])
        for values in ({}, {"OPENAI_API_KEY": KEY}, {"OPENAI_MODEL": MODEL},
                       {"DEEPSEEK_MODEL": MODEL, "DEEPSEEK_API_KEY": KEY}):
            with self.subTest(values=list(values)), self.assertRaises(ModelConfigurationError):
                OpenAIConfig.from_env(values)
        with patch.dict(os.environ, {"OPENAI_MODEL": MODEL, "OPENAI_API_KEY": KEY}):
            self.assertEqual(OpenAIConfig.from_env(), config)
        self.post.assert_not_called()

    def test_config_validates_model_credential_timeout_and_output_boundaries(self):
        cases = {
            "model": (None, "", " ", "a b", "a\n", "a\x00", "a" * 129, "模型"),
            "api_key": (None, "", " ", "a b", "a\x00", "a\x16", "a\n", "密钥"),
            "timeout_seconds": (True, False, None, "5", 0, -1, 30.01, float("nan"), float("inf")),
            "max_output_tokens": (True, False, None, "128", 0, -1, 4097, 1.5, float("nan")),
        }
        for field, values in cases.items():
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaises(ModelConfigurationError) as caught:
                    OpenAIConfig(**{"model": MODEL, "api_key": KEY, field: value})
                self.assertNotIn(KEY, str(caught.exception))
        OpenAIConfig("a" * 128, KEY, timeout_seconds=0.1, max_output_tokens=1)
        OpenAIConfig(MODEL, KEY, timeout_seconds=30, max_output_tokens=4096)
        for config in (None, {}, DeepSeekConfig(MODEL, KEY)):
            with self.subTest(config_type=type(config).__name__), self.assertRaises(ModelConfigurationError):
                OpenAIToolModel(config)
        self.post.assert_not_called()

    def test_fixed_endpoint_developer_role_function_schema_and_openai_request_fields(self):
        response = self.prepare()
        model = OpenAIToolModel(OpenAIConfig(MODEL, KEY, timeout_seconds=7.5, max_output_tokens=128))
        with patch.dict(os.environ, {"OPENAI_BASE_URL": "https://untrusted.invalid", "DEEPSEEK_API_KEY": "unused"}):
            result = model.complete(self.messages, tools=self.tools)
        self.post.assert_called_once_with(OPENAI_CHAT_URL,
            headers={"Authorization": f"Bearer {KEY}"},
            json={"model": MODEL, "messages": [
                {"role": "developer", "content": "Only use declared fixture evidence."},
                {"role": "user", "content": "Inspect the synthetic fixture."}],
                "max_completion_tokens": 128, "store": False, "stream": False,
                "tools": [{"type": "function", "function": self.tool.to_dict()}], "tool_choice": "auto"},
            timeout=7.5, allow_redirects=False)
        self.assertEqual((result.provider, result.requested_model, result.model, result.is_mock),
                         ("openai", MODEL, "synthetic-served-openai-model", False))
        self.assertEqual(result.usage, TokenUsage(10, 5, 15))
        self.assertEqual(result.text, "Synthetic answer.")
        response.close.assert_called_once_with()
        body = self.post.call_args.kwargs["json"]
        for forbidden in ("temperature", "thinking", "max_tokens", "api_key", "base_url"):
            self.assertNotIn(forbidden, body)
        self.assertNotIn(KEY, json.dumps(body))
        self.assertEqual(self.messages[0].role, "system")

    def test_tool_calls_are_bounded_proposals_without_tool_execution(self):
        self.prepare(payload(finish="tool_calls", content=None, calls=[provider_call()]))
        with patch("trading_companion.research_tool.ResearchToolExecutor.execute", side_effect=AssertionError("No execution")):
            result = self.complete()
        self.assertEqual((result.status, result.provider), ("tool_calls", "openai"))
        self.assertEqual(result.tool_calls[0].arguments, {"window": 3})
        self.assertEqual(result.tool_calls[0].call_id, "call_1")
        self.assertIsNone(result.text)

    def test_tool_history_preserves_ids_arguments_results_and_does_not_mutate_input(self):
        call = ToolCall("call_1", "inspect_fixture", {"window": 3})
        result = {"value": 0, "warning": "人工样例", "missing": None}
        history = [*self.messages, ToolMessage("assistant", tool_calls=(call,)),
            ToolMessage("tool", call_id="call_1", name="inspect_fixture", result=result)]
        original = [item.to_dict() for item in history]
        self.prepare()
        answer = self.model.complete(history, tools=self.tools)
        wire = self.post.call_args.kwargs["json"]["messages"]
        self.assertEqual(wire[2], {"role": "assistant", "content": None, "tool_calls": [provider_call()]})
        self.assertEqual(wire[3]["role"], "tool")
        self.assertEqual(wire[3]["tool_call_id"], "call_1")
        self.assertEqual(json.loads(wire[3]["content"]), result)
        self.assertNotIn("name", wire[3])
        self.assertEqual(answer.status, "complete")
        self.assertEqual([item.to_dict() for item in history], original)

    def test_text_only_calls_omit_tool_fields_and_refuse_undeclared_tools(self):
        self.prepare()
        self.assertTrue(self.model.complete(self.messages, tools=()).is_complete)
        body = self.post.call_args.kwargs["json"]
        self.assertNotIn("tools", body)
        self.assertNotIn("tool_choice", body)
        self.prepare(payload(finish="tool_calls", calls=[provider_call()]))
        with self.assertRaises(ModelCallError) as caught:
            self.model.complete(self.messages, tools=())
        self.assertEqual(caught.exception.code, "unknown_tool")

    def test_invalid_history_or_declarations_are_rejected_before_http(self):
        pending = ToolMessage("assistant", tool_calls=(ToolCall("call_1", "inspect_fixture", {}),))
        for messages, tools in (([], self.tools), ([ChatMessage("user", "wrong protocol")], self.tools),
                ([*self.messages, pending], self.tools), (self.messages, (self.tool, self.tool)),
                (self.messages, [self.tool.to_dict()])):
            with self.subTest(messages=len(messages)), self.assertRaises(ModelRequestError):
                self.model.complete(messages, tools=tools)
        self.post.assert_not_called()

    def test_response_choices_metadata_roles_and_content_are_strict(self):
        values = [None, [], "body", {}, {"choices": []}, {"choices": [None]}, {"choices": [{}, {}]}]
        for field, value in (("model", None), ("model", "bad model"), ("model", "x" * 129)):
            values.append({**payload(), field: value})
        for value in ([], {}, "message", None):
            candidate = payload()
            candidate["choices"][0]["message"] = value
            values.append(candidate)
        for role in ("user", "tool", None):
            candidate = payload()
            candidate["choices"][0]["message"]["role"] = role
            values.append(candidate)
        for content in (None, "", " \n", [], {}, 1, "x" * 20001):
            values.append(payload(content=content))
        for value in values:
            with self.subTest(shape=type(value).__name__):
                self.assert_bad(value)

    def test_finish_reasons_and_legacy_functions_do_not_smuggle_tool_actions(self):
        for finish in (None, "unknown", "function_call", "error"):
            self.assert_bad(payload(finish=finish), code="unsupported_response")
        candidate = payload()
        candidate["choices"][0]["message"]["function_call"] = {"name": "inspect_fixture", "arguments": "{}"}
        self.assert_bad(candidate, code="unsupported_response")
        self.assert_bad(payload(finish="stop", calls=[provider_call()]))
        self.assert_bad(payload(finish="tool_calls", calls=[]))

    def test_refusal_and_incomplete_results_never_expose_actionable_calls(self):
        for finish in ("length", "aborted", "insufficient_system_resource"):
            for calls in ([provider_call()], [{"function": {"arguments": KEY}}]):
                self.prepare(payload(finish=finish, content="Partial output.", calls=calls))
                result = self.complete()
                self.assertEqual((result.status, result.provider), ("incomplete", "openai"))
                self.assertEqual(result.tool_calls, ())
        for finish, refusal in (("content_filter", None), ("stop", KEY)):
            candidate = payload(finish=finish, calls=[provider_call()])
            candidate["choices"][0]["message"]["refusal"] = refusal
            self.prepare(candidate)
            result = self.complete()
            self.assertEqual((result.status, result.provider), ("refused", "openai"))
            self.assertEqual(result.tool_calls, ())
            self.assertNotIn(KEY, json.dumps(result.to_dict()))

    def test_tool_call_shapes_ids_and_collection_limits_are_checked(self):
        for calls in ("calls", {}, [None], [{}], [dict(provider_call(), type="code")],
                [dict(provider_call(), function=None)], [provider_call(identifier="bad id")],
                [provider_call(identifier="x" * 65)], [provider_call(), provider_call()],
                [provider_call(identifier=f"call_{i}") for i in range(9)]):
            self.assert_bad(payload(finish="tool_calls", calls=calls))
        self.prepare(payload(finish="tool_calls", calls=[provider_call(identifier=f"call_{i}") for i in range(8)]))
        self.assertEqual(len(self.complete().tool_calls), 8)

    def test_arguments_must_be_bounded_strict_json_objects(self):
        for arguments in (None, {}, [], 3, "", "[]", "null", '"text"', '{"x":NaN}', '{"x":Infinity}',
                '{"x":1e999}', '{"window":3,"window":4}', '{"secret":"' + KEY,
                '{"x":"' + "a" * 10000 + '"}', '{"x":' + "[" * 13 + "0" + "]" * 13 + "}"):
            self.assert_bad(payload(finish="tool_calls", calls=[provider_call(arguments=arguments)]))

    def test_unknown_tools_and_reused_history_identifiers_are_rejected(self):
        self.assert_bad(payload(finish="tool_calls", calls=[provider_call(name="undeclared")]), code="unknown_tool")
        history = [*self.messages, ToolMessage("assistant", tool_calls=(ToolCall("call_1", "inspect_fixture", {}),)),
            ToolMessage("tool", call_id="call_1", name="inspect_fixture", result={"value": 7})]
        self.prepare(payload(finish="tool_calls", calls=[provider_call()]))
        with self.assertRaises(ModelCallError) as caught:
            self.model.complete(history, tools=self.tools)
        self.assertEqual(caught.exception.code, "invalid_response")

    def test_usage_unknown_zero_and_invalid_values_follow_shared_contract(self):
        for usage in (None, {}, {"prompt_tokens": 0}, {"total_tokens": None}):
            candidate = payload()
            candidate["usage"] = usage
            self.prepare(candidate)
            result = self.complete()
            self.assertEqual(result.usage, None if usage is None else TokenUsage(**usage))
        for usage in ([], "usage", {"prompt_tokens": -1}, {"completion_tokens": True},
                      {"total_tokens": 1.5}, {"total_tokens": float("nan")}):
            candidate = payload()
            candidate["usage"] = usage
            self.assert_bad(candidate)

    def test_reasoning_and_provider_extras_are_not_exposed_or_replayed(self):
        candidate = payload(finish="tool_calls", calls=[provider_call()])
        candidate["choices"][0]["message"]["reasoning_content"] = KEY
        candidate["internal_debug"] = KEY
        candidate["usage"]["completion_tokens_details"] = {"reasoning_tokens": 3}
        original = deepcopy(candidate)
        self.prepare(candidate)
        proposal = self.complete()
        self.assertNotIn(KEY, repr(proposal))
        self.assertNotIn(KEY, json.dumps(proposal.to_dict()))
        self.assertEqual(candidate, original)
        self.prepare()
        self.model.complete([*self.messages, proposal.as_message(),
            ToolMessage("tool", call_id="call_1", name="inspect_fixture", result={"value": 7})], tools=self.tools)
        self.assertNotIn(KEY, json.dumps(self.post.call_args.kwargs["json"]))

    def test_http_errors_are_closed_sanitized_and_never_retried(self):
        for status, code in ((401, "authentication_error"), (403, "authentication_error"), (429, "rate_limited"),
                (302, "provider_error"), (400, "provider_error"), (500, "provider_error"), (KEY, "provider_error")):
            self.post.reset_mock()
            response = self.prepare({"error": KEY}, status=status)
            with self.assertRaises(ModelCallError) as caught:
                self.complete()
            self.assertEqual(caught.exception.code, code)
            self.assertNotIn(KEY, str(caught.exception))
            self.post.assert_called_once()
            response.json.assert_not_called()
            response.close.assert_called_once_with()

    def test_network_and_invalid_json_failures_do_not_leak_body_or_credentials(self):
        for error, code in ((requests.exceptions.Timeout(KEY), "timeout"),
                            (requests.exceptions.ConnectionError(KEY), "connection_error")):
            self.post.reset_mock()
            self.post.side_effect = error
            with self.assertRaises(ModelCallError) as caught:
                self.complete()
            self.assertEqual(caught.exception.code, code)
            self.assertNotIn(KEY, str(caught.exception))
            self.assertTrue(caught.exception.__suppress_context__)
            self.post.assert_called_once()
        for error in (requests.exceptions.JSONDecodeError(KEY, "invalid", 0), ValueError(KEY), RecursionError(KEY)):
            response = self.prepare()
            response.json.side_effect = error
            with self.assertRaises(ModelCallError) as caught:
                self.complete()
            self.assertEqual(caught.exception.code, "invalid_response")
            self.assertNotIn(KEY, str(caught.exception))
            response.close.assert_called_once_with()

    def test_missing_optional_dependency_fails_without_network(self):
        with patch.dict("sys.modules", {"requests": None}), self.assertRaises(ModelConfigurationError):
            self.complete()
        self.post.assert_not_called()

    def test_request_timeout_is_clamped_to_remaining_turn_budget(self):
        clock = [0.0]
        control = TurnControl(TurnLimits(timeout_seconds=10), clock=lambda: clock[0])
        control.start()
        clock[0] = 8.0
        self.prepare()
        with bind_turn(control):
            self.complete()
        self.assertEqual(self.post.call_args.kwargs["timeout"], 2)

    def test_preexisting_cancellation_stops_before_http(self):
        control = TurnControl()
        control.start()
        control.cancel()
        with bind_turn(control), self.assertRaises(TurnStopped) as caught:
            self.complete()
        self.assertEqual(caught.exception.code, "turn_cancelled")
        self.post.assert_not_called()

    def test_controlled_model_meters_then_discards_late_or_cancelled_responses(self):
        for mode in ("late", "cancel"):
            with self.subTest(mode=mode):
                clock = [0.0]
                control = TurnControl(TurnLimits(timeout_seconds=10), clock=lambda: clock[0])
                control.start()
                response = self.prepare()
                def returned(*args, **kwargs):
                    if mode == "late":
                        clock[0] = 11.0
                    else:
                        control.cancel()
                    return response
                self.post.side_effect = returned
                model = ControlledToolModel(self.model, control)
                with bind_turn(control), self.assertRaises(TurnStopped) as caught:
                    model.complete(self.messages, tools=self.tools)
                self.assertEqual(caught.exception.code, "turn_deadline_exceeded" if mode == "late" else "turn_cancelled")
                self.assertEqual(control.to_dict()["observed_tokens"], 15)
                self.assertEqual(control.to_dict()["responses_received"], 1)
                response.close.assert_called_once_with()

    def test_mocked_openai_round_trip_uses_real_synthetic_research_executor(self):
        arguments = {"instrument_id": "000001.SZ", "start_date": "2026-09-21", "as_of_date": "2026-09-25",
            "indicators": [{"kind": "sma", "window": 3}, {"kind": "sma", "window": 20},
                           {"kind": "volume", "window": 3}]}
        first = self.prepare(payload(finish="tool_calls", content=None, calls=[
            provider_call(name="analyze_daily_indicators", arguments=json.dumps(arguments))]))
        second = Mock()
        second.status_code = 200
        second.json.return_value = payload()
        self.post.side_effect = [first, second]
        source = CsvDailyBarSource(Path(__file__).resolve().parents[1] / "data" /
                                   "sample_daily_bars_synthetic.csv", is_synthetic=True)
        executor = ResearchToolExecutor(development_source=source)
        proposal = self.model.complete(self.messages, tools=(RESEARCH_TOOL_DEFINITION,))
        execution = executor.execute(proposal, messages=self.messages)
        final = self.model.complete([*self.messages, proposal.as_message(), *execution.messages],
                                    tools=(RESEARCH_TOOL_DEFINITION,))
        self.assertEqual((proposal.provider, final.provider), ("openai", "openai"))
        self.assertEqual(execution.status, "executed")
        self.assertEqual(execution.full_result.data_authenticity, "synthetic")
        self.assertEqual(execution.full_result.calculation_status, "limited")
        self.assertEqual(executor.analyses_used, 1)
        self.assertTrue(final.is_complete)
        self.assertEqual(self.post.call_count, 2)
        wire_result = self.post.call_args.kwargs["json"]["messages"][-1]
        self.assertEqual(wire_result["tool_call_id"], proposal.tool_calls[0].call_id)
        self.assertEqual(json.loads(wire_result["content"]), execution.messages[0].result)
        first.close.assert_called_once_with()
        second.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
