"""Locate fingerprinted source files that now live in either evaluation_tools or the product package."""

from pathlib import Path

import trading_companion


EVALUATION = Path(__file__).resolve().parent
PRODUCT = Path(trading_companion.__file__).resolve().parent


def source_file(name):
    """Return a bare module file name from evaluation_tools first, otherwise from trading_companion."""
    local = EVALUATION / name
    return local if local.is_file() else PRODUCT / name
