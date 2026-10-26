"""Small, explainable indicator profiles; explicit user specifications always win."""

from dataclasses import dataclass
import re

from trading_companion.multi_contracts import DEFAULT_INDICATORS, IndicatorSpec


@dataclass(frozen=True)
class IndicatorSelection:
    profile: str
    indicators: tuple[IndicatorSpec, ...]
    reason: str


def select_complementary_indicators(question: str) -> IndicatorSelection:
    """Select dependencies, never a market direction or an investment score."""
    if type(question) is not str:
        raise ValueError("Indicator selection requires question text")
    # Only omitted indicator lists use profiles; general questions retain their defaults.
    if re.search(r"背离|动能|动量|乏力|超买|超卖|反转", question):
        return IndicatorSelection("momentum", (
            IndicatorSpec("sma"), IndicatorSpec("macd"), IndicatorSpec("rsi"),
            IndicatorSpec("adx"), IndicatorSpec("volume"), IndicatorSpec("atr"),
        ), "结合价格位置、动量、趋势强度与量能核对，避免重复堆叠均线。")
    if re.search(r"量价|放量|缩量|成交配合|成交是否|成交支持", question):
        return IndicatorSelection("volume_price", (
            IndicatorSpec("sma"), IndicatorSpec("vwma"), IndicatorSpec("obv"),
            IndicatorSpec("volume"), IndicatorSpec("macd"), IndicatorSpec("atr"),
        ), "比较成交量加权价格、累计方向量和近期量能，分别保留其口径。")
    if re.search(r"趋势强度|趋势强不强|趋势是否|趋势还是震荡|震荡还是趋势|趋势行情", question):
        return IndicatorSelection("trend_strength", (
            IndicatorSpec("sma"), IndicatorSpec("sma", window=60), IndicatorSpec("macd"),
            IndicatorSpec("adx"), IndicatorSpec("atr"), IndicatorSpec("volume"),
        ), "分别核对均线方向参考、趋势强度、波动与成交，不把强度当方向。")
    if re.search(r"支撑|压力位|阻力|波动幅度|波动风险", question):
        return IndicatorSelection("structure_volatility", (
            IndicatorSpec("sma"), IndicatorSpec("boll"), IndicatorSpec("atr"),
            IndicatorSpec("volume"), IndicatorSpec("adx"),
        ), "价格结构与波动分开计算，历史候选位置不自动成为交易目标。")
    return IndicatorSelection("balanced", DEFAULT_INDICATORS,
                              "默认覆盖趋势、动量、波动和量能；明确参数始终优先。")
