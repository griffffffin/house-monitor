"""Shared data model and low-level parsing helpers used by every scraper."""

import re
from dataclasses import dataclass
from typing import Optional


@dataclass
class Listing:
    """A single real-estate listing, normalized to a common shape across all sources."""

    id: str
    title: str
    price: float
    url: str
    source: str
    first_seen: str
    last_seen: str
    price_changed: bool = False
    old_price: float = 0.0


def parse_de_price(text: str) -> float:
    """Parse a German-formatted price string into a float.

    German number formatting uses '.' as the thousands separator and ',' as
    the decimal separator (e.g. "139.000,00" -> 139000.0, "45.000 €" -> 45000.0).
    Returns 0.0 instead of raising when no price can be found, since callers
    already treat 0.0 as "price could not be determined".
    """
    if not text:
        return 0.0
    m = re.search(r"\d[\d.,]*", text)
    if not m:
        return 0.0
    raw = m.group(0).rstrip(".,").replace(".", "").replace(",", ".")
    try:
        return float(raw)
    except ValueError:
        return 0.0


def decode_utf8_or_latin1(raw: bytes, declared_charset: Optional[str] = None) -> str:
    """Decode response bytes, preferring UTF-8 over the server's declared charset.

    Some servers incorrectly declare latin-1/iso-8859-1 for a response whose
    bytes are actually UTF-8. Since latin-1 decoding accepts any byte sequence
    without error, trusting the declared encoding first would silently produce
    mojibake (e.g. "Grünen" -> "GrÃ¼nen") instead of failing loudly.
    """
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        return raw.decode(declared_charset or "latin-1")
    except (UnicodeDecodeError, LookupError):
        return raw.decode("utf-8", errors="replace")
