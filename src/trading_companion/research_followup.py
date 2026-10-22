"""One bounded interpretation call; cached numbers and relations are rendered locally."""

from dataclasses import dataclass, field
import json
import re

from trading_companion.agent_input import InputIssue, parse_indicator_specs
from trading_companion.errors import InvalidRequestError
from trading_companion.indicator_knowledge import KnowledgeAnswer, normalized, topic_key, topic_spans
from trading_companion.model import ModelCallError, ModelConfigurationError, ModelRequestError
from trading_companion.multi_contracts import IndicatorSpec
from trading_companion.relation_explanation import MAX_CLAIMS, validate_indicator_explanation
from trading_companion.research_memory import SavedResearch
from trading_companion.research_report import build_research_report, render_report_fields, indicator_label
from trading_companion.research_tool import ToolExecution
from trading_companion.security_directory import SecurityDirectory
from trading_companion.tool_model import ToolCapableModel, ToolDefinition, ToolMessage, ToolModelResponse


FOLLOWUP_TOOL_NAME = "interpret_saved_research_question"
FOLLOWUP_TOOL = ToolDefinition(FOLLOWUP_TOOL_NAME,
    "Interpret a question about ONE saved research result. No price requests or state changes. "
    "Copy ALL explicit indicator mentions verbatim, including periods. Submit facts for numeric lookup, "
    "explain for definitions with saved values, relations for checked comparisons, clarify for ambiguity, "
    "unsupported for forecasts, causal news explanations, trades, changed scope or other tasks. "
    "Never invent values, identifiers, indicators, citations or extra fields.",
    {"type": "object", "additionalProperties": False,
     "required": ["request_id", "action", "topic_mentions"], "properties": {
         "request_id": {"type": "string"},
         "action": {"type": "string", "enum": ["facts", "explain", "relations", "clarify", "unsupported"]},
         "topic_mentions": {"type": "array", "maxItems": 12, "items": {"type": "string"}},
     }})
_VIEWS = {"查看上次结果", "查看上次研究结果", "查看上次成功结果", "查看上一轮结果", "上次结果", "再看一下上次结果"}
_CHANGE = re.compile(r"改成|改为|修改|更改|调整|替换|换成|换为|添加|删除|增加|移除|沿用.*条件")
_CUE = re.compile(r"上次|上一轮|刚才|刚刚|这里|这个|这些|那.*呢|结果|为什么|多少|高于|低于|上穿|下穿|金叉|死叉")
_FORBIDDEN = re.compile(r"下单|买入|卖出|该买|该卖|目标价|止盈|止损|上涨概率|收益|保证|预测|新闻|公告|财务|基本面|消息|忽略|编造|捏造")
_DATES = re.compile(r"[0-9]{4}[-年/]|今天|今日|昨天|昨日|明天|未来|最新|实时|最近|截至|截止|换股|改日期|数据源|腾讯|东方财富")
_UNSUPPORTED_TOPICS = re.compile(
    r"(?<![A-Za-z])(?:KDJ|OBV|CCI|DMI|ROC|WR|TRIX|SAR)(?![A-Za-z])|随机指标|能量潮|顺势指标",
    re.IGNORECASE)


def is_result_view(question: str) -> bool:
    return normalized(question).rstrip("。.!！?？").strip() in _VIEWS


def is_followup_question(question: str) -> bool:
    text = normalized(question)
    if re.match(r"(?:请)?(?:分析|研究)|新问题\s*[:：]", text):
        return False
    return is_result_view(text) or not _CHANGE.search(text) and bool(_CUE.search(text))


@dataclass(frozen=True)
class FollowupAnswer:
    status: str
    code: str
    text: str
    request_id: str | None = None
    action: str | None = None
    indicator_ids: tuple[str, ...] = ()
    historical: bool = False
    relation_count: int = 0
    relations_truncated: bool = False

    def to_dict(self) -> dict[str, object]:
        return {"version": "saved-research-answer-v1", "origin": "application",
                "validation_scope": "saved_fields_and_checked_relations_not_free_text_semantics",
                "status": self.status, "code": self.code, "request_id": self.request_id,
                "action": self.action, "indicator_ids": list(self.indicator_ids), "historical": self.historical,
                "relation_count": self.relation_count, "relations_truncated": self.relations_truncated,
                "market_data_requested": False, "text": self.text}


@dataclass(frozen=True)
class FollowupRun:
    answer: FollowupAnswer
    model_calls_attempted: int = 0
    model_response: ToolModelResponse | None = field(default=None, repr=False)
    failure_code: str | None = None


def _clarify(code, message):
    raise InputIssue("needs_clarification", code, message)


def _select_specs(mentions, request):
    selected = []
    for mention in mentions:
        text = normalized(mention)
        key = topic_key(text)
        kind = "macd" if key in ("dif", "dea", "histogram") else key
        if kind is None:
            _clarify("followup_topic_not_supported", "未识别该指标，请明确项目支持的指标名称及参数。")
        generic = not re.search(r"[0-9()]", text)
        if generic:
            kinds = ("sma", "ema") if text == "均线" else (kind,)
            matches = [spec for spec in request.indicators if spec.kind in kinds]
        else:
            chinese = re.fullmatch(r"([0-9]+)\s*(?:日|天)\s*.+", text)
            try:
                explicit = (IndicatorSpec(kind, window=int(chinese[1])) if chinese else
                            parse_indicator_specs(text)[0])
                matches = [spec for spec in request.indicators if spec == explicit]
            except (InvalidRequestError, ValueError, IndexError):
                _clarify("followup_topic_not_supported", "指标参数无法匹配，请明确完整指标实例。")
        if not matches:
            _clarify("indicator_not_in_saved_result", "上次结果没有计算该指标；不会补造或自动取数，请提交完整新问题。")
        if len(matches) != 1:
            _clarify("followup_indicator_ambiguous", "上次结果有多个匹配实例，请明确参数，例如SMA20或SMA60。")
        if matches[0] not in selected:
            selected.append(matches[0])
    return tuple(selected)


def _checked_relations(result, request, selected):
    identifiers = {spec.indicator_id for spec in selected}
    aliases = tuple((f"f{index}", fact) for index, fact in enumerate(result.facts))
    alias_index = {fact.fact_id: alias for alias, fact in aliases}
    fact_index = {fact.fact_id: fact for fact in result.facts}
    def relevant(item):
        if not identifiers or item.indicator_id in identifiers:
            return True
        if item.field not in ("mean_position", "mean_cross"):
            return False
        dependencies = {fact_index[key].indicator_id for key in item.dependencies if key in fact_index}
        return len(dependencies) == 2 and dependencies <= identifiers
    candidates = [item for item in result.relations if item.status == "available"
                  and relevant(item)]
    if not candidates:
        return (), False
    chosen = candidates[:MAX_CLAIMS]
    payload = {"version": "indicator-explanation-v1", "request_id": request.request_id,
        "instrument_id": request.instrument_id, "trade_date": result.latest_trade_date.isoformat(),
        "claims": [{"evidence_id": alias_index.get(item.fact_id, "missing"),
                    "relation": item.field, "value": item.value} for item in chosen]}
    execution = ToolExecution("executed", (), result, aliases)
    checked = validate_indicator_explanation(json.dumps(payload), execution=execution, expected_request=request)
    if checked.status != "accepted":
        _clarify("saved_relation_integrity_failed", "所请求的旧结果关系未通过当前证据检查，没有展示关系或重新取数。")
    return tuple(claim.text for claim in checked.claims), len(candidates) > len(chosen)


def render_saved_answer(snapshot: SavedResearch, *, action="overview", mentions=(), historical=False) -> FollowupAnswer:
    result, request = snapshot.result, snapshot.request
    report = build_research_report(result, expected_request=request)
    if report.status not in ("complete", "limited"):
        return FollowupAnswer("failed", "saved_result_integrity_failed", "已保存结果未通过当前检查；请重新研究。")
    try:
        selected = _select_specs(mentions, request)
        if action == "explain" and not mentions:
            _clarify("followup_topic_required", "希望解释哪一项指标？请提供名称，多个实例时请带参数。")
        title = ("上次成功研究结果（历史查看，不代表本轮新查询或最新行情）" if historical else
                 "基于会话中已保存的结果（未重新取数，不是实时行情）")
        lines = [title + "。", f"原研究：{snapshot.security.name}（{request.instrument_id}），"
                 f"{request.start_date} 至 {request.as_of_date}；实际最新交易日：{result.latest_trade_date}。",
                 f"结果保存于：{snapshot.saved_at.isoformat()}；查看不会延长有效期。"]
        if action == "overview":
            lines.append(report.text)
        else:
            lines.append("人工样例 / 非真实行情。" if result.data_authenticity == "synthetic" else
                         "开发CSV / 真实性未经验证。" if result.source == "csv" else "供应商历史日线，未经独立认证。")
            lines.append(f"原数据源：{request.source}；复权口径：{result.price_adjustment}。")
            identifiers = {spec.indicator_id for spec in selected}
            fields = tuple(item for item in report.fields if not identifiers or item.indicator_id in identifiers)
            if action == "explain":
                topics = tuple(dict.fromkeys(
                    _select_specs((mention,), request)[0].kind if normalized(mention) == "均线" else topic_key(mention)
                    for mention in mentions))
                lines.append(KnowledgeAnswer(topics).text)
            lines.extend(render_report_fields(fields))
            lines.extend(["限制：", *report.limitations])
        relations, truncated = _checked_relations(result, request, selected) if action in ("overview", "relations", "explain") else ((), False)
        if relations:
            lines.extend(["重新核对的已保存指标关系：", *relations])
        elif action == "relations":
            lines.append("所选指标没有可用关系；缺历史或未定义时不补造关系。")
        if truncated:
            lines.append("本次最多展示8条已核对关系，未声称覆盖全部；可明确指标缩小范围。")
        lines.append("不据此解释新闻原因、预测涨跌或给出交易目标；本轮没有查询行情。")
        return FollowupAnswer("accepted", "saved_result_shown" if action == "overview" else "saved_question_answered",
            "\n".join(lines), request.request_id, action, tuple(spec.indicator_id for spec in selected),
            historical, len(relations), truncated)
    except InputIssue as issue:
        return FollowupAnswer(issue.status, issue.code, str(issue), request.request_id, action, historical=historical)


def answer_saved_question(question: str, *, snapshot: SavedResearch, model: ToolCapableModel,
                          directory: SecurityDirectory, historical=False) -> FollowupRun:
    if is_result_view(question):
        return FollowupRun(render_saved_answer(snapshot, historical=historical))
    blocked = FollowupAnswer("out_of_scope", "followup_scope_not_supported",
        "本入口只解释已保存结果，不接受新日期/股票/来源、预测、新闻原因或交易要求；请明确完整新研究问题。")
    text = normalized(question)
    if _FORBIDDEN.search(text) or _DATES.search(text) or _CHANGE.search(text):
        return FollowupRun(blocked)
    if _UNSUPPORTED_TOPICS.search(text):
        return FollowupRun(FollowupAnswer("needs_clarification", "followup_topic_not_supported",
            "问题包含当前不支持的指标，未忽略该要求或重新取数；请明确只询问已计算的指标。"))
    for start, end in directory.mentioned_spans(question):
        match = directory.lookup(question[start:end])
        if match.status != "resolved" or match.candidates[0].instrument_id != snapshot.request.instrument_id:
            return FollowupRun(blocked)
    messages = [ToolMessage("system", "Interpret a saved-result question, not a fresh analysis. "
        "Return exactly one interpret_saved_research_question call. Cached scope: " + json.dumps({
            "request_id": snapshot.request.request_id, "instrument_id": snapshot.request.instrument_id,
            "start_date": snapshot.request.start_date.isoformat(), "as_of_date": snapshot.request.as_of_date.isoformat(),
            "indicators": [indicator_label(spec) for spec in snapshot.request.indicators]}, separators=(",", ":"))),
        ToolMessage("user", question)]
    response = None
    try:
        response = model.complete(messages, tools=(FOLLOWUP_TOOL,))
        if response.status != "tool_calls" or len(response.tool_calls) != 1 or response.tool_calls[0].name != FOLLOWUP_TOOL_NAME:
            _clarify("invalid_followup_response", "未取得可校验的追问理解结果，没有展示模型原文或重新查询。")
        args = response.tool_calls[0].arguments
        if (args.keys() != {"request_id", "action", "topic_mentions"} or args["request_id"] != snapshot.request.request_id
                or args["action"] not in ("facts", "explain", "relations", "clarify", "unsupported")
                or type(args["topic_mentions"]) is not list or len(args["topic_mentions"]) > 12
                or any(type(item) is not str or not item.strip() or len(item) > 80 for item in args["topic_mentions"])):
            _clarify("invalid_followup_response", "追问理解字段或结果编号不符合约定，没有展示模型原文或重新查询。")
        mentions = args["topic_mentions"]
        spans = [(match.start(), match.end()) for mention in mentions
                 for match in re.finditer(re.escape(normalized(mention)), text)]
        if (any(mention not in question for mention in mentions)
                or any(not any(start <= left and right <= end for start, end in spans) for left, right in topic_spans(text))):
            _clarify("ungrounded_followup_topics", "模型遗漏指标或提取了原问题中不存在的内容，请明确要解释的指标。")
        if args["action"] == "unsupported":
            answer = blocked
        elif args["action"] == "clarify":
            answer = FollowupAnswer("needs_clarification", "followup_needs_clarification", "请明确需要查看数值、解释含义，还是比较已有关系。")
        else:
            answer = render_saved_answer(snapshot, action=args["action"], mentions=mentions, historical=historical)
        return FollowupRun(answer, 1, response)
    except InputIssue as issue:
        return FollowupRun(FollowupAnswer(issue.status, issue.code, str(issue)), 1, response)
    except (ModelCallError, ModelRequestError, ModelConfigurationError) as error:
        return FollowupRun(FollowupAnswer("failed", "followup_model_unavailable",
            "本轮追问理解不可用；没有重试、重新取数或展示模型原文，可用“查看上次结果”查看可信事实。"),
            1, response, error.code if isinstance(error, ModelCallError) else "model_request_or_configuration_error")
