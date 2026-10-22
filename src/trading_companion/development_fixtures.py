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
