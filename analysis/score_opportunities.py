"""Score every RFQ on a 0-100 scale, tuned for a BEGINNING contractor.

Framework (subscores sum to 100):

    SOURCEABILITY        0-25    Can we actually find / buy this item? (AMSC-driven)
    PROFIT POTENTIAL     0-20    Margin % AND absolute dollars of expected profit
    TECHNICAL RISK       0-15    Certs / testing / TDP / QPL burden (higher = LESS risk)
    COMPETITION          0-12    How crowded is the bid? (higher = less crowded)
    TIME-TO-QUOTE        0-10    How fast can we turn it around? (higher = faster)
    CAPITAL EFFICIENCY   0-10    Fit for a small firm's working capital
    RESPONSE WINDOW      0-8     Days left to research + submit a quote

The top of the leaderboard is deliberately biased toward contracts a
first-time DIBBS bidder can realistically win and fulfill:

* AMSC is the backbone of sourceability. Z (commercial/COTS) and G (gov't
  owns the full tech data package) are the two codes every "getting started
  on DIBBS" guide tells you to filter for -- anyone can source and bid them.
  Restricted codes (B/C/D/H/P/Q/R/S) mean approved-source-only: a newcomer
  effectively can't compete, so they're hard-capped into the AVOID band.
* Profit potential now counts absolute dollars, not just margin %. A $120
  order at 30% margin is still only ~$36 of profit -- not worth an hour of
  work. Margin % and expected profit dollars are scored together.
* Capital sweet spot is $500-$5K: big enough to matter, small enough that a
  beginner can float it and a mistake isn't fatal.
* The response window rewards RFQs with enough runway to research suppliers
  (7-35 days), instead of the old "delivery 30-60d" logic which was really
  measuring the same close date anyway.

Calibration:
* Average contract lands ~50-65. Top 5% > 80. 95+ requires the full
  beginner-dream checklist (see _meets_perfect_conditions).
* Hard ceilings keep one strong subscore from masking a disqualifier.
* Custom-manufactured / engineering-required items are hard-excluded.

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
    AmscInfo,
    FscInfo,
    classify_amsc,
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

# Capital bands (USD). The beginner sweet spot is $500-$5K: real money but
# survivable if something goes wrong.
CAPITAL_SWEET_LOW:    int = 500
CAPITAL_SWEET_HIGH:   int = 5_000
CAPITAL_GOOD_HIGH:    int = 10_000
CAPITAL_MODERATE_HIGH: int = 25_000
CAPITAL_HEAVY_HIGH:   int = 35_000

# Response-window sweet spot (days until the RFQ closes): enough time to
# find a supplier and quote without rushing.
WINDOW_SWEET_LOW:  int = 7
WINDOW_SWEET_HIGH: int = 35

# Quote-time bands (hours).
QUOTE_FAST_HOURS: float = 0.5
QUOTE_GOOD_HOURS: float = 1.0
QUOTE_OK_HOURS:   float = 2.0
QUOTE_SLOW_HOURS: float = 4.0

# Minimum expected profit (mid-margin estimate) for a deal to be worth the
# paperwork at all.
MIN_WORTHWHILE_PROFIT_USD: int = 100

# Hard ceilings on the overall score when major red flags are present.
# The AVOID band starts below 60, so a ceiling under 60 forces AVOID.
CEILING_NORMAL:            int = 100
CEILING_TDP_REQUIRED:      int = 85   # build-to-print
CEILING_RISKY_KEYWORD:     int = 80   # aerospace-critical / hazmat etc.
CEILING_BIG_CAPITAL:       int = 75   # > $25K
CEILING_TIER_D:            int = 70   # weapons / hazmat / medical
CEILING_AMSC_QUALIFIED:    int = 68   # QPL / special tooling / testing
CEILING_SOLE_SOURCE_CAGE:  int = 65   # single approved CAGE
CEILING_VERY_BIG_CAPITAL:  int = 60   # > $35K
CEILING_SOLE_SOURCE_SA:    int = 55   # sole-source set-aside
CEILING_AMSC_RESTRICTED:   int = 54   # approved-source-only -> AVOID band
CEILING_TINY_PROFIT:       int = 55   # expected profit < $100
CEILING_CUSTOM:            int = 45   # hard exclusion (custom/engineered)
CEILING_OVER_BUDGET:       int = 35   # capital exceeds working capital
CEILING_CLOSED:            int = 35   # already closed

# Action thresholds. BID is deliberately selective (~top 5% of a typical
# daily pull) so the "Top Opportunities" tab stays a short, high-quality list.
BID_MIN_SCORE:   int = 80
AVOID_MAX_SCORE: int = 59

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


def _money(usd: int | float | None) -> str:
    if usd is None:
        return "unknown"
    return f"~${int(usd):,}"


def _days_to_close(rfq: dict[str, Any], today: date) -> int | None:
    close = _parse_date(rfq.get("close_date"))
    if close is None:
        return None
    return (close - today).days


# ---------------------------------------------------------------------------
# Subscores -- each returns (score, notes)
# ---------------------------------------------------------------------------

def _score_sourceability(
    rfq: dict[str, Any], fsc: FscInfo, amsc: AmscInfo, item_lower: str,
) -> tuple[int, list[str]]:
    """0-25. High = a beginner can find this part and quote it today.

    AMSC is the government's own statement of how the item can be bought,
    so it anchors this subscore.
    """
    s = 0
    notes: list[str] = []

    if amsc.bucket == "commercial":
        s += 10; notes.append(f"+10 AMSC {amsc.code}: {amsc.label}")
    elif amsc.bucket == "open":
        s += 8; notes.append(f"+8  AMSC {amsc.code}: {amsc.label}")
    elif amsc.bucket == "moderate":
        s += 5; notes.append(f"+5  AMSC {amsc.code}: {amsc.label}")
    elif amsc.bucket == "qualified":
        s += 1; notes.append(f"+1  AMSC {amsc.code}: {amsc.label}")
    elif amsc.bucket == "restricted":
        s += 0; notes.append(f"+0  AMSC {amsc.code}: {amsc.label}")
    else:
        s += 4; notes.append("+4  AMSC unknown (not in source data); assumed average")

    if fsc.tier == "A":
        s += 6; notes.append(f"+6  FSC {fsc.code} ({fsc.label}): specialty industrial; distributors stock it")
    elif fsc.tier == "B":
        s += 4; notes.append(f"+4  FSC {fsc.code} ({fsc.label}): sourceable from specialty distributors")
    elif fsc.tier == "C":
        s += 3; notes.append(f"+3  FSC {fsc.code} ({fsc.label}): commodity; trivially easy to source")
    elif fsc.tier == "D":
        s -= 4; notes.append(f"-4  FSC {fsc.code} ({fsc.label}): hard for a small distributor")
    else:
        s += 3; notes.append("+3  FSC unknown; assumed sourceable")

    fk = has_friendly_keyword(item_lower)
    if fk:
        s += 2; notes.append(f"+2  item name suggests a commercial part ('{fk}')")

    rk = has_risky_keyword(item_lower)
    if rk:
        s -= 3; notes.append(f"-3  item name contains risky keyword ('{rk}')")

    cage_count = _count_csv(rfq.get("approved_source_cages"))
    if cage_count == 0:
        s += 3; notes.append("+3  no approved-source restriction (open market)")
    elif cage_count == 1:
        s -= 6; notes.append("-6  single approved CAGE (sole-source)")
    elif cage_count == 2:
        s += 0; notes.append("+0  only 2 approved CAGEs")
    else:
        s += 2; notes.append(f"+2  {cage_count} approved CAGEs (broad supplier base)")

    if rfq.get("manufacturer_part_numbers"):
        s += 2; notes.append("+2  manufacturer part number given (direct catalog lookup)")

    if rfq.get("technical_documents_available"):
        s -= 2; notes.append("-2  TDP required (drawing-controlled)")

    return _clamp(s, 0, 25), notes


def _score_profit_potential(
    est_capital: int | None, margin_mid: float, est_profit: float | None,
) -> tuple[int, list[str]]:
    """0-20. Blend of margin % (0-12) and absolute profit dollars (0-8).

    Margin percentage alone is misleading: 30% of a $150 order is pocket
    change. Both dimensions have to be there.
    """
    notes: list[str] = []

    # Margin % component (0-12), from the tier-based midpoint estimate.
    if margin_mid >= 25:
        m = 12; notes.append(f"+12 estimated margin ~{margin_mid:.0f}% (excellent)")
    elif margin_mid >= 20:
        m = 10; notes.append(f"+10 estimated margin ~{margin_mid:.0f}% (strong)")
    elif margin_mid >= 15:
        m = 8;  notes.append(f"+8  estimated margin ~{margin_mid:.0f}% (healthy)")
    elif margin_mid >= 10:
        m = 5;  notes.append(f"+5  estimated margin ~{margin_mid:.0f}% (thin)")
    else:
        m = 2;  notes.append(f"+2  estimated margin ~{margin_mid:.0f}% (commodity-thin)")

    # Absolute-profit component (0-8).
    if est_profit is None:
        p = 4; notes.append("+4  expected profit unknown (no quantity); assumed average")
    elif est_profit >= 2_000:
        p = 8; notes.append(f"+8  expected profit {_money(est_profit)} (well worth the effort)")
    elif est_profit >= 1_000:
        p = 7; notes.append(f"+7  expected profit {_money(est_profit)}")
    elif est_profit >= 500:
        p = 6; notes.append(f"+6  expected profit {_money(est_profit)}")
    elif est_profit >= 250:
        p = 4; notes.append(f"+4  expected profit {_money(est_profit)} (modest)")
    elif est_profit >= MIN_WORTHWHILE_PROFIT_USD:
        p = 2; notes.append(f"+2  expected profit {_money(est_profit)} (barely worth it)")
    else:
        p = 0; notes.append(f"+0  expected profit {_money(est_profit)} (< ${MIN_WORTHWHILE_PROFIT_USD}; not worth the paperwork)")

    return _clamp(m + p, 0, 20), notes


def _score_competition(
    rfq: dict[str, Any], fsc: FscInfo, amsc: AmscInfo, item_lower: str,
) -> tuple[int, list[str]]:
    """0-12. Higher = LESS competition expected."""
    s = 6
    notes: list[str] = ["+6  baseline (moderate competition)"]

    if fsc.tier in ("A", "B"):
        s += 2; notes.append(f"+2  FSC {fsc.code} narrows the field to specialty distributors")
    elif fsc.tier == "C":
        s -= 3; notes.append(f"-3  FSC {fsc.code} is commodity; many bidders likely")
    elif fsc.tier == "D":
        s -= 1; notes.append(f"-1  FSC {fsc.code}: specialists dominate")

    if amsc.bucket == "commercial":
        s -= 1; notes.append("-1  COTS item; anyone can bid, expect a crowd")
    elif amsc.bucket in ("qualified", "restricted"):
        s -= 2; notes.append(f"-2  AMSC {amsc.code}: incumbents / approved sources dominate")

    ck = has_commodity_keyword(item_lower)
    if ck:
        s -= 2; notes.append(f"-2  generic commodity item ('{ck}'); large bidder pool")

    qty = rfq.get("quantity")
    if isinstance(qty, int):
        if qty <= 25:
            s += 1; notes.append(f"+1  qty {qty} is tiny; big distributors won't bother")
        elif qty <= 500:
            s += 2; notes.append(f"+2  qty {qty} is in the small-distributor sweet spot")
        elif qty <= 2_500:
            s += 0; notes.append(f"+0  qty {qty} is medium")
        else:
            s -= 2; notes.append(f"-2  qty {qty} is large; attracts big players")

    set_aside = (rfq.get("set_aside") or "").lower()
    if "sole source" in set_aside or "sole-source" in set_aside:
        s -= 8; notes.append("-8  sole-source set-aside (likely can't bid)")
    elif "set-aside" in set_aside or "set aside" in set_aside:
        s += 2; notes.append(f"+2  set-aside narrows the field ({rfq.get('set_aside')})")

    return _clamp(s, 0, 12), notes


def _score_time_to_quote(quote_hours: float) -> tuple[int, list[str]]:
    """0-10. Reward fast-quote opportunities.

    For a one-person shop, quoting throughput trades directly against deal
    count: a 15-minute quote at 15% margin compounds faster than a 6-hour
    quote at 25%.
    """
    if quote_hours <= QUOTE_FAST_HOURS:
        return 10, [f"+10 ~{int(quote_hours * 60)} min to quote (excellent throughput)"]
    if quote_hours <= QUOTE_GOOD_HOURS:
        return 8, [f"+8  ~{quote_hours:.1f}h to quote (fast)"]
    if quote_hours <= QUOTE_OK_HOURS:
        return 5, [f"+5  ~{quote_hours:.1f}h to quote (acceptable)"]
    if quote_hours <= QUOTE_SLOW_HOURS:
        return 2, [f"+2  ~{quote_hours:.1f}h to quote (slow; competes with other work)"]
    return 1, [f"+1  ~{quote_hours:.1f}h to quote (very slow; rarely worth it)"]


def _score_technical_risk(
    rfq: dict[str, Any],
    fsc: FscInfo,
    amsc: AmscInfo,
    item_lower: str,
    text_blob: str,
) -> tuple[int, list[str]]:
    """0-15. Higher = LESS technical risk."""
    s = 15
    notes: list[str] = ["+15 baseline (assume no testing / FAT / hazmat)"]

    if fsc.tier == "D":
        s -= 8; notes.append(f"-8  high-risk FSC {fsc.code} ({fsc.label})")
    if amsc.bucket == "qualified":
        s -= 4; notes.append(f"-4  AMSC {amsc.code}: {amsc.label}")
    elif amsc.bucket == "restricted":
        s -= 5; notes.append(f"-5  AMSC {amsc.code}: {amsc.label}")
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


def _score_capital_efficiency(estimated_capital_usd: int | None) -> tuple[int, list[str]]:
    """0-10. Fit for a beginner's working capital ($500-$5K sweet spot)."""
    if estimated_capital_usd is None:
        return 5, ["+5  capital estimate unavailable; assumed average"]
    c = estimated_capital_usd
    if c < 100:
        return 3,  [f"+3  {_money(c)} -- too small to be worth the overhead"]
    if c < CAPITAL_SWEET_LOW:
        return 7,  [f"+7  {_money(c)} -- small; fine as a low-risk first bid"]
    if c <= CAPITAL_SWEET_HIGH:
        return 10, [f"+10 {_money(c)} -- beginner sweet spot (${CAPITAL_SWEET_LOW:,}-${CAPITAL_SWEET_HIGH:,})"]
    if c <= CAPITAL_GOOD_HIGH:
        return 8,  [f"+8  {_money(c)} -- comfortable for a ${WORKING_CAPITAL_USD:,} budget"]
    if c <= CAPITAL_MODERATE_HIGH:
        return 5,  [f"+5  {_money(c)} -- moderate capital lockup"]
    if c <= CAPITAL_HEAVY_HIGH:
        return 3,  [f"+3  {_money(c)} -- heavy lockup; ties up most working capital"]
    if c <= WORKING_CAPITAL_USD:
        return 1,  [f"+1  {_money(c)} -- consumes nearly all working capital"]
    return 0, [f"+0  {_money(c)} -- exceeds ${WORKING_CAPITAL_USD:,} budget; needs financing"]


def _score_response_window(days_to_close: int | None) -> tuple[int, list[str]]:
    """0-8. Days left to research suppliers and submit a quote.

    Sweet spot 7-35 days: enough runway to find a supplier and price it
    without rushing, close enough that pricing won't go stale.
    """
    if days_to_close is None:
        return 4, ["+4  close date unknown (assumed average)"]
    d = days_to_close
    if d < 0:
        return 0, [f"+0  closed {-d}d ago -- can't bid"]
    if d <= 2:
        return 1, [f"+1  only {d}d to close (very rushed)"]
    if d < WINDOW_SWEET_LOW:
        return 4, [f"+4  {d}d to close (tight but workable)"]
    if d <= WINDOW_SWEET_HIGH:
        return 8, [f"+8  {d}d to close (comfortable research window)"]
    if d <= 60:
        return 6, [f"+6  {d}d to close (plenty of time; check back closer to close)"]
    return 5, [f"+5  {d}d to close (very far out)"]


# ---------------------------------------------------------------------------
# Derived estimates
# ---------------------------------------------------------------------------

def _estimate_capital(qty: Any, unit_price: float) -> int | None:
    if qty is None or not isinstance(qty, int) or qty <= 0:
        return None
    return int(round(qty * unit_price))


def _estimate_margin_range(fsc: FscInfo, amsc: AmscInfo, has_tdp: bool) -> tuple[float, float]:
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
    # Wide-open commercial items get bid down; restricted items price high
    # (if you can source them at all).
    if amsc.bucket == "commercial":
        lo -= 2.0; hi -= 2.0
    elif amsc.bucket in ("qualified", "restricted"):
        lo += 4.0; hi += 6.0
    if has_tdp:
        lo += 3.0; hi += 3.0
    return round(max(2.0, lo), 1), round(max(4.0, hi), 1)


def _estimate_quote_hours(
    rfq: dict[str, Any],
    fsc: FscInfo,
    amsc: AmscInfo,
    item_lower: str,
) -> float:
    """Rough estimate of supplier-research + quote-build hours."""
    # Base from FSC tier (Tier A/C items are usually catalog look-ups).
    if fsc.tier in ("A", "C"):
        base = 0.5
    elif fsc.tier == "B":
        base = 1.25
    elif fsc.tier == "D":
        base = 4.0
    else:
        base = 1.5

    # AMSC adjustments: COTS quotes are quick; restricted means OEM-chasing.
    if amsc.bucket == "commercial":
        base *= 0.75
    elif amsc.bucket == "qualified":
        base += 1.5
    elif amsc.bucket == "restricted":
        base += 2.0

    cage_count = _count_csv(rfq.get("approved_source_cages"))
    if cage_count == 1:
        base += 2.0  # sole-source means hunting down the OEM
    elif cage_count == 2:
        base += 0.5
    elif cage_count >= 5:
        base -= 0.25

    if rfq.get("manufacturer_part_numbers"):
        base = max(0.25, base - 0.25)  # MPN given -> direct lookup

    if rfq.get("technical_documents_available"):
        base += 1.5  # pulling drawings, reading specs, etc.

    if has_risky_keyword(item_lower):
        base += 1.5  # extra research / cert chasing

    if has_commodity_keyword(item_lower):
        base = max(0.25, base - 0.25)  # catalog lookup

    return round(max(0.25, base), 2)


def _estimate_profit(est_capital_usd: int | None, margin_mid_pct: float) -> float | None:
    if est_capital_usd is None:
        return None
    return round(est_capital_usd * (margin_mid_pct / 100.0), 2)


def _estimate_profit_per_hour(est_profit: float | None, quote_hours: float) -> float | None:
    """Estimated dollar-profit per hour of procurement effort."""
    if est_profit is None or quote_hours <= 0:
        return None
    return round(est_profit / quote_hours, 2)


def _estimate_win_probability(
    competition_score: int, sourceability_score: int, amsc: AmscInfo, cage_count: int,
) -> float:
    """Rough 0..1 estimate. Higher competition_score = less competition."""
    p = 0.06
    p += (competition_score / 12.0) * 0.25    # up to +0.25
    p += (sourceability_score / 25.0) * 0.20  # up to +0.20

    amsc_factor = {
        "commercial": 1.15, "open": 1.10, "moderate": 0.90,
        "qualified": 0.45, "restricted": 0.25,
    }.get(amsc.bucket, 0.90)
    p *= amsc_factor

    if cage_count == 1:
        p *= 0.30
    elif cage_count == 2:
        p *= 0.60
    return round(min(0.60, max(0.02, p)), 2)


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
    if competition_score >= 9:
        return "Low"
    if competition_score >= 5:
        return "Medium"
    return "High"


# ---------------------------------------------------------------------------
# Score ceilings & perfect-score gating
# ---------------------------------------------------------------------------

def _compute_ceiling(
    rfq: dict[str, Any],
    fsc: FscInfo,
    amsc: AmscInfo,
    item_lower: str,
    est_capital: int | None,
    est_profit: float | None,
    days_to_close: int | None,
) -> tuple[int, list[str]]:
    """Return (ceiling, reasons). Score is clamped to the LOWEST ceiling hit."""
    reasons: list[str] = []
    ceiling = CEILING_NORMAL

    if days_to_close is not None and days_to_close < 0:
        ceiling = min(ceiling, CEILING_CLOSED)
        reasons.append(f"closed {-days_to_close}d ago")

    custom_kw = is_likely_custom_or_engineered(item_lower, rfq.get("raw_text"))
    if custom_kw:
        ceiling = min(ceiling, CEILING_CUSTOM)
        reasons.append(f"custom/engineered keyword: '{custom_kw}'")

    if amsc.bucket == "restricted":
        ceiling = min(ceiling, CEILING_AMSC_RESTRICTED)
        reasons.append(f"AMSC {amsc.code}: approved-source-only ({amsc.label})")
    elif amsc.bucket == "qualified":
        ceiling = min(ceiling, CEILING_AMSC_QUALIFIED)
        reasons.append(f"AMSC {amsc.code}: qualification barrier ({amsc.label})")

    if fsc.tier == "D":
        ceiling = min(ceiling, CEILING_TIER_D)
        reasons.append(f"Tier-D FSC {fsc.code} ({fsc.label})")

    if est_capital is not None and est_capital > WORKING_CAPITAL_USD:
        ceiling = min(ceiling, CEILING_OVER_BUDGET)
        reasons.append(f"capital {_money(est_capital)} exceeds ${WORKING_CAPITAL_USD:,}")
    elif est_capital is not None and est_capital > CAPITAL_HEAVY_HIGH:
        ceiling = min(ceiling, CEILING_VERY_BIG_CAPITAL)
        reasons.append(f"capital {_money(est_capital)} > ${CAPITAL_HEAVY_HIGH:,}")
    elif est_capital is not None and est_capital > CAPITAL_MODERATE_HIGH:
        ceiling = min(ceiling, CEILING_BIG_CAPITAL)
        reasons.append(f"capital {_money(est_capital)} > ${CAPITAL_MODERATE_HIGH:,}")

    if est_profit is not None and est_profit < MIN_WORTHWHILE_PROFIT_USD:
        ceiling = min(ceiling, CEILING_TINY_PROFIT)
        reasons.append(f"expected profit {_money(est_profit)} < ${MIN_WORTHWHILE_PROFIT_USD}")

    if has_risky_keyword(item_lower):
        ceiling = min(ceiling, CEILING_RISKY_KEYWORD)
        reasons.append("risky keyword (aerospace-critical / hazmat / etc.)")

    if rfq.get("technical_documents_available"):
        ceiling = min(ceiling, CEILING_TDP_REQUIRED)
        reasons.append("TDP required")

    cage_count = _count_csv(rfq.get("approved_source_cages"))
    if cage_count == 1:
        ceiling = min(ceiling, CEILING_SOLE_SOURCE_CAGE)
        reasons.append("single approved CAGE (sole-source)")

    set_aside = (rfq.get("set_aside") or "").lower()
    if "sole source" in set_aside or "sole-source" in set_aside:
        ceiling = min(ceiling, CEILING_SOLE_SOURCE_SA)
        reasons.append("sole-source set-aside")

    return ceiling, reasons


def _meets_perfect_conditions(
    sub: dict[str, int],
    amsc: AmscInfo,
    est_capital: int | None,
    est_profit: float | None,
    days_to_close: int | None,
    margin_high: float,
    quote_hours: float,
    cage_count: int,
    item_lower: str,
    fsc: FscInfo,
    rfq: dict[str, Any],
) -> tuple[bool, list[str]]:
    """Check the perfect-score (>=95) conditions. Returns (ok, missing).

    A 95+ is the "beginner dream contract": buy-it-anywhere AMSC, healthy
    margin AND real dollars, low competition, pocket-money capital, a
    comfortable window, no compliance strings, and a quick quote.
    """
    missing: list[str] = []

    if amsc.bucket not in ("commercial", "open"):
        missing.append(f"AMSC '{amsc.code or '?'}' is not Z/G (open sourcing)")
    if cage_count == 1:
        missing.append("only 1 approved CAGE (need multi-supplier)")
    if margin_high < 20:
        missing.append(f"estimated margin top {margin_high}% < 20%")
    if est_profit is None or est_profit < 500:
        missing.append("expected profit < $500")
    if sub["competition"] < 9:
        missing.append("competition subscore < 9/12 (bidder pool not narrow enough)")
    if est_capital is None or est_capital > CAPITAL_GOOD_HIGH:
        missing.append(f"capital required > ${CAPITAL_GOOD_HIGH:,}")
    if days_to_close is None or days_to_close < WINDOW_SWEET_LOW or days_to_close > 45:
        missing.append(f"response window outside {WINDOW_SWEET_LOW}-45d")
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

def _recommend_action(
    total: int, ceiling: int, custom_kw: str | None, amsc: AmscInfo,
) -> tuple[str, str]:
    """Map the final score + red flags to BID / INVESTIGATE / AVOID."""
    if custom_kw:
        return "AVOID", f"Custom-manufactured / engineering required ('{custom_kw}'); outside small-distributor scope."
    if ceiling <= CEILING_CLOSED:
        return "AVOID", "Solicitation has already closed."
    if amsc.bucket == "restricted":
        return "AVOID", (
            f"AMSC {amsc.code} ({amsc.label}): only approved sources can realistically "
            "win this. Not a beginner opportunity."
        )
    if total <= AVOID_MAX_SCORE:
        return "AVOID", "Low overall score across multiple factors."
    if total >= BID_MIN_SCORE and ceiling >= 90:
        return "BID IMMEDIATELY", "Beginner-friendly: open sourcing, healthy profit, manageable capital, no red flags."
    if total >= BID_MIN_SCORE:
        return "INVESTIGATE SUPPLIER FIRST", "High score but a red flag is in play; verify before quoting."
    return "INVESTIGATE SUPPLIER FIRST", "Promising -- verify supplier and pricing before quoting."


# ---------------------------------------------------------------------------
# Main scoring function
# ---------------------------------------------------------------------------

def score_rfq(rfq: dict[str, Any], today: date | None = None) -> dict[str, Any]:
    """Score a single RFQ dict. Returns a payload ready for ``save_score``."""
    today = today or date.today()

    fsc = classify_fsc(rfq.get("fsc"))
    amsc = classify_amsc(rfq.get("amsc"))
    item_lower = (rfq.get("item_name") or "").lower()
    text_blob = item_lower + " " + (rfq.get("raw_text") or "").lower()[:5000]
    days_to_close = _days_to_close(rfq, today)

    # --------------------- Derived estimates ---------------------
    unit_price = estimate_unit_price(rfq.get("fsc"), rfq.get("item_name"))
    est_capital = _estimate_capital(rfq.get("quantity"), unit_price)
    margin_lo, margin_hi = _estimate_margin_range(
        fsc, amsc, bool(rfq.get("technical_documents_available"))
    )
    margin_mid = (margin_lo + margin_hi) / 2.0
    quote_hours = _estimate_quote_hours(rfq, fsc, amsc, item_lower)
    est_profit = _estimate_profit(est_capital, margin_mid)
    profit_per_hour = _estimate_profit_per_hour(est_profit, quote_hours)

    # --------------------- 7 subscores ---------------------
    src, src_notes      = _score_sourceability(rfq, fsc, amsc, item_lower)
    prof, prof_notes    = _score_profit_potential(est_capital, margin_mid, est_profit)
    comp, comp_notes    = _score_competition(rfq, fsc, amsc, item_lower)
    ttq, ttq_notes      = _score_time_to_quote(quote_hours)
    risk, risk_notes    = _score_technical_risk(rfq, fsc, amsc, item_lower, text_blob)
    cap, cap_notes      = _score_capital_efficiency(est_capital)
    window, window_notes = _score_response_window(days_to_close)

    raw_total = src + prof + comp + ttq + risk + cap + window

    # --------------------- Apply ceilings ---------------------
    ceiling, ceiling_reasons = _compute_ceiling(
        rfq, fsc, amsc, item_lower, est_capital, est_profit, days_to_close,
    )
    capped_total = min(raw_total, ceiling)

    # --------------------- Perfect-score gating ---------------------
    sub = {
        "sourceability": src, "competition": comp, "profit_potential": prof,
        "time_to_quote": ttq, "capital_efficiency": cap,
        "technical_risk": risk, "delivery": window,
    }
    cage_count = _count_csv(rfq.get("approved_source_cages"))
    perfect_ok, perfect_missing = _meets_perfect_conditions(
        sub, amsc, est_capital, est_profit, days_to_close, margin_hi,
        quote_hours, cage_count, item_lower, fsc, rfq,
    )
    if not perfect_ok and capped_total >= PERFECT_SCORE_MIN_OVERALL:
        capped_total = PERFECT_SCORE_MIN_OVERALL - 1

    total = _clamp(capped_total, 0, 100)

    # --------------------- Action recommendation ---------------------
    custom_kw = is_likely_custom_or_engineered(item_lower, rfq.get("raw_text"))
    action, action_reason = _recommend_action(total, ceiling, custom_kw, amsc)

    win_prob = _estimate_win_probability(comp, src, amsc, cage_count)
    profit_bucket = _profit_bucket(prof, margin_hi)
    competition_bucket = _competition_bucket(comp)

    # --------------------- Notes (the "detailed explanation") ---------------------
    lines: list[str] = []
    lines.append(f"=== OVERALL: {total}/100  --  {action} ===")
    lines.append("")
    if amsc.code:
        lines.append(f"AMSC {amsc.code}: {amsc.label}")
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
        (f"SOURCEABILITY: {src}/25",      src_notes),
        (f"PROFIT POTENTIAL: {prof}/20  ({profit_bucket})", prof_notes),
        (f"TECHNICAL RISK: {risk}/15  (higher = less risk)", risk_notes),
        (f"COMPETITION:   {comp}/12  ({competition_bucket} competition expected)", comp_notes),
        (f"TIME-TO-QUOTE: {ttq}/10  (~{quote_hours}h estimated)", ttq_notes),
        (f"CAPITAL EFFICIENCY: {cap}/10", cap_notes),
        (f"RESPONSE WINDOW: {window}/8",  window_notes),
    ]
    for header, ns in sections:
        lines.append(header)
        for n in ns:
            lines.append(f"  {n}")
        lines.append("")

    lines.append("ESTIMATES")
    lines.append(f"  Capital required: {_money(est_capital)}")
    lines.append(f"  Margin range:     {margin_lo}% - {margin_hi}%")
    lines.append(f"  Expected profit:  {_money(est_profit)}")
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
    legacy_margin = int(round(prof / 20.0 * 100))            # 0..20 -> 0..100
    legacy_competition = int(round((12 - comp) / 12.0 * 100))
    legacy_sourcing_diff = int(round((25 - src) / 25.0 * 100))
    legacy_urgency = int(round((8 - window) / 8.0 * 100))

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
        "delivery": window,  # response-window score (column kept for compat)
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
