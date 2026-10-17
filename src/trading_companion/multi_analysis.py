"""One bounded data read, aligned calculations, and deterministic evidence."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from itertools import combinations
from math import isfinite

from trading_companion import indicators as calculations
from trading_companion.daily_bar import DailyBar
from trading_companion.data_source import DailyBarSource
from trading_companion.errors import DataSourceError, DataValidationError, InvalidRequestError
from trading_companion.multi_contracts import (
    ALGORITHM_VERSIONS, DEFAULT_INDICATORS, ComponentMetadata, Fact, FieldResult,
    IndicatorResult, IndicatorRow, IndicatorSpec, MultiIndicatorRequest,
    MultiIndicatorResult, RelationResult,
)


def _fact_id(request, trade_date, indicator_id, field):
    return (f"{request.request_id}|{request.instrument_id}|{trade_date.isoformat()}|"
            f"{indicator_id}|{field}")


def _validate_source(source, request, allow_development_csv):
    if type(request) is not MultiIndicatorRequest:
        raise InvalidRequestError("A validated multi-indicator request is required")
    # Revalidate a request before any provider read, including trusted object injection.
    MultiIndicatorRequest(request.instrument_id, request.start_date, request.as_of_date,
                          request.indicators, request.source, request.request_id)
    if source.source_name != request.source:
        raise InvalidRequestError("Requested and configured data sources differ")
    if type(allow_development_csv) is not bool:
        raise InvalidRequestError("Development CSV permission must be a boolean")
    if request.source == "csv":
        if not allow_development_csv:
            raise InvalidRequestError("CSV requires explicit trusted development configuration")
        if type(getattr(source, "is_synthetic", False)) is not bool:
            raise InvalidRequestError("CSV authenticity marker must be a boolean")
    else:
        today = datetime.now(timezone(timedelta(hours=8))).date()
        if request.as_of_date >= today:
            raise InvalidRequestError("Only completed daily bars before today in Shanghai are supported")
        if source.adjustment != "unadjusted":
            raise InvalidRequestError("Online analysis requires explicitly unadjusted daily bars")
    configured_fields = {"instrument_id": request.instrument_id,
                         "start_date": request.start_date, "end_date": request.as_of_date}
    for name, expected in configured_fields.items():
        if hasattr(source, name) and getattr(source, name) != expected:
            raise InvalidRequestError("Requested instrument or range differs from configured source")


def _validate_records(bars, request):
    if not isinstance(bars, list):
        raise DataValidationError("Source must return a list of DailyBar records")
    previous_date = None
    for bar in bars:
        if type(bar) is not DailyBar:
            raise DataValidationError("Source contains invalid daily records")
        try:
            DailyBar.__post_init__(bar)
        except (ValueError, TypeError, OverflowError) as error:
            raise DataValidationError("Source contains invalid OHLCV fields") from error
        if bar.instrument_id != request.instrument_id:
            raise DataValidationError("Source contains another instrument")
        if previous_date is not None and bar.trade_date <= previous_date:
            raise DataValidationError("Source dates are not strictly increasing")
        previous_date = bar.trade_date


def _field(request, bars, index, indicator_id, name, value, required,
           unit="CNY/share", status=None):
    if status is None:
        if (value is None) != (index + 1 < required):
            raise ValueError("Indicator availability contradicts its record threshold")
        status = "available" if value is not None else "insufficient_history"
    if ((status == "available" and index + 1 < required)
            or (status == "undefined" and index + 1 < required)
            or (status == "insufficient_history" and index + 1 >= required)):
        raise ValueError("Indicator status contradicts its record threshold")
    if status == "available":
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or (isinstance(value, float) and not isfinite(value))):
            raise ValueError("Indicator returned an invalid numeric field")
    elif value is not None:
        raise ValueError("Unavailable indicator fields must be null")
    reasons = {"available": "ready", "insufficient_history": "insufficient_records",
               "undefined": "zero_prior_volume_baseline",
               "calculation_error": "numeric_calculation_failed"}
    fact_id = (_fact_id(request, bars[index].trade_date, indicator_id, name)
               if status == "available" and index >= len(bars) - 2 else None)
    return FieldResult(bars[index].trade_date, status, value, reasons[status], unit,
                       required, index + 1, max(0, required - index - 1), fact_id)


def _definitions(spec):
    parameters = spec.parameters
    window = parameters.get("window", 0)
    if spec.kind == "macd":
        slow, signal = parameters["slow"], parameters["signal"]
        return {"dif": (slow, "CNY/share"),
                "dea": (slow + signal - 1, "CNY/share"),
                "histogram": (slow + signal - 1, "CNY/share")}
    if spec.kind == "boll":
        return {name: (window, "CNY/share") for name in ("middle", "upper", "lower")}
    if spec.kind == "volume":
        return {"volume_shares": (1, "shares"), "volume_ma": (window, "shares"),
                "previous_volume_ma": (window + 1, "shares"),
                "relative_volume": (window + 1, "multiple")}
    return {"value": (window + (spec.kind in ("rsi", "atr")),
                      "index_points" if spec.kind == "rsi" else "CNY/share")}


def _components(spec, bars):
    parameters = spec.parameters
    window = parameters.get("window", 0)
    # (name, period, required records, seed, recursive, relevant periods)
    if spec.kind == "macd":
        fast, slow, signal = (parameters[name] for name in ("fast", "slow", "signal"))
        definitions = (
            ("fast_ema", fast, fast, "first_n_closes_sma", True, (fast,)),
            ("slow_ema", slow, slow, "first_n_closes_sma", True, (slow,)),
            ("dif", slow, slow, "fast_ema_minus_slow_ema", True, (fast, slow)),
            ("dea", signal, slow + signal - 1, "first_signal_valid_dif_sma", True,
             (fast, slow, signal)),
            ("histogram", signal, slow + signal - 1, "2_times_dif_minus_dea", True,
             (fast, slow, signal)),
        )
    elif spec.kind in ("rsi", "atr", "ema"):
        seed = {"rsi": "first_n_changes_gain_loss_means_wilder",
                "atr": "first_n_true_ranges_mean_wilder",
                "ema": "first_n_closes_sma"}[spec.kind]
        definitions = (("value", window, window + (spec.kind != "ema"), seed, True, (window,)),)
    else:
        seed = {"sma": "rolling_close_arithmetic_mean", "boll": "rolling_mean_population_stddev",
                "volume": "current_inclusive_mean_and_strictly_prior_window"}[spec.kind]
        definitions = tuple((name, window or 1, required, seed, False, (window,))
                            for name, (required, _) in _definitions(spec).items())
        if spec.kind == "volume":
            definitions = (
                ("volume_shares", 1, 1, "observed_share_count", False, (1,)),
                ("volume_ma", window, window, "current_inclusive_window_mean", False, (window,)),
                ("previous_volume_ma", window, window + 1, "strictly_prior_window_mean", False, (window,)),
                ("relative_volume", window, window + 1, "current_divided_by_prior_window_mean",
                 False, (window,)),
            )
    metadata = []
    for name, period, required, seed, recursive, periods in definitions:
        updates = max(0, len(bars) - required) if recursive else 0
        threshold = 3 * max(periods) if recursive else None
        metadata.append(ComponentMetadata(
            name, period, required, bars[0].trade_date, seed,
            bars[required - 1].trade_date if len(bars) >= required else None,
            updates, periods, bool(recursive and len(bars) >= required and updates < threshold),
            threshold,
        ))
    return tuple(metadata)


def _calculate(spec, bars, request):
    functions = {"sma": calculations.sma_close, "ema": calculations.ema_close,
                 "macd": calculations.macd_close, "rsi": calculations.rsi_close,
                 "boll": calculations.boll_close, "atr": calculations.atr,
                 "volume": calculations.volume_activity}
    definitions = _definitions(spec)
    failed = False
    try:
        values = functions[spec.kind](bars, **spec.parameters)
        if len(values) != len(bars):
            # A broken adapter/function contract is a programming failure, not a market state.
            raise RuntimeError("Indicator output length differs from its input")
        rows = []
        for index, value in enumerate(values):
            fields = {}
            for name, (required, unit) in definitions.items():
                number = value if name == "value" else getattr(value, name)
                status = (value.relative_volume_status
                          if spec.kind == "volume" and name == "relative_volume" else None)
                fields[name] = _field(request, bars, index, spec.indicator_id, name,
                                      number, required, unit, status)
            rows.append(IndicatorRow(bars[index].trade_date, fields))
    except (ValueError, OverflowError, ArithmeticError):
        # Only expected numeric failures are contained; unexpected exceptions must surface.
        failed = True
        rows = [IndicatorRow(bar.trade_date, {
            name: _field(request, bars, index, spec.indicator_id, name, None, required,
                         unit, "calculation_error")
            for name, (required, unit) in definitions.items()
        }) for index, bar in enumerate(bars)]
    components = _components(spec, bars)
    if failed:
        components = tuple(replace(item, first_available_date=None, recursive_updates=0,
                                   start_effect_warning=False) for item in components)
    else:
        # Undefined values can delay actual availability beyond the minimum record count.
        components = tuple(
            replace(item, first_available_date=next(
                (row.trade_date for row in rows
                 if row.fields[item.name].status == "available"), None))
            if item.name in definitions else item for item in components
        )
    return IndicatorResult(spec.indicator_id, spec.kind, spec.parameters,
                           ALGORITHM_VERSIONS[spec.kind], components, tuple(rows))


def _compare(left, right):
    return "above" if left > right else "below" if left < right else "equal"


def _change(current, previous):
    return "increasing" if current > previous else "decreasing" if current < previous else "unchanged"


def _cross(previous_left, previous_right, current_left, current_right):
    # Compare operands directly: subtracting extreme finite values could overflow.
    if previous_left <= previous_right and current_left > current_right:
        return "cross_up"
    if previous_left >= previous_right and current_left < current_right:
        return "cross_down"
    return "no_cross"


def _relation(request, bars, indicator_id, name, dependencies, required, evaluate, rule):
    statuses = [item.status if item is not None else "insufficient_history" for item in dependencies]
    if "calculation_error" in statuses:
        status, reason = "calculation_error", "dependency_calculation_failed"
    elif "undefined" in statuses:
        status, reason = "undefined", "dependency_undefined"
    elif "insufficient_history" in statuses:
        status, reason = "insufficient_history", "dependency_history_insufficient"
    else:
        status, reason = "available", "ready"
    ready = status == "available"
    value = evaluate(*(item.value for item in dependencies)) if ready else None
    # Incomplete relations cite no numeric evidence as a completed conclusion.
    evidence = tuple(item.fact_id for item in dependencies) if ready else ()
    if ready and any(item is None for item in evidence):
        raise RuntimeError("Relation refers to a missing evidence identifier")
    fact_id = _fact_id(request, bars[-1].trade_date, indicator_id, name) if ready else None
    return RelationResult(f"{indicator_id}:{name}", indicator_id, name, bars[-1].trade_date,
                          status, value, reason, required, len(bars), max(0, required - len(bars)),
                          evidence, rule, fact_id)


def _boll_position(close, middle, upper, lower):
    if upper == lower:
        return {"above": "above_coincident_bands", "below": "below_coincident_bands",
                "equal": "at_coincident_bands"}[_compare(close, middle)]
    if close > upper:
        return "above_upper"
    if close == upper:
        return "at_upper"
    if close > middle:
        return "between_middle_and_upper"
    if close == middle:
        return "at_middle"
    if close > lower:
        return "between_lower_and_middle"
    return "at_lower" if close == lower else "below_lower"


def _relations(request, bars, results, market):
    relations = []
    current_close = market[-1]["close_price"]
    previous_close = market[-2]["close_price"] if len(market) >= 2 else None

    def add(indicator_id, name, dependencies, required, evaluate, rule):
        relations.append(_relation(request, bars, indicator_id, name, dependencies,
                                   required, evaluate, rule))

    for result in results:
        current = result.latest
        previous = result.series[-2].fields if len(result.series) >= 2 else {}
        identifier, kind = result.indicator_id, result.kind
        if kind in ("sma", "ema"):
            field = current["value"]
            required = field.required_bars
            add(identifier, "price_position", (current_close, field), required, _compare,
                "close compared with current mean at full precision")
            add(identifier, "mean_change", (field, previous.get("value")), required + 1, _change,
                "current mean compared with immediately previous record")
            add(identifier, "price_cross", (previous_close, previous.get("value"), current_close, field),
                required + 1, _cross, "up: previous<=mean and current>mean; down: previous>=mean and current<mean")
        elif kind == "macd":
            required = current["dea"].required_bars
            add(identifier, "dif_dea_position", (current["dif"], current["dea"]), required, _compare,
                "current DIF compared with DEA at full precision")
            add(identifier, "dif_dea_cross", (previous.get("dif"), previous.get("dea"),
                                              current["dif"], current["dea"]), required + 1, _cross,
                "up: previous DIF<=DEA and current DIF>DEA; down: previous DIF>=DEA and current DIF<DEA")
            add(identifier, "histogram_change", (current["histogram"], previous.get("histogram")),
                required + 1, _change, "current histogram compared with immediately previous record")
        elif kind == "rsi":
            add(identifier, "reference_zone", (current["value"],), current["value"].required_bars,
                lambda value: "below_30" if value < 30 else "above_70" if value > 70 else "neutral",
                "below 30 / above 70; exact 30 and 70 are neutral reference boundaries")
        elif kind == "boll":
            add(identifier, "price_position", (current_close, current["middle"],
                                              current["upper"], current["lower"]),
                current["middle"].required_bars, _boll_position,
                "close compared with current population-standard-deviation bands, including equality")
            add(identifier, "bands_state", (current["upper"], current["lower"]),
                current["middle"].required_bars,
                lambda upper, lower: "coincident" if upper == lower else "distinct",
                "upper equals lower identifies coincident bands")
        elif kind == "atr":
            add(identifier, "atr_change", (current["value"], previous.get("value")),
                current["value"].required_bars + 1, _change,
                "ATR numeric volatility-scale change only; no price-direction conclusion")
        elif kind == "volume":
            required = current["relative_volume"].required_bars
            add(identifier, "relative_volume_position", (current["relative_volume"],), required,
                lambda value: _compare(value, 1), "relative volume compared with one")
            add(identifier, "current_vs_prior_mean", (current["volume_shares"],
                                                       current["previous_volume_ma"]), required, _compare,
                "current share volume compared with strictly prior-window mean; zero baseline allowed here")
    means = [item for item in results if item.kind in ("sma", "ema")]
    for left, right in combinations(means, 2):
        identifier = f"ma_pair[{left.indicator_id};{right.indicator_id}]"
        left_current, right_current = left.latest["value"], right.latest["value"]
        required = max(left_current.required_bars, right_current.required_bars)
        left_previous = left.series[-2].fields["value"] if len(bars) >= 2 else None
        right_previous = right.series[-2].fields["value"] if len(bars) >= 2 else None
        add(identifier, "mean_position", (left_current, right_current), required, _compare,
            "left named mean compared with right named mean at full precision")
        add(identifier, "mean_cross", (left_previous, right_previous, left_current, right_current),
            required + 1, _cross, "up: previous left<=right and current left>right; down: previous left>=right and current left<right")
    return tuple(relations)


def _facts(request, market, results, relations):
    facts = []
    parameters_by_id = {indicator.indicator_id: indicator.parameters for indicator in results}
    for fields in market[-2:]:
        for name, value in fields.items():
            facts.append(Fact(value.fact_id, request.request_id, request.instrument_id,
                              value.trade_date, "daily_bar", {}, name, value.value, value.unit))
    for indicator in results:
        for row in indicator.series[-2:]:
            for name, value in row.fields.items():
                if value.fact_id is not None:
                    facts.append(Fact(value.fact_id, request.request_id, request.instrument_id,
                                      value.trade_date, indicator.indicator_id, indicator.parameters,
                                      name, value.value, value.unit))
    for relation in relations:
        if relation.fact_id is not None:
            facts.append(Fact(relation.fact_id, request.request_id, request.instrument_id,
                              relation.trade_date, relation.indicator_id,
                              parameters_by_id.get(relation.indicator_id, {}), relation.field,
                              relation.value, "relation", relation.dependencies))
    identifiers = {fact.fact_id for fact in facts}
    if len(identifiers) != len(facts) or any(
            dependency not in identifiers for fact in facts for dependency in fact.dependencies):
        raise RuntimeError("Analysis evidence is duplicated or incomplete")
    return tuple(facts)


def analyze_indicators(
    source: DailyBarSource, request: MultiIndicatorRequest, *, allow_development_csv: bool = False,
) -> MultiIndicatorResult:
    """Analyze one validated snapshot; CSV injection is trusted development configuration."""
    _validate_source(source, request, allow_development_csv)
    synthetic = bool(request.source == "csv" and getattr(source, "is_synthetic", False))
    authenticity = "synthetic" if synthetic else "unverified_csv" if request.source == "csv" else "provider_reported"
    warnings = [
        "仅按日线日期截取；未验证历史发布时间、修订版本或当时可得性。",
        "未校验证券身份、ST、上市时长、停复牌或可交易性；不推断缺失原因。",
        "多个指标使用相同价格与量能，不能视为独立证据或投票胜率。",
        "公司行动可能影响不复权价格及多个指标；未验证具体公司行动。",
    ]
    if request.source == "csv":
        warnings.append("开发样例/非真实行情分析；CSV真实性与复权未知，未经独立验证。")
        if synthetic:
            warnings.append("受信开发配置标记为人工样例/非真实行情，不用于市场建议。")
    else:
        warnings.append("供应商量额单位按适配器转换；覆盖与精度不代表已全面验证。")
        if request.source == "tencent":
            warnings.append("腾讯上游量额可能已舍入，单位换算不能恢复源端丢失的精度。")
    retrieved_at = None

    def failure(status, reason, message):
        return MultiIndicatorResult(request, status, "unavailable", reason, message,
                                    request.source, source.adjustment, authenticity,
                                    retrieved_at, warnings=tuple(warnings))

    try:
        bars = source.read_daily_bars()
    except DataSourceError:
        return failure("source_error", "source_unavailable", "行情源读取失败；未使用替代来源。")
    except DataValidationError:
        return failure("invalid_data", "invalid_source_data", "行情源返回的数据无效。")
    retrieved_at = datetime.now(timezone.utc)
    try:
        _validate_records(bars, request)
    except DataValidationError:
        return failure("invalid_data", "invalid_source_data", "行情记录、顺序或字段未通过校验。")
    if not bars:
        return failure("no_data", "empty_source", "行情源没有返回记录；无法推断具体原因。")
    selected = [bar for bar in bars if request.start_date <= bar.trade_date <= request.as_of_date]
    if not selected:
        return failure("no_data", "no_matching_bars", "指定区间内没有行情；无法推断具体原因。")
    bars = selected
    if bars[-1].trade_date < request.as_of_date:
        warnings.append("实际最新日早于请求截止日；不据此推断休市或停牌。")
    results = tuple(_calculate(spec, bars, request) for spec in request.indicators)
    market = [{
        "close_price": _field(request, bars, index, "daily_bar", "close_price", bar.close_price, 1),
        "volume_shares": _field(request, bars, index, "daily_bar", "volume_shares", bar.volume_shares,
                                1, "shares"),
    } for index, bar in enumerate(bars)]
    relations = _relations(request, bars, results, market)
    facts = _facts(request, market, results, relations)
    statuses = [value.status for indicator in results for value in indicator.latest.values()]
    calculation_status = ("complete" if all(status == "available" for status in statuses)
                          else "limited" if "available" in statuses else "unavailable")
    if any(component.start_effect_warning for indicator in results for component in indicator.components):
        warnings.append("部分递推分量初始化后更新不足相关最长周期的3倍，可能受计算起点影响；这是项目提示阈值，不是收敛或可靠性保证。")
    if "calculation_error" in statuses:
        warnings.append("部分指标发生数值计算错误；该实例受限，其余有效结果保留。")
    if not any(value.status == "available" for indicator in results
               for name, value in indicator.latest.items() if name != "volume_shares"):
        warnings.append("全部请求数值字段不可用，禁止依据本次结果生成综合走势判断。")
    return MultiIndicatorResult(
        request, "ok", calculation_status, "analysis_complete", "多指标事实计算完成。",
        request.source, source.adjustment, authenticity, retrieved_at, tuple(bars),
        results, relations, facts, tuple(warnings),
    )
