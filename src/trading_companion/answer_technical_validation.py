"""Exact indicator parameters and bounded RSI predicates against latest supplied facts."""
from decimal import Decimal, InvalidOperation
import re

from trading_companion.multi_contracts import IndicatorSpec, PARAMETER_DEFAULTS
from trading_companion.errors import InvalidRequestError

_MENTION = re.compile(r'(?<![A-Za-z0-9_])(?:(?P<timeframe>日线|周线)(?:的)?\s*)?'
    r'(?P<kind>VWMA|VOLUME|MACD|BOLL|SMA|EMA|RSI|ATR|ADX|OBV|MA)(?![A-Za-z_])'
    r'(?P<parameters>(?!\s+[+\-]?\d+(?:\.\d+)?\s*(?:元|点|%|倍))\s*(?:\([^)]*(?:\)|$)|[+\-]?\d+(?:\.\d+)?))?', re.I)
_PREFIX = re.compile(r'(?<![\d.+\-])(?P<window>[+\-]?\d+(?:\.\d+)?)\s*(?P<period>日|天|周)\s*'
    r'(?P<kind>指数移动平均线|简单移动平均线|均线|VWMA|SMA|EMA|RSI|ATR|ADX|MA)'
    r'(?![A-Za-z_])', re.I)
_DIRECTION = re.compile(r'(?P<op>高于|低于|大于|小于|超过)\s*(?P<line>30|50|70)(?![\d.%元])')
_RANGE = re.compile(r'(?:处于|位于|处|在)\s*(?P<low>30|50|70)\s*(?:至|到|—|~|-)\s*'
                    r'(?P<high>30|50|70)\s*(?:区间|之间|内)(?![\d.%元])')
_EXTREME = re.compile(r'超买|超卖')
_UNSUPPORTED = re.compile(r'若|如果|假如|一旦|未来|可能|将|此前|昨日|上周|曾|并非|不高于|不低于|没有|未|不超过')
_READING = re.compile(r'\(\s*[+\-]?\d+(?:\.\d+)?\s*(?:元(?:/股)?|点|%|倍)\s*\)')
_CROSS_PREFIX = re.compile(r'日线(?:与|和)(?:最近)?完整周线均(?:位于|在)(?:同参数)?\s*$')


class InvalidTechnical(ValueError):
    pass


def _predicate_true(relation, value):
    if not value.is_finite() or not 0 <= value <= 100:
        return False
    if relation.re is _DIRECTION:
        line = Decimal(relation['line'])
        return value < line if relation['op'] in ('低于', '小于') else value > line
    if relation.re is _RANGE:
        low, high = Decimal(relation['low']), Decimal(relation['high'])
        return low < high and low <= value <= high
    return value > 70 if relation[0] == '超买' else value < 30


def technical_context(bundle, context):
    """Keep checked fact identities locally; never send this sidecar to the model."""
    from trading_companion.research_report import build_research_report
    from trading_companion.technical_insights import build_technical_insights
    from trading_companion.research_price_context import price_context_evidence
    supplied = {row['id']: row for row in context['evidence']}
    by_original = {original.id: supplied[f'E{i}'] for i, original in enumerate(bundle.evidence, 1)
                   if f'E{i}' in supplied}
    output = {}
    for company in bundle.companies:
        confirmed = (tuple(spec.indicator_id for spec in bundle.plan.indicators)
                     if 'technical' in bundle.plan.kinds or bundle.plan.wants_risk else ())
        values, local_rsi = [], []
        result = company.technical
        if (result and result.request.instrument_id == company.instrument_id
                and result.request.indicators == bundle.plan.indicators
                and result.request.start_date == bundle.plan.start_date
                and result.request.as_of_date == bundle.plan.end_date):
            report = build_research_report(result, expected_request=result.request)
            # Background means are already-calculated daily evidence, not extra
            # requested indicators. Recompute their provenance, then require that
            # the exact record was actually supplied to this answer.
            if report.status in ('complete', 'limited'):
                for mean in price_context_evidence(result, allow_extra_means=not bundle.plan.indicators_explicit):
                    match = re.fullmatch(re.escape(result.request.request_id) + r'\|price_context:ma(20|60)', mean.id)
                    row = by_original.get(mean.id)
                    if (match and row and row['instrument_id'] == company.instrument_id
                            and row.get('label') == mean.label and row.get('raw_value') == mean.raw_value
                            and row.get('unit') == mean.unit and row.get('date') == mean.date
                            and row.get('temporal_scope') == 'daily_price_context'):
                        values.append(dict(indicator_id=f'sma(window={match[1]})', timeframe='daily',
                            field='value', value=str(mean.raw_value), date=mean.date, ref=row['id'],
                            source='price_context'))
            for field in report.fields:
                row = by_original.get(field.fact_id)
                if (report.status in ('complete', 'limited') and field.status == 'available'
                        and field.indicator_id in confirmed and field.indicator_id.startswith('rsi(')
                        and field.field == 'value' and field.unit == 'index_points'
                        and field.trade_date == result.latest_trade_date.isoformat() and field.fact_id):
                    local_rsi.append(dict(indicator_id=field.indicator_id, timeframe='daily', field='value',
                        value=str(field.value), date=field.trade_date, ref=field.fact_id,
                        instrument_id=company.instrument_id, source='local_computed_rsi'))
                if (field.status == 'available' and field.indicator_id in confirmed and row
                        and row['instrument_id'] == company.instrument_id
                        and row.get('raw_value') == field.value and row.get('unit') == field.unit
                        and row.get('date') == field.trade_date):
                    values.append(dict(indicator_id=field.indicator_id, timeframe='daily',
                        field=field.field, value=str(field.value), date=field.trade_date, ref=row['id']))
            insights = (build_technical_insights(result, expected_request=result.request)
                        if company.technical_insights else None)
            if report.status in ('complete', 'limited') and insights and insights.weekly_bars:
                latest = insights.weekly_bars[-1].trade_date.isoformat()
                for item in insights.observations:
                    row = by_original.get(item.dependencies[0]) if item.dependencies else None
                    if (item.timeframe == 'weekly' and item.status == 'available'
                            and item.indicator_id in confirmed and item.indicator_id.startswith('rsi(')
                            and item.trade_date.isoformat() == latest and item.unit == 'index_points'
                            and item.dependencies):
                        local_rsi.append(dict(indicator_id=item.indicator_id, timeframe='weekly', field=item.field,
                            value=str(item.value), date=latest, ref=item.dependencies[0],
                            instrument_id=company.instrument_id, source='local_computed_rsi'))
                    if (item.category == 'alignment' and item.timeframe == 'cross_timeframe'
                            and item.status == 'available' and item.indicator_id in confirmed
                            and item.value in ('aligned_above', 'aligned_below') and row
                            and row['instrument_id'] == company.instrument_id
                            and row.get('raw_value') == item.value
                            and row.get('date') == item.trade_date.isoformat()
                            and row.get('temporal_scope') == 'cross_timeframe_derived'):
                        values.append(dict(indicator_id=item.indicator_id, timeframe='cross_timeframe',
                            field='alignment', value=item.value, date=row['date'], ref=row['id']))
                    if (item.timeframe == 'weekly' and item.status == 'available'
                            and item.indicator_id in confirmed and item.trade_date.isoformat() == latest
                            and type(item.value) in (int, float) and row
                            and row['instrument_id'] == company.instrument_id
                            and row.get('raw_value') == item.value and row.get('unit') == item.unit
                            and row.get('date') == latest and row.get('temporal_scope') == 'completed_week_derived'):
                        values.append(dict(indicator_id=item.indicator_id, timeframe='weekly',
                            field=item.field, value=str(item.value), date=latest, ref=row['id']))
        output[company.instrument_id] = dict(confirmed=confirmed, values=values, local_rsi=local_rsi)
    return output


def _spec(match):
    kind = match['kind'].lower()
    kind = 'sma' if kind == 'ma' else kind
    raw = (match['parameters'] or '').strip()
    if not raw or _READING.fullmatch(raw):
        return None
    if raw.startswith('('):
        if not raw.endswith(')'):
            raise InvalidTechnical('incomplete_indicator_parameters')
        raw = raw[1:-1]
    names = tuple(PARAMETER_DEFAULTS[kind])
    parts = [part.strip() for part in raw.split(',')]
    try:
        if any('=' in part for part in parts):
            pairs = [part.split('=') for part in parts]
            if (any(len(pair) != 2 for pair in pairs) or len({p[0].strip() for p in pairs}) != len(pairs)):
                raise ValueError
            parameters = {name.strip(): float(value) if name.strip() == 'stddev_multiplier' else int(value)
                          for name, value in pairs}
            if set(parameters) != set(names):
                raise ValueError
        else:
            if len(parts) != len(names):
                raise ValueError
            parameters = {name: float(value) if name == 'stddev_multiplier' else int(value)
                          for name, value in zip(names, parts)}
        return IndicatorSpec(kind, **parameters).indicator_id
    except (ValueError, TypeError, InvalidRequestError):
        raise InvalidTechnical('invalid_indicator_parameters') from None


def mask_technical(text, context, instrument, records, *, stats=None):
    """Return same-length text with only proven parameters/predicate constants masked."""
    own = context.get('_technical_validation', {}).get(instrument, {})
    mentions = list(_MENTION.finditer(text))
    masked = list(text)
    instances = {}
    supplied = {r['id']: r for r in records}
    values = [v for v in own.get('values', ()) if v['ref'] in supplied
              and supplied[v['ref']].get('instrument_id') == instrument
              and supplied[v['ref']].get('date') == v['date']
              and str(supplied[v['ref']].get('raw_value')) == v['value']]
    confirmed = set(own.get('confirmed', ())) | {
        v['indicator_id'] for v in values if v.get('source') == 'price_context' and v['timeframe'] == 'daily'}
    local_rsi = [v for v in own.get('local_rsi', ()) if v.get('instrument_id') == instrument
                 and v.get('source') == 'local_computed_rsi' and v.get('indicator_id') in confirmed
                 and v.get('timeframe') in ('daily', 'weekly') and v.get('field') == 'value'
                 and context['start_date'] <= v.get('date', '') <= context['daily_cutoff']]
    relation_values = values + local_rsi
    from trading_companion.answer_clauses import clause_spans
    clauses = clause_spans(text)
    prefixes = {}
    for prefix in _PREFIX.finditer(text):
        # English indicators may carry their own following parameters (20天RSI6).
        # Chinese mean names instead allow the actual reading immediately after them.
        if prefix['kind'].isascii() and re.match(r'\s*[\d(]', text[prefix.end():]):
            continue
        if prefix['period'] != '周' and re.search(r'(?:最近|近|过去)\s*$', text[:prefix.start()]):
            continue
        kind = {'均线': 'sma', '简单移动平均线': 'sma', '指数移动平均线': 'ema',
                'ma': 'sma'}.get(prefix['kind'].lower(), prefix['kind'].lower())
        try:
            spec = IndicatorSpec(kind, window=int(prefix['window'])).indicator_id
        except (ValueError, InvalidRequestError):
            raise InvalidTechnical('invalid_indicator_parameters') from None
        timeframe = 'weekly' if prefix['period'] == '周' or re.search(r'周线(?:的)?\s*$', text[:prefix.start()]) else 'daily'
        if spec not in confirmed or timeframe == 'weekly' and not any(
                v['indicator_id'] == spec and v['timeframe'] == timeframe for v in values):
            raise InvalidTechnical('prefix_indicator_not_confirmed_or_unavailable')
        prefixes[prefix.start('kind')] = (spec, timeframe)
        masked[prefix.start():prefix.end()] = ' ' * (prefix.end() - prefix.start())
    timeframes = {}
    used = []
    for match in mentions:
        suffix = text[match.end():]
        # A complete, unit-bearing reading in parentheses is data, not another
        # parameter list. Leave it visible for the ordinary numeric checks.
        reading = _READING.match(suffix)
        following_reading = re.match(r'\s*(?:约|为|是)?\s*[+\-]?\d+(?:\.\d+)?\s*(?:元(?:/股)?|点|%)', suffix)
        if match['parameters'] and (re.match(r'[A-Za-z0-9_.]|元|点|%|倍', suffix) and not (match['parameters'].endswith(')') and following_reading)
                                    or suffix.startswith('(') and not reading):
            raise InvalidTechnical('indicator_parameter_suffix_invalid')
        spec, timeframe = prefixes.get(match.start('kind'),
            (_spec(match), 'weekly' if match['timeframe'] == '周线' else 'daily'))
        if not match['timeframe'] and _CROSS_PREFIX.search(text[:match.start()]):
            direction = re.match(r'(上方|下方)(?=[，,。；;！!?？]|$)', suffix)
            candidates = [v for v in values if v['indicator_id'] == spec and v['field'] == 'alignment'
                          and v['timeframe'] == 'cross_timeframe'
                          and v['value'] == ('aligned_above' if direction and direction[1] == '上方' else 'aligned_below')]
            if (not direction or not spec or not spec.startswith(('sma(', 'ema(')) or not candidates
                    or _UNSUPPORTED.search(text[:match.start()])):
                raise InvalidTechnical('cross_timeframe_alignment_unverified')
            used.extend(v['ref'] for v in candidates)
            timeframe = 'cross_timeframe'
        if spec:
            if spec not in confirmed:
                raise InvalidTechnical('indicator_not_confirmed')
            if timeframe == 'weekly' and not any(v['indicator_id'] == spec and v['timeframe'] == 'weekly'
                    for v in relation_values):
                # A missing-data statement does not assert a calculated reading.
                if not re.match(r'.{0,18}(?:不足|不可用|未提供|未计算)', suffix):
                    raise InvalidTechnical('weekly_indicator_unavailable')
            masked[match.start():match.end()] = ' ' * (match.end() - match.start())
        instances[match.start()] = spec
        timeframes[match.start()] = timeframe
    parameters_masked = ''.join(masked)
    relations = sorted([*list(_DIRECTION.finditer(parameters_masked)), *list(_RANGE.finditer(parameters_masked)),
                        *list(_EXTREME.finditer(parameters_masked))], key=lambda m: m.start())
    selected_periods = {}
    for relation in relations:
        preceding = [m for m in mentions if m.end() <= relation.start()]
        if not preceding or preceding[-1]['kind'].lower() != 'rsi':
            continue
        mention = preceding[-1]
        local = parameters_masked[mention.end():relation.end()]
        if len(local) > 100 or _UNSUPPORTED.search(local):
            raise InvalidTechnical('unsupported_rsi_relation')
        # A conditional/historical prefix also changes the meaning of the predicate.
        clause_start = next((start for start, end, _ in clauses if start <= mention.start() < end), mention.start())
        if _UNSUPPORTED.search(parameters_masked[clause_start:mention.start()]):
            raise InvalidTechnical('unsupported_rsi_relation')
        if re.match(r'\s*(?:之外|以外|外|以上|以下|之上|之下)', parameters_masked[relation.end():]):
            raise InvalidTechnical('unsupported_rsi_relation_suffix')
        spec = instances[mention.start()]
        timeframe = timeframes[mention.start()]
        if mention.start() not in selected_periods:
            # Select one period for the complete mention, including all predicates
            # and an adjacent reading; never splice daily readings and weekly relations.
            following = next((m.start() for m in mentions if m.start() > mention.start()), len(text))
            siblings = [r for r in relations if mention.end() <= r.start() < following]
            reading = re.match(r'\s*(?:的?(?:值|读数|数值))?\s*(?:约|为|是|在|报)?\s*([+\-]?\d+(?:\.\d+)?)\s*(?:点|%)', text[mention.end():])
            periods = [timeframe] if mention['timeframe'] or mention.start('kind') in prefixes else ['daily', 'weekly']
            chosen = []
            for period in periods:
                options = [v for v in relation_values if v['indicator_id'].startswith('rsi(')
                           and (not spec or v['indicator_id'] == spec) and v['timeframe'] == period and v['field'] == 'value']
                if len({(v['indicator_id'], v['date'], v['value']) for v in options}) != 1:
                    continue
                try:
                    value = Decimal(options[0]['value'])
                    if not all(_predicate_true(r, value) for r in siblings):
                        continue
                    if reading:
                        from trading_companion.answer_validation import _rounded
                        shown = Decimal(reading[1])
                        supplied_reading = any(v['indicator_id'] == options[0]['indicator_id'] and v['timeframe'] == period
                                               and v['field'] == 'value' for v in values)
                        if not supplied_reading or not _rounded(shown, value, shown):
                            continue
                except (InvalidOperation, ValueError):
                    continue
                chosen = options
                break
            if not chosen:
                raise InvalidTechnical('rsi_value_missing_ambiguous_or_relation_false')
            selected_periods[mention.start()] = chosen
        candidates = selected_periods[mention.start()]
        masked[relation.start():relation.end()] = ' ' * (relation.end() - relation.start())
        used.extend(v['ref'] for v in candidates if v['ref'] in supplied)
        if stats is not None and any(v.get('source') == 'local_computed_rsi' and v['ref'] not in supplied for v in candidates):
            stats['rsi_local_value_checks'] = stats.get('rsi_local_value_checks', 0) + 1
    remaining = ''.join(masked)
    for start, end, clause in clauses:
        if (re.search(r'RSI', clause, re.I) and re.search(r'参考(?:线|阈值)|阈值.*参考', clause)
                and not any(pattern.search(clause) for pattern in (_DIRECTION, _RANGE, _EXTREME))):
            for threshold in re.finditer(r'(?<![\d.])(?:30|50|70)(?![\d.%元])', remaining[start:end]):
                left, right = start + threshold.start(), start + threshold.end()
                masked[left:right] = ' ' * (right - left)
            if stats is not None:
                stats['rsi_reference_explanations'] = stats.get('rsi_reference_explanations', 0) + 1
    remaining = ''.join(masked)
    if (any(m['kind'].lower() == 'rsi' for m in mentions)
            and re.search(r'中线|中轴|分界|参考|阈值|区间|高于|低于|超买|超卖|靠拢|接近', parameters_masked)
            and re.search(r'(?<![\d.])(?:30|50|70)(?![\d.%元])', remaining)):
        raise InvalidTechnical('unverified_rsi_reference_line')
    return remaining, list(dict.fromkeys(used))


def reading_candidates(text, context, instrument, records):
    """Restrict adjacent indicator readings to their parameter and timeframe facts."""
    own = context.get('_technical_validation', {}).get(instrument, {})
    supplied = {r['id']: r for r in records}
    values = [v for v in own.get('values', ()) if v['ref'] in supplied
              and supplied[v['ref']].get('instrument_id') == instrument
              and supplied[v['ref']].get('date') == v['date']
              and str(supplied[v['ref']].get('raw_value')) == v['value']]
    output = {}
    prefixes = {}
    for prefix in _PREFIX.finditer(text):
        kind = {'均线': 'sma', '简单移动平均线': 'sma', '指数移动平均线': 'ema', 'ma': 'sma'}.get(prefix['kind'].lower(), prefix['kind'].lower())
        if prefix['kind'].isascii() and re.match(r'\s*[\d(]', text[prefix.end():]):
            continue
        prefixes[prefix.start('kind')] = (IndicatorSpec(kind, window=int(prefix['window'])).indicator_id,
            'weekly' if prefix['period'] == '周' or re.search(r'周线(?:的)?\s*$', text[:prefix.start()]) else 'daily')
    for match in _MENTION.finditer(text):
        reading = re.match(r'\s*(?:的?(?:值|读数|数值))?\s*(?:约|为|是|在|报)?\s*(?P<number>[+\-]?\d+(?:\.\d+)?)\s*(?:元(?:/股)?|点|%)', text[match.end():])
        if not reading:
            continue
        spec, prefix_period = prefixes.get(match.start('kind'), (_spec(match), None))
        kind = match['kind'].lower().replace('ma', 'sma') if match['kind'].lower() == 'ma' else match['kind'].lower()
        candidates = [v for v in values if v['indicator_id'] == spec or not spec and v['indicator_id'].startswith(kind + '(')]
        daily = {v['ref'] for v in candidates if v['timeframe'] == 'daily'}
        weekly = {v['ref'] for v in candidates if v['timeframe'] == 'weekly'}
        # The supplied program plan explicitly contains its daily ATR operand.
        # Keep that source when the standalone indicator row was trimmed.
        atr_specs = [s for s in own.get('confirmed', ()) if s.startswith('atr(')]
        if kind == 'atr' and len(atr_specs) == 1 and (not spec or spec == atr_specs[0]):
            daily.update(r['id'] for r in records if r.get('content_kind') == 'trade_plan'
                         and r.get('instrument_id') == instrument and 'atr' in (r.get('values') or {})
                         and context['start_date'] <= (r.get('date') or '') <= context['daily_cutoff'])
        if match['timeframe'] == '周线' or prefix_period == 'weekly':
            daily = set()
        elif match['timeframe'] == '日线' or prefix_period == 'daily':
            weekly = set()
        if candidates or match['timeframe'] or any(s.startswith(kind+'(') for s in own.get('confirmed', ())):
            output[match.end() + reading.start('number')] = (daily, weekly)
    return output
