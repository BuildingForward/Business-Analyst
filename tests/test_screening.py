"""Screening: industry classification and the acquirability score."""

import unittest

from business_analyst.models import Listing, SellerFinancing
from business_analyst.screening import industries
from business_analyst.screening.scoring import (
    ScreenConfig,
    infer_seller_financing,
    rank,
    score_listing,
)


def listing(**kwargs) -> Listing:
    base = dict(
        source="test", external_id="1", name="Test Co", industry="commercial cleaning",
        asking_price=450_000, cash_flow=185_000, established_year=2005,
    )
    base.update(kwargs)
    return Listing(**base)


class TestIndustryClassification(unittest.TestCase):
    def test_exact_name(self):
        self.assertEqual(industries.classify("commercial cleaning").name, "commercial cleaning")

    def test_alias(self):
        self.assertEqual(industries.classify("Amazon FBA brand").name, "e-commerce")
        self.assertEqual(industries.classify("local mechanic shop").name, "auto repair")

    def test_longest_match_wins(self):
        """'commercial cleaning' must beat the bare 'cleaning' alias."""
        self.assertEqual(
            industries.classify("Commercial Cleaning and janitorial").name, "commercial cleaning"
        )

    def test_unknown_falls_back(self):
        self.assertEqual(industries.classify("interdimensional widgets").name, "unclassified")
        self.assertEqual(industries.classify("").name, "unclassified")

    def test_every_profile_has_a_sane_range(self):
        for profile in industries.all_profiles():
            self.assertLess(profile.typical_sde_multiple_low, profile.typical_sde_multiple_high)
            self.assertGreater(profile.typical_sde_multiple_low, 0)
            self.assertIn(profile.capital_intensity, {"low", "medium", "high"})
            self.assertIn(profile.owner_dependence, {"low", "medium", "high"})


class TestFinancingInference(unittest.TestCase):
    def test_structured_field_wins(self):
        l = listing(seller_financing=SellerFinancing.REFUSED, description="seller financing!")
        self.assertEqual(infer_seller_financing(l), SellerFinancing.REFUSED)

    def test_reads_listing_copy(self):
        self.assertEqual(
            infer_seller_financing(listing(description="Owner financing available")),
            SellerFinancing.OFFERED,
        )

    def test_negative_language_beats_positive(self):
        self.assertEqual(
            infer_seller_financing(listing(description="Cash only. No seller financing.")),
            SellerFinancing.REFUSED,
        )

    def test_silence_is_unknown(self):
        self.assertEqual(infer_seller_financing(listing(description="Great business")),
                         SellerFinancing.UNKNOWN)


class TestScoring(unittest.TestCase):
    def test_good_deal_scores_well_and_passes(self):
        score = score_listing(listing(
            description="Manager-run with recurring monthly contracts. Seller financing available.",
            reason_for_sale="Owner retiring", days_on_market=200,
        ))
        self.assertTrue(score.passed)
        self.assertGreater(score.total, 65)

    def test_cash_only_is_a_hard_fail(self):
        score = score_listing(listing(seller_financing=SellerFinancing.REFUSED))
        self.assertFalse(score.passed)
        self.assertTrue(any("carrying paper" in f for f in score.hard_fails))

    def test_overpriced_is_a_hard_fail(self):
        score = score_listing(listing(asking_price=2_000_000, cash_flow=100_000))
        self.assertFalse(score.passed)
        self.assertTrue(any("above the" in f for f in score.hard_fails))

    def test_blocked_industry_is_a_hard_fail(self):
        score = score_listing(listing(industry="dental practice"))
        self.assertFalse(score.passed)
        self.assertTrue(any("excluded from the thesis" in f for f in score.hard_fails))

    def test_too_new_is_a_hard_fail(self):
        score = score_listing(listing(established_year=2025))
        self.assertFalse(score.passed)
        self.assertTrue(any("minimum" in f for f in score.hard_fails))

    def test_missing_cash_flow_is_a_hard_fail(self):
        score = score_listing(listing(cash_flow=None, ebitda=None))
        self.assertFalse(score.passed)

    def test_mandate_restricts_industries(self):
        cfg = ScreenConfig(allowed_industries={"hvac"})
        self.assertFalse(score_listing(listing(), cfg).passed)
        self.assertTrue(score_listing(listing(industry="hvac"), cfg).passed)

    def test_score_is_bounded_and_explainable(self):
        score = score_listing(listing())
        self.assertGreaterEqual(score.total, 0)
        self.assertLessEqual(score.total, 100)
        self.assertEqual(len(score.components), 7)
        for c in score.components:
            self.assertGreaterEqual(c.score, 0)
            self.assertLessEqual(c.score, 1)
            self.assertTrue(c.rationale, "every component must justify itself")
        self.assertIn("Acquirability score", score.explain())

    def test_thin_cushion_is_flagged(self):
        score = score_listing(listing(asking_price=430_000, cash_flow=115_000))
        self.assertTrue(any("cushion" in f for f in score.flags))

    def test_real_estate_is_flagged(self):
        score = score_listing(listing(real_estate_included=True))
        self.assertTrue(any("Real estate" in f for f in score.flags))

    def test_rank_orders_and_excludes_failures(self):
        good = listing(external_id="g", description="Seller financing available, manager-run")
        bad = listing(external_id="b", seller_financing=SellerFinancing.REFUSED)
        ranked = rank([bad, good])
        self.assertEqual(len(ranked), 1)
        self.assertEqual(ranked[0][0].external_id, "g")


if __name__ == "__main__":
    unittest.main()
