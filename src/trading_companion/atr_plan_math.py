"""Unit-neutral exact ATR geometry; callers own identity, evidence and permissions."""
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, localcontext

GRID = Decimal('.01')


def atr_geometry(entry, atr, multiple, targets):
    with localcontext() as context:
        context.prec = 80
        entry, value, distance = (Decimal(str(v)) for v in (entry, atr, multiple))
        targets = tuple(Decimal(str(v)) for v in targets)
        if (not all(v.is_finite() and v > 0 for v in (entry, value, distance, *targets))
                or len(targets) != 2 or targets[0] >= targets[1]):
            raise ValueError('Invalid ATR geometry')
        stop = (entry-distance*value).quantize(GRID, rounding=ROUND_CEILING)
        risk = entry-stop
        first, second = ((entry+t*risk).quantize(GRID, rounding=ROUND_FLOOR) for t in targets)
        if not 0 < stop < entry < first < second:
            raise ValueError('price_grid')
        def ratio(target):
            return format(((target-entry)/risk).quantize(Decimal('.000001'), rounding=ROUND_FLOOR), 'f')
        return dict(reference_entry=format(entry, '.2f') if entry == entry.quantize(GRID) else format(entry, 'f'),
                    reference_stop=format(stop, '.2f'), target_1=format(first, '.2f'), target_2=format(second, '.2f'),
                    reward_risk_1=ratio(first), reward_risk_2=ratio(second),
                    atr=format(value.quantize(Decimal('.0001'), rounding=ROUND_HALF_UP), '.4f'),
                    atr_multiple=str(multiple))


def purpose_values(core, purpose):
    """One mapping for stock and index plans; do not expose unused target fields."""
    if purpose == 'conditional_entry':
        return dict(core)
    if purpose == 'holding_exit':
        return dict(reference_close=core['reference_entry'], exit_below=core['reference_stop'])
    if purpose == 'wait':
        return dict(observe_below=core['reference_stop'], observe_above=core['target_1'])
    raise ValueError('Unknown plan purpose')
