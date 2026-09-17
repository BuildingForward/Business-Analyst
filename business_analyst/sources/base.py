"""Source interface and shared parsing helpers."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Iterable, Iterator, List, Optional

from ..models import Listing, SellerFinancing

_MULTIPLIERS = {"k": 1_000, "m": 1_000_000, "mm": 1_000_000, "b": 1_000_000_000}
_MONEY = re.compile(r"\$?\s*([\d,]+(?:\.\d+)?)\s*(k|mm|m|b)?\b", re.I)


def parse_money(text: Optional[str]) -> Optional[float]:
    """Parse '$1.2M', '450,000', '250k' into a float. None when absent."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    cleaned = text.strip()
    if not cleaned or cleaned.lower() in {"n/a", "na", "-", "undisclosed", "not disclosed"}:
        return None
    match = _MONEY.search(cleaned)
    if not match:
        return None
    number = float(match.group(1).replace(",", ""))
    suffix = (match.group(2) or "").lower()
    return number * _MULTIPLIERS.get(suffix, 1)


def parse_int(text: Optional[str]) -> Optional[int]:
    value = parse_money(text)
    return int(value) if value is not None else None


def parse_financing(text: Optional[str]) -> SellerFinancing:
    """Read a free-text financing field into the enum."""
    if not text:
        return SellerFinancing.UNKNOWN
    body = str(text).strip().lower()
    if body in {"yes", "y", "true", "offered", "available"}:
        return SellerFinancing.OFFERED
    if body in {"no", "n", "false", "refused", "cash only"}:
        return SellerFinancing.REFUSED
    if "negotiab" in body or "consider" in body or "possible" in body or "maybe" in body:
        return SellerFinancing.NEGOTIABLE
    if "no seller" in body or "no owner" in body or "cash only" in body or "all cash" in body:
        return SellerFinancing.REFUSED
    if "seller financ" in body or "owner financ" in body or "will carry" in body:
        return SellerFinancing.OFFERED
    return SellerFinancing.UNKNOWN


def parse_pct(text: Optional[str]) -> Optional[float]:
    """'70%' or '0.7' -> 0.7."""
    if text is None or text == "":
        return None
    if isinstance(text, (int, float)):
        value = float(text)
    else:
        cleaned = str(text).strip().rstrip("%")
        try:
            value = float(cleaned)
        except ValueError:
            return None
        if "%" in str(text):
            value /= 100
            return max(0.0, min(1.0, value))
    return max(0.0, min(1.0, value / 100 if value > 1 else value))


# Signals that a location is corporate-owned or franchised. A chain outlet
# has no owner who can sell you the business, so these must be filtered
# whatever source they arrive from - not just the one that happened to
# implement the check first.
_CHAIN_HINTS = (
    "mcdonald", "subway", "starbucks", "7-eleven", "walmart", "target",
    "home depot", "lowe's", "autozone", "o'reilly", "jiffy lube", "midas",
    "meineke", "valvoline", "roto-rooter", "rotorooter", "servpro", "terminix",
    "orkin", "merry maids", "chem-dry", "jan-pro", "coverall", "u-haul",
    "public storage", "extra space", "cubesmart", "h&r block", "jackson hewitt",
    "geico", "state farm", "allstate", "progressive", "farmers insurance",
    "ace hardware", "napa auto", "les schwab", "aamco", "maaco", "snap-on",
)

_FRANCHISE_WORDS = re.compile(r"\bfranchise[ed]?\b|\bcorporate[- ]owned\b|\bchain\b", re.I)


def looks_like_chain(name: str, *context: str) -> bool:
    """True if the name or surrounding text marks this as a chain outlet.

    Context can be anything descriptive - listing copy, a notes column -
    so an explicit "Franchise location" is caught as well as a known brand.
    """
    lowered = (name or "").lower()
    if any(hint in lowered for hint in _CHAIN_HINTS):
        return True
    return bool(_FRANCHISE_WORDS.search(" ".join(c or "" for c in context)))


class Source(ABC):
    """A place listings come from.

    Implementations must be polite: respect robots.txt and rate limits, and
    never authenticate against a site the operator has not agreed terms with.
    """

    name: str = "source"
    #: Set False for sources that require a paid or licensed feed.
    free: bool = True

    @abstractmethod
    def fetch(self, limit: int = 50) -> Iterable[Listing]:
        """Yield listings, newest or most relevant first."""

    def __iter__(self) -> Iterator[Listing]:
        return iter(self.fetch())


class SourceRegistry:
    """Name -> source instance, so the CLI can select sources by name."""

    def __init__(self) -> None:
        self._sources = {}

    def register(self, source: Source) -> Source:
        self._sources[source.name] = source
        return source

    def get(self, name: str) -> Optional[Source]:
        return self._sources.get(name)

    def names(self) -> List[str]:
        return sorted(self._sources)

    def all(self) -> List[Source]:
        return [self._sources[n] for n in self.names()]

    def fetch(self, names: Optional[Iterable[str]] = None, limit: int = 50) -> List[Listing]:
        """Fetch from the named sources, skipping any that fail."""
        import logging

        log = logging.getLogger(__name__)
        chosen = [self._sources[n] for n in (names or self.names()) if n in self._sources]
        listings: List[Listing] = []
        for source in chosen:
            try:
                listings.extend(source.fetch(limit=limit))
            except Exception as exc:  # noqa: BLE001 - a dead source must not stop the run
                log.error("Source %s failed: %s", source.name, exc)
        return listings
