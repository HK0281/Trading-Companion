"""Artificial document contracts; no provider or model access."""

from dataclasses import replace
from datetime import datetime
import json
import unittest

from trading_companion.research_documents import (
    DocumentCandidate, DocumentError, DocumentExcerpt, MAX_NEWS_CHARACTERS, ResearchDocument,
    SHANGHAI, announcement_code, supported_news_url, trim_incomplete_tail,
)


NOW = "2026-11-12T12:00:00+08:00"


def news_candidate(**changes):
    values = dict(parent_id="news-title:artificial", instrument_id="000001.SZ", security_name="平安银行",
                  kind="news", source="sina_company_news", title="平安银行人工新闻标题",
                  url="https://finance.sina.com.cn/stock/2026-11-11/doc-test123.shtml",
                  published_date="2026-11-11", published_at="2026-11-11T10:15:00+08:00")
    return DocumentCandidate(**{**values, **changes})


def announcement_candidate(**changes):
    values = dict(parent_id="announcement-title:artificial", instrument_id="000001.SZ", security_name="平安银行",
                  kind="announcements", source="eastmoney_announcements", title="平安银行人工公告标题",
                  url="https://data.eastmoney.com/notices/detail/000001/AN202611110001234567.html",
                  published_date="2026-11-11", published_at=None)
    return DocumentCandidate(**{**values, **changes})


class ResearchDocumentTests(unittest.TestCase):
    def test_content_digest_and_identity_are_stable_across_retrievals_but_track_content_and_parent(self):
        document = ResearchDocument(news_candidate(), ("平安银行人工第一段。", "人工第二段。"), NOW, "sina-artibody-v3")
        later = replace(document, retrieved_at="2026-11-12T13:00:00+08:00")
        changed = replace(document, paragraphs=("平安银行人工第一段已改变。",))
        reparented = replace(document, candidate=replace(document.candidate, parent_id="news-title:other"))
        self.assertEqual((document.id, document.content_sha256), (later.id, later.content_sha256))
        self.assertNotEqual(document.content_sha256, changed.content_sha256)
        self.assertNotEqual(document.id, reparented.id)
        self.assertEqual(document.content_sha256, reparented.content_sha256)

    def test_excerpts_preserve_unicode_offsets_and_are_bounded_exact_text(self):
        paragraph = "平安银行😀人工第一段。" + "甲" * 490 + "。经营风险人工说明。"
        document = ResearchDocument(news_candidate(), (paragraph, "独立的经营风险说明。", "末段。"), NOW, "sina-artibody-v3")
        excerpts = document.excerpts("经营风险")
        self.assertEqual(len(excerpts), 2)
        self.assertIn("独立的经营风险说明。", [e.text for e in excerpts])
        for excerpt in excerpts:
            original = document.paragraphs[excerpt.paragraph_index - 1]
            self.assertEqual(excerpt.text, original[excerpt.start:excerpt.end])
            self.assertLessEqual(len(excerpt.text), 480)
            self.assertEqual(excerpt.document_id, document.id)
            self.assertEqual(excerpt.content_sha256, document.content_sha256)
            self.assertEqual(len(excerpt.sha256), 64)
        first = document.excerpts("完全无匹配的词", max_count=1, max_chars=20)[0]
        self.assertEqual(first.text, "平安银行😀人工第一段。")

    def test_long_sentences_are_skipped_instead_of_splitting_numbers_negations_or_conditions(self):
        long_sentence = "若条件不满足不得买入" + "甲" * 460 + "12345万元尚未到账。"
        self.assertGreater(len(long_sentence), 480)
        document = ResearchDocument(news_candidate(), (long_sentence + "平安银行完整短句。",), NOW, "sina-artibody-v3")
        excerpts = document.excerpts("12345万元")
        self.assertEqual([e.text for e in excerpts], ["平安银行完整短句。"])
        self.assertEqual(excerpts[0].start, len(long_sentence))
        only_long = replace(document, paragraphs=(long_sentence,))
        self.assertEqual(only_long.excerpts("条件"), ())

    def test_summary_excludes_body_and_does_not_expose_unselected_text(self):
        private = "正文全文不应进入摘要或默认对象日志"
        document = ResearchDocument(news_candidate(), (private,), NOW, "sina-artibody-v3")
        summary = document.public_summary()
        self.assertNotIn(private, json.dumps(summary, ensure_ascii=False))
        self.assertNotIn(private, repr(document))
        self.assertEqual(summary["status"], "complete")
        self.assertEqual(summary["paragraph_count"], 1)
        self.assertEqual(summary["character_count"], len(private))
        self.assertNotIn("raw_value", summary)

    def test_partial_coverage_is_strict_source_specific_and_changes_identity_even_for_the_same_text(self):
        document = ResearchDocument(news_candidate(), ("平安银行人工完整句。",), NOW, "sina-artibody-v3")
        partial = replace(document, complete=False, incomplete_reasons=('document_tables_omitted',), omitted_tables=1)
        self.assertNotEqual(document.id, partial.id)
        self.assertEqual(document.content_sha256, partial.content_sha256)
        self.assertEqual(partial.public_summary()['status'], 'partial')
        self.assertIs(partial.public_summary()['complete'], False)
        self.assertEqual(partial.public_summary()['code'], 'document_tables_omitted')
        for changes in (dict(complete=0), dict(omitted_tables=True), dict(page_count=2),
                        dict(private_use_characters_replaced=1), dict(omitted_trailing_characters=1),
                        dict(complete=False), dict(incomplete_reasons=[]), dict(omitted_tables=-1),
                        dict(omitted_tables=512001), dict(complete=False, omitted_tables=1,
                            incomplete_reasons=('document_first_page_only',)),
                        dict(complete=False, omitted_tables=1,
                            incomplete_reasons=('document_tables_omitted', 'document_tables_omitted'))):
            with self.subTest(changes=changes), self.assertRaises(DocumentError):
                replace(document, **changes)
        announcement = ResearchDocument(announcement_candidate(), ('人工公告完整句。',), NOW,
                                        'eastmoney-announcement-body-v2', private_use_characters_replaced=1)
        self.assertTrue(announcement.complete)
        partial = replace(announcement, complete=False, page_count=2,
                          incomplete_reasons=('document_first_page_only',), omitted_trailing_characters=5)
        self.assertEqual(partial.coverage_metadata()['page_count'], 2)
        with self.assertRaises(DocumentError):
            replace(partial, omitted_tables=1)

    def test_partial_tail_removes_the_entire_suffix_without_merging_or_splitting_numeric_conditions(self):
        cases = (
            (('平安银行完整句。', '金额为123.45万元，尚未满足条件'), ('平安银行完整句。',)),
            (('已完成。金额为123.45万元，尚未满足', '尾部表单'), ('已完成。',)),
            (('首句！', '说明「有效？」未完成'), ('首句！', '说明「有效？」')),
            (('完整句。',), ('完整句。',)),
            (('前项已完成。', '发行条件（规模123万元。尚未获批'), ('前项已完成。',)),
            (('前项已完成。', '公司表示“预计收入123万元。但该预计尚未获批'), ('前项已完成。',)),
            (('前项已完成。', '参见https://example.test/q?条件尚未'), ('前项已完成。',)),
            (('前项已完成。', '参见HTTPS://EXAMPLE.TEST/q?条件尚未'), ('前项已完成。',)),
            (('前项已完成。', "公司表示'预计收入123万元。但尚未获批"), ('前项已完成。',)),
            (("company's业务保持稳定。尚未完成",), ("company's业务保持稳定。",)),
            (('前项已完成。', "公司表示'company's预计收入123万元。但尚未获批"), ('前项已完成。',)),
            (('前项已完成。', "公司表示'company's预计收入123万元。但尚未获批'"), ('前项已完成。',)),
            (('前项已完成。', '公司表示‘company’s预计收入123万元。但尚未获批'), ('前项已完成。',)),
            (('前项已完成。', '发行条件（规模123万元。仍需审批）不完整'), ('前项已完成。',)),
            (('前项已完成。', '说明“金额123万元。”未完成'), ('前项已完成。', '说明“金额123万元。”')),
        )
        for paragraphs, expected in cases:
            with self.subTest(paragraphs=paragraphs):
                actual, count = trim_incomplete_tail(paragraphs)
                self.assertEqual(actual, expected)
                self.assertEqual(count, len('\n\n'.join(paragraphs)) - len('\n\n'.join(expected)))
        for paragraphs in (('123.45',), ('123.',), ('说明：',), ('第一项；',), ()):
            with self.subTest(paragraphs=paragraphs), self.assertRaises(DocumentError):
                trim_incomplete_tail(paragraphs)

    def test_source_allowlist_rejects_alternate_hosts_query_userinfo_and_wrong_stock(self):
        urls = ("http://finance.sina.com.cn/stock/doc-test.shtml",
                "https://finance.sina.com.cn.evil.invalid/stock/doc-test.shtml",
                "https://finance.sina.com.cn@evil.invalid/stock/doc-test.shtml",
                "https://finance.sina.com.cn/stock/doc-test.shtml?url=other",
                "https://finance.sina.com.cn/../doc-test.shtml",
                "https://cj.sina.cn/articles/view/123/abc")
        for url in urls:
            self.assertFalse(supported_news_url(url))
            with self.assertRaises(DocumentError):
                news_candidate(url=url)
        self.assertEqual(announcement_code(announcement_candidate().url, "000001.SZ"), "AN202611110001234567")
        self.assertIsNone(announcement_code(announcement_candidate().url, "600000.SH"))
        with self.assertRaises(DocumentError):
            announcement_candidate(url=announcement_candidate().url + "#attachment")

    def test_contract_rejects_unbound_candidate_and_invalid_timestamps(self):
        bad = (dict(instrument_id="000001.SH"), dict(parent_id=""), dict(security_name=" "),
               dict(source="eastmoney_news"), dict(kind="financial"), dict(published_date="2026-02-30"),
               dict(published_at="2026-11-11T10:15:00"), dict(published_at="2026-11-10T10:15:00+08:00"))
        for changes in bad:
            with self.subTest(changes=changes), self.assertRaises(DocumentError):
                news_candidate(**changes)
        with self.assertRaises(DocumentError):
            ResearchDocument(news_candidate(), ("人工正文",), "2026-11-11T10:00:00+08:00", "sina-artibody-v3")

    def test_document_character_paragraph_and_extractor_limits_are_not_silent_truncation(self):
        candidate = news_candidate()
        for paragraphs in (("甲" * (MAX_NEWS_CHARACTERS + 1),), ("人工段落",) * 129, ("段落\x00损坏",), (" leading",), []):
            with self.subTest(size=len(paragraphs)), self.assertRaises(DocumentError):
                ResearchDocument(candidate, paragraphs, NOW, "sina-artibody-v3")
        full = ResearchDocument(candidate, ("甲" * MAX_NEWS_CHARACTERS,), NOW, "sina-artibody-v3")
        self.assertEqual(len(full.text), MAX_NEWS_CHARACTERS)
        with self.assertRaises(DocumentError):
            replace(full, extractor_version="eastmoney-announcement-body-v2")

    def test_news_and_announcement_paragraph_limits_and_selected_excerpt_locations_follow_the_source(self):
        news = ResearchDocument(news_candidate(), ('人工完整句。',) * 128, NOW, 'sina-artibody-v3')
        self.assertEqual(len(news.paragraphs), 128)
        with self.assertRaises(DocumentError):
            replace(news, paragraphs=news.paragraphs + ('新闻多一段。',))
        announcement = ResearchDocument(announcement_candidate(), ('人工完整句。',) * 399 + ('最后一段独立期限。',),
                                        NOW, 'eastmoney-announcement-body-v2')
        chosen = announcement.excerpts('最后一段独立期限', max_count=1)
        self.assertEqual(len(chosen), 1)
        self.assertEqual((chosen[0].paragraph_index, chosen[0].text, chosen[0].kind),
                         (400, announcement.paragraphs[399], 'announcements'))
        self.assertNotIn('kind', chosen[0].to_dict(), 'Source remains on the owning document in public excerpt records')
        with self.assertRaises(DocumentError):
            replace(announcement, paragraphs=announcement.paragraphs + ('公告多一段。',))
        for index in (129, 400):
            with self.subTest(index=index):
                fragment = DocumentExcerpt(announcement.id, announcement.content_sha256, index, 0, 3, '原文。',
                                           kind='announcements')
                self.assertEqual(fragment.paragraph_index, index)
                with self.assertRaises(DocumentError):
                    replace(fragment, kind='news')
        for changes in (dict(paragraph_index=401), dict(paragraph_index=True), dict(kind='financial')):
            with self.subTest(changes=changes), self.assertRaises(DocumentError):
                replace(chosen[0], **changes)

    def test_optional_times_preserve_actual_publication_and_reject_future_or_backward_updates(self):
        document = ResearchDocument(news_candidate(), ("人工正文",), datetime(2026, 11, 12, 12, tzinfo=SHANGHAI),
                                    "sina-artibody-v3", "2026-11-11T10:15:09+08:00", "2026-11-11T11:00:00+08:00")
        self.assertEqual(document.published_at, "2026-11-11T10:15:09+08:00")
        for stamp in ("2026-11-11T09:00:00+08:00", "2026-11-13T09:00:00+08:00"):
            with self.assertRaises(DocumentError):
                replace(document, updated_at=stamp)

    def test_separate_page_metadata_time_is_bounded_without_relabeling_it_as_publication_or_update(self):
        document = ResearchDocument(news_candidate(), ("人工正文",), NOW, "sina-artibody-v3",
                                    published_at="2026-11-11T10:15:09+08:00",
                                    metadata_at="2026-11-11T10:20:00+08:00")
        self.assertEqual(document.published_at, "2026-11-11T10:15:09+08:00")
        self.assertIsNone(document.updated_at)
        self.assertEqual(document.metadata_at, "2026-11-11T10:20:00+08:00")
        for value in ("2026-11-13T00:00:00+08:00", "2026-11-11T10:00:00+08:00", "bad time"):
            with self.subTest(value=value), self.assertRaises(DocumentError):
                replace(document, metadata_at=value)

    def test_excerpt_limit_validation_and_invalid_location(self):
        document = ResearchDocument(news_candidate(), ("人工正文",), NOW, "sina-artibody-v3")
        for options in (dict(max_count=3), dict(max_count=0), dict(max_chars=481), dict(max_chars=True)):
            with self.assertRaises(DocumentError):
                document.excerpts("原文", **options)
        with self.assertRaises(DocumentError):
            DocumentExcerpt(document.id, document.content_sha256, 1, 0, 9, "原文")
        with self.assertRaises(ValueError):
            DocumentError("private-source-error-that-must-not-be-exported")


if __name__ == "__main__":
    unittest.main()
