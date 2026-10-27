"""Bounded quote sampling from trusted directory IDs or fixed vendor list pages."""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
from math import isfinite
import re
from time import monotonic
import unicodedata

from trading_companion.errors import DataSourceError, DataValidationError, InvalidRequestError
from trading_companion.screening import SHANGHAI, ScreeningQuote, ScreeningSnapshot, ScreeningStratumCoverage, market_stratum
from trading_companion.security_directory import Security, SecurityDirectory
from trading_companion.turn_control import checkpoint, request_timeout


ENDPOINT = "https://82.push2.eastmoney.com/api/qt/clist/get"
SOURCE = "eastmoney_stratified_quote_list_v1"
MAX_RESPONSE_BYTES = 200_000
MAX_TOTAL_BYTES = 2_000_000
TENCENT_ENDPOINT = "https://qt.gtimg.cn/q="
TENCENT_SOURCE = "tencent_directory_stratified_batch_quotes_v1"
MARKET_FILTERS = {
    "sse_main": "m:1 t:2", "szse_main": "m:0 t:6", "star": "m:1 t:23",
    "chinext": "m:0 t:80", "bse": "m:0 t:81 s:2048",
}
SOURCE_FIELDS = {"price": "f2", "change_percent": "f3", "amount_cny": "f6",
                 "turnover_percent": "f8", "pe_dynamic": "f9", "pb": "f23",
                 "market_cap_cny": "f20", "float_market_cap_cny": "f21"}


def _aware(value):
    return type(value) is datetime and value.tzinfo is not None and value.utcoffset() is not None


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise DataValidationError("Duplicate quote JSON key")
        result[key] = value
    return result


def _reject_constant(_):
    raise DataValidationError("Nonfinite quote JSON constant")


def _numeric(value, *, price=False):
    if value is None or value in ("-", "", "--"):
        return None
    if type(value) not in (str, int, float):
        raise DataValidationError("Invalid quote source number")
    if type(value) is str and (len(value) > 80 or not re.fullmatch(r"-?[0-9]+(?:\.[0-9]+)?", value)):
        raise DataValidationError("Invalid quote source decimal")
    try:
        parsed = Decimal(str(value))
        number = float(parsed)
        if not parsed.is_finite() or not isfinite(number):
            raise ValueError
    except (ValueError, InvalidOperation, OverflowError):
        raise DataValidationError("Invalid quote source number") from None
    # A zero last price is a missing quote, often seen in suspended/listing records.
    return None if price and number == 0 else number


def _identity(row):
    code, market = row.get("f12"), row.get("f13")
    if type(code) is not str or re.fullmatch(r"[0-9]{6}", code) is None or type(market) is not int:
        raise DataValidationError("Invalid quote instrument identity")
    if market == 1 and code.startswith("6"):
        exchange = "SH"
    elif market == 0 and code.startswith(("0", "3")):
        exchange = "SZ"
    elif market == 0 and code.startswith(("4", "8", "9")):
        exchange = "BJ"
    else:
        raise DataValidationError("Quote code does not match its source market")
    return Security(f"{code}.{exchange}", row.get("f14"))


def _normalized_name(value):
    return "".join(unicodedata.normalize("NFKC", value).split()).casefold()


def _tencent_name_matches(observed, security):
    observed_name, directory_name = _normalized_name(observed), _normalized_name(security.name)
    if observed_name == directory_name:
        return True
    # Tencent adds the STAR unprofitable-issuer qualifier to some exchange base names.
    return (market_stratum(security.instrument_id) == "star" and observed_name.endswith("-u")
            and observed_name[:-2] == directory_name)


class EastmoneyQuoteSnapshotSource:
    """Five fixed markets, up to two pages each; failed pages never become empty matches."""

    def __init__(self, *, session=None, clock=None, timeout_seconds=6.0, deadline_seconds=25.0,
                 page_size=100, pages_per_stratum=1):
        if (type(timeout_seconds) not in (int, float) or not isfinite(timeout_seconds)
                or not 0 < timeout_seconds <= 10 or type(deadline_seconds) not in (int, float)
                or not isfinite(deadline_seconds) or not 0 < deadline_seconds <= 30
                or type(page_size) is not int or not 1 <= page_size <= 100
                or type(pages_per_stratum) is not int or not 1 <= pages_per_stratum <= 2
                or clock is not None and not callable(clock)):
            raise InvalidRequestError("Quote collection must use bounded trusted configuration")
        self._session, self._clock = session, clock or (lambda: datetime.now(timezone.utc))
        self._timeout, self._deadline = timeout_seconds, deadline_seconds
        self._page_size, self._pages = page_size, pages_per_stratum

    def _now(self):
        value = self._clock()
        if not _aware(value):
            raise InvalidRequestError("Quote retrieval clock must be timezone-aware")
        return value

    def _json(self, params, deadline, byte_budget):
        checkpoint()
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise DataSourceError("Quote collection deadline exceeded")
        try:
            import requests
        except ImportError:
            raise DataSourceError("Install the data extra to request quote snapshots") from None
        client = requests if self._session is None else self._session
        timeout = request_timeout(min(self._timeout, remaining))
        response = None
        try:
            response = client.request("GET", ENDPOINT, params=params, timeout=(min(3.0, timeout), timeout),
                allow_redirects=False, stream=True,
                headers={"User-Agent": "TradingCompanion/0.1 public-research", "Accept": "application/json",
                         "Referer": "https://quote.eastmoney.com/"})
            checkpoint()
            if response.status_code != 200:
                raise DataSourceError("Quote source did not return HTTP 200; no retry or failover")
            declared = response.headers.get("Content-Length")
            maximum = min(MAX_RESPONSE_BYTES, byte_budget)
            if declared is not None and (type(declared) is not str or len(declared) > 9 or not declared.isascii()
                    or not declared.isdigit() or int(declared) > maximum):
                raise DataValidationError("Quote response exceeds the response budget")
            body = bytearray()
            for chunk in response.iter_content(8192):
                checkpoint()
                if monotonic() >= deadline:
                    raise DataSourceError("Quote collection deadline exceeded")
                if type(chunk) is not bytes or len(body) + len(chunk) > maximum:
                    raise DataValidationError("Quote response exceeds the response budget")
                body.extend(chunk)
            if monotonic() >= deadline:
                raise DataSourceError("Quote collection deadline exceeded")
            payload = json.loads(bytes(body).decode("utf-8-sig"), object_pairs_hook=_pairs,
                                 parse_constant=_reject_constant)
            return payload, len(body)
        except requests.RequestException:
            raise DataSourceError("Quote source connection failed; no retry or failover") from None
        except DataValidationError:
            raise
        except (UnicodeError, ValueError, RecursionError):
            raise DataValidationError("Quote source returned invalid JSON") from None
        finally:
            if response is not None:
                response.close()

    def read_snapshot(self, directory: SecurityDirectory) -> ScreeningSnapshot:
        """Only fresh, nonfixture configured directories authorize online identities."""
        if type(directory) is not SecurityDirectory:
            raise InvalidRequestError("Quote collection needs a validated security directory")
        started = self._now()
        directory.assert_usable(started.astimezone(SHANGHAI).date(), development=False)
        deadline, used_bytes, quotes, coverage = monotonic() + self._deadline, 0, [], []
        trusted = {item.instrument_id: item for item in directory.securities}
        seen = set()
        for stratum, market_filter in MARKET_FILTERS.items():
            total, scanned, verified, page_count, previous_code = None, 0, 0, 0, None
            for page in range(1, self._pages + 1):
                params = {"pn": str(page), "pz": str(self._page_size), "po": "0", "np": "1",
                          "fltt": "2", "invt": "2", "fid": "f12", "fs": market_filter,
                          "fields": ",".join((*SOURCE_FIELDS.values(), "f12", "f13", "f14", "f124"))}
                payload, size = self._json(params, deadline, MAX_TOTAL_BYTES - used_bytes)
                used_bytes += size
                if type(payload) is not dict or type(payload.get("rc")) is not int or payload["rc"] != 0:
                    raise DataValidationError("Quote source reported a business error")
                data = payload.get("data")
                if (type(data) is not dict or type(data.get("total")) is not int
                        or not 0 <= data["total"] <= 10000 or type(data.get("diff")) is not list):
                    raise DataValidationError("Quote source returned an invalid page")
                if total is None:
                    total = data["total"]
                elif data["total"] != total:
                    raise DataValidationError("Quote source total changed across pages")
                expected = min(self._page_size, max(0, total - (page - 1) * self._page_size))
                if len(data["diff"]) != expected:
                    raise DataValidationError("Quote source returned a short or oversized page")
                page_count += 1
                for row in data["diff"]:
                    checkpoint()
                    if type(row) is not dict:
                        raise DataValidationError("Invalid quote source record")
                    security = _identity(row)
                    if market_stratum(security.instrument_id) != stratum:
                        raise DataValidationError("Quote response escaped its requested market stratum")
                    if security.instrument_id in seen or previous_code is not None and security.instrument_id[:6] <= previous_code:
                        raise DataValidationError("Duplicate or out-of-order quote identity")
                    previous_code = security.instrument_id[:6]
                    seen.add(security.instrument_id)
                    scanned += 1
                    # Parse even unknown identities, so malformed rows cannot hide as exclusions.
                    stamp = row.get("f124")
                    if type(stamp) is not int or not 946684800 <= stamp <= 4102444800:
                        raise DataValidationError("Quote source is missing a valid dated timestamp")
                    quoted_at = datetime.fromtimestamp(stamp, timezone.utc)
                    values = {field: _numeric(row.get(raw), price=field == "price") for field, raw in SOURCE_FIELDS.items()}
                    if quoted_at > self._now():
                        raise DataValidationError("Quote source timestamp is in the future")
                    ScreeningQuote(security.instrument_id, security.name, quoted_at, **values)
                    if security.instrument_id not in trusted:
                        continue
                    identity = trusted[security.instrument_id]
                    if _normalized_name(security.name) != _normalized_name(identity.name):
                        raise DataValidationError("Quote name does not match the configured security directory")
                    quotes.append(ScreeningQuote(identity.instrument_id, identity.name, quoted_at, **values))
                    verified += 1
                if scanned == total:
                    break
            coverage.append(ScreeningStratumCoverage(stratum, total, scanned, verified, page_count))
        retrieved = self._now()
        if retrieved < started:
            raise InvalidRequestError("Quote retrieval clock moved backwards")
        return ScreeningSnapshot(tuple(quotes), SOURCE, retrieved, directory, tuple(coverage))


def _scaled_numeric(value, multiplier):
    number = _numeric(value)
    if number is None:
        return None
    try:
        result = float(Decimal(str(value)) * multiplier)
        if not isfinite(result):
            raise ValueError
        return result
    except (ValueError, InvalidOperation, OverflowError):
        raise DataValidationError("Invalid scaled quote source number") from None


class TencentQuoteSnapshotSource:
    """Explicit provider alternative, using only bounded IDs from the trusted directory."""

    def __init__(self, *, session=None, clock=None, timeout_seconds=6.0, deadline_seconds=25.0,
                 per_stratum=None, batch_size=80, coverage="standard"):
        if per_stratum is None:
            per_stratum = 200 if coverage == "expanded" else 50
        if (type(timeout_seconds) not in (int, float) or not isfinite(timeout_seconds)
                or not 0 < timeout_seconds <= 10 or type(deadline_seconds) not in (int, float)
                or not isfinite(deadline_seconds) or not 0 < deadline_seconds <= 30
                or type(coverage) is not str or coverage not in ("standard", "expanded")
                or type(per_stratum) is not int or not 1 <= per_stratum <= (100 if coverage == "standard" else 200)
                or type(batch_size) is not int or not 50 <= batch_size <= 80
                or clock is not None and not callable(clock)):
            raise InvalidRequestError("Tencent screening needs bounded trusted sampling and batch configuration")
        self._session, self._clock = session, clock or (lambda: datetime.now(timezone.utc))
        self._timeout, self._deadline = timeout_seconds, deadline_seconds
        self._per_stratum, self._batch_size = per_stratum, batch_size
        self._coverage = coverage

    def _now(self):
        value = self._clock()
        if not _aware(value):
            raise InvalidRequestError("Quote retrieval clock must be timezone-aware")
        return value

    def _download(self, symbols, deadline, byte_budget):
        checkpoint()
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise DataSourceError("Quote collection deadline exceeded")
        try:
            import requests
        except ImportError:
            raise DataSourceError("Install the data extra to request quote snapshots") from None
        response = None
        client = requests if self._session is None else self._session
        timeout = request_timeout(min(self._timeout, remaining))
        try:
            response = client.request("GET", TENCENT_ENDPOINT + ",".join(symbols),
                timeout=(min(3.0, timeout), timeout), allow_redirects=False, stream=True,
                headers={"User-Agent": "TradingCompanion/0.1 public-research", "Referer": "https://gu.qq.com/"})
            checkpoint()
            if response.status_code != 200:
                raise DataSourceError("Tencent quotes did not return HTTP 200; no retry or failover")
            maximum = min(MAX_RESPONSE_BYTES, byte_budget)
            declared = response.headers.get("Content-Length")
            if declared is not None and (type(declared) is not str or len(declared) > 9 or not declared.isascii()
                    or not declared.isdigit() or int(declared) > maximum):
                raise DataValidationError("Tencent quote response exceeds its response budget")
            body = bytearray()
            for chunk in response.iter_content(8192):
                checkpoint()
                if monotonic() >= deadline:
                    raise DataSourceError("Quote collection deadline exceeded")
                if type(chunk) is not bytes or len(body) + len(chunk) > maximum:
                    raise DataValidationError("Tencent quote response exceeds its response budget")
                body.extend(chunk)
            if monotonic() >= deadline:
                raise DataSourceError("Quote collection deadline exceeded")
            return bytes(body).decode("gb18030"), len(body)
        except requests.RequestException:
            raise DataSourceError("Tencent quote connection failed; no retry or failover") from None
        except UnicodeError:
            raise DataValidationError("Tencent quote response has invalid encoding") from None
        finally:
            if response is not None:
                response.close()

    def _parse(self, text, expected):
        # This endpoint returns assignments, not executable JavaScript or JSON.
        if text.strip() == 'v_pv_none_match="1";':
            return {}
        parsed, position = {}, 0
        pattern = re.compile(r'\s*v_([a-z]{2}[0-9]{6})="([^"\r\n]*)";')
        for match in pattern.finditer(text):
            if match.start() != position:
                raise DataValidationError("Unexpected Tencent quote response syntax")
            position = match.end()
            symbol, payload = match.groups()
            if symbol not in expected or symbol in parsed or len(payload) > 5000:
                raise DataValidationError("Tencent quote response identity is unexpected or duplicated")
            parsed[symbol] = payload
        if not parsed or text[position:].strip():
            raise DataValidationError("Unexpected or empty Tencent quote response syntax")
        result = {}
        for symbol, payload in parsed.items():
            if not payload:
                continue
            fields = payload.split("~")
            if not 83 <= len(fields) <= 120 or fields[2] != symbol[2:] or fields[82] != "CNY":
                raise DataValidationError("Tencent quote identity, currency or field schema changed")
            security = expected[symbol]
            observed = Security(security.instrument_id, fields[1])
            if not _tencent_name_matches(observed.name, security):
                raise DataValidationError("Tencent quote name does not match the configured directory")
            if re.fullmatch(r"[0-9]{14}", fields[30]) is None:
                raise DataValidationError("Tencent quote lacks a full dated timestamp")
            try:
                quoted_at = datetime.strptime(fields[30], "%Y%m%d%H%M%S").replace(tzinfo=SHANGHAI)
            except ValueError:
                raise DataValidationError("Tencent quote date is invalid") from None
            if quoted_at > self._now():
                raise DataValidationError("Tencent quote timestamp is in the future")
            amount = _scaled_numeric(fields[57], 10000)
            if amount is not None and fields[35]:
                detail = fields[35].split("/")
                if len(detail) != 3:
                    raise DataValidationError("Tencent raw amount detail is invalid")
                detailed_amount = _numeric(detail[2])
                if detailed_amount is None or abs(Decimal(str(amount)) - Decimal(str(detailed_amount))) > 1:
                    raise DataValidationError("Tencent amount units disagree within the quote")
            result[symbol] = ScreeningQuote(security.instrument_id, security.name, quoted_at,
                price=_numeric(fields[3], price=True), change_percent=_numeric(fields[32]),
                amount_cny=amount, turnover_percent=_numeric(fields[38]), pe_dynamic=_numeric(fields[52]),
                pb=_numeric(fields[46]), market_cap_cny=_scaled_numeric(fields[45], 100000000),
                float_market_cap_cny=_scaled_numeric(fields[44], 100000000))
        return result

    def read_snapshot(self, directory: SecurityDirectory) -> ScreeningSnapshot:
        if type(directory) is not SecurityDirectory:
            raise InvalidRequestError("Tencent quotes need a validated security directory")
        started = self._now()
        directory.assert_usable(started.astimezone(SHANGHAI).date(), development=False)
        deadline, used_bytes, quotes, coverage = monotonic() + self._deadline, 0, [], []
        for stratum in MARKET_FILTERS:
            universe = sorted((item for item in directory.securities if market_stratum(item.instrument_id) == stratum),
                              key=lambda item: item.instrument_id)
            if self._coverage == "expanded":
                selected_count = min(self._per_stratum, len(universe))
                selected = [universe[index * len(universe) // selected_count] for index in range(selected_count)]
            else:
                selected = universe[:self._per_stratum]
            found, requests_made = 0, 0
            for offset in range(0, len(selected), self._batch_size):
                batch = selected[offset:offset + self._batch_size]
                expected = {item.instrument_id[-2:].lower() + item.instrument_id[:6]: item for item in batch}
                text, size = self._download(tuple(expected), deadline, MAX_TOTAL_BYTES - used_bytes)
                used_bytes += size
                parsed = self._parse(text, expected)
                requests_made += 1
                quotes.extend(parsed.values())
                found += len(parsed)
                if monotonic() >= deadline:
                    raise DataSourceError("Quote collection deadline exceeded")
            coverage.append(ScreeningStratumCoverage(stratum, len(universe), len(selected), found, requests_made,
                missing_quote_records=len(selected) - found, total_basis="configured_directory",
                selection_limit=self._per_stratum if self._coverage == "expanded" else None))
        retrieved = self._now()
        if retrieved < started:
            raise InvalidRequestError("Quote retrieval clock moved backwards")
        return ScreeningSnapshot(tuple(quotes), TENCENT_SOURCE, retrieved, directory, tuple(coverage),
                                 sampling_policy=("configured_directory_code_uniform_interval" if self._coverage == "expanded"
                                     else "configured_directory_fixed_market_strata_code_ascending"))


def configured_quote_source(provider, coverage="standard", *, per_stratum=None, pages_per_stratum=None):
    """Trusted launcher/CLI options only; chat cannot alter these resource bounds."""
    if (type(provider) is not str or provider not in ("tencent", "eastmoney")
            or type(coverage) is not str or coverage not in ("standard", "expanded")):
        raise InvalidRequestError("Choose a supported quote source and coverage profile")
    if provider == "tencent":
        if pages_per_stratum is not None or coverage == "expanded" and per_stratum is not None:
            raise InvalidRequestError("Expanded coverage and provider-specific sample overrides cannot be combined")
        return TencentQuoteSnapshotSource(coverage=coverage,
            per_stratum=(200 if coverage == "expanded" else 50 if per_stratum is None else per_stratum))
    if per_stratum is not None or coverage == "expanded" and pages_per_stratum is not None:
        raise InvalidRequestError("Expanded coverage and provider-specific sample overrides cannot be combined")
    return EastmoneyQuoteSnapshotSource(pages_per_stratum=(2 if coverage == "expanded"
        else 1 if pages_per_stratum is None else pages_per_stratum))
