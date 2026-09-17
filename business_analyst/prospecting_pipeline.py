"""Off-market pipeline: pull local businesses, qualify, hand off.

Mirrors the listing pipeline but ends at a handoff export rather than an
offer, because a business that is not for sale cannot be offered on until
someone has actually spoken to the owner.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional

from .models import Prospect, ProspectReport
from .outreach import export_csv, export_json, readme_for_agent
from .screening.prospecting import ProspectConfig, estimate_financials, score_prospect
from .sources.base import SourceRegistry
from .store import DealStore

log = logging.getLogger(__name__)


@dataclass
class ProspectRunStats:
    pulled: int = 0
    new: int = 0
    qualified: int = 0
    rejected: int = 0
    exported: int = 0
    requests: int = 0
    errors: List[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{self.pulled} pulled, {self.new} new, {self.qualified} qualified, "
            f"{self.rejected} rejected, {self.exported} handed off"
        )


@dataclass
class ProspectPipelineConfig:
    screen: ProspectConfig = field(default_factory=ProspectConfig)
    output_dir: Path = Path("handoff")
    fetch_limit: int = 500
    min_handoff_score: float = 50.0
    #: Re-exporting a prospect the outreach agent already has wastes its time
    #: and risks a duplicate approach to the same owner.
    only_unexported: bool = True

    def __post_init__(self) -> None:
        self.output_dir = Path(self.output_dir)


class ProspectPipeline:
    def __init__(
        self,
        store: DealStore,
        registry: SourceRegistry,
        config: Optional[ProspectPipelineConfig] = None,
    ):
        self.store = store
        self.registry = registry
        self.config = config or ProspectPipelineConfig()

    def pull(self, sources: Optional[Iterable[str]] = None) -> List[Prospect]:
        return list(self.registry.fetch(sources, limit=self.config.fetch_limit))

    def new_only(self, prospects: Iterable[Prospect]) -> List[Prospect]:
        """Drop within-batch duplicates and anything already stored."""
        seen = set()
        fresh: List[Prospect] = []
        for prospect in prospects:
            if prospect.fingerprint in seen:
                continue
            seen.add(prospect.fingerprint)
            if self.store.prospect_exists(prospect):
                continue
            fresh.append(prospect)
        return fresh

    def qualify(self, prospects: Iterable[Prospect]) -> List[ProspectReport]:
        """Estimate, score and persist. Returns the ones that pass."""
        qualified: List[ProspectReport] = []
        for prospect in prospects:
            estimate = estimate_financials(prospect)
            score = score_prospect(prospect, self.config.screen, estimate)
            report = ProspectReport(prospect=prospect, estimate=estimate, score=score)
            self.store.upsert_prospect(report)
            if score.passed:
                qualified.append(report)
        qualified.sort(key=lambda r: -(r.score.total if r.score else 0))
        return qualified

    def handoff(self, reports: Optional[List[ProspectReport]] = None) -> dict:
        """Write the export the outreach agent consumes."""
        if reports is None:
            reports = self.store.top_prospects(
                limit=self.config.fetch_limit,
                min_score=self.config.min_handoff_score,
                unexported_only=self.config.only_unexported,
            )
        reports = [
            r for r in reports
            if (r.score.total if r.score else 0) >= self.config.min_handoff_score
        ]
        out = self.config.output_dir
        out.mkdir(parents=True, exist_ok=True)

        paths = {
            "json": export_json(reports, out / "prospects.json"),
            "csv": export_csv(reports, out / "prospects.csv"),
        }
        readme = out / "README.md"
        readme.write_text(readme_for_agent(), encoding="utf-8")
        paths["readme"] = readme

        self.store.mark_exported([r.prospect.prospect_id for r in reports])
        return {"paths": paths, "count": len(reports)}

    def run_once(self, sources: Optional[Iterable[str]] = None) -> ProspectRunStats:
        stats = ProspectRunStats()
        try:
            pulled = self.pull(sources)
            stats.pulled = len(pulled)
            fresh = self.new_only(pulled)
            stats.new = len(fresh)
            qualified = self.qualify(fresh)
            stats.qualified = len(qualified)
            stats.rejected = stats.new - stats.qualified
            result = self.handoff()
            stats.exported = result["count"]
        except Exception as exc:  # noqa: BLE001 - a run must always close out
            log.exception("Prospect run failed")
            stats.errors.append(str(exc))
        for source in self.registry.all():
            stats.requests += getattr(source, "requests_made", 0)
        return stats
