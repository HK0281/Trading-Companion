"""Application-owned decision objectives; model output cannot grant execution rights."""

import re

_DECISION = re.compile(
    r"能不能[买卖]|可不可以[买卖]|能[买卖](?:吗|么)|该不该[买卖]|(?:值不值得|值得)(?:购买|买|卖)|"
    r"值不值|该买|该卖|适合[买卖]|买卖评级|交易计划|参考计划|(?:能|可以|还能)(?:上车|追)(?:吗|么)|"
    r"(?:现在|当前).*(?:购买|买入|卖出)|(?:走势|后市)(?:怎么|如何)|"
    r"(?:后面|后续|接下来|未来|后市).*(?:怎么走|走势|方向|涨|跌)|"
    r"预测.*(?:走势|股价|方向)|还会[涨跌]|会不会[涨跌]|(?:(?<!止盈)止损|止盈(?!止损)|目标价).*(?:怎么|多少|如何)|"
    r"(?:(?<!止盈)止损|止盈(?!止损)|目标价)(?:呢|是多少)|后面怎么走")
_NO_PLAN = re.compile(r"(?:不要|不用|无需|不需要)(?:再)?(?:交易计划|参考计划|止盈止损|风险计划)")


def wants_decision(question):
    if type(question) is not str:
        return False
    text = _NO_PLAN.sub("", question)
    return bool(_DECISION.search(text))


def excludes_plan(question):
    return bool(_NO_PLAN.search(question))


RATINGS = {
    "买入": ("conditional_entry", ("偏多",)),
    "增持": ("conditional_entry", ("偏多",)),
    "持有": ("wait", ("震荡", "偏多")),
    "减持": ("holding_exit", ("震荡", "偏空")),
    "卖出": ("holding_exit", ("震荡", "偏空")),
}
