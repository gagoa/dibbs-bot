"""SQLite database access layer for dibbs-bot.

This module is intentionally thin: it uses the stdlib `sqlite3` driver with a
small set of helper functions (init, connect, upsert RFQ, save score, queries).
Run as a script (``python db/database.py``) to initialize the database from
``db/models.sql``.
"""

from __future__ import annotations

import logging
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

# Allow running this file directly as a script (python db/database.py) by
# adding the project root to sys.path before importing local packages.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from utils.config import SETTINGS
from utils.logging_config import configure_logging

logger = logging.getLogger(__name__)

SCHEMA_PATH: Path = Path(__file__).resolve().parent / "models.sql"

# Columns we accept on insert/update for an RFQ. Keeping this list explicit
# makes the upsert immune to accidentally writing unknown keys.
RFQ_COLUMNS: tuple[str, ...] = (
    "solicitation_number",
    "nsn",
    "fsc",
    "item_name",
    "quantity",
    "unit_of_issue",
    "issue_date",
    "close_date",
    "set_aside",
    "purchase_request_number",
    "buyer",
    "approved_source_cages",
    "manufacturer_part_numbers",
    "technical_documents_available",
    "url",
    "raw_text",
)


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------

def connect(db_path: Path | None = None) -> sqlite3.Connection:
    """Return a sqlite3 connection with sensible defaults."""
    path = db_path or SETTINGS.db_path
    conn = sqlite3.connect(path)
    # Rows behave like dicts: row["solicitation_number"].
    conn.row_factory = sqlite3.Row
    # Enforce FK cascade so deleting an RFQ removes its scores.
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


@contextmanager
def get_connection(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """Context manager that commits on success and closes always."""
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def init_db(db_path: Path | None = None) -> Path:
    """Create tables (idempotent). Returns the resolved db path."""
    path = db_path or SETTINGS.db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    with get_connection(path) as conn:
        conn.executescript(sql)
    logger.info("Initialized database at %s", path)
    return path


# ---------------------------------------------------------------------------
# RFQ CRUD
# ---------------------------------------------------------------------------

def upsert_rfq(conn: sqlite3.Connection, rfq: dict[str, Any]) -> int:
    """Insert or update an RFQ keyed by ``solicitation_number``.

    Returns the row id. Unknown keys are ignored; missing keys become NULL.
    Booleans are coerced to 0/1 for the technical_documents_available column.
    """
    if not rfq.get("solicitation_number"):
        raise ValueError("rfq must include 'solicitation_number'")

    payload = {col: rfq.get(col) for col in RFQ_COLUMNS}
    # Normalize the only boolean column.
    payload["technical_documents_available"] = (
        1 if payload.get("technical_documents_available") else 0
    )

    columns = ", ".join(RFQ_COLUMNS)
    placeholders = ", ".join(f":{c}" for c in RFQ_COLUMNS)
    # On conflict (same solicitation_number), update every column except the
    # primary key. excluded.* refers to the row we tried to insert.
    update_clause = ", ".join(
        f"{c} = excluded.{c}" for c in RFQ_COLUMNS if c != "solicitation_number"
    )

    sql = (
        f"INSERT INTO rfqs ({columns}) VALUES ({placeholders}) "
        f"ON CONFLICT(solicitation_number) DO UPDATE SET {update_clause}"
    )
    cur = conn.execute(sql, payload)

    # cur.lastrowid is the inserted id; on UPDATE path we need to look it up.
    if cur.lastrowid:
        row_id = cur.lastrowid
    else:
        row = conn.execute(
            "SELECT id FROM rfqs WHERE solicitation_number = ?",
            (payload["solicitation_number"],),
        ).fetchone()
        row_id = int(row["id"])
    return row_id


def bulk_upsert_rfqs(rfqs: Iterable[dict[str, Any]], db_path: Path | None = None) -> int:
    """Convenience wrapper that opens a connection and upserts many RFQs."""
    count = 0
    with get_connection(db_path) as conn:
        for rfq in rfqs:
            upsert_rfq(conn, rfq)
            count += 1
    logger.info("Upserted %d RFQ row(s)", count)
    return count


def save_score(conn: sqlite3.Connection, score: dict[str, Any]) -> int:
    """Insert a new opportunity_scores row. Returns its id."""
    sql = (
        "INSERT INTO opportunity_scores "
        "(rfq_id, score, margin_potential, competition_level, "
        " sourcing_difficulty, urgency, notes) "
        "VALUES (:rfq_id, :score, :margin_potential, :competition_level, "
        "        :sourcing_difficulty, :urgency, :notes)"
    )
    cur = conn.execute(sql, score)
    return int(cur.lastrowid)


# ---------------------------------------------------------------------------
# Read helpers used by the dashboard
# ---------------------------------------------------------------------------

def fetch_all_rfqs_with_scores(db_path: Path | None = None) -> list[dict[str, Any]]:
    """Return every RFQ joined with its most recent score (if any)."""
    sql = """
    SELECT
        r.*,
        s.score             AS score,
        s.margin_potential  AS margin_potential,
        s.competition_level AS competition_level,
        s.sourcing_difficulty AS sourcing_difficulty,
        s.urgency           AS urgency,
        s.notes             AS score_notes,
        s.created_at        AS scored_at
    FROM rfqs r
    LEFT JOIN (
        -- pick the newest score per rfq
        SELECT s1.*
        FROM opportunity_scores s1
        JOIN (
            SELECT rfq_id, MAX(id) AS max_id
            FROM opportunity_scores
            GROUP BY rfq_id
        ) latest ON latest.max_id = s1.id
    ) s ON s.rfq_id = r.id
    ORDER BY (s.score IS NULL), s.score DESC, r.close_date ASC
    """
    with get_connection(db_path) as conn:
        rows = conn.execute(sql).fetchall()
    return [dict(r) for r in rows]


def fetch_rfq_by_id(rfq_id: int, db_path: Path | None = None) -> dict[str, Any] | None:
    """Return a single RFQ row (with its latest score) or None."""
    with get_connection(db_path) as conn:
        row = conn.execute(
            """
            SELECT r.*,
                   s.score, s.margin_potential, s.competition_level,
                   s.sourcing_difficulty, s.urgency,
                   s.notes AS score_notes, s.created_at AS scored_at
            FROM rfqs r
            LEFT JOIN opportunity_scores s
                   ON s.id = (
                       SELECT id FROM opportunity_scores
                       WHERE rfq_id = r.id
                       ORDER BY id DESC LIMIT 1
                   )
            WHERE r.id = ?
            """,
            (rfq_id,),
        ).fetchone()
    return dict(row) if row else None


def count_rfqs(db_path: Path | None = None) -> int:
    with get_connection(db_path) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM rfqs").fetchone()[0])


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    configure_logging()
    path = init_db()
    n = count_rfqs()
    logger.info("Database ready: %s (rfq rows=%d)", path, n)
    print(f"Database initialized at: {path}")
    print(f"RFQ rows currently in DB: {n}")


if __name__ == "__main__":
    main()
