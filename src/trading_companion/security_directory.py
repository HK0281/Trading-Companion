"""Trusted, dated security snapshots; model memory never supplies identity mappings."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import tempfile
import unicodedata

from trading_companion.akshare_adapter import to_akshare_symbol
from trading_companion.errors import DataSourceError, DataValidationError, InvalidRequestError


MAX_SNAPSHOT_BYTES = 5_000_000


def _text(value: object, limit: int = 80) -> str:
    if (type(value) is not str or not value.strip() or len(value) > limit
            or any(not character.isprintable() for character in value)):
        raise DataValidationError("Invalid directory text")
    return value.strip()


def _normalized(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold().replace(" ", "")


@dataclass(frozen=True)
class Security:
    instrument_id: str
    name: str
    aliases: tuple[str, ...] = ()

    def __post_init__(self):
        try:
            to_akshare_symbol(self.instrument_id)
        except InvalidRequestError:
            raise DataValidationError("Invalid directory instrument") from None
        object.__setattr__(self, "name", _text(self.name))
        if type(self.aliases) is not tuple or len(self.aliases) > 12:
            raise DataValidationError("Invalid directory aliases")
        object.__setattr__(self, "aliases", tuple(dict.fromkeys(_text(alias) for alias in self.aliases)))

    def to_dict(self) -> dict[str, str]:
        return {"instrument_id": self.instrument_id, "name": self.name}


@dataclass(frozen=True)
class SecurityMatch:
    status: str
    match_type: str
    candidates: tuple[Security, ...]


@dataclass(frozen=True)
class SecurityDirectory:
    securities: tuple[Security, ...]
    source: str
    retrieved_at: datetime
    is_fixture: bool = False

    def __post_init__(self):
        if (type(self.securities) is not tuple or not 1 <= len(self.securities) <= 10000
                or any(type(item) is not Security for item in self.securities)):
            raise DataValidationError("Invalid directory records")
        if len({item.instrument_id for item in self.securities}) != len(self.securities):
            raise DataValidationError("Duplicate directory instrument")
        object.__setattr__(self, "source", _text(self.source, 128))
        if (type(self.retrieved_at) is not datetime or self.retrieved_at.tzinfo is None
                or self.retrieved_at.utcoffset() is None or type(self.is_fixture) is not bool):
            raise DataValidationError("Directory needs an aware timestamp and explicit fixture flag")

    def assert_usable(self, reference_date: date, *, development: bool) -> None:
        if type(reference_date) is not date:
            raise InvalidRequestError("Reference date must be a date")
        if self.is_fixture and not development:
            raise DataValidationError("Fixture directory cannot authorize online research")
        snapshot_date = self.retrieved_at.astimezone(timezone(timedelta(hours=8))).date()
        if not 0 <= (reference_date - snapshot_date).days <= 7:
            raise DataValidationError("Directory is stale or dated in the future")

    def metadata(self) -> dict[str, object]:
        return {"source": self.source, "retrieved_at": self.retrieved_at.isoformat(),
                "is_fixture": self.is_fixture, "records": len(self.securities),
                "identity_scope": "current_snapshot_not_historical_names_or_risk_status"}

    def lookup(self, mention: str) -> SecurityMatch:
        """Only exact identities can resolve automatically; partial matches suggest."""
        query = _normalized(_text(mention))
        exact = tuple(item for item in self.securities if query in (
            _normalized(item.name), item.instrument_id.casefold(), item.instrument_id[:6]))
        if exact:
            return SecurityMatch("resolved" if len(exact) == 1 else "ambiguous", "exact", exact)
        aliases = tuple(item for item in self.securities
                        if any(query == _normalized(alias) for alias in item.aliases))
        partial = tuple(item for item in self.securities if len(query) >= 2 and any(
            query in _normalized(name) for name in (item.name, *item.aliases)))
        candidates = tuple(sorted({item.instrument_id: item for item in (*aliases, *partial)}.values(),
                                  key=lambda item: item.instrument_id))
        if len(candidates) > 1:
            return SecurityMatch("ambiguous", "name_candidates", candidates)
        if aliases and len(candidates) == 1:
            return SecurityMatch("resolved", "verified_alias", candidates)
        return SecurityMatch("needs_confirmation" if candidates else "not_found", "partial", candidates)

    def mentioned_spans(self, question: str) -> tuple[tuple[int, int], ...]:
        """Find known literal identities independently of the model's extraction."""
        spans = {(match.start(), match.end()) for match in re.finditer(
            r"(?<![0-9])[0-9]{6}(?:\.(?:SH|SZ|BJ))?(?![0-9])", question, re.IGNORECASE)}
        for item in self.securities:
            for name in (item.name, *item.aliases):
                spans.update((match.start(), match.end()) for match in re.finditer(
                    re.escape(name), question, re.IGNORECASE))
        # Prefer full names/codes over aliases contained inside those same mentions.
        return tuple(sorted(span for span in spans if not any(
            other != span and other[0] <= span[0] and span[1] <= other[1] for other in spans)))


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise DataValidationError("Duplicate directory JSON key")
        result[key] = value
    return result


def load_security_directory(path: str | Path) -> SecurityDirectory:
    """Load only a trusted application-selected snapshot; never a model-selected path."""
    try:
        selected = Path(path)
        if selected.stat().st_size > MAX_SNAPSHOT_BYTES:
            raise DataValidationError("Directory snapshot is too large")
        payload = json.loads(selected.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    except OSError:
        raise DataSourceError("Cannot read the configured security directory") from None
    except (UnicodeError, ValueError, RecursionError):
        raise DataValidationError("Invalid directory snapshot JSON") from None
    try:
        if type(payload) is not dict or payload.keys() != {"source", "retrieved_at", "is_fixture", "securities"}:
            raise DataValidationError("Invalid directory snapshot fields")
        if type(payload["securities"]) is not list:
            raise DataValidationError("Invalid directory snapshot records")
        records = []
        for item in payload["securities"]:
            if (type(item) is not dict or item.keys() - {"instrument_id", "name", "aliases"}
                    or not {"instrument_id", "name"} <= item.keys()
                    or type(item.get("aliases", [])) is not list):
                raise DataValidationError("Invalid directory record fields")
            records.append(Security(item["instrument_id"], item["name"], tuple(item.get("aliases", []))))
        return SecurityDirectory(tuple(records), payload["source"],
                                 datetime.fromisoformat(payload["retrieved_at"]), payload["is_fixture"])
    except (TypeError, ValueError, KeyError):
        raise DataValidationError("Invalid directory snapshot data") from None


def check_directory_destination(path: str | Path) -> Path:
    """Never overwrite fixtures, unrelated JSON files, or symbolic-link targets."""
    selected = Path(path)
    try:
        if selected.suffix.lower() != ".json" or selected.is_symlink():
            raise InvalidRequestError("Directory output must be a non-symlink JSON snapshot")
        if selected.exists() and load_security_directory(selected).is_fixture:
            raise InvalidRequestError("Refusing to overwrite a fixture directory")
    except OSError:
        raise DataSourceError("Cannot inspect the configured directory destination") from None
    return selected


def save_security_directory(directory: SecurityDirectory, path: str | Path) -> None:
    """Verify a same-directory temporary file before atomically replacing the target."""
    if type(directory) is not SecurityDirectory:
        raise InvalidRequestError("Expected a validated security directory")
    temporary = None
    try:
        selected = check_directory_destination(path)
        payload = json.dumps({
            "source": directory.source, "retrieved_at": directory.retrieved_at.isoformat(),
            "is_fixture": directory.is_fixture,
            "securities": [{**item.to_dict(), "aliases": list(item.aliases)} for item in directory.securities],
        }, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
        if len(payload.encode("utf-8")) > MAX_SNAPSHOT_BYTES:
            raise DataValidationError("Directory snapshot is too large")
        selected.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=selected.parent,
                                         prefix=".directory-", suffix=".json", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if load_security_directory(temporary) != directory:
            raise DataValidationError("Directory snapshot did not round-trip")
        os.replace(temporary, selected)
        temporary = None
    except OSError:
        raise DataSourceError("Cannot save the security directory; no replacement was completed") from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                raise DataSourceError("Cannot remove the temporary directory snapshot; target was not replaced") from None
