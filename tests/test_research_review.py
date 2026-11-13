"""Original judgments, exact price provenance and conservative review dates; entirely offline."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import unittest
from unittest.mock import Mock

from trading_companion.memory_store import LOCAL_USER, SqliteConversationStore
from trading_companion.research_review import (
    CONTEXT_VERSION, LEGACY_NOTICE, ReviewError, list_reviews, load_review, project_review,
)


NOW = datetime(2026, 11, 12, 3, tzinfo=timezone.utc)
ASKED = datetime(2026, 10, 1, 15, 55, tzinfo=timezone.utc)
UPDATED = datetime(2026, 10, 2, 1, tzinfo=timezone.utc)
STOCK = "000001.SZ"
OTHER = "600000.SH"
TECHNICAL_ID = "technical-request"
PLAN_ID = TECHNICAL_ID + "|atr_reference_plan_v1:conditional_entry"


def original(*, instrument=STOCK, context=True):
    plan = dict(version="trade-plan-v1", plan_id=PLAN_ID, instrument_id=instrument, request_id=TECHNICAL_ID,
                purpose="conditional_entry", status="available", code="plan_ready", origin="application",
                execution_allowed=False, source_date="2026-09-30", source="tencent", price_adjustment="unadjusted",
                data_authenticity="provider_reported", method="atr_reference_plan_v1",
                values=dict(reference_entry="10.00", reference_stop="9.00", target_1="11.50", target_2="12.50",
                            reward_risk_1="1.50", reward_risk_2="2.50", atr="0.6667", atr_multiple="1.5"))
    answer = dict(status="limited", code="company_research_partial", answer="当时原文10.00元。",
        company_research=dict(version="product-research-answer-v3", daily_cutoff="2026-10-01", explicit_historical=False,
            securities=[dict(instrument_id=instrument, name="原股票名")],
            decisions=[dict(instrument_id=instrument, status="available", code="decision_ready", rating="买入",
                            direction="偏多", conviction="低", horizon="未来20个交易日", plan_id=PLAN_ID)],
            trade_plans=[plan], evidence=[]))
    if context:
        answer["review_context"] = dict(version=CONTEXT_VERSION, event="company_research",
                                        completed_at="2026-10-01T16:05:00+00:00")
    return answer


def price_evidence():
    return dict(id=TECHNICAL_ID + "|price_context:latest_close", instrument_id=STOCK, kind="technical", content_kind="fact",
                date="2026-09-30", source="tencent", data_authenticity="provider_reported", unit="CNY/share",
                raw_value="10.00", temporal_scope="daily_price_context",
                dependencies=[TECHNICAL_ID + "|raw_daily_bar|2026-09-30|close_price"],
                calculation_basis=dict(version="daily_price_context_v1", price_adjustment="unadjusted", sample_count="1",
                                       cutoff="2026-09-30", dependency_scope="technical.bars:trade_date:field"))


def row(result=None):
    return dict(conversation_id="a" * 32, sequence=45, request_id="chat-request-not-technical", asked_at=ASKED.isoformat(),
                conversation_updated_at=UPDATED.isoformat(), question="当时问题", result=original() if result is None else result)


class ProjectionTests(unittest.TestCase):
    def project(self, result=None, source_row=None, now=NOW):
        return project_review(source_row or row(result), instrument_id=STOCK, now=now)

    def test_public_available_v3_and_internal_request_binding_preserve_original(self):
        source = row()
        frozen = deepcopy(source)
        item = self.project(source_row=source)
        self.assertEqual((item["status"], item["rating"], item["historical_type"]), ("available", "买入", "current"))
        self.assertEqual(item["trade_plan"]["values"]["reference_entry"], "10.00")
        self.assertNotEqual(item["request_id"], item["trade_plan"]["request_id"])
        self.assertTrue(item["prices"]["enabled"])
        self.assertEqual(item["prices"]["technical_request_id"], TECHNICAL_ID)
        self.assertEqual(source, frozen)

    def test_new_completion_crosses_shanghai_midnight_and_is_not_source_cutoff(self):
        item = self.project()
        self.assertEqual(item["prices"]["start_date"], "2026-10-03")
        self.assertEqual(item["prices"]["basis"], "completed_at")
        self.assertEqual(item["daily_cutoff"], "2026-10-01")
        self.assertIsNone(item["prices"]["notice"])

    def test_legacy_conversation_date_is_explicit_and_can_move_later(self):
        source = row(original(context=False))
        first = self.project(source_row=source)
        self.assertIsNone(first["completed_at"])
        self.assertEqual(first["prices"]["basis"], "conversation_updated_at")
        self.assertEqual(first["prices"]["start_date"], "2026-10-03")
        self.assertEqual(first["prices"]["notice"], LEGACY_NOTICE)
        source["conversation_updated_at"] = "2026-10-10T20:00:00+00:00"
        self.assertEqual(self.project(source_row=source)["prices"]["start_date"], "2026-10-12")

    def test_missing_naive_or_before_asked_updated_at_disables_legacy_without_guess(self):
        for stamp in (None, "2026-10-02T01:00:00", "2026-09-30T01:00:00+00:00", "not a time"):
            source = row(original(context=False))
            source["conversation_updated_at"] = stamp
            with self.subTest(stamp=stamp):
                item = self.project(source_row=source)
                self.assertEqual(item["prices"]["code"], "review_completion_unknown")
                self.assertFalse(item["prices"]["enabled"])
                self.assertEqual(item["prices"]["notice"], LEGACY_NOTICE)

    def test_invalid_new_context_does_not_fall_back_or_refresh_it(self):
        for context in ({}, dict(version=CONTEXT_VERSION, event="company_followup", completed_at=UPDATED.isoformat()),
                        dict(version=CONTEXT_VERSION, event="company_research", completed_at="2026-10-02T01:00:00")):
            value = original()
            value["review_context"] = context
            self.assertEqual(self.project(value)["prices"]["code"], "review_completion_unknown")

    def test_empty_range_disables_and_366_day_inclusive_limit(self):
        value = original()
        value["review_context"]["completed_at"] = NOW.isoformat()
        self.assertEqual(self.project(value)["prices"]["code"], "review_prices_not_yet_available")
        value["review_context"]["completed_at"] = "2023-12-30T12:00:00+00:00"
        source = row(value)
        source["asked_at"] = "2023-12-30T11:00:00+00:00"
        prices = self.project(source_row=source)["prices"]
        self.assertEqual((prices["start_date"], prices["end_date"]), ("2023-12-31", "2024-12-30"))

    def test_unavailable_and_legacy_fields_remain_readable_without_prose_inference(self):
        value = original()
        decision = value["company_research"]["decisions"][0]
        decision.update(status="unavailable", code="decision_insufficient_evidence", rating=None, direction=None,
                        conviction=None, plan_id=None)
        self.assertEqual(self.project(value)["status"], "unavailable")
        del value["company_research"]["decisions"]
        del value["company_research"]["explicit_historical"]
        item = self.project(value)
        self.assertEqual((item["status"], item["historical_type"]), ("unrecorded", "unknown"))
        self.assertIsNone(item["rating"])
        self.assertIsNone(item["trade_plan"])

    def test_historical_research_uses_actual_completion_and_followups_are_excluded(self):
        value = original()
        value["company_research"]["explicit_historical"] = True
        self.assertEqual(self.project(value)["historical_type"], "historical")
        for code in ("product_followup_answered", "product_saved_result", "index_research_completed"):
            value["code"] = code
            self.assertIsNone(self.project(value))

    def test_other_security_and_duplicate_identity_are_not_standardized(self):
        self.assertIsNone(self.project(original(instrument=OTHER)))
        value = original()
        value["company_research"]["securities"].append(dict(instrument_id=STOCK, name="冲突名称"))
        with self.assertRaisesRegex(ReviewError, "review_corrupt"):
            self.project(value)

    def test_cross_security_duplicate_wrong_method_and_wrong_purpose_plans_are_rejected(self):
        for update in (dict(instrument_id=OTHER), dict(request_id="other-technical"), dict(origin="model"),
                       dict(method="new_unverified_method"), dict(purpose="wait"), dict(execution_allowed=True)):
            value = original()
            value["company_research"]["trade_plans"][0].update(update)
            with self.subTest(update=update):
                item = self.project(value)
                self.assertIsNone(item["trade_plan"])
                self.assertEqual(item["prices"]["code"], "review_price_basis_conflict")
        value = original()
        value["company_research"]["trade_plans"] *= 2
        self.assertIsNone(self.project(value)["trade_plan"])

    def test_adjustment_and_authenticity_refuse_prices_but_keep_original_plan(self):
        for update, code in ((dict(price_adjustment="qfq"), "review_adjustment_mismatch"),
                             (dict(data_authenticity="synthetic"), "review_authenticity_unusable"),
                             (dict(data_authenticity=None), "review_authenticity_unusable"),
                             (dict(source="csv"), "review_authenticity_unusable")):
            value = original()
            value["company_research"]["trade_plans"][0].update(update)
            item = self.project(value)
            self.assertIsNotNone(item["trade_plan"])
            self.assertFalse(item["prices"]["enabled"])
            self.assertEqual(item["prices"]["code"], code)

    def test_v2_horizon_plan_preserves_standard_metadata_without_rebuilding(self):
        value = original()
        company = value["company_research"]
        plan = company["trade_plans"][0]
        horizon = company["decisions"][0]["horizon"]
        plan_id = TECHNICAL_ID + "|atr_reference_plan_v2:" + sha256(horizon.encode()).hexdigest()[:16] + ":conditional_entry"
        plan.update(version="trade-plan-v2", method="atr_reference_plan_v2", plan_id=plan_id, horizon=horizon,
                    horizon_profile="medium", atr_timeframe="daily", target_multiples=["1.5", "2.5"])
        company["decisions"][0]["plan_id"] = plan_id
        item = self.project(value)
        self.assertEqual(item["trade_plan"]["horizon_profile"], "medium")
        self.assertEqual(item["trade_plan"]["target_multiples"], ["1.5", "2.5"])
        self.assertTrue(item["prices"]["enabled"])

    def test_latest_price_evidence_can_prove_basis_without_a_plan(self):
        value = original()
        company = value["company_research"]
        company["trade_plans"] = []
        company["decisions"][0]["plan_id"] = None
        company["evidence"] = [price_evidence()]
        item = self.project(value)
        self.assertTrue(item["prices"]["enabled"])
        self.assertIsNone(item["trade_plan"])
        # The actual last bar may precede a weekend/holiday requested cutoff.
        self.assertNotEqual(item["daily_cutoff"], item["prices"]["source_date"])
        for mutation in (dict(date="2026-10-02"), dict(unit="CNY"), dict(dependencies=[])):
            changed = deepcopy(value)
            changed["company_research"]["evidence"][0].update(mutation)
            self.assertFalse(self.project(changed)["prices"]["enabled"])

    def test_plan_and_latest_close_conflict_is_not_hidden_by_plan_preference(self):
        value = original()
        evidence = price_evidence()
        value["company_research"]["evidence"] = [evidence]
        self.assertTrue(self.project(value)["prices"]["enabled"])
        evidence["calculation_basis"]["price_adjustment"] = "qfq"
        self.assertEqual(self.project(value)["prices"]["code"], "review_price_basis_conflict")

    def test_malformed_field_types_are_unrecorded_instead_of_crashing(self):
        for mutation in (dict(rating=[]), dict(code=[]), dict(status=[])):
            value = original()
            value["company_research"]["decisions"][0].update(mutation)
            self.assertEqual(self.project(value)["status"], "unrecorded")
        value = original()
        value["company_research"]["trade_plans"][0]["version"] = []
        self.assertFalse(self.project(value)["prices"]["enabled"])


class ReviewPaginationTests(unittest.TestCase):
    def setUp(self):
        self.store = SqliteConversationStore(":memory:", clock=lambda: UPDATED)
        self.addCleanup(self.store.close)
        self.conversation = self.store.create(LOCAL_USER, provider="deepseek", data_mode="tencent", title="test")

    def add(self, result, *, asked=ASKED, request="chat", conversation=None):
        self.store.append(LOCAL_USER, conversation or self.conversation, request_id=request, asked_at=asked,
                          question="原问题", result=result, digest="invalid digest never read")

    def query(self, **options):
        return list_reviews(self.store, LOCAL_USER, data_mode="tencent", instrument_id=STOCK, now=NOW, **options)

    def test_twenty_originals_empty_matching_page_and_last_scanned_cursor(self):
        self.add(original())
        for _ in range(20):
            self.add(original(instrument=OTHER))
        page = self.query()
        self.assertEqual((page["scanned"], page["items"], page["has_more"]), (20, [], True))
        second = self.query(cursor=page["next_cursor"])
        self.assertEqual((second["scanned"], len(second["items"]), second["has_more"]), (1, 1, False))
        self.assertEqual(second["items"][0]["sequence"], 1)

    def test_filter_changes_and_invalid_cursors_are_rejected(self):
        for _ in range(21):
            self.add(original())
        cursor = self.query()["next_cursor"]
        for options in (dict(cursor=cursor, from_date="2026-10-01"), dict(cursor="bad!"),
                        dict(from_date="2026-10-10", to_date="2026-10-01"), dict(from_date="not-a-date"),
                        dict(from_date="0001-01-01"), dict(to_date="9999-12-31")):
            with self.subTest(options=options), self.assertRaises(ReviewError):
                self.query(**options)

    def test_bad_last_scanned_result_counts_and_still_advances(self):
        for _ in range(21):
            self.add(original())
        with self.store._connection:
            self.store._connection.execute("UPDATE turns SET result = ? WHERE sequence = 2", (b"invalid",))
        page = self.query()
        self.assertEqual((page["scanned"], page["skipped_invalid"], len(page["items"])), (20, 1, 19))
        second = self.query(cursor=page["next_cursor"])
        self.assertEqual([item["sequence"] for item in second["items"]], [1])

    def test_shanghai_date_filter_and_readonly_detail_after_forty_turns(self):
        for _ in range(45):
            self.add(original())
        self.add(original(), asked=datetime(2026, 10, 1, 16, tzinfo=timezone.utc), request="midnight")
        page = self.query(from_date="2026-10-02", to_date="2026-10-02")
        self.assertEqual([item["request_id"] for item in page["items"]], ["midnight"])
        detail = load_review(self.store, LOCAL_USER, data_mode="tencent", conversation_id=self.conversation,
                             sequence=1, request_id="chat", instrument_id=STOCK, now=NOW)
        self.assertEqual(detail["question"], "原问题")
        self.assertEqual(detail["result"]["answer"], "当时原文10.00元。")

    def test_deleted_cursor_anchor_never_reappears_and_detail_is_invalidated(self):
        first_id = self.conversation
        for _ in range(20):
            self.add(original(), asked=ASKED + timedelta(hours=1))
        second_id = self.store.create(LOCAL_USER, provider="deepseek", data_mode="tencent", title="older")
        self.add(original(), conversation=second_id)
        page = self.query()
        self.store.delete(LOCAL_USER, first_id)
        next_page = self.query(cursor=page["next_cursor"])
        self.assertEqual([item["conversation_id"] for item in next_page["items"]], [second_id])
        with self.assertRaisesRegex(ReviewError, "review_not_found"):
            load_review(self.store, LOCAL_USER, data_mode="tencent", conversation_id=first_id,
                        sequence=1, request_id="chat", instrument_id=STOCK, now=NOW)

    def test_closed_and_disabled_are_distinct_from_empty(self):
        disabled = list_reviews(Mock(), LOCAL_USER, data_mode="synthetic", instrument_id=STOCK, now=NOW)
        self.assertFalse(disabled["enabled"])
        self.store.close()
        with self.assertRaisesRegex(ReviewError, "review_store_unavailable"):
            self.query()


if __name__ == "__main__":
    unittest.main()
