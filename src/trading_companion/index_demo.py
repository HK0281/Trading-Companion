"""Explicitly artificial index model responses for the offline demo only."""
import json

from trading_companion.index_agent import INTENT_NAME
from trading_companion.research_intent import _local_date_expression
from trading_companion.tool_model import ToolCall, ToolModelResponse


def index_demo_response(messages, tools):
    if tools and tools[0].name == INTENT_NAME:
        return ToolModelResponse('tool_calls', tool_calls=(ToolCall('index_demo_intent', INTENT_NAME,
            dict(date_expression=_local_date_expression(messages[-1].content))),))
    context = next((m.content for m in messages if m.role == 'assistant'
                    and m.content and m.content.startswith('UNTRUSTED_INDEX_CONTEXT\n')), None)
    if not tools and context:
        data = json.loads(context.split('\n', 1)[1])
        close = data['evidence'][0]
        claim = lambda title, text: dict(title=title, text=text, evidence_ids=[close['id']])
        payload = dict(version='index-answer-v1', instrument_id=data['instrument_id'],
            direction='暂不判断', conviction='低', horizon=data['objective']['horizon'], blocks=[
                claim('结论', '人工示例暂不判断方向；此处用于演示指数研究的展示方式。'),
                claim('当前情况', f"{close['date']}完整日线收盘{close['display_value']}点。"),
                claim('情景', '若后续收盘走强可重新研究，走弱则关注下行风险，不能据此保证后续趋势。'),
                claim('观察重点', '核对历史点位与指标，若后续走势与当前观察相反应重新评估。'),
            ])
        candidate = next((c for c in data.get('scenario_candidates', ())
                          if c['plan']['purpose'] == 'wait' and c['plan']['status'] == 'available'), None)
        if candidate:
            payload['direction'] = '震荡'
            payload['blocks'][0]['text'] = '人工示例的方向设为震荡，把握低，仅用于演示。'
            payload['scenario_blocks'] = [dict(title='观察重点',
                text=''.join(f"指数{r['label']}为{r['display_value']}点。" for r in candidate['evidence']),
                evidence_ids=[r['id'] for r in candidate['evidence']],
                numbers=[dict(text=r['display_value']+'点', ref=r['id'], field=r['field']) for r in candidate['evidence']])]
        return ToolModelResponse('complete', text=json.dumps(payload, ensure_ascii=False))
    return None
