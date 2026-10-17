import json
import unittest
from contextlib import redirect_stdout
from dataclasses import FrozenInstanceError
from io import StringIO
from unittest.mock import patch

from trading_companion.model import ModelCallError, ModelConfigurationError, ModelRequestError, TokenUsage
from trading_companion.tool_model import (
    FakeToolModel, ToolCall, ToolDefinition, ToolMessage, ToolModelResponse,
    validate_tool_messages,
)
from trading_companion.tool_model_demo import main, run_demo


SECRET = "synthetic-sensitive-marker"


def definition(name="inspect_fixture"):
    return ToolDefinition(name, "Offline test declaration only", {
        "type": "object", "properties": {"label": {"type": "string"}},
        "required": ["label"], "additionalProperties": False,
    })


def call(call_id="call_1", name="inspect_fixture"):
    return ToolCall(call_id, name, {"label": "synthetic"})


def result(call_id="call_1", name="inspect_fixture"):
    return ToolMessage("tool", call_id=call_id, name=name, result={"is_synthetic": True, "value": 7})


def user():
    return ToolMessage("user", "Inspect an artificial fixture.")


def proposal(*calls):
    return ToolModelResponse("tool_calls", tool_calls=calls or (call(),))


class StrictJsonTests(unittest.TestCase):
    def test_json_arguments_preserve_valid_nested_types_and_detach_all_accessors(self):
        original = {"a": [1, 2.5, None, True, {"text": "人工"}]}
        item = ToolCall("call_1", "inspect_fixture", original)
        original["a"][4]["text"] = "changed"
        decoded = item.arguments
        self.assertEqual(decoded["a"][4]["text"], "人工")
        decoded["a"].append("external")
        serialized = item.to_dict()
        serialized["arguments"]["a"].clear()
        self.assertEqual(len(item.arguments["a"]), 5)
        json.dumps(item.to_dict(), allow_nan=False)
        with self.assertRaises(FrozenInstanceError):
            item.name = "changed"

    def test_schema_and_tool_results_are_not_changed_by_external_mutation(self):
        schema = {"type": "object", "properties": {"label": {"type": "string"}}, "required": ["label"]}
        tool = ToolDefinition("inspect_fixture", SECRET, schema)
        schema["properties"]["label"]["type"] = "number"
        tool.parameters["required"].clear()
        tool.to_dict()["parameters"]["properties"].clear()
        self.assertEqual(tool.parameters["properties"]["label"]["type"], "string")
        payload = {"values": [{"label": SECRET}]}
        message = ToolMessage("tool", call_id="call_1", name="inspect_fixture", result=payload)
        payload["values"][0]["label"] = "changed"
        message.result["values"].clear()
        message.to_dict()["result"]["values"].clear()
        self.assertEqual(message.result["values"][0]["label"], SECRET)

    def test_arguments_and_results_require_objects_not_json_scalar_or_array(self):
        for value in (None, True, False, 1, 1.5, [], (), "null", "[]", '"text"', "true"):
            with self.subTest(value=value):
                with self.assertRaises(ModelRequestError):
                    ToolCall("call_1", "inspect_fixture", value)
                with self.assertRaises(ModelRequestError):
                    ToolMessage("tool", call_id="call_1", name="inspect_fixture", result=value)

    def test_nonstring_keys_nonfinite_numbers_and_python_objects_are_rejected(self):
        values = ({1: "value"}, {"a": {None: 1}}, {"a": float("nan")},
                  {"a": [float("inf")]}, {"a": -float("inf")},
                  {"a": object()}, {"a": (1, 2)}, {"a": {1, 2}})
        for value in values:
            with self.subTest(value_type=type(value).__name__), self.assertRaises(ModelRequestError):
                ToolCall("call_1", "inspect_fixture", value)

    def test_json_text_rejects_duplicates_nonfinite_syntax_and_parser_content_leaks(self):
        texts = ('{"a":1,"a":2}', '{"a":{"x":1,"x":2}}', '{"a":NaN}',
                 '{"a":Infinity}', '{"a":-Infinity}', '{"a":1e999}',
                 '{"a":' + SECRET, SECRET)
        for text in texts:
            with self.subTest(text_kind=text[:10]):
                with self.assertRaises(ModelRequestError) as caught:
                    ToolCall("call_1", "inspect_fixture", text)
                self.assertNotIn(SECRET, str(caught.exception))
        self.assertEqual(ToolCall("call_1", "inspect_fixture", '{"label":"ok"}').arguments,
                         {"label": "ok"})

    def test_cycles_deep_structures_large_nodes_and_large_payloads_are_rejected(self):
        cycle = {}
        cycle["self"] = cycle
        deep = {}
        cursor = deep
        for _ in range(14):
            cursor["child"] = {}
            cursor = cursor["child"]
        for payload in (cycle, deep, {"values": [0] * 2001}, {"text": "x" * 10000},
                        '{"text":"' + "x" * 10000 + '"}'):
            with self.subTest(payload_type=type(payload).__name__), self.assertRaises(ModelRequestError):
                ToolCall("call_1", "inspect_fixture", payload)

    def test_shared_acyclic_subobjects_are_copied_as_legal_json(self):
        shared = {"value": 1}
        item = ToolCall("call_1", "inspect_fixture", {"left": shared, "right": shared})
        left = item.arguments
        left["left"]["value"] = 2
        self.assertEqual(left["right"]["value"], 1)
        self.assertEqual(item.arguments["left"]["value"], 1)

    def test_oversized_python_values_are_rejected_before_json_serialization(self):
        payloads = ({"items": [0] * 2001}, {str(index): 0 for index in range(1001)},
                    {"text": "x" * 10001}, {"x" * 10001: 1},
                    {"left": "x" * 6000, "right": "y" * 6000},
                    {"unicode": "界" * 1800}, {"emoji": "😀" * 900})
        for payload in payloads:
            with self.subTest(payload_keys=len(payload)), patch(
                    "trading_companion.tool_model.json.dumps",
                    side_effect=AssertionError("Oversized payload reached serialization")):
                with self.assertRaises(ModelRequestError):
                    ToolCall("call_1", "inspect_fixture", payload)


class ToolDeclarationAndMessageTests(unittest.TestCase):
    def test_identifiers_have_explicit_length_shape_and_type_limits(self):
        for value in (None, True, False, 1, "", "has space", "x" * 65, "slash/name", "密钥"):
            with self.subTest(value=value), self.assertRaises(ModelRequestError):
                ToolCall(value, "inspect_fixture", {})
            with self.subTest(name=value), self.assertRaises(ModelRequestError):
                ToolCall("call_1", value, {})
        ToolCall("x" * 64, "t" * 64, {})

    def test_schema_checks_root_declaration_shape_without_becoming_instance_validator(self):
        invalid = ({}, {"type": "array"}, {"type": "object", "properties": []},
                   {"type": "object", "properties": {"x": "string"}},
                   {"type": "object", "required": ["missing"]},
                   {"type": "object", "required": [True]},
                   {"type": "object", "properties": {"x": {}}, "required": ["x", "x"]},
                   {"type": "object", "additionalProperties": None},
                   {"type": "object", "$ref": "https://not-read.example"})
        for schema in invalid:
            with self.subTest(schema=schema), self.assertRaises(ModelRequestError):
                ToolDefinition("inspect_fixture", "description", schema)
        # Missing declared business fields are intentionally deferred to the future executor.
        model = FakeToolModel([proposal(ToolCall("call_1", "inspect_fixture", {}))])
        self.assertEqual(model.complete([user()], tools=[definition()]).status, "tool_calls")

    def test_plain_messages_and_tool_proposals_have_distinct_field_combinations(self):
        self.assertEqual(ToolMessage("user", "text").to_dict(), {"role": "user", "content": "text"})
        self.assertIsNone(ToolMessage("assistant", tool_calls=[call()]).content)
        invalid = (
            lambda: ToolMessage("tool", "text", call_id="call_1", name="inspect_fixture", result={}),
            lambda: ToolMessage("tool", call_id="call_1", name="inspect_fixture"),
            lambda: ToolMessage("tool", call_id=None, name="inspect_fixture", result={}),
            lambda: ToolMessage("user", "text", result={}),
            lambda: ToolMessage("user", "text", tool_calls=[call()]),
            lambda: ToolMessage("assistant", "text", call_id="call_1"),
            lambda: ToolMessage("assistant"),
            lambda: ToolMessage("assistant", tool_calls=True),
            lambda: ToolMessage("assistant", tool_calls=[{"name": "inspect_fixture"}]),
            lambda: ToolMessage("developer", "text"),
            lambda: ToolMessage(True, "text"),
        )
        for constructor in invalid:
            with self.assertRaises(ModelRequestError):
                constructor()

    def test_empty_text_bool_and_none_are_rejected_for_required_text_fields(self):
        for value in (None, True, False, 123, "", " \n"):
            with self.subTest(value=value), self.assertRaises(ModelRequestError):
                ToolMessage("user", value)
            with self.subTest(description=value), self.assertRaises(ModelRequestError):
                ToolDefinition("inspect_fixture", value, {"type": "object"})

    def test_repr_hides_messages_arguments_schema_results_response_and_script(self):
        tool = ToolDefinition("inspect_fixture", SECRET, {"type": "object", "description": SECRET})
        item = ToolCall("call_1", "inspect_fixture", {"label": SECRET})
        message = ToolMessage("assistant", SECRET, tool_calls=[item])
        tool_result = ToolMessage("tool", call_id="call_1", name="inspect_fixture", result={"x": SECRET})
        response = ToolModelResponse("complete", SECRET)
        for value in (tool, item, message, tool_result, response, FakeToolModel([response])):
            self.assertNotIn(SECRET, repr(value))
        self.assertIn(SECRET, response.to_dict()["text"])


class HistoryValidationTests(unittest.TestCase):
    def setUp(self):
        self.tools = (definition(), definition("second_fixture"))
        self.calls = (call(), call("call_2", "second_fixture"))
        self.start = [user(), proposal(*self.calls).as_message()]

    def validate(self, messages):
        return validate_tool_messages(messages, tools=self.tools)

    def test_multiple_results_may_arrive_in_any_order_but_every_id_is_required(self):
        for outputs in ((result(), result("call_2", "second_fixture")),
                        (result("call_2", "second_fixture"), result())):
            history, _, ids = self.validate([*self.start, *outputs])
            self.assertEqual(len(history), 4)
            self.assertEqual(ids, {"call_1", "call_2"})

    def test_missing_unknown_duplicate_or_name_mismatched_results_are_rejected(self):
        invalid = ([*self.start], [*self.start, result()],
                   [*self.start, result("unknown")],
                   [*self.start, result(name="second_fixture")],
                   [*self.start, result(), result()],
                   [*self.start, result(), result("call_2", "second_fixture"), result()])
        for history in invalid:
            with self.subTest(length=len(history)), self.assertRaises(ModelRequestError):
                self.validate(history)

    def test_user_or_assistant_cannot_interrupt_a_pending_tool_group(self):
        for interruption in (user(), ToolMessage("assistant", "premature answer"),
                             proposal(call("call_3")).as_message(), ToolMessage("system", "instruction")):
            with self.subTest(role=interruption.role), self.assertRaises(ModelRequestError):
                self.validate([*self.start, result(), interruption])

    def test_orphan_tool_result_cannot_be_mistaken_for_a_user_request(self):
        for history in ([result()], [user(), result()],
                        [user(), ToolMessage("assistant", "answer"), result()]):
            with self.assertRaises(ModelRequestError):
                self.validate(history)

    def test_call_ids_are_unique_within_group_and_across_previous_groups(self):
        with self.assertRaises(ModelRequestError):
            ToolMessage("assistant", tool_calls=[call(), call()])
        resolved = [user(), proposal().as_message(), result()]
        with self.assertRaises(ModelRequestError):
            self.validate([*resolved, proposal().as_message(), result()])

    def test_tool_calls_must_name_a_current_declared_tool(self):
        with self.assertRaises(ModelRequestError):
            self.validate([user(), proposal(call(name="undeclared")).as_message(), result(name="undeclared")])
        with self.assertRaises(ModelRequestError):
            validate_tool_messages([user()], tools=[definition(), definition()])

    def test_only_a_user_or_completed_result_group_can_end_a_model_request(self):
        invalid = ([], None, "text", [user().to_dict()], [ToolMessage("system", "instructions")],
                   [ToolMessage("assistant", "answer")],
                   [user(), ToolMessage("assistant", "answer")],
                   [user(), user()], [user(), ToolMessage("system", "instructions")])
        for history in invalid:
            with self.subTest(history_type=type(history).__name__), self.assertRaises(ModelRequestError):
                self.validate(history)
        self.validate([ToolMessage("system", "instructions"), user()])
        self.validate([user(), ToolMessage("assistant", "answer"), user()])

    def test_messages_and_user_text_have_explicit_nontruncating_limits(self):
        ToolMessage("user", "x" * 2000)
        with self.assertRaises(ModelRequestError):
            ToolMessage("user", "x" * 2001)
        history = []
        for _ in range(10):
            history.extend((user(), ToolMessage("assistant", "answer")))
        history.append(user())
        with self.assertRaises(ModelRequestError):
            self.validate(history)

    def test_tool_json_and_definitions_count_toward_total_request_limit(self):
        calls = (ToolCall("call_1", "inspect_fixture", {"x": "a" * 9000}),
                 ToolCall("call_2", "second_fixture", {"x": "b" * 9000}))
        messages = [user(), proposal(*calls).as_message(),
                    ToolMessage("tool", call_id="call_1", name="inspect_fixture", result={"x": "c" * 9000}),
                    result("call_2", "second_fixture")]
        with self.assertRaises(ModelRequestError):
            self.validate(messages)
        giant_tools = [ToolDefinition("tool_a", "x" * 11000, {"type": "object"}),
                       ToolDefinition("tool_b", "y" * 11000, {"type": "object"})]
        with self.assertRaises(ModelRequestError):
            validate_tool_messages([user()], tools=giant_tools)


class FakeToolModelTests(unittest.TestCase):
    def setUp(self):
        self.tools = (definition(),)
        self.post = patch("requests.post", side_effect=AssertionError("Unexpected network request")).start()
        self.addCleanup(patch.stopall)

    def test_complete_offline_round_trip_and_script_consumption(self):
        script = [proposal(), ToolModelResponse("complete", "Artificial fixture summary.")]
        model = FakeToolModel(script)
        script.clear()
        initial = [ToolMessage("system", "Offline only"), user()]
        first = model.complete(initial, tools=self.tools)
        self.assertEqual(first.status, "tool_calls")
        self.assertFalse(first.is_complete)
        complete = model.complete([*initial, first.as_message(), result()], tools=self.tools)
        self.assertTrue(complete.is_complete)
        self.assertEqual(model.responses_consumed, 2)
        self.assertEqual((complete.provider, complete.model, complete.is_mock, complete.usage),
                         ("fake", "fake-tools-v1", True, None))
        with self.assertRaises(ModelCallError) as caught:
            model.complete(initial, tools=self.tools)
        self.assertEqual(caught.exception.code, "script_exhausted")
        self.post.assert_not_called()

    def test_input_validation_failure_never_consumes_the_next_response(self):
        model = FakeToolModel([ToolModelResponse("complete", "answer")])
        with self.assertRaises(ModelRequestError):
            model.complete([user(), proposal().as_message()], tools=self.tools)
        self.assertEqual(model.responses_consumed, 0)
        self.assertEqual(model.complete([user()], tools=self.tools).text, "answer")

    def test_unknown_model_tool_is_a_classified_output_error_and_does_not_consume(self):
        model = FakeToolModel([proposal(call(name="undeclared"))])
        with self.assertRaises(ModelCallError) as caught:
            model.complete([user()], tools=self.tools)
        self.assertEqual(caught.exception.code, "unknown_tool")
        self.assertEqual(model.responses_consumed, 0)
        self.assertEqual(model.complete([user()], tools=[definition("undeclared")]).status, "tool_calls")

    def test_model_cannot_reuse_a_historical_call_id(self):
        model = FakeToolModel([proposal()])
        with self.assertRaises(ModelCallError) as caught:
            model.complete([user(), proposal().as_message(), result()], tools=self.tools)
        self.assertEqual(caught.exception.code, "invalid_response")
        self.assertEqual(model.responses_consumed, 0)

    def test_invalid_script_item_is_output_error_and_exhaustion_never_restarts(self):
        model = FakeToolModel([{SECRET: "not a protocol response"}])
        with self.assertRaises(ModelCallError) as caught:
            model.complete([user()], tools=self.tools)
        self.assertEqual(caught.exception.code, "invalid_response")
        self.assertNotIn(SECRET, str(caught.exception))
        self.assertEqual(model.responses_consumed, 0)
        with self.assertRaises(ModelCallError):
            FakeToolModel([]).complete([user()], tools=self.tools)

    def test_refused_and_incomplete_are_valid_consumed_responses_but_never_complete(self):
        model = FakeToolModel([ToolModelResponse("refused", "Cannot answer."),
                               ToolModelResponse("incomplete", "partial"),
                               ToolModelResponse("incomplete")])
        for count in (1, 2, 3):
            response = model.complete([user()], tools=self.tools)
            self.assertFalse(response.is_complete)
            self.assertEqual(model.responses_consumed, count)
            with self.assertRaises(ModelCallError):
                response.as_message()

    def test_response_status_fields_cannot_mix_text_calls_refusals_or_truncation(self):
        invalid = (lambda: ToolModelResponse("complete"),
                   lambda: ToolModelResponse("complete", "answer", (call(),)),
                   lambda: ToolModelResponse("tool_calls"),
                   lambda: ToolModelResponse("refused", None),
                   lambda: ToolModelResponse("refused", "reason", (call(),)),
                   lambda: ToolModelResponse("incomplete", "partial", (call(),)),
                   lambda: ToolModelResponse("unknown", "answer"),
                   lambda: ToolModelResponse(True, "answer"),
                   lambda: ToolModelResponse("complete", SECRET, is_mock=None),
                   lambda: ToolModelResponse("complete", "answer", usage={}))
        for constructor in invalid:
            with self.assertRaises(ModelCallError) as caught:
                constructor()
            self.assertNotIn(SECRET, str(caught.exception))
        ToolModelResponse("tool_calls", "Requesting the fixture.", (call(),))

    def test_provider_model_metadata_are_opaque_bounded_names_not_call_ids(self):
        response = ToolModelResponse("complete", "answer", provider="provider.example",
                                     requested_model="org/model-v1.0", model="org/model-v1.0:revision")
        self.assertEqual(response.model, "org/model-v1.0:revision")
        for value in (None, True, "", "has space", "new\nline", "control\x00", "x" * 129):
            with self.subTest(value=value), self.assertRaises(ModelCallError):
                ToolModelResponse("complete", "answer", model=value)

    def test_response_and_script_payloads_are_detached_and_fake_usage_remains_unknown(self):
        arguments = {"nested": [{"value": 7}]}
        item = ToolCall("call_1", "inspect_fixture", arguments)
        calls = [item]
        response = ToolModelResponse("tool_calls", tool_calls=calls,
                                     usage=TokenUsage(1, 2, 3), is_mock=False)
        model = FakeToolModel([response])
        calls.clear()
        arguments["nested"].clear()
        response.to_dict()["tool_calls"][0]["arguments"]["nested"].clear()
        actual = model.complete([user()], tools=self.tools)
        self.assertEqual(actual.tool_calls[0].arguments, {"nested": [{"value": 7}]})
        self.assertIsNone(actual.usage)
        self.assertTrue(actual.is_mock)
        json.dumps(actual.to_dict(), allow_nan=False)

    def test_fake_script_and_request_collections_are_bounded_and_noncallable(self):
        for script in (None, "text", lambda: [], [ToolModelResponse("complete", "x")] * 101):
            with self.subTest(script_type=type(script).__name__), self.assertRaises(ModelConfigurationError):
                FakeToolModel(script)
        with self.assertRaises(ModelRequestError):
            validate_tool_messages([user()], tools=[definition(f"tool_{i}") for i in range(13)])
        with self.assertRaises(ModelCallError):
            proposal(*(call(f"call_{i}") for i in range(9)))

    def test_oversized_response_is_rejected_without_silent_truncation(self):
        with self.assertRaises(ModelCallError):
            ToolModelResponse("complete", "x" * 20001)
        calls = tuple(ToolCall(f"call_{i}", "inspect_fixture", {"x": "a" * 9000}) for i in range(3))
        with self.assertRaises(ModelCallError):
            proposal(*calls)

    def test_demo_is_explicitly_mock_and_injects_only_fixed_artificial_results(self):
        first, second = run_demo(), run_demo()
        self.assertEqual(first, second)
        self.assertEqual(first["status"], "offline_mock_demo")
        self.assertTrue(first["is_mock"])
        self.assertFalse(first["executed_tools"])
        self.assertFalse(first["network_used"])
        self.assertEqual(first["injected_result"]["result"]["value"], 7)
        self.assertEqual(first["proposal"]["status"], "tool_calls")
        self.assertEqual(first["final_response"]["status"], "complete")
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(), 0)
        self.assertEqual(json.loads(output.getvalue()), first)
        self.post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
