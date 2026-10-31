"""Independent index evidence archive, without chat or generated narrative text."""
from datetime import date, datetime
from hashlib import sha256
import json

from trading_companion.index_contracts import IndexBar, IndexResearchRequest, IndexResearchResult
from trading_companion.index_research import answer_context
from trading_companion.multi_contracts import IndicatorSpec

VERSION = 'index-evidence-snapshot-v1'


def snapshot_data(result):
    result.__post_init__()
    context = answer_context(result)
    raw = result.to_dict()
    # Display rounding must not hide a change to the full-precision OHLC.
    digest = sha256(json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()
    return dict(version=VERSION, index_snapshot=raw, index_snapshot_sha256=digest, evidence=context['evidence'],
                limitations=context['limitations'])


def validate_snapshot(data):
    """Recompute all daily and complete-week evidence from the original OHLC."""
    raw = data['index_snapshot']
    params = raw['parameters']
    request = IndexResearchRequest(params['instrument_id'], date.fromisoformat(params['start_date']),
        date.fromisoformat(params['as_of_date']), tuple(IndicatorSpec(s['kind'], **{k: v for k, v in s.items() if k != 'kind'})
        for s in params['indicators']), params['source'], raw['request_id'], raw['benchmark_defaulted'], raw['horizon'])
    bars = tuple(IndexBar(**{**b, 'trade_date': date.fromisoformat(b['trade_date'])}) for b in raw['bars'])
    result = IndexResearchResult(request, bars, datetime.fromisoformat(raw['retrieved_at']))
    if data != snapshot_data(result):
        raise ValueError('Index snapshot evidence mismatch')
    return result
