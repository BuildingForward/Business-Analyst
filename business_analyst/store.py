"""SQLite persistence.

The bot runs continuously, so it must remember what it has already seen,
scored and paid to analyse. Dedupe happens on two keys: the source's own id,
and a fuzzy fingerprint that collapses the same business listed by two brokers.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

from .models import DealReport, DealScore, DealStage, Listing, OfferStructure

SCHEMA = """
CREATE TABLE IF NOT EXISTS deals (
    deal_id       TEXT PRIMARY KEY,
    fingerprint   TEXT NOT NULL,
    source        TEXT NOT NULL,
    external_id   TEXT NOT NULL,
    name          TEXT NOT NULL,
    industry      TEXT,
    location      TEXT,
    asking_price  REAL,
    cash_flow     REAL,
    score         REAL,
    stage         TEXT NOT NULL,
    listing_json  TEXT NOT NULL,
    score_json    TEXT,
    offer_json    TEXT,
    discovered_at TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_deals_fingerprint ON deals(fingerprint);
CREATE INDEX IF NOT EXISTS idx_deals_stage ON deals(stage);
CREATE INDEX IF NOT EXISTS idx_deals_score ON deals(score DESC);

CREATE TABLE IF NOT EXISTS sections (
    deal_id      TEXT NOT NULL,
    key          TEXT NOT NULL,
    title        TEXT NOT NULL,
    content      TEXT NOT NULL,
    model        TEXT,
    tokens_in    INTEGER DEFAULT 0,
    tokens_out   INTEGER DEFAULT 0,
    generated_at TEXT NOT NULL,
    PRIMARY KEY (deal_id, key),
    FOREIGN KEY (deal_id) REFERENCES deals(deal_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    discovered  INTEGER DEFAULT 0,
    screened    INTEGER DEFAULT 0,
    analyzed    INTEGER DEFAULT 0,
    calls       INTEGER DEFAULT 0,
    notes       TEXT
);
"""


class DealStore:
    """Everything the bot has ever seen, and what it concluded."""

    def __init__(self, path: str | Path = "deals.db"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._memory_conn: Optional[sqlite3.Connection] = None
        if self.path == ":memory:":
            # A shared connection, otherwise each open gets a fresh empty database.
            self._memory_conn = sqlite3.connect(self.path)
            self._memory_conn.row_factory = sqlite3.Row
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        if self._memory_conn is not None:
            yield self._memory_conn
            self._memory_conn.commit()
            return
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ---- writes -------------------------------------------------------

    def upsert(self, report: DealReport) -> str:
        """Insert or update a deal and its analysis sections."""
        listing = report.listing
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO deals (deal_id, fingerprint, source, external_id, name,
                    industry, location, asking_price, cash_flow, score, stage,
                    listing_json, score_json, offer_json, discovered_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(deal_id) DO UPDATE SET
                    name=excluded.name,
                    industry=excluded.industry,
                    location=excluded.location,
                    asking_price=excluded.asking_price,
                    cash_flow=excluded.cash_flow,
                    score=excluded.score,
                    stage=excluded.stage,
                    listing_json=excluded.listing_json,
                    score_json=excluded.score_json,
                    offer_json=excluded.offer_json,
                    updated_at=excluded.updated_at
                """,
                (
                    listing.deal_id,
                    listing.fingerprint,
                    listing.source,
                    listing.external_id,
                    listing.name,
                    listing.industry,
                    listing.location,
                    listing.asking_price,
                    listing.cash_flow,
                    report.score.total if report.score else None,
                    report.stage.value,
                    json.dumps(listing.to_dict()),
                    json.dumps(report.score.to_dict()) if report.score else None,
                    json.dumps(report.offer.to_dict()) if report.offer else None,
                    listing.discovered_at,
                    report.updated_at,
                ),
            )
            for section in report.sections:
                conn.execute(
                    """
                    INSERT INTO sections (deal_id, key, title, content, model,
                        tokens_in, tokens_out, generated_at)
                    VALUES (?,?,?,?,?,?,?,?)
                    ON CONFLICT(deal_id, key) DO UPDATE SET
                        content=excluded.content,
                        model=excluded.model,
                        tokens_in=excluded.tokens_in,
                        tokens_out=excluded.tokens_out,
                        generated_at=excluded.generated_at
                    """,
                    (
                        listing.deal_id,
                        section.key,
                        section.title,
                        section.content,
                        section.model,
                        section.tokens_in,
                        section.tokens_out,
                        section.generated_at,
                    ),
                )
        return listing.deal_id

    def start_run(self) -> int:
        from datetime import datetime, timezone

        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO runs (started_at) VALUES (?)",
                (datetime.now(timezone.utc).isoformat(timespec="seconds"),),
            )
            return int(cur.lastrowid)

    def finish_run(
        self, run_id: int, discovered: int, screened: int, analyzed: int,
        calls: int, notes: str = "",
    ) -> None:
        from datetime import datetime, timezone

        with self.connect() as conn:
            conn.execute(
                """UPDATE runs SET finished_at=?, discovered=?, screened=?,
                   analyzed=?, calls=?, notes=? WHERE id=?""",
                (
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    discovered, screened, analyzed, calls, notes, run_id,
                ),
            )

    # ---- reads --------------------------------------------------------

    def exists(self, listing: Listing) -> bool:
        """True if this exact listing, or the same business, is already known."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM deals WHERE deal_id=? OR fingerprint=? LIMIT 1",
                (listing.deal_id, listing.fingerprint),
            ).fetchone()
        return row is not None

    def has_analysis(self, deal_id: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM sections WHERE deal_id=? LIMIT 1", (deal_id,)
            ).fetchone()
        return row is not None

    def get(self, deal_id: str) -> Optional[DealReport]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM deals WHERE deal_id=?", (deal_id,)).fetchone()
            if row is None:
                return None
            sections = conn.execute(
                "SELECT * FROM sections WHERE deal_id=? ORDER BY generated_at", (deal_id,)
            ).fetchall()
        return _row_to_report(row, sections)

    def top(
        self, limit: int = 20, stage: Optional[DealStage] = None
    ) -> List[Tuple[str, str, float, str]]:
        """(deal_id, name, score, stage) for the best-scoring deals.

        The stage matters: a rejected deal can still score well on the
        weighted factors, and must never look like a candidate.
        """
        query = "SELECT deal_id, name, COALESCE(score, 0) AS score, stage FROM deals"
        params: list = []
        if stage:
            query += " WHERE stage=?"
            params.append(stage.value)
        query += " ORDER BY score DESC LIMIT ?"
        params.append(limit)
        with self.connect() as conn:
            return [
                (r["deal_id"], r["name"], r["score"], r["stage"])
                for r in conn.execute(query, params)
            ]

    def pending_analysis(self, limit: int = 10, min_score: float = 0.0) -> List[DealReport]:
        """Screened deals that have not been analysed yet, best first."""
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT d.* FROM deals d
                LEFT JOIN sections s ON s.deal_id = d.deal_id
                WHERE d.stage = ? AND COALESCE(d.score, 0) >= ? AND s.deal_id IS NULL
                ORDER BY d.score DESC LIMIT ?
                """,
                (DealStage.SCREENED.value, min_score, limit),
            ).fetchall()
        return [_row_to_report(r, []) for r in rows]

    def counts(self) -> dict:
        with self.connect() as conn:
            rows = conn.execute("SELECT stage, COUNT(*) c FROM deals GROUP BY stage").fetchall()
            total = conn.execute("SELECT COUNT(*) c FROM deals").fetchone()["c"]
        counts = {r["stage"]: r["c"] for r in rows}
        counts["total"] = total
        return counts


def _row_to_report(row: sqlite3.Row, section_rows) -> DealReport:
    from .models import AnalysisSection

    listing = Listing.from_dict(json.loads(row["listing_json"]))
    score = DealScore.from_dict(json.loads(row["score_json"])) if row["score_json"] else None
    if score is not None:
        from .models import ScoreComponent

        score.components = [ScoreComponent(**c) for c in score.components]
    offer = None
    if row["offer_json"]:
        payload = json.loads(row["offer_json"])
        from .models import NoteTerms

        notes = [NoteTerms(**n) for n in payload.pop("notes", [])]
        offer = OfferStructure.from_dict(payload)
        offer.notes = notes
    sections = [
        AnalysisSection(
            key=s["key"], title=s["title"], content=s["content"], model=s["model"] or "",
            tokens_in=s["tokens_in"], tokens_out=s["tokens_out"], generated_at=s["generated_at"],
        )
        for s in section_rows
    ]
    return DealReport(
        listing=listing, score=score, offer=offer, sections=sections,
        stage=DealStage(row["stage"]), updated_at=row["updated_at"],
    )
