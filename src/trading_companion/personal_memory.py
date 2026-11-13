"""Bounded local watchlist records; these fields never form model context."""

from datetime import datetime, timezone
from hashlib import sha256
import json
import re

from trading_companion.akshare_adapter import to_akshare_symbol
from trading_companion.errors import InvalidRequestError


VERSION = "personal-memory-watchlist-v1"
PREFERENCES_VERSION = "personal-memory-preferences-v1"
STORE_VERSION = "personal-memory-store-v2"
LOCAL_USER = "local"
MAX_WATCHLIST = 50
MAX_PREFERENCES = 50
DISABLED_MESSAGE = "本服务未开启自选股；请使用启动参数 --personal-memory-db 开启。"

_ERROR_CODES = frozenset((
    "invalid_personal_memory_path", "invalid_personal_memory_user", "invalid_personal_memory_time",
    "invalid_watchlist_instrument", "invalid_watchlist_name", "personal_memory_schema_mismatch",
    "personal_memory_corrupt", "personal_memory_store_unavailable", "personal_memory_store_closed",
    "watchlist_full", "preferences_full", "preferences_conflict", "invalid_preferences", "empty_preferences",
    "invalid_preferences_revision",
))
PREFERENCE_FIELDS = ("holding_status", "horizon", "risk")
_PREFERENCE_VALUES = {
    "holding_status": frozenset(("unknown", "held", "not_held")),
    "horizon": frozenset(("short", "medium", "long")),
    "risk": frozenset(("low", "medium", "high")),
}


class PersonalMemoryError(ValueError):
    """Only fixed application codes may cross the local-store error boundary."""

    def __init__(self, code):
        self.code = code if type(code) is str and code in _ERROR_CODES else "personal_memory_store_unavailable"
        super().__init__(self.code)


def checked_user(value):
    if (type(value) is not str or not 1 <= len(value) <= 64 or value != value.strip()
            or not value.isprintable()):
        raise PersonalMemoryError("invalid_personal_memory_user")
    return value


def checked_instrument_id(value):
    if type(value) is not str or re.fullmatch(r"[0-9]{6}\.(?:SH|SZ|BJ)", value) is None:
        raise PersonalMemoryError("invalid_watchlist_instrument")
    try:
        to_akshare_symbol(value)
    except InvalidRequestError:
        raise PersonalMemoryError("invalid_watchlist_instrument") from None
    return value


def checked_name(value):
    if (type(value) is not str or not 1 <= len(value) <= 80 or not value.isprintable()
            or not value.strip()):
        raise PersonalMemoryError("invalid_watchlist_name")
    return value.strip()


def timestamp(value):
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise PersonalMemoryError("invalid_personal_memory_time")
    try:
        return value.astimezone(timezone.utc).isoformat(timespec="seconds")
    except (OverflowError, ValueError):
        raise PersonalMemoryError("invalid_personal_memory_time") from None


def checked_record(value):
    """Validate persisted/public rows, including canonical aware UTC timestamps."""
    try:
        if type(value) is not dict or value.keys() != {"instrument_id", "name", "created_at", "updated_at"}:
            raise PersonalMemoryError("personal_memory_corrupt")
        instrument_id = checked_instrument_id(value["instrument_id"])
        name = checked_name(value["name"])
        if name != value["name"]:
            raise PersonalMemoryError("personal_memory_corrupt")
        dates = []
        for key in ("created_at", "updated_at"):
            text = value[key]
            if type(text) is not str or not 20 <= len(text) <= 32:
                raise PersonalMemoryError("personal_memory_corrupt")
            parsed = datetime.fromisoformat(text)
            if timestamp(parsed) != text:
                raise PersonalMemoryError("personal_memory_corrupt")
            dates.append(parsed)
        if dates[1] < dates[0]:
            raise PersonalMemoryError("personal_memory_corrupt")
        return {"instrument_id": instrument_id, "name": name,
                "created_at": value["created_at"], "updated_at": value["updated_at"]}
    except (PersonalMemoryError, ValueError, TypeError, OverflowError):
        raise PersonalMemoryError("personal_memory_corrupt") from None


def checked_preferences_changes(value):
    """Missing keys stay untouched; explicit None clears exactly that category."""
    if type(value) is not dict or not value or value.keys() - set(PREFERENCE_FIELDS):
        raise PersonalMemoryError("invalid_preferences")
    for key, field in value.items():
        if field is not None and (type(field) is not str or field not in _PREFERENCE_VALUES[key]):
            raise PersonalMemoryError("invalid_preferences")
    return dict(value)


def checked_revision(value, *, collection=False):
    length = 64 if collection else 32
    if type(value) is not str or len(value) != length or re.fullmatch(r"[0-9a-f]+", value) is None:
        raise PersonalMemoryError("invalid_preferences_revision")
    return value


def checked_preference_record(value):
    try:
        base_keys = ("instrument_id", "name", "created_at", "updated_at")
        if type(value) is not dict or value.keys() != {*base_keys, *PREFERENCE_FIELDS, "revision"}:
            raise PersonalMemoryError("personal_memory_corrupt")
        result = checked_record({key: value[key] for key in base_keys})
        categories = checked_preferences_changes({key: value[key] for key in PREFERENCE_FIELDS})
        if not any(field is not None for field in categories.values()):
            raise PersonalMemoryError("personal_memory_corrupt")
        return {**result, **categories, "revision": checked_revision(value["revision"])}
    except (PersonalMemoryError, ValueError, TypeError):
        raise PersonalMemoryError("personal_memory_corrupt") from None


def preferences_collection_revision(items, user_id=LOCAL_USER):
    """A bounded content token, never a second stored copy or a time-based revision."""
    user_id = checked_user(user_id)
    if type(items) is not list or len(items) > MAX_PREFERENCES:
        raise PersonalMemoryError("personal_memory_corrupt")
    checked = [checked_preference_record(item) for item in items]
    if len({item["instrument_id"] for item in checked}) != len(checked):
        raise PersonalMemoryError("personal_memory_corrupt")
    payload = {"version": PREFERENCES_VERSION, "user_id": user_id,
               "items": sorted(checked, key=lambda item: item["instrument_id"])}
    return sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
