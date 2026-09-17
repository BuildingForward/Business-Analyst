"""Generic RSS/Atom and JSON HTTP sources.

Many brokerages and marketplaces publish a feed or a JSON endpoint for saved
searches. Those are fair game and stable. Scraping a site's HTML is not
included here on purpose: it breaks constantly and usually violates the site's
terms of service. Point this at a feed you are entitled to read, or export a
CSV and use CsvSource.
"""

from __future__ import annotations

import json
import logging
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any, Dict, Iterable, List, Optional

from ..models import Listing
from .base import Source
from .files import row_to_listing

log = logging.getLogger(__name__)

USER_AGENT = "business-analyst-bot/0.1 (+https://github.com/BuildingForward/Business-Analyst)"
_NS = {"atom": "http://www.w3.org/2005/Atom"}


def _http_get(url: str, timeout: float = 20.0) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return response.read()


class RssSource(Source):
    """Pull listings from an RSS 2.0 or Atom feed.

    Feeds rarely carry financials, so entries come through with title,
    link and description only. The screener will flag the missing cash flow
    rather than silently guessing at it.
    """

    def __init__(self, url: str, name: Optional[str] = None, industry: str = ""):
        self.url = url
        self.name = name or "rss"
        self.industry = industry

    def fetch(self, limit: int = 50) -> Iterable[Listing]:
        raw = _http_get(self.url)
        root = ET.fromstring(raw)
        items = root.findall(".//item") or root.findall(".//atom:entry", _NS)
        listings: List[Listing] = []

        for i, item in enumerate(items[:limit]):
            title = _text(item, "title") or _text(item, "atom:title")
            link = _text(item, "link") or _attr(item, "atom:link", "href")
            description = (
                _text(item, "description") or _text(item, "atom:summary")
                or _text(item, "atom:content") or ""
            )
            guid = _text(item, "guid") or _text(item, "atom:id") or link or f"{self.name}-{i}"
            listings.append(
                Listing(
                    source=self.name,
                    external_id=str(guid),
                    name=(title or f"feed item {i}").strip(),
                    url=(link or "").strip(),
                    industry=self.industry,
                    description=description.strip(),
                    raw={"feed": self.url},
                )
            )
        return listings


class JsonApiSource(Source):
    """Pull listings from a JSON HTTP endpoint.

    `records_path` is a dotted path to the array inside the payload, e.g.
    "data.results". Rows are mapped with the same flexible column aliases the
    CSV source uses.
    """

    def __init__(
        self,
        url: str,
        name: Optional[str] = None,
        records_path: str = "",
        headers: Optional[Dict[str, str]] = None,
    ):
        self.url = url
        self.name = name or "json-api"
        self.records_path = records_path
        self.headers = headers or {}

    def fetch(self, limit: int = 50) -> Iterable[Listing]:
        request = urllib.request.Request(
            self.url, headers={"User-Agent": USER_AGENT, **self.headers}
        )
        with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310
            payload = json.loads(response.read())

        rows: Any = payload
        for part in filter(None, self.records_path.split(".")):
            rows = rows.get(part, []) if isinstance(rows, dict) else []
        if isinstance(rows, dict):
            rows = rows.get("listings", [])
        if not isinstance(rows, list):
            raise ValueError(
                f"{self.name}: expected a list at '{self.records_path or 'root'}', "
                f"got {type(rows).__name__}"
            )
        return [row_to_listing(r, self.name, i) for i, r in enumerate(rows[:limit])]


def _text(item: ET.Element, tag: str) -> Optional[str]:
    node = item.find(tag, _NS) if ":" in tag else item.find(tag)
    return node.text if node is not None and node.text else None


def _attr(item: ET.Element, tag: str, attr: str) -> Optional[str]:
    node = item.find(tag, _NS) if ":" in tag else item.find(tag)
    return node.get(attr) if node is not None else None
