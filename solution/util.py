"""Small shared helpers: time, email normalization, deterministic hashing, errors."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any


class RejectError(Exception):
    """A request or case is refused before any business state is touched."""

    def __init__(self, code: str, message: str, details: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


def iso(ms: int | None) -> str | None:
    """Render epoch milliseconds as an ISO-8601 UTC string."""
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso_ms(text: str) -> int:
    """Parse an ISO-8601 instant (trailing Z or an explicit offset) to epoch ms."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("not a non-empty string")
    value = text.strip()
    if value.endswith(("Z", "z")):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def stable(obj: Any) -> str:
    """Deterministic JSON for hashing request parameters and storing receipts."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256(obj: Any) -> str:
    return hashlib.sha256(stable(obj).encode("utf-8")).hexdigest()


def normalize_email(raw: Any) -> str:
    """ASCII-lowercase the domain, preserve the local part verbatim.

    The whole address must be printable ASCII; any non-ASCII character would make
    the required "domain is ASCII lower case" rule ambiguous, so it is refused.
    """
    if not isinstance(raw, str):
        raise RejectError("INVALID_EMAIL", "email must be a string")
    if not raw:
        raise RejectError("INVALID_EMAIL", "email must not be empty")
    if not all(0x20 < ord(ch) < 0x7F for ch in raw):
        raise RejectError("INVALID_EMAIL", "email must contain printable ASCII characters only")
    if raw.count("@") != 1:
        raise RejectError("INVALID_EMAIL", "email must contain exactly one '@'")
    local, domain = raw.split("@")
    if not local:
        raise RejectError("INVALID_EMAIL", "email local part must not be empty")
    if not domain:
        raise RejectError("INVALID_EMAIL", "email domain must not be empty")
    for label in domain.split("."):
        if not label:
            raise RejectError("INVALID_EMAIL", "email domain must not contain empty labels")
        if label[0] == "-" or label[-1] == "-":
            raise RejectError("INVALID_EMAIL", "email domain labels must not start or end with '-'")
    return local + "@" + domain.lower()
