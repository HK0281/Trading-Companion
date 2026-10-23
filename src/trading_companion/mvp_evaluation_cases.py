"""Fixed, reviewable MVP scenarios; fixture interpretations are not a language model."""

from dataclasses import dataclass, replace
from datetime import date, timedelta

from trading_companion.multi_contracts import DEFAULT_INDICATORS, IndicatorSpec, MultiIndicatorRequest
from trading_companion.development_fixtures import REFERENCE_DATE


START, END = date(2026, 9, 21), date(2026, 9, 25)
DATE_TEXT = "2026-09-21 至 2026-09-25"
SMA3 = IndicatorSpec("sma", window=3)
RSI2 = IndicatorSpec("rsi", window=2)
PAIR = (SMA3, RSI2)
ALL = (SMA3, IndicatorSpec("ema", window=3), IndicatorSpec("macd", fast=2, slow=3, signal=2),
       RSI2, IndicatorSpec("boll", window=3, stddev_multiplier=2), IndicatorSpec("atr", window=2),
       IndicatorSpec("volume", window=3))


@dataclass(frozen=True)
class Scope:
    indicators: tuple[IndicatorSpec, ...] = PAIR
    start: date = START
    end: date = END
    instrument_id: str = "000001.SZ"

    def request(self):
        return MultiIndicatorRequest(self.instrument_id, self.start, self.end, self.indicators, source="csv")


@dataclass(frozen=True)
class Step:
    question: str
    status: str
    codes: tuple[str, ...]
    calls: int
    analyses: int = 0
    scope: Scope | None = None
    input_mode: str = "natural"
    intent: str = "research"
    mentions: tuple[str, ...] = ()
    date_expression: str | None = None
    indicators: tuple[str, ...] = ()
    report_status: str | None = None
    saved: bool = False
    explanation_status: str = "not_performed"
    synthesis_status: str | None = None


@dataclass(frozen=True)
class Scenario:
    name: str
    category: str
    steps: tuple[Step, ...]

    @property
    def model_call_limit(self):
        return sum(step.calls for step in self.steps)


def explicit(name, text, specs, *, partial=False):
    return Scenario(name, "numeric_research", (Step(
        f"分析 000001.SZ，{DATE_TEXT}，指标：{text}",
        "limited" if partial else "completed", ("partial_research" if partial else "research_completed",),
        2, 1, Scope(specs), input_mode="explicit", report_status="limited" if partial else "complete",
        explanation_status="accepted", synthesis_status="complete" if specs == ALL and not partial else "limited",
    ),))


def natural(question=None, *, mentions=("平安银行",), indicators=("SMA3", "RSI2"),
            expression=DATE_TEXT, scope=Scope(), partial=False):
    return Step(question or f"分析平安银行，{DATE_TEXT}，只看SMA3和RSI2",
        "limited" if partial else "completed", ("partial_research" if partial else "research_completed",),
        3, 1, scope, mentions=mentions, date_expression=expression, indicators=indicators,
        report_status="limited" if partial else "complete", explanation_status="accepted", synthesis_status="limited")


def unclear(question, *, mentions, expression=DATE_TEXT, indicators=("SMA3",),
            codes=("instrument_needs_clarification",)):
    return Step(question, "needs_clarification", codes, 1, mentions=mentions,
                date_expression=expression, indicators=indicators)


BASE = natural()
AMBIGUOUS = unclear(f"分析平安，{DATE_TEXT}，只看SMA3", mentions=("平安",))
TWO_SMA = (SMA3, IndicatorSpec("sma", window=4), RSI2)

SCENARIOS = (
    explicit("single_sma", "SMA3", (SMA3,)),
    explicit("single_ema", "EMA3", (ALL[1],)),
    explicit("single_macd", "MACD(2,3,2)", (ALL[2],)),
    explicit("single_rsi", "RSI2", (RSI2,)),
    explicit("single_boll", "BOLL(3,2)", (ALL[4],)),
    explicit("single_atr", "ATR2", (ALL[5],)),
    explicit("single_volume", "VOLUME3", (ALL[6],)),
    explicit("complete_combination", "SMA3、EMA3、MACD(2,3,2)、RSI2、BOLL(3,2)、ATR2、VOLUME3", ALL),
    explicit("partial_history", "SMA3、SMA20、VOLUME3", (SMA3, IndicatorSpec("sma", window=20), ALL[6]), partial=True),
    Scenario("default_indicators", "defaults", (replace(natural(f"分析平安银行，{DATE_TEXT}", indicators=(),
        scope=Scope(DEFAULT_INDICATORS), partial=True), calls=2, codes=("research_not_ready",),
        explanation_status="not_performed", synthesis_status=None),)),
    Scenario("natural_name", "identity", (BASE,)),
    Scenario("bare_code", "identity", (natural(f"分析000001，{DATE_TEXT}，只看SMA3和RSI2", mentions=("000001",)),)),
    Scenario("ambiguous_name", "clarification", (AMBIGUOUS,)),
    Scenario("unknown_name", "clarification", (unclear(f"分析不存在样例股，{DATE_TEXT}，只看SMA3", mentions=("不存在样例股",)),)),
    Scenario("multiple_stocks", "clarification", (unclear(f"分析平安银行和中国平安，{DATE_TEXT}，只看SMA3",
        mentions=("平安银行", "中国平安")),)),
    Scenario("today_rejected", "date_boundaries", (unclear("分析平安银行，今天，只看SMA3", mentions=("平安银行",),
        expression="今天", codes=("unsupported_date_expression", "intent_needs_clarification")),)),
    Scenario("future_rejected", "date_boundaries", (unclear("分析平安银行，明天，只看SMA3", mentions=("平安银行",),
        expression="明天", codes=("unsupported_date_expression", "intent_needs_clarification")),)),
    Scenario("default_dates", "defaults", (natural("分析平安银行，只看SMA3和RSI2", expression=None,
        scope=Scope(PAIR, END - timedelta(days=364), END)),)),
    Scenario("knowledge", "knowledge", (Step("解释一下MACD里的DIF和DEA", "completed", ("indicator_knowledge_answered",), 0),)),
    Scenario("news_not_supported", "scope", (Step("根据新闻选出潜力股", "out_of_scope", ("unsupported_intent",), 1, intent="unsupported"),)),
    Scenario("order_not_supported", "scope", (Step("帮我下单买入平安银行", "out_of_scope", ("unsupported_intent",), 1, intent="unsupported"),)),
    Scenario("target_not_supported", "scope", (Step("给出平安银行的止盈止损价", "out_of_scope", ("unsupported_intent",), 1, intent="unsupported"),)),
    Scenario("followup_without_result", "memory", (Step("这里的RSI为什么偏高", "needs_clarification", ("no_saved_result",), 0),)),
    Scenario("identity_continuation", "conversation", (AMBIGUOUS, Step("平安银行", "completed", ("research_completed",),
        2, 1, Scope((SMA3,)), report_status="complete", explanation_status="accepted", synthesis_status="limited"))),
    Scenario("indicator_edit", "conversation", (BASE, Step("把SMA3改成5日，其他不变", "completed", ("research_completed",),
        2, 1, Scope((IndicatorSpec("sma", window=5), RSI2)), report_status="complete",
        explanation_status="accepted", synthesis_status="limited"))),
    Scenario("ambiguous_edit", "conversation", (
        natural(f"分析平安银行，{DATE_TEXT}，只看SMA3、SMA4和RSI2", indicators=("SMA3", "SMA4", "RSI2"), scope=Scope(TWO_SMA)),
        Step("改成5日", "needs_clarification", ("indicator_edit_ambiguous", "indicator_edit_target_required"), 0),
        Step("SMA3", "completed", ("research_completed",), 2, 1,
            Scope((IndicatorSpec("sma", window=5), IndicatorSpec("sma", window=4), RSI2)), report_status="complete",
            explanation_status="accepted", synthesis_status="limited"))),
    Scenario("latest_refresh", "conversation", (BASE, Step("看最新的", "completed", ("research_completed",),
        2, 1, Scope(), report_status="complete", explanation_status="accepted", synthesis_status="limited"))),
    Scenario("relative_range_update", "conversation", (BASE, Step("把日期改成最近一个月", "completed", ("research_completed",),
        2, 1, Scope(PAIR, END - timedelta(days=29), END), report_status="complete",
        explanation_status="accepted", synthesis_status="limited"))),
    Scenario("saved_followup", "memory", (BASE, Step("这里的RSI为什么偏高", "completed", ("saved_question_answered",),
        1, scope=Scope(), report_status="complete", saved=True))),
    Scenario("explicit_history", "memory", (BASE, Step("查看上次结果", "completed", ("saved_result_shown",),
        0, scope=Scope(), report_status="complete", saved=True, synthesis_status="limited"))),
)

assert len(SCENARIOS) == 30 and len({case.name for case in SCENARIOS}) == 30
assert REFERENCE_DATE == END + timedelta(days=1)
