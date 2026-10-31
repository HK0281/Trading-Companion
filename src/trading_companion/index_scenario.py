"""Independent, recomputable index-point plans, selected by the checked trend call."""
from decimal import Decimal, DecimalException, ROUND_HALF_UP
from hashlib import sha256
import json

from trading_companion.atr_plan_math import atr_geometry, purpose_values
from trading_companion.decision_horizon import PARAMETERS, profile
from trading_companion._indicator_values import atr
from trading_companion.index_research import index_weeks
from trading_companion.multi_contracts import ALGORITHM_VERSIONS
from trading_companion.trade_plan import PURPOSES

VERSION = 'index-scenario-plan-v1'
METHOD = 'index_atr_scenario_v1'
DIRECTION_PURPOSE = {'偏多': 'conditional_entry', '震荡': 'wait', '偏空': 'holding_exit'}
NOTE = '指数不可直接交易，点位为情景参考。'
LABELS = dict(reference_entry='入场参考', reference_stop='止损参考（跌破观察位）', target_1='止盈1（目标1）',
              target_2='止盈2（目标2）', reward_risk_1='实际盈亏比1', reward_risk_2='实际盈亏比2',
              reference_close='收盘参考', exit_below='退出观察位（跌破止损参考）',
              observe_below='下方观察位', observe_above='上方观察位', atr='ATR', atr_multiple='ATR倍数')
REASONS = dict(plan_not_requested='本轮未请求点位计划。', plan_excluded='本轮已排除参考计划。',
    risk_atr_not_requested='范围未允许ATR14，未补充计划依赖。', decision_horizon_required='尚未确认观察期限。',
    plan_long_history_insufficient='长线计划需要至少365自然日窗口及350日实际历史跨度。',
    plan_weekly_atr_unavailable='完整周线不足15根或周ATR不可用，未回退日ATR。',
    plan_atr_unavailable='同轮日ATR14不足或波动无效。', plan_price_grid_unavailable='点位舍入后无法形成有效计划。',
    index_direction_unavailable='尚无通过核对的明确方向，未展示点位计划。')


def candidates(result):
    result.__post_init__()
    request = result.request
    selected = None
    code = request.scenario_code or ('plan_not_requested' if not request.scenario_requested else None)
    try:
        selected = profile(request.horizon)
    except ValueError:
        code = code or 'decision_horizon_required'
    timeframe, multiple, targets = PARAMETERS[selected] if selected else (None, None, ())
    code = code or (None if any(s.kind == 'atr' and s.parameters['window'] == 14 for s in request.indicators)
                    else 'risk_atr_not_requested')
    bars, core, atr_day = result.bars, None, None
    if not code and timeframe == 'weekly':
        if ((request.as_of_date-request.start_date).days+1 < 365
                or (bars[-1].trade_date-bars[0].trade_date).days < 350):
            code = 'plan_long_history_insufficient'
        else:
            bars = tuple(item[1] for item in index_weeks(result))
            if len(bars) < 15:
                code = 'plan_weekly_atr_unavailable'
    if not code:
        value = atr(bars, 14)[-1]
        if value is None or not Decimal(str(value)).is_finite() or value <= 0:
            code = 'plan_weekly_atr_unavailable' if timeframe == 'weekly' else 'plan_atr_unavailable'
        else:
            atr_day = bars[-1].trade_date.isoformat()
            try:
                core = atr_geometry(result.bars[-1].close_price, value, multiple, targets)
            except (DecimalException, ValueError, OverflowError):
                code = 'plan_price_grid_unavailable'
    # Bind the whole immutable source snapshot, including full precision OHLC,
    # window provenance and all period rules, rather than rounded evidence.
    binding = dict(snapshot=result.to_dict(), method=METHOD, atr_algorithm=ALGORITHM_VERSIONS['atr'],
                   week_rule='monday; omit truncated first and incomplete last', horizon=request.horizon)
    digest = sha256(json.dumps(binding, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    output = []
    for purpose in PURPOSES:
        output.append(dict(version=VERSION, method=METHOD, plan_id=f'{request.request_id}:{METHOD}:{digest[:24]}:{purpose}',
            purpose=purpose, instrument_kind='index', instrument_id=request.instrument_id, request_id=request.request_id,
            price_unit='index_point', status='available' if core else 'unavailable', code='plan_ready' if core else code,
            horizon=request.horizon, horizon_profile=selected, atr_timeframe=timeframe, atr_source_date=atr_day,
            source_date=result.bars[-1].trade_date.isoformat(), source=request.source, data_authenticity=result.authenticity,
            input_snapshot_sha256=digest, atr_algorithm=ALGORITHM_VERSIONS['atr'],
            atr_multiple=multiple, target_multiples=list(targets),
            values=purpose_values(core, purpose) if core else None, origin='application', execution_allowed=False,
            note=NOTE))
    return tuple(output)


def selected_plan(result, direction, *, origin='model'):
    purpose = DIRECTION_PURPOSE.get(direction) if origin == 'model' else None
    plan = dict(next(p for p in candidates(result) if p['purpose'] == (purpose or 'wait')))
    if purpose is None:
        code = 'index_direction_unavailable' if plan['status'] == 'available' else plan['code']
        plan.update(purpose=None, status='unavailable', code=code, values=None, plan_id=None)
    plan['reason'] = REASONS.get(plan['code'], '')
    return plan


def plan_evidence(plan):
    if plan['status'] != 'available':
        return []
    return [dict(id=f'S{list(PURPOSES).index(plan["purpose"])+1}_{i+1}', instrument_id=plan['instrument_id'],
        content_kind='index_scenario', kind='technical', plan_id=plan['plan_id'], field=field,
        raw_value=value, display_value=value, label=LABELS[field],
        unit='ratio' if field.startswith('reward_risk') or field == 'atr_multiple' else 'index_point',
        date=plan['atr_source_date'] if field == 'atr' else plan['source_date'], source=plan['source'],
        timeframe=plan['atr_timeframe'], display=LABELS[field]+'：'+value)
        for i, (field, value) in enumerate(plan['values'].items())]


def plan_text(plan):
    if plan['status'] != 'available':
        return '点位情景计划：'+plan['reason']+' '+NOTE
    prefix = ('假如已持有跟踪该指数的基金，以下仅为指数层面的跌破警惕位，不对应基金成交价。\n'
              if plan['purpose'] == 'holding_exit' else '')
    rows = [f'观察期限：{plan["horizon"]}；波动口径：'+('完整周线' if plan['atr_timeframe'] == 'weekly' else '日线')
            +f'ATR14，日期{plan["atr_source_date"]}；收盘日期{plan["source_date"]}。']
    for field, value in plan['values'].items():
        unit = '倍' if field.startswith('reward_risk') or field == 'atr_multiple' else '点'
        if unit == '倍':
            value = format(Decimal(value).quantize(Decimal('.01'), rounding=ROUND_HALF_UP), 'f').rstrip('0').rstrip('.')
        rows.append(f'{LABELS[field]}：{value}{unit}')
    return '点位情景计划：\n'+prefix+'\n'.join(rows)+'\n'+NOTE
