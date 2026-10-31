"""Shared decimal display precision; underlying calculation snapshots stay unchanged."""
from decimal import Decimal, localcontext


def display_number(value):
    """Two fractional digits, using the stock price-context half-even convention."""
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError('Finite evidence number required')
    with localcontext() as context:
        context.prec = max(34, len(number.as_tuple().digits) + 4)
        return format(number, '.2f')


def same_display_number(left, right):
    return display_number(left) == display_number(right)
