"""Single-dispatch read-only research execution with one attempt per trusted turn."""

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
import json
import re
from threading import Lock

from trading_companion.akshare_adapter import AkshareDailyBarSource
from trading_companion.data_source import CsvDailyBarSource
from trading_companion.errors import DataSourceError, DataValidationError, InvalidRequestError
from trading_companion.model import ModelConfigurationError, ModelRequestError
from trading_companion.multi_analysis import IndicatorSpec, MultiIndicatorRequest, analyze_indicators
from trading_companion.multi_contracts import ALGORITHM_VERSIONS, Fact, MultiIndicatorResult
from trading_companion.tencent_adapter import TencentDailyBarSource
from trading_companion.tool_model import ToolDefinition, ToolMessage, ToolModelResponse, validate_tool_messages
from trading_companion.turn_control import TurnStopped, checkpoint, reserve_analysis


TOOL_NAME = "analyze_daily_indicators"
RELATION_RULE_CATALOG = {
    "price_position": "MA: close vs mean. BOLL: close vs bands including equality; coincident bands explicit.",
    "mean_change": "Current mean vs immediately previous mean: increasing/decreasing/unchanged.",
    "price_cross": "Up: previous close<=mean and current close>mean; down: previous close>=mean and current close<mean.",
    "mean_position": "Left named mean vs right named mean: above/below/equal.",
    "mean_cross": "Up: previous left<=right and current left>right; down: previous left>=right and current left<right.",
    "dif_dea_position": "Current DIF vs DEA: above/below/equal.",
    "dif_dea_cross": "Up: previous DIF<=DEA and current DIF>DEA; down: previous DIF>=DEA and current DIF<DEA.",
    "histogram_change": "Current histogram vs immediately previous histogram; histogram=2*(DIF-DEA).",
    "reference_zone": "RSI<30: below_30; RSI>70: above_70; exact 30/70 and between: neutral.",
    "bands_state": "Upper equals lower: coincident; otherwise distinct.",
    "atr_change": "Current ATR vs previous ATR; numeric volatility scale only, no price direction.",
    "relative_volume_position": "Current volume / strictly prior n-record mean compared with 1.",
    "current_vs_prior_mean": "Current share volume vs strictly prior n-record mean, including zero baseline.",
    "strength_zone": "ADX below 25 or at/above 25; reference strength, not direction or a probability.",
    "di_position": "+DI versus -DI: above/below/equal; direction reference distinct from strength.",
    "obv_change": "Current OBV vs previous; snapshot starts at zero, never a currency fund-flow amount.",
}
SEED_METHOD_CATALOG = {f"c{index}": method for index, method in enumerate((
    "rolling_close_arithmetic_mean", "first_n_closes_sma", "fast_ema_minus_slow_ema",
    "first_signal_valid_dif_sma", "2_times_dif_minus_dea",
    "first_n_changes_gain_loss_means_wilder", "rolling_mean_population_stddev",
    "first_n_true_ranges_mean_wilder", "observed_share_count",
    "current_inclusive_window_mean", "strictly_prior_window_mean",
    "current_divided_by_prior_window_mean",
    "first_n_dx_mean_wilder", "wilder_positive_dm_divided_by_wilder_tr",
    "wilder_negative_dm_divided_by_wilder_tr", "cumulative_signed_volume_first_record_zero",
    "rolling_close_weighted_by_exact_share_volume",
))}
RESEARCH_TOOL_DEFINITION = ToolDefinition(TOOL_NAME,
    "Read-only daily research. No trading. research-rules-v1 uses full precision and adjacent records: "
    + json.dumps(RELATION_RULE_CATALOG, separators=(",", ":"))
    + " research-seeds-v1: " + json.dumps(SEED_METHOD_CATALOG, separators=(",", ":")), {
    "type": "object", "properties": {
        "instrument_id": {"type": "string", "description": "Complete SH/SZ/BJ identifier."},
        "start_date": {"type": "string", "description": "Explicit YYYY-MM-DD."},
        "as_of_date": {"type": "string", "description": "Explicit completed daily-bar cutoff."},
        "source": {"type": "string", "enum": ["tencent", "akshare_eastmoney"]},
        "indicators": {"type": "array", "items": {"type": "object", "properties": {
            "kind": {"type": "string", "enum": ["sma", "ema", "macd", "rsi", "boll", "atr", "volume", "adx", "obv", "vwma"]},
            "window": {"type": "integer"}, "fast": {"type": "integer"},
            "slow": {"type": "integer"}, "signal": {"type": "integer"},
            "stddev_multiplier": {"type": "number"},
        }, "required": ["kind"], "additionalProperties": False}},
    }, "required": ["instrument_id", "start_date", "as_of_date"], "additionalProperties": False,
})


class ToolExecutionError(Exception):
    """A fixed classified boundary failure, never the original provider exception."""

    def __init__(self, code: str):
        self.code = code
        super().__init__("Research tool proposal cannot be executed")


@dataclass(frozen=True)
class ToolExecution:
    status: str
    messages: tuple[ToolMessage, ...] = field(repr=False)
    full_result: MultiIndicatorResult | None = field(default=None, repr=False)
    _fact_aliases: tuple[tuple[str, Fact], ...] = field(default=(), repr=False)
    projection_metrics: dict[str, int] = field(default_factory=dict)

    @property
    def fact_aliases(self) -> dict[str, Fact]:
        """Resolve request-local aliases to detached full server facts and dependencies."""
        return deepcopy(dict(self._fact_aliases))


def _iso_date(value):
    if not isinstance(value, str) or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value) is None:
        raise InvalidRequestError("Explicit ISO dates are required")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise InvalidRequestError("Invalid ISO date") from None


def _request(arguments, *, development, request_id=None):
    required = {"instrument_id", "start_date", "as_of_date"}
    if type(arguments) is not dict or arguments.keys() - (required | {"indicators", "source"}) or required - arguments.keys():
        raise InvalidRequestError("Invalid research argument keys")
    if development and "source" in arguments:
        raise InvalidRequestError("Development source is selected only by trusted configuration")
    source = "csv" if development else arguments.get("source", "tencent")
    if not development and source not in ("tencent", "akshare_eastmoney"):
        raise InvalidRequestError("Only explicit online research providers are supported")
    start, cutoff = _iso_date(arguments["start_date"]), _iso_date(arguments["as_of_date"])
    if not development and cutoff >= datetime.now(timezone(timedelta(hours=8))).date():
        raise InvalidRequestError("Daily bars must precede today in Shanghai")
    parameters = {}
    if request_id is not None:
        parameters["request_id"] = request_id
    if "indicators" in arguments:
        requested = arguments["indicators"]
        if type(requested) is not list or not requested:
            raise InvalidRequestError("Indicators must be a nonempty array")
        specs = []
        for item in requested:
            if type(item) is not dict or "kind" not in item:
                raise InvalidRequestError("Indicator entries must be explicit objects")
            specs.append(IndicatorSpec(item["kind"], **{key: value for key, value in item.items() if key != "kind"}))
        parameters["indicators"] = tuple(specs)
    return MultiIndicatorRequest(arguments["instrument_id"], start, cutoff, source=source, **parameters)


def _build_source(request):
    if request.source == "tencent":
        return TencentDailyBarSource(request.instrument_id, request.start_date, request.as_of_date)
    if request.source == "akshare_eastmoney":
        return AkshareDailyBarSource(request.instrument_id, request.start_date, request.as_of_date)
    raise ToolExecutionError("source_configuration_error")


def _metrics(payload):
    nodes = 0
    stack = [payload]
    while stack:
        value = stack.pop()
        nodes += 1
        if type(value) is dict:
            nodes += len(value)
            stack.extend(value.values())
        elif type(value) is list:
            stack.extend(value)
    try:
        return {"ascii_characters": len(json.dumps(payload, ensure_ascii=True, allow_nan=False,
                                                    separators=(",", ":"))), "nodes": nodes}
    except (ValueError, TypeError, OverflowError):
        return {"nodes": nodes, "serialization_failed": 1}


_WARNINGS = {
    "仅按日线日期截取；未验证历史发布时间、修订版本或当时可得性。": "date_filter_only_not_point_in_time_verified",
    "未校验证券身份、ST、上市时长、停复牌或可交易性；不推断缺失原因。": "security_and_tradability_unverified_do_not_infer_missing_data_causes",
    "多个指标使用相同价格与量能，不能视为独立证据或投票胜率。": "correlated_indicators_are_not_independent_votes_or_win_rates",
    "公司行动可能影响不复权价格及多个指标；未验证具体公司行动。": "corporate_actions_unverified_may_affect_unadjusted_prices",
    "开发样例/非真实行情分析；CSV真实性与复权未知，未经独立验证。": "development_csv_not_real_market_analysis_authenticity_and_adjustment_unknown",
    "受信开发配置标记为人工样例/非真实行情，不用于市场建议。": "trusted_configuration_marks_synthetic_not_market_advice",
    "供应商量额单位按适配器转换；覆盖与精度不代表已全面验证。": "adapter_units_converted_coverage_and_precision_not_fully_verified",
    "腾讯上游量额可能已舍入，单位换算不能恢复源端丢失的精度。": "tencent_volume_amount_may_be_rounded_conversion_cannot_restore_precision",
    "实际最新日早于请求截止日；不据此推断休市或停牌。": "latest_precedes_cutoff_do_not_infer_holiday_or_suspension",
    "部分递推分量初始化后更新不足相关最长周期的3倍，可能受计算起点影响；这是项目提示阈值，不是收敛或可靠性保证。": "start_effect_if_updates_below_3x_longest_period_not_convergence_or_reliability_guarantee",
    "部分指标发生数值计算错误；该实例受限，其余有效结果保留。": "numeric_failure_isolated_other_valid_indicators_retained",
    "全部请求数值字段不可用，禁止依据本次结果生成综合走势判断。": "no_available_computed_fields_no_trend_synthesis",
}


def _project(result, *, development):
    identifiers = {spec.indicator_id: f"i{index}" for index, spec in enumerate(result.request.indicators)}
    aliases = {fact.fact_id: f"f{index}" for index, fact in enumerate(result.facts)}
    local_facts = tuple((aliases[fact.fact_id], fact) for fact in result.facts)
    current = result.latest_trade_date
    previous = result.bars[-2].trade_date if len(result.bars) > 1 else None
    kinds = {spec.kind for spec in result.request.indicators}
    # Field-level exceptions are local to this result; the raw bar volume is always present.
    units = {"default": "CNY/share", "volume_shares": "shares"}
    if "rsi" in kinds:
        units["rsi.value"] = "index_points"
    if "volume" in kinds:
        units.update(volume_ma="shares", previous_volume_ma="shares", relative_volume="multiple")
    if "adx" in kinds:
        units.update({f"adx.{field}": "index_points" for field in ("value", "plus_di", "minus_di")})
    if "obv" in kinds:
        units["obv.value"] = "shares"
    indicators = [[identifiers[spec.indicator_id], spec.kind, spec.parameters, ALGORITHM_VERSIONS[spec.kind]]
                  for spec in result.request.indicators]
    latest, components, states = [], [], {}
    seed_codes = {method: short for short, method in SEED_METHOD_CATALOG.items()}

    def state(status, reason):
        key = (status, reason)
        if key not in states:
            states[key] = f"s{len(states)}"
        return states[key]

    for indicator in result.indicators:
        short = identifiers[indicator.indicator_id]
        for name, value in indicator.latest.items():
            latest.append([short, name, state(value.status, value.reason), value.required_bars,
                           value.missing_bars, aliases.get(value.fact_id)])
        for component in indicator.components:
            components.append([short, component.name, component.period, component.required_bars,
                               seed_codes[component.seed_method], component.first_available_date.isoformat()
                               if component.first_available_date else None,
                               component.recursive_updates, list(component.related_periods),
                               component.warning_threshold_updates, component.start_effect_warning])
    numeric_facts = []
    for fact in result.facts:
        if not fact.dependencies:
            numeric_facts.append([aliases[fact.fact_id], identifiers.get(fact.indicator_id, "bar"),
                                  fact.field, 0 if fact.trade_date == current else 1,
                                  fact.value])
    relations = []
    for relation in result.relations:
        reference = identifiers.get(relation.indicator_id)
        if reference is None:
            # Pair IDs are created by M1b from these exact indicator instance IDs.
            members = [short for original, short in identifiers.items()
                       if relation.indicator_id == original or original in relation.indicator_id]
            reference = "/".join(members)
        relations.append([aliases.get(relation.fact_id), reference, relation.field,
                          state(relation.status, relation.reason), relation.value, relation.missing_bars,
                          [aliases[dependency] for dependency in relation.dependencies]])
    payload = {
        "version": "research-view-v1", "status": "analysis_result",
        "request_id": result.request.request_id, "instrument_id": result.request.instrument_id,
        "range": [result.request.start_date.isoformat(), result.request.as_of_date.isoformat()],
        "source": result.source, "mode": "trusted_development_csv" if development else "online",
        "price_adjustment": result.price_adjustment, "data_authenticity": result.data_authenticity,
        "retrieved_at": result.retrieved_at.isoformat() if result.retrieved_at else None,
        "data_status": result.data_status, "calculation_status": result.calculation_status,
        "reason": result.reason, "synthesis_allowed": result.synthesis_allowed,
        "dates": [current.isoformat() if current else None, previous.isoformat() if previous else None],
        "calculation_start": result.bars[0].trade_date.isoformat() if result.bars else None,
        "bars_used": len(result.bars),
        "warnings": [_WARNINGS.get(warning, warning) for warning in result.warnings],
        "alias_scope": "request_id-local; fact_aliases resolve IDs/dependencies",
        "view_scope": "all latest fields/relations; current/prior facts; full series local",
        "rule_catalog": "research-rules-v1; relation.field",
        "seed_catalog": "research-seeds-v1",
        "units": units,
        "tables": {
            "indicators": {"columns": ["id", "kind", "parameters", "algorithm"], "rows": indicators},
            "states": {"columns": ["id", "status", "reason"],
                       "rows": [[short, status, reason] for (status, reason), short in states.items()]},
            "latest_fields": {"columns": ["indicator", "field", "state", "required", "missing", "fact"], "rows": latest},
            "components": {"columns": ["indicator", "component", "period", "required", "seed", "first_date", "updates", "related_periods", "hint_threshold", "start_hint"], "rows": components},
            "numeric_facts": {"columns": ["fact", "indicator", "field", "date_index", "value"], "rows": numeric_facts},
            "relations": {"columns": ["fact", "indicator_or_pair", "field", "state", "value", "missing", "dependencies"], "rows": relations},
        },
    }
    return payload, local_facts


def _error(call, code):
    return ToolMessage("tool", call_id=call.call_id, name=call.name, result={
        "status": "rejected", "code": code, "synthesis_allowed": False,
        "message": "Research request was not executed or could not return a usable result.",
    })


def _too_large(call, request_id, indicator_count, bars_used):
    return ToolMessage("tool", call_id=call.call_id, name=call.name, result={
        "status": "result_too_large", "synthesis_allowed": False,
        "request_id": request_id, "full_result_retained": True,
        "indicator_count": indicator_count, "bars_used": bars_used,
        "message": "Complete projection exceeds protocol or remaining history capacity; no fields were silently removed.",
    })


class ResearchToolExecutor:
    """Trusted application creates a fresh instance per user turn; attempts cannot reset."""

    def __init__(self, *, development_source: CsvDailyBarSource | None = None,
                 request_id: str | None = None):
        if development_source is not None and (type(development_source) is not CsvDailyBarSource
                                                or type(development_source.is_synthetic) is not bool):
            raise ModelConfigurationError("Development research source must be a trusted CSV configuration")
        if request_id is not None and (type(request_id) is not str
                                       or re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", request_id) is None):
            raise ModelConfigurationError("Request ID must be bounded and supplied by trusted application code")
        self._development_source = development_source
        self._request_id = request_id
        self._used = 0
        self._lock = Lock()

    @property
    def analyses_used(self) -> int:
        with self._lock:
            return self._used

    def execute(self, response: ToolModelResponse, *, messages) -> ToolExecution:
        history, _, seen_ids = validate_tool_messages(messages, tools=(RESEARCH_TOOL_DEFINITION,))
        if type(response) is not ToolModelResponse or response.status != "tool_calls":
            raise ToolExecutionError("not_a_tool_proposal")
        if any(call.name != TOOL_NAME for call in response.tool_calls):
            raise ToolExecutionError("unknown_tool")
        if any(call.call_id in seen_ids for call in response.tool_calls):
            raise ToolExecutionError("reused_call_id")
        rejected = tuple(_error(call, "multiple_calls_not_allowed" if len(response.tool_calls) != 1
                                else "invalid_arguments") for call in response.tool_calls)

        def check_history(outputs):
            validate_tool_messages([*history, response.as_message(), *outputs],
                                   tools=(RESEARCH_TOOL_DEFINITION,))

        # Verify that even an error can close the proposal before spending an attempt or IO.
        check_history(rejected)
        if len(response.tool_calls) != 1:
            return ToolExecution("rejected", rejected)
        call = response.tool_calls[0]
        try:
            request = _request(call.arguments, development=self._development_source is not None,
                               request_id=self._request_id)
        except (InvalidRequestError, TypeError, ValueError):
            return ToolExecution("rejected", rejected)
        # Reserve the largest fallback envelope before any actual attempt or source construction.
        check_history((_too_large(call, request.request_id, 12, 366),))
        check_history((_error(call, "analysis_budget_exhausted"),))
        with self._lock:
            if self._used:
                return ToolExecution("rejected", (_error(call, "analysis_budget_exhausted"),))
            reserve_analysis()
            # Consume before construction/read; a failed attempt cannot be retried invisibly.
            self._used = 1
        try:
            source = self._development_source if self._development_source is not None else _build_source(request)
            full_result = analyze_indicators(source, request,
                                             allow_development_csv=self._development_source is not None)
        except TurnStopped:
            raise
        except (ModelConfigurationError, InvalidRequestError, DataSourceError, DataValidationError):
            return ToolExecution("failed", (_error(call, "execution_failed"),))
        except Exception:
            raise ToolExecutionError("internal_error") from None
        checkpoint()
        payload, local_facts = _project(full_result, development=self._development_source is not None)
        try:
            message = ToolMessage("tool", call_id=call.call_id, name=call.name, result=payload)
            metrics = _metrics(payload)
            check_history((message,))
        except ModelRequestError:
            metrics = _metrics(payload)
            message = _too_large(call, request.request_id, len(request.indicators), len(full_result.bars))
            check_history((message,))
            return ToolExecution("limited", (message,), full_result, local_facts, metrics)
        return ToolExecution("executed", (message,), full_result, local_facts, metrics)
