"""Index-only chart contracts with null quantities, bound to frozen daily research."""
from dataclasses import dataclass
from datetime import date, datetime, time
from copy import deepcopy
import json

from trading_companion.chart_intervals import ChartUnavailable, SHANGHAI
from trading_companion.chart_projection import MAX_JSON_BYTES
from trading_companion.index_contracts import IndexBar, IndexResearchRequest, IndexResearchResult
from trading_companion.index_research import index_series, index_weeks
from trading_companion.multi_contracts import IndicatorSpec


def _bounded(value):
    if len(json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')) > MAX_JSON_BYTES:
        raise ChartUnavailable('chart_payload_limit')
    return value


def daily_chart(result, *, context='current', saved_at=None):
    result.__post_init__()
    request = result.request
    return _bounded(dict(version='index-chart-v1', status='available', code='aligned_research_series',
        validation_scope='request_bound_aligned_series_not_full_historical_evidence',
        request_id=request.request_id, **{k: v for k, v in request.parameters().items() if k != 'indicators'},
        instrument_kind='index', price_unit='index_point', volume_status='unit_unverified',
        price_adjustment='unadjusted', data_authenticity=result.authenticity,
        retrieved_at=result.retrieved_at.isoformat(), context=context,
        saved_at=saved_at.isoformat() if saved_at else None,
        bars=[dict(date=b.trade_date.isoformat(), open=b.open_price, high=b.high_price,
                   low=b.low_price, close=b.close_price, volume=None) for b in result.bars],
        series=index_series(result.bars, request.indicators)))


@dataclass(frozen=True)
class IndexChartBinding:
    request: IndexResearchRequest
    snapshot: dict
    result: IndexResearchResult


def bind_index_chart(turn, data_mode):
    try:
        chart, view, params = turn['chart'], turn['index_research'], turn['parameters']
        if (turn['status'] not in ('completed', 'limited') or view['request_id'] != chart['request_id']
                or turn['report']['request_id'] != chart['request_id']
                or chart['source'] != ('csv' if data_mode == 'synthetic' else data_mode)):
            raise ValueError()
        specs = tuple(IndicatorSpec(p['kind'], **{k: v for k, v in p.items() if k != 'kind'}) for p in params['indicators'])
        request = IndexResearchRequest(params['instrument_id'], date.fromisoformat(params['start_date']),
            date.fromisoformat(params['as_of_date']), specs, params['source'], chart['request_id'],
            view['benchmark_defaulted'], view['horizon'])
        bars = tuple(IndexBar(request.instrument_id, date.fromisoformat(b['date']), b['open'], b['high'], b['low'],
                              b['close'], volume=b['volume']) for b in chart['bars'])
        result = IndexResearchResult(request, bars, datetime.fromisoformat(chart['retrieved_at']))
        expected = daily_chart(result, context=chart['context'],
            saved_at=datetime.fromisoformat(chart['saved_at']) if chart['saved_at'] else None)
        if chart != expected or params != request.parameters():
            raise ValueError()
        return IndexChartBinding(request, deepcopy(chart), result)
    except (ValueError, TypeError, KeyError, AttributeError):
        raise ChartUnavailable('chart_binding_invalid') from None


def index_capabilities(binding):
    return dict(version='research-chart-capabilities-v1', research_id=binding.request.request_id,
        intervals={'1w': dict(enabled=True, code='ready'),
                   **{i: dict(enabled=False, code='chart_hourly_source_unsupported') for i in ('60m', '120m')}})


def weekly_chart(binding):
    result, snapshot = binding.result, binding.snapshot
    weeks = index_weeks(result, completed_only=False)
    if not weeks:
        raise ChartUnavailable('chart_insufficient_history')
    bars = [dict(date=b.trade_date.isoformat(),
        start_at=datetime.combine(start, time(9, 30), SHANGHAI).isoformat(),
        end_at=datetime.combine(b.trade_date, time(15), SHANGHAI).isoformat(), complete=complete,
        open=b.open_price, high=b.high_price, low=b.low_price, close=b.close_price, volume=None)
        for start, b, complete in weeks]
    return _bounded(dict(**{k: snapshot[k] for k in ('request_id', 'instrument_id', 'start_date', 'as_of_date', 'source',
        'price_adjustment', 'data_authenticity', 'retrieved_at', 'context', 'saved_at', 'instrument_kind', 'price_unit', 'volume_status')},
        version='index-chart-interval-v1', status='available', code='chart_only_series', interval='1w', timezone='Asia/Shanghai',
        coverage=dict(start_at=bars[0]['start_at'], end_at=bars[-1]['end_at'], bar_count=len(bars)),
        bars=bars, series=index_series(tuple(w[1] for w in weeks), binding.request.indicators)))
