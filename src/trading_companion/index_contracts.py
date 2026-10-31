"""Index-only identities and point-valued daily snapshots; unknown quantity is never zero."""
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from math import isfinite
import re
from uuid import uuid4

from trading_companion.market_scope import INDICES, IndexIdentity
from trading_companion.multi_contracts import DEFAULT_INDICATORS, validate_research_conditions
from trading_companion.errors import InvalidRequestError, DataValidationError

DEFAULTS = tuple(spec for spec in DEFAULT_INDICATORS if spec.kind != 'volume')
VOLUME_KINDS = frozenset(('volume', 'obv', 'vwma'))
LIMITATIONS = (
    '指数不可直接交易；本轮仅提供指数技术研究，不对应任何ETF或账户操作。',
    '成交量和成交额单位尚未核实，量能指标不可用；未以零值替代。',
    '未研究公司财报、资金流、全市场宽度、板块轮动或宏观归因。',
    '支撑压力为历史点位候选，不保证未来触达或突破有效。',
)


def identity(instrument_id):
    for index in INDICES:
        if type(instrument_id) is str and index.instrument_id == instrument_id:
            return index
    raise InvalidRequestError('A whitelisted index identity is required')


@dataclass(frozen=True)
class IndexBar:
    instrument_id: str
    trade_date: date
    open_price: float
    high_price: float
    low_price: float
    close_price: float
    volume: None = None
    amount: None = None

    def __post_init__(self):
        identity(self.instrument_id)
        values = (self.open_price, self.high_price, self.low_price, self.close_price)
        if (type(self.trade_date) is not date or self.trade_date.weekday() > 4
                or any(type(v) not in (float, int) or not isfinite(v) or not 0 < v <= 1e15 for v in values)
                or self.low_price > min(values) or self.high_price < max(values)
                or self.volume is not None or self.amount is not None):
            raise DataValidationError('Invalid index OHLC or unverified quantity')

    @property
    def values(self):
        # The price-only numeric kernel never reads volume for these objects.
        return self


@dataclass(frozen=True)
class IndexResearchRequest:
    instrument_id: str
    start_date: date
    as_of_date: date
    indicators: tuple = DEFAULTS
    source: str = 'tencent'
    request_id: str = field(default_factory=lambda: uuid4().hex)
    benchmark_defaulted: bool = False
    horizon: str | None = None

    def __post_init__(self):
        identity(self.instrument_id)
        object.__setattr__(self, 'indicators', validate_research_conditions(
            self.start_date, self.as_of_date, self.indicators))
        if (self.source not in ('tencent', 'csv') or type(self.benchmark_defaulted) is not bool
                or self.benchmark_defaulted and self.instrument_id != '000001.SH'
                or type(self.request_id) is not str or re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', self.request_id) is None
                or self.horizon is not None and (type(self.horizon) is not str or len(self.horizon) > 120)):
            raise InvalidRequestError('Invalid index research conditions')

    @property
    def index(self) -> IndexIdentity:
        return identity(self.instrument_id)

    def parameters(self):
        return dict(instrument_id=self.instrument_id, start_date=self.start_date.isoformat(),
                    as_of_date=self.as_of_date.isoformat(), source=self.source,
                    indicators=[dict(kind=s.kind, **s.parameters) for s in self.indicators])


def validate_bars(bars, request):
    if type(bars) not in (tuple, list) or not 1 <= len(bars) <= 366:
        raise DataValidationError('Index daily history is empty or oversized')
    previous = None
    for bar in bars:
        if type(bar) is not IndexBar:
            raise DataValidationError('Index bars must retain their index-only type')
        bar.__post_init__()
        if (bar.instrument_id != request.instrument_id
                or not request.start_date <= bar.trade_date <= request.as_of_date
                or previous is not None and previous >= bar.trade_date):
            raise DataValidationError('Index identity or daily window mismatch')
        previous = bar.trade_date


@dataclass(frozen=True)
class IndexResearchResult:
    request: IndexResearchRequest
    bars: tuple[IndexBar, ...]
    retrieved_at: datetime

    def __post_init__(self):
        if type(self.request) is not IndexResearchRequest:
            raise DataValidationError('A validated index request is required')
        self.request.__post_init__()
        validate_bars(self.bars, self.request)
        object.__setattr__(self, 'bars', tuple(self.bars))
        if type(self.retrieved_at) is not datetime or self.retrieved_at.utcoffset() is None:
            raise DataValidationError('Index retrieval time must be aware')

    @property
    def authenticity(self):
        return 'synthetic' if self.request.source == 'csv' else 'provider_reported'

    def to_dict(self):
        return dict(version='index-snapshot-v1', parameters=self.request.parameters(),
                    request_id=self.request.request_id, benchmark_defaulted=self.request.benchmark_defaulted,
                    horizon=self.request.horizon, retrieved_at=self.retrieved_at.isoformat(),
                    data_authenticity=self.authenticity,
                    bars=[{**asdict(b), 'trade_date': b.trade_date.isoformat()} for b in self.bars])
