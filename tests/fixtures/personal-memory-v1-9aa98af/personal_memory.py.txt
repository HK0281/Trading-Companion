"""Bounded local watchlist records; these fields never form model context."""

from datetime import datetime, timezone
import re

from trading_companion.akshare_adapter import to_akshare_symbol
from trading_companion.errors import InvalidRequestError


VERSION = "personal-memory-watchlist-v1"
STORE_VERSION = "personal-memory-store-v1"
LOCAL_USER = "local"
MAX_WATCHLIST = 50
DISABLED_MESSAGE = "本服务未开启自选股；请使用启动参数 --personal-memory-db 开启。"

_ERROR_CODES = frozenset((
    "invalid_personal_memory_path", "invalid_personal_memory_user", "invalid_personal_memory_time",
    "invalid_watchlist_instrument", "invalid_watchlist_name", "personal_memory_schema_mismatch",
    "personal_memory_corrupt", "personal_memory_store_unavailable", "personal_memory_store_closed",
    "watchlist_full",
))


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
