"""Command line interface.

    python -m business_analyst screen   --source data/sample_listings.csv
    python -m business_analyst run      --source data/sample_listings.csv --provider dry-run
    python -m business_analyst watch    --source data/sample_listings.csv --interval 3600
    python -m business_analyst analyze  --deal <id> --playbook swot
    python -m business_analyst prompts  --list
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import List, Optional

from .analysis import prompts as prompt_lib
from .analysis.engine import Analyst
from .analysis.llm import DEFAULT_MODEL, default_client
from .models import DealReport, DealStage
from .pipeline import Pipeline, PipelineConfig
from .reporting import render_loi, render_memo, render_shortlist
from .screening.finance import StructureParams, sensitivity
from .screening.industries import all_profiles
from .screening.scoring import ScreenConfig, score_listing
from .models import OutreachStatus
from .prospecting_pipeline import ProspectPipeline, ProspectPipelineConfig
from .screening.prospecting import ProspectConfig, estimate_financials, score_prospect
from .sources import CsvSource, JsonApiSource, JsonSource, RssSource
from .sources.base import SourceRegistry
from .sources.google_places import INDUSTRY_TYPES, GooglePlacesSource
from .sources.local import CsvProspectSource
from .store import DealStore


def build_registry(specs: List[str]) -> SourceRegistry:
    """Turn --source arguments into live sources.

    A spec is a file path, or `rss:<url>`, or `json-api:<url>`.
    """
    registry = SourceRegistry()
    for spec in specs:
        if spec.startswith("rss:"):
            registry.register(RssSource(spec[4:], name=f"rss:{_host(spec[4:])}"))
        elif spec.startswith("json-api:"):
            registry.register(JsonApiSource(spec[9:], name=f"api:{_host(spec[9:])}"))
        else:
            path = Path(spec)
            if path.suffix.lower() == ".json":
                registry.register(JsonSource(path))
            else:
                registry.register(CsvSource(path))
    return registry


def build_prospect_registry(args) -> SourceRegistry:
    """Google Places by default; CSV sources when given."""
    registry = SourceRegistry()
    for spec in args.source or []:
        registry.register(CsvProspectSource(Path(spec)))

    if not args.no_google:
        key = args.api_key or os.environ.get("GOOGLE_PLACES_API_KEY", "")
        if not key:
            if registry.names():
                print(
                    "No GOOGLE_PLACES_API_KEY set; using the CSV sources only.",
                    file=sys.stderr,
                )
                return registry
            raise SystemExit(
                "Google Places needs an API key. Set GOOGLE_PLACES_API_KEY, or pass\n"
                "--source <file.csv> with businesses you already have, or --no-google."
            )
        center = None
        if args.center:
            lat, _, lon = args.center.partition(",")
            center = (float(lat), float(lon))
        registry.register(
            GooglePlacesSource(
                api_key=key,
                center=center,
                area=args.area,
                radius_km=args.radius,
                step_km=args.step,
                industries=args.industry or (),
                max_requests=args.max_requests,
                cache_dir=Path(args.places_cache) if args.places_cache else None,
                include_chains=args.include_chains,
            )
        )
    return registry


def _host(url: str) -> str:
    from urllib.parse import urlparse

    return urlparse(url).netloc or "feed"


def _structure_from_args(args) -> StructureParams:
    return StructureParams(
        buyer_salary=args.salary,
        target_dscr=args.target_dscr,
        min_dscr=args.min_dscr,
        note_rate=args.rate,
        note_term_years=args.term,
        standby_months=args.standby,
        interest_only_months=args.interest_only,
    )


def _make_analyst(args) -> Optional[Analyst]:
    if args.provider == "none":
        return None
    client = default_client(
        provider=args.provider,
        model=args.model,
        cache_dir=Path(args.cache_dir) if args.cache_dir else None,
        max_calls=args.max_calls,
        effort=args.effort,
    )
    return Analyst(client)


def _pipeline(args) -> Pipeline:
    structure = _structure_from_args(args)
    config = PipelineConfig(
        screen=ScreenConfig(
            min_sde=args.min_sde,
            max_asking_price=args.max_price,
            max_price_to_sde=args.max_multiple,
            structure=structure,
        ),
        structure=structure,
        playbooks=args.playbooks or list(prompt_lib.TRIAGE_SET),
        analysis_threshold=args.threshold,
        max_analyses_per_run=args.max_deals,
        output_dir=Path(args.out),
    )
    return Pipeline(
        store=DealStore(args.db),
        registry=build_registry(args.source),
        analyst=_make_analyst(args),
        config=config,
    )


# ---- commands ---------------------------------------------------------


def cmd_screen(args) -> int:
    pipeline = _pipeline(args)
    fresh = pipeline.discover()
    passed = pipeline.screen(fresh)
    print(f"Fetched {len(fresh)} new listings; {len(passed)} passed screening.\n")
    if passed:
        print(render_shortlist(passed))
    if args.verbose:
        for report in passed:
            print(f"\n--- {report.listing.name} ---")
            print(report.score.explain())
    return 0


def cmd_run(args) -> int:
    pipeline = _pipeline(args)
    stats = pipeline.run_once()
    print(stats.summary())
    for err in stats.errors:
        print(f"error: {err}", file=sys.stderr)
    print(f"Reports written to {args.out}/")
    return 1 if stats.errors else 0


def cmd_watch(args) -> int:
    pipeline = _pipeline(args)
    print(f"Watching every {args.interval}s. Ctrl-C to stop.")
    try:
        history = pipeline.run_forever(
            interval_seconds=args.interval, max_cycles=args.cycles
        )
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0
    for i, stats in enumerate(history, 1):
        print(f"cycle {i}: {stats.summary()}")
    return 0


def cmd_analyze(args) -> int:
    store = DealStore(args.db)
    report = store.get(args.deal)
    if report is None:
        print(f"No deal with id {args.deal}", file=sys.stderr)
        return 1
    analyst = _make_analyst(args)
    if analyst is None:
        print("Analysis needs a provider; pass --provider dry-run or anthropic.", file=sys.stderr)
        return 1

    playbooks = args.playbooks or prompt_lib.PLAYBOOK_ORDER
    updated = analyst.analyze(report.listing, playbooks, report.offer, report=report)
    store.upsert(updated)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{args.deal}.md"
    path.write_text(render_memo(updated, _structure_from_args(args)), encoding="utf-8")
    print(f"Wrote {path} ({len(updated.sections)} sections).")
    return 0


def cmd_offer(args) -> int:
    store = DealStore(args.db)
    report = store.get(args.deal)
    if report is None:
        print(f"No deal with id {args.deal}", file=sys.stderr)
        return 1
    if report.offer is None:
        print("This deal has no structured offer; run `screen` first.", file=sys.stderr)
        return 1
    text = render_loi(report, buyer_name=args.buyer, buyer_contact=args.contact)
    if args.out_file:
        Path(args.out_file).write_text(text, encoding="utf-8")
        print(f"Wrote {args.out_file}")
    else:
        print(text)
    return 0


def cmd_show(args) -> int:
    store = DealStore(args.db)
    report = store.get(args.deal)
    if report is None:
        print(f"No deal with id {args.deal}", file=sys.stderr)
        return 1
    print(render_memo(report, _structure_from_args(args)))
    return 0


def cmd_list(args) -> int:
    store = DealStore(args.db)
    counts = store.counts()
    print("Pipeline:", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    stage = DealStage(args.stage) if args.stage else None
    rows = store.top(limit=args.limit, stage=stage)
    if not rows:
        print("No deals yet. Run `screen` or `run` first.")
        return 0
    print(f"\n{'deal id':<18}{'score':>7}  {'stage':<14}name")
    for deal_id, name, score, stage in rows:
        print(f"{deal_id:<18}{score:>7.1f}  {stage:<14}{name}")
    return 0


def cmd_stress(args) -> int:
    store = DealStore(args.db)
    report = store.get(args.deal)
    if report is None or report.offer is None:
        print("No such deal, or it has no offer yet.", file=sys.stderr)
        return 1
    rows = sensitivity(report.listing, _structure_from_args(args), offer=report.offer)
    print(f"{report.listing.name} - price held at ${report.offer.purchase_price:,.0f}\n")
    print(f"{'haircut':>8}{'SDE':>12}{'debt svc':>12}{'DSCR':>8}{'free cash':>12}  covers")
    for r in rows:
        print(
            f"{r['sde_haircut'] * 100:>7.0f}%{r['sde']:>12,.0f}{r['debt_service']:>12,.0f}"
            f"{r['dscr']:>8.2f}{r['free_cash']:>12,.0f}  {'yes' if r['viable'] else 'NO'}"
        )
    return 0


def cmd_prospect(args) -> int:
    """Pull local businesses, qualify them, and write the outreach handoff."""
    registry = build_prospect_registry(args)
    if not registry.names():
        print("No sources configured.", file=sys.stderr)
        return 2

    config = ProspectPipelineConfig(
        screen=ProspectConfig(
            structure=_structure_from_args(args),
            min_age_years=args.min_age,
            min_estimated_sde=args.min_sde if args.min_sde else None,
            require_contact=not args.allow_uncontactable,
            exclude_chains=not args.include_chains,
        ),
        output_dir=Path(args.handoff_dir),
        fetch_limit=args.limit,
        min_handoff_score=args.min_score,
        only_unexported=not args.reexport,
    )
    pipeline = ProspectPipeline(DealStore(args.db), registry, config)
    stats = pipeline.run_once()

    print(stats.summary())
    if stats.requests:
        print(f"{stats.requests} billable Places requests used.")
    for err in stats.errors:
        print(f"error: {err}", file=sys.stderr)
    if stats.exported:
        print(f"Handoff written to {args.handoff_dir}/ (prospects.json, prospects.csv, README.md)")
    else:
        print("Nothing new qualified for handoff.")
    return 1 if stats.errors else 0


def cmd_handoff(args) -> int:
    """Re-export the current qualified list without pulling anything new."""
    config = ProspectPipelineConfig(
        output_dir=Path(args.handoff_dir),
        min_handoff_score=args.min_score,
        only_unexported=not args.reexport,
    )
    pipeline = ProspectPipeline(DealStore(args.db), SourceRegistry(), config)
    result = pipeline.handoff()
    print(f"Exported {result['count']} prospects to {args.handoff_dir}/")
    return 0


def cmd_prospects(args) -> int:
    """List stored prospects by score."""
    store = DealStore(args.db)
    counts = store.prospect_counts()
    print("Prospects:", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    reports = store.top_prospects(limit=args.limit, min_score=args.min_score)
    if not reports:
        print("None yet. Run `prospect` first.")
        return 0
    print(f"\n{'prospect id':<18}{'score':>7}  {'est SDE':>10}  {'email':<12}name")
    for r in reports:
        sde = r.estimate.sde if r.estimate and r.estimate.sde else 0
        has_email = "yes" if r.prospect.email else "no"
        print(
            f"{r.prospect.prospect_id:<18}{r.score.total if r.score else 0:>7.1f}"
            f"  {sde:>10,.0f}  {has_email:<12}{r.prospect.name}"
        )
    return 0


def cmd_status(args) -> int:
    """Record what the outreach agent came back with."""
    store = DealStore(args.db)
    if store.get_prospect(args.prospect) is None:
        print(f"No prospect with id {args.prospect}", file=sys.stderr)
        return 1
    store.set_prospect_status(args.prospect, OutreachStatus(args.set))
    print(f"{args.prospect} -> {args.set}")
    return 0


def cmd_place_types(args) -> int:
    print(f"{'industry':<22}google place types")
    for industry, types in sorted(INDUSTRY_TYPES.items()):
        print(f"{industry:<22}{', '.join(types)}")
    return 0


def cmd_prompts(args) -> int:
    if args.show:
        book = prompt_lib.PLAYBOOKS.get(args.show)
        if not book:
            print(f"Unknown playbook '{args.show}'.", file=sys.stderr)
            return 1
        print(f"# {book.title}\n\n{book.purpose}\n\n{book.template}")
        return 0
    for key in prompt_lib.PLAYBOOK_ORDER:
        book = prompt_lib.PLAYBOOKS[key]
        print(f"{key:<22} {book.title:<26} {book.purpose}")
    return 0


def cmd_industries(args) -> int:
    print(f"{'industry':<22}{'multiple':>12}  {'carry':<7}{'capex':<8}{'owner dep.':<11}")
    for p in all_profiles():
        rng = f"{p.typical_sde_multiple_low:.1f}-{p.typical_sde_multiple_high:.1f}x"
        print(
            f"{p.name:<22}{rng:>12}  {'yes' if p.seller_finance_friendly else 'no':<7}"
            f"{p.capital_intensity:<8}{p.owner_dependence:<11}"
        )
    return 0


# ---- parser -----------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="business-analyst",
        description="Find, screen and analyse businesses acquirable with no cash down.",
    )
    parser.add_argument("--db", default="deals.db", help="SQLite database path")
    parser.add_argument("--out", default="reports", help="Directory for generated memos")
    parser.add_argument("-v", "--verbose", action="store_true")

    sub = parser.add_subparsers(dest="command", required=True)

    def add_source_args(p):
        p.add_argument(
            "--source", action="append", default=[],
            help="CSV/JSON path, rss:<url> or json-api:<url>. Repeatable.",
        )
        p.add_argument("--min-sde", type=float, default=100_000.0)
        p.add_argument("--max-price", type=float, default=5_000_000.0)
        p.add_argument("--max-multiple", type=float, default=4.5)

    def add_structure_args(p):
        p.add_argument(
            "--salary", type=float, default=150_000.0, help="Operator salary taken first"
        )
        p.add_argument("--target-dscr", type=float, default=1.5)
        p.add_argument("--min-dscr", type=float, default=1.25)
        p.add_argument("--rate", type=float, default=0.06, help="Seller note rate, e.g. 0.06")
        p.add_argument("--term", type=int, default=7, help="Seller note term in years")
        p.add_argument("--standby", type=int, default=3, help="Months of no payments")
        p.add_argument("--interest-only", type=int, default=12)

    def add_model_args(p):
        p.add_argument(
            "--provider", default="dry-run",
            choices=["anthropic", "dry-run", "echo", "none"],
            help="dry-run renders prompts without spending anything",
        )
        p.add_argument("--model", default=DEFAULT_MODEL)
        p.add_argument("--effort", default="high", choices=["low", "medium", "high", "xhigh", "max"])
        p.add_argument("--cache-dir", default=".llm-cache")
        p.add_argument("--max-calls", type=int, default=24, help="Hard cap on model calls per run")
        p.add_argument(
            "--playbook", dest="playbooks", action="append", default=[],
            choices=prompt_lib.PLAYBOOK_ORDER, help="Repeatable; defaults to the triage set",
        )
        p.add_argument("--threshold", type=float, default=55.0, help="Min score to spend on analysis")
        p.add_argument("--max-deals", type=int, default=5, help="Max deals analysed per run")

    p = sub.add_parser("screen", help="Fetch and score listings; no model calls")
    add_source_args(p); add_structure_args(p); add_model_args(p)
    p.set_defaults(func=cmd_screen, provider="none")

    p = sub.add_parser("run", help="One full pass: discover, screen, analyse, report")
    add_source_args(p); add_structure_args(p); add_model_args(p)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("watch", help="Run continuously on an interval")
    add_source_args(p); add_structure_args(p); add_model_args(p)
    p.add_argument("--interval", type=int, default=3600, help="Seconds between cycles")
    p.add_argument("--cycles", type=int, default=None, help="Stop after N cycles")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("analyze", help="Run playbooks against one stored deal")
    add_structure_args(p); add_model_args(p)
    p.add_argument("--deal", required=True)
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("offer", help="Draft an LOI for a stored deal")
    add_structure_args(p)
    p.add_argument("--deal", required=True)
    p.add_argument("--buyer", default="[Buyer entity]")
    p.add_argument("--contact", default="[contact details]")
    p.add_argument("--out-file", default=None)
    p.set_defaults(func=cmd_offer)

    p = sub.add_parser("show", help="Print a stored deal's memo")
    add_structure_args(p)
    p.add_argument("--deal", required=True)
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("list", help="List stored deals by score")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--stage", choices=[s.value for s in DealStage], default=None)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("stress", help="Stress a deal's offer against an overstated SDE")
    add_structure_args(p)
    p.add_argument("--deal", required=True)
    p.set_defaults(func=cmd_stress)

    p = sub.add_parser(
        "prospect",
        help="Pull local businesses from Google, qualify them, write the outreach handoff",
    )
    add_structure_args(p)
    p.add_argument("--area", default="", help='Area to search, e.g. "Tampa, FL"')
    p.add_argument("--center", default="", help="lat,lon instead of --area (skips geocoding)")
    p.add_argument("--radius", type=float, default=8.0, help="Search radius in km")
    p.add_argument("--step", type=float, default=2.5, help="Grid tile size in km")
    p.add_argument(
        "--industry", action="append", default=[], choices=sorted(INDUSTRY_TYPES),
        help="Repeatable; defaults to every mapped industry",
    )
    p.add_argument("--api-key", default=None, help="Defaults to $GOOGLE_PLACES_API_KEY")
    p.add_argument("--max-requests", type=int, default=120, help="Cap on billable Places calls")
    p.add_argument("--places-cache", default=".places-cache")
    p.add_argument("--no-google", action="store_true", help="Use only --source CSV files")
    p.add_argument("--source", action="append", default=[], help="CSV of businesses you hold")
    p.add_argument("--limit", type=int, default=500)
    p.add_argument("--min-age", type=int, default=10, help="Minimum years trading")
    p.add_argument("--min-sde", type=float, default=0, help="Override the estimated-SDE floor")
    p.add_argument("--min-score", type=float, default=50.0, help="Minimum score to hand off")
    p.add_argument("--include-chains", action="store_true")
    p.add_argument("--allow-uncontactable", action="store_true")
    p.add_argument("--handoff-dir", default="handoff")
    p.add_argument("--reexport", action="store_true", help="Include already-exported prospects")
    p.set_defaults(func=cmd_prospect)

    p = sub.add_parser("handoff", help="Re-export the qualified prospect list")
    p.add_argument("--handoff-dir", default="handoff")
    p.add_argument("--min-score", type=float, default=50.0)
    p.add_argument("--reexport", action="store_true")
    p.set_defaults(func=cmd_handoff)

    p = sub.add_parser("prospects", help="List stored prospects by score")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--min-score", type=float, default=0.0)
    p.set_defaults(func=cmd_prospects)

    p = sub.add_parser("status", help="Record an outreach outcome against a prospect")
    p.add_argument("--prospect", required=True)
    p.add_argument("--set", required=True, choices=[s.value for s in OutreachStatus])
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("place-types", help="Show the industry to Google place type mapping")
    p.set_defaults(func=cmd_place_types)

    p = sub.add_parser("prompts", help="List or print the analyst playbooks")
    p.add_argument("--show", default=None, help="Print one playbook's template")
    p.set_defaults(func=cmd_prompts)

    p = sub.add_parser("industries", help="Show the industry screening table")
    p.set_defaults(func=cmd_industries)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", False) else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    needs_source = args.command in {"screen", "run", "watch"}
    if needs_source and not getattr(args, "source", None):
        print("No --source given; nothing to fetch.", file=sys.stderr)
        return 2
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
