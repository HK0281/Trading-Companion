"""Explicit management language only; no research question silently changes a watchlist."""

import unittest

from trading_companion.personal_memory_commands import parse_personal_memory_command


class PersonalMemoryCommandTests(unittest.TestCase):
    def test_single_add_remove_and_list_have_no_research_state(self):
        examples = (
            ("把平安银行加入自选", "add", "平安银行"),
            ("请把000001.SZ加入自选。", "add", "000001.SZ"),
            ("将宁德时代添加到我的自选股", "add", "宁德时代"),
            ("新和成加入自选", "add", "新和成"),
            ("把和而泰加入自选", "add", "和而泰"),
            ("从自选移除平安银行", "remove", "平安银行"),
            ("从我的自选股里删除000001.SZ", "remove", "000001.SZ"),
            ("把平安银行从自选中移除", "remove", "平安银行"),
            ("查看自选", "list", None),
            ("请列出我的自选股", "list", None),
        )
        for question, action, mention in examples:
            with self.subTest(question=question):
                parsed = parse_personal_memory_command(question)
                self.assertEqual((parsed.action, parsed.mention), (action, mention))
                self.assertIsNone(parsed.message)
                self.assertIsNone(parsed.code)

    def test_research_and_knowledge_are_not_management(self):
        for question in (
            "分析自选股里的平安银行", "平安银行值得加入自选吗", "宁德时代适合加入自选吗",
            "平安银行应该加入自选吗", "哪些股票值得加入自选", "自选股怎么分析", "什么是自选股",
            "如何把股票加入自选", "帮我分析自选里的平安银行", "平安银行为什么值得加入自选",
            "将平安银行加入自选是否合适", "把平安银行加入自选值得吗", "研究把平安银行加入自选是否合适",
            "看看自选股里的平安银行", "我的成本价10元，现在能买吗", "我持有平安银行，分析走势",
            "如何设置仓位", "分析持仓的风险偏好", "如何记录我的成本价", "分析如何更新我的仓位",
            "平安银行现在能买吗", "第一个", "000001.SZ", "短线", None, "", "x" * 2001,
        ):
            with self.subTest(question=question):
                self.assertIsNone(parse_personal_memory_command(question))

    def test_multiple_and_mixed_commands_only_return_notices(self):
        for question in (
            "把平安银行、宁德时代加入自选", "把平安银行和宁德时代加入自选",
            "把000001.SZ,300750.SZ加入自选", "把平安银行加入自选并研究一下",
            "把平安银行加入自选，然后分析宁德时代", "把平安银行加入自选并从自选移除宁德时代",
            "把平安银行加入自选并分析是否适合长期", "研究是否合适，然后把平安银行加入自选",
            "查看自选并分析是否适合长期", "查看自选并分析平安银行",
            "把研究一下平安银行加入自选", "把平安银行加入自选？",
        ):
            with self.subTest(question=question):
                command = parse_personal_memory_command(question)
                self.assertEqual(command.action, "notice")
                self.assertIsNone(command.mention)
                self.assertNotIn("平安银行、宁德时代", command.message)

    def test_negation_never_authorizes_a_write(self):
        for question in ("不要把平安银行加入自选", "别把平安银行从自选移除", "平安银行没有加入自选", "不要查看自选"):
            with self.subTest(question=question):
                result = parse_personal_memory_command(question)
                self.assertEqual(result.action, "notice")
                self.assertEqual(result.code, "personal_memory_command_negated")

    def test_cost_preferences_and_clear_are_local_fixed_notices(self):
        for question in ("记住我的成本价10.4元", "请保存我的仓位比例20%", "删除我的成本", "不要记录我的仓位",
                         "请帮我设置我的成本价10元", "分析一下平安银行，再记住我的仓位20%",
                         "把平安银行的成本价设为10元", "把平安银行的仓位设为20%", "设置平安银行的仓位为20%",
                         "请分析平安银行，然后把平安银行的成本价设为10元"):
            with self.subTest(question=question):
                result = parse_personal_memory_command(question)
                self.assertEqual(result.action, "notice")
                self.assertEqual(result.code, "personal_memory_cost_not_supported")
                self.assertNotIn("10.4", result.message)
                self.assertEqual(result.message, "目前只记录是否持有、期限和风险偏好，不记录成本和仓位。")
        for question in ("清空自选", "请删除全部我的自选股", "清除全部自选"):
            with self.subTest(question=question):
                result = parse_personal_memory_command(question)
                self.assertEqual(result.action, "notice")
                self.assertEqual(result.code, "personal_memory_clear_confirmation_required")
                self.assertIn("确认", result.message)

    def test_preferences_explicit_one_stock_one_field(self):
        for question, field, value in (
            ("记住我持有平安银行", "holding_status", "held"),
            ("请记住我未持有000001.SZ。", "holding_status", "not_held"),
            ("把平安银行的持有状态设为不确定", "holding_status", "unknown"),
            ("把平安银行的期限偏好设为中线", "horizon", "medium"),
            ("把平安银行的风险偏好设为谨慎", "risk", "low"),
            ("把平安银行的风险偏好设为未填写", "risk", None),
        ):
            with self.subTest(question=question):
                command = parse_personal_memory_command(question)
                self.assertEqual((command.scope, command.action, command.field, command.value),
                                 ("preferences", "update", field, value))
                self.assertIn(command.mention, ("平安银行", "000001.SZ"))
        for question, mention in (("查看持仓与偏好", None), ("查看平安银行的持仓与偏好", "平安银行")):
            command = parse_personal_memory_command(question)
            self.assertEqual((command.scope, command.action, command.mention), ("preferences", "list", mention))

    def test_preferences_unsupported_mixed_negated_and_multi_subject_stay_local(self):
        for question in (
            "不要记住我持有平安银行", "记住我持有平安银行并分析走势",
            "记住我持有平安银行和宁德时代", "记住我持有平安银行，风险偏好谨慎",
            "把平安银行的风险偏好设为谨慎并分析走势", "把平安银行的期限偏好设为随便",
            "记住我持有", "记住我的风险偏好", "查看持仓与偏好并分析股票",
            "把平安银行的持有状态设为持有并记住仓位20%", "记住我持有平安银行成本10元",
            "分析平安银行，然后把平安银行的期限偏好设为中线", "不要查看持仓与偏好",
        ):
            with self.subTest(question=question):
                command = parse_personal_memory_command(question)
                self.assertEqual((command.scope, command.action), ("preferences", "notice"))
        for question in ("删除平安银行的持仓与偏好", "清空持仓与偏好"):
            self.assertIn("确认", parse_personal_memory_command(question).message)

    def test_preferences_confirmation_preserves_action_category_and_code(self):
        from trading_companion.personal_memory_commands import preference_reply
        for question in ("记住我未持有平安", "把平安的期限偏好设为中线", "查看平安的持仓与偏好"):
            command = parse_personal_memory_command(question)
            reply = parse_personal_memory_command(preference_reply(command, "000001.SZ"))
            self.assertEqual((reply.action, reply.field, reply.value, reply.mention),
                             (command.action, command.field, command.value, "000001.SZ"))


if __name__ == "__main__":
    unittest.main()
