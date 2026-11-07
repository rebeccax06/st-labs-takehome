"""Small deterministic conversions shared by adapters; originals stay in evidence."""

import re
from datetime import UTC, datetime
from ipaddress import ip_address

from app.ingestion.errors import InputError


def text(value, path: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise InputError("Expected a string", path=path)
    return value.strip() or None


def timestamp(value, path: str) -> datetime | None:
    value = text(value, path)
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("Timezone missing")
        return parsed.astimezone(UTC)
    except (ValueError, OverflowError) as exc:
        raise InputError("Expected an ISO 8601 timestamp with timezone", path=path) from exc


def boolean(value, path: str, warn) -> bool | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        return value
    if not isinstance(value, str):
        raise InputError("Expected a boolean or boolean description", path=path)
    lowered = " ".join(value.casefold().split())
    if lowered in {"true", "yes", "enabled", "1", "filevault enabled", "filevault 2",
                   "luks full disk encryption", "bitlocker enabled"}:
        return True
    if lowered in {"false", "no", "disabled", "0", "not encrypted", "unencrypted",
                   "filevault disabled", "bitlocker disabled"}:
        return False
    warn("unknown_boolean", path, "Unrecognized boolean description; value remains unknown")
    return None


def status(value, path: str, warn, *, user=False) -> str | None:
    value = text(value, path)
    if value is None:
        return None
    mapped = {"active": "active", "enabled": "active", "inactive": "inactive",
              "deactivated": "inactive", "disabled": "inactive", "unknown": "unknown"}
    if user:
        mapped["suspended"] = "suspended"
    result = mapped.get(value.casefold())
    if result is None:
        warn("unknown_status", path, "Unrecognized status; normalized to unknown")
    return result or "unknown"


def operating_system(value, path: str) -> tuple[str | None, str | None]:
    value = text(value, path)
    if value is None:
        return None, None
    match = re.match(r"^(mac\s?os(?:\s+x)?|os\s+x|windows|ubuntu|debian|linux|rhel|centos|fedora|freebsd)\b(.*)",
                     value, re.IGNORECASE)
    if not match:
        return "unknown", value
    family, version = match.groups()
    key = family.casefold().replace(" ", "")
    normalized = ("macos" if key in {"macos", "macosx", "osx"} else
                  "linux" if key in {"ubuntu", "debian", "linux", "rhel", "centos", "fedora"} else key)
    return normalized, version.strip() or None


def ip(value, path: str) -> str | None:
    value = text(value, path)
    if value is None:
        return None
    try:
        return str(ip_address(value))
    except ValueError as exc:
        raise InputError("Invalid IP address", path=path) from exc


def mac(value, path: str) -> str | None:
    value = text(value, path)
    if value is None:
        return None
    compact = re.sub(r"[:.\-]", "", value)
    if not re.fullmatch(r"[0-9a-fA-F]{12}", compact):
        raise InputError("Invalid MAC address", path=path)
    return ":".join(compact[i:i + 2].upper() for i in range(0, 12, 2))


def nonmissing(values: dict) -> dict:
    return {key: value for key, value in values.items() if value is not None}
