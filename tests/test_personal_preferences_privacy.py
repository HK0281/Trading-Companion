"""Compare complete offline research requests across stored personal-data changes."""

from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

from scripts.verify import clean_environment, offline_network
from trading_companion.api_service import ResearchApiService
from trading_companion.news_triage_shadow import NewsTitleCapture
from trading_companion.news_triage_sorting import NewsTriageSorter
from trading_companion.personal_memory_store import SqlitePersonalMemoryStore
from trading_companion.turn_trace import JsonlTraceSink
from tests.test_news_triage_sorting import Transport, expanded_data, settings
from tests.test_product_agent import ProductModelFixture
from tests.test_product_narrative_answer import NarrativeModel
from tests.test_product_research import MemorySourceFactory, NOW, directory
from tests.test_research_intent_adapter import IntentFixture
from tests.test_research_intent_v2 import draft


QUESTION = "分析平安银行的新闻和公告"
PRIVATE_TIME = datetime(2041, 7, 19, 8, 23, 47, tzinfo=timezone.utc)


class FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)


class RecordingResearchModel:
    """Record every full protocol call, including legacy understanding and synthesis."""

    def __init__(self):
        self.calls = []
        self.understanding = ProductModelFixture()
        self.answer = NarrativeModel()

    def complete(self, messages, *, tools):
        self.calls.append(deepcopy((tuple(messages), tuple(tools))))
        if tools:
            return self.understanding.complete(messages, tools)
        return self.answer.complete(messages, tools=tools)


class PreferencesPrivacyTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(TemporaryDirectory()))
        self.stack.enter_context(clean_environment())
        self.stack.enter_context(offline_network())
        self.stack.enter_context(patch("trading_companion.local_env._read_local_values",
                                       side_effect=AssertionError("No .env reads")))
        self.stack.enter_context(patch("trading_companion.multi_contracts.uuid4", return_value=UUID(int=714)))
        for module in ("multi_analysis", "analysis", "research_tool"):
            self.stack.enter_context(patch(f"trading_companion.{module}.datetime", FixedDatetime))

    def build(self, label, *, intent_enabled, sorting):
        folder = self.root / label
        store = SqlitePersonalMemoryStore(folder / "personal.sqlite3", clock=lambda: PRIVATE_TIME)
        self.stack.callback(store.close)
        answers, intents = [], []

        def answer_factory():
            model = RecordingResearchModel()
            answers.append(model)
            return model

        def intent_factory():
            model = IntentFixture([draft(research_kinds=[dict(kind="news", phrase="新闻"),
                dict(kind="announcements", phrase="公告")])])
            intents.append(model)
            return model

        post = Transport()
        capture = NewsTitleCapture(folder / "titles")
        sorter = NewsTriageSorter(configure=settings, post=post)
        service = ResearchApiService(model_factory=answer_factory, provider="deepseek", data_mode="tencent",
            intent_model_factory=intent_factory, directory_loader=lambda _: directory(),
            wall_clock=lambda: NOW, reference_date=NOW.date(), product_data_service=expanded_data(4),
            conversation_context=True, personal_memory_store=store, headline_capture=capture,
            research_intent_v2_enabled=intent_enabled, news_triage_sorting=sorting, news_sorter=sorter,
            trace_sink=JsonlTraceSink(folder / "trace"))
        self.stack.callback(service.close)
        return service, store, answers, intents, post, capture, sorter

    def observe_states(self, *, intent_enabled, sorting):
        label = f"intent-{intent_enabled}-sorting-{sorting}"
        service, store, answers, intents, post, capture, _ = self.build(label,
            intent_enabled=intent_enabled, sorting=sorting)
        factory = MemorySourceFactory()
        observations, private_tokens = [], []
        with patch("trading_companion.product_research._build_source", side_effect=factory):
            for phase in ("empty", "saved", "modified", "deleted"):
                current = service.preferences()["items"]
                if phase == "saved":
                    service.patch_preferences("000001.SZ",
                        dict(holding_status="held", horizon="long", risk="high"), None)
                elif phase == "modified":
                    service.patch_preferences("000001.SZ",
                        dict(holding_status="not_held", horizon="short", risk="low"), current[0]["revision"])
                elif phase == "deleted":
                    service.remove_preferences("000001.SZ", current[0]["revision"])
                for item in service.preferences()["items"]:
                    private_tokens.extend(item[key] for key in ("revision", "created_at", "updated_at"))

                post.calls.clear()
                intent_count = len(intents)
                with patch.object(store, "list_preferences", side_effect=AssertionError("Research read preferences")), \
                        patch.object(store, "list", side_effect=AssertionError("Research read watchlist")), \
                        patch.object(service, "_personal_store", side_effect=AssertionError("Research opened personal data")):
                    binding = service.create_session()
                    result = service.message(binding["session_id"], binding["access_token"], uuid4().hex, QUESTION)["result"]
                self.assertIsNotNone(result.get("company_research"), (phase, result["code"]))
                self.assertIsNone(result.get("personal_memory"))
                self.assertTrue(answers[-1].calls, phase)
                intent_calls = deepcopy(intents[-1].calls) if len(intents) > intent_count else []
                self.assertEqual(bool(intent_calls), intent_enabled)
                # Sorting may run independent item tasks; compare complete bodies as a multiset,
                # without deleting fields or normalizing any content in those bodies.
                classification = sorted((provider, json.dumps(body, sort_keys=True, ensure_ascii=False))
                                        for provider, body in post.calls)
                if sorting:
                    self.assertTrue(any(provider == "jev" for provider, _ in classification), phase)
                    self.assertTrue(any(provider == "deepseek" for provider, _ in classification), phase)
                else:
                    self.assertEqual(classification, [])
                observations.append((intent_calls, deepcopy(answers[-1].calls), classification))

        for index, observed in enumerate(observations[1:], 1):
            self.assertEqual(observed, observations[0], f"Private-data state {index} changed complete requests")
        model_text = repr(observations)
        for private in ("holding_status", "collection_revision", "personal-memory-preferences-v1", *private_tokens):
            self.assertNotIn(private, model_text)
        self.assertGreater(capture.status()["records_written"], 0)
        for file in (self.root / label / "titles").rglob("*.json"):
            public_text = file.read_text(encoding="utf-8")
            for private in ("holding_status", "collection_revision", *private_tokens):
                self.assertNotIn(private, public_text)

    def test_full_intent_and_answer_requests_unchanged_before_save_modify_delete(self):
        for enabled in (False, True):
            with self.subTest(intent_enabled=enabled):
                self.observe_states(intent_enabled=enabled, sorting=False)

    def test_actual_jev_and_classifier_request_bodies_unchanged_across_private_changes(self):
        self.observe_states(intent_enabled=True, sorting=True)

    def test_explicit_cost_setters_mixed_and_negated_management_never_call_out(self):
        service, store, answers, intents, post, capture, sorter = self.build("management",
            intent_enabled=True, sorting=True)
        binding = service.create_session()
        with patch.object(capture, "capture", wraps=capture.capture) as capture_spy, \
                patch.object(sorter, "sort", wraps=sorter.sort) as sorter_spy:
            for question in (
                "把平安银行的成本价设为10.73元", "设置平安银行的仓位为23%",
                "请分析平安银行，然后把平安银行的期限偏好设为中线",
                "不要把平安银行的风险偏好设为积极",
                "记住我持有平安银行并分析走势",
                "把平安银行的风险偏好设为谨慎并记住我的仓位23%",
            ):
                with self.subTest(question=question):
                    result = service.message(binding["session_id"], binding["access_token"], uuid4().hex, question)["result"]
                    self.assertEqual(result["stage"], "personal_memory")
                    self.assertEqual((result["model_calls_attempted"], result["analyses_used"]), (0, 0))
                    self.assertEqual(result["personal_memory"]["items"], [])
            capture_spy.assert_not_called()
            sorter_spy.assert_not_called()
        self.assertEqual(store.list_preferences()["items"], [])
        self.assertTrue(all(model.calls == [] for model in answers))
        self.assertTrue(all(model.calls == [] for model in intents))
        self.assertEqual(post.calls, [])
        self.assertEqual(service._entries[binding["session_id"]].digests, [])


if __name__ == "__main__":
    unittest.main()
