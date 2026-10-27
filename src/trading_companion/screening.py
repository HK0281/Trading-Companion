"""Deterministic filtering of an explicitly bounded, identity-verified quote sample."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from math import isfinite
from typing import ClassVar

from trading_companion.errors import DataValidationError, InvalidRequestError
from trading_companion.security_directory import Security, SecurityDirectory
from trading_companion.turn_control import checkpoint


SHANGHAI = timezone(timedelta(hours=8))
FIELDS = {
    "price": ("最新价", "CNY/share"),
    "change_percent": ("涨跌幅", "percent"),
    "amount_cny": ("成交额", "CNY"),
    "turnover_percent": ("换手率", "percent"),
    "pe_dynamic": ("动态市盈率", "ratio"),
    "pb": ("市净率", "ratio"),
    "market_cap_cny": ("总市值", "CNY"),
    "float_market_cap_cny": ("流通市值", "CNY"),
}
STRATA = {
    "sse_main": "沪市主板", "szse_main": "深市主板", "star": "科创板",
    "chinext": "创业板", "bse": "北交所",
}
OPERATORS = {"lt": "<", "lte": "≤", "eq": "=", "gte": "≥", "gt": ">"}
MAX_SAMPLE_RECORDS = 1000
SAMPLING_METHODS = {
    "fixed_market_strata_code_ascending_bounded_pages": "vendor_page_order",
    "configured_directory_fixed_market_strata_code_ascending": "directory_code_prefix",
    "configured_directory_code_uniform_interval": "directory_code_uniform_interval",
}


def _aware(value):
    return type(value) is datetime and value.tzinfo is not None and value.utcoffset() is not None


def _text(value, maximum=160):
    return type(value) is str and bool(value.strip()) and len(value) <= maximum and value.isprintable()


def _number(value):
    return type(value) is float and isfinite(value) or type(value) is int and value.bit_length() <= 256


def market_stratum(instrument_id: str) -> str:
    """Use a validated security identity, not its risk label or listing age."""
    if instrument_id.endswith(".BJ"):
        return "bse"
    if instrument_id.endswith(".SH"):
        return "star" if instrument_id.startswith(("688", "689")) else "sse_main"
    return "chinext" if instrument_id.startswith("3") else "szse_main"


@dataclass(frozen=True)
class ScreeningQuote:
    instrument_id: str
    name: str
    quoted_at: datetime
    price: float | None = None
    change_percent: float | None = None
    amount_cny: float | None = None
    turnover_percent: float | None = None
    pe_dynamic: float | None = None
    pb: float | None = None
    market_cap_cny: float | None = None
    float_market_cap_cny: float | None = None

    def __post_init__(self):
        security = Security(self.instrument_id, self.name)
        object.__setattr__(self, "name", security.name)
        if not _aware(self.quoted_at):
            raise DataValidationError("Quote timestamp must be timezone-aware")
        for field in FIELDS:
            value = getattr(self, field)
            if value is not None and not _number(value):
                raise DataValidationError("Quote values must be finite numbers or null")
            if (value is not None and field in (
                    "price", "amount_cny", "turnover_percent", "market_cap_cny", "float_market_cap_cny")
                    and (value < 0 or field == "price" and value == 0)):
                raise DataValidationError("Quote price must be positive; amount, turnover and caps nonnegative")

    def to_dict(self):
        return {"instrument_id": self.instrument_id, "name": self.name,
                "quoted_at": self.quoted_at.isoformat(),
                "quote_date": self.quoted_at.astimezone(SHANGHAI).date().isoformat(),
                "stratum": market_stratum(self.instrument_id),
                "values": {field: getattr(self, field) for field in FIELDS},
                "risk_status_verified": False, "listing_date": None}


@dataclass(frozen=True)
class ScreeningStratumCoverage:
    stratum: str
    vendor_total: int
    scanned_records: int
    verified_records: int
    pages_read: int
    missing_quote_records: int = 0
    total_basis: str = "vendor_declared"
    selection_limit: int | None = None

    def __post_init__(self):
        if (type(self.stratum) is not str or self.stratum not in STRATA or any(type(value) is not int or value < 0 for value in (
                self.vendor_total, self.scanned_records, self.verified_records, self.pages_read, self.missing_quote_records))
                or not self.verified_records + self.missing_quote_records <= self.scanned_records <= self.vendor_total <= 10000
                or type(self.total_basis) is not str or self.total_basis not in ("vendor_declared", "configured_directory")
                or self.scanned_records > MAX_SAMPLE_RECORDS
                or self.pages_read > (2 if self.total_basis == "vendor_declared" else 4)
                or self.selection_limit is not None and (
                    type(self.selection_limit) is not int or not 1 <= self.selection_limit <= 200)):
            raise DataValidationError("Invalid quote sampling coverage")

    def to_dict(self):
        return {"stratum": self.stratum, "label": STRATA[self.stratum], "total_basis": self.total_basis,
                "vendor_total": self.vendor_total if self.total_basis == "vendor_declared" else None,
                "directory_records": self.vendor_total if self.total_basis == "configured_directory" else None,
                "scanned_records": self.scanned_records, "identity_verified_records": self.verified_records,
                "unverified_records": self.scanned_records - self.verified_records - self.missing_quote_records,
                "missing_quote_records": self.missing_quote_records,
                "pages_read": self.pages_read,
                "vendor_listing_exhausted": self.scanned_records == self.vendor_total if self.total_basis == "vendor_declared" else None,
                "directory_stratum_exhausted": self.scanned_records == self.vendor_total if self.total_basis == "configured_directory" else None}


@dataclass(frozen=True)
class ScreeningSnapshot:
    quotes: tuple[ScreeningQuote, ...]
    source: str
    retrieved_at: datetime
    directory: SecurityDirectory
    strata: tuple[ScreeningStratumCoverage, ...]
    is_fixture: bool = False
    sampling_policy: str = "fixed_market_strata_code_ascending_bounded_pages"

    def __post_init__(self):
        if (type(self.quotes) is not tuple or len(self.quotes) > MAX_SAMPLE_RECORDS
                or any(type(quote) is not ScreeningQuote for quote in self.quotes)
                or not _text(self.source) or not _aware(self.retrieved_at)
                or type(self.directory) is not SecurityDirectory or type(self.is_fixture) is not bool
                or self.directory.is_fixture != self.is_fixture
                or type(self.sampling_policy) is not str or self.sampling_policy not in SAMPLING_METHODS):
            raise DataValidationError("Invalid quote snapshot or mixed fixture identity")
        if (type(self.strata) is not tuple or not 1 <= len(self.strata) <= len(STRATA)
                or any(type(item) is not ScreeningStratumCoverage for item in self.strata)
                or len({item.stratum for item in self.strata}) != len(self.strata)
                or sum(item.scanned_records for item in self.strata) > MAX_SAMPLE_RECORDS
                or sum(item.pages_read for item in self.strata) > 20
                or sum(item.verified_records for item in self.strata) != len(self.quotes)):
            raise DataValidationError("Quote snapshot coverage does not match its records")
        trusted = {item.instrument_id: item for item in self.directory.securities}
        seen = set()
        for quote in self.quotes:
            if quote.instrument_id in seen:
                raise DataValidationError("Duplicate quote identity")
            seen.add(quote.instrument_id)
            if (quote.instrument_id not in trusted or trusted[quote.instrument_id].name != quote.name
                    or quote.quoted_at > self.retrieved_at):
                raise DataValidationError("Quote identity or timestamp is not bound to its snapshot")
        for item in self.strata:
            directory_count = sum(market_stratum(security.instrument_id) == item.stratum for security in self.directory.securities)
            if item.total_basis == "configured_directory" and item.vendor_total != directory_count:
                raise DataValidationError("Quote coverage does not match the independent directory count")
            interval = self.sampling_policy == "configured_directory_code_uniform_interval"
            if interval:
                if (item.total_basis != "configured_directory" or item.selection_limit is None
                        or item.scanned_records != min(directory_count, item.selection_limit)):
                    raise DataValidationError("Invalid uniform-interval sample coverage")
            elif item.selection_limit is not None:
                raise DataValidationError("Unexpected interval sampling limit")
            if sum(market_stratum(quote.instrument_id) == item.stratum for quote in self.quotes) != item.verified_records:
                raise DataValidationError("Quote identities do not match stratum coverage")
        if any(market_stratum(quote.instrument_id) not in {item.stratum for item in self.strata} for quote in self.quotes):
            raise DataValidationError("A quote has no sampling coverage")

    def metadata(self):
        return {"source": self.source, "retrieved_at": self.retrieved_at.isoformat(),
                "data_authenticity": "synthetic" if self.is_fixture else "vendor_snapshot",
                "quote_timestamp_scope": "vendor_quote_update_time_not_independent_exchange_confirmation",
                "directory": self.directory.metadata(),
                "sampling_policy": self.sampling_policy,
                "strata": [item.to_dict() for item in self.strata],
                "independent_full_market_completeness_proven": False}

    def directory_audit(self):
        """Reconcile directory IDs with verified quotes, independently of vendor totals."""
        coverage = {item.stratum: item for item in self.strata}
        rows = []
        for stratum in STRATA:
            total = sum(market_stratum(item.instrument_id) == stratum for item in self.directory.securities)
            observed = sum(market_stratum(quote.instrument_id) == stratum for quote in self.quotes)
            item = coverage.get(stratum)
            selected = item.scanned_records if item and item.total_basis == "configured_directory" else None
            rows.append({"stratum": stratum, "directory_records": total,
                "directory_observed_records": observed, "directory_unobserved_records": total - observed,
                "directory_selected_records": selected,
                "directory_not_selected_records": total - selected if selected is not None else None,
                "vendor_not_scanned_records": item.vendor_total - item.scanned_records
                    if item and item.total_basis == "vendor_declared" else None,
                "selection_limit": item.selection_limit if item else None})
        return {"version": "screening-directory-audit-v1",
            "selection_method": "synthetic_fixture" if self.is_fixture else SAMPLING_METHODS[self.sampling_policy],
            "directory_records": len(self.directory.securities), "directory_observed_records": len(self.quotes),
            "directory_unobserved_records": len(self.directory.securities) - len(self.quotes),
            "request_count": sum(item.pages_read for item in self.strata), "strata": rows}


@dataclass(frozen=True)
class ScreeningCondition:
    field: str
    operator: str
    value: float

    def __post_init__(self):
        if type(self.field) is not str or self.field not in FIELDS:
            raise InvalidRequestError("Unsupported screening field")
        if type(self.operator) is not str or self.operator not in OPERATORS or not _number(self.value):
            raise InvalidRequestError("Use a supported numeric screening operator and finite value")

    def to_dict(self):
        return {"field": self.field, "label": FIELDS[self.field][0], "operator": self.operator,
                "value": self.value, "unit": FIELDS[self.field][1]}


@dataclass(frozen=True)
class ScreeningRequest:
    conditions: tuple[ScreeningCondition, ...]
    sort_by: str = "amount_cny"
    sort_direction: str = "desc"
    limit: int = 10
    max_quote_age_days: int = 7
    version: ClassVar[str] = "quote-screening-request-v1"

    def __post_init__(self):
        if (type(self.conditions) is not tuple or not 1 <= len(self.conditions) <= 12
                or any(type(item) is not ScreeningCondition for item in self.conditions)
                or len(set(self.conditions)) != len(self.conditions)):
            raise InvalidRequestError("Use one to twelve distinct screening conditions")
        if (type(self.sort_by) is not str or self.sort_by not in FIELDS
                or type(self.sort_direction) is not str or self.sort_direction not in ("asc", "desc")
                or type(self.limit) is not int or not 1 <= self.limit <= 50
                or type(self.max_quote_age_days) is not int or not 0 <= self.max_quote_age_days <= 14):
            raise InvalidRequestError("Use a supported explicit sort, at most 50 rows and at most 14 quote-age days")

    @classmethod
    def from_dict(cls, payload):
        if (type(payload) is not dict or payload.keys() - {
                "conditions", "sort_by", "sort_direction", "limit", "max_quote_age_days"}
                or "conditions" not in payload or type(payload["conditions"]) is not list
                or not 1 <= len(payload["conditions"]) <= 12):
            raise InvalidRequestError("Invalid screening request fields")
        conditions = []
        for item in payload["conditions"]:
            if type(item) is not dict or item.keys() != {"field", "operator", "value"}:
                raise InvalidRequestError("Invalid screening condition fields")
            conditions.append(ScreeningCondition(**item))
        return cls(tuple(conditions), **{key: value for key, value in payload.items() if key != "conditions"})

    def to_dict(self):
        return {"conditions": [{"field": item.field, "operator": item.operator, "value": item.value}
                               for item in self.conditions],
                "sort_by": self.sort_by, "sort_direction": self.sort_direction,
                "limit": self.limit, "max_quote_age_days": self.max_quote_age_days}


def _condition_state(value, condition):
    if value is None:
        return "unknown"
    actual, target = Decimal(str(value)), Decimal(str(condition.value))
    passed = {"lt": actual < target, "lte": actual <= target, "eq": actual == target,
              "gte": actual >= target, "gt": actual > target}[condition.operator]
    return "passed" if passed else "failed"


@dataclass(frozen=True)
class ScreeningResult:
    status: str
    code: str
    request: ScreeningRequest
    rows: tuple[dict, ...]
    coverage: dict
    source: dict
    condition_summary: tuple[dict, ...]

    def to_dict(self):
        return {"version": "quote-screening-result-v1", "status": self.status, "code": self.code,
                "request": self.request.to_dict(), "rows": list(self.rows), "coverage": self.coverage,
                "source": self.source, "conditions": list(self.condition_summary),
                "fields": {field: {"label": label, "unit": unit} for field, (label, unit) in FIELDS.items()},
                "scope": "numeric_conditions_within_actual_identity_verified_sample_not_full_market",
                "ranking_policy": {"method": "requested_single_metric_raw_value",
                    "ties": "instrument_id_ascending", "missing_values": "unknown_never_zero_filled",
                    "missing_sort_values": "not_ranked_counted_separately"},
                "inclusion_policy": {"star_market": "not_excluded", "st": "not_excluded",
                    "new_listings": "not_excluded", "suspensions": "not_excluded",
                    "risk_and_listing_status": "not_verified_by_quote_snapshot"},
                "limitations": ["仅在实际覆盖样本中按明确数值条件筛选，不代表全市场排名或潜力预测。",
                    "报价时间由供应商提供；跨页不是原子快照，未验证历史版本或实时可交易性。",
                    "动态市盈率和市净率沿用供应商口径；没有独立核对财报或跨行业可比性。"],
                "overall_score": None, "forecast": None, "trading_enabled": False}


class ScreeningService:
    """Pure local evaluation. Collection, models and trade execution are outside this service."""

    def __init__(self, *, clock=None):
        if clock is not None and not callable(clock):
            raise InvalidRequestError("Screening clock must be callable")
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def screen(self, snapshot: ScreeningSnapshot, request: ScreeningRequest) -> ScreeningResult:
        if type(snapshot) is not ScreeningSnapshot or type(request) is not ScreeningRequest:
            raise InvalidRequestError("Screen validated quote snapshots with a validated request")
        now = self._clock()
        if not _aware(now):
            raise InvalidRequestError("Screening clock must be timezone-aware")
        if not timedelta(0) <= now - snapshot.retrieved_at <= timedelta(hours=24):
            raise DataValidationError("Quote snapshot is in the future or more than 24 hours old")
        snapshot.directory.assert_usable(now.astimezone(SHANGHAI).date(), development=snapshot.is_fixture)
        summary = [{**item.to_dict(), "passed": 0, "failed": 0, "unknown": 0} for item in request.conditions]
        matched, eligible, stale, missing_sort = [], 0, 0, 0
        field_coverage = {field: {"available": 0, "missing": 0} for field in FIELDS}
        for quote in snapshot.quotes:
            checkpoint()
            for field in FIELDS:
                field_coverage[field]["missing" if getattr(quote, field) is None else "available"] += 1
            quote_age = (now.astimezone(SHANGHAI).date() - quote.quoted_at.astimezone(SHANGHAI).date()).days
            if not 0 <= quote_age <= request.max_quote_age_days:
                stale += 1
                continue
            eligible += 1
            checks = []
            for condition, totals in zip(request.conditions, summary):
                value = getattr(quote, condition.field)
                state = _condition_state(value, condition)
                totals[state] += 1
                checks.append({**condition.to_dict(), "actual_value": value, "status": state,
                               "quoted_at": quote.quoted_at.isoformat()})
            if any(item["status"] != "passed" for item in checks):
                continue
            if getattr(quote, request.sort_by) is None:
                missing_sort += 1
                continue
            matched.append({**quote.to_dict(), "condition_checks": checks,
                            "sort_value": getattr(quote, request.sort_by)})
        matched.sort(key=lambda item: item["instrument_id"])
        matched.sort(key=lambda item: Decimal(str(item["sort_value"])), reverse=request.sort_direction == "desc")
        rows = tuple({**item, "sample_position": index} for index, item in enumerate(matched[:request.limit], 1))
        coverage = {"scope": "actual_bounded_sample", "independent_full_market_completeness_proven": False,
            "vendor_declared_records": (sum(item.vendor_total for item in snapshot.strata)
                if all(item.total_basis == "vendor_declared" for item in snapshot.strata) else None),
            "directory_universe_records": (sum(item.vendor_total for item in snapshot.strata)
                if all(item.total_basis == "configured_directory" for item in snapshot.strata) else None),
            "scanned_records": sum(item.scanned_records for item in snapshot.strata),
            "identity_verified_records": len(snapshot.quotes),
            "unverified_records": sum(item.scanned_records - item.verified_records - item.missing_quote_records for item in snapshot.strata),
            "missing_quote_records": sum(item.missing_quote_records for item in snapshot.strata),
            "age_eligible_records": eligible, "stale_records_not_evaluated": stale,
            "condition_evaluated_records": eligible, "condition_matched_records": len(matched) + missing_sort,
            "matched_missing_sort_records": missing_sort, "sortable_matched_records": len(matched),
            "returned_records": len(rows), "result_truncated": len(rows) < len(matched),
            "field_coverage_within_verified_sample": field_coverage,
            "not_sampled_records_are_not_condition_failures": True,
            "directory_audit": snapshot.directory_audit(),
            "strata": [item.to_dict() for item in snapshot.strata]}
        status = "completed" if rows else "empty" if eligible else "unavailable"
        code = "sample_conditions_evaluated" if eligible else "no_age_eligible_verified_quotes"
        return ScreeningResult(status, code, request, rows, coverage, snapshot.metadata(), tuple(summary))


def _display_number(field, value):
    unit = FIELDS[field][1]
    if unit == "CNY":
        if abs(value) >= 100000000:
            return f"{value / 100000000:.2f}亿元"
        if abs(value) >= 10000:
            return f"{value / 10000:.2f}万元"
        return f"{value:g}元"
    suffix = {"CNY/share": "元", "percent": "%", "ratio": "倍"}[unit]
    return f"{value:g}{suffix}"


def render_screening_result(result: ScreeningResult) -> str:
    """User-facing text is concise; detailed checks remain in the structured result."""
    if type(result) is not ScreeningResult:
        raise InvalidRequestError("Expected a screening result")
    coverage, request = result.coverage, result.request
    lines = ["以下是按你给定条件筛选的实际覆盖样本。"]
    if result.source["data_authenticity"] == "synthetic":
        lines.append("当前为人工样例，数值不是真实行情。")
    conditions = "，".join(f"{FIELDS[item.field][0]}{OPERATORS[item.operator]}{_display_number(item.field, item.value)}"
                           for item in request.conditions)
    lines.append(f"条件：{conditions}；按{FIELDS[request.sort_by][0]}{'从高到低' if request.sort_direction == 'desc' else '从低到高'}排列。")
    lines.append(f"本次扫描{coverage['scanned_records']}条，身份已核对{coverage['identity_verified_records']}条；"
                 f"符合条件{coverage['condition_matched_records']}条，展示{coverage['returned_records']}条。")
    audit = coverage["directory_audit"]
    method = {"directory_code_prefix": "按板块代码顺序取前列", "directory_code_uniform_interval": "按板块代码均匀间隔抽样",
              "vendor_page_order": "按供应商分页顺序", "synthetic_fixture": "固定人工样例"}[audit["selection_method"]]
    lines.append(f"采样方式：{method}；可信目录共{audit['directory_records']}只，其中{audit['directory_unobserved_records']}只尚未观察到可核对报价，包含未采样部分。")
    absent = [STRATA[item["stratum"]] for item in audit["strata"] if item["directory_records"] == 0]
    if absent:
        lines.append(f"目录未覆盖：{'、'.join(absent)}；不能据此判断该市场没有股票。")
    shown_fields = tuple(dict.fromkeys((*[item.field for item in request.conditions], request.sort_by)))
    for row in result.rows:
        values = "，".join(f"{FIELDS[field][0]} {_display_number(field, row['values'][field])}" for field in shown_fields)
        lines.append(f"{row['sample_position']}. {row['name']}（{row['instrument_id']}）：{values}；报价日期 {row['quote_date']}。")
    if not result.rows:
        lines.append("本次实际覆盖样本没有可展示的匹配项；这不表示全市场没有符合条件的股票。")
    if coverage["matched_missing_sort_records"]:
        lines.append(f"另有{coverage['matched_missing_sort_records']}项符合条件，但排序字段缺失，未参与排名。")
    if coverage["missing_quote_records"]:
        lines.append(f"本次所选样本中有{coverage['missing_quote_records']}只没有返回可用报价，未按条件失败计数。")
    label = {"tencent_directory_stratified_batch_quotes_v1": "腾讯批量报价",
             "eastmoney_stratified_quote_list_v1": "东方财富报价列表"}.get(result.source["source"], result.source["source"])
    lines.append(f"来源：{label}；采集时间：{result.source['retrieved_at']}。")
    lines.append("这是有界样本的数值条件结果，不是全市场排名或潜力预测。科创板、ST和新股没有被默认排除；未覆盖或未核对的证券不视为条件未通过。")
    return "\n\n".join(lines)


def demo_screening_snapshot(*, clock=None) -> ScreeningSnapshot:
    """Small synthetic fixture with STAR, ST-labelled and BSE examples, without network I/O."""
    now = (clock or (lambda: datetime.now(timezone.utc)))()
    if not _aware(now):
        raise InvalidRequestError("Demo clock must be timezone-aware")
    identities = (Security("000001.SZ", "平安银行"), Security("600010.SH", "*ST人工样例"),
                  Security("688981.SH", "中芯国际"), Security("300001.SZ", "特锐德"),
                  Security("920000.BJ", "安徽凤凰"))
    quotes = tuple(ScreeningQuote(item.instrument_id, item.name, now,
        price=10.0 + index, change_percent=index - 2.0, amount_cny=(index + 1) * 1000000.0,
        turnover_percent=index + 0.5, pe_dynamic=None if index == 2 else 5.0 + index * 2,
        pb=0.8 + index * 0.2) for index, item in enumerate(identities))
    directory = SecurityDirectory(identities, "screening_synthetic_identity_fixture", now, is_fixture=True)
    strata = tuple(ScreeningStratumCoverage(key, 1, 1, 1, 0) for key in STRATA)
    return ScreeningSnapshot(quotes, "synthetic_quote_fixture_not_live_market", now, directory, strata, True)
