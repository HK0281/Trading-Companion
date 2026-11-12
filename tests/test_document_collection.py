"""Document enrichment uses synthetic responses, private workers and deterministic clocks."""

from dataclasses import replace
from datetime import datetime, timedelta
import json
from threading import Lock
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import requests

from trading_companion.collection_execution import CollectionExecutor, CollectionScope, CollectionUnavailable
from trading_companion.document_collection import (
    DocumentCollector, DETAIL_ENDPOINT, decode_document_payload, document_trigger, documents_for_context,
)
from trading_companion.product_research import ProductCompany, ProductEvidence, ProductResearchBundle
from trading_companion.research_documents import SHANGHAI
from trading_companion.turn_control import TurnControl, TurnLimits, TurnStopped, bind_turn
from tests.test_company_announcement_body import detail_payload
from tests.test_company_news_body import article_html


NOW = datetime(2026, 11, 12, 12, tzinfo=SHANGHAI)
SECURITIES = (("000001.SZ", "平安银行"), ("600000.SH", "浦发银行"), ("600036.SH", "招商银行"))


class Clock:
    def __init__(self):
        self.value, self.lock = 0.0, Lock()

    def __call__(self):
        with self.lock:
            return self.value

    def advance(self, seconds):
        with self.lock:
            self.value += seconds


def title(security=SECURITIES[0], *, kind="news", serial=0, url=None):
    code, name = security
    text = name + "人工" + ("新闻" if kind == "news" else "公告") + "标题"
    url = url or (f"https://finance.sina.com.cn/stock/2026-11-11/doc-test{code[:6]}{serial}.shtml"
                  if kind == "news" else f"https://data.eastmoney.com/notices/detail/{code[:6]}/AN202611110001234567.html")
    return ProductEvidence(f"{kind}:{code}:{serial}", code, kind, "人工标题", text,
        "sina_company_news" if kind == "news" else "eastmoney_announcements", "2026-11-11", url,
        "title", "2026-11-11T10:15:00+08:00" if kind == "news" else None,
        "datetime" if kind == "news" else "date", raw_value=text, unit="text")


def bundle(*, securities=SECURITIES[:1], kinds=("news",), historical=False, duplicates=False):
    companies = []
    for security in securities:
        records = tuple(title(security, kind=kind, serial=serial)
                        for kind in kinds if kind in ("news", "announcements")
                        for serial in range(2 if duplicates and kind == "news" else 1))
        companies.append(ProductCompany(*security, records, ()))
    return ProductResearchBundle(SimpleNamespace(explicit_historical=historical, kinds=kinds), tuple(companies))


def response(content=None, *, status=200, media="text/html; charset=utf-8", declared=None, chunks=None):
    headers = {"Content-Type": media}
    if declared is not None:
        headers["Content-Length"] = declared
    result = Mock(status_code=status, headers=headers)
    if chunks is None:
        raw = (article_html() if content is None else content).encode("utf-8")
        chunks = [raw]
    result.iter_content.return_value = chunks
    return result


class DocumentCollectionTests(unittest.TestCase):
    def setUp(self):
        self.clock, self.session, self.traces = Clock(), Mock(), []
        self.executor = CollectionExecutor()
        self.addCleanup(lambda: self.executor.close(wait=True))
        self.collector = DocumentCollector(session=self.session, clock=self.clock, executor=self.executor,
                                           wall_clock=lambda: NOW)
        self.session.request.return_value = response()
        for guard in (patch("requests.request", side_effect=AssertionError("No external HTTP")),
                      patch("socket.socket.connect", side_effect=AssertionError("No external connections")),
                      patch("trading_companion.document_collection.trace_documents", side_effect=lambda fields: self.traces.append(dict(fields)))):
            guard.start()
            self.addCleanup(guard.stop)

    def enrich(self, selected=None, *, question="请读平安银行新闻原文", sorting=False):
        return self.collector.enrich(bundle() if selected is None else selected, question=question, now=NOW, sorting=sorting)

    def test_charsetless_response_uses_utf8_meta_without_corrupting_the_candidate_title(self):
        page = article_html().replace('<head>', '<head><meta charset="utf-8"/>')
        self.session.request.return_value = response(page, media='text/html')
        result = self.enrich()
        self.assertEqual(result.documents['items'][0]['status'], 'available')
        self.assertTrue(result.supplemental_evidence)
        self.assertIn('平安银行', result.supplemental_evidence[0].display)

    def test_header_charset_precedes_meta_and_explicit_decoding_never_uses_replacement_characters(self):
        page = '<meta charset="gbk"><p>人工中文正文。</p>'
        raw = page.encode('utf-8')
        self.assertEqual(decode_document_payload(raw, 'news', 'text/html; charset="UTF-8"'), page)
        for charset in ('utf-7', 'utf-16', 'iso-8859-1', '', 'unicode_escape'):
            with self.subTest(charset=charset), self.assertRaises(ValueError):
                decode_document_payload(raw, 'news', 'text/html; charset=' + charset)
        with self.assertRaises(UnicodeError):
            decode_document_payload(b'\x81', 'news', 'text/html; charset=utf-8')
        with self.assertRaises(ValueError):
            decode_document_payload(raw, 'news', 'text/html; charset=utf-8; charset=gbk')

    def test_meta_charset_supports_real_self_closing_and_http_equiv_forms(self):
        for charset in ('utf-8', 'gb2312', 'gbk', 'gb18030'):
            for meta in ('<meta charset="' + charset + '"/>',
                         '<META content="text/html; charset=' + charset + '" HTTP-EQUIV="Content-Type">'):
                with self.subTest(charset=charset, meta=meta):
                    page = '<html><head>' + meta + '</head><p>人工中文正文。</p></html>'
                    self.assertEqual(decode_document_payload(page.encode(charset), 'news', 'text/html'), page)
        for meta in ('<meta charset="utf-7">', '<meta charset="">',
                     '<meta charset="utf-8"><meta charset="gbk">', '<meta charset="utf-8" charset="gbk">'):
            with self.subTest(meta=meta), self.assertRaises(ValueError):
                decode_document_payload(meta.encode(), 'news', 'text/html')

    def test_undeclared_encoding_falls_back_strictly_and_ignores_commented_or_late_meta(self):
        page = '<!-- <meta charset="utf-7"> --><p>人工中文正文。</p>'
        for charset in ('utf-8', 'gb18030'):
            self.assertEqual(decode_document_payload(page.encode(charset), 'news', 'text/html'), page)
        page = ' ' * 2048 + '<meta charset="utf-7"><p>人工中文正文。</p>'
        self.assertEqual(decode_document_payload(page.encode(), 'news', 'text/html'), page)
        with self.assertRaises(UnicodeError):
            decode_document_payload(b'\x81', 'news', 'text/html')
        raw = b'<script>"<meta charset=\"utf-7\">"</script><p>text</p>'
        self.assertEqual(decode_document_payload(raw, 'news', 'text/html'), raw.decode())

    def test_invalid_charset_is_an_isolated_document_failure_and_still_counts_request(self):
        reply = response(media='text/html; charset=utf-16')
        self.session.request.return_value = reply
        selected = bundle()
        result = self.enrich(selected)
        self.assertEqual(result.evidence, selected.evidence)
        self.assertEqual(result.documents['items'][0]['code'], 'document_invalid_response')
        self.assertEqual(self.traces[-1]['requests'], 1)
        reply.close.assert_called_once()

    def test_json_decode_keeps_utf8_and_rejects_duplicate_keys_nonfinite_and_bad_media(self):
        self.assertEqual(decode_document_payload(b'\xef\xbb\xbf{"data":{}}', 'announcements', 'application/json'), {'data': {}})
        for raw in (b'{"a":1,"a":2}', b'{"a":NaN}'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                decode_document_payload(raw, 'announcements', 'application/json')
        for media in ('application/jsonp', 'text/html', 'application/json; charset=gbk'):
            with self.subTest(media=media), self.assertRaises(ValueError):
                decode_document_payload(b'{}', 'announcements', media)

    def test_event_trigger_and_non_event_historical_or_absent_domains_do_not_read(self):
        for selected, question, reason in (
            (bundle(), "分析一下平安银行", "documents_not_requested"),
            (bundle(), "不要读新闻正文", "documents_not_requested"),
            (bundle(historical=True), "读新闻原文", "documents_historical"),
            (bundle(kinds=("technical",)), "读新闻原文", "documents_not_requested")):
            with self.subTest(reason=reason):
                self.assertEqual(document_trigger(selected.plan, question), reason)
                self.assertIs(self.enrich(selected, question=question), selected)
                self.assertEqual(self.traces[-1]["documents_skipped_reason"], reason)
        self.session.request.assert_not_called()

    def test_unsupported_or_unverified_candidates_never_create_network_targets(self):
        selected = bundle()
        parent = selected.companies[0].evidence[0]
        for changed in (replace(parent, url="https://evil.invalid/doc-test.shtml"),
                        replace(parent, source="unverified_news"), replace(parent, data_authenticity="synthetic"),
                        replace(parent, content_kind="snippet"), replace(parent, date="2026-02-30")):
            altered = replace(selected, companies=(replace(selected.companies[0], evidence=(changed,)),))
            self.assertIs(self.enrich(altered), altered)
        self.session.request.assert_not_called()

    def test_one_article_per_company_and_original_evidence_ids_stay_in_the_same_global_order(self):
        selected = bundle(securities=SECURITIES, kinds=("news", "announcements"), duplicates=True)
        parents = [company.evidence[0] for company in selected.companies]
        replies = [response(article_html(title=parent.raw_value, paragraphs=(name + "人工新闻正文。",)))
                   for parent, (_, name) in zip(parents, SECURITIES)]
        self.session.request.side_effect = replies
        result = self.enrich(selected, question="读三家公司的新闻原文")
        self.assertEqual([call.args[1] for call in self.session.request.call_args_list], [p.url for p in parents])
        self.assertEqual(self.session.request.call_count, 3)
        self.assertEqual(result.documents["status"], "available")
        self.assertEqual([e.id for e in result.evidence[:len(selected.evidence)]], [e.id for e in selected.evidence])
        self.assertEqual([e.instrument_id for e in result.supplemental_evidence], [s[0] for s in SECURITIES])
        self.assertEqual(self.traces[-1]["requests"], 3)
        for reply in replies:
            reply.close.assert_called_once()

    def test_single_company_can_read_news_and_same_announcement_detail_without_following_attachment(self):
        selected = bundle(kinds=("news", "announcements"))
        replies = [response(), response(json.dumps(detail_payload(), ensure_ascii=False), media="application/json")]
        self.session.request.side_effect = replies
        result = self.enrich(selected, question="请读新闻与公告正文")
        self.assertEqual([item["kind"] for item in result.documents["items"]], ["news", "announcements"])
        second = self.session.request.call_args_list[1]
        self.assertEqual(second.args, ("GET", DETAIL_ENDPOINT))
        self.assertEqual(second.kwargs["params"], dict(art_code="AN202611110001234567", page_index=1, client_source="web"))
        self.assertEqual(self.session.request.call_count, 2)
        for call in self.session.request.call_args_list:
            self.assertEqual(call.kwargs["timeout"], (3.0, 4.0))
            self.assertFalse(call.kwargs["allow_redirects"])

    def test_partial_news_and_first_page_keep_evidence_flags_without_extra_requests(self):
        selected = bundle(kinds=('news', 'announcements'))
        page = article_html().replace('</div><!-- 正文 end -->',
            '<table><tr><td><p>未读取表格中数字987654.32</p></td></tr></table></div><!-- 正文 end -->')
        announcement = detail_payload(page_size=2, notice_content='平安银行完整公告句。金额为123.45万元，尚未满足')
        self.session.request.side_effect = [response(page), response(json.dumps(announcement), media='application/json')]
        result = self.enrich(selected, question='读新闻和公告正文')
        self.assertEqual(self.session.request.call_count, 2)
        self.assertEqual(result.documents['status'], 'partial')
        self.assertEqual([i['code'] for i in result.documents['items']],
                         ['document_tables_omitted', 'document_first_page_only'])
        self.assertTrue(all(i['status'] == 'available' and i['complete'] is False for i in result.documents['items']))
        self.assertTrue(result.supplemental_evidence)
        self.assertTrue(all(e.document['document_complete'] is False for e in result.supplemental_evidence))
        self.assertNotIn('987654.32', json.dumps(result.documents, ensure_ascii=False))
        self.assertNotIn('123.45', json.dumps(result.documents, ensure_ascii=False))
        self.assertEqual(self.traces[-1]['available'], 2)
        self.assertEqual((self.traces[-1]['complete'], self.traces[-1]['partial']), (0, 2))
        public = documents_for_context(result, {})
        self.assertTrue(all(i['complete'] is False and i['excerpts'] == [] for i in public['items']))
        self.assertEqual(result.evidence[:len(selected.evidence)], selected.evidence)

    def test_announcement_excerpt_after_paragraph_128_keeps_its_actual_location(self):
        selected = bundle(kinds=('announcements',))
        text = '\n'.join(['平安银行人工背景。'] * 399 + ['平安银行最后期限为2026年11月30日。'])
        self.session.request.return_value = response(json.dumps(detail_payload(notice_content=text)), media='application/json')
        result = self.enrich(selected, question='请读平安银行公告最后期限原文')
        self.assertEqual(result.documents['items'][0]['paragraph_count'], 400)
        self.assertEqual(result.documents['items'][0]['code'], 'document_read')
        fragment = next(e for e in result.supplemental_evidence if e.document['paragraph_index'] == 400)
        self.assertEqual(fragment.display, '平安银行最后期限为2026年11月30日。')
        self.assertIs(fragment.document['document_complete'], True)
        self.assertEqual(self.session.request.call_count, 1)

    def test_attachment_only_is_unavailable_and_keeps_title(self):
        selected = bundle(kinds=("announcements",))
        payload = detail_payload(notice_content="", attach_url="https://ignored.invalid/attachment.pdf")
        self.session.request.return_value = response(json.dumps(payload), media="application/json")
        result = self.enrich(selected, question="请读公告全文")
        self.assertEqual(result.documents["items"][0]["code"], "document_attachment_only")
        self.assertEqual(result.evidence, selected.evidence)
        self.assertEqual(result.supplemental_evidence, ())
        self.assertEqual(self.session.request.call_count, 1)

    def test_four_second_request_deadline_is_local_failure_with_title_and_response_closed(self):
        reply = response()
        def delayed(*args, **kwargs):
            self.clock.advance(4.01)
            return reply
        self.session.request.side_effect = delayed
        selected = bundle()
        result = self.enrich(selected)
        self.assertEqual(result.evidence, selected.evidence)
        self.assertEqual(result.documents["items"][0]["code"], "document_timeout")
        self.assertEqual(self.traces[-1]["documents_skipped_reason"], "documents_timeout")
        self.assertEqual(self.traces[-1]["requests"], 1)
        reply.close.assert_called_once()

    def test_twelve_second_stage_stops_subsequent_work_without_throwing_or_dropping_titles(self):
        reply = response()
        def stalled(*args, **kwargs):
            self.clock.advance(12.1)
            return reply
        self.session.request.side_effect = stalled
        selected = bundle(securities=SECURITIES)
        result = self.enrich(selected)
        self.assertEqual(result.evidence, selected.evidence)
        self.assertEqual(result.documents["status"], "unavailable")
        self.assertEqual(self.session.request.call_count, 1)
        self.assertEqual([item["code"] for item in result.documents["items"]], ["document_timeout"] * 3)
        self.assertEqual(self.traces[-1]["documents_skipped_reason"], "documents_timeout")
        reply.close.assert_called_once()

    def test_answer_time_reserve_and_sorting_reserve_use_same_turn_clock(self):
        selected = bundle()
        for elapsed, sorting in ((70, False), (65, True)):
            self.clock = Clock()
            self.collector.clock = self.clock
            control = TurnControl(TurnLimits(), clock=self.clock)
            control.start()
            self.clock.advance(elapsed)
            with bind_turn(control):
                self.assertIs(self.enrich(selected, sorting=sorting), selected)
            self.assertEqual(self.traces[-1]["documents_skipped_reason"], "documents_insufficient_time")
        self.session.request.assert_not_called()

    def test_partial_answer_reserve_tightens_request_timeout_and_returns_title_after_deadline(self):
        control = TurnControl(TurnLimits(), clock=self.clock)
        control.start()
        self.clock.advance(68)
        reply = response()
        def delayed(*args, **kwargs):
            self.assertEqual(kwargs["timeout"], (2.0, 2.0))
            self.clock.advance(2.01)
            return reply
        self.session.request.side_effect = delayed
        selected = bundle()
        with bind_turn(control):
            result = self.enrich(selected)
        self.assertEqual(result.evidence, selected.evidence)
        self.assertEqual(self.traces[-1]["documents_skipped_reason"], "documents_timeout")
        self.assertLessEqual(self.clock(), 71)
        reply.close.assert_called_once()

    def test_redirect_wrong_type_declared_and_actual_size_fail_once_and_close_response(self):
        cases = ((response(status=302), "document_redirect_blocked"),
                 (response(status=503), "document_http_error"),
                 (response(media="application/pdf"), "document_content_type_invalid"),
                 (response(media="application/x-text/html"), "document_content_type_invalid"),
                 (response(declared="512001"), "document_too_large"),
                 (response(chunks=[b"x" * 512000, b"x"]), "document_too_large"))
        for reply, code in cases:
            with self.subTest(code=code):
                self.session.request.reset_mock()
                self.session.request.return_value = reply
                selected = bundle()
                result = self.enrich(selected)
                self.assertEqual(result.documents["items"][0]["code"], code)
                self.assertEqual(result.evidence, selected.evidence)
                self.session.request.assert_called_once()
                reply.close.assert_called_once()
                self.assertEqual(self.traces[-1]["requests"], 1)

    def test_announcement_json_media_type_is_exact_and_never_accepts_jsonp_or_html_disguises(self):
        selected = bundle(kinds=("announcements",))
        for media in ("application/jsonp", "application/json-like", "text/jsonp"):
            self.session.request.reset_mock()
            reply = response(json.dumps(detail_payload()), media=media)
            self.session.request.return_value = reply
            result = self.enrich(selected, question="读公告正文")
            self.assertEqual(result.documents["items"][0]["code"], "document_content_type_invalid")
            self.assertEqual(result.evidence, selected.evidence)
            self.session.request.assert_called_once()
            reply.close.assert_called_once()

    def test_http_error_and_connection_failure_count_attempts_without_exporting_raw_error(self):
        self.session.request.side_effect = requests.Timeout("private connection text")
        selected = bundle()
        result = self.enrich(selected)
        self.assertEqual(result.documents["items"][0]["code"], "document_connection_failed")
        self.assertEqual(self.traces[-1]["requests"], 1)
        self.assertNotIn("private connection text", json.dumps(result.documents))
        self.assertEqual(result.evidence, selected.evidence)

    def test_request_and_byte_ledgers_stop_before_exceeding_their_allowance(self):
        from trading_companion.document_collection import _candidates
        candidate, _ = _candidates(bundle())[0]
        stage = CollectionScope(deadline=12, requests=1, response_bytes=10, clock=self.clock)
        scope = CollectionScope(deadline=4, requests=1, response_bytes=512000, clock=self.clock)
        reply = response(chunks=[b"12345678901"])
        self.session.request.return_value = reply
        with self.assertRaises(CollectionUnavailable) as caught:
            self.collector._read(candidate, stage, scope)
        self.assertEqual(caught.exception.code, "research_byte_budget_exhausted")
        self.assertEqual(stage.usage(), {"requests": 1, "response_bytes": 0})
        reply.close.assert_called_once()
        with self.assertRaises(CollectionUnavailable) as caught:
            self.collector._read(candidate, stage, scope)
        self.assertEqual(caught.exception.code, "research_request_budget_exhausted")
        self.assertEqual(self.session.request.call_count, 1)
        stage.close()
        scope.close()

    def test_wrong_article_company_and_announcement_identity_are_isolated(self):
        selected = bundle(kinds=("news", "announcements"))
        self.session.request.side_effect = [response(article_html(paragraphs=("完全无关公司的正文。",))),
            response(json.dumps(detail_payload(art_code="AN202611110009999999")), media="application/json")]
        result = self.enrich(selected, question="请读新闻和公告原文")
        self.assertEqual([item["code"] for item in result.documents["items"]],
                         ["document_instrument_mismatch", "document_identity_mismatch"])
        self.assertEqual(result.evidence, selected.evidence)

    def test_updates_after_research_cutoff_are_rejected_even_when_downloaded_later(self):
        selected = bundle()
        reply = response(article_html(extra_meta=(
            '<meta property="article:modified_time" content="2026-11-12T12:00:01+08:00">')))
        def downloaded_later(*args, **kwargs):
            self.clock.advance(2)
            return reply
        self.session.request.side_effect = downloaded_later
        self.collector.wall_clock = lambda: NOW + timedelta(seconds=2)
        result = self.enrich(selected)
        self.assertEqual(result.evidence, selected.evidence)
        self.assertEqual(result.documents['items'][0]['code'], 'document_time_mismatch')
        self.assertEqual(result.documents['status'], 'unavailable')
        self.assertEqual(self.traces[-1]['available'], 0)
        reply.close.assert_called_once()

    def test_publication_after_cutoff_is_rejected_and_exact_cutoff_update_is_accepted(self):
        selected = bundle()
        company = selected.companies[0]
        parent = replace(company.evidence[0], date='2026-11-12', published_at=NOW.isoformat())
        altered = replace(selected, companies=(replace(company, evidence=(parent,)),))
        self.collector.wall_clock = lambda: NOW + timedelta(seconds=2)
        self.session.request.return_value = response(article_html(
            published='2026-11-12T12:00:01+08:00', visible_date='2026年11月12日 12:00'))
        result = self.enrich(altered)
        self.assertEqual(result.documents['items'][0]['code'], 'document_time_mismatch')
        self.assertEqual(result.evidence, altered.evidence)
        self.session.request.return_value = response(article_html(extra_meta=(
            '<meta property="article:modified_time" content="2026-11-12T12:00:00+08:00">')))
        result = self.enrich(selected)
        self.assertEqual(result.documents['items'][0]['status'], 'available')

    def test_later_page_publication_metadata_must_not_bypass_the_research_cutoff(self):
        from trading_companion.company_news_body import parse_sina_article
        selected = bundle()
        self.collector.wall_clock = lambda: NOW + timedelta(seconds=2)
        for seconds, expected in ((1, 'document_time_mismatch'), (0, 'document_read')):
            def with_metadata(payload, candidate, *, retrieved_at):
                document = parse_sina_article(payload, candidate, retrieved_at=retrieved_at)
                return replace(document, metadata_at=(NOW + timedelta(seconds=seconds)).isoformat())
            with self.subTest(seconds=seconds), patch(
                    'trading_companion.document_collection.parse_sina_article', side_effect=with_metadata):
                result = self.enrich(selected)
            self.assertEqual(result.documents['items'][0]['code'], expected)
            if seconds:
                self.assertEqual(result.evidence, selected.evidence)

    def test_full_document_is_never_stored_in_raw_value_trace_or_public_document_result(self):
        private = "这段未被选中的长句" + "甲" * 600 + "12345万元。"
        self.session.request.return_value = response(article_html(paragraphs=("平安银行人工新闻正文。", private, "人工经营风险说明。")))
        result = self.enrich()
        self.assertTrue(result.supplemental_evidence)
        self.assertTrue(all(e.raw_value is None for e in result.supplemental_evidence))
        self.assertNotIn(private, json.dumps(result.documents, ensure_ascii=False))
        self.assertNotIn(private, json.dumps(self.traces, ensure_ascii=False))
        context = dict(evidence=[{"content_kind": "body_excerpt", "document": result.supplemental_evidence[0].document}])
        projected = documents_for_context(result, context)
        self.assertEqual(len(projected["items"][0]["excerpts"]), 1)
        self.assertEqual(len(result.documents["items"][0]["excerpts"]), 2)

    def test_real_turn_cancellation_is_not_swallowed_as_an_optional_document_failure(self):
        control = TurnControl(TurnLimits(), clock=self.clock)
        control.start()
        control.cancel()
        with bind_turn(control), self.assertRaises(TurnStopped):
            self.enrich()
        self.session.request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
