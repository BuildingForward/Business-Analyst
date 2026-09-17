"""The finance math is what a real offer rests on, so it is tested hardest."""

import unittest

from business_analyst.models import Listing, NoteTerms
from business_analyst.screening.finance import (
    MONTHS,
    StructureParams,
    annual_debt_service,
    balloon_balance,
    breakeven_haircut,
    build_offer,
    dscr,
    max_supportable_price,
    monthly_payment,
    note_payment_schedule,
    remaining_balance,
    sensitivity,
    stabilised_debt_service,
)


class TestAmortisation(unittest.TestCase):
    def test_known_payment(self):
        # $100k at 6% over 10 years is $1,110.21/mo.
        self.assertAlmostEqual(monthly_payment(100_000, 0.06, 10), 1110.21, places=2)

    def test_zero_rate_is_straight_line(self):
        self.assertAlmostEqual(monthly_payment(120_000, 0.0, 10), 1000.0, places=6)

    def test_zero_principal_costs_nothing(self):
        self.assertEqual(monthly_payment(0, 0.06, 10), 0.0)

    def test_schedule_respects_phases(self):
        note = NoteTerms(
            principal=100_000, annual_rate=0.12, term_years=5,
            interest_only_months=6, standby_months=3,
        )
        schedule = note_payment_schedule(note, 24)
        self.assertEqual(schedule[:3], [0.0, 0.0, 0.0], "standby months must pay nothing")
        # After standby the balance has grown by 3 months of 1% interest.
        grown = 100_000 * (1.01 ** 3)
        self.assertAlmostEqual(schedule[3], grown * 0.01, places=2)
        self.assertAlmostEqual(schedule[4], schedule[3], places=6)
        self.assertGreater(schedule[10], schedule[5], "amortising payment exceeds interest-only")

    def test_schedule_never_exceeds_term(self):
        note = NoteTerms(principal=50_000, annual_rate=0.06, term_years=2)
        schedule = note_payment_schedule(note, 36)
        self.assertEqual(len(schedule), 36)
        self.assertEqual(sum(schedule[24:]), 0.0, "nothing is due past maturity")

    def test_amortising_note_pays_off(self):
        note = NoteTerms(principal=100_000, annual_rate=0.06, term_years=5)
        self.assertAlmostEqual(remaining_balance(note, 5 * MONTHS), 0.0, places=2)

    def test_balloon_balance_is_outstanding_principal(self):
        note = NoteTerms(principal=100_000, annual_rate=0.06, term_years=20, balloon_months=60)
        balance = balloon_balance(note)
        self.assertIsNotNone(balance)
        self.assertLess(balance, 100_000)
        self.assertGreater(balance, 50_000, "20-year amortisation repays little in 5 years")

    def test_no_balloon_returns_none(self):
        self.assertIsNone(balloon_balance(NoteTerms(100_000, 0.06, 5)))


class TestCoverage(unittest.TestCase):
    def test_dscr_basic(self):
        self.assertAlmostEqual(dscr(150_000, 100_000), 1.5)

    def test_dscr_with_no_debt_is_capped_not_infinite(self):
        self.assertEqual(dscr(100_000, 0), 99.0)
        self.assertEqual(dscr(0, 0), 0.0)

    def test_year_one_is_cheaper_than_stabilised_when_ramped(self):
        note = NoteTerms(
            principal=300_000, annual_rate=0.06, term_years=7,
            standby_months=3, interest_only_months=12,
        )
        self.assertLess(annual_debt_service([note], 1), stabilised_debt_service([note]))


class TestPricing(unittest.TestCase):
    def test_solver_hits_the_target_dscr(self):
        """The solved price must actually produce the DSCR it was solved for."""
        params = StructureParams()
        available = 125_000.0
        price = max_supportable_price(available, params)
        from business_analyst.screening.finance import _note_for_price

        achieved = dscr(available, stabilised_debt_service([_note_for_price(price, params)]))
        self.assertAlmostEqual(achieved, params.target_dscr, places=2)

    def test_no_cash_flow_supports_no_price(self):
        self.assertEqual(max_supportable_price(0, StructureParams()), 0.0)
        self.assertEqual(max_supportable_price(-5_000, StructureParams()), 0.0)

    def test_longer_term_supports_a_higher_price(self):
        short = max_supportable_price(120_000, StructureParams(note_term_years=5))
        long = max_supportable_price(120_000, StructureParams(note_term_years=10))
        self.assertGreater(long, short)


class TestOffers(unittest.TestCase):
    def setUp(self):
        self.listing = Listing(
            source="t", external_id="1", name="Test Co",
            asking_price=500_000, cash_flow=180_000,
        )

    def test_offer_requires_no_cash(self):
        offer = build_offer(self.listing)
        self.assertEqual(offer.cash_at_close, 0.0)
        self.assertTrue(offer.viable)

    def test_offer_never_exceeds_the_ask(self):
        """An ask below what the cash flow supports is simply accepted."""
        cheap = Listing(source="t", external_id="2", name="Cheap", asking_price=200_000,
                        cash_flow=400_000)
        offer = build_offer(cheap, StructureParams(buyer_salary=150_000))
        self.assertEqual(offer.purchase_price, 200_000)
        self.assertEqual(offer.earnout_pct_of_sde, 0.0, "no gap means no earnout")

    def test_earnout_bridges_a_gap(self):
        offer = build_offer(self.listing)
        self.assertLess(offer.purchase_price, 500_000)
        self.assertGreater(offer.earnout_pct_of_sde, 0)

    def test_no_cash_flow_is_not_underwritable(self):
        blind = Listing(source="t", external_id="3", name="Blind", asking_price=400_000)
        offer = build_offer(blind)
        self.assertFalse(offer.viable)
        self.assertIn("No usable cash flow", offer.notes_on_viability)

    def test_salary_takes_priority_over_debt(self):
        small = Listing(source="t", external_id="4", name="Small", asking_price=200_000,
                        cash_flow=40_000)
        offer = build_offer(small, StructureParams(buyer_salary=60_000))
        self.assertFalse(offer.viable)
        self.assertIn("operator salary", offer.notes_on_viability)

    def test_reported_dscr_matches_the_schedule(self):
        """The memo's DSCR must be recomputable from the note it prints."""
        offer = build_offer(self.listing)
        available = 180_000 - offer.buyer_salary
        self.assertAlmostEqual(
            offer.dscr, round(dscr(available, stabilised_debt_service(offer.notes)), 3), places=3
        )

    def test_invalid_params_are_rejected(self):
        with self.assertRaises(ValueError):
            build_offer(self.listing, StructureParams(max_seller_note_pct=1.5))
        with self.assertRaises(ValueError):
            build_offer(self.listing, StructureParams(note_term_years=1, interest_only_months=12,
                                                      standby_months=6))


class TestStress(unittest.TestCase):
    def setUp(self):
        self.listing = Listing(source="t", external_id="1", name="Test Co",
                               asking_price=500_000, cash_flow=180_000)

    def test_haircuts_degrade_coverage(self):
        """The whole point: the price is held fixed while earnings are discounted."""
        rows = sensitivity(self.listing)
        prices = {r["price"] for r in rows}
        self.assertEqual(len(prices), 1, "sensitivity must not re-price the deal")
        ratios = [r["dscr"] for r in rows]
        self.assertEqual(ratios, sorted(ratios, reverse=True), "DSCR must fall as SDE falls")

    def test_breakeven_sits_between_pass_and_fail(self):
        offer = build_offer(self.listing)
        cushion = breakeven_haircut(self.listing, offer=offer)
        rows = sensitivity(self.listing, offer=offer, sde_haircuts=(cushion - 0.01, cushion + 0.01))
        self.assertTrue(rows[0]["viable"])
        self.assertFalse(rows[1]["viable"])

    def test_breakeven_is_zero_when_deal_does_not_clear(self):
        """SDE that cannot even cover the operator's salary has no cushion."""
        params = StructureParams(buyer_salary=60_000)
        weak = Listing(source="t", external_id="9", name="Weak", asking_price=100_000,
                       cash_flow=50_000)
        self.assertFalse(build_offer(weak, params).viable)
        self.assertEqual(breakeven_haircut(weak, params), 0.0)

    def test_marginal_deal_has_a_small_cushion(self):
        """Pricing at the 1.5x target against a 1.25x floor always leaves slack."""
        params = StructureParams(buyer_salary=60_000)
        thin = Listing(source="t", external_id="10", name="Thin", asking_price=100_000,
                       cash_flow=61_000)
        offer = build_offer(thin, params)
        self.assertTrue(offer.viable)
        cushion = breakeven_haircut(thin, params, offer=offer)
        self.assertGreater(cushion, 0.0)
        self.assertLess(cushion, 0.05, "a marginal deal must not look safe")


if __name__ == "__main__":
    unittest.main()
