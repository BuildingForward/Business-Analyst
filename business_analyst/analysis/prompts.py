"""The eight analyst playbooks.

Each playbook is the original operator prompt, hardened for autonomous use:
the placeholders are filled from a Listing, and every template carries a
standing instruction to separate disclosed fact from inference. An analyst
that silently invents a market size is worse than no analyst.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from ..models import Listing, OfferStructure
from ..screening.industries import IndustryProfile, classify

SYSTEM_PROMPT = """You are the analyst arm of a search fund that acquires small \
businesses using seller financing and no cash out of pocket.

Operating rules, in priority order:
1. Distinguish what is DISCLOSED in the brief from what you are INFERRING. Label \
inferences inline as (inferred). Never present an inference as a disclosed fact.
2. When a number you need was not disclosed, say so and state the diligence \
question that would produce it. Do not fabricate market sizes, growth rates, \
competitor revenue or customer counts.
3. Be specific and falsifiable. "Improve marketing" is not a recommendation; \
"move the 40% of revenue from one-off calls onto annual service agreements at \
$X/month" is.
4. Underwrite the downside first. The buyer is personally signing a note, so \
what breaks the business matters more than what could double it.
5. Keep every answer structured with clear headings and short paragraphs. \
Prefer tables for comparisons.
"""

_PRELUDE = "Business brief:\n{brief}\n\n"


def format_money(value: Optional[float]) -> str:
    """Only None is undisclosed. Zero is a fact, and often the important one."""
    return f"${value:,.0f}" if value is not None else "not disclosed"


def build_brief(
    listing: Listing,
    profile: Optional[IndustryProfile] = None,
    offer: Optional[OfferStructure] = None,
) -> str:
    """Render everything known about a listing into a compact fact sheet."""
    profile = profile or classify(listing.industry or listing.name)
    lines = [
        f"- Name: {listing.name}",
        f"- Industry: {listing.industry or 'unstated'} (classified as {profile.name})",
        f"- Location: {listing.location or 'not disclosed'}",
        f"- Asking price: {format_money(listing.asking_price)}",
        f"- Revenue: {format_money(listing.revenue)}",
        f"- SDE / cash flow: {format_money(listing.sde)}",
    ]
    if listing.price_to_sde:
        lines.append(
            f"- Asking multiple: {listing.price_to_sde:.2f}x SDE "
            f"(industry range {profile.typical_sde_multiple_low:.1f}-"
            f"{profile.typical_sde_multiple_high:.1f}x)"
        )
    if listing.established_year:
        lines.append(f"- Established: {listing.established_year} ({listing.age_years} years)")
    if listing.employees is not None:
        lines.append(f"- Employees: {listing.employees}")
    if listing.owner_hours_per_week is not None:
        lines.append(f"- Owner hours/week: {listing.owner_hours_per_week:.0f}")
    if listing.reason_for_sale:
        lines.append(f"- Stated reason for sale: {listing.reason_for_sale}")
    lines.append(f"- Seller financing posture: {listing.seller_financing.value}")
    if listing.inventory:
        lines.append(f"- Inventory included: {format_money(listing.inventory)}")
    if listing.real_estate_included:
        lines.append("- Real estate: included in the asking price")
    if listing.days_on_market is not None:
        lines.append(f"- Days on market: {listing.days_on_market}")
    if listing.description:
        lines.append(f"- Listing copy: {listing.description.strip()}")
    if offer:
        lines.append(
            f"- Contemplated structure: {format_money(offer.purchase_price)} price, "
            f"{format_money(offer.cash_at_close)} cash at close, "
            f"{offer.dscr:.2f}x stabilised DSCR"
        )
    return "\n".join(lines)


@dataclass
class Playbook:
    """One named analysis with a template and the fields it needs."""

    key: str
    title: str
    purpose: str
    template: str

    def render(self, **kwargs: str) -> str:
        return _PRELUDE.format(brief=kwargs.pop("brief", "")) + self.template.format(**kwargs)


PLAYBOOKS: Dict[str, Playbook] = {
    "full_analysis": Playbook(
        key="full_analysis",
        title="Full Business Analysis",
        purpose="End-to-end strategic read on the target.",
        template=(
            "Act as an MBA-trained business strategist. Analyze this business "
            "end-to-end: revenue model, market position, competitive threats, "
            "operational gaps, growth bottlenecks, and the 3 strategic moves most "
            "likely to drive meaningful growth over the next 12 months. Prioritize "
            "your recommendations by impact and effort, and present the priorities "
            "as a table with columns: Move | Impact (1-5) | Effort (1-5) | "
            "First action | How it is financed without new capital.\n\n"
            "Close with the three facts that, if false, would most damage this "
            "thesis, and how a buyer would verify each one in diligence."
        ),
    ),
    "market_research": Playbook(
        key="market_research",
        title="Market Research Report",
        purpose="Is this market worth entering, and is now the time?",
        template=(
            "Act as a senior market research analyst. The buyer is entering "
            "{market} via acquisition of this business, whose offer is {offer}. "
            "Analyze the market size, most valuable customer segments, major "
            "trends, unmet needs, competitive landscape, and market timing. "
            "Identify the biggest opportunity and the strongest reason to enter "
            "now or to walk away.\n\n"
            "State explicitly which figures are disclosed, which are inferred, "
            "and which would need primary research. End with a one-line verdict: "
            "ENTER, ENTER WITH CONDITIONS, or AVOID, plus the single fact that "
            "would flip it."
        ),
    ),
    "competitor_teardown": Playbook(
        key="competitor_teardown",
        title="Competitor Teardown",
        purpose="Where the exploitable gap is.",
        template=(
            "Act as a competitive intelligence strategist. The relevant "
            "competitors are {competitors}. Break down each competitor's "
            "positioning, pricing, strengths, weaknesses, acquisition strategy, "
            "and key differentiators as a comparison table. Then identify the "
            "most valuable market gap this business could realistically exploit "
            "within 12 months given its current size and cash constraints, and "
            "how to position the offer to stand out.\n\n"
            "If you do not have specific knowledge of a named competitor, say so "
            "and describe the archetype instead rather than inventing detail."
        ),
    ),
    "pricing_audit": Playbook(
        key="pricing_audit",
        title="Pricing Strategy Audit",
        purpose="Find the trapped margin.",
        template=(
            "Act as a pricing strategist. The offer is {offer} and it currently "
            "prices at {current_price}. Evaluate the pricing against value "
            "delivered, target customer, competitive landscape, and willingness "
            "to pay. Identify whether this business is underpricing or "
            "overpricing, where its pricing power comes from, and the positioning "
            "changes that would justify a higher price.\n\n"
            "Quantify the upside: at the current revenue base, what does each 1% "
            "of price realization add to SDE, and what does that do to the "
            "business's value at the industry multiple? Flag the churn risk of "
            "each move."
        ),
    ),
    "segmentation": Playbook(
        key="segmentation",
        title="Customer Segmentation",
        purpose="Who to sell to first after close.",
        template=(
            "Act as a customer segmentation specialist. The current customer base "
            "is {customers}. Identify the 3 highest-value customer segments. For "
            "each, explain who they are, what they need most, their buying "
            "triggers, willingness to pay, and how to reach them. Then recommend "
            "where acquisition effort should go first, using explicit criteria "
            "(payback period, cycle length, concentration risk, cash required).\n\n"
            "Assume the buyer has no marketing budget in year one beyond existing "
            "cash flow. Rank by cash-efficiency accordingly."
        ),
    ),
    "swot": Playbook(
        key="swot",
        title="SWOT with Fixes",
        purpose="Evidence-based SWOT, not platitudes.",
        template=(
            "Act as a strategic business analyst. Build a specific, "
            "evidence-based SWOT for this business - not generic business advice. "
            "Every item must reference something in the brief or be labelled "
            "(inferred). For every weakness, give a practical fix with a cost and "
            "a timeframe. For every threat, give a mitigation. For every "
            "opportunity, give the first action to take this month.\n\n"
            "Present as four sections, each as a table: Item | Evidence | "
            "Fix / mitigation / first action | Cost | Timeframe."
        ),
    ),
    "gtm": Playbook(
        key="gtm",
        title="Go-To-Market Plan",
        purpose="How to grow it once owned.",
        template=(
            "Act as a senior go-to-market strategist. The product is {product} "
            "for {audience} in {market}. Build a complete GTM plan covering the "
            "ideal beachhead segment, positioning, acquisition channels, launch "
            "sequence, messaging, and a first-100-customer strategy. Rank the "
            "channels in a table by expected speed, cost, and scalability.\n\n"
            "Constraint: this is a post-acquisition plan funded entirely from the "
            "acquired business's own cash flow, after debt service. Treat any "
            "channel needing meaningful upfront spend as out of scope in year one "
            "and say so."
        ),
    ),
    "growth_plan": Playbook(
        key="growth_plan",
        title="Strategic Growth Plan",
        purpose="Path from current cash flow to the target.",
        template=(
            "Act as a business strategy consultant. This business currently "
            "generates {current_revenue} from {revenue_source}, and the goal is "
            "{target_revenue} within {timeframe}. Build a realistic growth plan "
            "covering revenue levers, new markets, offer expansion, bolt-on "
            "acquisition opportunities, and the key metrics to watch. Identify the "
            "single biggest constraint holding back growth and the first move to "
            "overcome it.\n\n"
            "Show the arithmetic: which lever contributes how much of the gap, and "
            "state plainly if the target is not reachable in the timeframe from "
            "the disclosed base. Every use of cash must survive the existing debt "
            "service."
        ),
    ),
}

PLAYBOOK_ORDER: List[str] = [
    "full_analysis",
    "market_research",
    "competitor_teardown",
    "pricing_audit",
    "segmentation",
    "swot",
    "gtm",
    "growth_plan",
]

# Cheapest useful subset when the bot is triaging many deals at once.
TRIAGE_SET: List[str] = ["full_analysis", "swot", "growth_plan"]


def default_context(listing: Listing, offer: Optional[OfferStructure] = None) -> Dict[str, str]:
    """Fill every template placeholder from what the listing actually says."""
    profile = classify(listing.industry or listing.name)
    market = listing.industry or profile.name
    if listing.location:
        market = f"{market} in {listing.location}"

    revenue_source = listing.description.strip() or f"{profile.name} operations"
    target_revenue = (
        format_money(listing.revenue * 2) if listing.revenue else "double current revenue"
    )  # revenue of None or 0 gives no meaningful target to name
    return {
        "brief": build_brief(listing, profile, offer),
        "market": market,
        "offer": listing.description.strip() or f"{profile.name} services",
        "competitors": (
            "not named in the listing - identify the likely competitor set for "
            f"{market} and analyse the archetypes"
        ),
        "current_price": "not disclosed in the listing",
        "customers": (
            listing.description.strip()
            or f"not described beyond the {profile.name} classification"
        ),
        "product": listing.name,
        "audience": f"buyers of {profile.name} services",
        "current_revenue": format_money(listing.revenue),
        "revenue_source": revenue_source,
        "target_revenue": target_revenue,
        "timeframe": "36 months",
    }


def render(key: str, listing: Listing, offer: Optional[OfferStructure] = None, **overrides) -> str:
    """Render one playbook for a listing."""
    if key not in PLAYBOOKS:
        raise KeyError(f"Unknown playbook '{key}'. Known: {', '.join(PLAYBOOK_ORDER)}")
    ctx = default_context(listing, offer)
    ctx.update({k: v for k, v in overrides.items() if v is not None})
    return PLAYBOOKS[key].render(**ctx)
