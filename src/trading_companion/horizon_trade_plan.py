"""Versioned daily/complete-week ATR inputs and exact horizon-specific price rules."""

from dataclasses import replace
from decimal import Decimal, DecimalException, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, localcontext
from hashlib import sha256
import json

from trading_companion.decision_horizon import PARAMETERS, profile
from trading_companion.indicators import atr
from trading_companion.multi_contracts import ALGORITHM_VERSIONS
from trading_companion.research_report import build_research_report
from trading_companion.risk_research import build_risk_scenario, PRICE_GRID
from trading_companion.technical_insights import aggregate_completed_weeks
from trading_companion.trade_plan import TradePlan, PURPOSES, HORIZON_METHOD
from trading_companion.atr_plan_math import atr_geometry, purpose_values

WEEKLY_METHOD = 'complete-week-atr14-v1:' + ALGORITHM_VERSIONS['atr']


def weekly_atr_evidence(result, *, expected_request):
    """Bind weekly ATR to every actual daily OHLC input; never reuse weekly-close MA facts."""
    report = build_research_report(result, expected_request=expected_request)
    if report.status not in ('complete', 'limited'):
        return None, 'risk_evidence_mismatch'
    if not any(s.kind == 'atr' and s.parameters['window'] == 14 for s in expected_request.indicators):
        return None, 'risk_atr_not_requested'
    if (expected_request.as_of_date - expected_request.start_date).days + 1 < 365:
        return None, 'plan_long_history_insufficient'
    weeks = aggregate_completed_weeks(result.bars, start_date=expected_request.start_date,
                                      as_of_date=expected_request.as_of_date)
    if len(weeks) < 15:
        return None, 'plan_weekly_atr_unavailable'
    # Also require observed history to cover approximately a year, rather than an empty requested window.
    if not result.bars or (result.bars[-1].trade_date - result.bars[0].trade_date).days < 350:
        return None, 'plan_long_history_insufficient'
    value = atr(weeks, 14)[-1]
    if value is None or not Decimal(str(value)).is_finite() or value <= 0:
        return None, 'plan_weekly_atr_unavailable'
    included = []
    from trading_companion.technical_insights import group_daily_weeks
    last = weeks[-1].trade_date
    for _, rows in group_daily_weeks(result.bars, start_date=expected_request.start_date, as_of_date=expected_request.as_of_date):
        if rows[-1].trade_date <= last:
            included.extend(rows)
    inputs = [(b.trade_date.isoformat(), *(str(getattr(b, f)) for f in ('open_price', 'high_price', 'low_price', 'close_price')))
              for b in included]
    binding = dict(request_id=expected_request.request_id, instrument_id=expected_request.instrument_id,
        source=result.source, adjustment=result.price_adjustment, start=expected_request.start_date.isoformat(),
        end=expected_request.as_of_date.isoformat(), method=WEEKLY_METHOD, inputs=inputs)
    digest = sha256(json.dumps(binding, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    identifier = f'{expected_request.request_id}|weekly_atr14:{digest}'
    dependencies = tuple(f'{expected_request.request_id}|{b.trade_date.isoformat()}|daily_ohlc:{field}'
                         for b in included for field in ('open_price', 'high_price', 'low_price', 'close_price'))
    from trading_companion.product_research import ProductEvidence
    return ProductEvidence(identifier, expected_request.instrument_id, 'technical', '周线ATR(14)',
        f'{value:.4f}元', result.source, last.isoformat(), publication_precision='date',
        data_authenticity=result.data_authenticity, temporal_scope='completed_week_atr_derived',
        dependencies=dependencies, raw_value=value, unit='CNY/share',
        calculation_basis=(('method', WEEKLY_METHOD), ('window', '14'), ('complete_week', 'Sunday boundary; omit truncated first week'),
                           ('input_snapshot_digest', digest))), None


def build_horizon_plans(result, *, expected_request, horizon, unavailable_code=None):
    instrument = expected_request.instrument_id if expected_request is not None else result
    request_id = expected_request.request_id if expected_request is not None else None
    try:
        selected = profile(horizon)
    except ValueError:
        selected = None
    timeframe, multiple, targets = PARAMETERS[selected] if selected else (None, None, ())
    common = dict(instrument_id=instrument, request_id=request_id, method=HORIZON_METHOD, horizon=horizon,
        horizon_profile=selected, atr_timeframe=timeframe, atr_multiple=multiple, target_multiples=targets)
    code, core = unavailable_code or 'risk_market_data_unavailable', None
    if not unavailable_code and not selected:
        code = 'decision_horizon_required'
    elif expected_request is not None and not unavailable_code:
        checked = build_risk_scenario(result, expected_request=expected_request)
        code = checked.code
        common.update(source_date=checked.source_date, source=checked.source, price_adjustment=checked.price_adjustment,
            data_authenticity=checked.data_authenticity, atr_indicator_id=checked.atr_indicator_id,
            atr_source_date=checked.source_date, evidence_ids=checked.evidence_ids)
        if not any(s.kind == 'atr' and s.parameters['window'] == 14 for s in expected_request.indicators):
            code = 'risk_atr_not_requested'
        elif checked.status == 'available' and selected:
            value = Decimal(checked.values.atr)
            weekly = None
            if timeframe == 'weekly':
                common['atr_source_date'] = None
                weekly, code = weekly_atr_evidence(result, expected_request=expected_request)
                if weekly:
                    value = Decimal(str(weekly.raw_value))
                    common.update(atr_source_date=weekly.date, atr_indicator_id='weekly:atr:window=14',
                                  evidence_ids=(checked.evidence_ids[0], weekly.id))
            if timeframe == 'daily' or weekly:
                try:
                    core = atr_geometry(checked.values.reference_entry, value, multiple, targets)
                except (DecimalException, ValueError, OverflowError):
                    code = 'plan_price_grid_unavailable'
    plans = []
    identity = sha256(str(horizon).encode()).hexdigest()[:16]
    for purpose in PURPOSES:
        values = purpose_values(core, purpose) if core else None
        plans.append(TradePlan(f'{request_id or instrument}|{HORIZON_METHOD}:{identity}:{purpose}', purpose=purpose,
            status='available' if values else 'unavailable', code='plan_ready' if values else code,
            values=tuple(values.items()) if values else (), **common))
    return tuple(plans)


def build_horizon_risk_scenario(result, *, expected_request, horizon):
    """Old risk follow-ups use the same first target and stop as current plans."""
    from trading_companion.risk_research import RiskScenario, RiskValues
    blocked = (None if any(s.kind == 'atr' and s.parameters['window'] == 14 for s in expected_request.indicators)
               else 'risk_atr_not_requested')
    plan = build_horizon_plans(result, expected_request=expected_request, horizon=horizon, unavailable_code=blocked)[0]
    values = None
    if plan.values:
        p = dict(plan.values)
        entry, stop, target = (Decimal(p[k]) for k in ('reference_entry', 'reference_stop', 'target_1'))
        risk = entry-stop
        values = RiskValues(p['reference_entry'], p['atr'], p['atr_multiple'], plan.target_multiples[0], p['reference_stop'],
            p['target_1'], str(risk), str(target-entry), str((risk/entry*100).quantize(Decimal('.0001'), rounding=ROUND_CEILING)),
            p['reward_risk_1'])
    return RiskScenario(plan.status, plan.code, plan.request_id, plan.instrument_id, plan.source_date, plan.source,
        plan.data_authenticity, plan.price_adjustment, plan.atr_indicator_id, values, plan.evidence_ids,
        ('与本轮已选持有期限的参考计划使用同一波动和价格规则。',), method='atr_multiple_horizon_long_v2',
        horizon=horizon, atr_source_date=plan.atr_source_date, atr_timeframe=plan.atr_timeframe)
