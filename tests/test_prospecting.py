"""Off-market prospecting: Google Places, estimation, scoring and handoff."""

import json
import tempfile
import unittest
from pathlib import Path

from business_analyst.models import Estimate, OutreachStatus, Prospect, ProspectReport
from business_analyst.outreach import (
    STANDING_CONSTRAINTS,
    build_brief,
    export_csv,
    export_json,
    personalization_hooks,
)
from business_analyst.prospecting_pipeline import ProspectPipeline, ProspectPipelineConfig
from business_analyst.screening.finance import StructureParams
from business_analyst.screening.prospecting import (
    ProspectConfig,
    estimate_financials,
    estimate_staff,
    rank_prospects,
    score_prospect,
)
from business_analyst.sources.base import SourceRegistry, looks_like_chain
from business_analyst.sources.google_places import (
    FIELD_MASK,
    GooglePlacesSource,
    RequestBudgetExceeded,
    grid_points,
    place_to_prospect,
)
from business_analyst.sources.local import CsvProspectSource
from business_analyst.store import DealStore


def prospect(**kwargs) -> Prospect:
    base = dict(
        source="google-places", external_id="ChIJ1", name="Bay Area Plumbing",
        industry="plumbing", city="Tampa", state="FL", phone="(813) 555-0100",
        established_year=1994, review_count=80,
    )
    base.update(kwargs)
    return Prospect(**base)


PLACE = {
    "id": "ChIJabc",
    "displayName": {"text": "Bay Area Plumbing"},
    "formattedAddress": "412 W Kennedy Blvd, Tampa, FL 33606",
    "nationalPhoneNumber": "(813) 555-0100",
    "websiteUri": "https://example.test",
    "businessStatus": "OPERATIONAL",
    "userRatingCount": 80,
    "location": {"latitude": 27.94, "longitude": -82.47},
    "addressComponents": [
        {"types": ["locality"], "shortText": "Tampa"},
        {"types": ["administrative_area_level_1"], "shortText": "FL"},
        {"types": ["postal_code"], "shortText": "33606"},
    ],
}


class TestGridTiling(unittest.TestCase):
    def test_grid_covers_the_area(self):
        points = grid_points((27.95, -82.46), radius_km=8, step_km=2.5)
        self.assertGreater(len(points), 20, "an 8km radius needs many 2.5km tiles")

    def test_every_point_is_inside_the_radius(self):
        import math

        center = (27.95, -82.46)
        for lat, lon in grid_points(center, radius_km=5, step_km=2.0):
            north = (lat - center[0]) * 110.574
            east = (lon - center[1]) * 111.320 * math.cos(math.radians(lat))
            self.assertLessEqual(math.hypot(north, east), 5.001)

    def test_tiny_radius_still_searches_the_centre(self):
        self.assertEqual(len(grid_points((27.95, -82.46), radius_km=0.1, step_km=5)), 1)


class TestPlaceMapping(unittest.TestCase):
    def test_maps_the_fields_outreach_needs(self):
        p = place_to_prospect(PLACE)
        self.assertEqual(p.name, "Bay Area Plumbing")
        self.assertEqual(p.city, "Tampa")
        self.assertEqual(p.state, "FL")
        self.assertEqual(p.phone, "(813) 555-0100")
        self.assertEqual(p.review_count, 80)
        self.assertTrue(p.has_contact)

    def test_closed_businesses_are_dropped(self):
        closed = dict(PLACE, businessStatus="CLOSED_PERMANENTLY")
        self.assertIsNone(place_to_prospect(closed))

    def test_nameless_result_is_dropped(self):
        self.assertIsNone(place_to_prospect({"id": "x", "businessStatus": "OPERATIONAL"}))


class FakeTransport:
    """Stands in for Google so tests never touch the network."""

    def __init__(self, places=None):
        self.calls = []
        self.places = places if places is not None else [PLACE]

    def __call__(self, url, payload, headers):
        self.calls.append({"url": url, "payload": payload, "headers": headers})
        return {"places": self.places}


class TestGooglePlacesSource(unittest.TestCase):
    def test_api_key_is_required(self):
        with self.assertRaises(ValueError):
            GooglePlacesSource(api_key="")

    def test_field_mask_is_sent_on_every_request(self):
        transport = FakeTransport()
        source = GooglePlacesSource(
            api_key="k", center=(27.95, -82.46), radius_km=1, step_km=1,
            industries=["plumbing"], transport=transport,
        )
        list(source.fetch())
        self.assertTrue(transport.calls)
        for call in transport.calls:
            self.assertEqual(call["headers"]["X-Goog-FieldMask"], FIELD_MASK)
            self.assertEqual(call["headers"]["X-Goog-Api-Key"], "k")

    def test_request_budget_is_enforced(self):
        transport = FakeTransport()
        source = GooglePlacesSource(
            api_key="k", center=(27.95, -82.46), radius_km=10, step_km=1,
            industries=["plumbing"], max_requests=3, transport=transport,
        )
        list(source.fetch())
        self.assertLessEqual(source.requests_made, 3, "must not exceed the cap")

    def test_budget_exhaustion_raises_from_the_raw_call(self):
        source = GooglePlacesSource(
            api_key="k", center=(0, 0), max_requests=0, transport=FakeTransport()
        )
        with self.assertRaises(RequestBudgetExceeded):
            source.search_nearby((0, 0), 1000, ["plumber"])

    def test_cache_prevents_a_second_billable_call(self):
        with tempfile.TemporaryDirectory() as d:
            transport = FakeTransport()
            kwargs = dict(
                api_key="k", center=(27.95, -82.46), radius_km=0.1, step_km=1,
                industries=["plumbing"], cache_dir=Path(d), transport=transport,
            )
            list(GooglePlacesSource(**kwargs).fetch())
            first = len(transport.calls)
            second_source = GooglePlacesSource(**kwargs)
            list(second_source.fetch())
            self.assertEqual(len(transport.calls), first, "cached run must not call Google")
            self.assertEqual(second_source.requests_made, 0)

    def test_results_are_deduplicated_across_tiles(self):
        transport = FakeTransport()
        source = GooglePlacesSource(
            api_key="k", center=(27.95, -82.46), radius_km=4, step_km=2,
            industries=["plumbing"], transport=transport,
        )
        results = list(source.fetch())
        self.assertGreater(len(transport.calls), 1, "several tiles queried")
        self.assertEqual(len(results), 1, "the same place id collapses to one prospect")

    def test_chains_are_excluded_by_default(self):
        chain = dict(PLACE, id="c1", displayName={"text": "Roto-Rooter Plumbing"})
        source = GooglePlacesSource(
            api_key="k", center=(27.95, -82.46), radius_km=0.1, step_km=1,
            industries=["plumbing"], transport=FakeTransport([chain]),
        )
        self.assertEqual(list(source.fetch()), [])

    def test_missing_center_and_area_is_an_error(self):
        source = GooglePlacesSource(api_key="k", transport=FakeTransport())
        with self.assertRaises(ValueError):
            list(source.fetch())


class TestChainDetection(unittest.TestCase):
    def test_known_brands(self):
        self.assertTrue(looks_like_chain("Roto-Rooter Plumbing Tampa"))
        self.assertTrue(looks_like_chain("Jiffy Lube #4412"))

    def test_franchise_language_in_context(self):
        self.assertTrue(looks_like_chain("Generic Cleaners", "Franchise location"))

    def test_independents_pass(self):
        self.assertFalse(looks_like_chain("Bay Area Plumbing", "Family-owned since 1994"))

    def test_applies_to_csv_sources_too(self):
        """Chain filtering must not be specific to whichever source implemented it."""
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "p.csv"
            path.write_text(
                "Business Name,Industry,Phone,Notes\n"
                "Roto-Rooter Plumbing Tampa,plumbing,555,Franchise location\n"
            )
            found = list(CsvProspectSource(path).fetch())
        self.assertTrue(found[0].is_chain)


class TestEstimation(unittest.TestCase):
    def test_reported_headcount_wins(self):
        self.assertEqual(estimate_staff(prospect(employees=12)), 12)

    def test_review_volume_bands_the_size(self):
        small = estimate_staff(prospect(employees=None, review_count=3))
        large = estimate_staff(prospect(employees=None, review_count=400))
        self.assertLess(small, large)

    def test_no_signal_means_no_estimate(self):
        est = estimate_financials(prospect(employees=None, review_count=None))
        self.assertFalse(est.known)
        self.assertIn("unknown", est.basis)

    def test_estimate_uses_industry_priors(self):
        est = estimate_financials(prospect(industry="hvac", employees=10))
        self.assertEqual(est.revenue, 1_800_000)   # 10 x 180k
        self.assertEqual(est.sde, 270_000)         # x 15%
        self.assertIn("hvac", est.basis)

    def test_reported_headcount_is_more_confident(self):
        self.assertEqual(estimate_financials(prospect(employees=10)).confidence, "medium")
        self.assertEqual(
            estimate_financials(prospect(employees=None, review_count=80)).confidence, "low"
        )

    def test_indicative_price_brackets_the_estimate(self):
        est = estimate_financials(prospect(industry="hvac", employees=10))
        self.assertLess(est.indicative_price_low, est.indicative_price_high)


class TestProspectScoring(unittest.TestCase):
    def test_a_long_held_independent_qualifies(self):
        score = score_prospect(prospect(industry="hvac", employees=10))
        self.assertTrue(score.passed)
        self.assertGreater(score.total, 55)

    def test_chain_is_a_hard_fail(self):
        score = score_prospect(prospect(is_chain=True, employees=10))
        self.assertFalse(score.passed)
        self.assertTrue(any("chain" in f.lower() for f in score.hard_fails))

    def test_too_small_to_pay_the_operator_is_a_hard_fail(self):
        cfg = ProspectConfig(structure=StructureParams(buyer_salary=150_000))
        score = score_prospect(prospect(industry="landscaping", employees=3), cfg)
        self.assertFalse(score.passed)
        self.assertTrue(any("operator salary" in f for f in score.hard_fails))

    def test_too_young_is_a_hard_fail(self):
        score = score_prospect(prospect(established_year=2022, employees=10))
        self.assertFalse(score.passed)

    def test_blocked_industry_is_a_hard_fail(self):
        score = score_prospect(prospect(industry="dental practice", employees=10))
        self.assertFalse(score.passed)

    def test_uncontactable_is_a_hard_fail(self):
        bare = Prospect(source="g", external_id="x", name="Ghost Co", industry="hvac",
                        employees=10, established_year=1990)
        self.assertFalse(score_prospect(bare).passed)

    def test_missing_email_is_flagged_not_fatal(self):
        score = score_prospect(prospect(industry="hvac", employees=10, email=""))
        self.assertTrue(score.passed)
        self.assertTrue(any("enrichment" in f for f in score.flags))

    def test_older_business_outscores_younger(self):
        old = score_prospect(prospect(industry="hvac", employees=10, established_year=1985))
        new = score_prospect(prospect(industry="hvac", employees=10, established_year=2012))
        self.assertGreater(old.total, new.total)

    def test_rank_excludes_failures(self):
        good = prospect(external_id="g", industry="hvac", employees=10)
        bad = prospect(external_id="b", is_chain=True, employees=10)
        ranked = rank_prospects([bad, good])
        self.assertEqual(len(ranked), 1)
        self.assertEqual(ranked[0][0].external_id, "g")


class TestHandoff(unittest.TestCase):
    def _report(self, **kwargs):
        p = prospect(**kwargs)
        est = estimate_financials(p)
        return ProspectReport(prospect=p, estimate=est, score=score_prospect(p, estimate=est))

    def test_brief_namespaces_estimates_and_carries_constraints(self):
        brief = build_brief(self._report(industry="hvac", employees=10))
        self.assertIn("MODELLED", brief.estimates["note"])
        self.assertEqual(brief.do_not_claim, STANDING_CONSTRAINTS)
        self.assertTrue(any("not disclosed" in c.lower() for c in brief.do_not_claim))

    def test_email_status_is_explicit(self):
        self.assertEqual(
            build_brief(self._report(email="a@b.test")).contact["email_status"], "present"
        )
        self.assertEqual(
            build_brief(self._report(email="", website="https://x.test")).contact["email_status"],
            "missing_enrichable_from_website",
        )
        self.assertEqual(
            build_brief(self._report(email="", website="")).contact["email_status"],
            "missing_no_route",
        )

    def test_hooks_contain_only_verifiable_facts(self):
        """Nothing modelled may appear as a personalization hook."""
        report = self._report(industry="hvac", employees=10)
        hooks = " ".join(personalization_hooks(report.prospect, report.estimate))
        self.assertIn("Trading 32 years", hooks)
        self.assertNotIn("1,800,000", hooks)
        self.assertNotIn("SDE", hooks)

    def test_json_export_shape(self):
        with tempfile.TemporaryDirectory() as d:
            path = export_json([self._report(industry="hvac", employees=10)], Path(d) / "p.json")
            payload = json.loads(path.read_text())
        self.assertEqual(payload["count"], 1)
        self.assertIn("schema_version", payload)
        self.assertIn("standing_constraints", payload)
        self.assertIn("prospect_id", payload["prospects"][0])

    def test_csv_export_prefixes_estimated_columns(self):
        with tempfile.TemporaryDirectory() as d:
            path = export_csv([self._report(industry="hvac", employees=10)], Path(d) / "p.csv")
            header = path.read_text().splitlines()[0]
        for column in ("est_sde", "est_revenue", "est_price_low"):
            self.assertIn(column, header)
        self.assertIn("email_status", header)


class TestProspectPipeline(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.out = Path(self.dir.name) / "handoff"
        self.store = DealStore(":memory:")
        self.registry = SourceRegistry()
        self.registry.register(CsvProspectSource("data/sample_prospects.csv", name="csv"))
        self.pipeline = ProspectPipeline(
            self.store, self.registry, ProspectPipelineConfig(output_dir=self.out)
        )

    def tearDown(self):
        self.dir.cleanup()

    def test_run_qualifies_and_hands_off(self):
        stats = self.pipeline.run_once()
        self.assertEqual(stats.pulled, 9)
        self.assertGreater(stats.qualified, 0)
        self.assertEqual(stats.exported, stats.qualified)
        self.assertTrue((self.out / "prospects.json").exists())
        self.assertTrue((self.out / "prospects.csv").exists())
        self.assertTrue((self.out / "README.md").exists())

    def test_hard_failed_prospects_never_reach_the_handoff(self):
        """Regression: a rejected prospect scoring well must not be exported.

        A franchise or an excluded industry can out-score a good target on
        the weighted factors; emailing one is worse than emailing nobody.
        """
        self.pipeline.run_once()
        payload = json.loads((self.out / "prospects.json").read_text())
        names = {p["name"] for p in payload["prospects"]}
        self.assertNotIn("Roto-Rooter Plumbing Tampa", names)
        self.assertNotIn("Harborview Family Dentistry", names)
        self.assertNotIn("Joe's Handyman Service", names)

    def test_second_run_hands_off_nothing(self):
        self.pipeline.run_once()
        second = self.pipeline.run_once()
        self.assertEqual(second.new, 0)
        self.assertEqual(second.exported, 0, "no duplicate approach to the same owner")

    def test_status_feedback_round_trips(self):
        self.pipeline.run_once()
        report = self.store.top_prospects(limit=1)[0]
        pid = report.prospect.prospect_id
        self.store.set_prospect_status(pid, OutreachStatus.REPLIED)
        self.assertEqual(self.store.get_prospect(pid).status, OutreachStatus.REPLIED)

    def test_broken_source_does_not_lose_the_run(self):
        class Boom:
            name = "boom"

            def fetch(self, limit=50):
                raise RuntimeError("google is down")

        self.registry.register(Boom())
        with self.assertLogs("business_analyst.sources.base", level="ERROR"):
            stats = self.pipeline.run_once()
        self.assertGreater(stats.qualified, 0)


class TestProspectCli(unittest.TestCase):
    def test_prospect_command_runs_without_google(self):
        from business_analyst.cli import main

        with tempfile.TemporaryDirectory() as d:
            code = main([
                "--db", str(Path(d) / "deals.db"),
                "prospect", "--no-google",
                "--source", "data/sample_prospects.csv",
                "--handoff-dir", str(Path(d) / "handoff"),
            ])
            self.assertEqual(code, 0)
            self.assertTrue((Path(d) / "handoff" / "prospects.json").exists())

    def test_place_types_command(self):
        from business_analyst.cli import main

        self.assertEqual(main(["place-types"]), 0)


if __name__ == "__main__":
    unittest.main()
