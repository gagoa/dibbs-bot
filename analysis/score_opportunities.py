"""Compute an opportunity score (0-100) for each RFQ in the database.

The score combines a handful of weighted heuristics. It's deliberately simple
and transparent — every score comes with a list of notes explaining each
contribution. Tune the constants in the WEIGHTS dict as you learn what works.

Run as a script (``python analysis/score_opportunities.py``) to (re)score every
RFQ currently in the DB.
"""

from __future__ import annotations

import logging
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

# Allow running directly: python analysis/score_opportunities.py
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from analysis.nsn_tools import (  # noqa: E402
    classify_fsc,
    has_friendly_keyword,
    has_risky_keyword,
)
from db.database import get_connection, save_score  # noqa: E402
from utils.config import SETTINGS  # noqa: E402
from utils.logging_config import configure_logging  # noqa: E402

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tunable scoring constants
# ---------------------------------------------------------------------------

# Base score every RFQ starts with before adjustments.
BASE_SCORE: int = 50

# Sweet-spot range for quantity. Outside this band, we deduct.
QTY_SWEET_LOW: int = 100
QTY_SWEET_HIGH: int = 10_000

# Days-until-close that we consider "comfortable".
DAYS_COMFORTABLE: int = 14
DAYS_TIGHT: int = 7

WEIGHTS: dict[str, int] = {
    "preferred_fsc": +15,
    "risky_fsc": -20,
    "unknown_fsc": -2,
    "friendly_keyword": +5,
    "risky_keyword": -15,
    "qty_in_band": +8,
    "qty_too_small": -5,
    "qty_too_large": -10,
    "close_comfortable": +10,
    "close_tight": -8,
    "close_imminent": -15,
    "close_past": -25,
    "set_aside_small_biz": +5,
    "set_aside_sole_source": -25,
    "tdp_available": -8,   # techs docs required is a signal of complexity
    "tdp_not_required": +5,
    "approved_sources_open": +6,
    "approved_sources_restricted": -10,
    "no_solicitation_url": -3,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_date(value: Any) -> date | None:
    """Accept ISO date strings, datetime objects, or None."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(str(value)).date()
    except ValueError:
        return None


def _clamp(value: int, lo: int = 0, hi: int = 100) -> int:
    return max(lo, min(hi, value))


def _count_csv(value: Any) -> int:
    """How many items are in a comma-separated string column?"""
    if not value:
        return 0
    return len([p for p in str(value).split(",") if p.strip()])


# ---------------------------------------------------------------------------
# Sub-scores (each returns 0..100 so the dashboard can show them as KPIs)
# ---------------------------------------------------------------------------

def _margin_potential(rfq: dict[str, Any], fsc_category: str) -> int:
    """Rough proxy for margin: friendlier FSC + simple keywords = higher."""
    base = 50
    if fsc_category == "preferred":
        base += 25
    elif fsc_category == "risky":
        base -= 25
    if has_friendly_keyword(rfq.get("item_name")):
        base += 10
    if has_risky_keyword(rfq.get("item_name")):
        base -= 15
    return _clamp(base)


def _competition_level(rfq: dict[str, Any]) -> int:
    """How crowded the bid is likely to be. Higher = more competition."""
    base = 50
    set_aside = (rfq.get("set_aside") or "").lower()
    if "sole source" in set_aside:
        base = 95  # we probably can't even bid
    elif "small business" in set_aside:
        base -= 10
    cage_count = _count_csv(rfq.get("approved_source_cages"))
    if cage_count == 0:
        base -= 5  # open market, but lots of bidders too
    elif cage_count >= 3:
        base -= 5
    return _clamp(base)


def _sourcing_difficulty(rfq: dict[str, Any], fsc_category: str) -> int:
    """How hard it'll be to actually source/build the part."""
    base = 40
    if fsc_category == "risky":
        base += 30
    if rfq.get("technical_documents_available"):
        # TDP "available" usually means TDP is *required* for the build.
        base += 10
    cage_count = _count_csv(rfq.get("approved_source_cages"))
    if cage_count == 1:
        base += 20  # single approved source = hard
    elif cage_count >= 3:
        base -= 10
    if has_risky_keyword(rfq.get("item_name")):
        base += 15
    return _clamp(base)


def _urgency(rfq: dict[str, Any], today: date) -> int:
    """How soon the bid closes. Higher = sooner / more urgent."""
    close = _parse_date(rfq.get("close_date"))
    if not close:
        return 30
    days = (close - today).days
    if days < 0:
        return 100  # already closed
    if days <= 3:
        return 95
    if days <= DAYS_TIGHT:
        return 75
    if days <= DAYS_COMFORTABLE:
        return 55
    if days <= 30:
        return 35
    return 20


# ---------------------------------------------------------------------------
# Main scoring function
# ---------------------------------------------------------------------------

def score_rfq(rfq: dict[str, Any], today: date | None = None) -> dict[str, Any]:
    """Score a single RFQ dict. Returns a payload ready for ``save_score``."""
    today = today or date.today()
    notes: list[str] = []
    score = BASE_SCORE

    # --- FSC bucket -------------------------------------------------------
    fsc_info = classify_fsc(rfq.get("fsc"))
    if fsc_info.category == "preferred":
        score += WEIGHTS["preferred_fsc"]
        notes.append(f"+{WEIGHTS['preferred_fsc']} preferred FSC {fsc_info.code} ({fsc_info.label})")
    elif fsc_info.category == "risky":
        score += WEIGHTS["risky_fsc"]
        notes.append(f"{WEIGHTS['risky_fsc']} risky FSC {fsc_info.code} ({fsc_info.label})")
    else:
        score += WEIGHTS["unknown_fsc"]
        notes.append(f"{WEIGHTS['unknown_fsc']} FSC {fsc_info.code or '?'} not in preferred/risky lists")

    # --- Item-name keywords ----------------------------------------------
    friendly = has_friendly_keyword(rfq.get("item_name"))
    risky = has_risky_keyword(rfq.get("item_name"))
    if friendly:
        score += WEIGHTS["friendly_keyword"]
        notes.append(f"+{WEIGHTS['friendly_keyword']} simple-part keyword: '{friendly}'")
    if risky:
        score += WEIGHTS["risky_keyword"]
        notes.append(f"{WEIGHTS['risky_keyword']} risky keyword: '{risky}'")

    # --- Quantity ---------------------------------------------------------
    qty = rfq.get("quantity")
    if isinstance(qty, int):
        if QTY_SWEET_LOW <= qty <= QTY_SWEET_HIGH:
            score += WEIGHTS["qty_in_band"]
            notes.append(f"+{WEIGHTS['qty_in_band']} quantity {qty} in sweet spot ({QTY_SWEET_LOW}-{QTY_SWEET_HIGH})")
        elif qty < QTY_SWEET_LOW:
            score += WEIGHTS["qty_too_small"]
            notes.append(f"{WEIGHTS['qty_too_small']} quantity {qty} is small (< {QTY_SWEET_LOW})")
        else:
            score += WEIGHTS["qty_too_large"]
            notes.append(f"{WEIGHTS['qty_too_large']} quantity {qty} is large (> {QTY_SWEET_HIGH})")
    else:
        notes.append("0 quantity unknown")

    # --- Close date -------------------------------------------------------
    close = _parse_date(rfq.get("close_date"))
    if close is None:
        notes.append("0 close date unknown")
    else:
        days = (close - today).days
        if days < 0:
            score += WEIGHTS["close_past"]
            notes.append(f"{WEIGHTS['close_past']} solicitation already closed ({-days}d ago)")
        elif days <= 3:
            score += WEIGHTS["close_imminent"]
            notes.append(f"{WEIGHTS['close_imminent']} closes in {days}d (too soon)")
        elif days <= DAYS_TIGHT:
            score += WEIGHTS["close_tight"]
            notes.append(f"{WEIGHTS['close_tight']} closes in {days}d (tight turnaround)")
        else:
            score += WEIGHTS["close_comfortable"]
            notes.append(f"+{WEIGHTS['close_comfortable']} closes in {days}d (comfortable window)")

    # --- Set-aside --------------------------------------------------------
    set_aside = (rfq.get("set_aside") or "").lower()
    if "sole source" in set_aside:
        score += WEIGHTS["set_aside_sole_source"]
        notes.append(f"{WEIGHTS['set_aside_sole_source']} sole-source set-aside (hard to win)")
    elif "small business" in set_aside:
        score += WEIGHTS["set_aside_small_biz"]
        notes.append(f"+{WEIGHTS['set_aside_small_biz']} small business set-aside (less competition)")

    # --- Technical documents ---------------------------------------------
    if rfq.get("technical_documents_available"):
        score += WEIGHTS["tdp_available"]
        notes.append(f"{WEIGHTS['tdp_available']} technical documents required/available (more complexity)")
    else:
        score += WEIGHTS["tdp_not_required"]
        notes.append(f"+{WEIGHTS['tdp_not_required']} no TDP needed")

    # --- Approved sources -------------------------------------------------
    cage_count = _count_csv(rfq.get("approved_source_cages"))
    if cage_count == 0:
        score += WEIGHTS["approved_sources_open"]
        notes.append(f"+{WEIGHTS['approved_sources_open']} no approved-source restriction")
    elif cage_count == 1:
        score += WEIGHTS["approved_sources_restricted"]
        notes.append(f"{WEIGHTS['approved_sources_restricted']} single approved CAGE (restrictive)")
    elif cage_count >= 2:
        notes.append(f"0 {cage_count} approved CAGEs (neutral)")

    # --- URL sanity check -------------------------------------------------
    if not rfq.get("url"):
        score += WEIGHTS["no_solicitation_url"]
        notes.append(f"{WEIGHTS['no_solicitation_url']} no solicitation URL on record")

    # --- Sub-scores for the dashboard ------------------------------------
    margin = _margin_potential(rfq, fsc_info.category)
    competition = _competition_level(rfq)
    sourcing = _sourcing_difficulty(rfq, fsc_info.category)
    urgency = _urgency(rfq, today)

    return {
        "rfq_id": rfq["id"],
        "score": _clamp(score),
        "margin_potential": margin,
        "competition_level": competition,
        "sourcing_difficulty": sourcing,
        "urgency": urgency,
        "notes": "\n".join(notes),
    }


# ---------------------------------------------------------------------------
# Run-on-DB helpers
# ---------------------------------------------------------------------------

def score_all(today: date | None = None) -> int:
    """Re-score every RFQ in the database. Returns rows written."""
    today = today or date.today()
    rows_written = 0
    with get_connection() as conn:
        rfqs = conn.execute(
            "SELECT * FROM rfqs ORDER BY id ASC"
        ).fetchall()
        for rfq in rfqs:
            payload = score_rfq(dict(rfq), today=today)
            save_score(conn, payload)
            rows_written += 1
            logger.debug(
                "Scored %s -> %d", rfq["solicitation_number"], payload["score"]
            )
    logger.info("Wrote %d score row(s)", rows_written)
    return rows_written


def main() -> None:
    configure_logging()
    logger.info("Scoring against DB: %s", SETTINGS.db_path)
    n = score_all()
    print(f"Scored {n} RFQ(s).")


if __name__ == "__main__":
    main()
