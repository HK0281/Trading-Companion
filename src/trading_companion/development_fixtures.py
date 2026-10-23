"""Pinned artificial inputs shared by opt-in development entry points."""

from datetime import date
from hashlib import sha256
from pathlib import Path

from trading_companion.data_source import CsvDailyBarSource
from trading_companion.errors import DataSourceError, DataValidationError
from trading_companion.security_directory import load_security_directory


ROOT = Path(__file__).resolve().parents[2]
REFERENCE_DATE = date(2026, 9, 26)
FIXTURES = (
    ("sample_daily_bars_synthetic.csv", "a8dd149f7f05b6266f0b0a700047ba408a97f5adc906b6ce4555ffdd0b93a4f3"),
    ("security_directory_fixture.json", "7bf34172c7b0243029180c7a82b92c95dc25151bec69a8e7867107d9ee7d41d5"),
)
MARKET_CONTEXT_FIXTURE = (
    "market_context_fixture.json", "754474f11055f82700eb641efa07f0308df52f31ac54cd5f860152d775821ed0",
)


def load_development_fixtures():
    """Verify known content before allowing it to be described as synthetic."""
    try:
        for name, digest in FIXTURES:
            content = (ROOT / "data" / name).read_bytes().replace(b"\r\n", b"\n")
            if sha256(content).hexdigest() != digest:
                raise DataValidationError("Development fixture version differs")
    except OSError:
        raise DataSourceError("Development fixtures are unavailable") from None
    directory = load_security_directory(ROOT / "data" / FIXTURES[1][0])
    directory.assert_usable(REFERENCE_DATE, development=True)
    return CsvDailyBarSource(ROOT / "data" / FIXTURES[0][0], is_synthetic=True), directory


def load_development_market_context():
    """Load independently pinned artificial states and calendar, never actual market facts."""
    from trading_companion.market_context import MAX_SNAPSHOT_BYTES, parse_market_context_bytes
    name, digest = MARKET_CONTEXT_FIXTURE
    selected = ROOT / "data" / name
    try:
        with selected.open("rb") as handle:
            raw = handle.read(MAX_SNAPSHOT_BYTES + 1)
        if len(raw) > MAX_SNAPSHOT_BYTES:
            raise DataValidationError("Development market context exceeds the size limit")
        content = raw.replace(b"\r\n", b"\n")
        if sha256(content).hexdigest() != digest:
            raise DataValidationError("Development market context version differs")
    except OSError:
        raise DataSourceError("Development market context is unavailable") from None
    return parse_market_context_bytes(content)
