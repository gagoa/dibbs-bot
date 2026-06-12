"""Score every RFQ on a 0-100 scale using the 7-subscore framework.

Framework (subscores sum to 100):

    SOURCEABILITY        0-18    Can we actually find / quote this item?
    COMPETITION          0-15    How crowded is the bid? (higher = less crowded)
    PROFIT POTENTIAL     0-15    Estimated gross margin
    TIME-TO-QUOTE        0-15    How fast can we turn it around? (higher = faster)
    TECHNICAL RISK       0-15    Certification / testing / TDP burden (higher = LESS risk)
    CAPITAL EFFICIENCY   0-12    Fit for available working capital
    DELIVERY             0-10    Lead time (peak 30-60 days)

The scorer also produces:
    estimated_capital_usd       qty * typical_unit_price (rough)
    estimated_margin_low/high   percent range
    estimated_quote_hours       est. supplier-research / quote-build time
    estimated_profit_per_hour   est. profit / quote hours
    estimated_win_probability   0..1
    recommended_action          BID IMMEDIATELY / INVESTIGATE SUPPLIER FIRST / AVOID

Calibration philosophy (per the scoring spec):
* The universe is BROAD -- any item a small distributor could realistically
  source. We do NOT preferentially favor fasteners.
* Scores are CONSERVATIVE. The average contract should land between 55 and
  70. Top 5% > 85. Top 0.5% > 95. Hard ceilings prevent inflation.
* Penalties are AGGRESSIVE: sole-source, TDP, risky keywords, capital >$25K,
  custom manufacturing, etc. each carry meaningful cuts.
* A "perfect score" (>=95) requires ALL of: multi-supplier, healthy margins,
  low competition, capital <20% of budget, realistic delivery, minimal tech
  risk, no compliance concerns, fast-quote.
* Custom-manufactured / engineering-required items are hard-excluded (capped
  at AVOID regardless of other subscores).

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
    is_likely_custom_or_engineered,
)
from db.database import get_connection, save_score  # noqa: E402
from utils.config import SETTINGS  # noqa: E402
from utils.logging_config import configure_logging  # noqa: E402

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tunable constants
# ---------------------------------------------------------------------------

# Assumed working capital available to the contractor.
WORKING_CAPITAL_USD: int = 50_000

# Capital-efficiency ceilings (USD). Tuned so a $50K firm gets max points for
# orders well below $10K (a "20% of capital" sweet spot per the spec).
CAPITAL_SWEET_HIGH: int = 10_000      # under this = max points
CAPITAL_MODERATE_HIGH: int = 25_000
CAPITAL_HEAVY_HIGH: int = 35_000

# Delivery sweet spot: too short = rushed, too long = ties up capital.
DELIVERY_MIN_DAYS: int = 30
DELIVERY_MAX_DAYS: int = 60

# Quote-time bands (hours).
QUOTE_FAST_HOURS: float = 0.5
QUOTE_GOOD_HOURS: float = 1.0
QUOTE_OK_HOURS:   float = 2.0
QUOTE_SLOW_HOURS: float = 4.0

# Hard ceilings on the overall score when major red flags are present.
# These prevent inflation from a single strong subscore. Tuned so the
# distribution lands roughly: avg 55-70, top 5% > 85, top 0.5% > 95.
CEILING_NORMAL:           int = 100
CEILING_SOLE_SOURCE:      int = 84   # single approved CAGE
CEILING_TDP_REQUIRED:     int = 89   # build-to-print
CEILING_RISKY_KEYWORD:    int = 84   # aerospace-critical / hazmat etc.
CEILING_BIG_CAPITAL:      int = 84   # > moderate cap ($25K)
CEILING_VERY_BIG_CAPITAL: int = 69   # > heavy cap ($35K)
CEILING_TIER_D:           int = 74   # weapons / hazmat / chemicals
CEILING_CUSTOM:           int = 49   # hard exclusion (custom/engineered)
CEILING_CLOSED:           int = 39   # already closed

# Perfect-score gating (must hit ALL conditions to score >= 95).
PERFECT_SCORE_MIN_OVERALL: int = 95


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


def _days_to_close(rfq: dict[str, Any], today: date) -> int | None:
    close = _parse_date(rfq.get("close_date"))
    if close is None:
        return None
    return (close - today).days


# ---------------------------------------------------------------------------
# Subscores -- each returns (score, notes)
# ---------------------------------------------------------------------------

def _score_sourceability(rfq: dict[str, Any], fsc: FscInfo, item_lower: str) -> tuple[int, list[str]]:
    """0-18. High = commercially available, multiple suppliers, distributor-friendly."""
    s = 10  # neutral starting baseline (assume sourceable by default)
    notes: list[str] = ["+10 baseline (assume distributor-sourceable)"]

    if fsc.tier == "A":
        s += 4; notes.append(f"+4  FSC {fsc.code} ({fsc.label}) is specialty industrial; distributors carry stock")
    elif fsc.tier == "B":
        s += 2; notes.append(f"+2  FSC {fsc.code} ({fsc.label}) is sourceable from specialty distributors")
    elif fsc.tier == "C":
        s += 1; notes.append(f"+1  FSC {fsc.code} ({fsc.label}) is commodity; widely available")
    elif fsc.tier == "D":
        s -= 6; notes.append(f"-6  FSC {fsc.code} ({fsc.label}) is hard to source for a small distributor")

    fk = has_friendly_keyword(item_lower)
    if fk:
        s += 1; notes.append(f"+1  item name suggests commercial item ('{fk}')")

    rk = has_risky_keyword(item_lower)
    if rk:
        s -= 3; notes.append(f"-3  item name contains risky keyword ('{rk}')")

    cage_count = _count_csv(rfq.get("approved_source_cages"))
    if cage_count == 0:
        s += 2; notes.append("+2  no approved-source restriction (open market)")
    elif cage_count == 1:
        s -= 7; notes.append("-7  single approved CAGE (sole-source)")
    elif cage_count == 2:
        s -= 1; notes.append("-1  only 2 approved CAGEs")
    elif cage_count <= 4:
        s += 1; notes.append(f"+1  {cage_count} approved CAGEs")
    else:
        s += 2; notes.append(f"+2  {cage_count} approved CAGEs (broad supplier base)")

    if rfq.get("technical_documents_available"):
        s -= 3; notes.append("-3  TDP required (drawing-controlled)")

    return _clamp(s, 0, 18), notes


def _score_competition(rfq: dict[str, Any], fsc: FscInfo, item_lower: str) -> tuple[int, list[str]]:
    """0-15. Higher = LESS competition. Sweet spot is specialty without being closed."""
    s = 8  # neutral baseline (moderate competition assumed)
    notes: list[str] = ["+8  baseline (moderate competition)"]

    if fsc.tier == "A":
        s += 3; notes.append(f"+3  FSC {fsc.code} narrows to distributors with specialty inventory")
    elif fsc.tier == "B":
        s += 3; notes.append(f"+3  FSC {fsc.code} attracts a moderate specialty bidder pool")
    elif fsc.tier == "C":
        s -= 4; notes.append(f"-4  FSC {fsc.code} is commodity; many bidders likely")
    elif fsc.tier == "D":
        s -= 1; notes.append(f"-1  FSC {fsc.code} -- specialists dominate")

    ck = has_commodity_keyword(item_lower)
    if ck:
        s -= 3; notes.append(f"-3  generic commodity item ('{ck}'); large bidder pool")

    qty = rfq.get("quantity")
    if isinstance(qty, int):
        if qty <= 25:
            s += 1; notes.append(f"+1  qty {qty} is tiny; large distributors won't bother")
        elif qty <= 250:
            s += 2; notes.append(f"+2  qty {qty} is in the small-distributor sweet spot")
        elif qty <= 2_500:
            s -= 1; notes.append(f"-1  qty {qty} is medium")
        else:
            s -= 3; notes.append(f"-3  qty {qty} is large; many bidders")

    set_aside = (rfq.get("set_aside") or "").lower()
    if "small business" in set_aside:
        s += 2; notes.append("+2  small business set-aside narrows the field")
    if "sole source" in set_aside or "sole-source" in set_aside:
        s -= 10; notes.append("-10 sole-source set-aside (likely can't bid)")

    cage_count = _count_csv(rfq.get("approved_source_cages"))
    if cage_count == 1:
        s -= 2; notes.append("-2  single approved CAGE (specialists may dominate)")
    elif 2 <= cage_count <= 3:
        s += 1; notes.append(f"+1  only {cage_count} CAGE(s); narrow bidder pool")

    return _clamp(s, 0, 15), notes


def _score_profit_potential(rfq: dict[str, Any], fsc: FscInfo, item_lower: str) -> tuple[int, list[str]]:
    """0-15. Estimated gross margin."""
    s = 8  # baseline ~10-15% margin
    notes: list[str] = ["+8  baseline (assume ~10-15% margin)"]

    if fsc.tier == "A":
        s += 3; notes.append("+3  specialty industrial (typical 15-25% margin)")
    elif fsc.tier == "B":
        s += 4; notes.append("+4  specialty electrical / mechanical (typical 18-30% margin)")
    elif fsc.tier == "C":
        s -= 4; notes.append("-4  commodity (typical 5-12% margin)")
    elif fsc.tier == "D":
        s += 0; notes.append("+0  high-risk category (margin potential offset by risk)")

    if has_risky_keyword(item_lower):
        s += 1; notes.append("+1  legacy/specialty keyword suggests modest pricing power")
    if has_commodity_keyword(item_lower):
        s -= 2; notes.append("-2  generic commodity item (margin pressure)")
    if rfq.get("technical_documents_available"):
        s += 1; notes.append("+1  TDP requirement filters some competitors")

    return _clamp(s, 0, 15), notes


def _score_time_to_quote(quote_hours: float) -> tuple[int, list[str]]:
    """0-15. HEAVILY reward fast-quote opportunities (per the spec).

    Time-to-quote is the estimated time to research suppliers, pull pricing,
    and build the bid response. For a one-person shop this directly trades
    against deal count -- a 15-minute quote with 15% margin compounds
    faster than a 6-hour quote with 25% margin.
    """
    if quote_hours <= QUOTE_FAST_HOURS:
        return 15, [f"+15 ~{int(quote_hours * 60)} min to quote (excellent throughput)"]
    if quote_hours <= QUOTE_GOOD_HOURS:
        return 12, [f"+12 ~{quote_hours:.1f}h to quote (fast)"]
    if quote_hours <= QUOTE_OK_HOURS:
        return 8,  [f"+8  ~{quote_hours:.1f}h to quote (acceptable)"]
    if quote_hours <= QUOTE_SLOW_HOURS:
        return 4,  [f"+4  ~{quote_hours:.1f}h to quote (slow; competes with other work)"]
    return 1, [f"+1  ~{quote_hours:.1f}h to quote (very slow; rarely worth it)"]


def _score_capital_efficiency(estimated_capital_usd: int | None) -> tuple[int, list[str]]:
    """0-12. Fit for available working capital."""
    if estimated_capital_usd is None:
        return 6, ["Capital estimate unavailable; assumed average (+6/12)"]
    c = estimated_capital_usd
    if c < 500:
        return 5,  [f"+5  {_money(c)} -- too small to be worth the overhead"]
    if c <= CAPITAL_SWEET_HIGH:
        return 12, [f"+12 {_money(c)} -- under 20% of ${WORKING_CAPITAL_USD:,} budget (sweet spot)"]
    if c <= CAPITAL_MODERATE_HIGH:
        return 9,  [f"+9  {_money(c)} -- moderate capital lockup"]
    if c <= CAPITAL_HEAVY_HIGH:
        return 5,  [f"+5  {_money(c)} -- heavy lockup; ties up most working capital"]
    if c <= WORKING_CAPITAL_USD:
        return 2,  [f"+2  {_money(c)} -- consumes nearly all working capital"]
    return 0, [f"+0  {_money(c)} -- exceeds ${WORKING_CAPITAL_USD:,} budget; needs financing"]


def _score_technical_risk(
    rfq: dict[str, Any],
    fsc: FscInfo,
    item_lower: str,
    text_blob: str,
) -> tuple[int, list[str]]:
    """0-15. Higher = LESS technical risk."""
    s = 15
    notes: list[str] = ["+15 baseline (assume no testing / FAT / hazmat)"]

    if fsc.tier == "D":
        s -= 8; notes.append(f"-8  high-risk FSC {fsc.code} ({fsc.label})")
    if has_risky_keyword(item_lower):
        s -= 5; notes.append("-5  risky keyword in item name (aerospace-critical / hazmat / etc.)")
    if rfq.get("technical_documents_available"):
        s -= 4; notes.append("-4  TDP required (build-to-print complexity)")

    test_keywords = (
        "first article", " fat ", "qualification test", "qpl", "qml",
        "calibration", "hazardous", "explosive", "shelf life",
    )
    for kw in test_keywords:
        if kw in text_blob:
            s -= 4; notes.append(f"-4  '{kw.strip()}' in solicitation text")
            break  # only ding once

    return _clamp(s, 0, 15), notes


def _score_delivery(days_to_close: int | None) -> tuple[int, list[str]]:
    """0-10. Sweet spot 30-60 days. Penalize both rushed AND too-long.

    Long delivery windows (90+ days) tie up working capital, so they aren't
    pure positives -- a sweet spot in the middle gets max points.
    """
    if days_to_close is None:
        return 4, ["+4  close date unknown (assumed average)"]
    d = days_to_close
    if d < 0:
        return 0, [f"+0  closed {-d}d ago -- can't bid"]
    if d < 7:
        return 1, [f"+1  only {d}d to close (very rushed)"]
    if d < 14:
        return 3, [f"+3  {d}d to close (rushed)"]
    if d < DELIVERY_MIN_DAYS:
        return 6, [f"+6  {d}d to close (tight but workable)"]
    if d <= DELIVERY_MAX_DAYS:
        return 10, [f"+10 {d}d to close (sweet spot: 30-60d window)"]
    if d <= 90:
        return 7, [f"+7  {d}d to close (long; capital ties up)"]
    return 4, [f"+4  {d}d to close (very long; slows turnover)"]


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
        lo, hi = 15.0, 25.0
    elif fsc.tier == "B":
        lo, hi = 18.0, 30.0
    elif fsc.tier == "C":
        lo, hi = 5.0, 12.0
    elif fsc.tier == "D":
        lo, hi = 22.0, 40.0
    else:
        lo, hi = 10.0, 20.0
    if has_tdp:
        lo += 3.0; hi += 3.0
    return round(lo, 1), round(hi, 1)


def _estimate_quote_hours(
    rfq: dict[str, Any],
    fsc: FscInfo,
    item_lower: str,
) -> float:
    """Rough estimate of supplier-research + quote-build hours.

    A one-person operation should heavily prefer 15-minute quotes over 6-hour
    quotes even at similar margins -- this estimate feeds both the
    time_to_quote subscore and the profit_per_hour derived metric.
    """
    # Base from FSC tier (Tier A items are usually catalog look-ups).
    if fsc.tier == "A":
        base = 0.5
    elif fsc.tier == "B":
        base = 1.25
    elif fsc.tier == "C":
        base = 0.5
    elif fsc.tier == "D":
        base = 4.0
    else:
        base = 1.5

    cage_count = _count_csv(rfq.get("approved_source_cages"))
    if cage_count == 1:
        base += 2.0  # sole-source means hunting down the OEM
    elif cage_count == 2:
        base += 0.5
    elif cage_count >= 5:
        base -= 0.25

    if rfq.get("technical_documents_available"):
        base += 1.5  # pulling drawings, reading specs, etc.

    if has_risky_keyword(item_lower):
        base += 1.5  # extra research / cert chasing

    if has_commodity_keyword(item_lower):
        base = max(0.25, base - 0.25)  # catalog lookup

    return round(max(0.25, base), 2)


def _estimate_profit_per_hour(
    est_capital_usd: int | None,
    margin_mid_pct: float,
    quote_hours: float,
) -> float | None:
    """Estimated dollar-profit per hour of procurement effort."""
    if est_capital_usd is None or quote_hours <= 0:
        return None
    est_profit = est_capital_usd * (margin_mid_pct / 100.0)
    return round(est_profit / quote_hours, 2)


def _estimate_win_probability(competition_score: int, sourceability_score: int, cage_count: int) -> float:
    """Rough 0..1 estimate. Higher competition_score = less competition."""
    p = 0.08
    p += (competition_score / 15.0) * 0.30   # up to +0.30
    p += (sourceability_score / 18.0) * 0.20 # up to +0.20
    if cage_count == 1:
        p *= 0.25
    elif cage_count == 2:
        p *= 0.55
    return round(min(0.65, max(0.02, p)), 2)


# ---------------------------------------------------------------------------
# Bucketing helpers used by the dashboard
# ---------------------------------------------------------------------------

def _profit_bucket(profit_score: int, margin_high: float) -> str:
    if profit_score >= 12 and margin_high >= 25:
        return "Very High"
    if profit_score >= 9 and margin_high >= 18:
        return "High"
    if profit_score >= 6:
        return "Medium"
    return "Low"


def _competition_bucket(competition_score: int) -> str:
    if competition_score >= 11:
        return "Low"
    if competition_score >= 6:
        return "Medium"
    return "High"


# ---------------------------------------------------------------------------
# Score ceilings & perfect-score gating
# ---------------------------------------------------------------------------

def _compute_ceiling(
    rfq: dict[str, Any],
    fsc: FscInfo,
    item_lower: str,
    text_blob: str,
    est_capital: int | None,
    days_to_close: int | None,
) -> tuple[int, list[str]]:
    """Return (ceiling, reasons). Score will be clamped to this ceiling.

    The lowest applicable ceiling wins -- e.g. a sole-source RFQ that's also
    custom-engineered gets capped at CEILING_CUSTOM (49), not 79.
    """
    reasons: list[str] = []
    ceiling = CEILING_NORMAL

    if days_to_close is not None and days_to_close < 0:
        ceiling = min(ceiling, CEILING_CLOSED)
        reasons.append(f"closed {-days_to_close}d ago")

    custom_kw = is_likely_custom_or_engineered(item_lower, rfq.get("raw_text"))
    if custom_kw:
        ceiling = min(ceiling, CEILING_CUSTOM)
        reasons.append(f"custom/engineered keyword: '{custom_kw}'")

    if fsc.tier == "D":
        ceiling = min(ceiling, CEILING_TIER_D)
        reasons.append(f"Tier-D FSC {fsc.code} ({fsc.label})")

    if est_capital is not None and est_capital > WORKING_CAPITAL_USD:
        ceiling = min(ceiling, 39)  # over budget -- effectively AVOID
        reasons.append(f"capital {_money(est_capital)} exceeds ${WORKING_CAPITAL_USD:,}")
    elif est_capital is not None and est_capital > CAPITAL_HEAVY_HIGH:
        ceiling = min(ceiling, CEILING_VERY_BIG_CAPITAL)
        reasons.append(f"capital {_money(est_capital)} > ${CAPITAL_HEAVY_HIGH:,}")
    elif est_capital is not None and est_capital > CAPITAL_MODERATE_HIGH:
        ceiling = min(ceiling, CEILING_BIG_CAPITAL)
        reasons.append(f"capital {_money(est_capital)} > ${CAPITAL_MODERATE_HIGH:,}")

    if has_risky_keyword(item_lower):
        ceiling = min(ceiling, CEILING_RISKY_KEYWORD)
        reasons.append("risky keyword (aerospace-critical / hazmat / etc.)")

    if rfq.get("technical_documents_available"):
        ceiling = min(ceiling, CEILING_TDP_REQUIRED)
        reasons.append("TDP required")

    cage_count = _count_csv(rfq.get("approved_source_cages"))
    if cage_count == 1:
        ceiling = min(ceiling, CEILING_SOLE_SOURCE)
        reasons.append("single approved CAGE (sole-source)")

    set_aside = (rfq.get("set_aside") or "").lower()
    if "sole source" in set_aside or "sole-source" in set_aside:
        ceiling = min(ceiling, CEILING_SOLE_SOURCE)
        reasons.append("sole-source set-aside")

    return ceiling, reasons


def _meets_perfect_conditions(
    sub: dict[str, int],
    est_capital: int | None,
    days_to_close: int | None,
    margin_high: float,
    quote_hours: float,
    cage_count: int,
    item_lower: str,
    fsc: FscInfo,
    rfq: dict[str, Any],
) -> tuple[bool, list[str]]:
    """Check the perfect-score (>=95) conditions. Returns (ok, missing).

    Per the spec, ALL of these must be true to award 95+:
    * Multiple verified suppliers
    * Healthy margins (>= 25%)
    * Low competition
    * Capital < 20% of available
    * Realistic delivery (30-60d)
    * Minimal tech risk
    * Item has repeat-purchase history (approximated)
    * No significant compliance concerns
    * Quick-quote (proxy for "easy to repeat")
    """
    missing: list[str] = []

    if cage_count == 1:
        missing.append("only 1 approved CAGE (need multi-supplier)")
    if margin_high < 25:
        missing.append(f"estimated margin top {margin_high}% < 25%")
    if sub["competition"] < 12:
        missing.append("competition subscore < 12/15 (bidder pool not narrow enough)")
    if est_capital is None or est_capital > CAPITAL_SWEET_HIGH:
        missing.append("capital required > 20% of budget")
    if days_to_close is None or days_to_close < DELIVERY_MIN_DAYS or days_to_close > DELIVERY_MAX_DAYS:
        missing.append("delivery window outside 30-60d sweet spot")
    if sub["technical_risk"] < 13:
        missing.append("technical risk subscore < 13/15")
    if has_risky_keyword(item_lower) or rfq.get("technical_documents_available"):
        missing.append("compliance / TDP concern present")
    if quote_hours > QUOTE_GOOD_HOURS:
        missing.append(f"quote time {quote_hours}h > {QUOTE_GOOD_HOURS}h (not fast enough)")
    if fsc.tier == "D":
        missing.append("Tier-D FSC (poor distributor fit)")

    return (len(missing) == 0), missing


# ---------------------------------------------------------------------------
# Recommended action
# ---------------------------------------------------------------------------

def _recommend_action(total: int, ceiling: int, custom_kw: str | None) -> tuple[str, str]:
    """Map the final score + red flags to BID / INVESTIGATE / AVOID."""
    if custom_kw:
        return "AVOID", f"Custom-manufactured / engineering required ('{custom_kw}'); outside small-distributor scope."
    if ceiling <= CEILING_CLOSED:
        return "AVOID", "Solicitation has already closed."
    if total < 60:
        return "AVOID", "Low overall score across multiple factors."
    if total >= 80 and ceiling >= CEILING_NORMAL:
        return "BID IMMEDIATELY", "Strong fit across the board; no major red flags."
    if total >= 80:
        return "INVESTIGATE SUPPLIER FIRST", "High score but a red flag is in play; verify before quoting."
    return "INVESTIGATE SUPPLIER FIRST", "Promising -- verify supplier and pricing before quoting."


# ---------------------------------------------------------------------------
# Main scoring function
# ---------------------------------------------------------------------------

def score_rfq(rfq: dict[str, Any], today: date | None = None) -> dict[str, Any]:
    """Score a single RFQ dict. Returns a payload ready for ``save_score``."""
    today = today or date.today()

    fsc = classify_fsc(rfq.get("fsc"))
    item_lower = (rfq.get("item_name") or "").lower()
    text_blob = item_lower + " " + (rfq.get("raw_text") or "").lower()[:5000]
    days_to_close = _days_to_close(rfq, today)

    # --------------------- Derived estimates ---------------------
    unit_price = estimate_unit_price(rfq.get("fsc"), rfq.get("item_name"))
    est_capital = _estimate_capital(rfq.get("quantity"), unit_price)
    margin_lo, margin_hi = _estimate_margin_range(fsc, bool(rfq.get("technical_documents_available")))
    margin_mid = (margin_lo + margin_hi) / 2.0
    quote_hours = _estimate_quote_hours(rfq, fsc, item_lower)
    profit_per_hour = _estimate_profit_per_hour(est_capital, margin_mid, quote_hours)

    # --------------------- 7 subscores ---------------------
    src, src_notes      = _score_sourceability(rfq, fsc, item_lower)
    comp, comp_notes    = _score_competition(rfq, fsc, item_lower)
    prof, prof_notes    = _score_profit_potential(rfq, fsc, item_lower)
    ttq, ttq_notes      = _score_time_to_quote(quote_hours)
    risk, risk_notes    = _score_technical_risk(rfq, fsc, item_lower, text_blob)
    cap, cap_notes      = _score_capital_efficiency(est_capital)
    deliv, deliv_notes  = _score_delivery(days_to_close)

    raw_total = src + comp + prof + ttq + risk + cap + deliv

    # --------------------- Apply ceilings ---------------------
    ceiling, ceiling_reasons = _compute_ceiling(
        rfq, fsc, item_lower, text_blob, est_capital, days_to_close,
    )
    capped_total = min(raw_total, ceiling)

    # --------------------- Perfect-score gating ---------------------
    sub = {
        "sourceability": src, "competition": comp, "profit_potential": prof,
        "time_to_quote": ttq, "capital_efficiency": cap,
        "technical_risk": risk, "delivery": deliv,
    }
    cage_count = _count_csv(rfq.get("approved_source_cages"))
    perfect_ok, perfect_missing = _meets_perfect_conditions(
        sub, est_capital, days_to_close, margin_hi, quote_hours,
        cage_count, item_lower, fsc, rfq,
    )
    if not perfect_ok and capped_total >= PERFECT_SCORE_MIN_OVERALL:
        capped_total = PERFECT_SCORE_MIN_OVERALL - 1

    total = _clamp(capped_total, 0, 100)

    # --------------------- Action recommendation ---------------------
    custom_kw = is_likely_custom_or_engineered(item_lower, rfq.get("raw_text"))
    action, action_reason = _recommend_action(total, ceiling, custom_kw)

    win_prob = _estimate_win_probability(comp, src, cage_count)
    profit_bucket = _profit_bucket(prof, margin_hi)
    competition_bucket = _competition_bucket(comp)

    # --------------------- Notes (the "detailed explanation") ---------------------
    lines: list[str] = []
    lines.append(f"=== OVERALL: {total}/100  --  {action} ===")
    lines.append("")
    if ceiling < CEILING_NORMAL:
        lines.append(f"!! CEILING APPLIED: capped at {ceiling}/100")
        for r in ceiling_reasons:
            lines.append(f"     - {r}")
        lines.append("")
    if not perfect_ok and raw_total >= 90:
        lines.append("!! PERFECT-SCORE GATING: would have scored >=95, but conditions not all met:")
        for m in perfect_missing[:5]:
            lines.append(f"     - {m}")
        lines.append("")
    sections = [
        (f"SOURCEABILITY: {src}/18",      src_notes),
        (f"COMPETITION:   {comp}/15  ({competition_bucket} competition expected)", comp_notes),
        (f"PROFIT POTENTIAL: {prof}/15  ({profit_bucket})", prof_notes),
        (f"TIME-TO-QUOTE: {ttq}/15  (~{quote_hours}h estimated)", ttq_notes),
        (f"TECHNICAL RISK: {risk}/15  (higher = less risk)", risk_notes),
        (f"CAPITAL EFFICIENCY: {cap}/12", cap_notes),
        (f"DELIVERY: {deliv}/10",         deliv_notes),
    ]
    for header, ns in sections:
        lines.append(header)
        for n in ns:
            lines.append(f"  {n}")
        lines.append("")

    lines.append("ESTIMATES")
    lines.append(f"  Capital required: {_money(est_capital)}")
    lines.append(f"  Margin range:     {margin_lo}% - {margin_hi}%")
    lines.append(f"  Win probability:  ~{int(win_prob * 100)}%")
    lines.append(f"  Quote time:       ~{quote_hours}h")
    pph_str = f"${profit_per_hour:,.0f}/h" if profit_per_hour is not None else "unknown"
    lines.append(f"  Profit/hour:      {pph_str}")
    lines.append(f"  Profit potential: {profit_bucket}")
    lines.append(f"  Competition:      {competition_bucket}")
    lines.append("")
    lines.append(f"RECOMMENDATION: {action}")
    lines.append(f"  {action_reason}")

    notes_text = "\n".join(lines)

    # --------------------- Legacy 0..100 KPIs (backward compat) ---------------------
    legacy_margin = int(round(prof / 15.0 * 100))           # 0..15 -> 0..100
    legacy_competition = int(round((15 - comp) / 15.0 * 100))
    legacy_sourcing_diff = int(round((18 - src) / 18.0 * 100))
    legacy_urgency = int(round((10 - deliv) / 10.0 * 100))

    return {
        "rfq_id": rfq["id"],
        "score": total,
        # 7 subscores
        "sourceability": src,
        "competition": comp,
        "profit_potential": prof,
        "time_to_quote": ttq,
        "capital_efficiency": cap,
        "technical_risk": risk,
        "delivery": deliv,
        # Derived estimates
        "estimated_capital_usd": est_capital,
        "estimated_margin_low": margin_lo,
        "estimated_margin_high": margin_hi,
        "estimated_win_probability": win_prob,
        "estimated_quote_hours": quote_hours,
        "estimated_profit_per_hour": profit_per_hour,
        "recommended_action": action,
        # Legacy KPIs
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
