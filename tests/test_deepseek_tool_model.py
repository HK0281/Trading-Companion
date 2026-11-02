import json
import os
from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import requests

from trading_companion.data_source import CsvDailyBarSource
from trading_companion.deepseek_model import DEEPSEEK_CHAT_URL, DeepSeekConfig
from trading_companion.deepseek_tool_model import DeepSeekToolModel
from trading_companion.model import (
    ChatMessage, ModelCallError, ModelConfigurationError, ModelRequestError, TokenUsage,
)
from trading_companion.research_tool import RESEARCH_TOOL_DEFINITION, ResearchToolExecutor
from trading_companion.tool_model import ToolCall, ToolDefinition, ToolMessage, ToolModelResponse


DUMMY_KEY = "synthetic-tool-test-credential-not-a-real-key"
DUMMY_MODEL = "synthetic-requested-tool-model"


def provider_call(call_id="call_1", name="inspect_fixture", arguments='{"window":3}'):
    return {"id": call_id, "type": "function", "function": {
        "name": name, "arguments": arguments,
    }}


def provider_payload(*, finish="stop", content="Synthetic explanation.", calls=None):
    message = {"role": "assistant", "content": content}
    if calls is not None:
        message["tool_calls"] = calls
    return {"model": "synthetic/served-model:revision",
            "choices": [{"finish_reason": finish, "message": message}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}


class DeepSeekToolModelTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.post_patch = patch("requests.post", side_effect=AssertionError("Unexpected HTTP call"))
        self.post = self.post_patch.start()
        self.addCleanup(self.post_patch.stop)
        self.config = DeepSeekConfig(DUMMY_MODEL, DUMMY_KEY)
        self.model = DeepSeekToolModel(self.config)
        self.messages = [ToolMessage("system", "Use only declared fixture evidence."),
                         ToolMessage("user", "Inspect the artificial fixture.")]
        self.tool = ToolDefinition("inspect_fixture", "Inspect artificial data.", {
            "type": "object", "properties": {"window": {"type": "integer"}},
            "required": ["window"], "additionalProperties": False,
        })
        self.tools = (self.tool,)

    def prepare(self, payload, status=200):
        response = Mock()
        response.status_code = status
        response.text = DUMMY_KEY
        response.json.return_value = payload
        self.post.side_effect = None
        self.post.return_value = response
        return response

    def complete(self):
        return self.model.complete(self.messages, tools=self.tools)

    def assert_bad_response(self, payload, code="invalid_response"):
        self.post.reset_mock()
        response = self.prepare(payload)
        with self.assertRaises(ModelCallError) as caught:
            self.complete()
        self.assertEqual(caught.exception.code, code)
        self.assertNotIn(DUMMY_KEY, str(caught.exception))
        self.post.assert_called_once()
        response.close.assert_called_once_with()

    def test_request_uses_function_envelope_and_explicit_nonstreaming_limits(self):
        response = self.prepare(provider_payload())
        config = DeepSeekConfig(DUMMY_MODEL, DUMMY_KEY, timeout_seconds=7.5, max_output_tokens=128)
        result = DeepSeekToolModel(config).complete(self.messages, tools=self.tools)
        self.post.assert_called_once_with(
            DEEPSEEK_CHAT_URL, headers={"Authorization": f"Bearer {DUMMY_KEY}"},
            json={"model": DUMMY_MODEL,
                  "messages": [{"role": "system", "content": "Use only declared fixture evidence."},
                               {"role": "user", "content": "Inspect the artificial fixture."}],
                  "tools": [{"type": "function", "function": self.tool.to_dict()}],
                  "tool_choice": "auto", "thinking": {"type": "disabled"},
                  "stream": False, "max_tokens": 128},
            timeout=7.5, allow_redirects=False,
        )
        self.assertEqual(result.status, "complete")
        self.assertEqual(result.text, "Synthetic explanation.")
        self.assertEqual(result.usage, TokenUsage(10, 5, 15))
        self.assertEqual((result.provider, result.requested_model, result.model, result.is_mock),
                         ("deepseek", DUMMY_MODEL, "synthetic/served-model:revision", False))
        response.close.assert_called_once_with()

    def test_arguments_decode_to_bounded_proposals_without_executing_anything(self):
        self.prepare(provider_payload(finish="tool_calls", content=None, calls=[provider_call()]))
        with patch.object(ResearchToolExecutor, "execute", side_effect=AssertionError("No execution here")) as execute:
            result = self.complete()
        self.assertEqual(result.status, "tool_calls")
        self.assertEqual(result.tool_calls[0].arguments, {"window": 3})
        self.assertEqual(result.tool_calls[0].call_id, "call_1")
        self.assertIsNone(result.text)
        self.assertFalse(result.is_complete)
        execute.assert_not_called()

    def test_temperature_is_explicit_opt_in_with_unchanged_default_envelope(self):
        self.prepare(provider_payload())
        self.complete()
        self.assertNotIn('temperature', self.post.call_args.kwargs['json'])
        model = DeepSeekToolModel(self.config, temperature=0)
        model.complete(self.messages, tools=self.tools)
        body = self.post.call_args.kwargs['json']
        self.assertEqual(body['temperature'], 0)
        self.assertEqual(body['thinking'], {'type': 'disabled'})
        for value in (True, False, -0.1, 2.1, float('nan'), float('inf'), '0'):
            with self.subTest(value=value), self.assertRaises(ModelConfigurationError):
                DeepSeekToolModel(self.config, temperature=value)

    def test_opt_in_diagnostics_distinguish_json_call_shape_and_missing_tools(self):
        model = DeepSeekToolModel(self.config, response_diagnostics=True)
        cases = ((provider_payload(finish='tool_calls', calls=[]), 'tool_calls_missing'),
                 (provider_payload(finish='tool_calls', calls=[provider_call(arguments='{"window":')]), 'tool_arguments_json_invalid'),
                 (provider_payload(finish='tool_calls', calls=[provider_call(arguments='[]')]), 'tool_call_invalid'),
                 ({'choices': []}, 'response_shape_invalid'))
        for payload, code in cases:
            self.post.reset_mock()
            provider = self.prepare(payload)
            with self.subTest(code=code), self.assertRaises(ModelCallError) as caught:
                model.complete(self.messages, tools=self.tools)
            self.assertEqual(caught.exception.code, code)
            self.assertNotIn(DUMMY_KEY, str(caught.exception))
            self.post.assert_called_once()
            provider.close.assert_called_once_with()
        provider = self.prepare(None)
        provider.json.side_effect = requests.exceptions.JSONDecodeError(DUMMY_KEY, 'invalid', 0)
        with self.assertRaises(ModelCallError) as caught:
            model.complete(self.messages, tools=self.tools)
        self.assertEqual(caught.exception.code, 'response_json_invalid')
        self.assertNotIn(DUMMY_KEY, str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)


    def test_candidate_repair_accepts_only_explicit_supported_single_versions(self):
        for version in ('research-intent-v2.5.4', 'research-intent-v2.6', 'research-intent-v2.6.1',
                        'research-intent-v2.6.2'):
            with self.subTest(version=version):
                tool = ToolDefinition('interpret_research_intent_v2', 'Synthetic candidate.', {
                    'type': 'object', 'properties': {'version': {'type': 'string', 'enum': [version]}}})
                self.prepare(provider_payload(finish='tool_calls', content=None, calls=[provider_call(
                    name=tool.name, arguments='{"objective_kind": analysis}')]))
                diagnostics = []
                model = DeepSeekToolModel(self.config, response_diagnostics=True, repair_tool_arguments=True,
                    save_failed_arguments=True, evaluation_scope='public', argument_diagnostic_sink=diagnostics.append)
                result = model.complete(self.messages, tools=(tool,))
                self.assertEqual(result.tool_calls[0].arguments, {'objective_kind': 'analysis'})
                self.assertTrue(diagnostics)
                # The same candidate declaration does not enable repair by default.
                with self.assertRaises(ModelCallError):
                    DeepSeekToolModel(self.config).complete(self.messages, tools=(tool,))

    def test_candidate_repair_and_capture_reject_other_declarations_before_http(self):
        for flags in (dict(repair_tool_arguments=True), dict(save_failed_arguments=True)):
            for versions, name, extra in ((['research-intent-v2.5.4', 'research-intent-v2.6'],
                    'interpret_research_intent_v2', False),
                    (['research-intent-v2.6', 'research-intent-v2.6.1'], 'interpret_research_intent_v2', False),
                    (['research-intent-v2.6.1', 'research-intent-v2.6.2'], 'interpret_research_intent_v2', False),
                    (['research-intent-v9'], 'interpret_research_intent_v2', False),
                    (['research-intent-v2.6'], 'inspect_fixture', False),
                    (['research-intent-v2.6.1'], 'inspect_fixture', False),
                    (['research-intent-v2.6.2'], 'inspect_fixture', False),
                    (['research-intent-v2.6.2'], 'interpret_research_intent_v2', True),
                    (['research-intent-v2.6.1'], 'interpret_research_intent_v2', True),
                    (['research-intent-v2.6'], 'interpret_research_intent_v2', True)):
                with self.subTest(flags=flags, versions=versions, name=name, extra=extra):
                    tool = ToolDefinition(name, 'Synthetic candidate.', {'type': 'object',
                        'properties': {'version': {'type': 'string', 'enum': versions}}})
                    model = DeepSeekToolModel(self.config, response_diagnostics=True, evaluation_scope='public',
                        argument_diagnostic_sink=lambda _: None, **flags)
                    with self.assertRaises(ModelConfigurationError):
                        model.complete(self.messages, tools=(tool, self.tool) if extra else (tool,))
            with self.assertRaises(ModelConfigurationError):
                DeepSeekToolModel(self.config, argument_diagnostic_sink=lambda _: None, **flags)
        self.post.assert_not_called()

    def test_empty_call_content_normalizes_to_none_but_nonempty_text_is_preserved(self):
        for content in (None, "", " \n", "Let me inspect the fixture."):
            self.prepare(provider_payload(finish="tool_calls", content=content, calls=[provider_call()]))
            result = self.complete()
            self.assertEqual(result.text, content if content and content.strip() else None)

    def test_completed_tool_history_converts_ids_and_serializes_structured_results(self):
        proposal = ToolModelResponse("tool_calls", tool_calls=(
            ToolCall("call_1", "inspect_fixture", {"window": 3}),))
        tool_result = {"value": 0, "warning": "人工样例", "nested": {"missing": None}}
        history = [*self.messages, proposal.as_message(),
                   ToolMessage("tool", call_id="call_1", name="inspect_fixture", result=tool_result)]
        original = [message.to_dict() for message in history]
        self.prepare(provider_payload())
        result = self.model.complete(history, tools=self.tools)
        wire = self.post.call_args.kwargs["json"]["messages"]
        self.assertEqual(wire[2], {"role": "assistant", "content": None,
                                  "tool_calls": [provider_call()]})
        self.assertEqual(wire[3]["role"], "tool")
        self.assertEqual(wire[3]["tool_call_id"], "call_1")
        self.assertIsInstance(wire[3]["content"], str)
        self.assertEqual(json.loads(wire[3]["content"]), tool_result)
        self.assertNotIn("result", wire[3])
        self.assertNotIn("name", wire[3])
        self.assertEqual(result.status, "complete")
        self.assertEqual([message.to_dict() for message in history], original)

    def test_normal_assistant_text_is_preserved_in_completed_conversation_history(self):
        self.prepare(provider_payload())
        messages = [*self.messages, ToolMessage("assistant", "Previous explanation."),
                    ToolMessage("user", "Explain that again.")]
        self.model.complete(messages, tools=self.tools)
        wire = self.post.call_args.kwargs["json"]["messages"]
        self.assertEqual(wire[-2], {"role": "assistant", "content": "Previous explanation."})

    def test_empty_tools_omit_tool_parameters_and_undeclared_output_is_rejected(self):
        self.prepare(provider_payload())
        self.assertTrue(self.model.complete(self.messages, tools=()).is_complete)
        body = self.post.call_args.kwargs["json"]
        self.assertNotIn("tools", body)
        self.assertNotIn("tool_choice", body)
        self.prepare(provider_payload(finish="tool_calls", calls=[provider_call()]))
        with self.assertRaises(ModelCallError) as caught:
            self.model.complete(self.messages, tools=())
        self.assertEqual(caught.exception.code, "unknown_tool")

    def test_invalid_history_and_declarations_fail_before_http(self):
        pending = ToolMessage("assistant", tool_calls=(ToolCall("call_1", "inspect_fixture", {}),))
        cases = [([], self.tools), ([ChatMessage("user", "Wrong contract")], self.tools),
                 ([*self.messages, pending], self.tools),
                 (self.messages, (self.tool, self.tool)),
                 (self.messages, [self.tool.to_dict()]),
                 ([ToolMessage("system", "x" * 19999), ToolMessage("user", "x")], self.tools)]
        for messages, tools in cases:
            with self.subTest(count=len(messages)), self.assertRaises(ModelRequestError):
                self.model.complete(messages, tools=tools)
        self.post.assert_not_called()

    def test_tool_configuration_respects_protocol_metadata_before_http(self):
        for config in (None, {}, DeepSeekConfig("x" * 129, DUMMY_KEY),
                       DeepSeekConfig("model\x00name", DUMMY_KEY)):
            with self.subTest(config_type=type(config).__name__), self.assertRaises(ModelConfigurationError):
                DeepSeekToolModel(config)
        DeepSeekToolModel(DeepSeekConfig("x" * 128, DUMMY_KEY))
        self.assertNotIn(DUMMY_KEY, repr(self.model))
        self.post.assert_not_called()

    def test_interrupted_responses_drop_even_valid_or_malformed_tool_calls(self):
        for reason in ("length", "insufficient_system_resource", "aborted"):
            for content in (None, "", "Partial output."):
                for calls in ([provider_call()], [{"function": {"arguments": DUMMY_KEY}}]):
                    self.prepare(provider_payload(finish=reason, content=content, calls=calls))
                    result = self.complete()
                    self.assertEqual(result.status, "incomplete")
                    self.assertFalse(result.is_complete)
                    self.assertEqual(result.tool_calls, ())
                    with self.assertRaises(ModelCallError):
                        result.as_message()

    def test_refusals_never_expose_calls_or_provider_refusal_details(self):
        for reason, refusal in (("content_filter", None), ("stop", DUMMY_KEY),
                                 ("tool_calls", DUMMY_KEY), ("length", DUMMY_KEY)):
            payload = provider_payload(finish=reason, calls=[provider_call()])
            payload["choices"][0]["message"]["refusal"] = refusal
            self.prepare(payload)
            result = self.complete()
            self.assertEqual(result.status, "refused")
            self.assertEqual(result.tool_calls, ())
            self.assertNotIn(DUMMY_KEY, json.dumps(result.to_dict()))
            with self.assertRaises(ModelCallError):
                result.as_message()

    def test_finish_reason_and_call_presence_must_agree(self):
        for payload in (provider_payload(finish="tool_calls"),
                        provider_payload(finish="tool_calls", calls=[]),
                        provider_payload(finish="stop", calls=[provider_call()])):
            self.assert_bad_response(payload)
        for reason in (None, "unknown", "function_call"):
            self.assert_bad_response(provider_payload(finish=reason), "unsupported_response")

    def test_legacy_function_call_is_not_converted_or_executed(self):
        payload = provider_payload()
        payload["choices"][0]["message"]["function_call"] = {"name": DUMMY_KEY}
        self.assert_bad_response(payload, "unsupported_response")

    def test_response_requires_one_assistant_choice_and_bounded_metadata(self):
        invalid = [None, [], "text", {}, {"choices": []}]
        mutations = [(("choices",), [None]),
                     (("choices",), provider_payload()["choices"] * 2),
                     (("choices", 0, "message"), None),
                     (("choices", 0, "message", "role"), "tool"),
                     (("choices", 0, "message", "content"), {}),
                     (("choices", 0, "message", "content"), "x" * 20001),
                     (("choices", 0, "message", "refusal"), True)]
        mutations += [(("model",), value) for value in (None, True, "", "has space", "bad\x00", "x" * 129)]
        for path, value in mutations:
            payload = provider_payload()
            target = payload
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            invalid.append(payload)
        for payload in invalid:
            self.assert_bad_response(payload)
        for content in (None, "", " \n"):
            self.assert_bad_response(provider_payload(content=content))

    def test_malformed_call_envelopes_and_collection_limits_are_rejected(self):
        invalid = ["calls", {}, [None], [{}], [dict(provider_call(), type="code")],
                   [dict(provider_call(), function=None)],
                   [provider_call(call_id="bad id")], [provider_call(call_id="x" * 65)],
                   [provider_call(), provider_call()],
                   [provider_call(call_id=f"call_{index}") for index in range(9)]]
        for calls in invalid:
            self.assert_bad_response(provider_payload(finish="tool_calls", calls=calls))

    def test_arguments_require_strict_bounded_json_object_strings(self):
        invalid = [None, {}, [], 3, "", "[]", "null", '"text"',
                   '{"x":NaN}', '{"x":Infinity}', '{"x":1e999}',
                   '{"window":3,"window":4}', '{"secret":"' + DUMMY_KEY,
                   '{"x":"' + "a" * 10000 + '"}',
                   '{"x":' + "[" * 13 + "0" + "]" * 13 + "}"]
        for arguments in invalid:
            self.assert_bad_response(provider_payload(finish="tool_calls", calls=[
                provider_call(arguments=arguments)]))

    def test_unknown_tool_and_reused_historical_id_are_classified(self):
        self.assert_bad_response(provider_payload(finish="tool_calls", calls=[
            provider_call(name=DUMMY_KEY)]), "unknown_tool")
        proposal = ToolModelResponse("tool_calls", tool_calls=(ToolCall("call_1", "inspect_fixture", {}),))
        history = [*self.messages, proposal.as_message(),
                   ToolMessage("tool", call_id="call_1", name="inspect_fixture", result={"value": 7})]
        self.prepare(provider_payload(finish="tool_calls", calls=[provider_call()]))
        with self.assertRaises(ModelCallError) as caught:
            self.model.complete(history, tools=self.tools)
        self.assertEqual(caught.exception.code, "invalid_response")

    def test_multiple_calls_are_only_proposals_with_unchanged_ids(self):
        self.prepare(provider_payload(finish="tool_calls", content=None, calls=[
            provider_call(call_id=f"call_{index}") for index in range(8)]))
        result = self.complete()
        self.assertEqual([call.call_id for call in result.tool_calls], [f"call_{index}" for index in range(8)])
        self.assertEqual(result.status, "tool_calls")

    def test_missing_usage_is_unknown_while_explicit_zero_is_preserved(self):
        for usage in (None, {}, {"prompt_tokens": 0}, {"total_tokens": None}):
            payload = provider_payload()
            payload["usage"] = usage
            self.prepare(payload)
            result = self.complete()
            if usage is None:
                self.assertIsNone(result.usage)
            else:
                self.assertIsNone(result.usage.total_tokens)
                self.assertEqual(result.usage.prompt_tokens, usage.get("prompt_tokens"))
        payload.pop("usage")
        self.prepare(payload)
        self.assertIsNone(self.complete().usage)

    def test_invalid_usage_never_becomes_a_successful_result(self):
        for usage in ([], "usage", {"prompt_tokens": -1}, {"completion_tokens": True},
                      {"total_tokens": 1.5}, {"total_tokens": float("nan")}):
            payload = provider_payload()
            payload["usage"] = usage
            self.assert_bad_response(payload)

    def test_reasoning_and_provider_extras_are_not_exposed_or_replayed(self):
        payload = provider_payload(finish="tool_calls", calls=[provider_call()])
        payload["choices"][0]["message"]["reasoning_content"] = DUMMY_KEY
        payload["internal_debug"] = DUMMY_KEY
        payload["usage"]["completion_tokens_details"] = {"reasoning_tokens": 0}
        original = deepcopy(payload)
        self.prepare(payload)
        proposal = self.complete()
        self.assertNotIn(DUMMY_KEY, json.dumps(proposal.to_dict()))
        self.assertNotIn(DUMMY_KEY, repr(proposal))
        self.assertEqual(payload, original)
        history = [*self.messages, proposal.as_message(),
                   ToolMessage("tool", call_id="call_1", name="inspect_fixture", result={"value": 7})]
        self.prepare(provider_payload())
        self.model.complete(history, tools=self.tools)
        self.assertNotIn(DUMMY_KEY, json.dumps(self.post.call_args.kwargs["json"]))

    def test_http_errors_are_sanitized_closed_and_never_retried(self):
        for status, code in ((401, "authentication_error"), (403, "authentication_error"),
                             (429, "rate_limited"), (302, "provider_error"),
                             (400, "provider_error"), (500, "provider_error")):
            self.post.reset_mock()
            response = self.prepare({"error": DUMMY_KEY}, status=status)
            with self.assertRaises(ModelCallError) as caught:
                self.complete()
            self.assertEqual(caught.exception.code, code)
            self.assertNotIn(DUMMY_KEY, str(caught.exception))
            self.post.assert_called_once()
            response.json.assert_not_called()
            response.close.assert_called_once_with()

    def test_network_and_json_errors_are_sanitized_and_never_retried(self):
        for error, code in ((requests.exceptions.Timeout(DUMMY_KEY), "timeout"),
                            (requests.exceptions.ConnectionError(DUMMY_KEY), "connection_error")):
            self.post.reset_mock()
            self.post.side_effect = error
            with self.assertRaises(ModelCallError) as caught:
                self.complete()
            self.assertEqual(caught.exception.code, code)
            self.assertNotIn(DUMMY_KEY, str(caught.exception))
            self.assertTrue(caught.exception.__suppress_context__)
            self.post.assert_called_once()
        self.post.reset_mock()
        response = self.prepare(None)
        response.json.side_effect = requests.exceptions.JSONDecodeError(DUMMY_KEY, "invalid", 0)
        with self.assertRaises(ModelCallError) as caught:
            self.complete()
        self.assertEqual(caught.exception.code, "invalid_response")
        self.assertNotIn(DUMMY_KEY, str(caught.exception))
        self.post.assert_called_once()
        response.close.assert_called_once_with()

    def test_missing_optional_dependency_fails_without_network(self):
        with patch.dict("sys.modules", {"requests": None}), self.assertRaises(ModelConfigurationError):
            self.complete()
        self.post.assert_not_called()

    def test_mocked_provider_round_trip_runs_real_research_on_trusted_synthetic_csv(self):
        arguments = {"instrument_id": "000001.SZ", "start_date": "2026-09-21",
                     "as_of_date": "2026-09-25", "indicators": [
                         {"kind": "sma", "window": 3}, {"kind": "sma", "window": 20},
                         {"kind": "volume", "window": 3}]}
        first = self.prepare(provider_payload(finish="tool_calls", content=None, calls=[
            provider_call(name="analyze_daily_indicators", arguments=json.dumps(arguments))]))
        second = Mock()
        second.status_code = 200
        second.json.return_value = provider_payload()
        self.post.side_effect = [first, second]
        source = CsvDailyBarSource(Path(__file__).resolve().parents[1] / "data" /
                                   "sample_daily_bars_synthetic.csv", is_synthetic=True)
        executor = ResearchToolExecutor(development_source=source)
        tools = (RESEARCH_TOOL_DEFINITION,)
        proposal = self.model.complete(self.messages, tools=tools)
        execution = executor.execute(proposal, messages=self.messages)
        history = [*self.messages, proposal.as_message(), *execution.messages]
        final = self.model.complete(history, tools=tools)
        self.assertEqual(execution.status, "executed")
        self.assertEqual(execution.full_result.data_status, "ok")
        self.assertEqual(execution.full_result.calculation_status, "limited")
        self.assertEqual(executor.analyses_used, 1)
        self.assertEqual(final.status, "complete")
        self.assertEqual(self.post.call_count, 2)
        wire_result = self.post.call_args.kwargs["json"]["messages"][-1]
        self.assertEqual(wire_result["tool_call_id"], proposal.tool_calls[0].call_id)
        self.assertEqual(json.loads(wire_result["content"]), execution.messages[0].result)
        self.assertEqual(json.loads(wire_result["content"])["source"], "csv")
        first.close.assert_called_once_with()
        second.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
