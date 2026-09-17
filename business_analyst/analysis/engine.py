"""Runs the analyst playbooks against a listing."""

from __future__ import annotations

import logging
from typing import Iterable, List, Optional

from ..models import AnalysisSection, DealReport, DealStage, Listing, OfferStructure
from . import prompts
from .llm import BudgetExhausted, Client

log = logging.getLogger(__name__)


class Analyst:
    """Turns a screened listing into a written analysis."""

    def __init__(self, client: Client, system_prompt: str = prompts.SYSTEM_PROMPT):
        self.client = client
        self.system_prompt = system_prompt

    def run_playbook(
        self,
        key: str,
        listing: Listing,
        offer: Optional[OfferStructure] = None,
        **overrides,
    ) -> AnalysisSection:
        prompt = prompts.render(key, listing, offer, **overrides)
        response = self.client.complete(self.system_prompt, prompt)
        book = prompts.PLAYBOOKS[key]
        return AnalysisSection(
            key=key,
            title=book.title,
            content=response.text,
            model=response.model,
            tokens_in=response.tokens_in,
            tokens_out=response.tokens_out,
        )

    def analyze(
        self,
        listing: Listing,
        playbooks: Optional[Iterable[str]] = None,
        offer: Optional[OfferStructure] = None,
        report: Optional[DealReport] = None,
    ) -> DealReport:
        """Run the requested playbooks, tolerating individual failures.

        One playbook erroring must not lose the others; a spent budget stops
        the run cleanly with whatever was already produced.
        """
        keys = list(playbooks or prompts.PLAYBOOK_ORDER)
        report = report or DealReport(listing=listing)
        sections: List[AnalysisSection] = list(report.sections)
        done = {s.key for s in sections}

        for key in keys:
            if key in done:
                log.debug("skipping %s; already present", key)
                continue
            try:
                section = self.run_playbook(key, listing, offer)
            except BudgetExhausted:
                log.warning("Budget spent after %d sections on %s", len(sections), listing.name)
                break
            except Exception as exc:  # noqa: BLE001 - one bad section must not lose the rest
                log.error("Playbook %s failed for %s: %s", key, listing.name, exc)
                sections.append(
                    AnalysisSection(
                        key=key,
                        title=prompts.PLAYBOOKS[key].title,
                        content=f"_Analysis failed: {exc}_",
                        model="error",
                    )
                )
                continue
            sections.append(section)

        report.sections = sections
        report.offer = offer or report.offer
        if sections:
            report.stage = DealStage.ANALYZED
        return report
