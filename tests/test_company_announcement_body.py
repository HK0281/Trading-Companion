"""Observed detail field layout with short synthetic content; no live source text."""

from copy import deepcopy
from dataclasses import replace
import unittest
from unittest.mock import patch

from trading_companion.company_announcement_body import parse_eastmoney_announcement
from trading_companion.research_documents import DocumentError
from tests.test_research_documents import NOW, announcement_candidate


def detail_payload(**changes):
    data = dict(art_code="AN202611110001234567", notice_title="平安银行人工公告标题", notice_date="2026-11-11",
                security=[dict(stock="000001", market_uni="0", short_name="平安银行", short_name_ch="平安银行",
                               short_name_cht="平安銀行", short_name_en="PAB")], short_name="平安银行",
                notice_content="<p>平安银行人工公告正文。</p><p>人工条件尚未满足，金额为12345万元。</p>")
    return {"code": "1", "data": {**data, **changes}}


class EastmoneyAnnouncementBodyTests(unittest.TestCase):
    def setUp(self):
        self.candidate = announcement_candidate()
        guard = patch("requests.request", side_effect=AssertionError("No external HTTP"))
        guard.start()
        self.addCleanup(guard.stop)

    def parse(self, payload=None):
        return parse_eastmoney_announcement(detail_payload() if payload is None else payload,
                                            self.candidate, retrieved_at=NOW)

    def assert_code(self, code, payload):
        with self.assertRaises(DocumentError) as caught:
            self.parse(payload)
        self.assertEqual(caught.exception.code, code)

    def test_matching_detail_yields_body_and_plain_text_never_follows_embedded_links(self):
        document = self.parse()
        self.assertEqual(document.paragraphs, ("平安银行人工公告正文。", "人工条件尚未满足，金额为12345万元。"))
        self.assertIsNone(document.published_at)
        self.assertEqual(document.candidate.parent_id, self.candidate.parent_id)
        content = "平安银行人工公告正文。\n第二段<a href='https://ignored.invalid/file.pdf'>仅保留文字</a>。"
        document = self.parse(detail_payload(notice_content=content))
        self.assertIn("仅保留文字", document.text)
        self.assertNotIn("ignored.invalid", document.text)
        self.assertEqual(document.extractor_version, "eastmoney-announcement-body-v3")
        self.assertTrue(document.complete)
        self.assertEqual(document.incomplete_reasons, ())

    def test_plain_visual_lines_rejoin_a_synthetic_board_resolution_before_excerpt_selection(self):
        content = "\n".join((
            "证券代码：000001 证券简称：平安银行", "平安银行股份有限公司", "人工董事会会议决议公告", "",
            "本公司董事会及全体董事保证本公告内容不存在任何虚假记载、误导性",
            "陈述或者重大遗漏，并对其内容的真实性、准确性和完整性承担法律责任。", "",
            "平安银行第十届董事会人工会议通知于", "2026 年 11 月 1 日以电子邮件形式送达。本次董事会会议",
            "于 2026 年 11 月 3 日以通讯方式召开，应参加会议董事 7 人，实际",
            "收到 7 名董事的有效表决票。会议的召集和召开符合相关规定。", "",
            "经与会董事审议，表决通过了以下人工合资议案：", "",
            "关于公司与人工合作方共同设立合资公司的议案", "",
            "为加强人工技术研发能力并提升服务质量，同意公司与人工合作方共同设立合资公司，",
            "公司认缴出资 7,500 万元人民币，持股 25%；人工合作方认缴出",
            "资 22,500 万元人民币，持股 75%。董事会授权管理层办理相关手续。",
            "（同意 7 票，反对 0 票，弃权 0 票）", "", "特此公告。", "平安银行股份有限公司", "董事会", "2026 年 11 月 4 日"))
        document = self.parse(detail_payload(notice_content=content))
        self.assertEqual(document.paragraphs[:3], ("证券代码：000001 证券简称：平安银行", "平安银行股份有限公司", "人工董事会会议决议公告"))
        self.assertIn("误导性陈述或者重大遗漏", document.paragraphs[3])
        self.assertIn("通知于2026 年", document.paragraphs[4])
        self.assertIn("实际收到 7 名董事", document.paragraphs[4])
        self.assertIn("认缴出资 22,500 万元", document.paragraphs[7])
        self.assertEqual(document.paragraphs[-4:], ("特此公告。", "平安银行股份有限公司", "董事会", "2026 年 11 月 4 日"))
        fragments = document.excerpts("表决通过的议案中同意、反对和弃权各几票", max_count=2)
        self.assertTrue(any("表决通过" in fragment.text and "议案" in fragment.text for fragment in fragments))
        self.assertTrue(any("同意 7 票" in fragment.text for fragment in fragments))
        self.assertFalse(any(fragment.text in document.paragraphs[:4] for fragment in fragments))
        for fragment in fragments:
            self.assertIn(fragment.text, document.paragraphs[fragment.paragraph_index - 1])

    def test_plain_visual_boundaries_use_only_explicit_punctuation_sections_and_empty_lines(self):
        for ending in ("。", "！", "？", "；", "：", "。”", "；’", "。）", "！)", "？】", "。』"):
            with self.subTest(ending=ending):
                first = "人工已披露完整内容" + ending
                document = self.parse(detail_payload(notice_content=first + "\n后续人工正文。"))
                self.assertEqual(document.paragraphs, (first, "后续人工正文。"))
        for marker in ("一、", "（一）", "(1)", "1.", "1、", "1.1 ", "第十二条", "第1条", "附件", "附件一：", "正文："):
            with self.subTest(marker=marker):
                document = self.parse(detail_payload(notice_content="上一项人工标题\n" + marker + "下一项人工内容。"))
                self.assertEqual(document.paragraphs, ("上一项人工标题", marker + "下一项人工内容。"))
        document = self.parse(detail_payload(notice_content="连续视觉\n正文需要拼接\n\n空行之后另起\n完整一段。"))
        self.assertEqual(document.paragraphs, ("连续视觉正文需要拼接", "空行之后另起完整一段。"))

    def test_plain_join_preserves_ascii_word_and_number_boundaries_without_adding_chinese_spaces(self):
        content = "人工中文跨\n行不加空格。\nProject\nAlpha\nCo.\nLtd.\n1\n23\n45.\n67。"
        document = self.parse(detail_payload(notice_content=content))
        self.assertEqual(document.paragraphs, ("人工中文跨行不加空格。", "Project Alpha Co. Ltd. 1 23", "45. 67。"))
        self.assertNotIn("123", document.text)
        self.assertNotIn("45.67", document.text)
        self.assertEqual(self.parse(detail_payload(notice_content="收入1\n23万元。")).paragraphs, ("收入1 23万元。",))
        self.assertEqual(self.parse(detail_payload(notice_content="收入1\n.23万元。")).paragraphs, ("收入1 .23万元。",))
        self.assertEqual(self.parse(detail_payload(
            notice_content="披露新增资金为\n1.23亿元，后续安排仍按已披露条件执行。"
        )).paragraphs, ("披露新增资金为1.23亿元，后续安排仍按已披露条件执行。",))

    def test_html_line_and_block_extraction_stays_unchanged_when_version_advances(self):
        document = self.parse(detail_payload(notice_content="<p>人工视觉\n换行文本</p><p>仍为第二段</p>"))
        self.assertEqual(document.paragraphs, ("人工视觉 换行文本", "仍为第二段"))
        self.assertEqual(document.extractor_version, "eastmoney-announcement-body-v3")

    def test_plain_join_keeps_unicode_and_compatible_numeric_endpoints_separate(self):
        for left, right in (("１", "23万元。"), ("1", "２３万元。"), ("１", "２３万元。"),
                            ("¹", "23万元。"), ("①", "㉓万元。"), ("١", "٢٣万元。"),
                            ("１", "．２３万元。"), ("１．", "２３万元。"),
                            ("１", ".23万元。"), ("1", "．23万元。")):
            with self.subTest(left=left, right=right):
                document = self.parse(detail_payload(notice_content="人工金额" + left + "\n" + right))
                self.assertEqual(document.paragraphs, ("人工金额" + left + " " + right,))

    def test_id_security_exchange_and_supplied_company_name_bind_to_candidate(self):
        cases = (dict(art_code="AN202611110009999999"),
                 dict(security=[dict(stock="600000", market_uni="1", short_name="别的公司")]),
                 dict(security=[dict(stock="000001", market_uni="1", short_name="平安银行")]),
                 dict(security=[dict(stock="000001", market_uni="0", short_name="别的公司")]),
                 dict(security=[dict(stock="000001", market_uni="0", short_name="平安银行", short_name_ch="别的公司")]),
                 dict(security=[dict(stock="000001", market_uni="0")]),
                 dict(security=[dict(stock="000001", short_name="平安银行")]),
                 dict(security=[dict(stock="000001", market_uni=0, short_name="平安银行")]),
                 dict(security=[dict(stock=1, market_uni="0", short_name="平安银行")]),
                 dict(short_name="别的公司"), dict(short_name_ch="别的公司"),
                 dict(stock_code="600000"), dict(stock="600000"), dict(market_uni="1"),
                 dict(security=[]), dict(security=["000001"]), dict(security=None))
        for changes in cases:
            with self.subTest(changes=changes):
                self.assert_code("document_identity_mismatch", detail_payload(**changes))
        payload = detail_payload(stock_list=[dict(stock_code="000001", market_code="0", stock_name="平安银行")])
        self.assertEqual(self.parse(payload).candidate.instrument_id, "000001.SZ")
        payload = detail_payload(codes=[dict(stock_code="000001", market_code="0")])
        self.assertEqual(self.parse(payload).candidate.instrument_id, "000001.SZ")
        for changes in (dict(codes=[dict(stock_code="600000", market_code="1")]),
                        dict(stock_list=[dict(stock_code="000001", market_code="1")]),
                        dict(codes=[dict(stock_code="000001", market_code="0", short_name="别的公司")]),
                        dict(stock_list=[])):
            self.assert_code("document_identity_mismatch", detail_payload(**changes))
        del payload["data"]["security"]
        self.assert_code("document_identity_mismatch", payload)

    def test_real_security_names_use_directory_normalization_and_all_aliases_must_agree(self):
        self.candidate = replace(self.candidate, security_name="平 安银行Ａ")
        row = dict(stock="000001", market_uni="0", short_name="平　安 银行a", short_name_ch="平 安银行Ａ")
        payload = detail_payload(security=[row], short_name="平 安 银行Ａ")
        self.assertEqual(self.parse(payload).candidate, self.candidate)
        for changes in (dict(stock_code="600000"), dict(market_code="1"), dict(stock_name="别的公司")):
            self.assert_code("document_identity_mismatch", detail_payload(security=[{**row, **changes}],
                             short_name="平 安 银行Ａ"))

    def test_cross_market_aggregate_name_never_supplies_the_target_security_identity(self):
        target = dict(stock="000001", market_uni="0", short_name="平安银行", short_name_ch="平安银行")
        other = dict(stock="01234", market_uni="116", short_name="人工银行股份", short_name_ch="人工银行股份")
        payload = detail_payload(security=[other, target], short_name="人工银行股份")
        self.assertTrue(self.parse(payload).paragraphs)
        self.assert_code("document_identity_mismatch", detail_payload(security=[other], short_name="人工银行股份"))
        for wrong_target in ({**target, "short_name": "人工银行股份", "short_name_ch": "人工银行股份"},
                             {"stock": "000001", "market_uni": "0"},
                             {**target, "market_uni": "116"}):
            self.assert_code("document_identity_mismatch", detail_payload(security=[other, wrong_target], short_name="人工银行股份"))
        self.assert_code("document_identity_mismatch", detail_payload(security=[target, {**target, "market_uni": "1"}]))
        self.assert_code("document_identity_mismatch", detail_payload(security=[other, target],
                         short_name="人工银行股份", short_name_ch="平安银行"))
        unclassified = {**other, "market_uni": ""}
        self.assertTrue(self.parse(detail_payload(security=[unclassified, target], short_name="人工银行股份")).paragraphs)
        self.assertTrue(self.parse(detail_payload(security=[{**other, "short_name_ch": "历史名称"}, target],
                             short_name="人工银行股份")).paragraphs)
        for wrong_other in ({**other, "market_uni": None}, {**other, "stock": ""}):
            self.assert_code("document_identity_mismatch", detail_payload(security=[wrong_other, target],
                             short_name="人工银行股份"))

    def test_historical_non_target_names_can_differ_but_target_aliases_must_match(self):
        target = dict(stock="000001", market_uni="0", short_name="平安银行", short_name_ch="平安银行")
        historical = dict(stock="02913", market_uni="116", short_name="人工证券股权", short_name_ch="历史控股(旧)")
        for aggregate in ("平安银行", "人工证券股权", "历史控股(旧)"):
            with self.subTest(aggregate=aggregate):
                self.assertTrue(self.parse(detail_payload(security=[target, historical], short_name=aggregate)).paragraphs)
        for other in (dict(stock="02923", market_uni="116"),
                      dict(stock="02923", market_uni="116", short_name=None, short_name_ch=[])):
            with self.subTest(other=other):
                self.assertTrue(self.parse(detail_payload(security=[other, target])).paragraphs)
        self.assert_code("document_identity_mismatch", detail_payload(security=[historical], short_name="人工证券股权"))
        self.assert_code("document_identity_mismatch", detail_payload(
            security=[historical, {**target, "short_name_ch": "历史控股(旧)"}], short_name="历史控股(旧)"))
        self.assert_code("document_identity_mismatch", detail_payload(security=[historical, target], short_name="未出现的名称"))
        for changes in (dict(stock="1234"), dict(stock="1234567"), dict(market_uni="HK"), dict(market_uni=116)):
            self.assert_code("document_identity_mismatch", detail_payload(security=[{**historical, **changes}, target]))

    def test_title_and_all_available_dates_must_match(self):
        self.assert_code("document_title_mismatch", detail_payload(notice_title="其他公告"))
        self.assert_code("document_title_mismatch", detail_payload(title="另一份公告"))
        self.assert_code("document_time_mismatch", detail_payload(notice_date="2026-11-10"))
        self.assert_code("document_time_mismatch", detail_payload(display_time="2026-11-10 12:00:00"))
        self.assert_code("document_time_unavailable", detail_payload(notice_date="2026-02-30"))
        self.assert_code("document_time_unavailable", detail_payload(notice_date="20261111"))
        payload = detail_payload()
        del payload["data"]["notice_date"]
        self.assert_code("document_time_unavailable", payload)

    def test_date_only_does_not_invent_time_and_exact_display_time_matches_list(self):
        self.assertIsNone(self.parse(detail_payload(notice_date="2026-11-11 00:00:00")).published_at)
        self.candidate = replace(self.candidate, published_at="2026-11-11T12:05:00+08:00")
        document = self.parse(detail_payload(display_time="2026-11-11 12:05:08"))
        self.assertEqual(document.published_at, self.candidate.published_at)
        self.assert_code("document_time_mismatch", detail_payload(display_time="2026-11-11 13:05:08"))

    def test_notice_day_rolls_only_after_parent_1500_and_never_supplies_publication(self):
        for clock in ("00:00:00", "12:05:00", "14:59:59", "15:00:00", "15:32:00", "16:00:00", "23:59:59"):
            self.candidate = replace(self.candidate, published_at="2026-11-11T" + clock + "+08:00")
            self.assertEqual(self.parse(detail_payload(notice_date="2026-11-11 09:00:00")).published_at,
                             self.candidate.published_at)
            if clock >= "15:00:00":
                self.assertEqual(self.parse(detail_payload(notice_date="2026-11-12 00:00:00")).published_at,
                                 self.candidate.published_at)
            else:
                self.assert_code("document_time_mismatch", detail_payload(notice_date="2026-11-12"))
            for day in ("2026-11-10", "2026-11-13"):
                self.assert_code("document_time_mismatch", detail_payload(notice_date=day))
        self.candidate = replace(self.candidate, published_at=None)
        self.assertIsNone(self.parse(detail_payload(notice_date="2026-11-11 16:30:00")).published_at)
        self.assert_code("document_time_mismatch", detail_payload(notice_date="2026-11-12",
                         eitime="2026-11-11 17:00:00"))

    def test_notice_rollover_threshold_is_always_shanghai_time(self):
        for parent, allowed in (("2026-11-11T06:59:59Z", False), ("2026-11-11T07:00:00Z", True),
                                ("2026-11-10T23:32:00-08:00", True), ("2026-11-11T16:59:59+10:00", False)):
            self.candidate = replace(self.candidate, published_at=parent)
            with self.subTest(parent=parent, allowed=allowed):
                payload = detail_payload(notice_date="2026-11-12", display_time=self.candidate.published_at)
                if allowed:
                    self.assertEqual(self.parse(payload).published_at, self.candidate.published_at)
                else:
                    self.assert_code("document_time_mismatch", payload)

    def test_notice_day_rollover_uses_shanghai_natural_days_including_weekends_and_years(self):
        for parent, notice in (("2026-11-06T16:00:00+08:00", "2026-11-07"),
                               ("2026-01-31T16:30:00+08:00", "2026-02-01"),
                               ("2025-12-31T08:00:00Z", "2026-01-01")):
            day = parent[:10]
            self.candidate = replace(self.candidate, published_date=day, published_at=parent)
            self.assertEqual(self.parse(detail_payload(notice_date=notice)).published_at, self.candidate.published_at)
        self.candidate = replace(self.candidate, published_date="2026-11-06", published_at="2026-11-06T16:00:00+08:00")
        self.assert_code("document_time_mismatch", detail_payload(notice_date="2026-11-09"))

    def test_display_time_is_only_a_matching_parent_minute_guard(self):
        for display in ("2026-11-11", None, "bad time", "2026-02-30 16:00:00"):
            self.assert_code("document_time_unavailable", detail_payload(display_time=display))
        self.assert_code("document_time_unavailable", detail_payload(display_time="2026-11-11 16:00:00"))
        self.candidate = replace(self.candidate, published_at="2026-11-11T16:00:42+08:00")
        self.assertEqual(self.parse(detail_payload(notice_date="2026-11-12", display_time="2026-11-11T08:00:01Z")).published_at,
                         self.candidate.published_at)
        self.assert_code("document_time_mismatch", detail_payload(notice_date="2026-11-12",
                         display_time="2026-11-12 16:00:42"))
        payload = detail_payload(display_time="2026-11-11 16:00:42")
        del payload["data"]["notice_date"]
        self.assert_code("document_time_unavailable", payload)

    def test_real_zero_notice_date_keeps_list_precision_and_never_uses_ingestion_time(self):
        payload = detail_payload(notice_date="2026-11-11 00:00:00", eitime="2026-11-12 01:02:03")
        self.assertIsNone(self.parse(payload).published_at)
        self.candidate = replace(self.candidate, published_at="2026-11-11T12:05:00+08:00")
        self.assertEqual(self.parse(payload).published_at, self.candidate.published_at)
        self.assert_code("document_time_mismatch", detail_payload(notice_date="2026-11-12 00:00:00",
                         eitime="2026-11-11 12:05:00"))

    def test_real_detail_layout_accepts_short_substantive_body_even_with_a_pdf_attachment(self):
        short = "本月未举行机构调研。公司按已披露信息回复了投资者问题。"
        payload = detail_payload(notice_content=short + "\n", notice_date="2026-11-11 00:00:00",
            eitime="2026-11-11 16:32:12", page_size=1, page_size_ch=0, page_size_cht=0, page_size_en=0,
            attach_list=[dict(attach_size=20, attach_type="0", attach_url="https://ignored.invalid/file.pdf", seq=1)],
            attach_list_ch=[dict(attach_type="0", attach_url="https://ignored.invalid/file.pdf", seq=1)],
            attach_list_en=[], attach_url="https://ignored.invalid/file.pdf", short_name="平安银行")
        self.assertEqual(self.parse(payload).paragraphs, (short,))
        payload["data"]["notice_content"] = "本公告具体内容详见附件。"
        self.assert_code("document_attachment_only", payload)

    def test_multiple_content_pages_are_incomplete_and_bad_page_counts_are_rejected(self):
        for pages in (2, 3, 100):
            document = self.parse(detail_payload(page_size=pages))
            self.assertFalse(document.complete)
            self.assertEqual(document.page_count, pages)
            self.assertEqual(document.incomplete_reasons, ("document_first_page_only",))
            self.assertEqual(document.omitted_trailing_characters, 0)
        for pages in (0, -1, True, False, "1", "2", 1.0, None, [], {}):
            self.assert_code("document_layout_invalid", detail_payload(page_size=pages))
        self.assertTrue(self.parse(detail_payload(page_size=1)).paragraphs)
        self.assert_code("document_time_mismatch", detail_payload(page_size=2, notice_date="2026-11-12 00:00:00"))

    def test_first_page_drops_entire_incomplete_suffix_before_excerpt_selection(self):
        content = "已完成第一项议案。\n预计收入123.45万元，尚未满足\n后续批准条件"
        document = self.parse(detail_payload(page_size=2, notice_content=content))
        self.assertEqual(document.paragraphs, ("已完成第一项议案。",))
        self.assertNotIn("123.45", document.text)
        self.assertEqual(document.omitted_trailing_characters, len("\n\n预计收入123.45万元，尚未满足后续批准条件"))
        for excerpt in document.excerpts("123.45万元"):
            self.assertIn(excerpt.text, document.paragraphs[excerpt.paragraph_index - 1])
            self.assertNotIn("123.45", excerpt.text)
        self.assertEqual(self.parse(detail_payload(notice_content=content)).paragraphs[-1], "预计收入123.45万元，尚未满足后续批准条件")
        for content in ("预计收入123.45万元", "预计收入123.", "预计收入123万元；尚未批准", "人工章节：未完成"):
            self.assert_code("document_empty_body", detail_payload(page_size=2, notice_content=content))
        document = self.parse(detail_payload(page_size=2, notice_content='已披露“条件尚未满足。”\n附件清单未完'))
        self.assertEqual(document.paragraphs, ('已披露“条件尚未满足。”',))
        document = self.parse(detail_payload(page_size=2, notice_content="已披露完整句。\n金额1\uf05223万元，尚未完成"))
        self.assertEqual(document.paragraphs, ("已披露完整句。",))
        self.assertEqual(document.private_use_characters_replaced, 1)

    def test_partial_trimming_never_bypasses_pretrim_limits(self):
        for pages in (1, 2):
            for content in ("完整句。" + "甲" * 32000, "\n".join(["完整句。"] * 401),
                            "\n".join(["未完的人工视觉行"] * 401), "\n".join(["甲" * 79] * 400)):
                with self.subTest(pages=pages, size=len(content)):
                    self.assert_code("document_too_large", detail_payload(page_size=pages, notice_content=content))

    def test_announcement_paragraph_limit_is_400_before_partial_trimming(self):
        for pages in (1, 2):
            for html in (False, True):
                for count in (128, 129, 400):
                    with self.subTest(pages=pages, html=html, count=count):
                        paragraphs = ["人工公告段落。"] * (count - 1) + ["末段特定问题已由公司董事会正式确认，相关安排将按已经披露的具体条件执行。"]
                        content = "".join("<p>" + paragraph + "</p>" for paragraph in paragraphs) if html else "\n".join(paragraphs)
                        document = self.parse(detail_payload(page_size=pages, notice_content=content))
                        self.assertEqual(len(document.paragraphs), count)
                        self.assertEqual(document.excerpts("末段特定问题", max_count=1)[0].paragraph_index, count)
                paragraphs = ["人工公告段落。"] * 400 + ["不完整尾段"]
                content = "".join("<p>" + paragraph + "</p>" for paragraph in paragraphs) if html else "\n".join(paragraphs)
                self.assert_code("document_too_large", detail_payload(page_size=pages, notice_content=content))

    def test_first_page_cannot_leave_only_a_title_after_trimming(self):
        self.candidate = replace(self.candidate, title=self.candidate.title + "。")
        title = self.candidate.title
        for attachment in (False, True):
            with self.subTest(attachment=attachment):
                payload = detail_payload(page_size=2, notice_title=title,
                    notice_content="<p>" + title + "</p><p>未完成的实质正文</p>")
                if attachment:
                    payload["data"]["attach_url"] = "https://ignored.invalid/file.pdf"
                self.assert_code("document_attachment_only" if attachment else "document_empty_body", payload)
        document = self.parse(detail_payload(page_size=2, notice_title=title,
            notice_content="<h1>" + title + "</h1><p>平安银行已披露实质正文。</p><p>未完成的后续条件</p>"))
        self.assertEqual(document.paragraphs, (title, "平安银行已披露实质正文。"))
        self.assertFalse(document.complete)

    def test_first_page_cannot_leave_only_a_summary_after_trimming(self):
        for content in ("摘要：人工摘要。\n正文：未完成的实质正文",
                        "<h1>" + self.candidate.title + "</h1><h2>内容摘要</h2><p>人工摘要。</p><h2>正文</h2><p>未完成的实质正文</p>"):
            for attachment in (False, True):
                with self.subTest(content=content, attachment=attachment):
                    payload = detail_payload(page_size=2, notice_content=content)
                    if attachment:
                        payload["data"]["attach_url"] = "https://ignored.invalid/file.pdf"
                    self.assert_code("document_attachment_only" if attachment else "document_empty_body", payload)
        document = self.parse(detail_payload(page_size=2,
            notice_content="摘要：人工摘要。\n正文：平安银行已披露实质正文。\n未完成的后续条件"))
        self.assertEqual(document.paragraphs, ("摘要：人工摘要。", "正文：平安银行已披露实质正文。"))

    def test_first_page_cannot_leave_only_an_attachment_prompt_after_trimming(self):
        payload = detail_payload(page_size=2, attach_url="https://ignored.invalid/file.pdf",
            notice_content="本公告具体内容详见附件。\n" + "未完成的尾段" * 40)
        self.assert_code("document_attachment_only", payload)

    def test_private_use_glyphs_replace_with_spaces_in_visible_body_only(self):
        document = self.parse(detail_payload(notice_content="人工项目\ue000特定调研。收入1\uf05223万元，条件\uf8ff尚未满足。"))
        self.assertEqual(document.paragraphs, ("人工项目 特定调研。收入1 23万元，条件 尚未满足。",))
        self.assertEqual(document.private_use_characters_replaced, 3)
        self.assertNotIn("123万元", document.text)
        self.assertTrue(document.complete)
        content = ("<div data-note='\ue000'><p>金额1&#xF052;23万元，条件\uf8ff尚未满足。</p>"
                   "<!--\ue000--><script>\ue000</script><p hidden>\ue000</p></div>")
        document = self.parse(detail_payload(notice_content=content))
        self.assertEqual(document.paragraphs, ("金额1 23万元，条件 尚未满足。",))
        self.assertEqual(document.private_use_characters_replaced, 2)
        self.assertEqual(self.parse(detail_payload(notice_content="人工正文\uf900。")).private_use_characters_replaced, 0)
        self.assert_code("document_title_mismatch", detail_payload(notice_title=self.candidate.title + "\ue000"))
        payload = detail_payload()
        payload["data"]["security"][0]["short_name"] += "\ue000"
        self.assert_code("document_identity_mismatch", payload)

    def test_other_visible_control_characters_are_rejected_before_whitespace_folding(self):
        for character in ("\0", "\x01", "\x1c", "\x85", "\u200b", "\u202e", "\ufeff", "\U000f0000"):
            for content in ("人工正文" + character + "。", "<p>人工正文" + character + "。</p>"):
                with self.subTest(character=repr(character), content=content):
                    self.assert_code("document_layout_invalid", detail_payload(notice_content=content))
        for encoded in ("&#0;", "&#x1c;", "&#x200b;", "&#x110000;"):
            self.assert_code("document_layout_invalid", detail_payload(notice_content="<p>人工正文" + encoded + "。</p>"))
        self.assert_code("document_layout_invalid", detail_payload(page_size=2, notice_content="完整句。\n未完成\u200b"))
        document = self.parse(detail_payload(notice_content="人工\t正文。\r\n第二段\u3000文本。"))
        self.assertEqual(document.paragraphs, ("人工 正文。", "第二段 文本。"))

    def test_attachment_only_response_and_download_prompt_are_not_body(self):
        for content in (None, "", "   ", "<script>private source error</script>", "公告全文详见附件。", "平安银行：本公告具体内容详见附件。"):
            self.assert_code("document_attachment_only", detail_payload(notice_content=content, attach_url="https://ignored.invalid/file.pdf"))
        payload = detail_payload(attach_list=[{"url": "https://ignored.invalid/file.pdf"}])
        del payload["data"]["notice_content"]
        self.assert_code("document_attachment_only", payload)
        self.assert_code("document_empty_body", detail_payload(notice_content=""))

    def test_no_title_or_abstract_field_is_promoted_to_body(self):
        payload = detail_payload(summary="看似是正文的人工摘要。", abstract="平安银行人工摘要。")
        del payload["data"]["notice_content"]
        self.assert_code("document_empty_body", payload)

    def test_body_containing_only_title_is_not_full_text_even_in_a_heading(self):
        title = self.candidate.title
        for content in (title, title + '\n' + title, '<h1>' + title + '</h1>', '<p>' + title + '</p><p>' + title + '</p>'):
            for attachment in (False, True):
                with self.subTest(content=content, attachment=attachment):
                    payload = detail_payload(notice_content=content)
                    if attachment:
                        payload['data']['attach_url'] = 'https://ignored.invalid/attachment.pdf'
                    self.assert_code('document_attachment_only' if attachment else 'document_empty_body', payload)

    def test_explicit_summary_only_is_rejected_but_short_body_and_following_body_section_survive(self):
        for content in ('摘要：人工公告的摘要。', self.candidate.title + '\n内容摘要：人工公告的摘要。',
                        '<h2>公告摘要</h2><p>人工公告的摘要。</p>',
                        '<h1>' + self.candidate.title + '</h1><p>内容摘要：人工公告的摘要。</p>'):
            self.assert_code('document_empty_body', detail_payload(notice_content=content))
            self.assert_code('document_attachment_only', detail_payload(
                notice_content=content, attach_url='https://ignored.invalid/attachment.pdf'))
        short = '平安银行董事会批准本次议案。'
        self.assertEqual(self.parse(detail_payload(notice_content=short)).paragraphs, (short,))
        full = '<h2>摘要</h2><p>人工摘要。</p><h2>正文</h2><p>' + short + '</p>'
        self.assertIn(short, self.parse(detail_payload(notice_content=full)).paragraphs)

    def test_scripts_ads_hidden_elements_and_malformed_markup_are_not_evidence(self):
        content = ("<div><p>平安银行<a href='https://ignored.invalid'>人工正文</a>。</p>"
                   "<script>private error</script><p hidden>隐藏文字</p><ins>广告文字</ins>"
                   "<p style='visibility:hidden'>隐形文字</p><div class='recommend'><p>推荐文字</p></div></div>")
        self.assertEqual(self.parse(detail_payload(notice_content=content)).paragraphs, ("平安银行人工正文。",))
        for content in ("<p>平安银行未关闭正文", "<div><p>平安银行</div></p>", "<p hidden hidden>平安银行</p>"):
            self.assert_code("document_layout_invalid", detail_payload(notice_content=content))

    def test_body_alias_must_not_conflict_and_bad_envelope_fails_without_raw_errors(self):
        payload = detail_payload(content="不同的正文")
        self.assert_code("document_layout_invalid", payload)
        for payload in ({}, {"data": []}, {"code": "0", "data": {}}, {"success": False, "data": {}}):
            self.assert_code("document_invalid_response", payload)
        payload = detail_payload()
        payload["data"]["content"] = payload["data"].pop("notice_content")
        self.assertTrue(self.parse(payload).paragraphs)

    def test_size_and_paragraph_limits_reject_full_body_rather_than_truncate(self):
        self.assert_code("document_too_large", detail_payload(notice_content="平安银行" + "甲" * 32000))
        self.assert_code("document_too_large", detail_payload(notice_content="\n".join(["人工公告"] * 401)))
        self.assert_code("document_too_large", detail_payload(notice_content="<p>" + "甲" * 180000 + "</p>"))
        document = self.parse(detail_payload(notice_content="平安银行" + "甲" * (32000 - 4)))
        self.assertEqual(len(document.text), 32000)

    def test_failure_does_not_mutate_candidate_or_vendor_payload(self):
        payload = detail_payload(art_code="AN202611110009999999")
        previous = deepcopy(payload)
        self.assert_code("document_identity_mismatch", payload)
        self.assertEqual(payload, previous)
        self.assertEqual(self.candidate.parent_id, "announcement-title:artificial")


if __name__ == "__main__":
    unittest.main()
