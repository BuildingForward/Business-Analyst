"""Orchestration: discover, screen, analyse, report.

The expensive step is analysis, so the pipeline is built around spending that
budget on the best-scoring deals only, and never on the same deal twice.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional

from .analysis import prompts
from .analysis.engine import Analyst
from .analysis.llm import Client
from .models import DealReport, DealStage, Listing
from .reporting import render_loi, render_memo, render_shortlist
from .screening.finance import StructureParams, build_offer
from .screening.scoring import ScreenConfig, score_listing
from .sources.base import SourceRegistry
from .store import DealStore

log = logging.getLogger(__name__)


@dataclass
class RunStats:
    discovered: int = 0
    new: int = 0
    screened: int = 0
    rejected: int = 0
    analyzed: int = 0
    calls: int = 0
    errors: List[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{self.discovered} seen, {self.new} new, {self.screened} passed screening, "
            f"{self.rejected} rejected, {self.analyzed} analysed, {self.calls} model calls"
        )


@dataclass
class PipelineConfig:
    screen: ScreenConfig = field(default_factory=ScreenConfig)
    structure: StructureParams = field(default_factory=StructureParams)
    playbooks: List[str] = field(default_factory=lambda: list(prompts.TRIAGE_SET))
    #: Only deals scoring at least this are worth an LLM call.
    analysis_threshold: float = 55.0
    #: Cap on how many deals get analysed per run, independent of token budget.
    max_analyses_per_run: int = 5
    output_dir: Path = Path("reports")
    fetch_limit: int = 100

    def __post_init__(self) -> None:
        self.output_dir = Path(self.output_dir)
        # The screener and the pipeline must underwrite on identical terms,
        # otherwise a deal can pass screening and then fail its own memo.
        self.screen.structure = self.structure


class Pipeline:
    """One full pass of the bot."""

    def __init__(
        self,
        store: DealStore,
        registry: SourceRegistry,
        analyst: Optional[Analyst] = None,
        config: Optional[PipelineConfig] = None,
    ):
        self.store = store
        self.registry = registry
        self.analyst = analyst
        self.config = config or PipelineConfig()

    # ---- stages -------------------------------------------------------

    def fetch(self, sources: Optional[Iterable[str]] = None) -> List[Listing]:
        """Raw pull from the selected sources."""
        return self.registry.fetch(sources, limit=self.config.fetch_limit)

    def discover(
        self,
        sources: Optional[Iterable[str]] = None,
        listings: Optional[Iterable[Listing]] = None,
    ) -> List[Listing]:
        """Drop duplicates within the batch and anything the store already knows.

        Pass `listings` to reuse an existing fetch rather than hitting every
        source a second time.
        """
        batch = list(listings) if listings is not None else self.fetch(sources)
        fresh: List[Listing] = []
        for listing in _dedupe(batch):
            if self.store.exists(listing):
                log.debug("already known: %s", listing.name)
                continue
            fresh.append(listing)
        return fresh

    def screen(self, listings: Iterable[Listing]) -> List[DealReport]:
        """Score every listing, persist all of them, return the passes."""
        passed: List[DealReport] = []
        for listing in listings:
            score = score_listing(listing, self.config.screen)
            offer = build_offer(listing, self.config.structure) if score.passed else None
            report = DealReport(
                listing=listing,
                score=score,
                offer=offer,
                stage=DealStage.SCREENED if score.passed else DealStage.REJECTED,
            )
            self.store.upsert(report)
            if score.passed:
                passed.append(report)
        passed.sort(key=lambda r: -(r.score.total if r.score else 0))
        return passed

    def analyze(self, reports: Iterable[DealReport]) -> List[DealReport]:
        """Run the playbooks on deals worth the spend."""
        if self.analyst is None:
            log.info("No analyst configured; skipping analysis.")
            return []

        analysed: List[DealReport] = []
        for report in reports:
            if len(analysed) >= self.config.max_analyses_per_run:
                log.info("Reached the per-run analysis cap.")
                break
            score = report.score.total if report.score else 0
            if score < self.config.analysis_threshold:
                log.debug("%s scores %.1f; below the analysis threshold.", report.listing.name, score)
                continue
            if self.store.has_analysis(report.listing.deal_id):
                continue

            log.info("Analysing %s (score %.1f)", report.listing.name, score)
            updated = self.analyst.analyze(
                report.listing, self.config.playbooks, report.offer, report=report
            )
            self.store.upsert(updated)
            analysed.append(updated)
        return analysed

    def write_reports(self, reports: List[DealReport]) -> List[Path]:
        """Write a memo per deal, an LOI for viable ones, and a shortlist."""
        out = self.config.output_dir
        out.mkdir(parents=True, exist_ok=True)
        written: List[Path] = []

        for report in reports:
            slug = _slug(report.listing.name) or report.listing.deal_id
            memo_path = out / f"{slug}-{report.listing.deal_id[:6]}.md"
            memo_path.write_text(render_memo(report, self.config.structure), encoding="utf-8")
            written.append(memo_path)

            if report.offer and report.offer.viable:
                loi_path = out / f"{slug}-{report.listing.deal_id[:6]}-LOI.md"
                loi_path.write_text(render_loi(report), encoding="utf-8")
                written.append(loi_path)

        if reports:
            shortlist = out / "shortlist.md"
            shortlist.write_text(render_shortlist(reports), encoding="utf-8")
            written.append(shortlist)
        return written

    # ---- full pass ----------------------------------------------------

    def run_once(self, sources: Optional[Iterable[str]] = None) -> RunStats:
        stats = RunStats()
        run_id = self.store.start_run()
        calls_before = self.analyst.client.calls_made if self.analyst else 0

        try:
            fetched = self.fetch(sources)
            stats.discovered = len(fetched)
            fresh = self.discover(listings=fetched)
            stats.new = len(fresh)

            passed = self.screen(fresh)
            stats.screened = len(passed)
            stats.rejected = stats.new - stats.screened

            analysed = self.analyze(passed)
            stats.analyzed = len(analysed)

            # Memos for everything that passed, with analysis where it exists.
            by_id = {r.listing.deal_id: r for r in passed}
            by_id.update({r.listing.deal_id: r for r in analysed})
            self.write_reports(sorted(by_id.values(), key=lambda r: -(r.score.total if r.score else 0)))
        except Exception as exc:  # noqa: BLE001 - a run must always close out
            log.exception("Run failed")
            stats.errors.append(str(exc))
        finally:
            if self.analyst:
                stats.calls = self.analyst.client.calls_made - calls_before
            self.store.finish_run(
                run_id, stats.discovered, stats.screened, stats.analyzed,
                stats.calls, "; ".join(stats.errors),
            )
        return stats

    def run_forever(
        self,
        interval_seconds: int = 3600,
        sources: Optional[Iterable[str]] = None,
        max_cycles: Optional[int] = None,
    ) -> List[RunStats]:
        """Continuous operation. A failing cycle is logged, never fatal."""
        history: List[RunStats] = []
        cycle = 0
        while max_cycles is None or cycle < max_cycles:
            cycle += 1
            log.info("--- cycle %d ---", cycle)
            stats = self.run_once(sources)
            history.append(stats)
            log.info("cycle %d: %s", cycle, stats.summary())
            if max_cycles is not None and cycle >= max_cycles:
                break
            time.sleep(interval_seconds)
        return history


def _dedupe(listings: Iterable[Listing]) -> List[Listing]:
    seen: set = set()
    out: List[Listing] = []
    for listing in listings:
        if listing.fingerprint in seen:
            continue
        seen.add(listing.fingerprint)
        out.append(listing)
    return out


def _slug(text: str) -> str:
    keep = [c.lower() if c.isalnum() else "-" for c in text]
    slug = "".join(keep)
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-")[:60]
