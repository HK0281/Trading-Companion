"""Artificial article text with the historically inspected Sina page structure."""

from dataclasses import replace
from html import escape
import unittest
from unittest.mock import patch

from trading_companion.company_news_body import parse_sina_article
from trading_companion.research_documents import DocumentError
from tests.test_research_documents import NOW, news_candidate


def article_html(*, title=None, paragraphs=None, published="2026-11-11T10:15:09+08:00", visible_date="2026年11月11日 10:15", extra_meta=""):
    candidate = news_candidate()
    paragraphs = ("平安银行000001人工正文第一段。", "人工经营风险第二段。") if paragraphs is None else paragraphs
    body = "".join("<p>" + escape(p) + "</p>" for p in paragraphs)
    return ("<html><head><meta charset='utf-8'><meta property='article:published_time' content='" + published
            + "'>" + extra_meta + "</head><body><h1 class='main-title'>" + escape(candidate.title if title is None else title)
            + "</h1><span class='date'>" + visible_date + "</span><div class='article' id='artibody'>"
            + body + "</div><!-- 正文 end --><p>结束标记以后的推荐段落不读</p></body></html>")


class SinaArticleBodyTests(unittest.TestCase):
    def setUp(self):
        self.candidate = news_candidate()
        guard = patch("requests.request", side_effect=AssertionError("No external HTTP"))
        guard.start()
        self.addCleanup(guard.stop)

    def parse(self, text=None, **options):
        return parse_sina_article(article_html() if text is None else text, self.candidate, retrieved_at=NOW, **options)

    def assert_code(self, code, text):
        with self.assertRaises(DocumentError) as caught:
            self.parse(text)
        self.assertEqual(caught.exception.code, code)

    def test_visible_body_excludes_promotions_and_retains_inline_link_text(self):
        text = article_html().replace("平安银行000001人工正文第一段。", "<a class='keyword' href='https://ignored.invalid'>平安银行</a>000001<font>人工正文</font>第一段。")
        text = text.replace("</div><!-- 正文 end -->", "<script><p>脚本干扰</p></script><ins class='sinaads'><p>广告干扰</p></ins>"
                            "<img src='https://ignored.invalid/pixel'><p hidden>隐藏干扰</p>"
                            "<p style='DISPLAY: none'>样式干扰</p><div class='recommend'><p>推荐干扰</p></div>"
                            "<!-- promo begin --><div><p>推广干扰</p></div><!-- promo end -->"
                            "</div><!-- 正文 end -->")
        document = self.parse(text)
        self.assertEqual(document.paragraphs, ("平安银行000001人工正文第一段。", "人工经营风险第二段。"))
        self.assertEqual(document.published_at, "2026-11-11T10:15:09+08:00")
        self.assertEqual(document.extractor_version, "sina-artibody-v3")

    def test_missing_duplicate_and_incomplete_regions_do_not_fall_back_to_whole_page(self):
        source = article_html()
        for text in (source.replace("id='artibody'", "id='other'"),
                     source.replace("</div>", ""), source.replace("<!-- 正文 end -->", ""),
                     source.replace("</body>", "<div class='article' id='artibody'></div></body>"),
                     source.replace("</div><!-- 正文 end -->", "<!-- promo begin --></div><!-- 正文 end -->"),
                     source.replace("id='artibody'", "id='artibody' id='other'"),
                     source.replace("</div><!-- 正文 end -->", "<h2>实质正文标题不能被静默漏掉</h2></div><!-- 正文 end -->")):
            with self.subTest(text=text[-60:]):
                self.assert_code("document_layout_invalid", text)

    def test_title_and_both_timestamp_fields_must_match_verified_list(self):
        self.assert_code("document_title_mismatch", article_html(title="别的公司人工标题"))
        self.assert_code("document_time_mismatch", article_html(visible_date="2026年11月11日 11:15"))
        self.assert_code("document_time_mismatch", article_html(published="2026-11-10T10:15:09+08:00", visible_date="2026年11月10日 10:15"))
        self.assert_code("document_time_mismatch", article_html(published="2026-11-11T11:15:09+08:00", visible_date="2026年11月11日 11:15"))
        self.assert_code("document_time_unavailable", article_html(published="2026-11-11T10:15:09"))
        self.assert_code("document_time_unavailable", article_html().replace("article:published_time", "missing"))
        self.assert_code("document_time_unavailable", article_html(extra_meta="<meta property='article:published_time' content='2026-11-11T10:15:09+08:00'>"))

    def test_company_must_appear_in_visible_body_not_title_ads_or_longer_number(self):
        for paragraph in ("别的公司自己的正文。", "代码10000012不是该证券代码。"):
            text = article_html(paragraphs=(paragraph,)).replace("</div><!-- 正文 end -->", "<ins><p>平安银行000001广告</p></ins></div><!-- 正文 end -->")
            self.assert_code("document_instrument_mismatch", text)
        self.assertEqual(self.parse(article_html(paragraphs=("证券代码000001的人工正文。",))).paragraphs,
                         ("证券代码000001的人工正文。",))

    def test_page_updates_cannot_be_future_or_before_publication(self):
        self.assert_code("document_updated_after_retrieval", article_html(extra_meta="<meta property='article:modified_time' content='2026-11-13T11:00:00+08:00'>"))
        self.assert_code("document_time_mismatch", article_html(extra_meta="<meta property='article:modified_time' content='2026-11-10T11:00:00+08:00'>"))
        document = self.parse(article_html(extra_meta="<meta property='article:modified_time' content='2026-11-11T11:00:00+08:00'>"))
        self.assertEqual(document.updated_at, "2026-11-11T11:00:00+08:00")

    def test_original_publication_matches_list_and_later_metadata_is_not_an_update(self):
        original = "<meta name='bytedance:published_time' content='2026-11-11T10:15:09+08:00'>"
        document = self.parse(article_html(published="2026-11-11T10:22:11+08:00", extra_meta=original))
        self.assertEqual(document.published_at, "2026-11-11T10:15:09+08:00")
        self.assertIsNone(document.updated_at)
        self.assertEqual(document.metadata_at, "2026-11-11T10:22:11+08:00")
        self.assertEqual(self.parse(article_html().replace("article:published_time", "bytedance:published_time")).published_at,
                         "2026-11-11T10:15:09+08:00")
        self.assert_code("document_time_mismatch", article_html(extra_meta=original.replace("10:15:09", "10:14:09")))
        self.assert_code("document_time_mismatch", article_html(published="2026-11-11T10:14:09+08:00", extra_meta=original))
        self.assert_code("document_time_mismatch", article_html(published="2026-11-13T10:15:09+08:00", extra_meta=original))

    def test_distinct_update_fields_are_validated_and_the_latest_is_retained(self):
        extra = ("<meta name='bytedance:published_time' content='2026-11-11T10:15:09+08:00'>"
                 "<meta name='bytedance:updated_time' content='2026-11-11T10:22:12+08:00'>"
                 "<meta property='article:modified_time' content='2026-11-11T10:22:10+08:00'>"
                 "<meta property='article:updated_time' content='2026-11-11T10:22:09+08:00'>"
                 "<meta property='og:updated_time' content='2026-11-11T10:22:08+08:00'>")
        document = self.parse(article_html(published="2026-11-11T10:22:13+08:00", extra_meta=extra))
        self.assertEqual(document.updated_at, "2026-11-11T10:22:12+08:00")
        self.assertEqual(document.metadata_at, "2026-11-11T10:22:13+08:00")
        self.assert_code("document_time_mismatch", article_html(extra_meta=extra.replace("10:22:12", "10:14:12")))
        self.assert_code("document_updated_after_retrieval", article_html(extra_meta=extra.replace(
            "2026-11-11T10:22:12", "2026-11-13T10:22:12")))

    def test_every_known_metadata_field_must_be_unique_and_have_an_exact_timestamp(self):
        names = ("bytedance:published_time", "article:published_time", "bytedance:updated_time",
                 "article:modified_time", "article:updated_time", "og:updated_time", "bytedance:lrDate_time")
        for name in names:
            base = article_html().replace("article:published_time", "bytedance:published_time") if name == "article:published_time" else article_html()
            for value in ("not-a-time", "2026-11-11T10:15:09", "2026-13-11T10:15:09+08:00", ""):
                with self.subTest(name=name, value=value):
                    self.assert_code("document_time_unavailable", base.replace("</head>",
                        "<meta name='" + name + "' content='" + value + "'></head>"))
            for second in ("2026-11-11T10:15:09+08:00", "2026-11-11T10:16:09+08:00"):
                with self.subTest(name=name, duplicate=second):
                    extra = ("<meta name='" + name + "' content='2026-11-11T10:15:09+08:00'>"
                             "<meta property='" + name + "' content='" + second + "'>")
                    self.assert_code("document_time_unavailable", base.replace("</head>", extra + "</head>"))

    def test_lrdate_metadata_time_is_checked_without_becoming_an_update(self):
        extra = "<meta property='bytedance:lrDate_time' content='2026-11-11T10:23:11+08:00'>"
        document = self.parse(article_html(extra_meta=extra))
        self.assertEqual(document.metadata_at, "2026-11-11T10:23:11+08:00")
        self.assertIsNone(document.updated_at)
        self.assert_code("document_time_mismatch", article_html(extra_meta=extra.replace("10:23:11", "10:14:11")))
        self.assert_code("document_time_mismatch", article_html(extra_meta=extra.replace("2026-11-11", "2026-11-13")))

    def test_empty_oversized_or_nontext_input_never_becomes_complete_document(self):
        self.assert_code("document_layout_invalid", article_html(paragraphs=()))
        self.assert_code("document_too_large", article_html(paragraphs=("平安银行" + "甲" * 16000,)))
        self.assert_code("document_too_large", article_html(paragraphs=("平安银行",) * 129))
        self.assert_code("document_too_large", article_html() + "甲" * 180000)
        self.assert_code("document_layout_invalid", "")
        self.assert_code("document_layout_invalid", b"not html")

    def test_candidate_time_can_be_date_only_but_document_keeps_exact_page_time(self):
        self.candidate = replace(self.candidate, published_at=None)
        self.assertEqual(self.parse().published_at, "2026-11-11T10:15:09+08:00")

    def test_inspected_plain_layout_uses_explicit_qr_container_not_repeated_unclosed_comments(self):
        # Structure follows news0/news2/news4/news5; all prose is invented.
        content = ("<!-- 相关专题top start --><p>人工专题推广</p><!-- 相关专题top end -->"
                   "<!-- 期货推广begin --><p>人工期货推广</p><!-- 期货推广end -->"
                   "<!-- 正文广告top start --><p>人工顶部广告</p><!-- 正文广告top end -->"
                   "<p>平安银行人工正文第一段。</p>"
                   "<!-- 正文下iframe --><iframe><p>人工iframe推广</p></iframe><!-- 正文下iframe -->"
                   "<!-- 文末二维码 start --><!-- 文末二维码 start -->"
                   "<style>.appendQr_wrap { color: black; }</style>"
                   "<div class='clearfix appendQr_wrap'><div class='appendQr_normal'><img src='ignored'></img></div>"
                   "<div class='appendQr_normal_txt'>人工二维码广告文字<p>人工广告段落</p></div></div>"
                   "<!-- 文末二维码 start --><!-- 文末二维码 start -->"
                   "<p>二维码块之后的实际正文仍须保留。</p>")
        text = article_html(paragraphs=()).replace("</div><!-- 正文 end -->", content + "</div><!-- 原始正文end -->"
            "<!-- 正文下部begin --><div><p>正文容器外推荐</p></div><!-- 正文下部end -->"
            "<!-- 正文 end --><div><p>第二个模板区推荐</p></div><!-- 正文 end -->")
        document = self.parse(text)
        self.assertEqual(document.paragraphs, ("平安银行人工正文第一段。", "二维码块之后的实际正文仍须保留。"))

    def test_inspected_image_and_table_paragraph_layout_preserves_caption_and_optional_inline_closures(self):
        # Structure follows news1/news6: captions are kept; the entire table is omitted.
        content = ("<div class='img_wrapper'><img src='ignored'><span class='img_descr'>"
                   "<p><font>人工图片说明。</font></p></span></div>"
                   "<p><span>平安银行人工正文<strong>未配对的内联格式也保留全部文字。</p></span>"
                   "<p>省略结束标签的段落。<p>后继段落仍应独立保留。</p>"
                   "<table><tbody><tr><td><p>人工字段。</p></td><td><p>人工数值12345万元。</p></td></tr></tbody></table>"
                   "<p><span hidden><!-- 正文 end -->隐藏文字</span>隐藏块不能提前结束实际正文。</p>"
                   "<p class='article-editor'>人工编辑信息</p>")
        text = article_html(paragraphs=()).replace("</div><!-- 正文 end -->", content + "</div><!-- 原始正文end -->"
            "<!-- 正文下部begin --><div><p>人工推荐</p></div><!-- 正文下部end -->"
            "<!-- 正文 end --><!-- 正文 end -->")
        document = self.parse(text)
        self.assertEqual(document.paragraphs, ("人工图片说明。", "平安银行人工正文未配对的内联格式也保留全部文字。",
            "省略结束标签的段落。", "后继段落仍应独立保留。", "隐藏块不能提前结束实际正文。"))
        self.assertFalse(document.complete)
        self.assertEqual(document.omitted_tables, 1)

    def test_visible_tables_are_reported_as_omitted_and_unknown_structural_closures_still_fail(self):
        text = article_html().replace("</div><!-- 正文 end -->", "<table><tr><td>实质表格12345万元</td></tr></table></div><!-- 正文 end -->")
        document = self.parse(text)
        self.assertEqual(document.paragraphs, ("平安银行000001人工正文第一段。", "人工经营风险第二段。"))
        self.assertFalse(document.complete)
        self.assertEqual(document.omitted_tables, 1)
        self.assertEqual(document.incomplete_reasons, ("document_tables_omitted",))
        self.assert_code("document_layout_invalid", article_html().replace("</p>", "</section></p>", 1))

    def test_body_end_markers_never_silently_discard_visible_tail_text(self):
        for marker in ("<!-- 正文 end -->", "<!-- 原始正文end -->",
                       "<!-- 正文 end --><!-- 原始正文end --><!-- 正文 end -->"):
            for tail in ("<p>实质尾段风险条件。</p>", "实质尾段的裸文本风险条件。",
                         "<div><a>实质尾段链接。</a></div>", "<blockquote>实质尾段引文。</blockquote>",
                         "<table><tr><td>实质尾段表格。</td></tr></table>"):
                with self.subTest(marker=marker, tail=tail):
                    text = article_html().replace("</div><!-- 正文 end -->",
                        marker + tail + "</div><!-- 正文 end -->")
                    self.assert_code("document_layout_invalid", text)

    def test_inspected_hidden_tail_containers_and_external_text_remain_excluded(self):
        tail = ("<!-- 原始正文end --><!-- 正文 end -->"
                "<div class='appendQr_wrap'><div class='appendQr_normal_txt'>人工二维码推广<p>人工推广说明</p></div></div>"
                "<div class='recommend'><p>人工推荐标题</p></div>"
                "<ins class='sinaads'><p>人工尾部广告</p></ins><p class='article-editor'>人工编辑信息</p>")
        text = article_html().replace("<div class='article'", "<p>正文容器外的网页导语。</p><div class='article'")
        text = text.replace("</div><!-- 正文 end -->", tail + "</div><!-- 正文 end --><!-- 正文 end -->")
        self.assertEqual(self.parse(text).paragraphs, ("平安银行000001人工正文第一段。", "人工经营风险第二段。"))

    def test_cross_paragraph_closure_of_hidden_formatting_is_rejected(self):
        for attributes in ("hidden", "class='promo'"):
            for boundary in ("<p>", "</p><p>", "<div>"):
                with self.subTest(attributes=attributes, boundary=boundary):
                    text = article_html().replace("</p>", "<b " + attributes + ">HIDDEN_FIRST"
                        + boundary + "HIDDEN_RECONSTRUCTED</b>" + ("</div>" if boundary == "<div>" else "</p>"), 1)
                    self.assert_code("document_layout_invalid", text)

    def test_body_boundary_cannot_implicitly_close_hidden_formatting(self):
        for attributes in ("hidden", "class='promo'"):
            with self.subTest(attributes=attributes):
                text = article_html().replace("</div><!-- 正文 end -->",
                    "<p>额外可见正文。<b " + attributes + ">HIDDEN_BODY_END</div><!-- 正文 end -->")
                self.assert_code("document_layout_invalid", text)

    def test_nested_tables_and_all_their_paragraphs_are_omitted_as_outer_blocks(self):
        content = ("<p>平安银行人工正文。</p><table><tr><td><p>表格段落不得进入片段。</p>"
                   "<table><tr><td>嵌套表格。</td></tr></table></td></tr></table>"
                   "<p>表格之间的叙述仍保留。</p><table><tr><td>第二个表格。</td></tr></table>"
                   "<p>末尾完整叙述。</p>")
        text = article_html(paragraphs=()).replace("</div><!-- 正文 end -->", content + "</div><!-- 正文 end -->")
        document = self.parse(text)
        self.assertEqual(document.paragraphs, ("平安银行人工正文。", "表格之间的叙述仍保留。", "末尾完整叙述。"))
        self.assertEqual(document.omitted_tables, 2)
        self.assertFalse(document.complete)

    def test_hidden_tables_do_not_claim_missing_article_content(self):
        content = ("<table hidden><tr><td>隐藏表格。</td></tr></table>"
                   "<div class='recommend'><table><tr><td>推荐表格。</td></tr></table></div>"
                   "<!-- promo begin --><table><tr><td>推广表格。</td></tr></table><!-- promo end -->")
        text = article_html().replace("</div><!-- 正文 end -->", content + "</div><!-- 正文 end -->")
        document = self.parse(text)
        self.assertTrue(document.complete)
        self.assertEqual(document.omitted_tables, 0)
        self.assertEqual(document.incomplete_reasons, ())

    def test_partial_documents_trim_incomplete_tail_before_security_binding(self):
        table = "<table><tr><td>人工表格。</td></tr></table>"
        text = article_html(paragraphs=("平安银行保留句。", "第二个完整句。不能保留的半句"))
        text = text.replace("</div><!-- 正文 end -->", table + "</div><!-- 正文 end -->")
        document = self.parse(text)
        self.assertEqual(document.paragraphs, ("平安银行保留句。", "第二个完整句。"))
        self.assertEqual(document.omitted_trailing_characters, len("不能保留的半句"))
        text = article_html(paragraphs=("没有目标证券的完整句。", "平安银行不完整尾句"))
        self.assert_code("document_instrument_mismatch", text.replace("</div><!-- 正文 end -->", table + "</div><!-- 正文 end -->"))
        text = article_html(paragraphs=("平安银行只有半句",))
        self.assert_code("document_empty_body", text.replace("</div><!-- 正文 end -->", table + "</div><!-- 正文 end -->"))

    def test_omitted_tables_cannot_supply_the_only_body_or_security_identity(self):
        table = "<table><tr><td><p>平安银行000001表格身份。</p></td></tr></table>"
        text = article_html(paragraphs=()).replace("</div><!-- 正文 end -->", table + "</div><!-- 正文 end -->")
        self.assert_code("document_layout_invalid", text)
        text = article_html(paragraphs=("其他公司的叙述。",)).replace("</div><!-- 正文 end -->", table + "</div><!-- 正文 end -->")
        self.assert_code("document_instrument_mismatch", text)

    def test_table_omission_keeps_unknown_text_structure_and_resource_checks(self):
        for tail, code in (("<table><tr><td>表格。</td></tr></table>未知实质判断。", "document_layout_invalid"),
                           ("<table><tr><td>未闭合表格。</table>", "document_layout_invalid"),
                           ("<table><tr><td><!-- 正文 end -->表格。</td></tr></table>", "document_layout_invalid"),
                           ("<table>" + "<span>" * 65 + "</span>" * 65 + "</table>", "document_too_large"),
                           ("<table>" + "<br>" * 12001 + "</table>", "document_too_large"),
                           ("<table></table><p>" + "甲" * 16001 + "</p>", "document_too_large"),
                           ("<table></table>" + "<p>尾句</p>" * 129, "document_too_large")):
            with self.subTest(code=code, tail=tail[:35]):
                self.assert_code(code, article_html().replace("</div><!-- 正文 end -->", tail + "</div><!-- 正文 end -->"))

    def test_quote_direct_inline_and_paragraph_text_preserves_source_order(self):
        content = ("<p>平安银行引用前正文。</p><blockquote>直接引用<span>及<a href='https://ignored.invalid'>链接文字</a></span>。"
                   "<p>引用中的段落。</p>段落之后的引用。<blockquote>嵌套引用。</blockquote>嵌套之后的引用。"
                   "<span hidden>隐藏内容</span><br>换行之后。</blockquote><p>引用后正文。</p>")
        text = article_html(paragraphs=()).replace("</div><!-- 正文 end -->", content + "</div><!-- 正文 end -->")
        document = self.parse(text)
        self.assertEqual(document.paragraphs, ("平安银行引用前正文。", "直接引用及链接文字。", "引用中的段落。",
                         "段落之后的引用。", "嵌套引用。", "嵌套之后的引用。 换行之后。", "引用后正文。"))
        self.assertTrue(document.complete)
        text = article_html(paragraphs=()).replace("</div><!-- 正文 end -->",
            "<blockquote>平安银行只有直接引用。</blockquote></div><!-- 正文 end -->")
        self.assertEqual(self.parse(text).paragraphs, ("平安银行只有直接引用。",))

    def test_quote_content_obeys_the_same_paragraph_and_character_limits(self):
        for content in ("<blockquote>平安银行" + "甲" * 16001 + "</blockquote>",
                        "<blockquote>平安银行。</blockquote>" * 129):
            with self.subTest(content=content[:35]):
                text = article_html(paragraphs=()).replace("</div><!-- 正文 end -->", content + "</div><!-- 正文 end -->")
                self.assert_code("document_too_large", text)

    def test_navigation_is_ignored_only_outside_paragraphs_and_quotes(self):
        content = ("<div><a href='https://ignored.invalid'>查看更多<span>人工问答</span></a></div>"
                   "<p>平安银行<a>正文链接不可丢掉</a>。</p><blockquote><a>引用链接不可丢掉。</a></blockquote>")
        text = article_html(paragraphs=()).replace("</div><!-- 正文 end -->", content + "</div><!-- 正文 end -->")
        self.assertEqual(self.parse(text).paragraphs, ("平安银行正文链接不可丢掉。", "引用链接不可丢掉。"))
        for unknown in ("<div><a>导航</a>未知实质判断。</div>", "<blockquote><div>未知结构内的引用。</div></blockquote>",
                        "<a><section>未知结构内的链接正文。</section></a>"):
            with self.subTest(unknown=unknown):
                self.assert_code("document_layout_invalid", article_html().replace("</div><!-- 正文 end -->", unknown + "</div><!-- 正文 end -->"))

    def test_new_content_paths_do_not_bypass_hidden_format_reconstruction_checks(self):
        for attributes in ("hidden", "class='promo'"):
            for content in ("<p>平安银行正文。<b " + attributes + ">隐藏<blockquote>仍隐藏。</b></blockquote>",
                            "<blockquote>平安银行正文。<b " + attributes + ">隐藏<p>仍隐藏。</b></p></blockquote>",
                            "<table><tr><td><b " + attributes + ">隐藏</td></tr></table><p>仍隐藏。</b></p>",
                            "<a>导航<b " + attributes + ">隐藏</a><p>仍隐藏。</b></p>"):
                with self.subTest(attributes=attributes, content=content[:45]):
                    self.assert_code("document_layout_invalid", article_html().replace("</div><!-- 正文 end -->", content + "</div><!-- 正文 end -->"))


if __name__ == "__main__":
    unittest.main()
