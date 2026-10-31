"""Trusted index identities and bounded, read-only market request recognition."""

from dataclasses import dataclass
import re
import unicodedata

from trading_companion.indicator_knowledge import ALIASES as INDICATOR_ALIASES
from trading_companion.tool_model import MAX_USER_CHARACTERS


@dataclass(frozen=True)
class IndexIdentity:
    instrument_id: str
    name: str
    symbol: str
    aliases: tuple[str, ...]


INDICES = (
    IndexIdentity("000001.SH", "上证指数", "sh000001", ("上证综合指数", "上证综指", "上证指数", "沪指", "上证")),
    IndexIdentity("399001.SZ", "深证成指", "sz399001", ("深证成份指数", "深证成指", "深成指")),
    IndexIdentity("000300.SH", "沪深300", "sh000300", ("沪深300指数", "沪深300", "沪深三百")),
    IndexIdentity("399006.SZ", "创业板指", "sz399006", ("创业板指数", "创业板指")),
    IndexIdentity("000688.SH", "科创50", "sh000688", ("上证科创板50成份指数", "科创50指数", "科创50", "科创五十")),
)
MARKET_MESSAGES = {
    "index_identity_required": "请明确要研究的指数名称：上证指数、深证成指、沪深300、创业板指或科创50。“科创板指数”不能唯一确认是科创50；未查询行情。",
    "market_research_unavailable": "已识别为指数研究问题。指数分析还在接入中，当前版本尚未提供；未查询行情，也未使用此前股票的研究结果。",
    "market_index_unsupported": "当前暂不支持该指数，未查询行情，也未使用此前股票的研究结果。",
    "market_tasks_need_selection": "本次包含多个研究对象或任务，请先明确要研究的一个指数或一只股票；未查询行情。",
    "market_scope_unsupported": "当前尚不支持板块、全市场宽度、成份股穿透或资金流分析，未查询相关数据。",
    "product_trade_execution_forbidden": "可以研究股票与风险情景，不能执行下单、委托或账户操作。",
    "product_unsafe_request": "本入口只接受只读研究问题，不执行外部链接、SQL或权限修改。",
}
_UNSUPPORTED = re.compile(r"(?:上证(?:50|180|380)|中证(?:500|1000|2000)|深证100|北证50)(?:指数)?|恒生(?:科技)?指数|恐慌指数")
_TIME_WORDS = r"今天|今日|现在|当前|最新|近期|最近|未来"
_OBSERVATION_WORDS = r"走势|行情|点位|后市|日线|周线|怎么样|怎么走|怎么看|支撑|压力|止损|止盈|目标|能买吗|能买|值得买吗"
_OBSERVATION_PREFIX = r"\s*的?\s*(?:(?:" + _TIME_WORDS + r")\s*的?\s*)?"
_MARKET = re.compile(
    r"大盘(?!股|蓝筹|风格)|A\s*股整体|(?:股票|宽基|大盘|A\s*股)\s*指数|"
    r"科创板指数|(?:A\s*股(?:市场)?|创业板)(?=" + _OBSERVATION_PREFIX + r"(?:" + _OBSERVATION_WORDS + r"))", re.I)
_FUND = re.compile(r"\s*(?:型)?(?:ETF|基金|期货|期权)", re.I)
_CONCEPT = re.compile(r"是什么|什么是|什么叫|定义|含义|概念|原理|怎么算|怎么计算|如何计算|计算公式|计算方法|上市规则")
_OBSERVATION = re.compile(_TIME_WORDS + "|" + _OBSERVATION_WORDS)
_ACTION = re.compile(r"(?:分析|研究|看看?|比较|对比)(?:一下|下)?\s*$")
_DETAIL = re.compile(_OBSERVATION_PREFIX + r"(?:" + _OBSERVATION_WORDS + r"|小时|技术|均线|MACD|RSI|ATR|BOLL)", re.I)
_BACKGROUND = re.compile(r"对.+(?:影响|冲击|利好|利空)|(?:影响|拖累|带动).*(?:公司|股票|个股)")
_BROAD = re.compile(r"板块(?:轮动|走势|分析)|全市场宽度|市场宽度|涨跌家数|成[份分]股(?:穿透|分析)|资金流(?:向|分析)?")
_INDICATOR_NAME = re.compile(
    r"(?<![A-Za-z])(?:" + "|".join(re.escape(name) for name in sorted(INDICATOR_ALIASES, key=len, reverse=True))
    + r")(?![A-Za-z])", re.I)


def _text(question):
    if (type(question) is not str or not question.strip() or len(question) > MAX_USER_CHARACTERS
            or any(not char.isprintable() and char not in "\n\r\t" for char in question)):
        return None
    return unicodedata.normalize("NFKC", question).strip()


def _mentions(text):
    """Longest identities win; never resolve bare numbers or an ETF as its index."""
    # Preserve offsets and reuse the curated indicator vocabulary. Unlisted
    # indicator names are also safe because bare "指数" is not a market identity.
    masked = list(text)
    for match in _INDICATOR_NAME.finditer(text):
        masked[match.start():match.end()] = " " * (match.end() - match.start())
    text = "".join(masked)
    spans = []
    for index in INDICES:
        names = sorted((*index.aliases, index.instrument_id, index.symbol), key=len, reverse=True)
        pattern = "|".join(re.escape(name) for name in names)
        for match in re.finditer(r"(?<![A-Za-z0-9_.])(?:" + pattern + r")(?![A-Za-z0-9_.零〇一二三四五六七八九十百千万])", text, re.I):
            if not _FUND.match(text[match.end():]):
                spans.append((match.start(), match.end(), index))
    for match in _UNSUPPORTED.finditer(text):
        if not _FUND.match(text[match.end():]):
            spans.append((match.start(), match.end(), None))
    for match in _MARKET.finditer(text):
        if (not _FUND.match(text[match.end():])
                and not any(start <= match.start() and match.end() <= end for start, end, _ in spans)):
            name = match.group()
            index = None
            if name == "创业板":
                index = INDICES[3]
            elif re.match(r"大盘|A\s*股", name, re.I):
                index = INDICES[0]
            spans.append((match.start(), match.end(), index))
    selected = []
    for span in sorted(spans, key=lambda item: (-(item[1] - item[0]), item[0])):
        if not any(span[0] < end and start < span[1] for start, end, _ in selected):
            selected.append(span)
    return sorted(selected, key=lambda item: item[0])


def market_reply_code(question, *, directory=None):
    """Read-only scope recognition. The historical success marker is resolved by market_selection."""
    text = _text(question)
    if text is None:
        return None
    spans = _mentions(text)
    if not spans:
        return None
    # Reuse execution guards rather than maintain another set of unsafe phrases.
    from trading_companion.product_plan import _execution_requested, _INJECTION
    if _execution_requested(text):
        return "product_trade_execution_forbidden"
    if _INJECTION.search(text):
        return "product_unsafe_request"
    if _CONCEPT.search(text) and not _OBSERVATION.search(text):
        return None
    masked = list(text)
    for start, end, _ in spans:
        masked[start:end] = " " * (end - start)
    company = directory is not None and bool(directory.mentioned_spans("".join(masked)))
    own_task = False
    for start, end, _ in spans:
        before = re.split(r"[,，;；。\n]", text[:start])[-1]
        after = re.split(r"[,，;；。\n]", text[end:], maxsplit=1)[0]
        if (not _BACKGROUND.search(before + text[start:end] + after)
                and (_ACTION.search(before) or _DETAIL.match(after))):
            own_task = True
    if _BACKGROUND.search(text) and not own_task:
        return None
    if company:
        return "market_tasks_need_selection" if own_task else None
    if re.search(r'(?:再|然后|接着|同时)(?:分析|研究|看看)', text):
        return "market_tasks_need_selection"
    if _BROAD.search(text):
        return "market_scope_unsupported"
    identities = {index.instrument_id for _, _, index in spans if index is not None}
    if len(identities) > 1:
        return "market_tasks_need_selection"
    if any(_UNSUPPORTED.fullmatch(text[start:end]) for start, end, _ in spans):
        return "market_index_unsupported"
    return "market_research_unavailable"


def market_selection(question, *, directory=None):
    """Resolve one index, or a fixed rejection. Bare numeric stock codes never enter."""
    code = market_reply_code(question, directory=directory)
    if code != 'market_research_unavailable':
        return None, False, code
    text = _text(question)
    spans = _mentions(text)
    indices = {item.instrument_id: item for _, _, item in spans if item is not None}
    if len(indices) != 1 or any(item is None for _, _, item in spans):
        return None, False, 'index_identity_required'
    index = next(iter(indices.values()))
    defaulted = index.instrument_id == '000001.SH' and any(
        re.search(r'大盘(?!股|蓝筹)|A\s*股', text[start:end], re.I)
        for start, end, item in spans if item is not None)
    return index, defaulted, None


def index_continuation(question, *, latest_kind, directory=None):
    if latest_kind != 'index' or type(question) is not str or _text(question) is None:
        return False
    if directory is not None and directory.mentioned_spans(question):
        return False
    from trading_companion.indicator_knowledge import parse_knowledge_question
    from trading_companion.open_knowledge import is_general_knowledge_question
    if parse_knowledge_question(question) is not None or is_general_knowledge_question(question, directory=directory):
        return False
    from trading_companion.routing_context import continuation_question, new_question
    return not new_question(question) and continuation_question(question)
