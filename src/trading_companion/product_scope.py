"""One grammar for positive/negative research scope and technical intent."""
from dataclasses import dataclass, replace
import re

from trading_companion.agent_input import InputIssue
from trading_companion.errors import InvalidRequestError
from trading_companion.research_intent import _INDICATOR_CUE as _DAILY_INDICATOR_CUE, _indicator as _daily_indicator

_WEEKLY_INDICATOR = r"[0-9零一二两三四五六七八九十百]+\s*周\s*(?:指数移动平均线|简单移动平均线|均线|SMA|EMA|RSI)"
_INDICATOR_CUE = re.compile(_WEEKLY_INDICATOR + "|" + _DAILY_INDICATOR_CUE.pattern, re.I)


def _indicator(phrase):
    # The company path computes both daily and weekly views of the same parameters.
    # Do not teach the original daily-only parser that a week means a daily window.
    if re.fullmatch(_WEEKLY_INDICATOR, phrase, re.I):
        return _daily_indicator(phrase.replace("周", "日"))
    return _daily_indicator(phrase)

_EXPLICIT_TECHNICAL = re.compile(_WEEKLY_INDICATOR + r"|回测|历史回放|策略回放|只看技术|仅技术|纯技术|技术面|技术指标|技术分析|技术研究|日线|K线|均线|SMA|EMA|MACD|RSI|ATR|BOLL|VOLUME|ADX|OBV|VWMA|周线|日周|多周期|背离|支撑|压力位|阻力|量价|放量|缩量|动能|乏力|趋势强度|趋势强不强|震荡还是趋势|趋势还是震荡", re.I)
_DOMAIN_TERMS = {
    "基本面": ("profile", "financial"), "财务": ("financial",), "财报": ("financial",),
    "公司资料": ("profile",), "公司背景": ("profile",),
    "技术": ("technical",), "技术指标": ("technical",), "技术面": ("technical",), "技术分析": ("technical",),
    "新闻": ("news",), "资讯": ("news",), "公告": ("announcements",),
    "止盈止损": ("risk",), "止盈": ("risk",), "止损": ("risk",),
}
_DOMAIN_WORD = "|".join(re.escape(term) for term in sorted(_DOMAIN_TERMS, key=len, reverse=True))
_DOMAIN_WORD_PATTERN = re.compile(_DOMAIN_WORD)
_DOMAIN_LIST = r"(?:" + _DOMAIN_WORD + r")(?:\s*(?:以及|和|与|及|、|或|/)\s*(?:" + _DOMAIN_WORD + r"))*"
_NEGATIVE_PREFIX = r"(?:不需要|无需|不用|不必|不要|不是|不)(?:再)?"
_ONLY = re.compile(r"只(?:看|分析|研究|要)|仅(?:看|分析|研究|要)|(?:仅|纯)(?=技术)")
_NEGATED_ONLY = re.compile(
    _NEGATIVE_PREFIX + r"\s*(?:只是|仅仅|只|仅)\s*(?:看|分析|研究|要)"
    r"(?:\s*(?:它的|该公司的)?" + _DOMAIN_LIST + r")?")
_NEGATIVE_DOMAIN = re.compile(
    _NEGATIVE_PREFIX + r"(?:看|分析|研究|考虑|查询|获取|计算)?"
    r"\s*(?:它的|该公司的)?(?P<domains>" + _DOMAIN_LIST + r")")
_NEGATIVE_INDICATOR = re.compile(
    _NEGATIVE_PREFIX + r"(?:看|分析|研究|考虑|查询|获取|计算)?"
    r"\s*(?P<indicator>" + _INDICATOR_CUE.pattern + ")", re.I)


def _positive_scope(question):
    excluded = set()
    def mask(match):
        for domain in _DOMAIN_WORD_PATTERN.finditer(match["domains"]):
            excluded.update(_DOMAIN_TERMS[domain.group()])
        return " " * len(match.group())
    # "Not only news" broadens analysis; it neither excludes news nor grants an
    # affirmative only-clause. Known lists stop before punctuation or new verbs.
    text = _NEGATED_ONLY.sub(lambda match: " " * len(match.group()), question)
    text = _NEGATIVE_DOMAIN.sub(mask, text)
    def mask_indicator(match):
        try:
            excluded.update("indicator:" + spec.kind for spec in _indicator(match["indicator"]))
        except (InputIssue, InvalidRequestError, ValueError):
            pass
        return " " * len(match.group())
    return _NEGATIVE_INDICATOR.sub(mask_indicator, text), excluded


def _broadens_only(question, positive):
    return _NEGATED_ONLY.search(question) is not None and _ONLY.search(positive) is None


def has_company_domain(question):
    positive, _ = _positive_scope(question)
    return bool(re.search(r"基本面|财报|财务|新闻|资讯|公告|主营|业务|行业|营收|利润|估值|市盈率|市净率|公司资料|公司背景|交易规则|上市时间", positive))


def is_natural_technical_request(question):
    if type(question) is not str:
        return False
    positive, _ = _positive_scope(question)
    return bool(_EXPLICIT_TECHNICAL.search(positive)) and not has_company_domain(question)


def is_product_indicator_edit(question):
    if type(question) is not str:
        return False
    # Recognize edit intent only; the execution parser must consume the entire command.
    return bool(re.match(r"\s*(?:(?:请帮我|请|帮我)\s*)?(?:(?:再加|添加|增加|加上|删除|移除|去掉|把|将)|(?:改成|改为|换成|换为|调整为))", question)
                and re.search(r"SMA|EMA|RSI|MACD|ATR|BOLL|ADX|OBV|VWMA|VOLUME|均线|指标|周期|窗口|布林|\d+\s*(?:日|天)", question, re.I))


@dataclass(frozen=True)
class ProductScope:
    """Trusted scope survives clarification/continuation; never reconstructed from public JSON."""
    domains: tuple[str, ...] = ()
    indicator_ids: tuple[str, ...] = ()
    excluded: tuple[str, ...] = ()

    @classmethod
    def from_question(cls, question, kinds, indicators):
        positive, excluded = _positive_scope(question)
        only = _ONLY.search(positive) is not None
        strict = only and _INDICATOR_CUE.search(positive) is not None
        return cls(tuple(kinds) if only else (),
                   tuple(spec.indicator_id for spec in indicators) if strict else (),
                   tuple(sorted(excluded)))

    def to_dict(self):
        return {"domains": list(self.domains), "indicator_ids": list(self.indicator_ids), "excluded": list(self.excluded)}

    @property
    def constrained(self):
        return bool(self.domains or self.indicator_ids or self.excluded)

    @property
    def permits_atr_dependency(self):
        return not self.indicator_ids and 'indicator:atr' not in self.excluded

    def edited(self, indicators, *, removed=()):
        present = {spec.kind for spec in indicators}
        excluded = tuple(sorted((set(self.excluded) | {'indicator:' + kind for kind in removed})
                                - {'indicator:' + kind for kind in present}))
        return replace(self, indicator_ids=tuple(spec.indicator_id for spec in indicators)
                       if self.indicator_ids else (), excluded=excluded)

    def validate(self, kinds, indicators):
        if (type(self) is not ProductScope or self.domains and not set(kinds) <= set(self.domains)
                or set(kinds) & set(self.excluded)
                or any('indicator:' + spec.kind in self.excluded for spec in indicators)
                or self.indicator_ids and tuple(spec.indicator_id for spec in indicators) != self.indicator_ids):
            raise InvalidRequestError('Research scope differs from the confirmed limits')

