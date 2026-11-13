"""Artificial document contracts; no provider or model access."""

from dataclasses import replace
from datetime import datetime
import json
import unittest

from trading_companion.research_documents import (
    DocumentCandidate, DocumentError, DocumentExcerpt, MAX_NEWS_CHARACTERS, ResearchDocument,
    SHANGHAI, announcement_code, document_question_terms, supported_news_url, trim_incomplete_tail,
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
        first_text = "平安银行😀人工第一段记录业务最新进展，相关生产安排仍需结合后续披露核对。"
        risk = "经营风险人工说明涉及原材料短缺，新增产线的生产安排仍可能延后。"
        independent = "独立的经营风险说明涉及许可条件，现有项目仍需等待后续审批结果。"
        paragraph = first_text + "甲" * 490 + "。" + risk
        document = ResearchDocument(news_candidate(), (paragraph, independent, "末段。"), NOW, "sina-artibody-v3")
        excerpts = document.excerpts("经营风险")
        self.assertEqual(len(excerpts), 2)
        self.assertIn(independent, [e.text for e in excerpts])
        for excerpt in excerpts:
            original = document.paragraphs[excerpt.paragraph_index - 1]
            self.assertEqual(excerpt.text, original[excerpt.start:excerpt.end])
            self.assertLessEqual(len(excerpt.text), 480)
            self.assertEqual(excerpt.document_id, document.id)
            self.assertEqual(excerpt.content_sha256, document.content_sha256)
            self.assertEqual(len(excerpt.sha256), 64)
        first = document.excerpts("完全无匹配的词", max_count=1, max_chars=60)[0]
        self.assertEqual(first.text, first_text)

    def test_long_sentences_are_skipped_instead_of_splitting_numbers_negations_or_conditions(self):
        long_sentence = "若条件不满足不得买入" + "甲" * 460 + "12345万元尚未到账。"
        self.assertGreater(len(long_sentence), 480)
        complete = "平安银行完整短句记录业务最新进展，相关生产安排仍需结合后续披露核对。"
        document = ResearchDocument(news_candidate(), (long_sentence + complete,), NOW, "sina-artibody-v3")
        excerpts = document.excerpts("12345万元")
        self.assertEqual([e.text for e in excerpts], [complete])
        self.assertEqual(excerpts[0].start, len(long_sentence))
        only_long = replace(document, paragraphs=(long_sentence,))
        self.assertEqual(only_long.excerpts("条件"), ())

    def test_administrative_paragraphs_do_not_displace_substantive_excerpts_or_change_identity(self):
        candidate = announcement_candidate(title="平安银行:关于人工业务事项的公告")
        noise = (
            "证券代码：000001 证券简称：平安银行 公告编号：临2026-001",
            "证券代码：000001 证券简称：平安银行",
            "关于人工业务事项的公告",
            "本公司董事会及全体董事保证本公告内容不存在任何虚假记载、误导性陈述或者重大",
            "本公司董事会及全体董事保证本公告内容不存在任何虚假记载、误导性陈述或者重大遗漏，"
            "并对其内容的真实性、准确性和完整性承担法律责任。",
            "（来源：人工来源）",
            "（责任编辑：人工编辑）",
            "本报告可能载有（除历史数据外）前瞻性陈述。该等前瞻性陈述乃根据平安银行对未来事件或绩效的"
            "现行假设、期望、信念、计划、目标及预测而作出。",
            "特此公告。",
            "平安银行股份有限公司董事会",
            "二〇二六年十一月十一日",
        )
        substantive = ("公司近日收到药品注册批准通知，相关产品正式获准上市，后续生产安排仍需落实。",
                       "本次获批后的生产安排尚待落实，公司将根据许可条件进一步披露相关项目的实施进展。")
        document = ResearchDocument(candidate, noise + substantive, NOW, "eastmoney-announcement-body-v3")
        identity = (document.id, document.content_sha256, document.paragraphs)
        excerpts = document.excerpts("平安银行000001最近公告的具体内容是什么？")
        self.assertEqual([e.text for e in excerpts], list(substantive))
        self.assertEqual([e.paragraph_index for e in excerpts], [len(noise) + 1, len(noise) + 2])
        self.assertTrue(all(e.start == 0 and e.end == len(e.text) for e in excerpts))
        self.assertEqual(identity, (document.id, document.content_sha256, document.paragraphs))
        self.assertEqual(replace(document, paragraphs=noise).excerpts("平安银行公告"), ())

    def test_disclaimer_sentences_are_skipped_without_rewriting_substantive_risks(self):
        business = "公司此次拟新增产线，仍需取得生产许可，是否投产及具体时间将以审批结果为准。"
        risk = "公司面临原材料短缺风险，现有生产计划可能延后，相关客户交付安排仍存在不确定性。"
        cases = (
            ("本公司董事会及全体董事保证本公告内容不存在任何虚假记载、误导性陈述或者重大遗漏，"
             "并对其内容的真实性、准确性和完整性承担法律责任。", business),
            ("本报告可能载有前瞻性陈述。", risk),
            ("证券代码：000001 证券简称：平安银行 公告编号：临2026-001。", business),
        )
        for prefix, substantive in cases:
            with self.subTest(prefix=prefix):
                paragraph = prefix + substantive
                document = ResearchDocument(announcement_candidate(), (paragraph,), NOW, "eastmoney-announcement-body-v3")
                excerpt, = document.excerpts("公司新增产线风险")
                self.assertEqual((excerpt.start, excerpt.end, excerpt.text), (len(prefix), len(paragraph), substantive))
        paragraph = business + "本报告可能载有前瞻性陈述。" + risk
        document = ResearchDocument(announcement_candidate(), (paragraph,), NOW, "eastmoney-announcement-body-v3")
        excerpts = document.excerpts("生产风险")
        self.assertEqual([e.text for e in excerpts], [business, risk])
        self.assertEqual(excerpts[1].start, len(paragraph) - len(risk))
        self.assertTrue(all(e.text == paragraph[e.start:e.end] for e in excerpts))

    def test_short_source_prefixes_are_filtered_with_or_without_parentheses(self):
        for paragraph in ("（来源：财闻）", "(责任编辑：人工编辑)", "（作者：人工作者）。", "来源：财闻",
                          "责任编辑：人工编辑", "来源：人工来源 公司新增产线尚未投产。"):
            with self.subTest(paragraph=paragraph):
                document = ResearchDocument(news_candidate(), (paragraph,), NOW, "sina-artibody-v3")
                self.assertEqual(document.excerpts("新闻内容"), ())
        paragraph = "来源：人工来源 公司新增产线项目仍在等待必要的生产许可，相关生产安排存在不确定性，需要继续跟踪后续披露。"
        self.assertGreaterEqual(len(paragraph), 40)
        for paragraph in (paragraph, "2026年11月11日，公司新增产线项目仍未完工，后续实施安排仍需结合审批结果核对。"):
            with self.subTest(paragraph=paragraph):
                document = ResearchDocument(news_candidate(), (paragraph,), NOW, "sina-artibody-v3")
                self.assertEqual([e.text for e in document.excerpts("新闻内容")], [paragraph])

    def test_question_topic_outranks_company_identifiers_and_generic_requests(self):
        paragraphs = ("公司已经启动设备更新项目，新增设备的生产安排仍需取得相关许可并结合后续信息核对。",
                      "平安银行000001近日发布公告，具体内容尚需结合后续相关信息继续核对。",
                      "本次临时股东会将审议新增董事议案。")
        document = ResearchDocument(news_candidate(), paragraphs, NOW, "sina-artibody-v3")
        excerpt, = document.excerpts("平安银行000001.SZ最近的临时股东会通知讲了哪些议案？", max_count=1)
        self.assertEqual((excerpt.paragraph_index, excerpt.text), (3, paragraphs[2]))
        first, = document.excerpts("平安银行000001最近公告主要内容是什么？", max_count=1)
        self.assertEqual(first.text, paragraphs[0])
        terms = document_question_terms("平安银行000001.SZ最近的临时股东会通知讲了哪些议案？",
                                        ("平安银行",), ("000001",))
        self.assertTrue({"临时", "股东", "通知", "议案"} <= terms)
        self.assertFalse({"平安", "银行", "000001", "最近", "哪些"} & terms)
        self.assertIn("1000001", document_question_terms("1000001", security_codes=("000001.SZ",)))
        self.assertIn("申请", document_question_terms("请解读药品申请上市的公告"))

    def test_substantive_candidates_require_length_and_sentence_or_numeric_content(self):
        complete = "公司新建生产设施目前仍在等待必要许可，相关业务安排尚需结合后续披露核对。"
        unfinished = complete.removesuffix("。")
        numerical = unfinished + "，涉及2027年度生产安排"
        document = ResearchDocument(news_candidate(), ("公司获得批准上市。", unfinished, complete, numerical),
                                    NOW, "sina-artibody-v3")
        excerpts = document.excerpts("完全无匹配的词")
        self.assertEqual([e.text for e in excerpts], [complete, numerical])
        self.assertTrue(all(len(e.text) >= 30 for e in excerpts))
        for line in ("一、基本情况", "（二）人工项目进展", "3.生产安排"):
            with self.subTest(line=line):
                self.assertEqual(replace(document, paragraphs=(line,)).excerpts("生产安排"), ())

    def test_short_agendas_and_vote_results_survive_without_becoming_fragmented_headers(self):
        for line in ("一、关于续聘会计师事务所的议案", "审议通过新增担保事项。", "表决结果如下", "同意9票，反对0票",
                     "（同意 7 票，反对 0 票，弃权 0 票）"):
            with self.subTest(line=line):
                document = ResearchDocument(announcement_candidate(), (line,), NOW, "eastmoney-announcement-body-v3")
                self.assertEqual([e.text for e in document.excerpts("议案表决")], [line])
        header = "议案 编号 表决 意见 同意 反对 弃权 股东 票数 100 200 300 单位 人民币 万元"
        self.assertGreaterEqual(len(header), 30)
        document = ResearchDocument(announcement_candidate(), (header,), NOW, "eastmoney-announcement-body-v3")
        self.assertEqual(document.excerpts("议案表决"), ())

    def test_approval_core_survives_source_company_and_repeated_title_lines(self):
        candidate = announcement_candidate(instrument_id="600276.SH", security_name="恒瑞医药",
            title="恒瑞医药:恒瑞医药关于获得药品注册批准及相关产品后续生产安排与业务进展的公告",
            url="https://data.eastmoney.com/notices/detail/600276/AN202611110001234567.html")
        core = ("近日，江苏恒瑞医药股份有限公司子公司收到国家药品监督管理局的通知，"
                "批准公司自主研发的 1 类新药上市，现将相关情况公告如下。")
        paragraphs = ("（来源：财闻）", "江苏恒瑞医药股份有限公司", candidate.title,
                      "关于获得药品注册批准及相关产品后续生产安排与业务进展的公告", core)
        document = ResearchDocument(candidate, paragraphs, NOW, "eastmoney-announcement-body-v3")
        identity = (document.id, document.content_sha256, document.paragraphs)
        excerpt, = document.excerpts("恒瑞医药最近药品注册批准公告讲了什么？")
        self.assertEqual((excerpt.paragraph_index, excerpt.start, excerpt.end, excerpt.text), (5, 0, len(core), core))
        self.assertEqual(identity, (document.id, document.content_sha256, document.paragraphs))

    def test_business_sentences_ending_with_a_company_name_are_not_name_only_lines(self):
        business = "本公司拟收购从事人工智能设备研发生产销售业务的某某股份有限公司。"
        self.assertGreaterEqual(len(business), 30)
        document = ResearchDocument(announcement_candidate(), (business,), NOW, "eastmoney-announcement-body-v3")
        excerpt, = document.excerpts("拟收购的公司")
        self.assertEqual((excerpt.start, excerpt.end, excerpt.text), (0, len(business), business))
        resolution = "审议通过收购某某股份有限公司"
        self.assertEqual([e.text for e in replace(document, paragraphs=(resolution,)).excerpts("审议通过收购")], [resolution])

    def test_combined_identity_and_standard_guarantee_do_not_displace_the_core_sentence(self):
        header = "证券代码：000001 证券简称：平安银行 公告编号：临2026-001 "
        guarantee = ("本公司董事会及全体董事保证本公告内容不存在任何虚假记载、误导性陈述或者重大遗漏，"
                     "并对其内容的真实性、准确性和完整性承担法律责任。")
        core = "公司本次拟投资314万元用于新建生产线，相关项目尚需取得生产许可，具体实施安排将另行披露。"
        administrative = header + guarantee
        paragraph = administrative + core
        document = ResearchDocument(announcement_candidate(), (paragraph,), NOW, "eastmoney-announcement-body-v3")
        excerpt, = document.excerpts("平安银行最新公告具体内容")
        self.assertEqual((excerpt.start, excerpt.end, excerpt.text), (len(administrative), len(paragraph), core))
        self.assertEqual(excerpt.text, paragraph[excerpt.start:excerpt.end])
        self.assertEqual(replace(document, paragraphs=(administrative,)).excerpts("公告内容"), ())
        # A genuine business sentence immediately after a header is not a guarantee.
        combined_business = header + core
        excerpt, = replace(document, paragraphs=(combined_business,)).excerpts("投资新建生产线")
        self.assertEqual((excerpt.start, excerpt.end, excerpt.text), (0, len(combined_business), combined_business))

    def test_html_identity_title_and_guarantee_sentence_is_skipped_without_changing_body_offsets(self):
        heading = "平安银行股份有限公司第八届董事会第五十九次会议决议公告"
        candidate = announcement_candidate(title="平安银行:" + heading)
        header = "证券代码：000001 证券简称：平安银行 公告编号：临2026-087 "
        rendered_heading = "平安银行股份有限公司 第八届董事会第五十九次会议决议公告 "
        guarantee = ("本公司董事会及全体董事保证本公告内容不存在任何虚假记载、误导性陈述或者重大遗漏，"
                     "并对其内容的真实性、准确性和完整性承担法律责任。")
        core = "公司间接子公司拟发行中期票据用于补充经营资金，全资子公司提供担保，相关安排仍需完成必要手续。"
        for padding in ("", "公司将根据实际发行进度及时披露相关担保安排，具体实施情况仍需结合后续正式文件核对。" * 12):
            with self.subTest(long_paragraph=bool(padding)):
                administrative = header + rendered_heading + guarantee
                paragraph = administrative + core + padding
                document = ResearchDocument(candidate, (paragraph,), NOW, "eastmoney-announcement-body-v3")
                identity = (document.id, document.content_sha256, document.paragraphs)
                excerpts = document.excerpts("中期票据担保具体内容")
                self.assertTrue(excerpts)
                self.assertEqual(excerpts[0].start, len(administrative))
                self.assertTrue(excerpts[0].text.startswith(core))
                self.assertTrue(all(e.start >= len(administrative) and e.text == paragraph[e.start:e.end] for e in excerpts))
                self.assertTrue(all(len(e.text) <= 480 for e in excerpts))
                self.assertLessEqual(len(excerpts), 2)
                self.assertEqual(identity, (document.id, document.content_sha256, document.paragraphs))
                self.assertEqual(replace(document, paragraphs=(administrative,)).excerpts("票据担保"), ())
                title_sentence = header + rendered_heading.rstrip() + "。"
                excerpt, = replace(document, paragraphs=(title_sentence + guarantee + core,)).excerpts("票据担保")
                self.assertEqual((excerpt.start, excerpt.text), (len(title_sentence + guarantee), core))
        for prefix in ("1 ", "23 ", "1234 "):
            with self.subTest(page_token=prefix):
                administrative = prefix + header + rendered_heading + guarantee
                paragraph = administrative + core
                document = ResearchDocument(candidate, (paragraph,), NOW, "eastmoney-announcement-body-v3")
                excerpt, = document.excerpts("票据担保")
                self.assertEqual((excerpt.start, excerpt.end, excerpt.text), (len(administrative), len(paragraph), core))
                self.assertEqual(excerpt.text, paragraph[excerpt.start:excerpt.end])
        # A continuous number or an overlong token is not guessed to be a page number.
        for prefix in ("1", "12345 "):
            with self.subTest(nonpage_prefix=prefix):
                paragraph = prefix + header + rendered_heading + guarantee
                document = ResearchDocument(candidate, (paragraph,), NOW, "eastmoney-announcement-body-v3")
                excerpt, = document.excerpts("董事会会议")
                self.assertEqual((excerpt.start, excerpt.text), (0, paragraph))

    def test_html_title_and_guarantee_filters_do_not_swallow_interleaved_business(self):
        heading = "平安银行股份有限公司关于间接子公司发行中期票据并由全资子公司提供担保的公告"
        candidate = announcement_candidate(title="平安银行:" + heading)
        header = "证券代码：000001 证券简称：平安银行 公告编号：临2026-088 "
        guarantee = ("本公司董事会及全体董事保证本公告内容不存在任何虚假记载、误导性陈述或者重大遗漏，"
                     "并对其内容的真实性、准确性和完整性承担法律责任。")
        business = "公司已确定本次担保上限为314万元，具体担保条件仍需履行必要审批程序"
        paragraphs = (header + heading + business + "，" + guarantee,
                      header + heading + guarantee.removesuffix("。") + "，" + business + "。",
                      header + "本次担保人为人工科技股份有限公司" + heading + guarantee,
                      header + "本次交易对方为人工产业发展股份有限公司" + heading + guarantee,
                      header + "本次担保人为平安银行股份有限公司" + heading + guarantee,
                      header + "人工科技股份有限公司" + heading + guarantee)
        for paragraph in paragraphs:
            with self.subTest(paragraph=paragraph):
                document = ResearchDocument(candidate, (paragraph,), NOW, "eastmoney-announcement-body-v3")
                excerpt, = document.excerpts("担保上限314万元")
                self.assertEqual((excerpt.start, excerpt.end, excerpt.text), (0, len(paragraph), paragraph))

    def test_legal_issuer_prefixes_do_not_turn_repeated_document_titles_into_evidence(self):
        candidate = announcement_candidate(security_name="人工银行", title="人工银行:人工银行2026年第三次临时股东会会议资料")
        combined_title = "中国人工银行股份有限公司2026 年第三次临时股东会会议资料"
        agenda = "关于选举人工候选人为公司非执行董事的议案"
        core = "公司本次拟发行资本工具以满足未来经营需求，相关发行安排仍需股东会审议并取得必要许可。"
        document = ResearchDocument(candidate, (combined_title, combined_title + "之二", combined_title + "（三）", agenda, core),
                                    NOW, "eastmoney-announcement-body-v3")
        self.assertEqual([e.text for e in document.excerpts("临时股东会议案")], [agenda, core])

    def test_procedural_meeting_fields_and_empty_resolution_intros_do_not_displace_business(self):
        notice = ("平安银行股份有限公司第十届董事会第六次会议通知于2026年11月1日通过传真、电子邮件等形式送达。"
                  "本次董事会会议于2026年11月3日采用通讯方式召开，应参加会议董事7人，实际收到7名董事的有效表决票。"
                  "会议的召集、召开符合《公司法》及《公司章程》的有关规定。")
        intro = "经与会董事审议，表决通过了如下决议："
        agenda = "关于公司与人工合作方共同设立合资公司的议案"
        business = "为加强技术研发能力，公司拟与人工合作方共同设立合资公司，董事会授权管理层办理相关手续。"
        document = ResearchDocument(announcement_candidate(), (notice, intro, agenda, business), NOW, "eastmoney-announcement-body-v3")
        self.assertEqual([e.text for e in document.excerpts("平安银行最近董事会决议公告具体内容")], [agenda, business])
        first = notice.split("。", 1)[0] + "。"
        mixed = first + business
        excerpt, = replace(document, paragraphs=(mixed,)).excerpts("董事会合资公司")
        self.assertEqual((excerpt.start, excerpt.end, excerpt.text), (len(first), len(mixed), business))
        substantive_notice = "本次董事会会议于2026年11月3日召开并批准公司新增生产线投资，相关许可仍需继续落实。"
        self.assertEqual([e.text for e in replace(document, paragraphs=(substantive_notice,)).excerpts("新增生产线")], [substantive_notice])

    def test_repeated_page_leaders_identify_a_contents_fragment_without_losing_the_real_agenda(self):
        contents = ("关于选举人工候选人为非执行董事的议案......1"
                    "关于发行资本工具和相关债务工具的议案......3关于新增项目的议案......5")
        agenda = "关于选举人工候选人为公司非执行董事的议案"
        core = "公司本次拟发行资本工具以满足未来经营需求，相关发行安排仍需股东会审议并取得必要许可。"
        document = ResearchDocument(announcement_candidate(), (contents, agenda, core), NOW, "eastmoney-announcement-body-v3")
        self.assertEqual([e.text for e in document.excerpts("临时股东会议案")], [agenda, core])

    def test_numeric_table_rows_do_not_displace_complete_pledge_business_sentences(self):
        row = "2025 年 8 月 2026 年 10 人工银行股份1,200,000 1.20% 0.29% 11 日 月 9 日 人工支行分行1,000,000 是 分行"
        english_row = "ExampleLong 125,000 24.50% 61,600, 20.74% 5.08% 0 0% 0 0%（XX） Holdings"
        business = "相关股东目前已解除部分股份质押，尚余质押股份100万股，后续安排仍需及时披露。"
        document = ResearchDocument(announcement_candidate(), (row, english_row, business), NOW, "eastmoney-announcement-body-v3")
        self.assertEqual([e.text for e in document.excerpts("股份质押")], [business])

    def test_complete_business_sentences_do_not_become_title_substrings_or_issuer_labels(self):
        cases = (
            ("平安银行：关于公司拟以自有资金50000万元投资新建高端装备生产基地项目的公告",
             "公司拟以自有资金50000万元投资新建高端装备生产基地项目。"),
            ("平安银行关于子公司获得药品注册证书的公告",
             "北京人工创新生物医药科技产业发展股份有限公司获得药品注册证书。"),
        )
        for title, business in cases:
            with self.subTest(business=business):
                self.assertGreaterEqual(len(business), 30)
                document = ResearchDocument(announcement_candidate(title=title), (business,), NOW, "eastmoney-announcement-body-v3")
                excerpt, = document.excerpts("最新公告具体内容")
                self.assertEqual((excerpt.start, excerpt.end, excerpt.text), (0, len(business), business))
                repeated = replace(document, paragraphs=(title + "。",))
                self.assertEqual(repeated.excerpts("最新公告具体内容"), ())
        title, business = cases[0]
        label = business.removesuffix("。")
        document = ResearchDocument(announcement_candidate(title=title), (label,), NOW, "eastmoney-announcement-body-v3")
        self.assertEqual(document.excerpts("投资项目"), ())

    def test_explicit_business_statements_are_exempt_from_both_space_fragment_branches(self):
        business = "公司已完成新增设备采购 10 万元 和 20 万元 的 合计 出资 安排"
        self.assertGreaterEqual(len(business), 30)
        document = ResearchDocument(announcement_candidate(), (business,), NOW, "eastmoney-announcement-body-v3")
        excerpt, = document.excerpts("新增设备采购")
        self.assertEqual((excerpt.start, excerpt.end, excerpt.text), (0, len(business), business))

    def test_reference_artifacts_are_omitted_only_inside_an_explicit_reference_section(self):
        artifact = "2. 人工证券登记服务有限责任公司证券质押及司法冻结明细表。"
        business = "相关股东目前已解除部分股份质押，尚余质押股份100万股，后续安排仍需及时披露。"
        document = ResearchDocument(announcement_candidate(),
            ("四、备查文件", "1. 解除证券质押登记通知；", artifact, business), NOW, "eastmoney-announcement-body-v3")
        excerpt, = document.excerpts("股份质押")
        self.assertEqual((excerpt.paragraph_index, excerpt.text), (4, business))
        self.assertEqual([e.text for e in replace(document, paragraphs=(artifact,)).excerpts("股份质押")], [artifact])
        resumed = replace(document, paragraphs=("四、备查文件", artifact, "五、后续业务安排", artifact))
        excerpt, = resumed.excerpts("证券质押")
        self.assertEqual((excerpt.paragraph_index, excerpt.text), (4, artifact))

    def test_old_and_reflowed_announcement_versions_remain_distinct_and_source_bound(self):
        paragraph = "公司新建生产设施目前仍在等待必要许可，相关业务安排尚需结合后续披露核对。"
        current = ResearchDocument(announcement_candidate(), (paragraph,), NOW, "eastmoney-announcement-body-v3")
        prior = replace(current, extractor_version="eastmoney-announcement-body-v2")
        self.assertEqual(prior.content_sha256, current.content_sha256)
        self.assertNotEqual(prior.id, current.id)
        self.assertEqual(prior.extractor_version, "eastmoney-announcement-body-v2")
        with self.assertRaises(DocumentError):
            replace(current, extractor_version="sina-artibody-v3")

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
                                        'eastmoney-announcement-body-v3', private_use_characters_replaced=1)
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
            replace(full, extractor_version="eastmoney-announcement-body-v3")

    def test_news_and_announcement_paragraph_limits_and_selected_excerpt_locations_follow_the_source(self):
        news = ResearchDocument(news_candidate(), ('人工完整句。',) * 128, NOW, 'sina-artibody-v3')
        self.assertEqual(len(news.paragraphs), 128)
        with self.assertRaises(DocumentError):
            replace(news, paragraphs=news.paragraphs + ('新闻多一段。',))
        last = '最后一段独立期限涉及项目实施安排，公司仍需等待许可并根据实际情况继续披露相关进展。'
        announcement = ResearchDocument(announcement_candidate(), ('人工完整句。',) * 399 + (last,),
                                        NOW, 'eastmoney-announcement-body-v3')
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
