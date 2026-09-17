"""Prompts, the LLM client's guards, and the analyst's failure handling."""

import tempfile
import unittest
from pathlib import Path

from business_analyst.analysis import prompts
from business_analyst.analysis.engine import Analyst
from business_analyst.analysis.llm import (
    BudgetExhausted,
    Client,
    DryRunProvider,
    EchoProvider,
    LLMResponse,
    ResponseCache,
    build_provider,
)
from business_analyst.models import Listing
from business_analyst.screening.finance import build_offer


def listing() -> Listing:
    return Listing(
        source="t", external_id="1", name="Sparkle Cleaning", industry="commercial cleaning",
        location="Tampa, FL", asking_price=450_000, revenue=980_000, cash_flow=185_000,
        established_year=2005, description="Manager-run with recurring contracts.",
    )


class TestPrompts(unittest.TestCase):
    def test_all_eight_playbooks_exist(self):
        self.assertEqual(len(prompts.PLAYBOOK_ORDER), 8)
        self.assertEqual(set(prompts.PLAYBOOK_ORDER), set(prompts.PLAYBOOKS))

    def test_every_playbook_renders_without_placeholders(self):
        l = listing()
        for key in prompts.PLAYBOOK_ORDER:
            text = prompts.render(key, l)
            self.assertNotIn("{", text, f"{key} left an unfilled placeholder")
            self.assertIn("Sparkle Cleaning", text)
            self.assertGreater(len(text), 400)

    def test_unknown_playbook_raises(self):
        with self.assertRaises(KeyError):
            prompts.render("does_not_exist", listing())

    def test_brief_reports_undisclosed_fields_honestly(self):
        bare = Listing(source="t", external_id="2", name="Mystery Co")
        brief = prompts.build_brief(bare)
        self.assertIn("not disclosed", brief)

    def test_brief_includes_the_structure_when_given(self):
        l = listing()
        brief = prompts.build_brief(l, offer=build_offer(l))
        self.assertIn("cash at close", brief)

    def test_zero_cash_at_close_is_stated_not_hidden(self):
        """$0 down is the thesis; the model must never be told it is undisclosed."""
        l = listing()
        brief = prompts.build_brief(l, offer=build_offer(l))
        self.assertIn("$0 cash at close", brief)
        self.assertNotIn("not disclosed cash at close", brief)

    def test_money_formatter_separates_zero_from_unknown(self):
        self.assertEqual(prompts.format_money(0), "$0")
        self.assertEqual(prompts.format_money(None), "not disclosed")

    def test_overrides_win(self):
        text = prompts.render("competitor_teardown", listing(), competitors="Alpha, Beta, Gamma")
        self.assertIn("Alpha, Beta, Gamma", text)

    def test_system_prompt_demands_evidence(self):
        self.assertIn("INFERRING", prompts.SYSTEM_PROMPT)
        self.assertIn("Do not fabricate", prompts.SYSTEM_PROMPT)

    def test_triage_set_is_a_real_subset(self):
        self.assertTrue(set(prompts.TRIAGE_SET).issubset(set(prompts.PLAYBOOK_ORDER)))


class TestProviders(unittest.TestCase):
    def test_build_provider_by_name(self):
        self.assertIsInstance(build_provider("echo"), EchoProvider)
        self.assertIsInstance(build_provider("dry-run"), DryRunProvider)

    def test_unknown_provider_raises(self):
        with self.assertRaises(ValueError):
            build_provider("gpt-whatever")

    def test_dry_run_calls_nothing_but_shows_the_prompt(self):
        out = DryRunProvider().complete("sys", "the actual prompt")
        self.assertIn("dry run", out.text)
        self.assertIn("the actual prompt", out.text)


class TestClientGuards(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.dir.cleanup()

    def test_budget_is_enforced(self):
        client = Client(provider=EchoProvider(), max_calls=2)
        client.complete("s", "one")
        client.complete("s", "two")
        with self.assertRaises(BudgetExhausted):
            client.complete("s", "three")

    def test_cache_prevents_a_second_call(self):
        client = Client(provider=EchoProvider(), cache=ResponseCache(Path(self.dir.name)))
        first = client.complete("s", "same prompt")
        second = client.complete("s", "same prompt")
        self.assertFalse(first.cached)
        self.assertTrue(second.cached)
        self.assertEqual(first.text, second.text)
        self.assertEqual(client.calls_made, 1, "a cache hit must not spend a call")

    def test_cache_misses_on_a_different_prompt(self):
        client = Client(provider=EchoProvider(), cache=ResponseCache(Path(self.dir.name)))
        client.complete("s", "prompt one")
        client.complete("s", "prompt two")
        self.assertEqual(client.calls_made, 2)

    def test_cached_hit_does_not_consume_budget(self):
        client = Client(provider=EchoProvider(), cache=ResponseCache(Path(self.dir.name)),
                        max_calls=1)
        client.complete("s", "p")
        self.assertEqual(client.complete("s", "p").cached, True)

    def test_corrupt_cache_entry_is_ignored(self):
        cache = ResponseCache(Path(self.dir.name))
        key = cache.key("echo", "s", "p")
        (Path(self.dir.name) / f"{key}.json").write_text("{not json")
        self.assertIsNone(cache.get(key))

    def test_token_accounting(self):
        client = Client(provider=EchoProvider())
        client.complete("s", "x" * 400)
        self.assertGreater(client.tokens_in, 0)
        self.assertGreater(client.tokens_out, 0)


class TestAnalyst(unittest.TestCase):
    def test_runs_requested_playbooks(self):
        report = Analyst(Client(provider=EchoProvider())).analyze(listing(), ["swot", "gtm"])
        self.assertEqual([s.key for s in report.sections], ["swot", "gtm"])
        self.assertEqual(report.stage.value, "analyzed")

    def test_one_failing_playbook_does_not_lose_the_others(self):
        class Flaky:
            name = "flaky"
            model = "flaky"
            calls = 0

            def complete(self, system, prompt):
                Flaky.calls += 1
                if Flaky.calls == 1:
                    raise RuntimeError("model exploded")
                return LLMResponse(text="fine", model="flaky")

        with self.assertLogs("business_analyst.analysis.engine", level="ERROR"):
            report = Analyst(Client(provider=Flaky(), retries=0)).analyze(
                listing(), ["swot", "gtm"]
            )
        self.assertEqual(len(report.sections), 2)
        self.assertIn("Analysis failed", report.sections[0].content)
        self.assertEqual(report.sections[1].content, "fine")

    def test_exhausted_budget_stops_cleanly_keeping_what_was_produced(self):
        analyst = Analyst(Client(provider=EchoProvider(), max_calls=1))
        report = analyst.analyze(listing(), ["swot", "gtm", "full_analysis"])
        self.assertEqual(len(report.sections), 1, "stops at the budget, keeps the work done")

    def test_existing_sections_are_not_regenerated(self):
        from business_analyst.models import AnalysisSection, DealReport

        existing = DealReport(
            listing=listing(),
            sections=[AnalysisSection(key="swot", title="SWOT", content="already done")],
        )
        client = Client(provider=EchoProvider())
        report = Analyst(client).analyze(listing(), ["swot", "gtm"], report=existing)
        self.assertEqual(client.calls_made, 1, "swot should be skipped")
        self.assertEqual(report.section("swot").content, "already done")


if __name__ == "__main__":
    unittest.main()
