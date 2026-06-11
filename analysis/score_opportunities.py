"""Score every RFQ on a 0-100 scale using the 6-subscore framework.

Framework (subscores sum to 100):

    SOURCEABILITY        0-20    Can we actually find / quote this item?
    COMPETITION          0-20    How crowded is the bid? (higher = less crowded)
    PROFIT POTENTIAL     0-20    Estimated gross margin
    CAPITAL EFFICIENCY   0-15    Fit for a ~$50K-working-capital firm
    TECHNICAL RISK       0-15    Certification / testing / TDP burden (higher = LESS risk)
    DELIVERY             0-10    Lead time (higher = MORE time)

The scorer also produces:
    estimated_capital_usd       qty x typical_unit_price (rough)
    estimated_margin_low/high   percent range
    estimated_win_probability   0..1
    recommended_action          BID IMMEDIATELY / INVESTIGATE SUPPLIER FIRST / AVOID

Every score comes with a detailed text explanation stored in
``opportunity_scores.notes``. Tune the constants below as you learn what
wins are repeatable vs. painful.

Run as a script (``python analysis/score_opportunities.py``) to (re)score
every RFQ currently in the DB.
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
    FscInfo,
    classify_fsc,
    estimate_unit_price,
    has_commodity_keyword,
    has_friendly_keyword,
    has_risky_keyword,
)
from db.database import get_connection, save_score  # noqa: E402
from utils.config import SETTINGS  # noqa: E402
from utils.logging_config import configure_logging  # noqa: E402

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tunable constants
# ---------------------------------------------------------------------------

# Assumed working capital available to the contractor. Used by the
# capital-efficiency subscore. Tune to your actual liquidity.
WORKING_CAPITAL_USD: int = 50_000

# Delivery-time bands (days until close), favoring longer lead times.
DELIVERY_BANDS: tuple[tuple[int, int, str], ...] = (
    (60, 10, "long lead time OK"),
    (30, 8,  "reasonable window"),
    (14, 5,  "tight but doable"),
    (7,  3,  "rushed"),
    (0,  1,  "very rushed"),
)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _parse_date(value: Any) -> date | None:
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


def _clamp(value: int | float, lo: int = 0, hi: int = 100) -> int:
    return int(max(lo, min(hi, value)))


def _count_csv(value: Any) -> int:
    if not value:
        return 0
    return len([p for p in str(value).split(",") if p.strip()])


def _money(usd: int | None) -> str:
    if usd is None:
        return "unknown"
    return f"~${usd:,}"


# ---------------------------------------------------------------------------
# Subscores -- each returns (score, notes)
# ---------------------------------------------------------------------------

def _score_sourceability(rfq: dict[str, Any], fsc: FscInfo, item_lower: str) -> tuple[int, list[str]]:
    """0-20. High = commercially available, multiple suppliers, standard hardware."""
    s = 0
    notes: list[str] = []

    # FSC tier contributes the bulk of the signal.
    if fsc.tier == "A":
        s += 12; notes.append(f"+12 FSC {fsc.code} is a preferred commercial category ({fsc.label})")
    elif fsc.tier == "B":
        s += 8;  notes.append(f"+8  FSC {fsc.code} ({fsc.label}) is specialty industrial but sourceable")
    elif fsc.tier == "C":
        s += 10; notes.append(f"+10 FSC {fsc.code} ({fsc.label}) is commodity / many distributors")
    elif fsc.tier == "D":
        s += 1;  notes.append(f"+1  FSC {fsc.code} ({fsc.label}) is hard-to-source / specialty")
    else:
        s += 5;  notes.append(f"+5  FSC {fsc.code or '?'} not categorized (assuming average)")

    # Item-name keywords nudge things up or down.
    fk = has_friendly_keyword(item_lower)
    if fk:
        s += 4; notes.append(f"+4  item name suggests commercial item ('{fk}')")
    rk = has_risky_keyword(item_lower)
    if rk:
        s -= 6; notes.append(f"-6  item name contains risky keyword ('{rk}')")

    # Approved CAGE breadth tells us how restricted sourcing is.
    cage_count = _count_csv(rfq.get("approved_source_cages"))
    if cage_count == 0:
        s += 4; notes.append("+4  no approved-source restriction (open market)")
    elif cage_count == 1:
        s -= 8; notes.append("-8  single approved CAGE (likely sole-source)")
    elif cage_count <= 3:
        s += 2; notes.append(f"+2  {cage_count} approved CAGEs (limited but workable)")
    else:
        s += 4; notes.append(f"+4  {cage_count} approved CAGEs (broad supplier base)")

    # TDP requirement implies build-to-print, not COTS.
    if rfq.get("technical_documents_available"):
        s -= 2; notes.append("-2  TDP required (drawing-controlled, not COTS)")

    return _clamp(s, 0, 20), notes


def _score_competition(rfq: dict[str, Any], fsc: FscInfo, item_lower: str) -> tuple[int, list[str]]:
    """0-20. Higher = LESS competition. Sweet spot is specialty (Tier B).

    The prompt says: 'specialized enough to discourage beginners but not a
    commodity everyone can quote.'
    """
    s = 0
    notes: list[str] = []

    if fsc.tier == "A":
        s += 14; notes.append(f"+14 FSC {fsc.code} ({fsc.label}) — specialty enough to filter bidders")
    elif fsc.tier == "B":
        s += 16; notes.append(f"+16 FSC {fsc.code} ({fsc.label}) — narrow specialty bidder pool")
    elif fsc.tier == "C":
        s += 6;  notes.append(f"+6  FSC {fsc.code} ({fsc.label}) — commodity; many bidders likely")
    elif fsc.tier == "D":
        s += 4;  notes.append(f"+4  FSC {fsc.code} — restricted but specialists dominate")
    else:
        s += 10; notes.append("+10 FSC tier unknown (assumed moderate competition)")

    ck = has_commodity_keyword(item_lower)
    if ck:
        s -= 4; notes.append(f"-4  generic commodity item ('{ck}'); large bidder pool")

    qty = rfq.get("quantity")
    if isinstance(qty, int):
        if qty <= 50:
            s += 2; notes.append(f"+2  qty {qty} is small; deters large distributors")
        elif qty <= 500:
            s += 4; notes.append(f"+4  qty {qty} is in sweet spot for small bidders")
        elif qty <= 5000:
            s += 0; notes.append(f"+0  qty {qty} is medium (no effect)")
        else:
            s -= 3; notes.append(f"-3  qty {qty} is large; attracts distributor bidders")

    set_aside = (rfq.get("set_aside") or "").lower()
    if "small business" in set_aside:
        s += 2; notes.append("+2  small business set-aside narrows the field")
    elif "sole source" in set_aside or "sole-source" in set_aside:
        s -= 15; notes.append("-15 sole-source set-aside (likely can't bid)")

    cage_count = _count_csv(rfq.get("approved_source_cages"))
    if 1 <= cage_count <= 3:
        s += 3; notes.append(f"+3  only {cage_count} approved CAGE(s); narrow bidder pool")

    return _clamp(s, 0, 20), notes


def _score_profit_potential(rfq: dict[str, Any], fsc: FscInfo, item_lower: str) -> tuple[int, list[str]]:
    """0-20. Estimated gross margin."""
    s = 0
    notes: list[str] = []

    if fsc.tier == "A":
        s += 14; notes.append(f"+14 specialty industrial (typical 15-25% margin)")
    elif fsc.tier == "B":
        s += 16; notes.append(f"+16 specialty electrical/mechanical (typical 18-30% margin)")
    elif fsc.tier == "C":
        s += 5;  notes.append(f"+5  commodity (typical 5-12% margin)")
    elif fsc.tier == "D":
        s += 12; notes.append(f"+12 high-risk category (potentially high margin if winnable)")
    else:
        s += 8;  notes.append(f"+8  uncategorized FSC (assumed 10-20% margin)")

    if has_risky_keyword(item_lower):
        s += 2; notes.append("+2  legacy/specialty keyword suggests pricing power")
    if rfq.get("technical_documents_available"):
        s += 2; notes.append("+2  TDP requirement filters competitors (margin lift)")

    return _clamp(s, 0, 20), notes


def _score_capital_efficiency(estimated_capital_usd: int | None) -> tuple[int, list[str]]:
    """0-15. Fit for a $50K-capital firm."""
    if estimated_capital_usd is None:
        return 7, ["Capital estimate unavailable; assumed average (+7/15)"]
    c = estimated_capital_usd
    if c < 500:
        return 6,  [f"+6  {_money(c)} — too small to be worth bidding"]
    if c <= 15_000:
        return 15, [f"+15 {_money(c)} — sweet spot for a ${WORKING_CAPITAL_USD:,}-capital firm"]
    if c <= 35_000:
        return 10, [f"+10 {_money(c)} — moderate capital lockup"]
    if c <= WORKING_CAPITAL_USD:
        return 5,  [f"+5  {_money(c)} — consumes most working capital"]
    return 0, [f"+0  {_money(c)} — exceeds ${WORKING_CAPITAL_USD:,} budget; needs financing"]


def _score_technical_risk(
    rfq: dict[str, Any],
    fsc: FscInfo,
    item_lower: str,
    text_blob: str,
) -> tuple[int, list[str]]:
    """0-15. Higher = LESS technical risk (per the prompt)."""
    s = 15
    notes: list[str] = [f"+15 baseline (assume no testing / no FAT / no hazmat)"]

    if fsc.tier == "D":
        s -= 8; notes.append(f"-8  high-risk FSC {fsc.code} ({fsc.label})")
    if has_risky_keyword(item_lower):
        s -= 5; notes.append("-5  risky keyword in item name")
    if rfq.get("technical_documents_available"):
        s -= 3; notes.append("-3  TDP required (build-to-print complexity)")

    test_keywords = (
        "first article", " fat ", "qualification test", "qpl", "qml",
        "calibration", "hazardous", "explosive", "shelf life",
    )
    for kw in test_keywords:
        if kw in text_blob:
            s -= 3; notes.append(f"-3  '{kw.strip()}' found in solicitation text")
            break  # only ding once

    return _clamp(s, 0, 15), notes


def _score_delivery(rfq: dict[str, Any], today: date) -> tuple[int, list[str]]:
    """0-10. Higher = more time to deliver."""
    close = _parse_date(rfq.get("close_date"))
    if close is None:
        return 4, ["+4  close date unknown (assumed average)"]
    days = (close - today).days
    if days < 0:
        return 0, [f"+0  closed {-days}d ago — can't bid"]
    for threshold, score, label in DELIVERY_BANDS:
        if days >= threshold:
            return score, [f"+{score} {days}d to close ({label})"]
    return 1, [f"+1  {days}d to close"]


# ---------------------------------------------------------------------------
# Derived estimates
# ---------------------------------------------------------------------------

def _estimate_capital(qty: Any, unit_price: float) -> int | None:
    if qty is None or not isinstance(qty, int) or qty <= 0:
        return None
    return int(round(qty * unit_price))


def _estimate_margin_range(fsc: FscInfo, has_tdp: bool) -> tuple[float, float]:
    """Return (low_pct, high_pct) estimated gross-margin range."""
    if fsc.tier == "A":
        lo, hi = 12.0, 22.0
    elif fsc.tier == "B":
        lo, hi = 18.0, 30.0
    elif fsc.tier == "C":
        lo, hi = 5.0, 12.0
    elif fsc.tier == "D":
        lo, hi = 25.0, 45.0
    else:
        lo, hi = 10.0, 20.0
    if has_tdp:
        lo += 3.0; hi += 3.0  # TDP shrinks the field, lifting margin
    return round(lo, 1), round(hi, 1)


def _estimate_win_probability(competition_score: int, sourceability_score: int, cage_count: int) -> float:
    """Rough 0..1 estimate. Higher competition_score = less competition."""
    p = 0.10
    p += (competition_score / 20.0) * 0.30   # up to +0.30
    p += (sourceability_score / 20.0) * 0.20 # up to +0.20
    if cage_count == 1:
        p *= 0.3
    elif cage_count == 2:
        p *= 0.6
    return round(min(0.70, max(0.02, p)), 2)


# ---------------------------------------------------------------------------
# Bucketing helpers used by the dashboard
# ---------------------------------------------------------------------------

def _profit_bucket(profit_score: int, margin_high: float) -> str:
    if profit_score >= 16 and margin_high >= 25:
        return "Very High"
    if profit_score >= 12 and margin_high >= 18:
        return "High"
    if profit_score >= 8:
        return "Medium"
    return "Low"


def _competition_bucket(competition_score: int) -> str:
    if competition_score >= 14:
        return "Low"
    if competition_score >= 8:
        return "Medium"
    return "High"


# ---------------------------------------------------------------------------
# Red flags + recommended action
# ---------------------------------------------------------------------------

def _red_flag(
    rfq: dict[str, Any],
    fsc: FscInfo,
    today: date,
    est_capital: int | None,
    item_lower: str,
) -> str | None:
    close = _parse_date(rfq.get("close_date"))
    if close and (close - today).days < 0:
        return "Solicitation already closed"
    if est_capital is not None and est_capital > WORKING_CAPITAL_USD:
        return f"Estimated capital {_money(est_capital)} exceeds ${WORKING_CAPITAL_USD:,} budget"
    if fsc.tier == "D" and has_risky_keyword(item_lower):
        return "Aviation-critical / weapons / hazardous category with risky keyword"
    cage_count = _count_csv(rfq.get("approved_source_cages"))
    if cage_count == 1 and rfq.get("technical_documents_available"):
        return "Single approved CAGE + TDP required (likely OEM-only sole-source)"
    set_aside = (rfq.get("set_aside") or "").lower()
    if "sole source" in set_aside or "sole-source" in set_aside:
        return "Sole-source set-aside"
    return None


def _recommend_action(
    total: int,
    sub: dict[str, int],
    red_flag: str | None,
) -> tuple[str, str]:
    """Return (action, one-line reason)."""
    if red_flag:
        return "AVOID", f"Red flag: {red_flag}."
    if total >= 70 and sub["sourceability"] >= 12 and sub["capital_efficiency"] >= 10:
        return "BID IMMEDIATELY", "Strong fit across the board; low risk, manageable capital, sourceable."
    if total >= 50:
        return "INVESTIGATE SUPPLIER FIRST", "Promising — verify supplier and pricing before quoting."
    return "AVOID", "Low overall score across multiple factors."


# ---------------------------------------------------------------------------
# Main scoring function
# ---------------------------------------------------------------------------

def score_rfq(rfq: dict[str, Any], today: date | None = None) -> dict[str, Any]:
    """Score a single RFQ dict. Returns a payload ready for ``save_score``."""
    today = today or date.today()

    fsc = classify_fsc(rfq.get("fsc"))
    item_lower = (rfq.get("item_name") or "").lower()
    # Truncate raw_text to keep keyword search bounded (some RFQs are huge).
    text_blob = item_lower + " " + (rfq.get("raw_text") or "").lower()[:5000]

    # Capital estimate (qty * rough unit price).
    unit_price = estimate_unit_price(rfq.get("fsc"), rfq.get("item_name"))
    est_capital = _estimate_capital(rfq.get("quantity"), unit_price)

    # 6 subscores.
    src, src_notes  = _score_sourceability(rfq, fsc, item_lower)
    comp, comp_notes = _score_competition(rfq, fsc, item_lower)
    prof, prof_notes = _score_profit_potential(rfq, fsc, item_lower)
    cap, cap_notes   = _score_capital_efficiency(est_capital)
    risk, risk_notes = _score_technical_risk(rfq, fsc, item_lower, text_blob)
    deliv, deliv_notes = _score_delivery(rfq, today)

    total = _clamp(src + comp + prof + cap + risk + deliv, 0, 100)

    # Derived estimates.
    margin_lo, margin_hi = _estimate_margin_range(fsc, bool(rfq.get("technical_documents_available")))
    cage_count = _count_csv(rfq.get("approved_source_cages"))
    win_prob = _estimate_win_probability(comp, src, cage_count)

    sub = {
        "sourceability": src, "competition": comp, "profit_potential": prof,
        "capital_efficiency": cap, "technical_risk": risk, "delivery": deliv,
    }
    red = _red_flag(rfq, fsc, today, est_capital, item_lower)
    action, action_reason = _recommend_action(total, sub, red)

    profit_bucket = _profit_bucket(prof, margin_hi)
    competition_bucket = _competition_bucket(comp)

    # ----------------------- Build the explanation -----------------------
    lines: list[str] = []
    lines.append(f"=== OVERALL: {total}/100  —  {action} ===")
    lines.append("")
    if red:
        lines.append(f"!! RED FLAG: {red}")
        lines.append("")
    sections = [
        (f"SOURCEABILITY: {src}/20",         src_notes),
        (f"COMPETITION:   {comp}/20  ({competition_bucket} competition expected)", comp_notes),
        (f"PROFIT POTENTIAL: {prof}/20  ({profit_bucket})", prof_notes),
        (f"CAPITAL EFFICIENCY: {cap}/15", cap_notes),
        (f"TECHNICAL RISK: {risk}/15  (higher = less risk)", risk_notes),
        (f"DELIVERY: {deliv}/10",           deliv_notes),
    ]
    for header, ns in sections:
        lines.append(header)
        for n in ns:
            lines.append(f"  {n}")
        lines.append("")

    lines.append("ESTIMATES")
    lines.append(f"  Capital required: {_money(est_capital)}")
    lines.append(f"  Margin range:     {margin_lo}% – {margin_hi}%")
    lines.append(f"  Win probability:  ~{int(win_prob * 100)}%")
    lines.append(f"  Profit potential: {profit_bucket}")
    lines.append(f"  Competition:      {competition_bucket}")
    lines.append("")
    lines.append(f"RECOMMENDATION: {action}")
    lines.append(f"  {action_reason}")

    notes_text = "\n".join(lines)

    # ----------------------- Legacy 0..100 KPIs --------------------------
    # The old dashboard widgets use these on a 0..100 scale. We derive them
    # from the new subscores so the existing detail panel keeps working
    # without code changes there.
    legacy_margin = int(round(prof * 5))                   # 0-20 -> 0-100
    legacy_competition = int(round((20 - comp) * 5))       # invert: high = more competition
    legacy_sourcing_diff = int(round((20 - src) * 5))      # invert: high = harder
    legacy_urgency = int(round((10 - deliv) * 10))         # invert: high = sooner

    return {
        "rfq_id": rfq["id"],
        "score": total,
        # v2 subscores
        "sourceability": src,
        "competition": comp,
        "profit_potential": prof,
        "capital_efficiency": cap,
        "technical_risk": risk,
        "delivery": deliv,
        # Derived estimates
        "estimated_capital_usd": est_capital,
        "estimated_margin_low": margin_lo,
        "estimated_margin_high": margin_hi,
        "estimated_win_probability": win_prob,
        "recommended_action": action,
        # Legacy 0..100 KPIs
        "margin_potential": legacy_margin,
        "competition_level": legacy_competition,
        "sourcing_difficulty": legacy_sourcing_diff,
        "urgency": legacy_urgency,
        "notes": notes_text,
    }


# ---------------------------------------------------------------------------
# Run-on-DB helpers
# ---------------------------------------------------------------------------

def score_all(today: date | None = None) -> int:
    """Re-score every RFQ in the database. Returns rows written."""
    today = today or date.today()
    rows_written = 0
    with get_connection() as conn:
        rfqs = conn.execute("SELECT * FROM rfqs ORDER BY id ASC").fetchall()
        for rfq in rfqs:
            payload = score_rfq(dict(rfq), today=today)
            save_score(conn, payload)
            rows_written += 1
            logger.debug(
                "Scored %s -> %d (%s)",
                rfq["solicitation_number"], payload["score"], payload["recommended_action"],
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
