"""Price facts derived from the already checked daily snapshot; never fetch quotes."""
from decimal import Decimal
from trading_companion.evidence_numbers import display_number


def price_context_evidence(result, *, allow_extra_means=True):
    from trading_companion.product_research import ProductEvidence
    from trading_companion.research_report import build_research_report

    report = build_research_report(result, expected_request=result.request)
    if report.status not in ("complete", "limited") or not result.bars:
        return ()
    bars = result.bars
    from trading_companion.technical_insights import _validate_bars
    try:
        _validate_bars(bars, result.request.instrument_id)
    except (ValueError, TypeError):
        return ()
    request = result.request
    latest = bars[-1]
    output = []

    def add(key, label, value, selected, *, unit="CNY/share", day=None, field="close_price"):
        value = Decimal(str(value))
        display = display_number(value) + ("%" if unit == "percent" else "元/股")
        output.append(ProductEvidence(f"{request.request_id}|price_context:{key}", request.instrument_id,
            "technical", label, display, request.source, (day or latest.trade_date).isoformat(),
            publication_precision="date", data_authenticity=result.data_authenticity,
            temporal_scope="daily_price_context", raw_value=str(value), unit=unit,
            dependencies=tuple(f"{request.request_id}|raw_daily_bar|{bar.trade_date.isoformat()}|{field}" for bar in selected),
            calculation_basis=(("version", "daily_price_context_v1"), ("price_adjustment", result.price_adjustment),
                ("sample_count", str(len(selected))), ("cutoff", latest.trade_date.isoformat()),
                ("dependency_scope", "technical.bars:trade_date:field"), ("extreme_tie", "latest_date")),
            retrieved_at=result.retrieved_at.isoformat() if result.retrieved_at else None))

    add("latest_close", "最新收盘价", latest.close_price, (latest,))
    for window in (5, 20, 60):
        if len(bars) > window:
            first = bars[-window-1]
            change = (Decimal(str(latest.close_price)) / Decimal(str(first.close_price)) - 1) * 100
            add(f"return_{window}d", f"近{window}日涨跌幅", change, (first, latest), unit="percent")
            add(f"start_close_{window}d", f"{window}日前收盘价", first.close_price, (first,), day=first.trade_date)
    for window in (20, 60):
        if len(bars) < window:
            continue
        selected = bars[-window:]
        high = max(selected, key=lambda b: (b.high_price, b.trade_date))
        low = min(selected, key=lambda b: (b.low_price, -b.trade_date.toordinal()))
        add(f"high_{window}d", f"近{window}日最高价", high.high_price, selected, day=high.trade_date, field="high_price")
        add(f"low_{window}d", f"近{window}日最低价", low.low_price, selected, day=low.trade_date, field="low_price")
        if allow_extra_means or any(spec.kind == "sma" and spec.parameters['window'] == window for spec in request.indicators):
            average = sum(Decimal(str(b.close_price)) for b in selected) / window
            add(f"ma{window}", f"MA{window}", average, selected)
    return tuple(output)
