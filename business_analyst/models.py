"""Core domain objects for the business analyst bot.

Everything the pipeline passes around is a plain dataclass so it can be
serialised to JSON for the SQLite store without a schema library.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DealStage(str, Enum):
    """Where a listing sits in the funnel."""

    DISCOVERED = "discovered"
    SCREENED = "screened"
    REJECTED = "rejected"
    ANALYZED = "analyzed"
    OFFER_DRAFTED = "offer_drafted"


class SellerFinancing(str, Enum):
    """What the listing says about seller/owner financing."""

    OFFERED = "offered"          # explicitly advertised
    NEGOTIABLE = "negotiable"    # "will consider", "possible"
    UNKNOWN = "unknown"          # not mentioned
    REFUSED = "refused"          # "cash only", "no seller financing"


def _as_jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {k: _as_jsonable(v) for k, v in dataclasses.asdict(value).items()}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _as_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_as_jsonable(v) for v in value]
    return value


@dataclass
class _JsonMixin:
    def to_dict(self) -> Dict[str, Any]:
        return {k: _as_jsonable(v) for k, v in dataclasses.asdict(self).items()}

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "_JsonMixin":
        fields = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in payload.items() if k in fields})  # type: ignore[arg-type]


@dataclass
class Listing(_JsonMixin):
    """A business for sale, normalised across every source."""

    source: str
    external_id: str
    name: str
    url: str = ""
    industry: str = "unclassified"
    location: str = ""
    description: str = ""

    asking_price: Optional[float] = None
    revenue: Optional[float] = None
    cash_flow: Optional[float] = None          # SDE as advertised
    ebitda: Optional[float] = None
    inventory: Optional[float] = None
    real_estate_included: bool = False
    ffe: Optional[float] = None                # furniture, fixtures & equipment

    established_year: Optional[int] = None
    employees: Optional[int] = None
    owner_hours_per_week: Optional[float] = None
    reason_for_sale: str = ""

    seller_financing: SellerFinancing = SellerFinancing.UNKNOWN
    seller_financing_pct: Optional[float] = None   # 0..1 if advertised
    days_on_market: Optional[int] = None

    raw: Dict[str, Any] = field(default_factory=dict)
    discovered_at: str = field(default_factory=_utcnow)

    def __post_init__(self) -> None:
        if isinstance(self.seller_financing, str):
            self.seller_financing = SellerFinancing(self.seller_financing)

    @property
    def deal_id(self) -> str:
        """Stable identity used for dedupe across runs and across sources."""
        basis = f"{self.source}:{self.external_id}".lower()
        return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]

    @property
    def fingerprint(self) -> str:
        """Fuzzy identity so the same business listed twice collapses to one deal."""
        name = "".join(ch for ch in self.name.lower() if ch.isalnum())
        loc = "".join(ch for ch in self.location.lower() if ch.isalnum())
        price = int(self.asking_price // 1000) if self.asking_price else 0
        return hashlib.sha1(f"{name}|{loc}|{price}".encode("utf-8")).hexdigest()[:16]

    @property
    def sde(self) -> Optional[float]:
        """Seller's discretionary earnings, best available estimate."""
        if self.cash_flow is not None:
            return self.cash_flow
        return self.ebitda

    @property
    def price_to_sde(self) -> Optional[float]:
        if not self.asking_price or not self.sde or self.sde <= 0:
            return None
        return self.asking_price / self.sde

    @property
    def age_years(self) -> Optional[int]:
        if not self.established_year:
            return None
        return max(0, date.today().year - self.established_year)


@dataclass
class ScoreComponent(_JsonMixin):
    """One weighted line item in the acquirability score."""

    name: str
    score: float        # 0..1
    weight: float
    rationale: str

    @property
    def contribution(self) -> float:
        return self.score * self.weight


@dataclass
class DealScore(_JsonMixin):
    """How well a listing fits a no-money-down, owner-financed acquisition."""

    total: float                                  # 0..100
    components: List[ScoreComponent] = field(default_factory=list)
    hard_fails: List[str] = field(default_factory=list)
    flags: List[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.hard_fails

    def explain(self) -> str:
        lines = [f"Acquirability score: {self.total:.1f}/100"]
        for c in sorted(self.components, key=lambda c: -c.contribution):
            lines.append(
                f"  {c.name:<26} {c.score:>5.2f} x {c.weight:>4.2f} "
                f"= {c.contribution * 100:>5.1f}  {c.rationale}"
            )
        for f in self.hard_fails:
            lines.append(f"  HARD FAIL: {f}")
        for f in self.flags:
            lines.append(f"  flag: {f}")
        return "\n".join(lines)


@dataclass
class NoteTerms(_JsonMixin):
    """Terms of a single promissory note in the capital stack."""

    principal: float
    annual_rate: float
    term_years: int
    interest_only_months: int = 0
    standby_months: int = 0        # months of no payments at all
    balloon_months: Optional[int] = None
    label: str = "seller note"


@dataclass
class OfferStructure(_JsonMixin):
    """A concrete no-cash-out-of-pocket offer."""

    purchase_price: float
    cash_at_close: float
    notes: List[NoteTerms] = field(default_factory=list)
    earnout_pct_of_sde: float = 0.0
    earnout_years: int = 0
    holdback: float = 0.0
    annual_debt_service: float = 0.0
    year_one_debt_service: float = 0.0
    dscr: float = 0.0
    year_one_dscr: float = 0.0
    free_cash_after_debt: float = 0.0
    buyer_salary: float = 0.0
    assumptions: List[str] = field(default_factory=list)
    viable: bool = False
    notes_on_viability: str = ""


@dataclass
class AnalysisSection(_JsonMixin):
    """Output of one of the eight analyst playbooks."""

    key: str
    title: str
    content: str
    model: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    generated_at: str = field(default_factory=_utcnow)


@dataclass
class DealReport(_JsonMixin):
    """Everything the bot knows about one opportunity."""

    listing: Listing
    score: Optional[DealScore] = None
    offer: Optional[OfferStructure] = None
    sections: List[AnalysisSection] = field(default_factory=list)
    stage: DealStage = DealStage.DISCOVERED
    updated_at: str = field(default_factory=_utcnow)

    def section(self, key: str) -> Optional[AnalysisSection]:
        for s in self.sections:
            if s.key == key:
                return s
        return None

    def to_dict(self) -> Dict[str, Any]:
        payload = super().to_dict()
        payload["deal_id"] = self.listing.deal_id
        return payload


@dataclass
class IndustryProfile(_JsonMixin):
    """Screening knowledge about an industry, used before any LLM call."""

    name: str
    typical_sde_multiple_low: float
    typical_sde_multiple_high: float
    seller_finance_friendly: bool
    recurring_revenue: bool
    capital_intensity: str           # low | medium | high
    owner_dependence: str            # low | medium | high
    notes: str = ""

    @property
    def mid_multiple(self) -> float:
        return (self.typical_sde_multiple_low + self.typical_sde_multiple_high) / 2
