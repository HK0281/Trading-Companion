"""Exact indicator parameters and bounded RSI predicates against latest supplied facts."""
from decimal import Decimal, InvalidOperation
import re

from trading_companion.multi_contracts import IndicatorSpec, PARAMETER_DEFAULTS
from trading_companion.errors import InvalidRequestError

_MENTION = re.compile(r'(?<![A-Za-z0-9_])(?:(?P<timeframe>日线|周线)(?:的)?\s*)?'
    r'(?P<kind>VWMA|VOLUME|MACD|BOLL|SMA|EMA|RSI|ATR|ADX|OBV|MA)(?![A-Za-z_])'
    r'(?P<parameters>\s*(?:\([^)]*(?:\)|$)|[+\-]?\d+(?:\.\d+)?))?', re.I)
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
        values = []
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
        output[company.instrument_id] = dict(confirmed=confirmed, values=values)
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


def mask_technical(text, context, instrument, records):
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
    prefixes = {}
    for prefix in _PREFIX.finditer(text):
        # English indicators may carry their own following parameters (20天RSI6).
        # Chinese mean names instead allow the actual reading immediately after them.
        if prefix['kind'].isascii() and re.match(r'\s*[\d(]', text[prefix.end():]):
            continue
        if prefix['period'] != '周' and re.search(r'(?:最近|近|过去)\s*$', text[:prefix.start()]):
            continue
        if prefix['period'] != '周' and '周线' in text[max(0, prefix.start()-12):prefix.start()]:
            raise InvalidTechnical('implicit_weekly_indicator_ambiguous')
        kind = {'均线': 'sma', '简单移动平均线': 'sma', '指数移动平均线': 'ema',
                'ma': 'sma'}.get(prefix['kind'].lower(), prefix['kind'].lower())
        try:
            spec = IndicatorSpec(kind, window=int(prefix['window'])).indicator_id
        except (ValueError, InvalidRequestError):
            raise InvalidTechnical('invalid_indicator_parameters') from None
        timeframe = 'weekly' if prefix['period'] == '周' else 'daily'
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
        if match['parameters'] and (re.match(r'[A-Za-z0-9_.]|元|点|%|倍', suffix)
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
        elif not match['timeframe'] and '周线' in text[max(0, match.start()-12):match.start()]:
            raise InvalidTechnical('implicit_weekly_indicator_ambiguous')
        if spec:
            if spec not in confirmed:
                raise InvalidTechnical('indicator_not_confirmed')
            if timeframe == 'weekly' and not any(
                    v['indicator_id'] == spec and v['timeframe'] == 'weekly' for v in values):
                raise InvalidTechnical('weekly_indicator_unavailable')
            masked[match.start():match.end()] = ' ' * (match.end() - match.start())
        instances[match.start()] = spec
        timeframes[match.start()] = timeframe
    parameters_masked = ''.join(masked)
    relations = sorted([*list(_DIRECTION.finditer(parameters_masked)), *list(_RANGE.finditer(parameters_masked)),
                        *list(_EXTREME.finditer(parameters_masked))], key=lambda m: m.start())
    for relation in relations:
        preceding = [m for m in mentions if m.end() <= relation.start()]
        if not preceding or preceding[-1]['kind'].lower() != 'rsi':
            continue
        mention = preceding[-1]
        local = parameters_masked[mention.end():relation.end()]
        if len(local) > 100 or _UNSUPPORTED.search(local):
            raise InvalidTechnical('unsupported_rsi_relation')
        # A conditional/historical prefix also changes the meaning of the predicate.
        if _UNSUPPORTED.search(parameters_masked[:mention.start()]):
            raise InvalidTechnical('unsupported_rsi_relation')
        if re.match(r'\s*(?:之外|以外|外|以上|以下|之上|之下)', parameters_masked[relation.end():]):
            raise InvalidTechnical('unsupported_rsi_relation_suffix')
        spec = instances[mention.start()]
        timeframe = timeframes[mention.start()]
        if not spec and not mention['timeframe'] and len({
                (v['indicator_id'], v['timeframe']) for v in values if v['indicator_id'].startswith('rsi(')}) != 1:
            raise InvalidTechnical('rsi_instance_or_timeframe_ambiguous')
        candidates = [v for v in values if v['indicator_id'].startswith('rsi(')
                      and (not spec or v['indicator_id'] == spec) and v['timeframe'] == timeframe
                      and v['field'] == 'value']
        if len({(v['indicator_id'], v['date'], v['value']) for v in candidates}) != 1:
            raise InvalidTechnical('rsi_latest_value_missing_or_ambiguous')
        try:
            value = Decimal(candidates[0]['value'])
            if not value.is_finite() or not 0 <= value <= 100:
                raise ValueError
        except (InvalidOperation, ValueError):
            raise InvalidTechnical('invalid_rsi_value') from None
        if relation.re is _DIRECTION:
            line = Decimal(relation['line'])
            ok = value < line if relation['op'] in ('低于', '小于') else value > line
        elif relation.re is _RANGE:
            low, high = Decimal(relation['low']), Decimal(relation['high'])
            ok = low < high and low <= value <= high
        else:
            ok = value > 70 if relation[0] == '超买' else value < 30
        if not ok:
            raise InvalidTechnical('rsi_relation_false')
        masked[relation.start():relation.end()] = ' ' * (relation.end() - relation.start())
        used.extend(v['ref'] for v in candidates)
    remaining = ''.join(masked)
    if (any(m['kind'].lower() == 'rsi' for m in mentions)
            and re.search(r'中线|中轴|分界|参考|阈值|区间|高于|低于|超买|超卖|靠拢|接近', parameters_masked)
            and re.search(r'(?<![\d.])(?:30|50|70)(?![\d.%元])', remaining)):
        raise InvalidTechnical('unverified_rsi_reference_line')
    return remaining, list(dict.fromkeys(used))
