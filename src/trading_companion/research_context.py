"""Whole-utterance outlook continuations, never extraction that drops new conditions."""

import json
import re
import unicodedata


_HORIZON = (
    r"下(?:个?月|一?个月|周|星期|礼拜)|明天|明日|后天|短线|短期|中线|中期|"
    r"(?:未来|接下来)(?:[0-9零一二两三四五六七八九十百]+\s*个?\s*"
    r"(?:交易日|自然日|天|日|星期|周|个月|月|年))?|后续"
)
_SUBJECT = r"(?:它|这只股票|这只股|该股)?"
_END = r"(?:的|可能的)?(?:走势|方向|趋势|表现)?(?:呢|怎么看|会怎么走|可能怎么走|怎么走|如何|怎么样|会涨吗|会跌吗)?"
_PATTERNS = (
    re.compile(r"(?:那|那么)?" + _SUBJECT + r"(?P<horizon>" + _HORIZON + r")" + _END),
    re.compile(r"(?:那|那么)?(?:请|帮我)?(?:预测|展望|分析|判断|看看|看)(?:一下|下)?"
               + _SUBJECT + r"(?P<horizon>" + _HORIZON + r")" + _END),
)


def contextual_outlook_expression(question: str) -> str | None:
    """Recognize only complete, bounded follow-ups without a new stock, date or indicator."""
    if type(question) is not str or not question.strip() or len(question) > 2000:
        return None
    text = unicodedata.normalize("NFKC", question).strip().rstrip("。.!！?？").strip()
    for pattern in _PATTERNS:
        match = pattern.fullmatch(text)
        if match is not None:
            return match["horizon"]
    return None


def objective_context(prepared) -> str:
    """Publish application-checked purpose separately from executable historical parameters."""
    if prepared is None or prepared.conditions is None or prepared.conditions.objective.kind != "outlook":
        return ""
    objective = prepared.conditions.objective
    context = "\nresearch_objective=" + json.dumps(objective.to_dict(), ensure_ascii=True, separators=(",", ":"))
    if objective.kind == "outlook":
        context += "\noutlook_reference_date=" + prepared.conditions.reference_date.isoformat()
    return context
