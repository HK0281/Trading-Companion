"""Task-independent ratios and nearest-rank latency summaries."""

from math import ceil


def ratio(a, b):
    return {"numerator": a, "denominator": b, "value": a / b if b else None}


def percentiles(values):
    values = sorted(values)
    return {"n": len(values), **{f"p{p}_ms": values[ceil(p / 100 * len(values)) - 1] if values else None for p in (50, 95)}}
