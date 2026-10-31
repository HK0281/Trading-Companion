"""Point-based observations using the same numeric kernel and week boundaries as stocks."""
from dataclasses import asdict
from datetime import timedelta

from trading_companion.chart_intervals import chart_series
from trading_companion.index_contracts import IndexBar, VOLUME_KINDS, LIMITATIONS
from trading_companion.ohlc_weeks import group_weeks, aggregate_prices
from trading_companion.evidence_numbers import display_number


def index_weeks(result, *, completed_only=True):
    result.__post_init__()
    request = result.request
    output = []
    for monday, rows in group_weeks(result.bars, start_date=request.start_date, as_of_date=request.as_of_date):
        complete = monday + timedelta(days=6) <= request.as_of_date
        if completed_only and not complete:
            continue
        prices = aggregate_prices(rows)
        bar = IndexBar(request.instrument_id, rows[-1].trade_date, prices.open_price,
                       prices.high_price, prices.low_price, prices.close_price)
        output.append((rows[0].trade_date, bar, complete))
    return tuple(output)


def index_series(bars, specs):
    # No NumericBar with fabricated volume is constructed for an index.
    series = chart_series(bars, tuple(s for s in specs if s.kind not in VOLUME_KINDS))
    return [{**s, 'unit': 'index_point' if s['unit'] == 'CNY/share' else s['unit']} for s in series]


def index_evidence(result):
    result.__post_init__()
    request = result.request
    records, readings = [], []
    def add(label, value, unit, day, *, indicator=None, field=None, timeframe='daily'):
        identifier = f'E{len(records) + 1}'
        shown = display_number(value)
        value = float(shown)
        row = dict(id=identifier, instrument_id=request.instrument_id, label=label,
                   raw_value=value, unit=unit, date=day.isoformat(), field=field,
                   display_value=shown, display=f'{label}：{shown}' + ('点' if unit == 'index_point' else '（指标读数）'),
                   content_kind='fact', kind='technical',
                   source=request.source, indicator_id=indicator, timeframe=timeframe)
        records.append(row)
        if indicator:
            readings.append(dict(indicator_id=indicator, timeframe=timeframe, field=field,
                                 value=str(value), date=day.isoformat(), ref=identifier))
    latest = result.bars[-1]
    add('最新完整日收盘点位', latest.close_price, 'index_point', latest.trade_date, field='close_price')
    # Historical extrema are candidates, never targets or a promised support level.
    rows = result.bars[-120:]
    add('研究窗口内最近至多120条日线最低点', min(b.low_price for b in rows), 'index_point', latest.trade_date, field='window_low')
    add('研究窗口内最近至多120条日线最高点', max(b.high_price for b in rows), 'index_point', latest.trade_date, field='window_high')
    weeks = tuple(item[1] for item in index_weeks(result))
    missing = []
    for timeframe, bars in (('daily', result.bars), ('weekly', weeks)):
        if not bars:
            missing.append('完整周线不足，周线读数不可用')
            continue
        for series in index_series(bars, request.indicators):
            value = series['values'][-1]
            if value is None:
                missing.append(('周线' if timeframe == 'weekly' else '日线') + series['label'] + '：历史不足或计算不可用')
            else:
                add(('周线' if timeframe == 'weekly' else '日线') + series['label'], value, series['unit'],
                    bars[-1].trade_date, indicator=series['indicator_id'], field=series['field'], timeframe=timeframe)
    missing.extend(s.indicator_id + '：成交量单位未核实' for s in request.indicators if s.kind in VOLUME_KINDS)
    return records, readings, tuple(missing)


def answer_context(result):
    from trading_companion.market_scope import INDICES
    records, readings, missing = index_evidence(result)
    request = result.request
    names = (*request.index.aliases, request.index.name, request.index.symbol)
    return dict(instrument_kind='index', identity_names=names,
        securities=[dict(instrument_id=i.instrument_id, name=i.name) for i in INDICES],
        instrument_id=request.instrument_id, name=request.index.name,
        start_date=request.start_date.isoformat(), daily_cutoff=request.as_of_date.isoformat(),
        objective=dict(horizon=request.horizon), evidence=records, price_levels=[],
        limitations=[*LIMITATIONS, *missing],
        _technical_validation={request.instrument_id: dict(
            confirmed=tuple(s.indicator_id for s in request.indicators), values=readings, local_rsi=[])})
