from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import hashlib
import importlib
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from trading_companion.daily_bar import DailyBar
from trading_companion.multi_analysis import analyze_indicators
from trading_companion.multi_contracts import IndicatorSpec, MultiIndicatorRequest
from trading_companion.product_plan import ProductPlan
from trading_companion.product_research import ProductCompany, ProductEvidence, ProductResearchBundle
from trading_companion.research_documents import DocumentCandidate, ResearchDocument
from trading_companion.risk_research import build_risk_scenario
from trading_companion.security_directory import Security
from trading_companion.snapshot_store import (
    MAX_SNAPSHOT_BYTES, ResearchSnapshotStore, SNAPSHOT_POLICY, SnapshotStoreError, VERSION,
)
from trading_companion.tool_model import ToolModelResponse


UTC = timezone.utc
OBSERVED = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
RETRIEVED = datetime(2026, 9, 26, 3, 0, tzinfo=UTC)
PRIVATE = "private-question-model-answer-never-save"
START, END = date(2026, 9, 21), date(2026, 9, 25)
DOCUMENT_EXCERPT = "平安银行披露业务进展，相关业务保持稳定。"
DOCUMENT_REMAINDER = "未供模型阅读的原文部分不得整篇归档。" * 100


class _MemorySource:
    source_name = "csv"
    adjustment = "unknown"
    is_synthetic = True

    def read_daily_bars(self):
        return [DailyBar("000001.SZ", START + timedelta(days=index), close, close, close, close, 100, 0)
                for index, close in enumerate((10, 11, 13, 12, 14))]


def make_bundle(*, technical=False, retrieved_at=RETRIEVED.isoformat(), marker="100亿元"):
    plan = ProductPlan(status="ready", securities=(Security("000001.SZ", "平安银行"),),
        kinds=("financial", "technical") if technical else ("financial",), as_of=END,
        start_date=START, end_date=END, source="csv" if technical else "tencent",
        user_question=PRIVATE, model_response=ToolModelResponse("complete", PRIVATE))
    items = (ProductEvidence("source-document-1", "000001.SZ", "financial", "营业收入", marker,
        "fixture-company-source", date="2026-08-25", published_at="2026-08-25T08:00:00+08:00",
        publication_precision="datetime", report_period="2026-06-30", retrieved_at=retrieved_at),)
    coverage = ({"kind": "financial", "status": "available", "code": "ok", "message": "资料可用",
                 "source": "fixture-company-source", "retrieved_at": retrieved_at},)
    result, risk = None, None
    if technical:
        request = MultiIndicatorRequest("000001.SZ", START, END,
            indicators=(IndicatorSpec("atr", window=2), IndicatorSpec("sma", window=3)),
            source="csv", request_id="snapshot-store-fixture")
        result = replace(analyze_indicators(_MemorySource(), request, allow_development_csv=True),
                         retrieved_at=RETRIEVED)
        risk = build_risk_scenario(result, expected_request=request)
        plan = replace(plan, indicators=request.indicators, indicators_explicit=True)
    return ProductResearchBundle(plan, (ProductCompany("000001.SZ", "平安银行", items, coverage, result, risk),))


def make_document_bundle(*, available=True, selected=True, retrieved_at=RETRIEVED.isoformat(), document_published_at=None,
                         complete=True, legacy=False, kind='news', paragraphs=None, excerpt_index=1):
    bundle = make_bundle()
    company = bundle.companies[0]
    parent = ProductEvidence("sina-title-1", company.instrument_id, "news", "news_title", "平安银行披露业务进展",
        "sina_company_news", date=END.isoformat(),
        url="https://finance.sina.com.cn/stock/doc-abcdef123.shtml", content_kind="title",
        publication_precision="date", retrieved_at=RETRIEVED.isoformat(), raw_value="平安银行披露业务进展", unit="text")
    if kind == 'announcements':
        parent = replace(parent, kind=kind, source='eastmoney_announcements',
                         url='https://data.eastmoney.com/notices/detail/000001/AN202609250001234567.html')
    reason = 'document_tables_omitted' if kind == 'news' else 'document_first_page_only'
    candidate = DocumentCandidate(parent.id, parent.instrument_id, company.name, parent.kind, parent.source,
                                  parent.raw_value, parent.url, parent.date)
    document = ResearchDocument(candidate, (DOCUMENT_EXCERPT + " 同段后续文字。", DOCUMENT_REMAINDER) if paragraphs is None else paragraphs,
                                retrieved_at, 'sina-artibody-v3' if kind == 'news' else 'eastmoney-announcement-body-v2',
                                published_at=document_published_at, complete=complete,
                                incomplete_reasons=() if complete else (reason,),
                                omitted_tables=1 if not complete and kind == 'news' else 0,
                                page_count=2 if not complete and kind == 'announcements' else 1)
    item = dict(instrument_id=parent.instrument_id, kind=parent.kind, parent_title_id=parent.id,
                title=parent.raw_value, url=parent.url, source=parent.source,
                status="available" if available else "unavailable",
                code=("document_read" if complete else reason) if available else "document_attachment_only")
    supplemental = ()
    if available:
        digest = hashlib.sha256(DOCUMENT_EXCERPT.encode("utf-8")).hexdigest()
        item.update(document_id=document.id, sha256=document.content_sha256,
                    extractor_version=document.extractor_version, extracted_characters=len(document.text),
                    paragraph_count=len(document.paragraphs),
                    excerpts=[dict(paragraph_index=excerpt_index, text=DOCUMENT_EXCERPT, sha256=digest)] if selected else [],
                    retrieved_at=document.retrieved_at, published_date=parent.date, publication_precision="date")
        metadata = dict(document_id=document.id, parent_title_id=parent.id, sha256=document.content_sha256,
                        paragraph_index=excerpt_index, excerpt_sha256=digest, extractor_version=document.extractor_version)
        if legacy:
            assert complete
            old_version = 'sina-artibody-v2' if kind == 'news' else 'eastmoney-announcement-body-v1'
            old_values = [candidate.instrument_id, candidate.parent_id, candidate.source, candidate.url,
                          candidate.title, candidate.published_date, document.published_at,
                          document.content_sha256, old_version]
            old_id = 'document:' + hashlib.sha256(json.dumps(old_values, separators=(',', ':')).encode('utf-8')).hexdigest()
            item.update(document_id=old_id, extractor_version=old_version)
            metadata.update(document_id=old_id, extractor_version=old_version)
        else:
            item.update(**document.coverage_metadata())
            metadata['document_complete'] = document.complete
        supplemental = (replace(parent, id=metadata['document_id'] + ":excerpt:" + str(excerpt_index), label="新闻正文片段", display=DOCUMENT_EXCERPT,
            content_kind="body_excerpt", dependencies=(parent.id,), raw_value=None,
            retrieved_at=document.retrieved_at, document=metadata),)
    return replace(bundle, plan=replace(bundle.plan, kinds=("financial", kind)),
        companies=(replace(company, evidence=(*company.evidence, parent)),), supplemental_evidence=supplemental,
        documents=dict(version="research-documents-v1", status=("available" if complete else "partial") if available else "unavailable",
                       code=("documents_read" if complete else "documents_partial") if available else "documents_unavailable", items=[item]))


class SnapshotStoreTests(unittest.TestCase):
    def test_indicator_scope_mismatch_cannot_be_archived(self):
        bundle = make_bundle(technical=True)
        altered = replace(bundle, plan=replace(bundle.plan, indicators=(IndicatorSpec("rsi", window=7),)))
        with self.assertRaises(SnapshotStoreError):
            self.store.save(altered)

    def setUp(self):
        for guard in (
            patch("socket.socket.connect", side_effect=AssertionError("No network in snapshot tests")),
            patch("trading_companion.local_env._read_local_values", side_effect=AssertionError("No .env reads")),
            patch("trading_companion.snapshot_store._utc_now", return_value=OBSERVED),
        ):
            guard.start()
            self.addCleanup(guard.stop)
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "research.sqlite3"
        self.bundle = make_bundle()
        self.store = ResearchSnapshotStore(self.path)
        self.addCleanup(self.store.close)

    def assert_error(self, code, function, *args, **kwargs):
        with self.assertRaises(SnapshotStoreError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), code)

    def update(self, snapshot_id, column, value):
        # Columns are fixed test literals, never external input.
        self.assertIn(column, ("payload", "digest", "schema_version", "observed_at", "source_latest_at", "unknown_count"))
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute(f"UPDATE research_snapshots SET {column} = ? WHERE snapshot_id = ?", (value, snapshot_id))

    def test_import_does_not_open_a_database(self):
        import trading_companion.snapshot_store as module
        spec = importlib.util.spec_from_file_location("_snapshot_import_without_io", module.__file__)
        with patch("sqlite3.connect", side_effect=AssertionError("Import must not open a database")):
            spec.loader.exec_module(importlib.util.module_from_spec(spec))

    def test_round_trip_keeps_technical_facts_dependencies_risk_and_synthetic_labels(self):
        bundle = make_bundle(technical=True)
        identifier = self.store.save(bundle)
        payload = self.store.load(identifier)
        self.assertEqual(len(identifier), 64)
        self.assertEqual(payload["version"], VERSION)
        self.assertEqual(payload["snapshot_policy"], SNAPSHOT_POLICY)
        self.assertFalse(payload["trading_enabled"])
        company = payload["companies"][0]
        self.assertEqual(company["instrument_id"], "000001.SZ")
        self.assertEqual(company["technical"], bundle.companies[0].technical.to_dict())
        self.assertEqual(len(company["technical"]["bars"]), 5)
        self.assertEqual(company["technical"]["data_authenticity"], "synthetic")
        self.assertTrue(company["technical"]["facts"])
        self.assertEqual(company["risk"], bundle.companies[0].risk.to_dict())
        self.assertEqual(len(company["risk"]["evidence_ids"]), 2)

    def test_multiple_companies_keep_original_scopes_and_evidence_order(self):
        original = self.bundle.companies[0]
        other = ProductCompany("600000.SH", "浦发银行",
            (replace(original.evidence[0], id="source-document-2", instrument_id="600000.SH", display="80亿元"),),
            original.coverage)
        plan = replace(self.bundle.plan, securities=(Security(original.instrument_id, original.name),
                                                    Security(other.instrument_id, other.name)))
        bundle = replace(self.bundle, plan=plan, companies=(original, other))
        payload = self.store.load(self.store.save(bundle))
        self.assertEqual([item["instrument_id"] for item in payload["companies"]], ["000001.SZ", "600000.SH"])
        self.assertEqual([item["evidence"][0]["display"] for item in payload["companies"]], ["100亿元", "80亿元"])
        self.assertEqual(self.store.list()[0]["evidence_count"], 2)
        duplicate_id = replace(bundle, companies=(original, replace(other,
            evidence=(replace(other.evidence[0], id=original.evidence[0].id),))))
        self.assert_error("invalid_snapshot_bundle", self.store.save, duplicate_id)

    def test_documents_off_keeps_legacy_payload_and_content_identifier(self):
        identifier = self.store.save(self.bundle)
        self.assertEqual(identifier, "f8b554abcd10bd1176dfada74e216df14ed4e9aa74b277262f05583d3a91a9f5")
        payload = self.store.load(identifier)
        self.assertNotIn("documents", payload)
        self.assertNotIn("supplemental_evidence", payload)

    def test_legacy_document_fields_and_identifier_round_trip_without_being_rewritten(self):
        bundle = make_document_bundle(legacy=True)
        identifier = self.store.save(bundle)
        payload = self.store.load(identifier)
        self.assertEqual(payload['documents'], bundle.documents)
        self.assertEqual(payload['supplemental_evidence'], [e.to_dict() for e in bundle.supplemental_evidence])
        self.assertNotIn('complete', payload['documents']['items'][0])
        self.assertNotIn('document_complete', payload['supplemental_evidence'][0]['document'])
        self.assertEqual(self.store.save(bundle), identifier)

    def test_partial_coverage_round_trips_even_when_no_excerpt_was_supplied(self):
        bundle = make_document_bundle(complete=False, selected=False)
        payload = self.store.load(self.store.save(bundle))
        self.assertEqual(payload['documents'], bundle.documents)
        self.assertEqual(payload['documents']['status'], 'partial')
        self.assertEqual(payload['documents']['items'][0]['excerpts'], [])
        self.assertIs(payload['supplemental_evidence'][0]['document']['document_complete'], False)

    def test_first_page_and_legacy_announcement_coverage_round_trip(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                bundle = make_document_bundle(kind='announcements', complete=legacy, legacy=legacy)
                payload = self.store.load(self.store.save(bundle))
                self.assertEqual(payload['documents'], bundle.documents)
                self.assertEqual(payload['supplemental_evidence'], [e.to_dict() for e in bundle.supplemental_evidence])
                if not legacy:
                    item = payload['documents']['items'][0]
                    self.assertEqual((item['code'], item['page_count']), ('document_first_page_only', 2))
                    self.assertIs(item['complete'], False)

    def test_announcement_paragraph_400_round_trips_with_matching_summary_and_candidates(self):
        for selected in (False, True):
            with self.subTest(selected=selected):
                bundle = make_document_bundle(kind='announcements', selected=selected,
                    paragraphs=('人工背景。',) * 399 + (DOCUMENT_EXCERPT,), excerpt_index=400)
                payload = self.store.load(self.store.save(bundle))
                self.assertEqual(payload['documents'], bundle.documents)
                self.assertEqual(payload['documents']['items'][0]['paragraph_count'], 400)
                self.assertEqual(payload['supplemental_evidence'][0]['document']['paragraph_index'], 400)
                evidence = bundle.supplemental_evidence[0]
                for count, index in ((401, 400), (400, 401), (128, 129)):
                    summary = deepcopy(bundle.documents)
                    summary['items'][0]['paragraph_count'] = count
                    summary['items'][0]['excerpts'] = [dict(paragraph_index=index, text=DOCUMENT_EXCERPT,
                        sha256=evidence.document['excerpt_sha256'])]
                    candidate = replace(evidence, document={**evidence.document, 'paragraph_index': index})
                    self.assert_error('invalid_snapshot_bundle', self.store.save,
                        replace(bundle, documents=summary, supplemental_evidence=(candidate,)))

    def test_partial_fields_must_be_complete_consistent_and_paired_with_all_candidates(self):
        bundle = make_document_bundle(complete=False)
        fields = ('complete', 'omitted_tables', 'page_count', 'private_use_characters_replaced', 'omitted_trailing_characters')
        for missing in fields:
            summary = deepcopy(bundle.documents)
            del summary['items'][0][missing]
            with self.subTest(missing=missing):
                self.assert_error('invalid_snapshot_bundle', self.store.save, replace(bundle, documents=summary))
        for changes in (dict(complete=0), dict(complete=True), dict(omitted_tables=True), dict(page_count=2),
                        dict(private_use_characters_replaced=1), dict(omitted_trailing_characters=16001),
                        dict(code='document_read'), dict(code='document_first_page_only'), dict(extra=0)):
            summary = deepcopy(bundle.documents)
            summary['items'][0].update(changes)
            with self.subTest(changes=changes):
                self.assert_error('invalid_snapshot_bundle', self.store.save, replace(bundle, documents=summary))
        evidence = bundle.supplemental_evidence[0]
        for value in (True, 0, None):
            metadata = {**evidence.document, 'document_complete': value}
            with self.subTest(value=value):
                self.assert_error('invalid_snapshot_bundle', self.store.save,
                    replace(bundle, supplemental_evidence=(replace(evidence, document=metadata),)))
        metadata = dict(evidence.document)
        del metadata['document_complete']
        self.assert_error('invalid_snapshot_bundle', self.store.save,
                          replace(bundle, supplemental_evidence=(replace(evidence, document=metadata),)))
        legacy = make_document_bundle(legacy=True)
        old = legacy.supplemental_evidence[0]
        self.assert_error('invalid_snapshot_bundle', self.store.save, replace(legacy,
            supplemental_evidence=(replace(old, document={**old.document, 'document_complete': True}),)))
        summary = deepcopy(bundle.documents)
        summary['status'] = 'available'
        self.assert_error('invalid_snapshot_bundle', self.store.save, replace(bundle, documents=summary))

    def test_new_extractor_cannot_downgrade_by_dropping_coverage_or_cross_sources_and_limits(self):
        bundle = make_document_bundle(complete=False)
        summary = deepcopy(bundle.documents)
        for key in ('complete', 'omitted_tables', 'page_count', 'private_use_characters_replaced', 'omitted_trailing_characters'):
            del summary['items'][0][key]
        summary['items'][0]['code'] = 'document_read'
        summary.update(status='available', code='documents_read')
        evidence = bundle.supplemental_evidence[0]
        metadata = dict(evidence.document)
        del metadata['document_complete']
        self.assert_error('invalid_snapshot_bundle', self.store.save,
            replace(bundle, documents=summary, supplemental_evidence=(replace(evidence, document=metadata),)))
        for value in ('eastmoney-announcement-body-v2', 'sina-artibody-v2', 'unknown'):
            summary = deepcopy(bundle.documents)
            summary['items'][0]['extractor_version'] = value
            changed = replace(evidence, document={**evidence.document, 'extractor_version': value})
            self.assert_error('invalid_snapshot_bundle', self.store.save,
                              replace(bundle, documents=summary, supplemental_evidence=(changed,)))
        for changes in (dict(extracted_characters=16001), dict(extracted_characters=16000, omitted_trailing_characters=1)):
            summary = deepcopy(bundle.documents)
            summary['items'][0].update(changes)
            self.assert_error('invalid_snapshot_bundle', self.store.save, replace(bundle, documents=summary))

    def test_document_excerpts_round_trip_without_archiving_full_text_or_moving_original_evidence(self):
        bundle = make_document_bundle(retrieved_at=(OBSERVED - timedelta(minutes=1)).isoformat())
        identifier = self.store.save(bundle)
        payload = self.store.load(identifier)
        self.assertEqual(payload["companies"][0]["evidence"],
                         [item.to_dict() for item in bundle.companies[0].evidence])
        self.assertEqual(payload["supplemental_evidence"], [item.to_dict() for item in bundle.supplemental_evidence])
        self.assertEqual(payload["documents"], bundle.documents)
        self.assertEqual(self.store.list()[0]["evidence_count"], len(bundle.evidence))
        self.assertEqual(payload["source_retrieval"]["latest_at"],
                         (OBSERVED - timedelta(minutes=1)).isoformat(timespec="microseconds"))
        self.assertNotIn(DOCUMENT_REMAINDER, json.dumps(payload, ensure_ascii=False))
        self.assertNotIn(DOCUMENT_REMAINDER.encode("utf-8"), self.path.read_bytes())
        self.assertNotIn(DOCUMENT_EXCERPT, json.dumps(self.store.list(), ensure_ascii=False))
        self.store.close()
        with ResearchSnapshotStore(self.path) as reopened:
            self.assertEqual(reopened.load(identifier)["supplemental_evidence"], payload["supplemental_evidence"])

    def test_failed_documents_and_skipped_stage_do_not_reject_valid_base_snapshot(self):
        failed = make_document_bundle(available=False)
        payload = self.store.load(self.store.save(failed))
        self.assertNotIn("supplemental_evidence", payload)
        self.assertEqual(payload["documents"], failed.documents)
        skipped = replace(self.bundle, documents=dict(version="research-documents-v1", status="skipped",
            code="document_not_requested", items=[]))
        payload = self.store.load(self.store.save(skipped))
        self.assertEqual(payload["documents"]["status"], "skipped")

    def test_unselected_bounded_excerpts_are_preserved_without_claiming_model_saw_them(self):
        bundle = make_document_bundle(selected=False)
        payload = self.store.load(self.store.save(bundle))
        self.assertEqual(payload["documents"]["items"][0]["excerpts"], [])
        self.assertEqual(payload["supplemental_evidence"][0]["display"], DOCUMENT_EXCERPT)
        self.assertEqual(self.store.list()[0]["evidence_count"], len(bundle.evidence))

    def test_opaque_document_id_survives_a_precise_article_time_not_in_date_only_public_metadata(self):
        bundle = make_document_bundle(document_published_at="2026-09-25T09:30:00+08:00")
        without_time = make_document_bundle()
        self.assertNotEqual(bundle.documents["items"][0]["document_id"],
                            without_time.documents["items"][0]["document_id"])
        payload = self.store.load(self.store.save(bundle))
        item = payload["documents"]["items"][0]
        excerpt = payload["supplemental_evidence"][0]
        self.assertEqual(item["publication_precision"], "date")
        self.assertNotIn("published_at", item)
        self.assertIsNone(excerpt["published_at"])
        self.assertEqual(excerpt["document"]["document_id"], item["document_id"])
        self.assertEqual(excerpt["document"]["sha256"], item["sha256"])

    def test_opaque_document_id_shape_uniqueness_and_per_excerpt_association_are_checked(self):
        bundle = make_document_bundle()
        for value in ("not-an-id", "document:" + "A" * 64, "document:" + "0" * 63, True, []):
            with self.subTest(value=value):
                summary = deepcopy(bundle.documents)
                summary["items"][0]["document_id"] = value
                self.assert_error("invalid_snapshot_bundle", self.store.save, replace(bundle, documents=summary))
        parent = bundle.companies[0].evidence[-1]
        other_parent = replace(parent, id=parent.id + ":second", url=parent.url.replace("abcdef123", "other123"))
        summary = deepcopy(bundle.documents)
        other_item = {**deepcopy(summary["items"][0]), "parent_title_id": other_parent.id, "url": other_parent.url}
        summary["items"].append(other_item)
        duplicate_id = replace(bundle, documents=summary,
            companies=(replace(bundle.companies[0], evidence=(*bundle.companies[0].evidence, other_parent)),))
        self.assert_error("invalid_snapshot_bundle", self.store.save, duplicate_id)
        evidence = bundle.supplemental_evidence[0]
        mismatched = replace(evidence, document={**evidence.document, "document_id": "document:" + "0" * 64})
        self.assert_error("invalid_snapshot_bundle", self.store.save,
                          replace(bundle, supplemental_evidence=(mismatched,)))
        self.assertEqual(self.store.list(), ())

    def test_three_documents_keep_six_bounded_candidates_when_only_a_subset_was_offered(self):
        from tests.test_document_answer import document_bundle
        bundle = document_bundle(count=3, excerpts_per_document=2)
        summary = deepcopy(bundle.documents)
        summary["items"][0]["excerpts"] = []
        summary["items"][1]["excerpts"] = summary["items"][1]["excerpts"][:1]
        bundle = replace(bundle, documents=summary)
        payload = self.store.load(self.store.save(bundle))
        self.assertEqual(len(payload["supplemental_evidence"]), 6)
        self.assertEqual([len(item["excerpts"]) for item in payload["documents"]["items"]], [0, 1, 2])
        self.assertTrue(all(len(item["display"]) <= 480 and item["raw_value"] is None
                            for item in payload["supplemental_evidence"]))
        self.assertEqual(self.store.list()[0]["evidence_count"], len(bundle.evidence))

    def test_two_distinct_excerpts_from_same_paragraph_keep_individual_hashes(self):
        bundle = make_document_bundle()
        first = bundle.supplemental_evidence[0]
        text = "同段后续文字。"
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        second = replace(first, id=first.id + ":second", display=text,
                         document={**first.document, "excerpt_sha256": digest})
        summary = deepcopy(bundle.documents)
        summary["items"][0]["excerpts"].append(dict(paragraph_index=1, text=text, sha256=digest))
        bundle = replace(bundle, supplemental_evidence=(first, second), documents=summary)
        payload = self.store.load(self.store.save(bundle))
        self.assertEqual([item["document"]["paragraph_index"] for item in payload["supplemental_evidence"]], [1, 1])
        self.assertEqual([item["display"] for item in payload["supplemental_evidence"]], [DOCUMENT_EXCERPT, text])

    def test_document_ids_and_parent_scope_prevent_cross_company_or_source_association(self):
        bundle = make_document_bundle()
        evidence = bundle.supplemental_evidence[0]
        other = ProductCompany("600000.SH", "浦发银行", (replace(bundle.companies[0].evidence[0],
            id="second-company-financial", instrument_id="600000.SH"),), bundle.companies[0].coverage)
        plan = replace(bundle.plan, securities=(*bundle.plan.securities, Security(other.instrument_id, other.name)))
        bundle = replace(bundle, plan=plan, companies=(*bundle.companies, other))
        payload = self.store.load(self.store.save(bundle))
        self.assertEqual([e["id"] for c in payload["companies"] for e in c["evidence"]],
                         [e.id for c in bundle.companies for e in c.evidence])
        variants = (
            replace(evidence, instrument_id=other.instrument_id),
            replace(evidence, source="eastmoney_announcements"),
            replace(evidence, id=bundle.companies[0].evidence[0].id),
            replace(evidence, dependencies=("missing-title",)),
            replace(evidence, document={**evidence.document, "parent_title_id": "second-company-financial"}),
            replace(evidence, document={**evidence.document, "sha256": "0" * 64}),
            replace(evidence, document={**evidence.document, "excerpt_sha256": "0" * 64}),
            replace(evidence, document={**evidence.document, "paragraph_index": True}),
        )
        for item in variants:
            with self.subTest(item=item):
                self.assert_error("invalid_snapshot_bundle", self.store.save,
                                  replace(bundle, supplemental_evidence=(item,)))

    def test_document_full_text_unknown_fields_and_excessive_excerpts_are_rejected_before_writes(self):
        bundle = make_document_bundle()
        evidence = bundle.supplemental_evidence[0]
        invalid_summary = []
        for changes in ({"full_text": DOCUMENT_REMAINDER}, {"extracted_characters": 32001},
                        {"paragraph_count": 129}, {"document_id": "document:" + "0" * 64}):
            summary = deepcopy(bundle.documents)
            summary["items"][0].update(changes)
            invalid_summary.append(replace(bundle, documents=summary))
        summary = deepcopy(bundle.documents)
        summary["items"] *= 4
        invalid_summary.append(replace(bundle, documents=summary))
        summary = deepcopy(bundle.documents)
        summary["items"][0]["excerpts"] *= 3
        invalid_summary.append(replace(bundle, documents=summary))
        for invalid in (*invalid_summary,
                        replace(bundle, supplemental_evidence=(replace(evidence, raw_value=DOCUMENT_REMAINDER),)),
                        replace(bundle, supplemental_evidence=(replace(evidence, display="x" * 481),)),
                        replace(bundle, supplemental_evidence=(replace(evidence,
                            document={**evidence.document, "full_text": DOCUMENT_REMAINDER}),)),
                        replace(bundle, supplemental_evidence=bundle.supplemental_evidence * 7),
                        replace(bundle, documents=None)):
            with self.subTest(invalid=invalid):
                self.assert_error("invalid_snapshot_bundle", self.store.save, invalid)
                self.assertEqual(self.store.list(), ())

    def test_document_time_mismatch_future_observation_and_missing_time_are_not_silently_archived(self):
        future = make_document_bundle(retrieved_at=(OBSERVED + timedelta(seconds=1)).isoformat())
        self.assert_error("snapshot_source_time_in_future", self.store.save, future)
        bundle = make_document_bundle()
        summary = deepcopy(bundle.documents)
        summary["items"][0]["published_date"] = (END + timedelta(days=1)).isoformat()
        self.assert_error("invalid_snapshot_bundle", self.store.save, replace(bundle, documents=summary))
        summary = deepcopy(bundle.documents)
        summary["items"][0]["retrieved_at"] = "2026-09-26T03:00:00"
        self.assert_error("invalid_snapshot_timestamp", self.store.save, replace(bundle, documents=summary))
        self.assertEqual(self.store.list(), ())

    def test_document_summary_without_excerpts_still_contributes_its_retrieval_boundary(self):
        bundle = make_document_bundle(selected=False, retrieved_at=(OBSERVED + timedelta(seconds=1)).isoformat())
        self.assert_error("snapshot_source_time_in_future", self.store.save,
                          replace(bundle, supplemental_evidence=()))

    def test_tampered_document_excerpt_is_detected_even_after_digest_and_content_id_are_recomputed(self):
        identifier = self.store.save(make_document_bundle())
        payload = self.store.load(identifier)
        payload["supplemental_evidence"][0]["display"] = "被篡改的片段"
        data = {key: value for key, value in payload.items()
                if key not in ("snapshot_id", "observed_at", "source_retrieval")}
        altered_id = hashlib.sha256(json.dumps(data, ensure_ascii=False, allow_nan=False,
            sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        payload["snapshot_id"] = altered_id
        raw = json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("UPDATE research_snapshots SET snapshot_id=?, digest=?, payload=? WHERE snapshot_id=?",
                               (altered_id, hashlib.sha256(raw).hexdigest(), raw, identifier))
        self.assert_error("snapshot_corrupt", self.store.load, altered_id)

    def test_question_and_model_response_are_not_written_to_payload_or_sqlite(self):
        payload = self.store.load(self.store.save(self.bundle))
        self.assertNotIn(PRIVATE, json.dumps(payload))
        self.assertNotIn(PRIVATE.encode(), self.path.read_bytes())
        self.assertNotIn("user_question", payload["plan"])
        self.assertNotIn("model_response", payload["plan"])
        self.assertNotIn("context", payload["plan"])

    def test_duplicate_save_is_idempotent_and_keeps_first_observation(self):
        first = self.store.save(self.bundle)
        with patch("trading_companion.snapshot_store._utc_now", return_value=OBSERVED + timedelta(days=1)):
            second = self.store.save(replace(self.bundle,
                plan=replace(self.bundle.plan, user_question="a different private question")))
        self.assertEqual(first, second)
        self.assertEqual(self.store.load(first)["observed_at"], OBSERVED.isoformat(timespec="microseconds"))
        self.assertEqual(len(self.store.list()), 1)

    def test_distinct_data_creates_distinct_identifier_and_survives_reopen(self):
        first, second = self.store.save(self.bundle), self.store.save(make_bundle(marker="101亿元"))
        self.assertNotEqual(first, second)
        self.store.close()
        with ResearchSnapshotStore(self.path) as reopened:
            self.assertEqual(reopened.load(first)["companies"][0]["evidence"][0]["display"], "100亿元")
            self.assertEqual(reopened.load(second)["companies"][0]["evidence"][0]["display"], "101亿元")

    def test_loaded_and_listed_objects_are_detached(self):
        identifier = self.store.save(self.bundle)
        payload = self.store.load(identifier)
        payload["companies"][0]["evidence"][0]["display"] = "tampered"
        metadata = self.store.list()[0]
        metadata["securities"].clear()
        self.assertEqual(self.store.load(identifier)["companies"][0]["evidence"][0]["display"], "100亿元")
        self.assertEqual(len(self.store.list()[0]["securities"]), 1)

    def test_known_at_uses_first_local_observation_instead_of_analysis_date(self):
        identifier = self.store.save(self.bundle)
        self.assert_error("snapshot_not_found", self.store.load, identifier,
                          known_at=OBSERVED - timedelta(microseconds=1))
        self.assertEqual(self.store.list(known_at=OBSERVED - timedelta(microseconds=1)), ())
        self.assertEqual(self.store.load(identifier, known_at=OBSERVED)["snapshot_id"], identifier)
        self.assertEqual(len(self.store.list(known_at=OBSERVED)), 1)

    def test_source_retrieval_timestamps_are_normalized_and_publication_is_separate(self):
        local = RETRIEVED.astimezone(timezone(timedelta(hours=8)))
        payload = self.store.load(self.store.save(make_bundle(retrieved_at=local.isoformat())))
        boundary = payload["source_retrieval"]
        self.assertEqual(boundary["latest_at"], RETRIEVED.isoformat(timespec="microseconds"))
        self.assertEqual(boundary["earliest_at"], boundary["latest_at"])
        self.assertEqual(boundary["unknown_count"], 0)
        self.assertEqual(boundary["sources"][0]["records"], 2)
        self.assertEqual(payload["companies"][0]["evidence"][0]["published_at"], "2026-08-25T08:00:00+08:00")
        self.assertEqual(len(self.store.list(known_at=OBSERVED.astimezone(timezone(timedelta(hours=8))))), 1)

    def test_missing_source_time_is_not_inferred_from_publication_and_excluded_from_known_at(self):
        identifier = self.store.save(make_bundle(retrieved_at=None))
        payload = self.store.load(identifier)
        self.assertEqual(payload["source_retrieval"]["unknown_count"], 2)
        self.assertIsNone(payload["source_retrieval"]["latest_at"])
        self.assertEqual(self.store.list(known_at=OBSERVED), ())
        self.assert_error("snapshot_not_found", self.store.load, identifier, known_at=OBSERVED)

    def test_naive_invalid_future_source_times_are_rejected_without_partial_writes(self):
        for value, code in (("not-a-time", "invalid_snapshot_timestamp"),
                            ("2026-09-26T03:00:00", "invalid_snapshot_timestamp"),
                            ((OBSERVED + timedelta(seconds=1)).isoformat(), "snapshot_source_time_in_future")):
            with self.subTest(value=value):
                self.assert_error(code, self.store.save, make_bundle(retrieved_at=value))
                self.assertEqual(self.store.list(), ())
        self.store.save(self.bundle)
        self.assertEqual(len(self.store.list()), 1)

    def test_known_at_only_accepts_aware_datetimes(self):
        identifier = self.store.save(self.bundle)
        for value in ("2026-10-25T12:00:00+00:00", date(2026, 10, 25), OBSERVED.replace(tzinfo=None), 0, True):
            with self.subTest(value=value):
                self.assert_error("invalid_known_at", self.store.load, identifier, known_at=value)
                self.assert_error("invalid_known_at", self.store.list, known_at=value)

    def test_list_is_bounded_ordered_metadata_without_document_text(self):
        first = self.store.save(self.bundle)
        with patch("trading_companion.snapshot_store._utc_now", return_value=OBSERVED + timedelta(seconds=1)):
            second = self.store.save(make_bundle(marker="101亿元"))
        metadata = self.store.list(limit=1)
        self.assertEqual([item["snapshot_id"] for item in metadata], [second])
        self.assertEqual([item["snapshot_id"] for item in self.store.list()], [second, first])
        self.assertEqual(metadata[0]["evidence_count"], 1)
        self.assertEqual(metadata[0]["technical_snapshots"], 0)
        self.assertNotIn("companies", metadata[0])
        self.assertNotIn("101亿元", json.dumps(metadata))

    def test_invalid_identifiers_and_limits_do_not_become_sql(self):
        self.store.save(self.bundle)
        for value in ("' OR 1=1 --", "../snapshot", "a" * 63, "A" * 64, None, 5, b"a" * 64):
            with self.subTest(value=value):
                self.assert_error("invalid_snapshot_id", self.store.load, value)
        self.assert_error("snapshot_not_found", self.store.load, "a" * 64)
        for value in (0, -1, 101, True, 1.0, "1; DROP TABLE research_snapshots", None):
            with self.subTest(limit=value):
                self.assert_error("invalid_snapshot_limit", self.store.list, value)
        self.assertEqual(len(self.store.list()), 1)

    def test_payload_and_metadata_corruption_are_detected_on_load(self):
        changes = (("payload", b"{}"), ("digest", "0" * 64), ("schema_version", 2),
                   ("observed_at", "2025-01-01T00:00:00+00:00"),
                   ("source_latest_at", None), ("unknown_count", 99))
        for column, value in changes:
            with self.subTest(column=column):
                path = self.path.parent / f"corrupt-{column}.sqlite3"
                with ResearchSnapshotStore(path) as store:
                    identifier = store.save(self.bundle)
                    with closing(sqlite3.connect(path)) as connection, connection:
                        connection.execute(f"UPDATE research_snapshots SET {column} = ? WHERE snapshot_id = ?",
                                           (value, identifier))
                    self.assert_error("snapshot_corrupt", store.load, identifier)

    def test_corrupted_existing_content_is_not_silently_replaced(self):
        identifier = self.store.save(self.bundle)
        self.update(identifier, "digest", "0" * 64)
        self.assert_error("snapshot_corrupt", self.store.save, self.bundle)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            self.assertEqual(connection.execute("SELECT digest FROM research_snapshots").fetchone()[0], "0" * 64)

    def test_recomputed_digest_cannot_hide_content_identity_or_envelope_version_changes(self):
        for field, value in (("version", "research-snapshot-v99"), ("trading_enabled", True),
                             ("snapshot_policy", "strict-pit"), ("snapshot_id", "b" * 64)):
            with self.subTest(field=field):
                identifier = self.store.save(make_bundle(marker=field))
                payload = self.store.load(identifier)
                payload[field] = value
                raw = json.dumps(payload, ensure_ascii=False).encode()
                self.update(identifier, "payload", raw)
                self.update(identifier, "digest", hashlib.sha256(raw).hexdigest())
                self.assert_error("snapshot_corrupt", self.store.load, identifier)

    def test_duplicate_json_keys_and_nan_are_corrupt_even_with_matching_digest(self):
        for raw in (b'{"version":"a","version":"b"}', b'{"value":NaN}'):
            with self.subTest(raw=raw):
                identifier = self.store.save(make_bundle(marker=str(raw)))
                self.update(identifier, "payload", raw)
                self.update(identifier, "digest", hashlib.sha256(raw).hexdigest())
                self.assert_error("snapshot_corrupt", self.store.load, identifier)

    def test_payload_limit_and_nonfinite_values_do_not_leave_rows(self):
        self.assert_error("snapshot_too_large", self.store.save, make_bundle(marker="x" * MAX_SNAPSHOT_BYTES))
        for value in (float("nan"), float("inf")):
            item = replace(self.bundle.companies[0].evidence[0], display=value)
            invalid = replace(self.bundle, companies=(replace(self.bundle.companies[0], evidence=(item,)),))
            self.assert_error("invalid_snapshot_payload", self.store.save, invalid)
        self.assertEqual(self.store.list(), ())

    def test_payload_larger_than_two_megabytes_is_supported_within_new_limit(self):
        marker = "资料" * 400_000
        identifier = self.store.save(make_bundle(marker=marker))
        self.assertEqual(self.store.load(identifier)["companies"][0]["evidence"][0]["display"], marker)

    def test_wrong_company_scope_duplicate_ids_and_untrusted_fields_are_rejected(self):
        company = self.bundle.companies[0]
        variants = (
            replace(self.bundle, plan=replace(self.bundle.plan, status="needs_clarification")),
            replace(self.bundle, plan=replace(self.bundle.plan, securities=(Security("600000.SH", "浦发银行"),))),
            replace(self.bundle, companies=()),
            replace(self.bundle, companies=(replace(company, instrument_id="600000.SH"),)),
            replace(self.bundle, companies=(replace(company, evidence=company.evidence * 2),)),
            replace(self.bundle, companies=(replace(company, evidence=(replace(company.evidence[0], instrument_id="600000.SH"),)),)),
            replace(self.bundle, companies=(replace(company, coverage=({"source": "fixture", "raw_question": PRIVATE},)),)),
            replace(self.bundle, companies=(replace(company, risk=object()),)),
        )
        for bundle in variants:
            with self.subTest(bundle=bundle):
                self.assert_error("invalid_snapshot_bundle", self.store.save, bundle)
        self.assertEqual(self.store.list(), ())

    def test_technical_snapshot_must_match_checked_plan_scope(self):
        bundle = make_bundle(technical=True)
        for changes in ({"source": "tencent"}, {"start_date": START - timedelta(days=1)},
                        {"end_date": END + timedelta(days=1)}):
            with self.subTest(changes=changes):
                invalid = replace(bundle, plan=replace(bundle.plan, **changes))
                self.assert_error("invalid_snapshot_bundle", self.store.save, invalid)
        company = bundle.companies[0]
        facts = tuple(replace(fact, value=999) if fact.field == "close_price" and fact.trade_date == END else fact
                      for fact in company.technical.facts)
        invalid = replace(bundle, companies=(replace(company, technical=replace(company.technical, facts=facts)),))
        self.assert_error("invalid_snapshot_bundle", self.store.save, invalid)

    def test_one_instance_serializes_multithreaded_saves_reads_and_lists(self):
        def work(index):
            identifier = self.store.save(make_bundle(marker=f"{index % 3}亿元"))
            self.assertEqual(self.store.load(identifier)["snapshot_id"], identifier)
            self.assertGreater(len(self.store.list()), 0)
            return identifier
        with ThreadPoolExecutor(max_workers=6) as pool:
            identifiers = tuple(pool.map(work, range(30)))
        self.assertEqual(len(set(identifiers)), 3)
        self.assertEqual(len(self.store.list()), 3)

    def test_two_connections_save_same_content_atomically(self):
        with ResearchSnapshotStore(self.path) as second:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(store.save, self.bundle) for store in (self.store, second)]
                identifiers = [future.result() for future in futures]
        self.assertEqual(len(set(identifiers)), 1)
        self.assertEqual(len(self.store.list()), 1)

    def test_concurrent_first_open_creates_one_schema_and_idempotent_record(self):
        path = self.path.parent / "concurrent-open.sqlite3"
        def work(_):
            with ResearchSnapshotStore(path, clock=lambda: OBSERVED) as store:
                return store.save(self.bundle)
        with ThreadPoolExecutor(max_workers=4) as pool:
            identifiers = tuple(pool.map(work, range(8)))
        self.assertEqual(len(set(identifiers)), 1)
        with ResearchSnapshotStore(path) as reopened:
            self.assertEqual(len(reopened.list()), 1)

    def test_injected_clock_sets_observation_and_invalid_clock_is_rejected(self):
        observed = OBSERVED + timedelta(days=1)
        with ResearchSnapshotStore(":memory:", clock=lambda: observed) as store:
            payload = store.load(store.save(self.bundle))
        self.assertEqual(payload["observed_at"], observed.isoformat(timespec="microseconds"))
        self.assert_error("invalid_snapshot_clock", ResearchSnapshotStore, ":memory:", clock=5)
        with ResearchSnapshotStore(":memory:", clock=lambda: observed.replace(tzinfo=None)) as store:
            self.assert_error("invalid_snapshot_timestamp", store.save, self.bundle)
            self.assertEqual(store.list(), ())

    def test_context_manager_and_closed_operations(self):
        with ResearchSnapshotStore(":memory:") as store:
            identifier = store.save(self.bundle)
        store.close()
        for function, args in ((store.save, (self.bundle,)), (store.load, (identifier,)), (store.list, ())):
            self.assert_error("snapshot_store_closed", function, *args)

    def test_unknown_database_version_tables_and_invalid_files_are_preserved(self):
        for kind in ("version", "table", "not-sqlite"):
            with self.subTest(kind=kind):
                path = self.path.parent / f"unknown-{kind}"
                if kind == "not-sqlite":
                    path.write_bytes(b"private-not-a-database")
                else:
                    with closing(sqlite3.connect(path)) as connection, connection:
                        connection.execute("PRAGMA user_version = 99" if kind == "version"
                                           else "CREATE TABLE unrelated (value TEXT)")
                before = path.read_bytes()
                expected = "snapshot_store_unavailable" if kind == "not-sqlite" else "snapshot_schema_mismatch"
                self.assert_error(expected, ResearchSnapshotStore, path)
                self.assertEqual(path.read_bytes(), before)

    def test_same_named_table_with_wrong_column_types_is_not_treated_as_our_schema(self):
        path = self.path.parent / "wrong-schema.sqlite3"
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("CREATE TABLE research_snapshots (snapshot_id INTEGER PRIMARY KEY, "
                "schema_version INTEGER NOT NULL, observed_at TEXT NOT NULL, source_latest_at TEXT, "
                "unknown_count INTEGER NOT NULL, digest TEXT NOT NULL, payload BLOB NOT NULL)")
            connection.execute("PRAGMA user_version = 1")
        before = path.read_bytes()
        self.assert_error("snapshot_schema_mismatch", ResearchSnapshotStore, path)
        self.assertEqual(path.read_bytes(), before)

    def test_missing_parent_and_invalid_path_return_fixed_errors(self):
        self.assert_error("snapshot_store_unavailable", ResearchSnapshotStore,
                          self.path.parent / "missing" / "store.sqlite3")
        for path in (None, "", " ", 1):
            self.assert_error("invalid_snapshot_path", ResearchSnapshotStore, path)


if __name__ == "__main__":
    unittest.main()
