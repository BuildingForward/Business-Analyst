"""Industry knowledge used to screen before spending a token on an LLM call.

Multiples are trailing SDE multiples for lower-middle-market US small
businesses. They are deliberately conservative: they exist to rank and to
sanity-check an asking price, not to produce a defensible valuation.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional

from ..models import IndustryProfile

_PROFILES: List[IndustryProfile] = [
    IndustryProfile(
        "home services", 2.0, 3.5, True, False, "medium", "high",
        "Fragmented, ageing owners, recurring maintenance contracts are the prize. "
        "Owner often *is* the sales function - check that first.",
    ),
    IndustryProfile(
        "hvac", 2.5, 4.0, True, True, "medium", "medium",
        "Service agreements produce genuine recurring revenue. Technician "
        "retention is the binding constraint, not demand.",
    ),
    IndustryProfile(
        "plumbing", 2.0, 3.5, True, False, "medium", "medium",
        "Licence transfer is a real closing risk; confirm the master licence "
        "holder stays or is replaceable.",
    ),
    IndustryProfile(
        "landscaping", 1.5, 3.0, True, True, "medium", "medium",
        "Seasonal working capital swings. Commercial contracts are worth "
        "multiples of residential route revenue.",
    ),
    IndustryProfile(
        "commercial cleaning", 2.0, 3.5, True, True, "low", "low",
        "Contracted, low-capex, absentee-friendly. The classic owner-financed "
        "target. Watch customer concentration.",
    ),
    IndustryProfile(
        "accounting", 2.5, 4.5, True, True, "low", "high",
        "Client relationships follow the partner. Insist on a long transition "
        "and a retention-based earnout.",
    ),
    IndustryProfile(
        "insurance agency", 3.0, 5.0, True, True, "low", "medium",
        "Renewal commissions are the most financeable cash flow in small "
        "business. Carrier appointments must be assignable.",
    ),
    IndustryProfile(
        "managed it services", 3.0, 5.0, True, True, "low", "medium",
        "MRR contracts underwrite well. Verify contract assignability and "
        "churn, not headline revenue.",
    ),
    IndustryProfile(
        "staffing", 2.0, 3.5, False, False, "high", "medium",
        "Payroll funding eats cash; a standby note rarely covers the gap.",
    ),
    IndustryProfile(
        "manufacturing", 3.0, 4.5, True, False, "high", "medium",
        "Capex and machine obsolescence are the hidden liabilities. Get an "
        "equipment condition report before pricing.",
    ),
    IndustryProfile(
        "e-commerce", 2.0, 3.5, False, False, "medium", "medium",
        "Platform and ad-account dependency make cash flow fragile. Sellers "
        "rarely carry paper.",
    ),
    IndustryProfile(
        "saas", 3.5, 6.0, False, True, "low", "low",
        "Rarely owner-financed and rarely priced off SDE at this size.",
    ),
    IndustryProfile(
        "restaurant", 1.5, 2.5, True, False, "high", "high",
        "Thin margins, lease risk, high failure rate. Avoid unless the real "
        "estate or the lease itself is the asset.",
    ),
    IndustryProfile(
        "retail", 1.5, 3.0, True, False, "medium", "medium",
        "Inventory is a large share of the price and is often overstated.",
    ),
    IndustryProfile(
        "laundromat", 2.5, 4.0, True, True, "high", "low",
        "Genuinely absentee, but the equipment and the lease are the deal.",
    ),
    IndustryProfile(
        "self storage", 4.0, 6.0, True, True, "high", "low",
        "Priced off cap rate, not SDE. Real-estate financing rules apply.",
    ),
    IndustryProfile(
        "trucking", 2.0, 3.0, True, False, "high", "medium",
        "Equipment notes usually already encumber the assets; check payoffs.",
    ),
    IndustryProfile(
        "medical practice", 2.5, 4.5, False, True, "medium", "high",
        "Licensure usually blocks a non-clinical buyer outright.",
    ),
    IndustryProfile(
        "auto repair", 2.0, 3.5, True, False, "medium", "medium",
        "Property lease and technician retention drive value.",
    ),
    IndustryProfile(
        "pest control", 3.0, 4.5, True, True, "low", "low",
        "Recurring routes are prime owner-finance collateral.",
    ),
]

_ALIASES: Dict[str, str] = {
    "heating": "hvac", "air conditioning": "hvac", "hvacr": "hvac", "heating and cooling": "hvac",
    "janitorial": "commercial cleaning", "cleaning": "commercial cleaning",
    "maid": "commercial cleaning", "custodial": "commercial cleaning",
    "bookkeeping": "accounting", "cpa": "accounting", "tax practice": "accounting",
    "msp": "managed it services", "it services": "managed it services",
    "it support": "managed it services", "managed services": "managed it services",
    "lawn": "landscaping", "lawn care": "landscaping", "tree service": "landscaping",
    "insurance": "insurance agency", "insurance brokerage": "insurance agency",
    "software": "saas", "b2b software": "saas",
    "ecommerce": "e-commerce", "online store": "e-commerce", "amazon fba": "e-commerce",
    "food service": "restaurant", "cafe": "restaurant", "bar": "restaurant",
    "pizzeria": "restaurant", "coffee shop": "restaurant",
    "storage": "self storage", "mini storage": "self storage",
    "freight": "trucking", "logistics": "trucking", "hauling": "trucking",
    "dental": "medical practice", "dental practice": "medical practice",
    "veterinary": "medical practice", "clinic": "medical practice",
    "auto body": "auto repair", "mechanic": "auto repair",
    "roofing": "home services", "electrical": "home services",
    "handyman": "home services", "painting": "home services",
    "garage door": "home services", "pool service": "home services",
    "exterminator": "pest control",
    "laundry": "laundromat", "coin laundry": "laundromat",
}

_BY_NAME: Dict[str, IndustryProfile] = {p.name: p for p in _PROFILES}

# Industries the no-money-down thesis simply does not work in.
DISQUALIFIED = {"medical practice"}

DEFAULT_PROFILE = IndustryProfile(
    "unclassified", 2.0, 3.5, False, False, "medium", "medium",
    "No industry profile matched; treat multiples as a rough prior only.",
)


def _normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower()).strip()


def classify(text: str) -> IndustryProfile:
    """Map free-text industry or listing copy onto a known profile.

    Matches the longest alias or profile name found, so "commercial cleaning"
    wins over a bare "cleaning" appearing in the same sentence.
    """
    haystack = _normalise(text)
    if not haystack:
        return DEFAULT_PROFILE

    best: Optional[str] = None
    best_len = 0
    candidates = list(_BY_NAME.keys()) + list(_ALIASES.keys())
    for term in candidates:
        if len(term) <= best_len:
            continue
        if re.search(rf"\b{re.escape(term)}\b", haystack):
            best = term
            best_len = len(term)

    if best is None:
        return DEFAULT_PROFILE
    canonical = _ALIASES.get(best, best)
    return _BY_NAME.get(canonical, DEFAULT_PROFILE)


def all_profiles() -> List[IndustryProfile]:
    return list(_PROFILES)


def get(name: str) -> Optional[IndustryProfile]:
    return _BY_NAME.get(_normalise(name))
