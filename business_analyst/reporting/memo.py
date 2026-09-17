"""Markdown deal memo - the thing a human actually reads before making an offer."""

from __future__ import annotations

from typing import List, Optional

from ..models import DealReport, OfferStructure
from ..screening import industries
from ..screening.finance import StructureParams, breakeven_haircut, sensitivity


def _money(value: Optional[float]) -> str:
    """Zero is a real, meaningful number here - only None is unknown."""
    return f"${value:,.0f}" if value is not None else "n/a"


def _financing_label(listing) -> str:
    """Show the inferred posture when the listing field itself is silent."""
    from ..models import SellerFinancing
    from ..screening.scoring import infer_seller_financing

    if listing.seller_financing != SellerFinancing.UNKNOWN:
        return listing.seller_financing.value
    inferred = infer_seller_financing(listing)
    if inferred == SellerFinancing.UNKNOWN:
        return "not stated - ask the broker"
    return f"{inferred.value} (inferred from listing copy)"


def _fact_table(report: DealReport) -> List[str]:
    l = report.listing
    rows = [
        ("Source", f"[{l.source}]({l.url})" if l.url else l.source),
        ("Industry", l.industry or "unstated"),
        ("Location", l.location or "not disclosed"),
        ("Asking price", _money(l.asking_price)),
        ("Revenue", _money(l.revenue)),
        ("SDE / cash flow", _money(l.sde)),
        ("Asking multiple", f"{l.price_to_sde:.2f}x SDE" if l.price_to_sde else "n/a"),
        ("Established", str(l.established_year) if l.established_year else "not disclosed"),
        ("Employees", str(l.employees) if l.employees is not None else "not disclosed"),
        ("Seller financing", _financing_label(l)),
        ("Days on market", str(l.days_on_market) if l.days_on_market is not None else "n/a"),
    ]
    out = ["| Fact | Value |", "| --- | --- |"]
    out += [f"| {k} | {v} |" for k, v in rows]
    return out


def _offer_table(offer: OfferStructure) -> List[str]:
    note = offer.notes[0] if offer.notes else None
    rows = [
        ("Offer price", _money(offer.purchase_price)),
        ("**Cash at close**", f"**{_money(offer.cash_at_close)}**"),
        ("Seller note", _money(note.principal) if note else "none"),
        (
            "Note terms",
            f"{note.annual_rate * 100:.1f}% / {note.term_years} yrs / "
            f"{note.standby_months}mo standby / {note.interest_only_months}mo IO"
            if note else "n/a",
        ),
        ("Holdback", _money(offer.holdback)),
        (
            "Earnout",
            f"{offer.earnout_pct_of_sde * 100:.0f}% of SDE for {offer.earnout_years} yrs"
            if offer.earnout_pct_of_sde else "none",
        ),
        ("Operator salary", _money(offer.buyer_salary)),
        ("Year-1 debt service", _money(offer.year_one_debt_service)),
        ("Stabilised debt service", _money(offer.annual_debt_service)),
        ("Year-1 DSCR", f"{offer.year_one_dscr:.2f}x"),
        ("**Stabilised DSCR**", f"**{offer.dscr:.2f}x**"),
        ("Free cash after debt", _money(offer.free_cash_after_debt)),
    ]
    out = ["| Term | Value |", "| --- | --- |"]
    out += [f"| {k} | {v} |" for k, v in rows]
    return out


def render_memo(report: DealReport, params: Optional[StructureParams] = None) -> str:
    """Full markdown memo: facts, score, structure, stress test, analysis."""
    params = params or StructureParams()
    l = report.listing
    profile = industries.classify(l.industry or l.name)
    lines: List[str] = [f"# {l.name}", ""]

    if report.score:
        verdict = "PURSUE" if report.score.passed and report.score.total >= 60 else (
            "REJECTED" if not report.score.passed else "WATCH"
        )
        lines += [
            f"**{verdict}** — acquirability score **{report.score.total:.1f}/100**",
            "",
        ]

    lines += ["## Facts", ""] + _fact_table(report) + [""]

    lines += [
        "## Industry read",
        "",
        f"Classified as **{profile.name}**. Typical range "
        f"{profile.typical_sde_multiple_low:.1f}–{profile.typical_sde_multiple_high:.1f}x SDE. "
        f"Capital intensity {profile.capital_intensity}, owner dependence "
        f"{profile.owner_dependence}.",
        "",
        f"> {profile.notes}",
        "",
    ]

    if report.score:
        lines += ["## Score breakdown", "", "| Factor | Score | Weight | Why |", "| --- | --- | --- | --- |"]
        for c in sorted(report.score.components, key=lambda c: -c.contribution):
            lines.append(f"| {c.name} | {c.score:.2f} | {c.weight:.2f} | {c.rationale} |")
        lines.append("")
        if report.score.hard_fails:
            lines += ["### Hard fails", ""] + [f"- {f}" for f in report.score.hard_fails] + [""]
        if report.score.flags:
            lines += ["### Flags", ""] + [f"- {f}" for f in report.score.flags] + [""]

    if report.offer:
        lines += ["## Proposed structure", ""] + _offer_table(report.offer) + [""]
        lines += [f"**Verdict:** {report.offer.notes_on_viability}", "", "### Assumptions", ""]
        lines += [f"- {a}" for a in report.offer.assumptions] + [""]

        rows = sensitivity(l, params, offer=report.offer)
        cushion = breakeven_haircut(l, params, offer=report.offer)
        lines += [
            "### Stress test",
            "",
            "Price and note held fixed; only the seller's earnings claim is discounted.",
            "",
            "| SDE haircut | SDE | Debt service | DSCR | Free cash | Covers? |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for r in rows:
            lines.append(
                f"| {r['sde_haircut'] * 100:.0f}% | {_money(r['sde'])} | "
                f"{_money(r['debt_service'])} | {r['dscr']:.2f}x | "
                f"{_money(r['free_cash'])} | {'yes' if r['viable'] else 'NO'} |"
            )
        lines += [
            "",
            f"**Breakeven:** the deal stops covering once SDE is overstated by "
            f"**{cushion * 100:.1f}%**.",
            "",
        ]

    if report.sections:
        lines += ["---", "", "## Analysis", ""]
        for section in report.sections:
            lines += [f"### {section.title}", "", section.content.strip(), ""]

    lines += [
        "---",
        "",
        "*Generated by the business analyst bot. Figures are the seller's own "
        "unless marked otherwise, and none of this is a substitute for "
        "diligence, an accountant or a lawyer.*",
    ]
    return "\n".join(lines)


def render_shortlist(reports: List[DealReport]) -> str:
    """One table ranking everything the bot currently likes."""
    lines = [
        "# Shortlist",
        "",
        "| Deal | Industry | Ask | SDE | Mult | Score | Offer | Cash | DSCR |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in reports:
        l = r.listing
        lines.append(
            f"| {l.name} | {l.industry or '—'} | {_money(l.asking_price)} | "
            f"{_money(l.sde)} | "
            f"{f'{l.price_to_sde:.1f}x' if l.price_to_sde else '—'} | "
            f"{r.score.total:.1f} | "
            f"{_money(r.offer.purchase_price) if r.offer else '—'} | "
            f"{_money(r.offer.cash_at_close) if r.offer else '—'} | "
            f"{f'{r.offer.dscr:.2f}x' if r.offer else '—'} |"
        )
    return "\n".join(lines)
