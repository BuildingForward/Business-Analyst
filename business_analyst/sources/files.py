"""File-backed sources: broker exports, manual entry, saved searches.

These carry the bot day to day. A broker's spreadsheet, a CSV exported from a
marketplace's own "save search" feature, or a hand-kept list of businesses the
operator has spotted locally all land here.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from ..models import Listing
from .base import Source, parse_financing, parse_int, parse_money, parse_pct

# Accepts the column names marketplaces and brokers actually use.
_ALIASES: Dict[str, List[str]] = {
    "name": ["name", "business name", "title", "headline", "business"],
    "external_id": ["id", "external_id", "listing id", "listing_id", "ref", "reference"],
    "url": ["url", "link", "listing url", "web"],
    "industry": ["industry", "category", "sector", "type", "business type"],
    "location": ["location", "city", "market", "region", "state", "area"],
    "description": ["description", "summary", "details", "notes", "overview", "blurb"],
    "asking_price": ["asking price", "asking_price", "price", "ask", "list price"],
    "revenue": ["revenue", "gross revenue", "sales", "gross sales", "annual revenue"],
    "cash_flow": ["cash flow", "cash_flow", "sde", "seller discretionary earnings", "owner benefit"],
    "ebitda": ["ebitda", "operating income"],
    "inventory": ["inventory"],
    "ffe": ["ffe", "ff&e", "furniture fixtures equipment", "equipment"],
    "established_year": ["established", "established_year", "year established", "founded", "since"],
    "employees": ["employees", "staff", "headcount", "employee count"],
    "owner_hours_per_week": ["owner hours", "owner_hours_per_week", "hours per week", "owner hours/week"],
    "reason_for_sale": ["reason for sale", "reason_for_sale", "reason", "why selling"],
    "seller_financing": ["seller financing", "seller_financing", "owner financing", "financing", "terms"],
    "seller_financing_pct": ["seller financing %", "seller_financing_pct", "carry %", "financing pct"],
    "days_on_market": ["days on market", "days_on_market", "dom", "listed days"],
    "real_estate_included": ["real estate", "real_estate_included", "property included", "re included"],
}

_TRUTHY = {"y", "yes", "true", "1", "included", "t"}


def _normalise_keys(row: Dict[str, Any]) -> Dict[str, Any]:
    return {str(k).strip().lower(): v for k, v in row.items() if k is not None}


def _pick(row: Dict[str, Any], field: str) -> Optional[Any]:
    for alias in _ALIASES.get(field, [field]):
        if alias in row:
            value = row[alias]
            if value not in (None, ""):
                return value
    return None


def row_to_listing(row: Dict[str, Any], source_name: str, index: int = 0) -> Listing:
    """Map one arbitrary spreadsheet row onto a Listing."""
    row = _normalise_keys(row)
    name = _pick(row, "name") or f"unnamed listing {index}"
    external = _pick(row, "external_id") or _pick(row, "url") or f"{source_name}-{index}"
    re_included = str(_pick(row, "real_estate_included") or "").strip().lower() in _TRUTHY

    return Listing(
        source=source_name,
        external_id=str(external),
        name=str(name).strip(),
        url=str(_pick(row, "url") or ""),
        industry=str(_pick(row, "industry") or "").strip(),
        location=str(_pick(row, "location") or "").strip(),
        description=str(_pick(row, "description") or "").strip(),
        asking_price=parse_money(_pick(row, "asking_price")),
        revenue=parse_money(_pick(row, "revenue")),
        cash_flow=parse_money(_pick(row, "cash_flow")),
        ebitda=parse_money(_pick(row, "ebitda")),
        inventory=parse_money(_pick(row, "inventory")),
        ffe=parse_money(_pick(row, "ffe")),
        real_estate_included=re_included,
        established_year=parse_int(_pick(row, "established_year")),
        employees=parse_int(_pick(row, "employees")),
        owner_hours_per_week=parse_money(_pick(row, "owner_hours_per_week")),
        reason_for_sale=str(_pick(row, "reason_for_sale") or "").strip(),
        seller_financing=parse_financing(_pick(row, "seller_financing")),
        seller_financing_pct=parse_pct(_pick(row, "seller_financing_pct")),
        days_on_market=parse_int(_pick(row, "days_on_market")),
        raw=dict(row),
    )


class CsvSource(Source):
    """Read listings from a CSV with flexible column naming."""

    def __init__(self, path: str | Path, name: Optional[str] = None):
        self.path = Path(path)
        self.name = name or f"csv:{self.path.stem}"

    def fetch(self, limit: int = 50) -> Iterable[Listing]:
        if not self.path.exists():
            raise FileNotFoundError(f"No such listings file: {self.path}")
        listings: List[Listing] = []
        with self.path.open(newline="", encoding="utf-8-sig") as fh:
            for i, row in enumerate(csv.DictReader(fh)):
                if i >= limit:
                    break
                listings.append(row_to_listing(row, self.name, i))
        return listings


class JsonSource(Source):
    """Read listings from a JSON array, or a {"listings": [...]} object."""

    def __init__(self, path: str | Path, name: Optional[str] = None):
        self.path = Path(path)
        self.name = name or f"json:{self.path.stem}"

    def fetch(self, limit: int = 50) -> Iterable[Listing]:
        if not self.path.exists():
            raise FileNotFoundError(f"No such listings file: {self.path}")
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        rows = payload.get("listings", []) if isinstance(payload, dict) else payload
        return [row_to_listing(r, self.name, i) for i, r in enumerate(rows[:limit])]
