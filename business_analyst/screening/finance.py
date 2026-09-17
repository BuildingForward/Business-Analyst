"""Acquisition finance math.

The bot's thesis is that the business buys itself: no cash out of pocket at
close, the purchase price is carried by the seller, and the business's own
cash flow services the note. Everything here supports that test.

The amortisation schedule is the single source of truth. Pricing is solved
numerically against that schedule rather than against a closed-form annuity,
so the price the bot offers and the coverage it reports can never disagree.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Tuple

from ..models import Listing, NoteTerms, OfferStructure

MONTHS = 12
DSCR_CAP = 99.0


def monthly_payment(principal: float, annual_rate: float, term_years: int) -> float:
    """Level-payment monthly payment for a whole number of years."""
    return monthly_payment_months(principal, annual_rate, term_years * MONTHS)


def monthly_payment_months(principal: float, annual_rate: float, n_months: int) -> float:
    """Level-payment monthly payment over an exact number of months."""
    if principal <= 0 or n_months <= 0:
        return 0.0
    r = annual_rate / MONTHS
    if r == 0:
        return principal / n_months
    return principal * (r * (1 + r) ** n_months) / ((1 + r) ** n_months - 1)


def note_payment_schedule(note: NoteTerms, months: int) -> List[float]:
    """Cash due in each of the first `months` months of a note.

    Three phases, in order: standby (nothing paid, interest capitalises),
    interest-only, then level amortisation over whatever term remains.
    A balloon is a financing event, not operating cash flow, so it is
    reported by `balloon_balance` rather than folded in here.
    """
    if months <= 0:
        return []

    total_months = note.term_years * MONTHS
    r = note.annual_rate / MONTHS
    balance = note.principal
    payments: List[float] = []

    standby = min(note.standby_months, total_months)
    for _ in range(standby):
        balance += balance * r
        payments.append(0.0)
        if len(payments) >= months:
            return payments[:months]

    io = max(0, min(note.interest_only_months, total_months - standby))
    for _ in range(io):
        payments.append(balance * r)
        if len(payments) >= months:
            return payments[:months]

    amort_months = total_months - standby - io
    level = monthly_payment_months(balance, note.annual_rate, amort_months)
    for _ in range(amort_months):
        payments.append(level)
        if len(payments) >= months:
            return payments[:months]

    # Past maturity nothing further is due.
    payments.extend([0.0] * (months - len(payments)))
    return payments[:months]


def remaining_balance(note: NoteTerms, after_months: int) -> float:
    """Principal still outstanding after `after_months` payments."""
    r = note.annual_rate / MONTHS
    balance = note.principal
    schedule = note_payment_schedule(note, after_months)
    for payment in schedule:
        interest = balance * r
        balance = balance + interest - payment
    return max(0.0, balance)


def balloon_balance(note: NoteTerms) -> Optional[float]:
    """Lump sum due if the note balloons, else None."""
    if note.balloon_months is None:
        return None
    return remaining_balance(note, note.balloon_months)


def annual_debt_service(notes: Iterable[NoteTerms], year: int = 1) -> float:
    """Cash debt service due in a given year (1-indexed) across all notes."""
    start = (year - 1) * MONTHS
    end = year * MONTHS
    return sum(sum(note_payment_schedule(n, end)[start:end]) for n in notes)


def stabilised_debt_service(notes: Iterable[NoteTerms]) -> float:
    """Debt service for the first full year after standby and interest-only end."""
    total = 0.0
    for note in notes:
        offset = note.standby_months + note.interest_only_months
        schedule = note_payment_schedule(note, offset + MONTHS)
        total += sum(schedule[offset : offset + MONTHS])
    return total


def dscr(cash_available: float, debt_service: float) -> float:
    """Debt service coverage ratio, capped when there is no debt to cover."""
    if debt_service <= 0:
        return DSCR_CAP if cash_available > 0 else 0.0
    return min(DSCR_CAP, cash_available / debt_service)


def _note_for_price(price: float, params: "StructureParams") -> NoteTerms:
    return NoteTerms(
        principal=price * params.max_seller_note_pct,
        annual_rate=params.note_rate,
        term_years=params.note_term_years,
        interest_only_months=params.interest_only_months,
        standby_months=params.standby_months,
        label="seller note",
    )


def max_supportable_price(
    available_cash_flow: float,
    params: "StructureParams",
    target_dscr: Optional[float] = None,
    ceiling: float = 50_000_000.0,
    tolerance: float = 1.0,
) -> float:
    """Largest price whose stabilised debt service still clears `target_dscr`.

    Solved by bisection against the real amortisation schedule, so standby
    interest capitalisation and the interest-only ramp are all accounted for.
    """
    target = target_dscr if target_dscr is not None else params.target_dscr
    if available_cash_flow <= 0 or target <= 0:
        return 0.0

    def coverage(price: float) -> float:
        return dscr(available_cash_flow, stabilised_debt_service([_note_for_price(price, params)]))

    if coverage(ceiling) >= target:
        return ceiling

    low, high = 0.0, ceiling
    while high - low > tolerance:
        mid = (low + high) / 2
        if coverage(mid) >= target:
            low = mid
        else:
            high = mid
    return low


@dataclass
class StructureParams:
    """Knobs for how aggressively the bot structures an offer."""

    buyer_salary: float = 60_000.0
    target_dscr: float = 1.5
    min_dscr: float = 1.25
    note_rate: float = 0.06
    note_term_years: int = 7
    standby_months: int = 3
    interest_only_months: int = 12
    max_seller_note_pct: float = 1.0        # 1.0 == 100% seller carry
    earnout_pct_of_sde: float = 0.15
    earnout_years: int = 3
    holdback_pct: float = 0.10              # of price, held against reps
    working_capital_buffer: float = 0.0     # cash the business must retain

    def validate(self) -> None:
        if not 0 < self.max_seller_note_pct <= 1.0:
            raise ValueError("max_seller_note_pct must be in (0, 1]")
        if self.min_dscr <= 0 or self.target_dscr <= 0:
            raise ValueError("DSCR targets must be positive")
        if self.note_term_years <= 0:
            raise ValueError("note_term_years must be positive")
        if self.standby_months + self.interest_only_months >= self.note_term_years * MONTHS:
            raise ValueError("standby plus interest-only cannot consume the whole term")
        if self.note_rate < 0:
            raise ValueError("note_rate cannot be negative")


def available_cash_flow(sde: float, params: StructureParams) -> float:
    """Cash left to service debt after the operator is paid."""
    return sde - params.buyer_salary - params.working_capital_buffer


def build_offer(
    listing: Listing,
    params: Optional[StructureParams] = None,
    sde_override: Optional[float] = None,
) -> OfferStructure:
    """Construct a zero-cash-at-close offer and test whether it clears.

    The structure is a seller note for the whole price (subject to
    `max_seller_note_pct`), a standby period so the buyer banks working
    capital first, an interest-only ramp, then amortisation. An earnout
    bridges the gap between the ask and what the cash flow supports.
    """
    params = params or StructureParams()
    params.validate()

    sde = sde_override if sde_override is not None else (listing.sde or 0.0)
    asking = listing.asking_price or 0.0
    assumptions: List[str] = []

    if sde <= 0:
        return OfferStructure(
            purchase_price=asking,
            cash_at_close=0.0,
            viable=False,
            buyer_salary=params.buyer_salary,
            notes_on_viability="No usable cash flow figure; cannot underwrite.",
            assumptions=["Seller has not disclosed SDE or cash flow."],
        )

    available = available_cash_flow(sde, params)
    if available <= 0:
        return OfferStructure(
            purchase_price=asking,
            cash_at_close=0.0,
            viable=False,
            buyer_salary=params.buyer_salary,
            notes_on_viability=(
                f"SDE of ${sde:,.0f} does not cover a ${params.buyer_salary:,.0f} "
                "operator salary; the business cannot service any debt."
            ),
            assumptions=[f"Operator salary of ${params.buyer_salary:,.0f} is taken first."],
        )

    ceiling = max_supportable_price(available, params)
    price = min(asking, ceiling) if asking else ceiling
    if asking and ceiling < asking:
        assumptions.append(
            f"Offer is below the ${asking:,.0f} ask because cash flow only "
            f"supports ${ceiling:,.0f} at a {params.target_dscr:.2f}x DSCR; "
            "the gap is bridged with an earnout."
        )

    note = _note_for_price(price, params)
    holdback = price * params.holdback_pct
    cash_at_close = price - note.principal

    assumptions.append(
        f"Seller carries {params.max_seller_note_pct * 100:.0f}% of the price "
        f"at {params.note_rate * 100:.1f}% over {params.note_term_years} years."
    )
    if params.standby_months:
        assumptions.append(
            f"First {params.standby_months} months on full standby (no payments) "
            "to fund working capital post-close; interest accrues to principal."
        )
    if params.interest_only_months:
        assumptions.append(
            f"Months {params.standby_months + 1}-"
            f"{params.standby_months + params.interest_only_months} are interest-only."
        )
    if holdback:
        assumptions.append(
            f"${holdback:,.0f} ({params.holdback_pct * 100:.0f}%) held back against "
            "reps, warranties and working-capital true-up."
        )

    year_one = annual_debt_service([note], year=1)
    stabilised = stabilised_debt_service([note])
    year_one_dscr = dscr(available, year_one)
    stabilised_dscr = dscr(available, stabilised)

    earnout_pct = 0.0
    earnout_years = 0
    if asking and price < asking * 0.98:
        earnout_pct = params.earnout_pct_of_sde
        earnout_years = params.earnout_years
        assumptions.append(
            f"Earnout of {earnout_pct * 100:.0f}% of SDE above the trailing "
            f"baseline for {earnout_years} years, so the seller reaches their "
            "number if the business performs."
        )

    viable = stabilised_dscr >= params.min_dscr and price > 0
    if viable:
        verdict = (
            f"Clears at {stabilised_dscr:.2f}x stabilised DSCR "
            f"({year_one_dscr:.2f}x in year one) with zero cash at close."
        )
    else:
        verdict = (
            f"Stabilised DSCR of {stabilised_dscr:.2f}x is below the "
            f"{params.min_dscr:.2f}x floor. Needs a longer term, a lower price, "
            "or more of the price pushed into the earnout."
        )

    return OfferStructure(
        purchase_price=round(price, 2),
        cash_at_close=round(cash_at_close, 2),
        notes=[note],
        earnout_pct_of_sde=earnout_pct,
        earnout_years=earnout_years,
        holdback=round(holdback, 2),
        annual_debt_service=round(stabilised, 2),
        year_one_debt_service=round(year_one, 2),
        dscr=round(stabilised_dscr, 3),
        year_one_dscr=round(year_one_dscr, 3),
        free_cash_after_debt=round(available - stabilised, 2),
        buyer_salary=params.buyer_salary,
        assumptions=assumptions,
        viable=viable,
        notes_on_viability=verdict,
    )


def sensitivity(
    listing: Listing,
    params: Optional[StructureParams] = None,
    sde_haircuts: Sequence[float] = (0.0, 0.10, 0.20, 0.30),
    offer: Optional[OfferStructure] = None,
) -> List[dict]:
    """Stress the *agreed* offer against an overstated SDE.

    The price and note are held fixed — that is the whole point. Small
    business sellers add back aggressively, and a note that only covers at
    the seller's own numbers is how a no-money-down deal becomes a personal
    liability. `breaks_at` in the summary is the haircut where coverage
    drops through the floor.
    """
    params = params or StructureParams()
    base_offer = offer or build_offer(listing, params)
    base_sde = listing.sde or 0.0
    notes = base_offer.notes

    rows = []
    for haircut in sde_haircuts:
        adjusted_sde = base_sde * (1 - haircut)
        available = available_cash_flow(adjusted_sde, params)
        stabilised = stabilised_debt_service(notes)
        coverage = dscr(available, stabilised)
        rows.append(
            {
                "sde_haircut": haircut,
                "sde": round(adjusted_sde, 2),
                "price": base_offer.purchase_price,
                "debt_service": round(stabilised, 2),
                "dscr": round(coverage, 3),
                "free_cash": round(available - stabilised, 2),
                "viable": coverage >= params.min_dscr,
            }
        )
    return rows


def breakeven_haircut(
    listing: Listing,
    params: Optional[StructureParams] = None,
    offer: Optional[OfferStructure] = None,
) -> float:
    """Fraction of SDE that can evaporate before the offer stops covering.

    Returns 0.0 if the deal does not clear even at the seller's own numbers.
    """
    params = params or StructureParams()
    base_offer = offer or build_offer(listing, params)
    base_sde = listing.sde or 0.0
    if base_sde <= 0:
        return 0.0
    stabilised = stabilised_debt_service(base_offer.notes)
    # Cash flow needed to hold min_dscr, converted back to an SDE haircut.
    required_available = stabilised * params.min_dscr
    required_sde = required_available + params.buyer_salary + params.working_capital_buffer
    if required_sde >= base_sde:
        return 0.0
    return round(1 - (required_sde / base_sde), 4)
