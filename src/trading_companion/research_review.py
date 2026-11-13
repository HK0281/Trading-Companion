"""Read-only, bounded projections of the original public company answers; no model or sources."""

import base64
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import re

from trading_companion.errors import InvalidRequestError
from trading_companion.memory_store import MemoryStoreError, REVIEW_SCAN_LIMIT
from trading_companion.research_decision import RATINGS
from trading_companion.tencent_adapter import to_tencent_symbol


VERSION = "research-review-v1"
CONTEXT_VERSION = "research-review-context-v1"
SHANGHAI = timezone(timedelta(hours=8))
LEGACY_NOTICE = "旧记录未保存完成时间，起点取所在对话最近保存日期之后，可能遗漏较早走势；继续该对话会使起点后移。"
RESEARCH_CODES = frozenset(("company_research_completed", "company_research_partial", "company_research_unavailable"))
_ERROR_CODES = frozenset(("review_invalid_request", "review_invalid_cursor", "review_not_found", "review_corrupt",
                          "review_store_unavailable"))
_PLAN_METHODS = {"trade-plan-v1": "atr_reference_plan_v1", "trade-plan-v2": "atr_reference_plan_v2"}
_PLAN_VALUES = {
    "conditional_entry": frozenset(("reference_entry", "reference_stop", "target_1", "target_2", "reward_risk_1",
                                    "reward_risk_2", "atr", "atr_multiple")),
    "holding_exit": frozenset(("reference_close", "exit_below")),
    "wait": frozenset(("observe_below", "observe_above")),
}
_PRICE_MESSAGES = {
    "review_prices_ready": "点击后获取回答之后的已完成日线；不会计算收益或判断准确率。",
    "review_completion_unknown": "未记录可用的完成时间或对话保存时间，无法确定后续走势起点。",
    "review_prices_not_yet_available": "该起点之后暂未进入可查询的已完成日线日期。",
    "review_price_basis_unknown": "未记录可核验的原价格口径，无法加载可比走势。",
    "review_price_basis_conflict": "原计划或价格证据存在冲突，无法加载可比走势。",
    "review_adjustment_mismatch": "原价格复权口径未知或与当前不复权日线不同，无法叠加走势。",
    "review_authenticity_unusable": "原价格真实性未知或属于人工、合成资料，无法加载可比走势。",
    "review_instrument_unsupported": "当前日线来源不支持该证券；仍可查看原判断。",
}


class ReviewError(ValueError):
    """Fixed codes only; source questions, SQL errors and paths never become error text."""

    def __init__(self, code):
        self.code = code if type(code) is str and code in _ERROR_CODES else "review_store_unavailable"
        super().__init__(self.code)


def _text(value, limit):
    if type(value) is not str:
        return None
    return " ".join("".join(c if c.isprintable() else " " for c in value).split())[:limit] or None


def _date(value):
    try:
        return date.fromisoformat(value) if type(value) is str and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value) else None
    except ValueError:
        return None


def _time(value):
    try:
        parsed = datetime.fromisoformat(value) if type(value) is str and len(value) <= 100 else None
        return parsed if parsed is not None and parsed.tzinfo is not None and parsed.utcoffset() is not None else None
    except ValueError:
        return None


def _now(value):
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ReviewError("review_invalid_request")
    return value


def _instrument(value):
    if type(value) is not str or re.fullmatch(r"[0-9]{6}\.(?:SH|SZ|BJ)", value) is None:
        raise ReviewError("review_invalid_request")
    return value


def _number(value):
    if type(value) is not str or not 1 <= len(value) <= 80:
        return False
    try:
        number = Decimal(value)
        return number.is_finite() and 0 < number <= 10 ** 15
    except InvalidOperation:
        return False


def _store_problem(error):
    return ReviewError("review_not_found" if error.code == "conversation_not_found" else "review_store_unavailable")


def _filters(instrument_id, from_date, to_date, data_mode, user_id):
    _instrument(instrument_id)
    lower, upper = _date(from_date), _date(to_date)
    if ((from_date is not None and lower is None) or (to_date is not None and upper is None)
            or lower is not None and upper is not None and lower > upper):
        raise ReviewError("review_invalid_request")
    try:
        after = datetime.combine(lower, datetime.min.time(), SHANGHAI).astimezone(timezone.utc) if lower else None
        before = (datetime.combine(upper + timedelta(days=1), datetime.min.time(), SHANGHAI).astimezone(timezone.utc)
                  if upper else None)
    except (ValueError, OverflowError):
        raise ReviewError("review_invalid_request") from None
    fingerprint = sha256(json.dumps([user_id, data_mode, instrument_id, from_date, to_date],
                                    ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()
    return after, before, fingerprint


def _cursor(value, fingerprint):
    if value is None:
        return None
    try:
        if type(value) is not str or not 1 <= len(value) <= 1024 or not value.isascii():
            raise ValueError()
        body = json.loads(base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True))
        if (type(body) is not dict or set(body) != {"version", "filter", "after"}
                or type(body["version"]) is not int or body["version"] != 1
                or body["filter"] != fingerprint or type(body["after"]) is not list or len(body["after"]) != 3):
            raise ValueError()
        stamp, conversation, sequence = body["after"]
        if (type(stamp) is not str or len(stamp) > 100 or type(conversation) is not str
                or len(conversation) > 33 or type(sequence) is not int or not -(2 ** 63) <= sequence < 2 ** 63):
            raise ValueError()
        return tuple(body["after"])
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        raise ReviewError("review_invalid_cursor") from None


def _next_cursor(row, fingerprint):
    body = {"version": 1, "filter": fingerprint,
            "after": [row["asked_at"], row["conversation_id"], row["sequence"]]}
    return base64.urlsafe_b64encode(json.dumps(body, separators=(",", ":")).encode()).decode().rstrip("=")


def _decision(company, instrument_id):
    values = company.get("decisions")
    matches = [d for d in values if type(d) is dict and d.get("instrument_id") == instrument_id] if type(values) is list else []
    if len(matches) != 1:
        return None
    value = matches[0]
    if value.get("status") == "unavailable":
        return value
    if (value.get("status") == "available" and value.get("code") == "decision_ready"
            and type(value.get("rating")) is str and value["rating"] in RATINGS
            and value.get("direction") in RATINGS[value["rating"]][1]
            and value.get("conviction") in ("高", "中", "低")):
        return value
    return None


def _plan(company, decision, instrument_id, cutoff):
    """The technical request id belongs to the plan, never to the outer chat request."""
    plan_id = decision.get("plan_id") if decision else None
    if plan_id is None:
        return None, False
    values = company.get("trade_plans")
    matches = [p for p in values if type(p) is dict and p.get("plan_id") == plan_id] if type(values) is list else []
    if len(matches) != 1 or decision.get("status") != "available":
        return None, True
    plan = matches[0]
    purpose = RATINGS[decision["rating"]][0]
    request_id, method = plan.get("request_id"), plan.get("method")
    day, prices = _date(plan.get("source_date")), plan.get("values")
    if (plan.get("instrument_id") != instrument_id or plan.get("status") != "available"
            or plan.get("origin") != "application" or plan.get("execution_allowed") is not False
            or plan.get("purpose") != purpose or type(plan.get("version")) is not str
            or _PLAN_METHODS.get(plan["version"]) != method
            or type(request_id) is not str or not 1 <= len(request_id) <= 100 or not request_id.isprintable()
            or type(plan_id) is not str or len(plan_id) > 250 or day is None or cutoff is None or day > cutoff
            or type(prices) is not dict or set(prices) != _PLAN_VALUES[purpose]
            or not all(_number(number) for number in prices.values())):
        return None, True
    expected = re.escape(request_id + "|" + method + ":")
    suffix = (r"[0-9a-f]{16}:" if plan["version"] == "trade-plan-v2" else "") + re.escape(purpose)
    if re.fullmatch(expected + suffix, plan_id) is None:
        return None, True
    fields = ("version", "plan_id", "instrument_id", "purpose", "status", "code", "request_id", "source_date", "source",
              "price_adjustment", "data_authenticity", "atr_indicator_id", "method", "horizon", "horizon_profile",
              "atr_timeframe", "atr_source_date", "atr_multiple")
    projected = {key: _text(plan.get(key), 250) for key in fields if key in plan}
    projected.update(origin="application", execution_allowed=False, values=dict(prices))
    for field in ("evidence_ids", "target_multiples", "assumptions"):
        if type(plan.get(field)) is list:
            projected[field] = [_text(v, 300) for v in plan[field][:16] if _text(v, 300) is not None]
    # Conditions are not recomputed from current prices. Keep only the small
    # application condition shape; unexpected nested content is not projected.
    condition = plan.get("condition")
    try:
        if type(condition) is dict and len(json.dumps(condition)) <= 2048:
            projected["condition"] = deepcopy(condition)
    except (ValueError, RecursionError):
        pass
    return projected, False


def _price_evidence(company, instrument_id, cutoff):
    values = company.get("evidence")
    matches = [e for e in values if type(e) is dict and e.get("instrument_id") == instrument_id
               and type(e.get("id")) is str and e["id"].endswith("|price_context:latest_close")] if type(values) is list else []
    if not matches:
        return None, False
    if len(matches) != 1:
        return None, True
    evidence = matches[0]
    basis = evidence.get("calculation_basis")
    day = _date(evidence.get("date"))
    request_id = evidence["id"][:-len("|price_context:latest_close")]
    if (type(basis) is not dict or basis.get("version") != "daily_price_context_v1" or cutoff is None or day is None
            or day > cutoff or basis.get("cutoff") != evidence.get("date") or basis.get("sample_count") != "1"
            or evidence.get("unit") != "CNY/share" or evidence.get("temporal_scope") != "daily_price_context"
            or evidence.get("kind") != "technical" or evidence.get("content_kind") != "fact"
            or not _number(evidence.get("raw_value")) or not 1 <= len(request_id) <= 100
            or evidence.get("dependencies") != [f"{request_id}|raw_daily_bar|{day.isoformat()}|close_price"]):
        return None, True
    return dict(source=evidence.get("source"), price_adjustment=basis.get("price_adjustment"),
                data_authenticity=evidence.get("data_authenticity"), source_date=day.isoformat(),
                technical_request_id=request_id), False


def _prices(row, result, company, decision, plan, plan_conflict, instrument_id, cutoff, now):
    context = result.get("review_context")
    is_new = context is not None
    completed = (_time(context.get("completed_at")) if type(context) is dict
                 and context.get("version") == CONTEXT_VERSION and context.get("event") == "company_research" else None)
    stamp = completed if is_new else _time(row.get("conversation_updated_at"))
    asked = _time(row.get("asked_at"))
    valid_time = stamp is not None and asked is not None and asked <= stamp <= now
    output = dict(enabled=False, code="review_completion_unknown", message="", start_date=None, end_date=None,
                  basis=("completed_at" if is_new else "conversation_updated_at") if valid_time else None,
                  basis_at=stamp.isoformat() if valid_time else None, notice=None if is_new else LEGACY_NOTICE,
                  source=None, price_adjustment=None, data_authenticity=None, source_date=None, technical_request_id=None)
    evidence, evidence_conflict = _price_evidence(company, instrument_id, cutoff)
    original = ({key: plan.get(key) for key in ("source", "price_adjustment", "data_authenticity", "source_date")}
                | {"technical_request_id": plan["request_id"]}) if plan else evidence
    conflict = plan_conflict or evidence_conflict
    if plan and evidence and any(original[key] != evidence[key] for key in original):
        conflict = True
    if original:
        output.update({key: _text(value, 100) for key, value in original.items()})
    code = "review_completion_unknown"
    if valid_time:
        try:
            start = stamp.astimezone(SHANGHAI).date() + timedelta(days=1)
            end = min(now.astimezone(SHANGHAI).date() - timedelta(days=1), start + timedelta(days=365))
            output.update(start_date=start.isoformat(), end_date=end.isoformat())
        except (ValueError, OverflowError):
            valid_time = False
    if valid_time:
        code = "review_prices_ready"
        try:
            to_tencent_symbol(instrument_id)
        except InvalidRequestError:
            code = "review_instrument_unsupported"
        if code == "review_prices_ready":
            if conflict:
                code = "review_price_basis_conflict"
            elif original is not None and original.get("source") == "csv":
                code = "review_authenticity_unusable"
            elif original is None or original.get("source") not in ("tencent", "akshare_eastmoney"):
                code = "review_price_basis_unknown"
            elif original.get("data_authenticity") != "provider_reported":
                code = "review_authenticity_unusable"
            elif original.get("price_adjustment") != "unadjusted":
                code = "review_adjustment_mismatch"
            elif start > end:
                code = "review_prices_not_yet_available"
    output.update(enabled=code == "review_prices_ready", code=code, message=_PRICE_MESSAGES[code])
    return output, completed.isoformat() if completed is not None and valid_time else None


def project_review(row, *, instrument_id, now):
    """Project one requested security, retaining missing old fields rather than guessing prose."""
    _instrument(instrument_id)
    _now(now)
    if row.get("invalid") or type(row.get("result")) is not dict:
        raise ReviewError("review_corrupt")
    result = row["result"]
    if type(result.get("code")) is not str or result["code"] not in RESEARCH_CODES:
        return None
    company = result.get("company_research")
    if type(company) is not dict:
        return None
    securities = company.get("securities")
    matches = [s for s in securities if type(s) is dict and s.get("instrument_id") == instrument_id] if type(securities) is list else []
    if not matches:
        return None
    if len(matches) != 1 or _time(row.get("asked_at")) is None:
        raise ReviewError("review_corrupt")
    decision = _decision(company, instrument_id)
    cutoff = _date(company.get("daily_cutoff"))
    plan, conflict = _plan(company, decision, instrument_id, cutoff)
    prices, completed = _prices(row, result, company, decision, plan, conflict, instrument_id, cutoff, now)
    historical = company.get("explicit_historical")
    return dict(conversation_id=row["conversation_id"], sequence=row["sequence"], request_id=row["request_id"],
                instrument_id=instrument_id, name=_text(matches[0].get("name"), 80) or instrument_id,
                question=_text(row.get("question"), 160) or "", asked_at=row["asked_at"],
                daily_cutoff=cutoff.isoformat() if cutoff else None, completed_at=completed,
                historical_type="historical" if historical is True else "current" if historical is False else "unknown",
                status=decision["status"] if decision else "unrecorded",
                code=_text(decision.get("code"), 100) if decision else None,
                rating=decision.get("rating") if decision and decision["status"] == "available" else None,
                direction=decision.get("direction") if decision and decision["status"] == "available" else None,
                conviction=decision.get("conviction") if decision and decision["status"] == "available" else None,
                horizon=_text(decision.get("horizon"), 160) if decision else None, trade_plan=plan, prices=prices)


def list_reviews(store, user_id, *, data_mode, instrument_id, from_date=None, to_date=None, cursor=None, now):
    _now(now)
    lower, upper, fingerprint = _filters(instrument_id, from_date, to_date, data_mode, user_id)
    after = _cursor(cursor, fingerprint)
    output = dict(version=VERSION, enabled=store is not None and data_mode != "synthetic", items=[], scanned=0,
                  skipped_invalid=0, has_more=False, next_cursor=None)
    if not output["enabled"]:
        return output
    try:
        page = store.scan_results(user_id, data_mode=data_mode, asked_from=lower, asked_before=upper,
                                  after=after, limit=REVIEW_SCAN_LIMIT)
    except MemoryStoreError as error:
        raise _store_problem(error) from None
    rows = page["rows"]
    for row in rows:
        output["scanned"] += 1
        try:
            item = project_review(row, instrument_id=instrument_id, now=now)
        except ReviewError:
            output["skipped_invalid"] += 1
            continue
        if item is not None:
            output["items"].append(item)
    output["has_more"] = bool(page["has_more"])
    if rows and output["has_more"]:
        output["next_cursor"] = _next_cursor(rows[-1], fingerprint)
    return output


def load_review(store, user_id, *, data_mode, conversation_id, sequence, request_id, instrument_id, now):
    _instrument(instrument_id)
    _now(now)
    if store is None or data_mode == "synthetic":
        raise ReviewError("review_not_found")
    try:
        row = store.get_result(user_id, conversation_id, sequence=sequence, request_id=request_id, data_mode=data_mode)
    except MemoryStoreError as error:
        raise _store_problem(error) from None
    item = project_review(row, instrument_id=instrument_id, now=now)
    if item is None:
        raise ReviewError("review_not_found")
    return dict(version=VERSION, item=item, question=row["question"], result=row["result"])
