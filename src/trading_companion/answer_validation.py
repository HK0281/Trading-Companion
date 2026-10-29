"""Typed, company-scoped sentence checks, not proof of arbitrary prose entailment."""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
import re
import unicodedata

STRUCTURE = "answer_structure_invalid"
NUMERIC = "answer_numeric_unmatched"
HARD = "answer_hard_content"
HORIZON = "answer_horizon_mismatch"
STANCE = "answer_stance_mismatch"
CODES = frozenset((STRUCTURE, NUMERIC, HARD, HORIZON, STANCE, "answer_model_unavailable",
                   "answer_context_unusable", "answer_partial", "answer_facts_only", "answer_ready"))
SECTIONS = frozenset(("summary", "current", "drivers", "levels", "scenarios", "stance", "decision", "structure"))
NUMBER = re.compile(r"(?<![0-9.])[-+]?\d+(?:\.\d+)?\s*(?:亿元|万元|元/股|元|%|％|个百分点|倍|亿股|万股|股)?")
DATE = re.compile(r"(?<!\d)(\d{4})[-年](\d{1,2})[-月](\d{1,2})日?(?!\d)")
MONEY = re.compile(r"([-+]?\d+(?:\.\d+)?)\s*(亿元|万元|元/股|元|%|％|个百分点|倍|亿股|万股|股)")
FACTOR = {"亿元": Decimal(100000000), "万元": Decimal(10000), "亿股": Decimal(100000000),
          "万股": Decimal(10000)}


@dataclass(frozen=True)
class Issue:
    code: str
    section: str
    index: int
    text: str = ""

    @property
    def hard(self):
        return self.code == HARD

    def public(self):
        # Deliberately never serialize the rejected sentence.
        return {"code": self.code, "section": self.section if self.section in SECTIONS else "structure",
                "index": max(0, min(self.index, 100))}


def normalize_horizon(value):
    if not isinstance(value, str):
        return None
    value = unicodedata.normalize("NFKC", value).replace(" ", "")
    value = re.sub(r"^(?:未来|接下来|今后)", "", value)
    if value in ("下个月", "下月"):
        return (1, "月")
    digits = {"一": "1", "二": "2", "两": "2", "三": "3", "四": "4", "五": "5", "六": "6"}
    for original, replacement in digits.items():
        value = value.replace(original, replacement)
    match = re.fullmatch(r"(\d+)(?:个)?(交易日|天|日|周|月|年)", value)
    if match:
        return (int(match[1]), "日" if match[2] == "天" else match[2])
    return value or None


def unsafe(text):
    from trading_companion.product_answer import _HTML, _LINK, _OUTPUT_INJECTION, _positive_claim
    return bool(_HTML.search(text) or _LINK.search(text) or _OUTPUT_INJECTION.search(text)
                or _positive_claim(text) or re.search(r"(?:概率|胜率).{0,8}[一二三四五六七八九十百]+(?:成|%|％)|百分之[一二三四五六七八九十百]+.{0,8}(?:概率|胜率)", text))


def _role(text):
    if re.search(r"止损|失效价", text): return "stop"
    if re.search(r"止盈\s*[1一]", text): return "target_1"
    if re.search(r"止盈\s*[2二]", text): return "target_2"
    if re.search(r"参考目标|目标价", text): return "target"
    if re.search(r"退出观察", text): return "exit_below"
    if re.search(r"下方观察", text): return "observe_below"
    if re.search(r"上方观察", text): return "observe_above"
    if re.search(r"入场|买入价|历史参考价", text): return "entry"
    financial = list(re.finditer(r"营收|收入|利润", text))
    if financial:
        role = "profit" if financial[-1][0] == "利润" else "revenue"
        return "quarter_" + role if "单季" in text else role
    for n in (20, 60):
        chinese = "二十" if n == 20 else "六十"
        if re.search(rf"MA\s*{n}|(?:{n}|{chinese})\s*日(?:均线|均价)", text, re.I): return f"ma{n}"
    if re.search(r"最高|高点", text): return "high"
    if re.search(r"最低|低点", text): return "low"
    if re.search(r"收盘|现价|当前价", text): return "close"
    if re.search(r"涨跌幅|涨幅|跌幅|上涨|下跌|回落|涨了|跌了", text): return "return"
    if "ATR" in text.upper(): return "atr"
    return None


def _atoms(record):
    unit = record.get("unit")
    raw = record.get("raw_value")
    role = _role(record.get("label", ""))
    try:
        value = Decimal(str(raw))
        if value.is_finite() and unit in ("CNY/share", "cny", "percent", "ratio", "shares"):
            return [(value, {"CNY/share": "price", "cny": "amount", "percent": "percent",
                             "ratio": "ratio", "shares": "shares"}[unit], role)]
    except (InvalidOperation, ValueError):
        pass
    result = []
    display = record.get("display", "")
    for m in MONEY.finditer(display):
        value, suffix = Decimal(m[1]), m[2]
        kind = ("percent" if suffix in ("%", "％", "个百分点") else "amount" if suffix in ("亿元", "万元")
                or unit == "cny" else "ratio" if suffix == "倍" else "shares" if "股" in suffix and suffix != "元/股" else "price")
        local_role = _role(re.split(r"[；;，,]", display[:m.start()])[-1]) if record.get("content_kind") in ("trade_plan", "scenario") else role
        result.append((value * FACTOR.get(suffix, 1), kind, local_role))
    return result


def _rounded(value, original, shown, *, price=False):
    quantum = Decimal(1).scaleb(-max(0, -shown.as_tuple().exponent)) / 2
    error = abs(value-original)
    if not price:
        return (value == 0 or original == 0 or (value > 0) == (original > 0)) and error <= quantum
    if original <= 0 or value <= 0:
        return False
    ordinary = min(max(Decimal("0.01"), abs(original)*Decimal("0.005")), abs(original)*Decimal("0.02"))
    if error <= ordinary:
        return True
    significant = len(shown.as_tuple().digits)
    # Coarse displays additionally have a strict relative cap.
    return significant >= 2 and error <= quantum and error <= abs(original)*Decimal("0.02")


def _numeric_match(sentence, records, context):
    normalized = unicodedata.normalize("NFKC", sentence).replace("−", "-")
    # Unsupported representations are removed rather than silently treated as prose.
    if re.search(r"(?<![\d.])[零〇一二两三四五六七八九十百千万亿]+(?:亿元|万元|元|个百分点|倍|成)|百分之|美元|港元|欧元|USD|HKD", normalized, re.I):
        return False
    if records and all(r.get("temporal_scope") in ("completed_week_derived", "cross_timeframe_derived") for r in records):
        if re.search(r"前一交易日|上一交易日|\d+日(?:均线|均价)", normalized):
            return False
    dated = []
    for match in DATE.finditer(normalized):
        try:
            day = date(*(int(x) for x in match.groups())).isoformat()
        except ValueError:
            return False
        if not context["start_date"] <= day <= context["daily_cutoff"] and not any(
                day in (r.get("date"), r.get("report_period")) for r in records):
            return False
        dated.append(day)
    # Keep dates as opaque markers so each number binds to its nearest preceding date.
    normalized = DATE.sub(lambda m: " DATE_" + str(dated.pop(0)) + " ", normalized)
    date_markers = re.findall(r"DATE_(\d{4}-\d{2}-\d{2})", normalized)
    if len(date_markers) > 26:
        return False
    for i, day in enumerate(date_markers):
        normalized = normalized.replace("DATE_" + day, "DATE" + chr(65+i), 1)
    # A cycle is not a price. Instrument codes must still match this company's records.
    for match in re.finditer(r"\b\d{6}(?:\.(?:SH|SZ|BJ))?\b", normalized, re.I):
        if not any(str(r.get("instrument_id", "")).startswith(match[0].upper()) for r in records):
            return False
    normalized = re.sub(r"\b\d{6}(?:\.(?:SH|SZ|BJ))?\b", " 证券 ", normalized, flags=re.I)
    normalized = re.sub(r"(?:S?MA)\s*\(?20\)?", "二十日均线", normalized, flags=re.I)
    normalized = re.sub(r"(?:S?MA)\s*\(?60\)?", "六十日均线", normalized, flags=re.I)
    normalized = re.sub(r"20\s*日(?:均线|均价)", "二十日均线", normalized)
    normalized = re.sub(r"60\s*日(?:均线|均价)", "六十日均线", normalized)
    normalized = re.sub(r"(?:MA|SMA|EMA|RSI|ATR|ADX|BOLL|VWMA)\s*\(?\d+(?:[,，]\d+(?:\.\d+)?)*\)?", " 指标 ", normalized, flags=re.I)
    normalized = re.sub(r"\d+\s*(?:个交易日|交易日|日|天|周|个月|月|年)(?![\u4e00-\u9fff]*元)", " 周期 ", normalized)
    normalized = re.sub(r"止盈\s*1(?!\d)", "止盈一", normalized)
    normalized = re.sub(r"止盈\s*2(?!\d)", "止盈二", normalized)
    normalized = re.sub(r"(?<=\d)\s*-\s*(?=\d)", "—", normalized)
    for interval in re.finditer(r"(?<![\d.])([+-]?\d+(?:\.\d+)?)\s*(元|亿元|万元|%)?\s*(?:—|–|~|至|到)\s*([+-]?\d+(?:\.\d+)?)\s*(元|亿元|万元|%)", normalized):
        left_unit, right_unit = interval[2] or interval[4], interval[4]
        if Decimal(interval[1])*FACTOR.get(left_unit, 1) > Decimal(interval[3])*FACTOR.get(right_unit, 1):
            return False
    atoms = [(value, kind, role, record) for record in records for value, kind, role in _atoms(record)]
    for match in NUMBER.finditer(normalized):
        token = match[0].strip()
        m = re.fullmatch(r"([-+]?\d+(?:\.\d+)?)\s*(.*)", token)
        shown, suffix = Decimal(m[1]), m[2]
        if not suffix:
            # In a range the first endpoint inherits the explicitly written unit.
            tail = normalized[match.end():]
            interval = re.match(r"\s*(?:—|–|~|至|到|-)\s*\d+(?:\.\d+)?\s*(元|%|亿元|万元)", tail)
            suffix = interval[1] if interval else ""
        kind = ("percent" if suffix in ("%", "个百分点") else "amount" if suffix in ("亿元", "万元")
                else "ratio" if suffix == "倍" else "shares" if suffix in ("股", "万股", "亿股")
                else "price" if suffix in ("元", "元/股") else None)
        value = shown * FACTOR.get(suffix, 1)
        local = re.split(r"[，,；;。]", normalized[:match.start()])[-1][-28:]
        role = _role(local)
        if suffix == "元" and role in ("revenue", "profit", "quarter_revenue", "quarter_profit"):
            kind = "amount"
        direction = re.search(r"(?:下跌|回落|(?<!涨)跌幅|下降|减少|下滑)\s*(?:约|了|为)?\s*$", local)
        if kind == "percent" and direction and shown >= 0:
            value = -value
        if kind == "percent" and re.search(r"(?:上涨|增长|增加|涨幅)\s*(?:约|了|为)?\s*$", local) and shown < 0:
            return False
        preceding_dates = re.findall(r"DATE([A-Z])", normalized[:match.start()])
        fact_date = date_markers[ord(preceding_dates[-1])-65] if preceding_dates else None
        if kind is None:
            kind = ("percent" if "同比" in local else "amount") if role in ("revenue", "profit", "quarter_revenue", "quarter_profit") else "price" if role in (
                "close", "high", "low", "stop", "entry", "target_1", "target_2", "ma20", "ma60", "atr") else None
            if kind is None:
                return False
        matched = False
        for original, expected_kind, expected_role, record in atoms:
            if kind is not None and kind != expected_kind:
                continue
            if role and expected_role and role != expected_role:
                continue
            if role in ("revenue", "profit", "quarter_revenue", "quarter_profit", "ma20", "ma60", "close", "return") and not expected_role:
                continue
            if role in ("stop", "entry", "target_1", "target_2", "exit_below", "observe_below", "observe_above") and role != expected_role:
                continue
            if ("同比" in local) != ("同比" in record.get("label", "")) and kind == "percent":
                continue
            if role in ("revenue", "profit", "quarter_revenue", "quarter_profit") and not fact_date:
                latest = max((r.get("report_period") or "" for r in records if _role(r.get("label", "")) == role), default="")
                if latest and record.get("report_period") != latest:
                    continue
            if role == "close" and not fact_date:
                historical = re.search(r"(\d+)\s*(?:个交易日|交易日|日)前", sentence)
                if historical:
                    if historical[1] + "日前收盘" not in record.get("label", ""):
                        continue
                else:
                    latest = max((r.get("date") or "" for r in records if _role(r.get("label", "")) == "close"), default="")
                    if latest and record.get("date") != latest:
                        continue
            if fact_date and fact_date not in (record.get("date"), record.get("report_period")):
                continue
            if suffix == "个百分点" and record.get("unit") != "percentage_points":
                continue
            # A 5-day return cannot supply a 20-day return of an accidentally equal value.
            window = re.search(r"近\s*(\d+)\s*(?:日|交易日)", sentence)
            if window and role in ("return", "high", "low") and not re.search(r"近" + window[1] + r"日", record.get("label", "")):
                continue
            if expected_kind == "amount":
                error = Decimal(1).scaleb(shown.as_tuple().exponent) * FACTOR.get(suffix, 1) / 2
                ok = abs(value-original) <= error and (value == 0 or original == 0 or (value > 0) == (original > 0))
            else:
                ok = _rounded(value, original, shown, price=expected_kind == "price")
            if ok:
                matched = True
                break
        if not matched:
            return False
    return True


def check_claim(item, *, instrument, context, section, index):
    """Return clean sentences, associated model IDs, issues and sentence counts."""
    if isinstance(item, str):
        item = {"text": item}
    if type(item) is not dict or type(item.get("text")) is not str or not 0 < len(item["text"]) <= 1800:
        return "", [], [Issue(STRUCTURE, section, index)], 0, 0
    text = item["text"].strip()
    supplied = {r["id"]: r for r in context["evidence"]}
    own = [r for r in supplied.values() if r["instrument_id"] == instrument]
    if not own:
        return "", [], [Issue(STRUCTURE, section, index, "本股没有送入模型的可用证据")], 1, 0
    ids = item.get("evidence_ids", [])
    if type(ids) is not list or len(ids) > 20 or any(type(i) is not str or i not in supplied for i in ids):
        return "", [], [Issue(HARD if unsafe(text) else STRUCTURE, section, index, "" if unsafe(text) else text)], 1, 0
    if any(supplied[i]["instrument_id"] != instrument for i in ids):
        return "", [], [Issue(HARD, section, index)], 1, 0
    records = [supplied[i] for i in ids] if ids else own
    safe, issues, generated = [], [], 0
    for position, sentence in enumerate(re.findall(r"[^。！？!?\n]+[。！？!?]?", text)):
        generated += 1
        wrong_company = any(s["instrument_id"] != instrument and (s["name"] in sentence or s["instrument_id"] in sentence)
                            for s in context["securities"])
        codes = re.findall(r"(?<!\d)\d{6}\.(?:SH|SZ|BJ)\b", sentence, re.I)
        wrong_company |= any(c.upper() != instrument for c in codes)
        mentioned_codes = re.findall(r"(?:股票|证券|代码)\s*(\d{6})(?!\d)", sentence)
        wrong_company |= any(c != instrument[:6] for c in mentioned_codes)
        wrong_company |= any(s["instrument_id"] != instrument and re.search(
            r"(?<!\d)" + re.escape(s["instrument_id"][:6]) + r"(?![\d.元万亿%])", sentence) for s in context["securities"])
        if unsafe(sentence) or wrong_company:
            issues.append(Issue(HARD, section, position))
        elif any(not ch.isprintable() and ch != "\t" for ch in sentence):
            issues.append(Issue(STRUCTURE, section, position, sentence))
        elif context.get("objective", {}).get("horizon") and any(
                normalize_horizon(h) != normalize_horizon(context["objective"]["horizon"])
                for h in re.findall(r"(?:未来|接下来|今后)\s*[一二两三四五六七八九十\d]+\s*(?:个交易日|交易日|天|日|周|个月|月|年)|下个月", sentence)):
            issues.append(Issue(HORIZON, section, position, sentence))
        elif not _numeric_match(sentence, records, context):
            issues.append(Issue(NUMERIC, section, position, sentence))
        else:
            safe.append(sentence.strip())
    return "".join(safe), list(dict.fromkeys(ids or [r["id"] for r in own])), issues, generated, len(safe)
