"""Rank listings by how well they fit a no-cash, owner-financed acquisition.

This runs on every listing before any LLM call, so it must be cheap and
deterministic. Hard fails knock a deal out entirely; the weighted score
decides what gets the expensive analysis budget.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

from ..models import DealScore, Listing, ScoreComponent, SellerFinancing
from . import industries
from .finance import (
    StructureParams,
    breakeven_haircut,
    build_offer,
)

# Phrases that tell us about seller financing even when the field is unset.
_FINANCE_POSITIVE = re.compile(
    r"seller financ|owner financ|seller carry|will carry|owner will finance|"
    r"seller note|terms available|financing available|sba pre-?qualified",
    re.I,
)
_FINANCE_NEGATIVE = re.compile(r"cash only|no seller financ|no owner financ|all cash", re.I)

_ABSENTEE = re.compile(
    r"absentee|semi-?absentee|owner works? (?:part|limited)|manager[- ]run|"
    r"management in place|turn-?key|passive", re.I,
)
_RECURRING = re.compile(
    r"recurring|contract(?:ed|s)? (?:revenue|customers|clients)|subscription|"
    r"retainer|route|service agreement|monthly recurring|mrr", re.I,
)
_DISTRESS = re.compile(
    r"retir(?:ing|ement)|health reason|relocat|divorce|estate sale|"
    r"motivated seller|must sell|burn(?:ed|t)? out|other interests", re.I,
)
_CONCENTRATION = re.compile(
    r"one (?:major |large )?(?:client|customer)|largest (?:client|customer) "
    r"(?:is |represents |accounts for )?\d+%|customer concentration", re.I,
)


@dataclass
class ScreenConfig:
    """Hard filters applied before scoring."""

    min_sde: float = 100_000.0
    max_sde: float = 3_000_000.0
    max_asking_price: float = 5_000_000.0
    max_price_to_sde: float = 4.5
    min_established_years: int = 3
    require_cash_flow: bool = True
    blocked_industries: set = field(default_factory=lambda: set(industries.DISQUALIFIED))
    allowed_industries: Optional[set] = None
    structure: StructureParams = field(default_factory=StructureParams)

    # Score weights; normalised at use so they need not sum to 1.
    weights: dict = field(
        default_factory=lambda: {
            "seller_financing": 0.26,
            "debt_coverage": 0.24,
            "price_discipline": 0.14,
            "durability": 0.12,
            "owner_independence": 0.10,
            "industry_fit": 0.08,
            "seller_motivation": 0.06,
        }
    )


def _text(listing: Listing) -> str:
    return " ".join(
        filter(None, [listing.name, listing.description, listing.reason_for_sale, listing.industry])
    )


def infer_seller_financing(listing: Listing) -> SellerFinancing:
    """Trust the structured field; otherwise read the listing copy."""
    if listing.seller_financing != SellerFinancing.UNKNOWN:
        return listing.seller_financing
    body = _text(listing)
    if _FINANCE_NEGATIVE.search(body):
        return SellerFinancing.REFUSED
    if _FINANCE_POSITIVE.search(body):
        return SellerFinancing.OFFERED
    return SellerFinancing.UNKNOWN


def _hard_fails(listing: Listing, cfg: ScreenConfig, profile) -> List[str]:
    fails: List[str] = []
    sde = listing.sde

    if cfg.require_cash_flow and (sde is None or sde <= 0):
        fails.append("No disclosed cash flow / SDE.")
    if sde is not None and sde > 0:
        if sde < cfg.min_sde:
            fails.append(f"SDE ${sde:,.0f} below the ${cfg.min_sde:,.0f} floor.")
        if sde > cfg.max_sde:
            fails.append(f"SDE ${sde:,.0f} above the ${cfg.max_sde:,.0f} ceiling.")
    if listing.asking_price and listing.asking_price > cfg.max_asking_price:
        fails.append(
            f"Ask ${listing.asking_price:,.0f} above the "
            f"${cfg.max_asking_price:,.0f} ceiling."
        )
    if listing.price_to_sde and listing.price_to_sde > cfg.max_price_to_sde:
        fails.append(
            f"Priced at {listing.price_to_sde:.1f}x SDE, above the "
            f"{cfg.max_price_to_sde:.1f}x limit."
        )
    if profile.name in cfg.blocked_industries:
        fails.append(f"Industry '{profile.name}' is excluded from the thesis.")
    if cfg.allowed_industries is not None and profile.name not in cfg.allowed_industries:
        fails.append(f"Industry '{profile.name}' is outside the current mandate.")
    if listing.age_years is not None and listing.age_years < cfg.min_established_years:
        fails.append(
            f"Only {listing.age_years} years old; below the "
            f"{cfg.min_established_years}-year minimum."
        )
    if infer_seller_financing(listing) == SellerFinancing.REFUSED:
        fails.append("Seller has ruled out carrying paper; no cash means no deal.")
    return fails


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def score_listing(listing: Listing, cfg: Optional[ScreenConfig] = None) -> DealScore:
    """Produce an explainable 0-100 acquirability score."""
    cfg = cfg or ScreenConfig()
    profile = industries.classify(listing.industry or listing.name or listing.description)
    body = _text(listing)
    components: List[ScoreComponent] = []
    flags: List[str] = []

    hard_fails = _hard_fails(listing, cfg, profile)

    # 1. Seller financing posture - the gate on the whole strategy.
    posture = infer_seller_financing(listing)
    if posture == SellerFinancing.OFFERED:
        pct = listing.seller_financing_pct
        if pct is not None:
            fin_score = _clamp(pct)
            fin_why = f"Seller advertises carrying {pct * 100:.0f}%."
        else:
            fin_score = 0.85
            fin_why = "Seller financing advertised, percentage unstated."
    elif posture == SellerFinancing.NEGOTIABLE:
        fin_score, fin_why = 0.6, "Seller open to discussing terms."
    elif posture == SellerFinancing.REFUSED:
        fin_score, fin_why = 0.0, "Seller wants all cash."
    else:
        fin_score, fin_why = 0.3, "Financing posture unknown; must be asked directly."
        flags.append("Ask the broker about seller carry before spending more time.")
    components.append(
        ScoreComponent("seller_financing", fin_score, cfg.weights["seller_financing"], fin_why)
    )

    # 2. Debt coverage - can the business actually carry itself?
    offer = build_offer(listing, cfg.structure)
    if offer.viable:
        cushion = breakeven_haircut(listing, cfg.structure, offer=offer)
        # 25% of SDE can vanish and still cover -> full marks.
        cov_score = _clamp(cushion / 0.25)
        cov_why = (
            f"{offer.dscr:.2f}x DSCR at ${offer.purchase_price:,.0f}; "
            f"survives a {cushion * 100:.0f}% SDE haircut."
        )
        if cushion < 0.10:
            flags.append(
                f"Thin cushion: a {cushion * 100:.0f}% earnings overstatement breaks the note."
            )
    else:
        cov_score = 0.0
        cov_why = offer.notes_on_viability
    components.append(
        ScoreComponent("debt_coverage", cov_score, cfg.weights["debt_coverage"], cov_why)
    )

    # 3. Price discipline versus the industry's normal multiple.
    multiple = listing.price_to_sde
    if multiple is None:
        price_score, price_why = 0.3, "Cannot compute a multiple from the disclosed figures."
    elif multiple <= profile.typical_sde_multiple_low:
        price_score = 1.0
        price_why = f"{multiple:.1f}x is at or below the {profile.name} floor."
    elif multiple >= profile.typical_sde_multiple_high:
        price_score = 0.15
        price_why = (
            f"{multiple:.1f}x exceeds the {profile.typical_sde_multiple_high:.1f}x "
            f"top of the {profile.name} range."
        )
    else:
        span = profile.typical_sde_multiple_high - profile.typical_sde_multiple_low
        price_score = _clamp(1 - (multiple - profile.typical_sde_multiple_low) / span)
        price_why = (
            f"{multiple:.1f}x sits inside the {profile.typical_sde_multiple_low:.1f}-"
            f"{profile.typical_sde_multiple_high:.1f}x {profile.name} range."
        )
    components.append(
        ScoreComponent("price_discipline", price_score, cfg.weights["price_discipline"], price_why)
    )

    # 4. Durability - age plus recurring revenue.
    dur = 0.0
    reasons = []
    age = listing.age_years
    if age is not None:
        dur += _clamp(age / 15) * 0.6
        reasons.append(f"{age} years trading")
    else:
        dur += 0.2
        reasons.append("age unknown")
    if _RECURRING.search(body) or profile.recurring_revenue:
        dur += 0.4
        reasons.append("recurring revenue")
    else:
        reasons.append("no recurring revenue signal")
    components.append(
        ScoreComponent("durability", _clamp(dur), cfg.weights["durability"], ", ".join(reasons))
    )

    # 5. Owner independence - can it run without the seller from day one?
    hours = listing.owner_hours_per_week
    if _ABSENTEE.search(body):
        own_score, own_why = 0.95, "Listing describes absentee or manager-run operations."
    elif hours is not None:
        own_score = _clamp(1 - (hours / 60))
        own_why = f"Owner works {hours:.0f} hours/week."
        if hours >= 45:
            flags.append("Owner-operator dependency: budget for a manager in the model.")
    elif profile.owner_dependence == "low":
        own_score, own_why = 0.7, f"{profile.name} is typically manager-run."
    elif profile.owner_dependence == "high":
        own_score, own_why = 0.25, f"{profile.name} usually depends heavily on the owner."
        flags.append("High owner dependence typical for this industry; verify who sells.")
    else:
        own_score, own_why = 0.45, "Owner involvement not disclosed."
    components.append(
        ScoreComponent("owner_independence", own_score, cfg.weights["owner_independence"], own_why)
    )

    # 6. Industry fit for the strategy itself.
    ind_score = 0.0
    ind_bits = []
    if profile.seller_finance_friendly:
        ind_score += 0.5
        ind_bits.append("sellers commonly carry")
    if profile.capital_intensity == "low":
        ind_score += 0.3
        ind_bits.append("low capex")
    elif profile.capital_intensity == "medium":
        ind_score += 0.15
        ind_bits.append("moderate capex")
    else:
        ind_bits.append("capital hungry")
    if profile.recurring_revenue:
        ind_score += 0.2
        ind_bits.append("contracted revenue")
    if profile.name == "unclassified":
        ind_score = 0.35
        ind_bits = ["industry not recognised"]
    components.append(
        ScoreComponent(
            "industry_fit", _clamp(ind_score), cfg.weights["industry_fit"],
            f"{profile.name}: " + ", ".join(ind_bits),
        )
    )

    # 7. Seller motivation - a motivated seller is what makes terms possible.
    if _DISTRESS.search(body):
        mot_score, mot_why = 0.9, "Stated reason for sale suggests genuine motivation."
    elif listing.days_on_market and listing.days_on_market > 180:
        mot_score = 0.75
        mot_why = f"On market {listing.days_on_market} days; the ask has not cleared."
    elif listing.days_on_market and listing.days_on_market < 30:
        mot_score, mot_why = 0.3, "Freshly listed; seller expectations still anchored high."
    else:
        mot_score, mot_why = 0.45, "No strong motivation signal."
    components.append(
        ScoreComponent("seller_motivation", mot_score, cfg.weights["seller_motivation"], mot_why)
    )

    if _CONCENTRATION.search(body):
        flags.append("Customer concentration language in the listing; diligence it early.")
    if listing.real_estate_included:
        flags.append(
            "Real estate is included; consider splitting it into a lease to cut the price."
        )
    if listing.inventory and listing.asking_price and listing.inventory > 0.3 * listing.asking_price:
        flags.append("Inventory is a large share of the ask; verify it and value it separately.")

    weight_total = sum(c.weight for c in components) or 1.0
    total = sum(c.contribution for c in components) / weight_total * 100

    return DealScore(
        total=round(total, 2), components=components, hard_fails=hard_fails, flags=flags
    )


def rank(listings, cfg: Optional[ScreenConfig] = None):
    """Score every listing and return (listing, score) best-first, passes only."""
    cfg = cfg or ScreenConfig()
    scored = [(l, score_listing(l, cfg)) for l in listings]
    passing = [(l, s) for l, s in scored if s.passed]
    return sorted(passing, key=lambda pair: -pair[1].total)
