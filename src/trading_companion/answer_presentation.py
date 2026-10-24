"""Compact application-owned views; presentation never introduces another model call."""

from trading_companion.indicator_knowledge import DEFINITIONS
from trading_companion.research_report import indicator_label
from trading_companion.research_synthesis import build_research_synthesis


_TITLES = {"trend": "均线位置", "momentum": "动量表现", "volatility": "波动变化", "volume": "成交量"}
_HEADLINES = {
    "aligned_upper_reference": "均线与动量参考同处上侧",
    "aligned_lower_reference": "均线与动量参考同处下侧",
    "conflicting": "指标参考存在分歧，需要分开看",
    "incomplete": "先看已核对的指标表现",
    "indeterminate": "指标处于参考边界，方向尚不明确",
}


def _brief_observation(item, specs):
    """Known relation enums get concise templates; never trim arbitrary source/model text."""
    label = indicator_label(specs[item.indicator_id])
    position = {"upper_reference": "高于", "lower_reference": "低于", "at_reference": "等于"}
    change = {"increasing": "增加", "decreasing": "减少", "unchanged": "不变"}
    if item.relation == "price_position" and item.value in position:
        return f"收盘价{position[item.value]}{label}。"
    if item.relation in ("mean_change", "histogram_change", "atr_change") and item.value in change:
        name = label + ("柱值" if item.relation == "histogram_change" else "波动尺度" if item.relation == "atr_change" else "")
        return f"{name}较前一条记录{change[item.value]}。"
    if item.relation == "dif_dea_position" and item.value in position:
        return f"{label}的DIF{position[item.value]}DEA。"
    if item.relation == "rsi_zone":
        zone = {"below_30": "低于30，处于低位参考区", "above_70": "高于70，处于高位参考区",
                "between_30_and_70": "位于30至70的参考区间（含边界）"}
        if item.value in zone:
            return f"{label}{zone[item.value]}。"
    if item.relation == "bands_state" and item.value in ("coincident", "distinct"):
        return f"{label}上下轨{'重合' if item.value == 'coincident' else '不重合'}。"
    if item.relation == "band_width_change" and item.value in change:
        return f"{label}轨道宽度较前一条记录{change[item.value]}。"
    if item.relation == "atr_scale" and item.value in ("zero", "nonzero"):
        return f"{label}波动尺度{'为零' if item.value == 'zero' else '非零'}。"
    if item.relation == "relative_volume_position" and item.value in position:
        return f"{label}量比{position[item.value]}1，以严格此前窗口均量为基线。"
    if item.relation == "zero_prior_baseline" and item.value == "undefined":
        return f"{label}此前均量为零，量比未定义。"
    return item.text


def build_answer_presentation(result, *, report, request, analysis):
    """Project checked objects, never extract conclusions from the long display string."""
    if result.knowledge_answer is not None:
        return {
            "version": "answer-presentation-v1", "kind": "knowledge",
            "request_id": None, "instrument_id": None, "trade_date": None,
            "headline": "把指标讲清楚",
            "sections": [{"id": key, "title": DEFINITIONS[key][0], "items": [DEFINITIONS[key][1]]}
                         for key in result.knowledge_answer.topics],
        }
    followup = result.followup_answer
    if (request is None or report is None or report.status not in ("complete", "limited")
            or report.request_id != request.request_id or not report.fields):
        return None
    if followup is not None:
        # A saved overview may use only the snapshot that public_turn already rechecked.
        if (result.status != "completed" or followup.status != "accepted"
                or followup.request_id != request.request_id):
            return None
        if followup.action in ("facts", "explain", "relations"):
            if not set(followup.indicator_ids) <= {item.indicator_id for item in request.indicators}:
                return None
            sections = [{"id": key, "title": DEFINITIONS[key][0], "items": [DEFINITIONS[key][1]]}
                        for key in followup.definition_topics]
            if followup.relation_texts:
                items = list(followup.relation_texts)
                if followup.relations_truncated:
                    items.append("本次展示前 8 项已核对关系；可明确指标缩小范围。")
                sections.append({"id": "saved_relations", "title": "与上次读数一起看", "items": items})
            elif followup.action == "relations":
                sections.append({"id": "saved_relations", "title": "指标关系", "items": ["所选指标没有可用关系。"]})
            return {
                "version": "answer-presentation-v1", "kind": "followup",
                "request_id": request.request_id, "instrument_id": request.instrument_id,
                "trade_date": report.fields[0].trade_date, "headline": "结合上次结果看" if followup.action != "facts" else "上次研究中的指标读数",
                "indicator_ids": list(followup.indicator_ids), "sections": sections,
            }
        if followup.action != "overview":
            return None
        synthesis = build_research_synthesis(analysis, expected_request=request) if analysis is not None else None
    else:
        synthesis = result.research_synthesis
    sections = []
    headline = "已整理本次研究的关键数据"
    if result._is_stopped:
        headline = "研究已停止，保留已核对的数据"
    elif (synthesis is not None and synthesis.status in ("complete", "limited")
          and synthesis.request_id == request.request_id):
        if synthesis.cross_dimension is not None:
            headline = _HEADLINES.get(synthesis.cross_dimension.state, headline)
        specs = {spec.indicator_id: spec for spec in request.indicators}
        sections = [{"id": item.dimension, "title": _TITLES[item.dimension],
                     "items": [_brief_observation(observation, specs) for observation in item.observations
                               if observation.relation != "rsi_midpoint"]}
                    for item in synthesis.dimensions if item.observations]
    return {
        "version": "answer-presentation-v1", "kind": "research",
        "request_id": request.request_id, "instrument_id": request.instrument_id,
        "trade_date": report.fields[0].trade_date, "headline": headline, "sections": sections,
    }
