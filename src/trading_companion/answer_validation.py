"""Typed, company-scoped sentence checks, not proof of arbitrary prose entailment."""
from dataclasses import dataclass, field
from decimal import Decimal
import re
import unicodedata

STRUCTURE = "answer_structure_invalid"
NUMERIC = "answer_numeric_unmatched"
HARD = "answer_hard_content"
HORIZON = "answer_horizon_mismatch"
STANCE = "answer_stance_mismatch"
CODES = frozenset((STRUCTURE, NUMERIC, HARD, HORIZON, STANCE, "answer_binding_invalid", "answer_binding_value_mismatch", "answer_model_unavailable",
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
    instrument: str = field(default="", repr=False)
    path: str = field(default="", repr=False)
    details: tuple = field(default=(), repr=False)

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


def _rounded(value, original, shown, *, price=False, diagnostic=None):
    quantum = Decimal(1).scaleb(-max(0, -shown.as_tuple().exponent)) / 2
    error = abs(value-original)
    if diagnostic is not None:
        diagnostic.update(difference=str(error), rounding_half_quantum=str(quantum))
    if not price:
        return (value == 0 or original == 0 or (value > 0) == (original > 0)) and error <= quantum
    if original <= 0 or value <= 0:
        return False
    ordinary = min(max(Decimal("0.01"), abs(original)*Decimal("0.005")), abs(original)*Decimal("0.02"))
    if diagnostic is not None:
        diagnostic.update(ordinary_tolerance=str(ordinary), relative_cap=str(abs(original)*Decimal('0.02')),
                          coarse_display_allowed=len(shown.as_tuple().digits)>=2)
    if error <= ordinary:
        return True
    significant = len(shown.as_tuple().digits)
    # Coarse displays additionally have a strict relative cap.
    return significant >= 2 and error <= quantum and error <= abs(original)*Decimal("0.02")


def check_claim(item, *, instrument, context, section, index, path='', stats=None, expected_plan=None):
    """Return clean sentences, associated model IDs, issues and sentence counts."""
    from trading_companion.answer_diagnostics import diagnostic_event, diagnostics_enabled
    from trading_companion.answer_numeric_bindings import bindings_for, check_numbers, resolve_ref, InvalidBinding
    def rejected(code, sentence=None):
        if diagnostics_enabled():
            diagnostic_event('rejected_field',instrument_id=instrument,section=section,item_index=index,
                             path=path or f'{section}[{index}]',value=item,sentence=sentence,code=code)
    if isinstance(item, str):
        item = {"text": item}
    if type(item) is not dict or type(item.get("text")) is not str or not 0 < len(item["text"]) <= 1800:
        rejected(STRUCTURE)
        return "", [], [Issue(STRUCTURE, section, index)], 0, 0
    text = item["text"]
    supplied = {r["id"]: r for r in context["evidence"]}
    own = [r for r in supplied.values() if r["instrument_id"] == instrument]
    if not own:
        rejected(STRUCTURE,text)
        return "", [], [Issue(STRUCTURE, section, index, "本股没有送入模型的可用证据")], 1, 0
    ids = item.get("evidence_ids", [])
    try:
        if type(ids) is not list or len(ids) > 20:
            raise InvalidBinding('invalid_citations', STRUCTURE)
        expanded = []
        for ref in ids:
            _, used = resolve_ref(ref, context, instrument)
            expanded.extend(used)
        ids = list(dict.fromkeys(expanded))
        spans = bindings_for(item["text"], item.get('numbers', []))
    except InvalidBinding as error:
        code = HARD if unsafe(text) else error.code
        rejected(code, text)
        return "", [], [Issue(code, section, index, "" if code == HARD else text)], 1, 0
    records = own
    used_ids = list(ids)
    safe, issues, generated = [], [], 0
    for position, sentence_match in enumerate(re.finditer(r"[^。！？!?\n]+[。！？!?]?", text)):
        sentence = sentence_match[0]
        before = len(issues)
        numeric = []
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
        else:
            code, matched_ids = check_numbers(sentence, records, context, instrument, spans,
                offset=sentence_match.start(), stats=stats, diagnostic=numeric, expected_plan=expected_plan)
            if code:
                issues.append(Issue(code, section, position, "" if code == HARD else sentence, instrument=instrument,
                    path=f'{path or f"{section}[{index}]"}.text.sentence[{position}]', details=tuple(numeric)))
            else:
                safe.append(sentence.strip())
                used_ids.extend(matched_ids)
        if len(issues)>before and diagnostics_enabled():
            diagnostic_event('rejected_sentence',instrument_id=instrument,section=section,item_index=index,
                  path=f'{path or f"{section}[{index}]"}.text.sentence[{position}]',
                  sentence_index=position,sentence=sentence,code=issues[-1].code,numeric=numeric)
    return "".join(safe), list(dict.fromkeys(used_ids or [r["id"] for r in own])), issues, generated, len(safe)
