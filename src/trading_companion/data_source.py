import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, Protocol

from trading_companion.csv_loader import load_daily_bars
from trading_companion.daily_bar import DailyBar
from trading_companion.errors import DataSourceError, DataValidationError


class DailyBarSource(Protocol):
    """Provide validated daily bars to the analysis pipeline."""

    @property
    def source_name(self) -> str: ...

    @property
    def adjustment(self) -> str: ...

    def read_daily_bars(self) -> list[DailyBar]: ...


@dataclass(frozen=True)
class CsvDailyBarSource:
    path: Path
    is_synthetic: bool = field(default=False, kw_only=True)
    source_name: ClassVar[str] = "csv"
    adjustment: ClassVar[str] = "unknown"

    def read_daily_bars(self) -> list[DailyBar]:
        try:
            return load_daily_bars(self.path)
        except OSError as error:
            raise DataSourceError(f"Cannot read CSV file: {error}") from error
        except (ValueError, csv.Error) as error:
            raise DataValidationError(f"Invalid CSV data: {error}") from error
