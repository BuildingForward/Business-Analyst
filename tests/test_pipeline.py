"""End-to-end pipeline behaviour, including the money-saving guards."""

import tempfile
import unittest
from pathlib import Path

from business_analyst.analysis.engine import Analyst
from business_analyst.analysis.llm import Client, EchoProvider
from business_analyst.models import DealStage, Listing
from business_analyst.pipeline import Pipeline, PipelineConfig
from business_analyst.reporting import render_loi, render_memo
from business_analyst.sources.base import Source, SourceRegistry
from business_analyst.store import DealStore

CSV_BODY = """Business Name,Industry,Asking Price,Cash Flow,Established,Seller Financing,Description
Sparkle Cleaning,commercial cleaning,450000,185000,2005,Yes,Manager-run with recurring contracts
Gulf HVAC,hvac,1200000,410000,1998,Yes,Service agreements in place
Bayside Cafe,restaurant,395000,88000,2019,No,Cash only sale
"""


class FakeSource(Source):
    """Serves a fixed set of listings without touching the network."""

    def __init__(self, listings, name="fake"):
        self._listings = listings
        self.name = name

    def fetch(self, limit=50):
        return self._listings[:limit]


def sample_listings():
    return [
        Listing(source="fake", external_id="1", name="Sparkle Cleaning",
                industry="commercial cleaning", asking_price=450_000, cash_flow=185_000,
                established_year=2005, description="Manager-run, recurring. Seller financing."),
        Listing(source="fake", external_id="2", name="Gulf HVAC", industry="hvac",
                asking_price=1_200_000, cash_flow=410_000, established_year=1998,
                description="Service agreements. Owner financing available."),
        Listing(source="fake", external_id="3", name="Bayside Cafe", industry="restaurant",
                asking_price=395_000, cash_flow=88_000, established_year=2019,
                description="Cash only."),
    ]


class PipelineTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.out = Path(self.dir.name) / "reports"
        self.store = DealStore(":memory:")
        self.registry = SourceRegistry()
        self.registry.register(FakeSource(sample_listings()))
        self.client = Client(provider=EchoProvider())
        self.pipeline = Pipeline(
            store=self.store,
            registry=self.registry,
            analyst=Analyst(self.client),
            config=PipelineConfig(output_dir=self.out, playbooks=["swot"]),
        )

    def tearDown(self):
        self.dir.cleanup()


class TestStages(PipelineTestCase):
    def test_discover_returns_everything_when_store_is_empty(self):
        self.assertEqual(len(self.pipeline.discover()), 3)

    def test_discover_drops_known_deals(self):
        self.pipeline.screen(self.pipeline.discover())
        self.assertEqual(self.pipeline.discover(), [])

    def test_discover_drops_duplicates_within_one_batch(self):
        dupes = sample_listings() + sample_listings()
        self.assertEqual(len(self.pipeline.discover(listings=dupes)), 3)

    def test_discover_can_reuse_a_fetch(self):
        fetched = self.pipeline.fetch()
        self.assertEqual(len(self.pipeline.discover(listings=fetched)), 3)

    def test_screen_splits_passes_from_rejects(self):
        passed = self.pipeline.screen(self.pipeline.discover())
        self.assertEqual(len(passed), 2)
        self.assertNotIn("Bayside Cafe", [r.listing.name for r in passed])
        self.assertEqual(self.store.counts()["rejected"], 1)

    def test_screen_ranks_best_first(self):
        passed = self.pipeline.screen(self.pipeline.discover())
        scores = [r.score.total for r in passed]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_rejected_deals_get_no_offer(self):
        self.pipeline.screen(self.pipeline.discover())
        rejected = [r for r in self.store.top() if r[3] == DealStage.REJECTED.value]
        self.assertTrue(rejected)
        self.assertIsNone(self.store.get(rejected[0][0]).offer)


class TestSpendControl(PipelineTestCase):
    def test_threshold_keeps_weak_deals_away_from_the_model(self):
        self.pipeline.config.analysis_threshold = 99.0
        passed = self.pipeline.screen(self.pipeline.discover())
        self.assertEqual(self.pipeline.analyze(passed), [])
        self.assertEqual(self.client.calls_made, 0)

    def test_per_run_cap_is_honoured(self):
        self.pipeline.config.max_analyses_per_run = 1
        passed = self.pipeline.screen(self.pipeline.discover())
        self.assertEqual(len(self.pipeline.analyze(passed)), 1)

    def test_already_analysed_deals_are_skipped(self):
        passed = self.pipeline.screen(self.pipeline.discover())
        self.pipeline.analyze(passed)
        calls = self.client.calls_made
        self.pipeline.analyze(passed)
        self.assertEqual(self.client.calls_made, calls, "must not pay twice for one deal")

    def test_analysis_is_skipped_without_an_analyst(self):
        self.pipeline.analyst = None
        passed = self.pipeline.screen(self.pipeline.discover())
        self.assertEqual(self.pipeline.analyze(passed), [])


class TestFullRun(PipelineTestCase):
    def test_run_once_produces_stats_and_files(self):
        stats = self.pipeline.run_once()
        self.assertEqual(stats.discovered, 3)
        self.assertEqual(stats.new, 3)
        self.assertEqual(stats.screened, 2)
        self.assertEqual(stats.rejected, 1)
        self.assertEqual(stats.analyzed, 2)
        self.assertEqual(stats.errors, [])
        self.assertTrue((self.out / "shortlist.md").exists())
        self.assertTrue(list(self.out.glob("*-LOI.md")), "viable deals get an LOI")

    def test_second_run_finds_nothing_new_and_spends_nothing(self):
        self.pipeline.run_once()
        calls = self.client.calls_made
        second = self.pipeline.run_once()
        self.assertEqual(second.new, 0)
        self.assertEqual(second.analyzed, 0)
        self.assertEqual(self.client.calls_made, calls)

    def test_a_broken_source_does_not_crash_the_run(self):
        class Boom(Source):
            name = "boom"

            def fetch(self, limit=50):
                raise RuntimeError("down")

        self.registry.register(Boom())
        with self.assertLogs("business_analyst.sources.base", level="ERROR"):
            stats = self.pipeline.run_once()
        self.assertEqual(stats.screened, 2, "the healthy source still delivers")

    def test_run_forever_stops_after_max_cycles(self):
        history = self.pipeline.run_forever(interval_seconds=0, max_cycles=2)
        self.assertEqual(len(history), 2)
        self.assertEqual(history[1].new, 0)

    def test_screener_and_pipeline_underwrite_identically(self):
        """A deal must never pass screening on terms its own memo disagrees with."""
        self.assertIs(self.pipeline.config.screen.structure, self.pipeline.config.structure)


class TestReporting(PipelineTestCase):
    def test_memo_contains_the_decisive_numbers(self):
        report = self.pipeline.screen(self.pipeline.discover())[0]
        memo = render_memo(report)
        self.assertIn("Cash at close", memo)
        self.assertIn("$0", memo, "zero cash at close must render as a number, not 'n/a'")
        self.assertIn("Stress test", memo)
        self.assertIn("Breakeven", memo)
        self.assertIn("Score breakdown", memo)

    def test_memo_includes_analysis_when_present(self):
        report = self.pipeline.analyze(self.pipeline.screen(self.pipeline.discover()))[0]
        self.assertIn("## Analysis", render_memo(report))

    def test_loi_is_non_binding_and_carries_the_terms(self):
        report = self.pipeline.screen(self.pipeline.discover())[0]
        loi = render_loi(report, buyer_name="Building Forward LLC")
        self.assertIn("not binding", loi)
        self.assertIn("Building Forward LLC", loi)
        self.assertIn("Seller Financing", loi)
        self.assertIn("attorney", loi)

    def test_loi_requires_an_offer(self):
        from business_analyst.models import DealReport

        with self.assertRaises(ValueError):
            render_loi(DealReport(listing=sample_listings()[0]))


class TestCliSmoke(unittest.TestCase):
    def test_screen_command_runs_end_to_end(self):
        from business_analyst.cli import main

        with tempfile.TemporaryDirectory() as d:
            csv_path = Path(d) / "listings.csv"
            csv_path.write_text(CSV_BODY)
            code = main([
                "--db", str(Path(d) / "deals.db"), "--out", str(Path(d) / "reports"),
                "screen", "--source", str(csv_path),
            ])
        self.assertEqual(code, 0)

    def test_run_command_with_echo_provider(self):
        from business_analyst.cli import main

        with tempfile.TemporaryDirectory() as d:
            csv_path = Path(d) / "listings.csv"
            csv_path.write_text(CSV_BODY)
            out = Path(d) / "reports"
            code = main([
                "--db", str(Path(d) / "deals.db"), "--out", str(out),
                "run", "--source", str(csv_path), "--provider", "echo",
                "--cache-dir", str(Path(d) / "cache"),
            ])
            self.assertEqual(code, 0)
            self.assertTrue((out / "shortlist.md").exists())

    def test_missing_source_is_an_error(self):
        from business_analyst.cli import main

        self.assertEqual(main(["screen"]), 2)


if __name__ == "__main__":
    unittest.main()
