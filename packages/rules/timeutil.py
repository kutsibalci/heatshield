"""The one place timestamps are parsed and formatted. Everything in HeatShield is UTC; a naive value is
taken to be UTC, and a bare date means midnight UTC."""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any


def to_utc(v: Any) -> datetime | None:
    """datetime / date / ISO-8601 string (with or without `Z`) → aware UTC datetime. None or "" → None."""
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if isinstance(v, date):
        return datetime(v.year, v.month, v.day, tzinfo=timezone.utc)
    dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def iso_z(dt: datetime | None) -> str | None:
    """Aware datetime → `2026-08-17T09:00:00Z`. None → None."""
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if dt else None
