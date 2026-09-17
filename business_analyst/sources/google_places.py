"""Google Places API (New) - the primary lead source.

This queries Google's official Places API rather than scraping search or
Maps result pages. Scraping those violates Google's terms, and Google
actively defends against it: result markup churns, CAPTCHAs appear, then
the IP is banned. A continuously running bot cannot rest on a source that
fails silently, so the sanctioned API is the only durable option.

Cost control matters because the account is billable beyond the free
monthly credit. Three mechanisms keep a run inside the free tier:

  * a **field mask** on every request - Places bills by the fields returned,
    so only the fields the screener actually reads are asked for;
  * a **hard request cap** per run, enforced before each call;
  * a **disk cache** keyed by the request, so re-running a search over the
    same area costs nothing.

Enumerating an area means tiling it: Nearby Search returns at most 20
results per call and does not paginate, so `grid_search` walks a lattice of
overlapping circles across the bounding box and de-duplicates by place id.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from ..models import Prospect
from .base import Source, looks_like_chain

log = logging.getLogger(__name__)

SEARCH_NEARBY = "https://places.googleapis.com/v1/places:searchNearby"
SEARCH_TEXT = "https://places.googleapis.com/v1/places:searchText"
GEOCODE = "https://maps.googleapis.com/maps/api/geocode/json"

# Only the fields the screener and outreach actually use. Every extra field
# moves the request into a more expensive SKU, so this list is the budget.
FIELD_MASK = ",".join(
    [
        "places.id",
        "places.displayName",
        "places.formattedAddress",
        "places.addressComponents",
        "places.nationalPhoneNumber",
        "places.websiteUri",
        "places.businessStatus",
        "places.userRatingCount",
        "places.rating",
        "places.primaryType",
        "places.types",
        "places.location",
    ]
)

#: Industry name -> Google place types worth querying for it.
INDUSTRY_TYPES: Dict[str, List[str]] = {
    "hvac": ["hvac_contractor"],
    "plumbing": ["plumber"],
    "home services": [
        "electrician", "roofing_contractor", "painter", "general_contractor",
        "handyman", "locksmith",
    ],
    "landscaping": ["landscaper", "lawn_care_service"],
    "commercial cleaning": ["cleaning_service", "janitorial_service"],
    "accounting": ["accounting"],
    "insurance agency": ["insurance_agency"],
    "managed it services": ["computer_repair_service", "it_services"],
    "auto repair": ["car_repair", "auto_parts_store"],
    "pest control": ["pest_control_service"],
    "laundromat": ["laundry"],
    "self storage": ["storage"],
    "restaurant": ["restaurant"],
    "retail": ["store"],
    "trucking": ["moving_company", "courier_service"],
    "manufacturing": ["general_contractor"],
}

class RequestBudgetExceeded(RuntimeError):
    """Raised when a run hits its cap on billable Places requests."""


def _km_to_deg_lat(km: float) -> float:
    return km / 110.574


def _km_to_deg_lon(km: float, at_lat: float) -> float:
    return km / (111.320 * max(0.01, math.cos(math.radians(at_lat))))


def grid_points(
    center: Tuple[float, float], radius_km: float, step_km: float
) -> List[Tuple[float, float]]:
    """Lattice of query centres covering a circle of `radius_km`.

    Steps are slightly less than the search diameter so adjacent circles
    overlap and nothing falls between them.
    """
    lat0, lon0 = center
    points: List[Tuple[float, float]] = []
    steps = max(0, int(math.ceil(radius_km / step_km)))
    for i in range(-steps, steps + 1):
        for j in range(-steps, steps + 1):
            dlat = i * _km_to_deg_lat(step_km)
            lat = lat0 + dlat
            dlon = j * _km_to_deg_lon(step_km, lat)
            lon = lon0 + dlon
            # Keep the lattice inside the requested circle.
            north = (lat - lat0) * 110.574
            east = (lon - lon0) * 111.320 * math.cos(math.radians(lat))
            if math.hypot(north, east) <= radius_km:
                points.append((round(lat, 6), round(lon, 6)))
    return points or [center]


def _post_json(url: str, payload: dict, headers: Dict[str, str], timeout: float = 30.0) -> dict:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return json.loads(response.read())


def _get_json(url: str, params: dict, timeout: float = 30.0) -> dict:
    full = f"{url}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(full, headers={"User-Agent": "business-analyst-bot/0.1"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return json.loads(response.read())


@dataclass
class GooglePlacesSource(Source):
    """Pull local businesses from Google Places.

    `fetch` runs the configured industries over a tiled area. Pass
    `transport` to inject a fake in tests; by default it posts to Google.
    """

    api_key: str
    center: Optional[Tuple[float, float]] = None
    area: str = ""
    radius_km: float = 8.0
    step_km: float = 2.5
    industries: Sequence[str] = ()
    name: str = "google-places"
    free: bool = False               # billable beyond the free monthly credit
    max_requests: int = 120          # hard cap per run
    cache_dir: Optional[Path] = None
    min_interval: float = 0.05       # politeness between calls
    include_chains: bool = False
    transport: Optional[Callable[[str, dict, dict], dict]] = None
    requests_made: int = field(default=0, init=False)
    _last_call: float = field(default=0.0, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.cache_dir:
            self.cache_dir = Path(self.cache_dir)
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        if not self.api_key:
            raise ValueError(
                "Google Places needs an API key. Set GOOGLE_PLACES_API_KEY, or use "
                "--source for a CSV of businesses you already have."
            )

    # ---- plumbing -----------------------------------------------------

    def _headers(self) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": self.api_key,
            "X-Goog-FieldMask": FIELD_MASK,
        }

    def _cache_key(self, url: str, payload: dict) -> str:
        blob = json.dumps({"url": url, "payload": payload}, sort_keys=True)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]

    def _call(self, url: str, payload: dict) -> dict:
        """One billable request, through cache and budget."""
        key = self._cache_key(url, payload)
        if self.cache_dir:
            cached = self.cache_dir / f"{key}.json"
            if cached.exists():
                try:
                    return json.loads(cached.read_text())
                except (json.JSONDecodeError, OSError):
                    pass

        if self.requests_made >= self.max_requests:
            raise RequestBudgetExceeded(
                f"Hit the cap of {self.max_requests} Places requests for this run. "
                "Raise --max-requests if you meant to search a wider area."
            )

        gap = time.monotonic() - self._last_call
        if gap < self.min_interval:
            time.sleep(self.min_interval - gap)

        transport = self.transport or (lambda u, p, h: _post_json(u, p, h))
        try:
            data = transport(url, payload, self._headers())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:400]
            if exc.code in (401, 403):
                raise RuntimeError(
                    f"Google rejected the key ({exc.code}). Check the key is valid, "
                    f"that Places API (New) is enabled, and that billing is attached. {detail}"
                ) from exc
            if exc.code == 429:
                raise RuntimeError(f"Google rate limit hit. {detail}") from exc
            raise RuntimeError(f"Places request failed ({exc.code}): {detail}") from exc

        self.requests_made += 1
        self._last_call = time.monotonic()

        if self.cache_dir:
            (self.cache_dir / f"{key}.json").write_text(json.dumps(data))
        return data

    # ---- searches -----------------------------------------------------

    def resolve_area(self, area: str) -> Tuple[float, float]:
        """Geocode a place name such as 'Tampa, FL' into a centre point."""
        data = _get_json(GEOCODE, {"address": area, "key": self.api_key})
        status = data.get("status")
        if status != "OK" or not data.get("results"):
            raise RuntimeError(
                f"Could not geocode {area!r} (status {status}). Pass --center lat,lon instead."
            )
        loc = data["results"][0]["geometry"]["location"]
        return float(loc["lat"]), float(loc["lng"])

    def search_nearby(
        self, center: Tuple[float, float], radius_m: float, place_types: Sequence[str]
    ) -> List[dict]:
        payload = {
            "includedTypes": list(place_types),
            "maxResultCount": 20,
            "locationRestriction": {
                "circle": {
                    "center": {"latitude": center[0], "longitude": center[1]},
                    "radius": float(radius_m),
                }
            },
        }
        return self._call(SEARCH_NEARBY, payload).get("places", [])

    def search_text(self, query: str, max_pages: int = 3) -> List[dict]:
        """Text search, following pagination up to Google's 60-result ceiling."""
        places: List[dict] = []
        token: Optional[str] = None
        for _ in range(max_pages):
            payload: dict = {"textQuery": query, "pageSize": 20}
            if token:
                payload["pageToken"] = token
            data = self._call(SEARCH_TEXT, payload)
            places.extend(data.get("places", []))
            token = data.get("nextPageToken")
            if not token:
                break
        return places

    def grid_search(
        self, center: Tuple[float, float], place_types: Sequence[str]
    ) -> List[dict]:
        """Tile the area so a 20-result-per-call limit still enumerates it."""
        found: Dict[str, dict] = {}
        radius_m = self.step_km * 1000 * 0.75
        for point in grid_points(center, self.radius_km, self.step_km):
            try:
                for place in self.search_nearby(point, radius_m, place_types):
                    pid = place.get("id")
                    if pid:
                        found[pid] = place
            except RequestBudgetExceeded:
                log.warning(
                    "Request budget spent after %d calls; returning %d places found so far.",
                    self.requests_made, len(found),
                )
                break
        return list(found.values())

    # ---- Source interface ---------------------------------------------

    def fetch(self, limit: int = 500) -> Iterable[Prospect]:
        center = self.center
        if center is None:
            if not self.area:
                raise ValueError("Give either center=(lat, lon) or area='Tampa, FL'.")
            center = self.resolve_area(self.area)

        industries = list(self.industries) or list(INDUSTRY_TYPES)
        raw: Dict[str, dict] = {}
        for industry in industries:
            types = INDUSTRY_TYPES.get(industry)
            if not types:
                log.warning("No Google place type mapped for industry %r; skipping.", industry)
                continue
            for place in self.grid_search(center, types):
                pid = place.get("id")
                if pid and pid not in raw:
                    place["_industry"] = industry
                    raw[pid] = place
            if self.requests_made >= self.max_requests:
                break

        prospects: List[Prospect] = []
        for place in list(raw.values())[:limit]:
            prospect = place_to_prospect(place, self.name)
            if prospect is None:
                continue
            if prospect.is_chain and not self.include_chains:
                continue
            prospects.append(prospect)
        return prospects


def place_to_prospect(place: dict, source_name: str = "google-places") -> Optional[Prospect]:
    """Map one Places result onto a Prospect. None if it is not operational."""
    status = place.get("businessStatus", "OPERATIONAL")
    if status != "OPERATIONAL":
        return None

    name = (place.get("displayName") or {}).get("text", "").strip()
    if not name:
        return None

    components = {}
    for component in place.get("addressComponents", []) or []:
        for kind in component.get("types", []):
            components.setdefault(kind, component.get("shortText") or component.get("longText"))

    location = place.get("location") or {}
    types = place.get("types", []) or []

    return Prospect(
        source=source_name,
        external_id=place.get("id", name),
        name=name,
        industry=place.get("_industry") or place.get("primaryType", "") or "unclassified",
        address=place.get("formattedAddress", ""),
        city=components.get("locality") or components.get("postal_town") or "",
        state=components.get("administrative_area_level_1", ""),
        postcode=components.get("postal_code", ""),
        lat=location.get("latitude"),
        lon=location.get("longitude"),
        phone=place.get("nationalPhoneNumber", ""),
        website=place.get("websiteUri", ""),
        review_count=place.get("userRatingCount"),
        is_chain=looks_like_chain(name, " ".join(types)),
        raw={
            "primaryType": place.get("primaryType"),
            "rating": place.get("rating"),
            "types": types,
        },
    )
