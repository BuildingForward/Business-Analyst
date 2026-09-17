"""Persistence, dedupe and round-tripping."""

import unittest

from business_analyst.models import DealReport, DealStage, Listing
from business_analyst.screening.finance import build_offer
from business_analyst.screening.scoring import score_listing
from business_analyst.store import DealStore


def report_for(**kwargs) -> DealReport:
    base = dict(
        source="broker", external_id="1", name="Sparkle Cleaning",
        industry="commercial cleaning", asking_price=450_000, cash_flow=185_000,
        established_year=2005,
    )
    base.update(kwargs)
    listing = Listing(**base)
    return DealReport(
        listing=listing, score=score_listing(listing), offer=build_offer(listing),
        stage=DealStage.SCREENED,
    )


class TestStore(unittest.TestCase):
    def setUp(self):
        self.store = DealStore(":memory:")

    def test_upsert_and_get_roundtrip(self):
        report = report_for()
        deal_id = self.store.upsert(report)
        loaded = self.store.get(deal_id)

        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.listing.name, "Sparkle Cleaning")
        self.assertEqual(loaded.listing.asking_price, 450_000)
        self.assertAlmostEqual(loaded.score.total, report.score.total)
        self.assertEqual(len(loaded.score.components), len(report.score.components))
        self.assertAlmostEqual(loaded.offer.dscr, report.offer.dscr)
        self.assertEqual(len(loaded.offer.notes), 1, "notes must survive the round trip")
        self.assertAlmostEqual(loaded.offer.notes[0].principal, report.offer.notes[0].principal)

    def test_missing_deal_returns_none(self):
        self.assertIsNone(self.store.get("nope"))

    def test_upsert_is_idempotent(self):
        report = report_for()
        self.store.upsert(report)
        self.store.upsert(report)
        self.assertEqual(self.store.counts()["total"], 1)

    def test_exists_on_same_source_id(self):
        report = report_for()
        self.store.upsert(report)
        self.assertTrue(self.store.exists(report.listing))

    def test_exists_catches_the_same_business_from_another_broker(self):
        self.store.upsert(report_for())
        duplicate = Listing(
            source="other-broker", external_id="99", name="Sparkle Cleaning",
            industry="commercial cleaning", asking_price=450_000,
        )
        self.assertTrue(self.store.exists(duplicate), "fingerprint should collapse duplicates")

    def test_different_business_is_not_a_duplicate(self):
        self.store.upsert(report_for())
        other = Listing(source="broker", external_id="2", name="Totally Different Co",
                        asking_price=999_000)
        self.assertFalse(self.store.exists(other))

    def test_sections_are_stored_and_updated(self):
        from business_analyst.models import AnalysisSection

        report = report_for()
        report.sections = [AnalysisSection(key="swot", title="SWOT", content="first")]
        deal_id = self.store.upsert(report)
        self.assertTrue(self.store.has_analysis(deal_id))

        report.sections = [AnalysisSection(key="swot", title="SWOT", content="second")]
        self.store.upsert(report)
        loaded = self.store.get(deal_id)
        self.assertEqual(len(loaded.sections), 1, "same key must update, not duplicate")
        self.assertEqual(loaded.sections[0].content, "second")

    def test_pending_analysis_excludes_analysed_deals(self):
        from business_analyst.models import AnalysisSection

        self.store.upsert(report_for(external_id="1", name="Alpha Co"))
        done = report_for(external_id="2", name="Beta Co")
        done.sections = [AnalysisSection(key="swot", title="SWOT", content="x")]
        self.store.upsert(done)

        pending = self.store.pending_analysis()
        self.assertEqual([r.listing.name for r in pending], ["Alpha Co"])

    def test_pending_analysis_respects_min_score(self):
        self.store.upsert(report_for())
        self.assertEqual(self.store.pending_analysis(min_score=99.0), [])

    def test_top_reports_stage(self):
        self.store.upsert(report_for(external_id="1", name="Alpha Co"))
        rejected = report_for(external_id="2", name="Beta Co")
        rejected.stage = DealStage.REJECTED
        self.store.upsert(rejected)

        rows = self.store.top()
        self.assertEqual(len(rows), 2)
        self.assertEqual({r[3] for r in rows}, {"screened", "rejected"})
        self.assertEqual(len(self.store.top(stage=DealStage.REJECTED)), 1)

    def test_run_bookkeeping(self):
        run_id = self.store.start_run()
        self.store.finish_run(run_id, discovered=10, screened=4, analyzed=2, calls=6)
        with self.store.connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        self.assertEqual(row["discovered"], 10)
        self.assertEqual(row["calls"], 6)
        self.assertIsNotNone(row["finished_at"])


if __name__ == "__main__":
    unittest.main()
