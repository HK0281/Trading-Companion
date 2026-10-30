"""Source-bound numbers and a unit-aware fallback, without prose role inference."""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
import re
import unicodedata

from trading_companion.answer_validation import DATE, FACTOR, HARD, MONEY, NUMBER, NUMERIC, _rounded

BINDING_INVALID = 'answer_binding_invalid'
BINDING_MISMATCH = 'answer_binding_value_mismatch'
COUNTERS = ('numeric_tokens', 'bound_numbers', 'bound_matched', 'bound_rejected',
            'unbound_matched', 'unbound_rejected', 'unbound_ambiguous')
UNITS = {'CNY/share': 'price', 'cny': 'amount', 'percent': 'percent',
         'percentage_points': 'points', 'ratio': 'ratio', 'shares': 'shares', 'count': 'count',
         'index_points': 'index'}
PLAN_UNITS = {key: 'CNY/share' for key in ('reference_entry', 'reference_stop', 'target_1', 'target_2',
              'reference_close', 'exit_below', 'observe_below', 'observe_above', 'atr')}
PLAN_UNITS.update(reward_risk_1='ratio', reward_risk_2='ratio', atr_multiple='ratio')
PLAN_UNITS.update(reference_target='CNY/share', risk_per_share='CNY/share', reward_per_share='CNY/share',
                  risk_pct='percent', reward_risk='ratio', effective_reward_risk='ratio')
FIELDS = {'营业收入': 'revenue', '归母净利润': 'parent_net_profit', '营收同比': 'revenue_yoy',
          '归母净利润同比': 'parent_net_profit_yoy', '加权净资产收益率': 'weighted_roe',
          '单季营业收入': 'quarter_revenue', '单季归母净利润': 'quarter_parent_net_profit',
          '单季营业收入同比': 'quarter_revenue_yoy', '单季归母净利润同比': 'quarter_parent_net_profit_yoy',
          '最新收盘价': 'latest_close', 'MA20': 'ma20', 'MA60': 'ma60'}
for _window in (5, 20, 60):
    FIELDS[f'{_window}日前收盘价'] = f'close_{_window}d_ago'
    FIELDS[f'近{_window}日涨跌幅'] = f'return_{_window}d'
    for _side, _label in (('high', '最高'), ('low', '最低')):
        FIELDS[f'近{_window}日{_label}价'] = f'{_side}_{_window}d'
PLAN_LABELS = {'止损': 'reference_stop', '止盈1': 'target_1', '止盈2': 'target_2',
               '入场参考': 'reference_entry', '入场价': 'reference_entry', '退出观察位': 'exit_below',
               '下方观察位': 'observe_below', '上方观察位': 'observe_above'}
_LABEL = '|'.join(map(re.escape, PLAN_LABELS))
PLAN_PREFIX = re.compile(r'(' + _LABEL + r')\s*(?:价(?:格|位)?|为|是|约|在|[:：]|\*\*)*\s*$')


@dataclass(frozen=True)
class Atom:
    value: Decimal
    kind: str
    evidence_id: str
    field: str | None = None
    plan_id: str | None = None
    source: str = 'record'


class InvalidBinding(ValueError):
    def __init__(self, reason, code=BINDING_INVALID):
        self.reason, self.code = reason, code


def _decimal(value):
    if type(value) not in (str, int, float, Decimal):
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except InvalidOperation:
        return None


def atoms(record):
    """Only trusted structured plan values, never numbers parsed from plan prose."""
    identity = record['id']
    if record.get('content_kind') == 'trade_plan' or record.get('content_kind') == 'scenario' and record.get('values'):
        return [Atom(value, UNITS[PLAN_UNITS[key]], identity, key, record.get('plan_id') or identity)
                for key, raw in record.get('values', {}).items() if key in PLAN_UNITS
                and (value := _decimal(raw)) is not None]
    value = _decimal(record.get('raw_value'))
    unit = record.get('unit')
    field = record.get('field') or FIELDS.get(record.get('label'))
    if value is not None and unit in UNITS:
        return [Atom(value, UNITS[unit], identity, field)]
    # For title records only the supplied (possibly truncated) title is available.
    title = record.get('kind') in ('news', 'announcements')
    display = unicodedata.normalize('NFKC', record.get('display', ''))
    result = []
    for match in MONEY.finditer(display):
        suffix = match[2]
        kind = ('points' if suffix == '个百分点' else 'percent' if suffix == '%' else
                'amount' if suffix in ('亿元', '万元') or unit == 'cny' else
                'ratio' if suffix == '倍' else 'shares' if suffix in ('股', '万股', '亿股') else 'price')
        value = Decimal(match[1]) * FACTOR.get(suffix, 1)
        value, error = _direction(display[:match.start()], match[1], value, kind)
        if not error:
            result.append(Atom(value, kind, identity, field, source='title' if title else 'display'))
    return result


def resolve_ref(ref, context, instrument):
    if type(ref) is not str:
        raise InvalidBinding('invalid_ref_type')
    evidence = {r['id']: r for r in context['evidence']}
    level = next((r for r in context.get('price_levels', ()) if r['id'] == ref), None)
    if level and level['instrument_id'] != instrument:
        raise InvalidBinding('cross_instrument_ref', HARD)
    if ref in evidence:
        record = evidence[ref]
        if record['instrument_id'] != instrument:
            raise InvalidBinding('cross_instrument_ref', HARD)
        return atoms(record), [ref]
    if not level or level.get('evidence_id') not in evidence:
        raise InvalidBinding('ref_not_supplied')
    record = evidence[level['evidence_id']]
    if record['instrument_id'] != instrument:
        raise InvalidBinding('cross_instrument_ref', HARD)
    candidates = [a for a in atoms(record) if a.field == level.get('field') and a.kind == 'price'
                  and a.value == _decimal(level.get('price'))]
    if not candidates:
        raise InvalidBinding('level_not_backed_by_supplied_record')
    return candidates, [record['id']]


def level_ref(ref, context, instrument):
    """Map an E endpoint only when it denotes exactly one supplied price level."""
    resolve_ref(ref, context, instrument)  # Includes stock identity and supply checks.
    direct = [r for r in context.get('price_levels', ()) if r['id'] == ref]
    if direct:
        return direct[0]
    matches = [r for r in context.get('price_levels', ()) if r.get('evidence_id') == ref
               and r['instrument_id'] == instrument]
    if len(matches) != 1:
        raise InvalidBinding('ambiguous_or_nonprice_endpoint')
    return matches[0]


def bindings_for(text, numbers):
    """Locate exact substrings before normalization. Never downgrade a bad binding."""
    if type(numbers) is not list or len(numbers) > 80:
        raise InvalidBinding('invalid_numbers_array')
    spans = []
    for binding in numbers:
        if type(binding) is not dict or type(binding.get('text')) is not str or not binding['text']:
            raise InvalidBinding('invalid_binding_text')
        found = list(re.finditer(re.escape(binding['text']), text))
        if not found:
            raise InvalidBinding('binding_text_not_in_block')
        for match in found:
            invalid = len(found) != 1 or any(match.start() < end and match.end() > start
                                           for start, end, _, _ in spans)
            if invalid:
                spans = [(s, e, b, bad or (match.start() < e and match.end() > s)) for s, e, b, bad in spans]
            spans.append((match.start(), match.end(), binding, invalid))
    return spans


def _masked(text, context, records):
    """Mask structural dates, identifiers and periods, retaining string positions."""
    normalized = unicodedata.normalize('NFKC', text).replace('−', '-')
    # NFKC can change length (e.g. a ligature); binding offsets use normalized slices separately.
    masked = normalized
    for match in DATE.finditer(normalized):
        try:
            day = date(*(int(x) for x in match.groups())).isoformat()
        except ValueError:
            raise InvalidBinding('invalid_date', NUMERIC)
        if not context['start_date'] <= day <= context['daily_cutoff'] and not any(
                day in (r.get('date'), r.get('report_period')) for r in records):
            raise InvalidBinding('date_outside_evidence', NUMERIC)
    patterns = (DATE.pattern, r'(?<!\d)\d{4}\s*年',
                r'(?<!\d)(?:1\s*[-—至]\s*(?:3|6|9|12)|[1-9]|1[0-2])\s*月(?!\s*元)',
                r'\d+\s*(?:个交易日|交易日|日|天|周|个月|月|年)(?![\u4e00-\u9fff]*元)',
                r'(?<=止盈)[12](?!\d)', r'(?<=盈亏比)[12](?!\d)', r'(?<=第)[1234](?=季度)')
    for pattern in patterns:
        masked = re.sub(pattern, lambda m: ' ' * len(m[0]), masked, flags=re.I)
    def mask_code(match):
        return ' ' * len(match[0]) if any(str(r.get('instrument_id', '')).startswith(match[0].upper())
                                         for r in records) else match[0]
    masked = re.sub(r'(?<!\d)\d{6}(?:\.(?:SH|SZ|BJ))?(?![\d.元万亿%])', mask_code, masked, flags=re.I)
    masked = re.sub(r'(?<=\d)(\s*)-(?=\s*\d)', lambda m: m[1]+'—', masked)
    return normalized, masked


def _direction(prefix, shown_text, value, kind):
    if kind != 'percent':
        return value, None
    local = re.split(r'[，,、；;。]', prefix)[-1][-20:]
    negative = re.search(r'(?:下跌|(?<!涨)跌幅|跌|回落|下降|减少|下滑|降)\s*(?:约|了|为|至)?\s*$', local)
    positive = re.search(r'(?:上涨|(?<!跌)涨幅|涨|回升|增长|增加|增)\s*(?:约|了|为|至)?\s*$', local)
    if shown_text.startswith(('+', '-')):
        return value, 'direction_sign_conflict' if (negative and value > 0 or positive and value < 0) else None
    return (-value if negative else value), None


def _bound_atoms(binding, context, instrument):
    if binding.get('derive') is not None:
        if (binding.get('derive') != 'distance' or set(binding) != {'text', 'derive', 'refs'}
                or type(binding.get('refs')) is not list or len(binding['refs']) != 2):
            raise InvalidBinding('unsupported_derivation')
        values, ids = [], []
        for ref in binding['refs']:
            candidates, used = resolve_ref(ref, context, instrument)
            prices = {a.value for a in candidates if a.kind == 'price'}
            if len(prices) != 1:
                raise InvalidBinding('ambiguous_distance_endpoint')
            values.append(prices.pop())
            ids.extend(used)
        if any(v <= 0 for v in values):
            raise InvalidBinding('invalid_distance_denominator_or_price')
        return [Atom(abs(values[1]-values[0])/abs(values[0])*100, 'percent', ids[0], source='distance')], ids
    if set(binding) - {'text', 'ref', 'field'} or 'ref' not in binding:
        raise InvalidBinding('invalid_binding_fields')
    candidates, ids = resolve_ref(binding['ref'], context, instrument)
    if 'field' in binding:
        field = binding['field']
        if type(field) is not str or not field or not any(a.field == field for a in candidates):
            raise InvalidBinding('field_not_supplied')
        candidates = [a for a in candidates if a.field == field]
    if not candidates:
        raise InvalidBinding('ref_has_no_numeric_value')
    return candidates, ids


def check_numbers(sentence, records, context, instrument, spans=(), *, offset=0, stats=None,
                  diagnostic=None, expected_plan=None):
    """Return a rejection code and the actual matched evidence IDs."""
    def count(key):
        if stats is not None:
            stats[key] = stats.get(key, 0) + 1
    def reject(reason, code=NUMERIC):
        if diagnostic is not None:
            diagnostic.append(dict(stage='sentence', reason=reason))
        return code, []
    if re.search(r'(?<![\d.])[零〇一二两三四五六七八九十百千万亿]+(?:亿元|万元|元|个百分点|倍|成)|百分之|美元|港元|欧元|USD|HKD', sentence, re.I):
        return reject('unsupported_number_representation_or_currency')
    from trading_companion.answer_technical_validation import mask_technical, InvalidTechnical
    try:
        normalized, masked = _masked(sentence, context, records)
        technical_mask, technical_ids = mask_technical(normalized, context, instrument, records)
        masked = ''.join(' ' if technical == ' ' else original
                         for original, technical in zip(masked, technical_mask, strict=True))
    except InvalidBinding as error:
        return reject(error.reason, error.code)
    except InvalidTechnical as error:
        return reject(str(error))
    local_spans = []
    for start, end, binding, bad in spans:
        if start < offset+len(sentence) and end > offset:
            # Normalize prefixes so offsets remain exact even with NFKC expansion.
            left = len(unicodedata.normalize('NFKC', sentence[:max(0, start-offset)]))
            right = len(unicodedata.normalize('NFKC', sentence[:end-offset]))
            local_spans.append((left, right, binding, bad or start < offset or end > offset+len(sentence)))
    tokens = list(NUMBER.finditer(masked))
    for start, end, binding, bad in local_spans:
        if bad or not any(start <= m.start() and m.start()+len(m[0].rstrip()) <= end for m in tokens):
            return reject('ambiguous_overlapping_or_nonnumeric_binding', BINDING_INVALID)
    all_atoms = [a for record in records for a in atoms(record)]
    used, code = list(technical_ids), None
    for match in tokens:
        count('numeric_tokens')
        binding = next((b for s, e, b, _ in local_spans
                        if s <= match.start() and match.start()+len(match[0].rstrip()) <= e), None)
        if binding is not None:
            count('bound_numbers')
        token = match[0].strip()
        split = re.fullmatch(r'([-+]?\d+(?:\.\d+)?)\s*(.*)', token)
        shown, suffix = Decimal(split[1]), split[2]
        if not suffix:
            interval = re.match(r'\s*[-—–~至到]\s*\d+(?:\.\d+)?\s*(元|%|亿元|万元)', masked[match.end():])
            suffix = interval[1] if interval else ''
        kind = ('percent' if suffix == '%' else 'points' if suffix == '个百分点' else
                'amount' if suffix in ('亿元', '万元') else 'ratio' if suffix == '倍' else
                'shares' if suffix in ('股', '万股', '亿股') else 'price' if suffix == '元/股' else
                'yuan' if suffix == '元' else None)
        value, direction_error = _direction(normalized[:match.start()], split[1], shown * FACTOR.get(suffix, 1), kind)
        detail = dict(token=token, shown=str(shown), value=str(value), kind=kind, unit=suffix,
                      binding=binding is not None, candidates=[])
        candidates = all_atoms
        binding_ids = []
        error_code = None
        try:
            if binding is not None:
                candidates, binding_ids = _bound_atoms(binding, context, instrument)
        except InvalidBinding as error:
            candidates, error_code = [], error.code
            detail['reason'] = error.reason
        label = PLAN_PREFIX.search(normalized[:match.start()])
        plan_field = PLAN_LABELS[label[1]] if label else None
        matched = []
        for atom in candidates:
            d = dict(evidence_id=atom.evidence_id, value=str(atom.value), kind=atom.kind,
                     field=atom.field, source=atom.source, difference=str(abs(value-atom.value)))
            detail['candidates'].append(d)
            if kind == 'yuan' and atom.kind not in ('price', 'amount') or kind not in (None, 'yuan', atom.kind):
                d['reason'] = 'unit_kind_mismatch'
            elif plan_field and (atom.field != plan_field or atom.plan_id is None):
                d['reason'] = 'plan_field_mismatch'
            elif expected_plan and atom.kind == 'price' and atom.plan_id != expected_plan:
                d['reason'] = 'selected_plan_mismatch'
            elif direction_error:
                d['reason'] = direction_error
            else:
                if atom.kind == 'amount':
                    tolerance = Decimal(1).scaleb(shown.as_tuple().exponent) * FACTOR.get(suffix, 1) / 2
                    ok = abs(value-atom.value) <= tolerance and (value == 0 or atom.value == 0 or (value > 0) == (atom.value > 0))
                else:
                    ok = _rounded(value, atom.value, shown, price=atom.kind == 'price', diagnostic=d)
                d['reason'] = 'matched' if ok else 'numeric_out_of_tolerance'
                if ok:
                    matched.append(atom)
        # Exact values disambiguate tolerance neighbours. Otherwise distinct values
        # within a tolerance are ambiguous; duplicate records aren't.
        exact = [a for a in matched if a.value == value]
        if exact:
            matched = exact
        distinct = {(a.value, a.kind) for a in matched}
        passed = len(distinct) == 1
        if not passed:
            if binding is None:
                count('unbound_rejected')
                if len(distinct) > 1:
                    count('unbound_ambiguous')
            else:
                count('bound_rejected')
            error_code = error_code or (BINDING_MISMATCH if binding is not None else NUMERIC)
            detail.setdefault('reason', 'ambiguous_match' if distinct else direction_error or 'no_candidate')
            # A later hard violation must not be hidden by an earlier soft failure.
            code = HARD if error_code == HARD or code == HARD else code or error_code
        else:
            count('bound_matched' if binding is not None else 'unbound_matched')
            used.extend(binding_ids)
            used.extend(a.evidence_id for a in matched)
        detail['matched'] = passed
        detail['candidates'] = sorted(detail['candidates'], key=lambda d: (d['reason'] != 'matched', Decimal(d['difference'])))[:3]
        if diagnostic is not None:
            diagnostic.append(detail)
    # Both interval endpoints were checked independently above; order must also hold.
    for m in re.finditer(r'(?<![\d.])([+\-]?\d+(?:\.\d+)?)\s*(元|亿元|万元|%)?\s*[-—–~至到]\s*([+\-]?\d+(?:\.\d+)?)\s*(元|亿元|万元|%)', masked):
        if Decimal(m[1])*FACTOR.get(m[2] or m[4], 1) > Decimal(m[3])*FACTOR.get(m[4], 1):
            return reject('reversed_interval', code or NUMERIC)
    return code, list(dict.fromkeys(used))
