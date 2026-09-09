"""Gizlilik yardımcıları: loglarda ve API çıktısında ham numara asla yer almaz."""
from __future__ import annotations

import hashlib
import hmac
import os
import re

_E164 = re.compile(r"^\+?[0-9]{8,15}$")


def normalize_phone(phone: str) -> str:
    """Boşluk/tire temizler, + ekler. Geçersizse ValueError."""
    p = re.sub(r"[\s\-\(\)]", "", phone or "")
    if p and not p.startswith("+"):
        p = "+" + p
    if not _E164.match(p):
        raise ValueError("geçersiz telefon numarası biçimi")
    return p


def mask_phone(phone: str) -> str:
    """+905551234567 -> +90555***4567 . Log ve UI için tek biçim."""
    try:
        p = normalize_phone(phone)
    except ValueError:
        return "***"
    if len(p) <= 8:
        return p[:3] + "***"
    return p[:6] + "***" + p[-4:]


def hash_phone(phone: str, salt: str | None = None) -> str:
    """HMAC-SHA256 ile numarayı tek yönlü hash'ler. Salt env'den (PHONE_HASH_SALT) gelir.

    Aynı numara aynı tenant'ta aynı hash'i üretir (batch eşleştirme için), ama hash'ten numaraya dönüş yok.
    """
    p = normalize_phone(phone)
    key = (salt if salt is not None else os.environ.get("PHONE_HASH_SALT", "dev-salt-change-me")).encode()
    return hmac.new(key, p.encode(), hashlib.sha256).hexdigest()


class MaskingFilter:
    """logging.Filter: kayıttaki E.164 numaraları maskeler."""

    _pat = re.compile(r"\+[0-9]{8,15}")

    def filter(self, record):  # noqa: D401 - logging protokolü
        try:
            msg = record.getMessage()
            masked = self._pat.sub(lambda m: mask_phone(m.group(0)), msg)
            record.msg = masked
            record.args = ()
        except Exception:  # pragma: no cover
            pass
        return True
