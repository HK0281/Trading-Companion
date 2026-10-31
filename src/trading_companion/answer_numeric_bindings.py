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
RELAXATION_COUNTERS = ('ignored_nonnumeric_bindings', 'ignored_binding_fields',
    'invalid_binding_fallbacks', 'invalid_binding_fallback_matched', 'invalid_binding_fallback_rejected',
    'rsi_local_value_checks', 'rsi_reference_explanations')
CLAUSE_COUNTERS = ('generated_clauses', 'retained_clauses', 'deleted_clauses', 'partially_retained_sentences')
UNITS = {'CNY/share': 'price', 'cny': 'amount', 'percent': 'percent',
         'percentage_points': 'points', 'ratio': 'ratio', 'shares': 'shares', 'count': 'count',
         'index_points': 'index', 'index_point': 'index_price'}
PLAN_UNITS = {key: 'CNY/share' for key in ('reference_entry', 'reference_stop', 'target_1', 'target_2',
              'reference_close', 'exit_below', 'observe_below', 'observe_above', 'atr')}
PLAN_UNITS.update(reward_risk_1='ratio', reward_risk_2='ratio', atr_multiple='ratio')
PLAN_UNITS.update(reference_target='CNY/share', risk_per_share='CNY/share', reward_per_share='CNY/share',
                  risk_pct='percent', reward_risk='ratio', effective_reward_risk='ratio')
FIELDS = {'营业收入': 'revenue', '归母净利润': 'parent_net_profit', '营收同比': 'revenue_yoy',
          '归母净利润同比': 'parent_net_profit_yoy', '加权净资产收益率': 'weighted_roe',
          '单季营业收入': 'quarter_revenue', '单季归母净利润': 'quarter_parent_net_profit',
          '单季营业收入同比': 'quarter_revenue_yoy', '单季归母净利润同比': 'quarter_parent_net_profit_yoy',
          '最新收盘价': 'latest_close', '收盘价': 'close_price', 'MA20': 'ma20', 'MA60': 'ma60'}
for _window in (5, 20, 60):
    FIELDS[f'{_window}日前收盘价'] = f'close_{_window}d_ago'
    FIELDS[f'近{_window}日涨跌幅'] = f'return_{_window}d'
    for _side, _label in (('high', '最高'), ('low', '最低')):
        FIELDS[f'近{_window}日{_label}价'] = f'{_side}_{_window}d'
PLAN_LABELS = {'止损': 'reference_stop', '止盈1': 'target_1', '止盈2': 'target_2',
               '入场参考': 'reference_entry', '入场价': 'reference_entry', '退出观察位': 'exit_below',
               '下方观察位': 'observe_below', '上方观察位': 'observe_above',
               '买入价': 'reference_entry', '退出价': 'exit_below', '目标价1': 'target_1',
               '目标价2': 'target_2', '目标价': 'target', '目标价格': 'target', '止盈': 'target',
               '参考目标': 'target', '参考止损': 'reference_stop', '入场位': 'reference_entry',
               '买入价格': 'reference_entry', '退出价格': 'exit_below'}
_LABEL = '|'.join(map(re.escape, sorted(PLAN_LABELS, key=len, reverse=True)))
PLAN_PREFIX = re.compile(r'(' + _LABEL + r')\s*(?:价(?:格|位)?|为|是|约|在|[:：]|\*\*)*\s*$')
PLAN_LANGUAGE = re.compile(r'止损|止盈|目标价|参考目标|买入价|入场价|入场位|入场参考|退出价|退出观察位|[上下]方观察位')
PLAN_SUFFIX = re.compile(r'^\s*(?:附近|一带)?\s*(?:是|为|的|作为|作)?\s*(?:计划|参考)?(' + _LABEL + r')')


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
        return [Atom(value, UNITS[unit], identity, field,
                     record.get('plan_id') if record.get('content_kind') == 'index_scenario' else None)]
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


def bindings_for(text, numbers, *, context=None, instrument=None, stats=None):
    """Locate exact spans; ignore nondata annotations, but never foreign citations."""
    if type(numbers) is not list or len(numbers) > 80:
        raise InvalidBinding('invalid_numbers_array')
    spans = []
    for binding in numbers:
        if type(binding) is not dict or type(binding.get('text')) is not str or not binding['text']:
            raise InvalidBinding('invalid_binding_text')
        fragment = unicodedata.normalize('NFKC', binding['text'])
        fragment = re.sub(r'(?:VWMA|VOLUME|MACD|BOLL|SMA|EMA|RSI|ATR|ADX|OBV|MA)\s*(?:\([^)]*\)|\d+)', '', fragment, flags=re.I)
        fragment = re.sub(DATE, '', fragment)
        fragment = re.sub(r'\d+\s*(?:个交易日|交易日|日|天|周|个月|月|年)', '', fragment)
        if not NUMBER.search(fragment):
            if context is not None and 'ref' in binding:
                try:
                    resolve_ref(binding['ref'], context, instrument)
                except InvalidBinding as error:
                    if error.code == HARD:
                        raise
            if stats is not None:
                stats['ignored_nonnumeric_bindings'] = stats.get('ignored_nonnumeric_bindings', 0) + 1
            continue
        found = list(re.finditer(re.escape(binding['text']), text))
        if not found:
            if (context is not None and context.get('instrument_kind') == 'index'
                    and not set(binding) - {'text', 'ref', 'field'}):
                # Recover only display-equivalent ordinary index numbers,
                # retaining the explicit ref, field and unit checks below.
                from trading_companion.evidence_numbers import same_display_number
                candidates, _ = _bound_atoms(binding, context, instrument, strict=True)
                literal = re.fullmatch(r'\s*([-+]?\d+(?:\.\d+)?)\s*(点)?\s*', binding['text'])
                if (literal and candidates and all(a.plan_id is None and a.kind in ('index', 'index_price') for a in candidates)
                        and any(same_display_number(literal[1], a.value) for a in candidates)):
                    forms = {m[0] for m in re.finditer(r'(?<![\d.])[-+]?\d+(?:\.\d+)?(?:点)?', text)
                             if (not literal[2] or m[0].endswith('点'))
                             and same_display_number(m[0].removesuffix('点'), literal[1])}
                    if len(forms) == 1:
                        binding = {**binding, 'text': forms.pop()}
                        found = list(re.finditer(re.escape(binding['text']), text))
                    elif not forms:
                        # A valid same-index annotation absent from the prose is
                        # metadata, not a reason to discard otherwise checked text.
                        # Unknown/foreign refs, wrong fields, units and values
                        # cannot reach here because they were checked above.
                        if stats is not None:
                            stats['ignored_unused_index_bindings'] = stats.get('ignored_unused_index_bindings', 0) + 1
                        continue
            if not found:
                raise InvalidBinding('binding_text_not_in_block')
        for match in found:
            invalid = any(match.start() < end and match.end() > start
                          and (match.start(), match.end()) != (start, end) for start, end, _, _ in spans)
            if invalid:
                spans = [(s, e, b, bad or (match.start() < e and match.end() > s
                         and (match.start(), match.end()) != (s, e))) for s, e, b, bad in spans]
            spans.append((match.start(), match.end(), binding, invalid))
    return spans


def _masked(text, context, records):
    """Mask structural dates, identifiers and periods, retaining string positions."""
    normalized = unicodedata.normalize('NFKC', text).replace('−', '-')
    # NFKC can change length (e.g. a ligature); binding offsets use normalized slices separately.
    masked = normalized
    if context.get('instrument_kind') == 'index':
        for name in sorted(context.get('identity_names', ()), key=len, reverse=True):
            masked = masked.replace(name, ' ' * len(name))
        window_label = '最近至多120条日线'
        if any(r.get('instrument_id') == context.get('instrument_id')
               and r.get('field') in ('window_low', 'window_high') and window_label in r.get('label', '')
               for r in records):
            # This exact cap is application-authored evidence metadata. Other
            # counts, asserted sample sizes and ordinary values stay visible.
            masked = re.sub(re.escape(window_label), lambda m: ' ' * len(m[0]), masked)
        if context.get('_index_scenario'):
            masked = re.sub(r'(?:止盈|目标|实际盈亏比)\s*[12](?!\d)',
                            lambda m: ' ' * len(m[0]), masked)
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


def _bound_atoms(binding, context, instrument, *, strict=False, stats=None):
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
            if strict or any(a.plan_id is not None for a in candidates):
                raise InvalidBinding('field_not_supplied')
            if stats is not None:
                stats['ignored_binding_fields'] = stats.get('ignored_binding_fields', 0) + 1
        else:
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
    from trading_companion.answer_technical_validation import mask_technical, reading_candidates, InvalidTechnical
    try:
        normalized, masked = _masked(sentence, context, records)
        technical_mask, technical_ids = mask_technical(normalized, context, instrument, records, stats=stats)
        restrictions = reading_candidates(normalized, context, instrument, records)
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
    index_context = context.get('instrument_kind') == 'index'
    def matches(value, atom, shown, diagnostic=None):
        if index_context and atom.plan_id is not None:
            return value == atom.value
        if index_context and atom.kind in ('index', 'index_price'):
            from trading_companion.evidence_numbers import same_display_number
            return same_display_number(value, atom.value)
        return _rounded(value, atom.value, shown, price=atom.kind == 'price', diagnostic=diagnostic)
    token_pattern = re.compile(NUMBER.pattern.replace('亿股|万股|股)?', '亿股|万股|股|点)?')) if index_context else NUMBER
    tokens = list(token_pattern.finditer(masked))
    for start, end, binding, bad in local_spans:
        if bad:
            return reject('ambiguous_overlapping_binding', BINDING_INVALID)
        if not any(start <= m.start() and m.start()+len(m[0].rstrip()) <= end for m in tokens):
            # A full-block date/period annotation may become nonnumeric only after masking.
            try:
                resolve_ref(binding.get('ref'), context, instrument)
            except InvalidBinding as error:
                if error.code == HARD:
                    return reject(error.reason, HARD)
            count('ignored_nonnumeric_bindings')
    all_atoms = [a for record in records for a in atoms(record)]
    used, code = list(technical_ids), None
    for match in tokens:
        count('numeric_tokens')
        bindings = [b for s, e, b, _ in local_spans
                    if s <= match.start() and match.start()+len(match[0].rstrip()) <= e]
        if bindings:
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
                'yuan' if suffix == '元' else 'index_price' if suffix == '点' and index_context else None)
        value, direction_error = _direction(normalized[:match.start()], split[1], shown * FACTOR.get(suffix, 1), kind)
        detail = dict(token=token, shown=str(shown), value=str(value), kind=kind, unit=suffix,
                      start=match.start(), end=match.end(), binding=bool(bindings), candidates=[])
        label = PLAN_PREFIX.search(normalized[:match.start()])
        suffix_label = PLAN_SUFFIX.search(normalized[match.end():])
        plan_field = PLAN_LABELS[label[1]] if label else PLAN_LABELS[suffix_label[1]] if suffix_label else None
        if index_context and context.get('_index_scenario'):
            labels = {'入场参考': 'reference_entry', '止损参考': 'reference_stop', '跌破观察位': 'reference_stop',
                '止损': 'reference_stop', '止盈1': 'target_1', '目标1': 'target_1',
                '止盈2': 'target_2', '目标2': 'target_2', '实际盈亏比1': 'reward_risk_1',
                '实际盈亏比2': 'reward_risk_2', '下方观察位': 'observe_below', '上方观察位': 'observe_above',
                '收盘参考': 'reference_close', '退出观察位': 'exit_below', '跌破止损参考': 'exit_below'}
            prefix = re.sub(r'\s+', '', normalized[:match.start()])
            prefix = re.sub(r'\((?:跌破观察位|目标[12]|跌破止损参考)\)', '', prefix)
            found = re.search('('+'|'.join(sorted(labels, key=len, reverse=True))
                              +r')(?:为|是|约|在|[:：])*(跌破|低于|高于|站上)?$', prefix)
            if found:
                plan_field = labels[found[1]]
                if found[2] and plan_field not in (
                        ('reference_stop', 'exit_below', 'observe_below') if found[2] in ('跌破', '低于')
                        else ('reference_entry', 'target_1', 'target_2', 'observe_above')):
                    return reject('index_scenario_direction_conflict', BINDING_INVALID)
            if plan_field is None:
                return reject('index_scenario_field_required', BINDING_INVALID)
        # A coordinated historical low in the same comma clause is not another
        # plan price: "跌破51.58元计划止损且失守50.11元低点".
        boundary = r'[，,；;。]|并且|以及|且|并|和'
        local = re.split(boundary, normalized[:match.start()])[-1] + re.split(boundary, normalized[match.end():])[0]
        level_binding = any(type(b.get('ref')) is str and re.fullmatch(r'L[1-9]\d*', b['ref']) for b in bindings)
        protected = bool(plan_field or expected_plan or kind in ('price', 'yuan', None) and PLAN_LANGUAGE.search(local))
        detail['ordinary_numeric'] = not protected
        matched, binding_ids, error_code, passed, ambiguous = [], [], None, True, False
        for binding in bindings or [None]:
            candidates, group_ids, fallback = all_atoms, [], False
            try:
                if binding is not None:
                    candidates, group_ids = _bound_atoms(binding, context, instrument,
                        strict=protected or index_context, stats=stats)
            except InvalidBinding as error:
                if (not index_context and not protected and not level_binding and binding.get('derive') is None and error.reason in
                        ('invalid_ref_type', 'ref_not_supplied', 'ref_has_no_numeric_value')):
                    candidates = [a for a in all_atoms if a.plan_id is None]
                    fallback = True
                    count('invalid_binding_fallbacks')
                    detail['fallback_reason'] = error.reason
                else:
                    candidates = []
                    error_code = HARD if error.code == HARD or error_code == HARD else error_code or error.code
                    detail['reason'] = error.reason
                    detail['ordinary_numeric'] = False
            restriction = restrictions.get(match.start())
            if index_context:
                role = None
                before = normalized[:match.start()]
                if re.search(r'收盘(?:点位|价)?\s*(?:为|是|约|在|[:：])?\s*$', before):
                    role = 'close_price'
                else:
                    window = re.search(r'(?:研究窗口|窗口|研究区间)[^，,；;。]{0,35}'
                                       r'(最低点|低点|最高点|高点)(?:为|是|约|在|[:：])?\s*$', before)
                    if window:
                        role = 'window_low' if window[1] in ('最低点', '低点') else 'window_high'
                if role:
                    role_refs = {a.evidence_id for a in all_atoms if a.field == role}
                    restriction = (role_refs, set())
                if restriction is not None and not protected and not error_code and binding is not None:
                    expected = [a for a in all_atoms if a.evidence_id in set.union(*restriction)
                        and a.plan_id is None and matches(value, a, shown)]
                    # Reconcile a misplaced citation only when the prose itself
                    # names a checked field/indicator/period with this exact value.
                    # Explicit fields, derivations and unknown refs remain strict.
                    if (expected and not binding.get('derive') and 'field' not in binding
                            and all(a.plan_id is None for a in candidates)
                            and not any(a.evidence_id in set.union(*restriction) and matches(value, a, shown)
                                        for a in candidates)):
                        candidates, group_ids = expected, []
                        count('index_context_binding_recoveries')
            if index_context and restriction is None:
                # Dimensionless RSI/ADX readings cannot become an index price merely
                # because the same number appears in evidence.
                candidates = [a for a in candidates if a.kind != 'index']
            # A dated close can stand for an entry only when it is EXACTLY this
            # stock's supplied reference_entry. Stops/targets retain their roles.
            if plan_field == 'reference_entry':
                entries = [a for a in all_atoms if a.plan_id is not None and a.field == 'reference_entry'
                           and a.value == value and (expected_plan is None or a.plan_id == expected_plan)]
                evidence = {r['id']: r for r in context['evidence'] if r['instrument_id'] == instrument}
                expanded = []
                for atom in candidates:
                    expanded.append(atom)
                    if (atom.plan_id is None and atom.kind == 'price' and atom.field in ('latest_close', 'close_price')
                            and atom.value == value and evidence.get(atom.evidence_id, {}).get('content_kind') == 'fact'):
                        for entry in entries:
                            if evidence[atom.evidence_id].get('date') == evidence.get(entry.evidence_id, {}).get('date'):
                                expanded.append(Atom(atom.value, atom.kind, atom.evidence_id, 'reference_entry', entry.plan_id,
                                                     source='close_equal_reference_entry'))
                candidates = expanded
            if restriction is not None:
                daily, weekly = restriction
                allowed = daily if any(a.evidence_id in daily and matches(value, a, shown)
                                       for a in candidates) else weekly
                candidates = [a for a in candidates if a.evidence_id in allowed]
                if index_context and suffix == '点' and candidates and all(a.kind == 'index' for a in candidates):
                    kind = 'index'
            group = []
            for atom in candidates:
                d = dict(evidence_id=atom.evidence_id, value=str(atom.value), kind=atom.kind,
                         field=atom.field, source=atom.source, difference=str(abs(value-atom.value)))
                detail['candidates'].append(d)
                field_ok = (atom.field in ('target_1', 'target_2', 'reference_target') if plan_field == 'target'
                            else atom.field == plan_field)
                if kind == 'yuan' and atom.kind not in ('price', 'amount') or kind not in (None, 'yuan', atom.kind):
                    d['reason'] = 'unit_kind_mismatch'
                elif plan_field and (not field_ok or atom.plan_id is None):
                    d['reason'] = 'plan_field_mismatch'
                elif protected and atom.kind in (('price', 'index_price') if index_context else ('price',)) and atom.plan_id is None:
                    d['reason'] = 'plan_source_required'
                elif expected_plan and atom.kind in (('price', 'index_price', 'ratio') if index_context else ('price',)) and atom.plan_id != expected_plan:
                    d['reason'] = 'selected_plan_mismatch'
                elif direction_error:
                    d['reason'] = direction_error
                else:
                    if atom.kind == 'amount':
                        tolerance = Decimal(1).scaleb(shown.as_tuple().exponent) * FACTOR.get(suffix, 1) / 2
                        ok = abs(value-atom.value) <= tolerance and (value == 0 or atom.value == 0 or (value > 0) == (atom.value > 0))
                    else:
                        ok = matches(value, atom, shown, diagnostic=d)
                    d['reason'] = 'matched' if ok else 'numeric_out_of_tolerance'
                    if ok:
                        group.append(atom)
            exact = [a for a in group if a.value == value]
            group = exact or group
            group_distinct = {(a.value, a.kind) for a in group}
            group_ok = len(group_distinct) == 1
            ambiguous |= len(group_distinct) > 1
            passed &= group_ok
            if fallback:
                count('invalid_binding_fallback_matched' if group_ok else 'invalid_binding_fallback_rejected')
            if group_ok:
                matched.extend(group)
                binding_ids.extend(group_ids)
        distinct = {(a.value, a.kind) for a in matched}
        if not passed:
            if not bindings:
                count('unbound_rejected')
                if ambiguous:
                    count('unbound_ambiguous')
            else:
                count('bound_rejected')
            error_code = error_code or (BINDING_MISMATCH if bindings else NUMERIC)
            detail.setdefault('reason', 'ambiguous_match' if ambiguous else
                              'binding_value_mismatch' if bindings and distinct else direction_error or 'no_candidate')
            # A later hard violation must not be hidden by an earlier soft failure.
            code = HARD if error_code == HARD or code == HARD else code or error_code
        else:
            count('bound_matched' if bindings else 'unbound_matched')
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
