"""Screening for off-market prospects.

A listed business discloses a price and an SDE. A local business that is
not for sale discloses nothing, so this module does two jobs the listing
screener never has to: it *estimates* size from headcount and industry
priors, and it scores succession pressure - the likelihood that an owner
would entertain an approach at all, and would carry paper if they did.

Every number produced here is an estimate and is labelled as one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

from ..models import DealScore, Estimate, Prospect, ScoreComponent
from . import industries
from .finance import StructureParams, max_supportable_price

# Employee count is rarely published. Review volume is a weak but real proxy
# for how much business a local firm does, so it is used only to separate a
# one-van operator from a real company, never as a financial input.
_REVIEWS_TO_STAFF = ((0, 2), (25, 4), (75, 8), (200, 15), (500, 30))

_SUCCESSION_HINTS = re.compile(
    r"family[- ]owned|since \d{4}|serving .* since|second generation|"
    r"third generation|established \d{4}|over \d+ years",
    re.I,
)


def estimate_staff(prospect: Prospect) -> Optional[int]:
    """Headcount if known, else a coarse band from review volume."""
    if prospect.employees is not None:
        return prospect.employees
    if prospect.review_count is None:
        return None
    staff = 2
    for threshold, count in _REVIEWS_TO_STAFF:
        if prospect.review_count >= threshold:
            staff = count
    return staff


def estimate_financials(prospect: Prospect) -> Estimate:
    """Band a prospect by size using industry priors. Never a valuation."""
    profile = industries.classify(prospect.industry or prospect.name)
    staff = estimate_staff(prospect)
    if staff is None:
        return Estimate(
            basis="No headcount or review volume; size unknown until the owner talks.",
            confidence="low",
        )

    revenue = staff * profile.revenue_per_employee
    sde = revenue * profile.sde_margin
    return Estimate(
        revenue=round(revenue, -3),
        sde=round(sde, -3),
        indicative_price_low=round(sde * profile.typical_sde_multiple_low, -3),
        indicative_price_high=round(sde * profile.typical_sde_multiple_high, -3),
        basis=(
            f"~{staff} staff "
            f"{'(reported)' if prospect.employees is not None else '(inferred from review volume)'} "
            f"x ${profile.revenue_per_employee:,.0f} revenue/employee for {profile.name}, "
            f"at a {profile.sde_margin:.0%} SDE margin."
        ),
        confidence="medium" if prospect.employees is not None else "low",
    )


@dataclass
class ProspectConfig:
    """Filters and weights for off-market screening."""

    structure: StructureParams = field(default_factory=StructureParams)
    require_contact: bool = True
    exclude_chains: bool = True
    min_estimated_sde: Optional[float] = None    # defaults to the operator salary
    min_age_years: int = 10
    blocked_industries: set = field(default_factory=lambda: set(industries.DISQUALIFIED))
    allowed_industries: Optional[set] = None

    weights: dict = field(
        default_factory=lambda: {
            "succession_pressure": 0.28,
            "industry_fit": 0.22,
            "size_fit": 0.20,
            "independence": 0.15,
            "contactability": 0.15,
        }
    )

    def sde_floor(self) -> float:
        if self.min_estimated_sde is not None:
            return self.min_estimated_sde
        return self.structure.buyer_salary


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def score_prospect(
    prospect: Prospect,
    cfg: Optional[ProspectConfig] = None,
    estimate: Optional[Estimate] = None,
) -> DealScore:
    """Rank an off-market business as an owner-finance approach target."""
    cfg = cfg or ProspectConfig()
    profile = industries.classify(prospect.industry or prospect.name)
    estimate = estimate or estimate_financials(prospect)
    components: List[ScoreComponent] = []
    flags: List[str] = []
    hard_fails: List[str] = []

    # --- hard filters ---
    if cfg.exclude_chains and prospect.is_chain:
        hard_fails.append("Looks like a chain or franchise outlet; there is no owner to sell.")
    if cfg.require_contact and not prospect.has_contact:
        hard_fails.append("No phone, email or address; cannot be approached.")
    if profile.name in cfg.blocked_industries:
        hard_fails.append(f"Industry '{profile.name}' is excluded from the thesis.")
    if cfg.allowed_industries is not None and profile.name not in cfg.allowed_industries:
        hard_fails.append(f"Industry '{profile.name}' is outside the current mandate.")
    if prospect.age_years is not None and prospect.age_years < cfg.min_age_years:
        hard_fails.append(
            f"Only {prospect.age_years} years old; below the {cfg.min_age_years}-year minimum."
        )

    floor = cfg.sde_floor()
    if estimate.sde is not None and estimate.sde < floor:
        hard_fails.append(
            f"Estimated SDE of ${estimate.sde:,.0f} is below the ${floor:,.0f} "
            "operator salary; too small to carry a buyer."
        )

    # 1. Succession pressure - the reason an unlisted owner picks up the phone.
    age = prospect.age_years
    pressure = 0.0
    why: List[str] = []
    if age is not None:
        # A business trading 25+ years usually has an owner near retirement.
        pressure += _clamp(age / 30) * 0.7
        why.append(f"{age} years trading")
    else:
        pressure += 0.25
        why.append("age unknown")
    if _SUCCESSION_HINTS.search(f"{prospect.name} {prospect.notes}"):
        pressure += 0.2
        why.append("long-tenure language")
    if not prospect.website:
        # No web presence usually means an older owner and, importantly,
        # far fewer competing buyers looking at the same business.
        pressure += 0.15
        why.append("no website")
    components.append(
        ScoreComponent(
            "succession_pressure", _clamp(pressure),
            cfg.weights["succession_pressure"], ", ".join(why),
        )
    )

    # 2. Industry fit for an owner-financed deal.
    fit = 0.0
    bits: List[str] = []
    if profile.seller_finance_friendly:
        fit += 0.45
        bits.append("sellers commonly carry")
    if profile.capital_intensity == "low":
        fit += 0.3
        bits.append("low capex")
    elif profile.capital_intensity == "medium":
        fit += 0.15
        bits.append("moderate capex")
    else:
        bits.append("capital hungry")
    if profile.recurring_revenue:
        fit += 0.25
        bits.append("recurring revenue")
    if profile.name == "unclassified":
        fit, bits = 0.3, ["industry not recognised"]
    components.append(
        ScoreComponent(
            "industry_fit", _clamp(fit), cfg.weights["industry_fit"],
            f"{profile.name}: " + ", ".join(bits),
        )
    )

    # 3. Size fit - big enough to pay an operator, small enough to be ignored
    #    by private equity and strategic buyers.
    if estimate.sde is None:
        size_score, size_why = 0.35, "Size unknown; confirm headcount on the first call."
    else:
        ceiling = max_supportable_price(
            estimate.sde - cfg.structure.buyer_salary, cfg.structure
        )
        if estimate.sde < floor:
            size_score = 0.0
            size_why = f"Estimated SDE ${estimate.sde:,.0f} cannot cover the operator."
        else:
            headroom = estimate.sde - floor
            # $400k of SDE above the salary is a comfortable full mark.
            size_score = _clamp(headroom / 400_000)
            size_why = (
                f"Estimated SDE ${estimate.sde:,.0f} supports roughly "
                f"${ceiling:,.0f} at {cfg.structure.target_dscr:.2f}x DSCR (estimate)."
            )
            if estimate.sde > 2_000_000:
                flags.append("Large enough to attract private equity; expect competition.")
    components.append(
        ScoreComponent("size_fit", size_score, cfg.weights["size_fit"], size_why)
    )

    # 4. Independence - an owner-operated firm is one that can actually be bought.
    if prospect.is_chain:
        ind_score, ind_why = 0.0, "Chain or franchise branding."
    elif prospect.review_count is not None and prospect.review_count < 300:
        ind_score, ind_why = 0.9, "Single-location independent."
    else:
        ind_score, ind_why = 0.6, "Independent, but large or multi-site."
    components.append(
        ScoreComponent("independence", ind_score, cfg.weights["independence"], ind_why)
    )

    # 5. Contactability - what the outreach agent has to work with.
    channels = [c for c, present in
                (("email", prospect.email), ("phone", prospect.phone),
                 ("website", prospect.website), ("mail", prospect.address)) if present]
    if prospect.email:
        contact_score = 1.0
    elif prospect.website and prospect.phone:
        contact_score = 0.7
    elif prospect.phone:
        contact_score = 0.5
    elif prospect.address:
        contact_score = 0.3
    else:
        contact_score = 0.0
    components.append(
        ScoreComponent(
            "contactability", contact_score, cfg.weights["contactability"],
            ("reachable by " + ", ".join(channels)) if channels else "no contact route",
        )
    )

    if not prospect.email:
        flags.append(
            "No email address: needs enrichment before a cold-email sequence can run."
        )
    if estimate.confidence == "low":
        flags.append("Size is a rough estimate; verify headcount and revenue early.")

    weight_total = sum(c.weight for c in components) or 1.0
    total = sum(c.contribution for c in components) / weight_total * 100

    return DealScore(
        total=round(total, 2), components=components, hard_fails=hard_fails, flags=flags
    )


def rank_prospects(prospects, cfg: Optional[ProspectConfig] = None):
    """Score prospects, keep the passes, best first."""
    cfg = cfg or ProspectConfig()
    scored = []
    for prospect in prospects:
        estimate = estimate_financials(prospect)
        score = score_prospect(prospect, cfg, estimate)
        if score.passed:
            scored.append((prospect, estimate, score))
    return sorted(scored, key=lambda row: -row[2].total)
