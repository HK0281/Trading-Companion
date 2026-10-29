"""Derive quarters from bounded eligible cumulative statements, with explicit dependencies."""
from dataclasses import replace
from datetime import date
from decimal import Decimal, InvalidOperation


def quarterly_evidence(records):
    amounts = [r for r in records if r.kind == "financial" and r.unit == "cny"
               and r.label in ("营业收入", "归母净利润") and r.report_period]
    if not amounts:
        return ()
    latest = max(r.report_period for r in amounts)
    index = {}
    for r in amounts:
        key = (r.instrument_id, r.label, r.report_period, r.source)
        index[key] = r if key not in index else None  # Ambiguous revisions are not interchangeable.
    output = []

    def quarter(record, period):
        current = index.get((record.instrument_id, record.label, period.isoformat(), record.source))
        if current is None:
            return None
        try:
            value = Decimal(str(current.raw_value))
            dependencies = [current]
            if period.month != 3:
                previous_month = period.month - 3
                previous = date(period.year, previous_month, 30 if previous_month in (6, 9) else 31)
                prior = index.get((record.instrument_id, record.label, previous.isoformat(), record.source))
                if prior is None:
                    return None
                value -= Decimal(str(prior.raw_value))
                dependencies.append(prior)
            if not value.is_finite():
                return None
            return value, dependencies
        except (InvalidOperation, ValueError, TypeError):
            return None

    for record in amounts:
        if record.report_period != latest:
            continue
        period = date.fromisoformat(latest)
        if (period.month, period.day) not in ((3, 31), (6, 30), (9, 30), (12, 31)):
            continue
        computed = quarter(record, period)
        if computed is None:
            continue
        value, dependencies = computed
        common = dict(temporal_scope="financial_quarter_derived", dependencies=tuple(r.id for r in dependencies),
                      date=max((r.date for r in dependencies if r.date), default=None))
        output.append(replace(record, id=record.id + ":quarter", label="单季" + record.label,
            raw_value=str(value), display=f"{value / Decimal(100000000):.2f}亿元", **common))
        prior = quarter(record, period.replace(year=period.year-1))
        if prior is not None and prior[0] != 0:
            yoy = (value-prior[0]) / abs(prior[0]) * 100
            output.append(replace(record, id=record.id + ":quarter_yoy", label="单季" + record.label + "同比",
                raw_value=str(yoy), unit="percent", display=f"{yoy:.2f}%",
                temporal_scope="financial_quarter_derived",
                dependencies=tuple(r.id for r in (*dependencies, *prior[1])),
                date=max((r.date for r in (*dependencies, *prior[1]) if r.date), default=None)))
    return tuple(output)
