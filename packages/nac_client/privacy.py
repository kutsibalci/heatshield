"""Privacy helpers: raw numbers never appear in logs or in API output."""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets

_E164 = re.compile(r"^\+?[0-9]{8,15}$")

# Without PHONE_HASH_SALT, a RANDOM salt per process. Previously there was a fixed "dev-salt-change-me":
# with a salt written in the repo, anyone could compute a number's hash and match it. The random salt
# is stable within the process (matching works) but changes on restart; that is why live mode makes
# the salt mandatory (see api.main.missing_production_secrets).
_PROCESS_SALT = secrets.token_hex(32)


def normalize_phone(phone: str) -> str:
    """Strips spaces/dashes, adds +. Raises ValueError if invalid."""
    p = re.sub(r"[\s\-\(\)]", "", phone or "")
    if p and not p.startswith("+"):
        p = "+" + p
    if not _E164.match(p):
        raise ValueError("geçersiz telefon numarası biçimi")
    return p


def mask_phone(phone: str) -> str:
    """+905551234567 -> +90555***4567 . The single format for logs and the UI."""
    try:
        p = normalize_phone(phone)
    except ValueError:
        return "***"
    if len(p) <= 8:
        return p[:3] + "***"
    return p[:6] + "***" + p[-4:]


def hash_phone(phone: str, salt: str | None = None) -> str:
    """One-way hashes the number with HMAC-SHA256. The salt comes from env (PHONE_HASH_SALT).

    The same number yields the same hash within the same tenant (for batch matching), but there is no way
    back from the hash to the number.
    """
    p = normalize_phone(phone)
    key = (salt if salt is not None else (os.environ.get("PHONE_HASH_SALT") or _PROCESS_SALT)).encode()
    return hmac.new(key, p.encode(), hashlib.sha256).hexdigest()


class MaskingFilter:
    """logging.Filter: masks E.164 numbers in the log record."""

    _pat = re.compile(r"\+[0-9]{8,15}")

    def filter(self, record):  # noqa: D401 - logging protocol
        try:
            msg = record.getMessage()
            masked = self._pat.sub(lambda m: mask_phone(m.group(0)), msg)
            record.msg = masked
            record.args = ()
        except Exception:  # pragma: no cover
            pass
        return True
