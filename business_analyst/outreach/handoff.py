"""Handoff to the cold-email agent.

This bot qualifies and briefs; a separate agent writes and sends. The
contract between them is this export: a stable, machine-readable record per
qualified prospect carrying everything the writing agent needs and nothing
it has to guess at.

Two fields exist specifically to stop the downstream agent writing something
false. `estimates` is explicitly namespaced so no figure in it can be
mistaken for a disclosed fact, and `do_not_claim` states outright what must
never be asserted in an email. An agent told only "revenue: 1440000" will
write "I see you're doing $1.4M" to an owner who never said any such thing,
which ends the conversation and can be defamatory about their business.
"""

from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from ..models import DealScore, Estimate, Prospect, ProspectReport
from ..screening import industries

SCHEMA_VERSION = "1.0"

#: Emitted on every record. The writing agent must honour these.
STANDING_CONSTRAINTS = [
    "Do not state revenue, profit, SDE or employee count as fact. Every figure "
    "in `estimates` is modelled from industry averages, not disclosed by the owner.",
    "Do not claim or imply the business is for sale, listed, or that the owner "
    "has expressed interest. This is an unsolicited approach to an unlisted business.",
    "Do not name a price, a multiple or an offer in a first touch.",
    "Do not claim a prior relationship, referral or conversation that did not happen.",
]


def _email_status(prospect: Prospect) -> str:
    if prospect.email:
        return "present"
    if prospect.website:
        return "missing_enrichable_from_website"
    return "missing_no_route"


def personalization_hooks(
    prospect: Prospect, estimate: Optional[Estimate] = None
) -> List[str]:
    """Verifiable specifics the writing agent can open with.

    Only facts that came from the source, so nothing here can be a
    fabrication. Anything modelled stays out of this list by design.
    """
    hooks: List[str] = []
    if prospect.age_years:
        hooks.append(
            f"Trading {prospect.age_years} years (since {prospect.established_year})"
        )
    if prospect.city:
        hooks.append(f"Local to {prospect.location}")
    if prospect.review_count:
        hooks.append(f"{prospect.review_count} public reviews")
    if not prospect.website:
        hooks.append("No website - likely does little online marketing")
    profile = industries.classify(prospect.industry or prospect.name)
    if profile.name != "unclassified":
        hooks.append(f"Operates in {profile.name}")
    return hooks


def suggested_angle(prospect: Prospect, score: Optional[DealScore] = None) -> str:
    """One line on how to open with this specific owner."""
    profile = industries.classify(prospect.industry or prospect.name)
    age = prospect.age_years or 0

    if age >= 25:
        return (
            "Lead with succession: a long-held local business and a direct, private "
            "approach about what happens to it next. No broker, no listing."
        )
    if profile.recurring_revenue:
        return (
            "Lead with continuity: the contracted customer base is the asset, and a "
            "sale that keeps staff and service in place protects it."
        )
    if not prospect.website:
        return (
            "Lead with a low-key, personal note - this owner is not marketing online "
            "and will not respond to anything that reads like a mass mailing."
        )
    return (
        "Lead with a straightforward buyer introduction: local, serious, discreet, "
        "and asking for a conversation rather than making a proposal."
    )


@dataclass
class OutreachBrief:
    """One qualified prospect, packaged for the writing agent."""

    prospect_id: str
    name: str
    industry: str
    contact: Dict[str, Any]
    qualification: Dict[str, Any]
    estimates: Dict[str, Any]
    hooks: List[str]
    angle: str
    do_not_claim: List[str] = field(default_factory=lambda: list(STANDING_CONSTRAINTS))
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def build_brief(report: ProspectReport) -> OutreachBrief:
    """Package one scored prospect for handoff."""
    prospect = report.prospect
    score = report.score
    estimate = report.estimate or Estimate()

    reasons = []
    if score:
        for component in sorted(score.components, key=lambda c: -c.contribution)[:3]:
            reasons.append(f"{component.name}: {component.rationale}")

    return OutreachBrief(
        prospect_id=prospect.prospect_id,
        name=prospect.name,
        industry=industries.classify(prospect.industry or prospect.name).name,
        contact={
            "phone": prospect.phone,
            "email": prospect.email,
            "email_status": _email_status(prospect),
            "website": prospect.website,
            "address": prospect.address,
            "city": prospect.city,
            "state": prospect.state,
            "owner_name": prospect.owner_name or None,
        },
        qualification={
            "score": score.total if score else None,
            "why": reasons,
            "flags": list(score.flags) if score else [],
            "status": report.status.value,
        },
        estimates={
            "note": "MODELLED, NOT DISCLOSED. Never assert these to the owner.",
            "revenue": estimate.revenue,
            "sde": estimate.sde,
            "indicative_price_low": estimate.indicative_price_low,
            "indicative_price_high": estimate.indicative_price_high,
            "basis": estimate.basis,
            "confidence": estimate.confidence,
        },
        hooks=personalization_hooks(prospect, estimate),
        angle=suggested_angle(prospect, score),
    )


def export_json(reports: Sequence[ProspectReport], path: str | Path) -> Path:
    """Write the full handoff payload."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "count": len(reports),
        "standing_constraints": STANDING_CONSTRAINTS,
        "prospects": [build_brief(r).to_dict() for r in reports],
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


CSV_COLUMNS = [
    "prospect_id", "name", "industry", "city", "state", "phone", "email",
    "email_status", "website", "address", "score", "est_sde", "est_revenue",
    "est_price_low", "est_price_high", "estimate_confidence", "hooks", "angle", "status",
]


def export_csv(reports: Sequence[ProspectReport], path: str | Path) -> Path:
    """Flat CSV for tools that will not read the JSON.

    The estimate columns keep their `est_` prefix so a modelled figure
    cannot be mistaken for a disclosed one in a spreadsheet.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for report in reports:
            brief = build_brief(report)
            writer.writerow({
                "prospect_id": brief.prospect_id,
                "name": brief.name,
                "industry": brief.industry,
                "city": brief.contact["city"],
                "state": brief.contact["state"],
                "phone": brief.contact["phone"],
                "email": brief.contact["email"],
                "email_status": brief.contact["email_status"],
                "website": brief.contact["website"],
                "address": brief.contact["address"],
                "score": brief.qualification["score"],
                "est_sde": brief.estimates["sde"],
                "est_revenue": brief.estimates["revenue"],
                "est_price_low": brief.estimates["indicative_price_low"],
                "est_price_high": brief.estimates["indicative_price_high"],
                "estimate_confidence": brief.estimates["confidence"],
                "hooks": " | ".join(brief.hooks),
                "angle": brief.angle,
                "status": brief.qualification["status"],
            })
    return path


def readme_for_agent() -> str:
    """Schema documentation shipped alongside the export."""
    return f"""# Outreach handoff - schema {SCHEMA_VERSION}

`prospects.json` holds one record per qualified, off-market business. These
owners have **not** listed their business and have not been contacted.

## Fields

| Field | Meaning |
| --- | --- |
| `prospect_id` | Stable id. Use it for dedupe and for reporting status back. |
| `contact.email_status` | `present`, `missing_enrichable_from_website`, or `missing_no_route`. |
| `qualification.score` | 0-100 fit for an owner-financed acquisition. Higher is better. |
| `qualification.why` | The top scoring factors, already in plain language. |
| `estimates.*` | **Modelled from industry averages. Not disclosed by the owner.** |
| `hooks` | Verifiable specifics safe to reference in a first touch. |
| `angle` | Suggested opening approach for this particular owner. |
| `do_not_claim` | Hard constraints. Every one must be honoured. |

## Constraints

{chr(10).join(f'- {c}' for c in STANDING_CONSTRAINTS)}

## Email coverage

Google Places returns a phone and a website, but almost never an email
address. Records with `email_status` other than `present` need an enrichment
step before any email sequence can run; `missing_enrichable_from_website`
means the business has its own site whose contact page is the natural source.

## Compliance

These are unsolicited B2B approaches. Before sending, confirm the sequence
meets the rules that apply to you - CAN-SPAM in the US requires a valid
physical postal address and a working opt-out in every commercial email, and
other jurisdictions are stricter. Honour any reply asking not to be
contacted, and feed it back so the prospect is marked `do_not_contact`.
"""
