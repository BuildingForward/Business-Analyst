"""Local-business sources other than Google.

A CSV importer for lists you already hold (chamber of commerce rosters,
licence registries, an export from a tool you pay for), so Google is the
default source but never the only possible one.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from ..models import Prospect
from .base import Source, looks_like_chain, parse_int
from .files import _normalise_keys

_ALIASES: Dict[str, List[str]] = {
    "name": ["name", "business name", "company", "title", "dba"],
    "external_id": ["id", "external_id", "licence", "license", "registration", "ref"],
    "industry": ["industry", "category", "type", "sic", "naics description", "trade"],
    "address": ["address", "street", "address line 1", "street address"],
    "city": ["city", "town", "locality"],
    "state": ["state", "province", "region"],
    "postcode": ["zip", "postcode", "postal code", "zip code"],
    "phone": ["phone", "telephone", "phone number", "tel"],
    "email": ["email", "e-mail", "email address"],
    "website": ["website", "url", "web", "site"],
    "owner_name": ["owner", "owner name", "principal", "registered agent", "contact"],
    "employees": ["employees", "employee count", "staff", "headcount"],
    "established_year": ["established", "year established", "founded", "since",
                         "registration date", "incorporated"],
    "review_count": ["reviews", "review count", "number of reviews"],
    "notes": ["notes", "description", "summary"],
}

_TRUTHY = {"y", "yes", "true", "1"}


def _pick(row: Dict[str, Any], field: str) -> Optional[Any]:
    for alias in _ALIASES.get(field, [field]):
        if alias in row and row[alias] not in (None, ""):
            return row[alias]
    return None


def row_to_prospect(row: Dict[str, Any], source_name: str, index: int = 0) -> Prospect:
    row = _normalise_keys(row)
    name = _pick(row, "name") or f"unnamed business {index}"
    external = _pick(row, "external_id") or _pick(row, "phone") or f"{source_name}-{index}"
    notes = str(_pick(row, "notes") or "").strip()
    declared = str(_pick(row, "is chain") or "").strip().lower() in _TRUTHY
    chain = declared or looks_like_chain(str(name), notes, str(_pick(row, "website") or ""))

    return Prospect(
        source=source_name,
        external_id=str(external),
        name=str(name).strip(),
        industry=str(_pick(row, "industry") or "").strip(),
        address=str(_pick(row, "address") or "").strip(),
        city=str(_pick(row, "city") or "").strip(),
        state=str(_pick(row, "state") or "").strip(),
        postcode=str(_pick(row, "postcode") or "").strip(),
        phone=str(_pick(row, "phone") or "").strip(),
        email=str(_pick(row, "email") or "").strip(),
        website=str(_pick(row, "website") or "").strip(),
        owner_name=str(_pick(row, "owner_name") or "").strip(),
        employees=parse_int(_pick(row, "employees")),
        established_year=parse_int(_pick(row, "established_year")),
        review_count=parse_int(_pick(row, "review_count")),
        is_chain=chain,
        notes=notes,
        raw=dict(row),
    )


class CsvProspectSource(Source):
    """Local businesses from a CSV you already hold."""

    def __init__(self, path: str | Path, name: Optional[str] = None):
        self.path = Path(path)
        self.name = name or f"csv:{self.path.stem}"

    def fetch(self, limit: int = 500) -> Iterable[Prospect]:
        if not self.path.exists():
            raise FileNotFoundError(f"No such prospect file: {self.path}")
        out: List[Prospect] = []
        with self.path.open(newline="", encoding="utf-8-sig") as fh:
            for i, row in enumerate(csv.DictReader(fh)):
                if i >= limit:
                    break
                out.append(row_to_prospect(row, self.name, i))
        return out
