"""Small versioned, application-owned indicator explanations; no market assertions."""

from dataclasses import dataclass
import re
import unicodedata


KNOWLEDGE_VERSION = "indicator-knowledge-v1"
DEFINITIONS = {
    "sma": ("SMA / 简单移动平均线", "取最近N条日线收盘价的算术平均。窗口越短，对近期价格变化通常越敏感；"
            "价格高于均线只说明相对位置，不能据此保证继续上涨。"),
    "ema": ("EMA / 指数移动平均线", "对近期收盘价赋予更大权重。本项目先用N条收盘价的SMA初始化，"
            "再按2/(N+1)递推；计算起点会影响早期结果，并不是价格预测。"),
    "macd": ("MACD", "DIF是快EMA减慢EMA；DEA是DIF的EMA。本项目柱值采用2×(DIF−DEA)。"
             "DIF高于DEA是位置关系，只有前后两条有效记录共同满足条件才能称为上穿。"),
    "dif": ("DIF", "MACD中的快周期EMA减慢周期EMA，衡量两条均线的差。DIF本身不是上涨概率；"
            "本项目常规参数为快12、慢26，但实际研究以展示的参数为准。"),
    "dea": ("DEA", "对DIF进行EMA平滑得到的信号线，常规信号周期为9。本项目以首批有效DIF的SMA初始化；"
            "DIF可能已可用而DEA仍缺历史，不能将缺失DEA当成0。"),
    "histogram": ("MACD柱值", "本项目柱值为2×(DIF−DEA)。柱值增加不一定表示上涨动量增强："
                  "还要区分正负、前后变化和其他信息，不能只看柱值大小作交易判断。"),
    "rsi": ("RSI / 相对强弱指标", "根据最近N次收盘价变化的平均上涨幅度和平均下跌幅度计算，"
            "本项目采用Wilder平滑，输出0至100。高于70、低于30仅为参考分区；"
            "所谓超买/超卖不代表必然反转，不能单独作为买卖信号。"),
    "boll": ("BOLL / 布林带", "中轨是窗口内收盘价均值，上下轨为均值加减指定倍数的总体标准差（ddof=0）。"
             "价格触及上轨不等于必然下跌；波动为0时上下轨可以重合。"),
    "atr": ("ATR / 平均真实波幅", "真实波幅同时考虑当日高低价和前一收盘价，本项目采用Wilder平滑。"
            "ATR反映价格波动的尺度，单位为元/股，不表示上涨或下跌方向，也不是止损价本身。"),
    "volume": ("量能 / VOLUME", "同时查看当日成交量、含当日窗口均量及严格此前N条记录的均量。"
               "相对量能=当日量÷严格此前窗口均量；此前均量为0时倍率未定义，不补成0或无穷大。"),
}
ALIASES = {
    "SMA": "sma", "简单移动平均线": "sma", "均线": "sma",
    "EMA": "ema", "指数移动平均线": "ema", "MACD": "macd", "DIF": "dif", "DEA": "dea",
    "柱值": "histogram", "MACD柱值": "histogram", "MACD柱": "histogram", "RSI": "rsi", "BOLL": "boll",
    "布林带": "boll", "布林线": "boll", "ATR": "atr", "VOLUME": "volume", "量能": "volume", "成交量": "volume",
}
_NAMES = "|".join(re.escape(name) for name in sorted(ALIASES, key=len, reverse=True))
TOPIC_PATTERN = re.compile(
    r"(?:[0-9]+\s*(?:日|天)\s*(?:简单移动平均线|指数移动平均线|均线|SMA|EMA|RSI|ATR))|"
    r"(?<![A-Za-z])(?:" + _NAMES + r")(?![A-Za-z])(?:\s*(?:\([^()]*\)|[0-9]+))?", re.IGNORECASE)


def normalized(text: str) -> str:
    return unicodedata.normalize("NFKC", text).strip()


def topic_spans(text: str) -> tuple[tuple[int, int], ...]:
    return tuple(match.span() for match in TOPIC_PATTERN.finditer(text))


def topic_key(mention: str) -> str | None:
    text = normalized(mention).upper()
    text = re.sub(r"^[0-9]+\s*(?:日|天)\s*", "", text)
    text = re.sub(r"\s*(?:\([^()]*\)|[0-9]+)$", "", text).strip()
    return ALIASES.get(text)


@dataclass(frozen=True)
class KnowledgeAnswer:
    topics: tuple[str, ...]

    @property
    def text(self) -> str:
        lines = ["指标知识（项目维护的定义，不是某只股票的行情结论）："]
        lines.extend(f"{DEFINITIONS[key][0]}：{DEFINITIONS[key][1]}" for key in self.topics)
        lines.append("这里只解释计算口径与适用限制；未查询行情，不提供收益保证或交易指令。")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, object]:
        return {"version": KNOWLEDGE_VERSION, "origin": "application_curated", "topics": list(self.topics),
                "market_data_used": False, "text": self.text}


def parse_knowledge_question(question: str) -> KnowledgeAnswer | None:
    """Recognize complete definition questions only; never discard additional requirements."""
    text = normalized(question)
    patterns = (
        r"(?:(?:请|帮我|请帮我)\s*)?(?:解释一下|解释|介绍一下|介绍|讲讲|讲一下|什么是)\s*(.+?)\s*[?？。!！]?",
        r"(.+?)\s*(?:分别是什么|是什么|是什么意思|什么意思|有什么含义|是怎么计算的|怎么算的)\s*[?？。!！]?",
    )
    body = None
    for pattern in patterns:
        match = re.fullmatch(pattern, text)
        if match:
            body = match[1].strip()
            break
    if body is None:
        return None
    macd_prefix = re.match(r"MACD(?:中的|里的|中|里)\s*", body, re.IGNORECASE)
    if macd_prefix:
        body = body[macd_prefix.end():]
    body = re.sub(r"(?:的)?(?:含义|定义)$", "", body).strip()
    body = re.sub(r"(?:超买|超卖)(?:的)?$", "", body).strip()
    pieces = re.split(r"\s*(?:和|与|及|、|,|以及)\s*", body)
    topics = []
    for piece in pieces:
        if not TOPIC_PATTERN.fullmatch(piece):
            return None
        key = topic_key(piece)
        if key is None:
            return None
        if macd_prefix and key not in ("dif", "dea", "histogram"):
            return None
        if key not in topics:
            topics.append(key)
    return KnowledgeAnswer(tuple(topics)) if 1 <= len(topics) <= 10 else None
