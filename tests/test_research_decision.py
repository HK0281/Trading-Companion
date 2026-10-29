import unittest

from trading_companion.agent_input import InputIssue
from trading_companion.research_decision import wants_decision
from trading_companion.research_objective import parse_outlook_expression


class ResearchDecisionTests(unittest.TestCase):
    def test_paraphrases_and_non_decisions(self):
        for q in ("现在这个价位值得购买吗", "值不值得买", "值得买吗", "那现在能买吗", "它后面怎么走", "走势怎么看", "预测一下走势", "还能上车吗", "目标价呢"):
            with self.subTest(question=q): self.assertTrue(wants_decision(q))
        for q in ("追踪消息", "上市后第一个交易日以来的走势", "止损是什么", "只看基本面", "不要交易计划", "那止盈止损呢"):
            with self.subTest(question=q): self.assertFalse(wants_decision(q))

    def test_trading_horizon_without_calendar_conversion(self):
        for q, expected in (("未来二十个交易日", "未来20个交易日"), ("未来366个交易日", "未来366个交易日"), ("未来两周", "未来2周")):
            self.assertEqual(parse_outlook_expression(q).horizon, expected)
        for q in ("未来367个交易日", "未来0个交易日", "未来3.5个交易日"):
            with self.assertRaises(InputIssue): parse_outlook_expression(q)
