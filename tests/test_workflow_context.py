"""Deterministic helper compaction preserves evidence, protocol and first observation."""

from datetime import date, timedelta
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from trading_companion.agent_input import request_arguments
from trading_companion.daily_bar import DailyBar
from trading_companion.data_source import CsvDailyBarSource
from trading_companion.indicator_knowledge import DEFINITIONS, KNOWLEDGE_VERSION, KnowledgeAnswer
from trading_companion.model import ModelRequestError
from trading_companion.multi_contracts import MultiIndicatorRequest
from trading_companion.tool_model import ToolCall, ToolMessage, validate_tool_messages
from trading_companion.workflow_context import _KNOWLEDGE_SUMMARIES, compact_consumed_helpers
from trading_companion.workflow_tools import (
    ADDITIONAL_TOOL_NAME, KNOWLEDGE_TOOL_NAME, SNAPSHOT_TOOL_NAME, WorkflowToolExecutor,
)


SECRET = "arbitrary-external-model-text-never-copy-into-summary"


class WorkflowContextTests(unittest.TestCase):
    def setUp(self):
        self.request = MultiIndicatorRequest("000001.SZ", date(2026, 1, 1), date(2026, 4, 10),
                                             source="csv", request_id="primary-evidence")
        source = CsvDailyBarSource(Path("not-opened.csv"), is_synthetic=True)
        self.executor = WorkflowToolExecutor(self.request, development_source=source)
        self.messages = [ToolMessage("system", "Only use the confirmed scope."),
                         ToolMessage("user", "Analyze the synthetic fixture.")]
        start = self.request.start_date
        records = [DailyBar("000001.SZ", start + timedelta(days=index),
                           10 + index / 10, 10 + index / 10, 10 + index / 10,
                           10 + index / 10, 1000 + index, 0.0) for index in range(100)]
        with patch.object(CsvDailyBarSource, "read_daily_bars", return_value=records):
            self.append("analyze_daily_indicators", request_arguments(self.request), "primary")
        self.append(KNOWLEDGE_TOOL_NAME, {"topics": ["sma", "rsi"]}, "knowledge")
        self.append(SNAPSHOT_TOOL_NAME, {"section": "availability"}, "availability")
        self.append(ADDITIONAL_TOOL_NAME, {"indicators": [{"kind": "ema", "window": 30}]}, "supplement")
        self.assertEqual(self.messages[-1].result["version"], "research-view-v1")

    def append(self, name, arguments, identifier):
        call = ToolCall(identifier, name, arguments)
        output = self.executor.execute(call, messages=self.messages)
        self.messages.extend((ToolMessage("assistant", tool_calls=(call,)), output))

    def compact(self, *, messages=None, count=None):
        messages = self.messages if messages is None else messages
        return compact_consumed_helpers(messages,
            consumed_message_count=len(messages) if count is None else count)

    def replaced_tool(self, index, payload, *, messages=None, name=None):
        changed = list(self.messages if messages is None else messages)
        original = changed[index]
        changed[index] = ToolMessage("tool", call_id=original.call_id,
            name=original.name if name is None else name, result=payload)
        return changed

    def test_consumed_helpers_shrink_but_primary_and_supplement_remain_exact(self):
        original = [message.to_dict() for message in self.messages]
        compacted = self.compact()
        self.assertIsNot(compacted, self.messages)
        self.assertEqual([message.to_dict() for message in self.messages], original)
        for index in (0, 1, 2, 3, 4, 6, 8, 9):
            self.assertIs(compacted[index], self.messages[index])
            self.assertEqual(compacted[index].to_dict(), original[index])
        self.assertEqual(compacted[5].result["status"], "consumed_knowledge_result")
        self.assertEqual(compacted[7].result["status"], "consumed_snapshot_summary")
        self.assertEqual(compacted[5].result["topics"], ["sma", "rsi"])
        self.assertEqual(set(compacted[5].result["definitions"]), {"sma", "rsi"})
        self.assertEqual(compacted[5].result["scope"], "indicator_definitions_only")
        self.assertIn("not model memory", compacted[5].result["note"])
        self.assertNotIn("text", compacted[5].result)
        self.assertNotIn("fields", compacted[7].result)
        size = lambda messages: len(json.dumps([message.to_dict() for message in messages], ensure_ascii=True))
        self.assertLess(size(compacted), size(self.messages))

    def test_latest_unconsumed_helper_must_be_supplied_in_full_to_next_model(self):
        messages = self.messages[:8]
        compacted = self.compact(messages=messages, count=6)
        self.assertEqual(compacted[5].result["status"], "consumed_knowledge_result")
        self.assertIs(compacted[7], messages[7])
        self.assertEqual(compacted[7].result["status"], "snapshot_summary")
        first_lookup = self.compact(messages=self.messages[:6], count=4)
        self.assertIs(first_lookup[5], self.messages[5])
        self.assertEqual(first_lookup[5].result["status"], "knowledge_result")

    def test_empty_consumed_prefix_does_not_compact_anything(self):
        output = self.compact(count=0)
        self.assertEqual(output, self.messages)
        self.assertTrue(all(left is right for left, right in zip(output, self.messages)))

    def test_repeated_compaction_is_idempotent(self):
        first = self.compact()
        second = self.compact(messages=first)
        self.assertEqual(first, second)
        self.assertTrue(all(left is right for left, right in zip(first, second)))

    def test_all_calls_arguments_pairings_and_protocol_remain_valid(self):
        output = self.compact()
        validate_tool_messages(output, tools=self.executor.definitions)
        for left, right in zip(self.messages, output):
            self.assertEqual(left.role, right.role)
            self.assertEqual(left.call_id, right.call_id)
            self.assertEqual(left.name, right.name)
            self.assertEqual(left.tool_calls, right.tool_calls)

    def test_helper_summary_does_not_copy_arbitrary_original_body_text(self):
        payload = self.messages[5].result
        payload["text"] = SECRET
        changed = self.replaced_tool(5, payload)
        output = self.compact(messages=changed)
        self.assertEqual(output[5].result["status"], "consumed_knowledge_result")
        self.assertNotIn(SECRET, json.dumps(output[5].to_dict()))
        self.assertEqual(changed[5].result["text"], SECRET)

    def test_every_application_topic_retains_a_bounded_canonical_definition(self):
        self.assertEqual(set(_KNOWLEDGE_SUMMARIES), set(DEFINITIONS))
        for topic in DEFINITIONS:
            with self.subTest(topic=topic):
                proposal = ToolMessage("assistant", tool_calls=(ToolCall(
                    "definition", KNOWLEDGE_TOOL_NAME, {"topics": [topic]}),))
                payload = {**KnowledgeAnswer((topic,)).to_dict(), "status": "knowledge_result"}
                original = ToolMessage("tool", call_id="definition", name=KNOWLEDGE_TOOL_NAME,
                                       result=payload)
                history = [ToolMessage("user", "Explain the requested indicator."), proposal, original]
                compacted = compact_consumed_helpers(history, consumed_message_count=len(history))
                summary = compacted[-1].result
                self.assertEqual(summary["version"], KNOWLEDGE_VERSION)
                self.assertEqual(summary["topics"], [topic])
                self.assertEqual(summary["definitions"], {topic: _KNOWLEDGE_SUMMARIES[topic]})
                definition = summary["definitions"][topic]
                self.assertTrue(definition.isascii())
                self.assertTrue(100 <= len(definition) <= 160, (topic, len(definition)))
                validate_tool_messages(compacted, tools=self.executor.definitions)

    def test_canonical_summaries_keep_essential_conventions_and_limitations(self):
        expected = {
            "sma": ("last N daily closes", "not a forecast"),
            "ema": ("first N closes' SMA", "2/(N+1)", "depend on start"),
            "macd": ("fast EMA-slow EMA", "DEA=EMA(DIF)", "2*(DIF-DEA)", "two adjacent"),
            "dif": ("fast=12", "slow=26", "not an upside probability"),
            "dea": ("signal=9", "valid DIF mean", "Missing DEA is not zero"),
            "histogram": ("2*(DIF-DEA)", "sign", "does not prove"),
            "rsi": ("Wilder", "0-100", "70", "30", "not guaranteed reversals"),
            "boll": ("population stddev", "ddof=0", "does not imply reversal", "collapse"),
            "atr": ("Wilder", "prior close", "CNY/share", "not direction", "stop-loss"),
            "volume": ("inclusive", "strictly prior", "today/prior mean", "undefined, not zero"),
        }
        for topic, fragments in expected.items():
            for fragment in fragments:
                with self.subTest(topic=topic, fragment=fragment):
                    self.assertIn(fragment, _KNOWLEDGE_SUMMARIES[topic])

    def test_rejected_failed_limited_and_unknown_results_are_never_upgraded(self):
        for status in ("rejected", "failed", "result_too_large", "unexpected_status"):
            with self.subTest(status=status):
                payload = self.messages[5].result
                payload["status"] = status
                changed = self.replaced_tool(5, payload)
                self.assertIs(self.compact(messages=changed)[5], changed[5])
        payload = self.messages[7].result
        payload["data_status"] = "source_error"
        changed = self.replaced_tool(7, payload)
        self.assertIs(self.compact(messages=changed)[7], changed[7])

    def test_unknown_tool_messages_are_untouched(self):
        changed = list(self.messages)
        changed[4] = ToolMessage("assistant", tool_calls=(ToolCall("knowledge", "unknown_helper", {}),))
        changed[5] = ToolMessage("tool", call_id="knowledge", name="unknown_helper",
                                result=self.messages[5].result)
        self.assertIs(self.compact(messages=changed)[5], changed[5])

    def test_mismatched_or_unpaired_helper_results_are_not_compacted(self):
        mismatched = list(self.messages)
        mismatched[5] = ToolMessage("tool", call_id="orphan", name=KNOWLEDGE_TOOL_NAME,
                                   result=self.messages[5].result)
        self.assertIs(self.compact(messages=mismatched)[5], mismatched[5])
        mismatched = list(self.messages)
        mismatched[4] = ToolMessage("assistant", tool_calls=(
            ToolCall("knowledge", KNOWLEDGE_TOOL_NAME, {"topics": ["atr"]}),))
        self.assertIs(self.compact(messages=mismatched)[5], mismatched[5])

    def test_malformed_or_unbounded_knowledge_fields_are_not_compacted(self):
        variants = ({"topics": []}, {"topics": [SECRET]}, {"topics": ["sma"] * 2},
                    {"origin": SECRET}, {"version": "unknown"}, {"market_data_used": True},
                    {"text": "x" * 3001}, {"text": None}, {"extra": SECRET})
        for changes in variants:
            with self.subTest(changes=list(changes)):
                payload = {**self.messages[5].result, **changes}
                changed = self.replaced_tool(5, payload)
                self.assertIs(self.compact(messages=changed)[5], changed[5])

    def test_snapshot_marker_requires_matching_primary_provenance(self):
        variants = ({"request_id": "different"}, {"instrument_id": "600000.SH"},
                    {"source": "tencent"}, {"data_authenticity": "provider_reported"},
                    {"price_adjustment": "unadjusted"}, {"retrieved_at": "invalid"},
                    {"bars_used": True}, {"bars_used": 367}, {"section": "unknown"},
                    {"fields": [["sma", "value", "made_up", 0]]}, {"extra": SECRET})
        for changes in variants:
            with self.subTest(changes=list(changes)):
                payload = {**self.messages[7].result, **changes}
                changed = self.replaced_tool(7, payload)
                self.assertIs(self.compact(messages=changed)[7], changed[7])

    def test_snapshot_without_primary_research_view_is_not_compacted(self):
        changed = list(self.messages)
        changed[3] = ToolMessage("tool", call_id="primary", name="analyze_daily_indicators",
                                result={"status": "result_too_large"})
        self.assertIs(self.compact(messages=changed)[7], changed[7])

    def test_overview_marker_uses_same_protected_primary(self):
        self.messages = self.compact()
        self.append(SNAPSHOT_TOOL_NAME, {"section": "overview"}, "overview")
        original = self.messages[-1]
        self.assertEqual(original.result["status"], "snapshot_summary")
        output = self.compact()
        self.assertEqual(output[-1].result["status"], "consumed_snapshot_summary")
        self.assertEqual(output[-1].result["section"], "overview")
        self.assertIs(output[3], self.messages[3])
        validate_tool_messages(output, tools=self.executor.definitions)

    def test_invalid_consumed_boundaries_and_message_shapes_are_rejected(self):
        for count in (-1, len(self.messages) + 1, True, None, "4"):
            with self.subTest(count=count), self.assertRaises(ModelRequestError):
                compact_consumed_helpers(self.messages, consumed_message_count=count)
        for messages in ([], {}, "messages", [object()], self.messages * 3):
            with self.subTest(kind=type(messages)), self.assertRaises(ModelRequestError):
                compact_consumed_helpers(messages, consumed_message_count=0)


if __name__ == "__main__":
    unittest.main()
