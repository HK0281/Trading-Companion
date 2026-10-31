"""Independent index evidence archive, without chat or generated narrative text."""
from datetime import date, datetime
from hashlib import sha256
import json

from trading_companion.index_contracts import IndexBar, IndexResearchRequest, IndexResearchResult
from trading_companion.index_research import answer_context
from trading_companion.multi_contracts import IndicatorSpec

VERSION = 'index-evidence-snapshot-v1'


def snapshot_data(result, *, direction=None):
    result.__post_init__()
    context = answer_context(result)
    raw = result.to_dict()
    # Display rounding must not hide a change to the full-precision OHLC.
    digest = sha256(json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()
    output = dict(version=VERSION, index_snapshot=raw, index_snapshot_sha256=digest, evidence=context['evidence'],
                  limitations=context['limitations'])
    if raw['version'] == 'index-snapshot-v2':
        from trading_companion.index_scenario import selected_plan
        if direction not in (None, '偏多', '震荡', '偏空', '暂不判断'):
            raise ValueError('Invalid archived direction')
        output.update(version='index-evidence-snapshot-v2', selected_direction=direction,
                      scenario_plan=selected_plan(result, direction or '暂不判断') if result.request.scenario_requested else None)
    return output


def validate_snapshot(data):
    """Recompute all daily and complete-week evidence from the original OHLC."""
    raw = data['index_snapshot']
    if raw['version'] not in ('index-snapshot-v1', 'index-snapshot-v2'):
        raise ValueError('Unsupported index snapshot version')
    params = raw['parameters']
    request = IndexResearchRequest(params['instrument_id'], date.fromisoformat(params['start_date']),
        date.fromisoformat(params['as_of_date']), tuple(IndicatorSpec(s['kind'], **{k: v for k, v in s.items() if k != 'kind'})
        for s in params['indicators']), params['source'], raw['request_id'], raw['benchmark_defaulted'], raw['horizon'],
        **({k: raw[k] for k in ('scenario_requested', 'historical_explicit', 'indicators_strict', 'scenario_code')}
           if raw['version'] == 'index-snapshot-v2' else {}))
    bars = tuple(IndexBar(**{**b, 'trade_date': date.fromisoformat(b['trade_date'])}) for b in raw['bars'])
    result = IndexResearchResult(request, bars, datetime.fromisoformat(raw['retrieved_at']))
    if data != snapshot_data(result, direction=data.get('selected_direction')):
        raise ValueError('Index snapshot evidence mismatch')
    return result
