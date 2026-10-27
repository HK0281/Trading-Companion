"""Dated A-share rule explanations, separate from order validation and tradability."""

from dataclasses import dataclass
from datetime import date

from trading_companion.errors import InvalidRequestError
from trading_companion.market_context import BOARDS, ST_STATUSES


RULES_VERSION = "a-share-research-rules-2026-10-26-v1"
RULES_CHECKED_THROUGH = date(2026, 10, 26)
SSE_2023 = "https://www.sse.com.cn/lawandrules/sselawsrules/stocks/exchange/c/c_20230216_5716380.shtml"
SZSE_2023 = "https://www.szse.cn/lawrules/rule/stock/trade/t20230217_598773.html"
SSE_2026 = "https://www.sse.com.cn/lawandrules/sselawsrules2025/trade/universal/c/c_20260424_10816492.shtml"
SZSE_2026 = "https://www.szse.cn/lawrules/rule/trade/current/t20260424_620190.html"
SSE_CHANGE = "https://www.sse.com.cn/aboutus/mediacenter/hotandd/c/c_20260424_10816474.shtml"
STAR_RISK = "https://www.sse.com.cn/aboutus/mediacenter/hotandd/c/c_20240412_10753148.shtml"
SZSE_FIRST_FIVE = "https://www.szse.cn/www/investor/knowledge/t20230509_600303.html"


@dataclass(frozen=True)
class RuleVersion:
    version: str
    effective_from: date
    effective_to: date | None
    normal_main_percent: int
    risk_main_percent: int
    growth_percent: int
    first_ipo_sessions: int
    published_date: date
    sse_url: str
    szse_url: str


RULE_TABLE = (
    RuleVersion("exchange-trading-2023", date(2023, 4, 10), date(2026, 7, 5),
                10, 5, 20, 5, date(2023, 2, 17), SSE_2023, SZSE_2023),
    RuleVersion("exchange-trading-2026", date(2026, 7, 6), None,
                10, 10, 20, 5, date(2026, 4, 24), SSE_2026, SZSE_2026),
)


def explain_market_rules(*, board: str, st_status: str, as_of_date: date,
                         listing_date: date | None = None, listing_trading_day: int | None = None,
                         special_event: str = "unknown") -> dict[str, object]:
    """Explain ordinary auction rules; no weekday inference or missing-bar suspension guess.

    listing_trading_day must be independently established from an exchange calendar.
    special_event is one of unknown, ordinary, relisting_first_day, delisting_first_day.
    """
    if (type(board) is not str or board not in BOARDS or type(st_status) is not str or st_status not in ST_STATUSES
            or type(as_of_date) is not date or listing_date is not None and type(listing_date) is not date
            or listing_trading_day is not None and (type(listing_trading_day) is not int or listing_trading_day < 1)
            or type(special_event) is not str or special_event not in {"unknown", "ordinary", "relisting_first_day", "delisting_first_day"}
            or listing_trading_day is not None and (listing_date is None or listing_date > as_of_date
                or listing_trading_day > (as_of_date - listing_date).days + 1)):
        raise InvalidRequestError("Invalid dated market-rule facts")
    version = next((rule for rule in RULE_TABLE if rule.effective_from <= as_of_date
                    and (rule.effective_to is None or as_of_date <= rule.effective_to)), None)
    output = {"version": RULES_VERSION, "status": "limited", "as_of_date": as_of_date.isoformat(),
        "rules_checked_through": RULES_CHECKED_THROUGH.isoformat(), "board": board, "st_status": st_status,
        "rule_version": version.version if version else None,
        "policy_effective_from": version.effective_from.isoformat() if version else None,
        "policy_effective_to": version.effective_to.isoformat() if version and version.effective_to else None,
        "policy_published_date": version.published_date.isoformat() if version else None,
        "publication_precision": "day" if version else "unknown",
        "ordinary_daily_limit_percent": None, "applied_daily_limit_percent": None,
        "price_limit_status": "unknown", "listing_trading_day": listing_trading_day,
        "ipo_first_sessions": version.first_ipo_sessions if version else None,
        "ipo_no_limit_applies": None, "same_day_resale": "unknown", "explanations": [], "sources": [],
        "unknowns": [], "tradability": "not_assessed", "execution_allowed": False,
        "scope": "ordinary_a_share_auction_rule_explanation_not_order_validation"}
    if version is None or as_of_date > RULES_CHECKED_THROUGH or board in {"unknown", "bse"}:
        output["unknowns"] = ["rule_version_not_covered" if version is None else
                              "rules_not_verified_for_future_date" if as_of_date > RULES_CHECKED_THROUGH else
                              "board_rule_not_covered"]
        output["explanations"] = ["该日期或板块尚未纳入已核实规则表，保留未知。"]
        return output
    is_sse = board in {"sse_main", "star"}
    source_url = version.sse_url if is_sse else version.szse_url
    output["sources"] = [{"title": "上海证券交易所交易规则" if is_sse else "深圳证券交易所交易规则",
                           "url": source_url, "published_date": version.published_date.isoformat(),
                           "publication_precision": "day", "effective_from": version.effective_from.isoformat()}]
    output["same_day_resale"] = "not_permitted_for_ordinary_a_shares"
    output["explanations"].append("普通A股实行T+1卖出约束：当日买入的股份不能在当日卖出；不据此判断账户可卖余额或资金取用时间。")
    growth = board in {"star", "chinext"}
    rate = version.growth_percent if growth else (version.risk_main_percent if st_status in {"st", "star_st"}
                                                 else version.normal_main_percent if st_status == "not_st"
                                                 or version.normal_main_percent == version.risk_main_percent else None)
    output["ordinary_daily_limit_percent"] = rate
    if growth:
        output["explanations"].append("科创板、创业板普通竞价日的涨跌幅限制为20%；ST或*ST不直接套用主板风险警示比例。")
    elif rate is not None:
        output["explanations"].append(f"该规则版本下，当前提供分类对应的普通竞价日涨跌幅限制为{rate}%。")
    else:
        output["unknowns"].append("historical_st_status_unknown")
        output["explanations"].append("主板普通股票10%，风险警示股票5%；缺少当日ST状态，不能指定其中一个比例。")
    output["explanations"].append("首次公开发行上市后的前5个交易日不设涨跌幅限制；这不表示不受盘中临停或有效申报价格范围约束。")
    if listing_date is not None and listing_date > as_of_date:
        output["price_limit_status"] = "not_yet_listed"
        output["unknowns"].append("analysis_precedes_listing")
    elif special_event in {"relisting_first_day", "delisting_first_day"}:
        output["price_limit_status"] = "no_daily_price_limit_special_event"
        output["explanations"].append("已明确为重新上市首日或进入退市整理期首日，适用无涨跌幅限制的特别情形。")
    elif listing_trading_day is not None and listing_date >= date(2023, 4, 10):
        first_five = listing_trading_day <= version.first_ipo_sessions
        output["ipo_no_limit_applies"] = first_five
        output["price_limit_status"] = "no_daily_price_limit_ipo" if first_five else "ordinary_limit" if special_event == "ordinary" and rate is not None else "conditional_ordinary_limit"
        if not first_five and special_event == "ordinary":
            output["applied_daily_limit_percent"] = rate
    else:
        output["unknowns"].append("ipo_session_ordinal_not_established")
    if special_event == "unknown":
        output["unknowns"].append("special_event_status_unknown")
    if rate is not None and output["price_limit_status"] == "unknown":
        output["price_limit_status"] = "conditional_ordinary_limit"
    if not output["unknowns"]:
        output["status"] = "available"
    return output


def rules_from_market_context(context: dict[str, object]) -> dict[str, object]:
    """Use only the selected analysis-date state, never a current name or missing daily bar."""
    try:
        if (type(context) is not dict or context.get("status") not in {"available", "limited"}
                or type(context.get("security")) is not dict):
            raise ValueError
        state = context["security"]
        as_of = date.fromisoformat(context["as_of_date"])
        listing = date.fromisoformat(state["listing_date"]) if state.get("listing_date") else None
        return explain_market_rules(board=state.get("board", "unknown"),
            st_status=state.get("st_status", "unknown"), as_of_date=as_of, listing_date=listing)
    except (KeyError, TypeError, ValueError):
        raise InvalidRequestError("Validated market context is required for a rule explanation") from None
