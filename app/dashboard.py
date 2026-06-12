"""Streamlit dashboard for dibbs-bot.

Launch with:
    streamlit run app/dashboard.py

The dashboard reads from the SQLite DB created by ``db/database.py`` and
scored by ``analysis/score_opportunities.py``. It does not write anything,
so you can rebuild data behind it without losing UI state.
"""

from __future__ import annotations

import sys
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any

# Allow `streamlit run app/dashboard.py` from the project root.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from analysis.nsn_tools import PREFERRED_FSCS, RISKY_FSCS, classify_fsc  # noqa: E402
from analysis.score_opportunities import score_all  # noqa: E402
from db.database import count_rfqs, fetch_all_rfqs_with_scores, init_db  # noqa: E402
from scraper.fetch_rfqs import DibbsSession, fetch_recent_indexes  # noqa: E402
from scraper.parse_rfqs import ingest_directory  # noqa: E402
from utils.config import SETTINGS  # noqa: E402
from utils.logging_config import configure_logging  # noqa: E402

# Make sure the same logging config the CLI uses is active here too -- so the
# log file captures everything triggered from the dashboard.
configure_logging()

st.set_page_config(
    page_title="dibbs-bot",
    page_icon=None,
    layout="wide",
)


# ---------------------------------------------------------------------------
# Optional shared-password gate
#
# If you deploy this dashboard somewhere public (e.g. Streamlit Community
# Cloud), set a password by adding this to your app's Secrets:
#
#     dibbs_password = "your-shared-password"
#
# Locally, with no secret configured, the gate is a no-op so `streamlit run`
# Just Works(tm) without any setup.
# ---------------------------------------------------------------------------

def _required_password() -> str | None:
    """Return the configured shared password, or None if none is set.

    We swallow every exception that ``st.secrets`` can raise so that running
    the dashboard with no ``secrets.toml`` simply disables the gate.
    """
    try:
        pw = st.secrets.get("dibbs_password", "")
    except Exception:  # noqa: BLE001
        return None
    pw = (pw or "").strip()
    return pw or None


def require_login() -> None:
    """Block the page behind a password if one is configured.

    Call this once at the top of ``main()``. If the user isn't authenticated
    yet, we render a tiny login form and call ``st.stop()`` so nothing else
    on the page is rendered (and crucially, no scrape can be triggered).
    """
    expected = _required_password()
    if expected is None:
        return  # No password configured -- local dev mode.

    if st.session_state.get("authenticated"):
        return

    st.title("dibbs-bot")
    st.caption("Enter the shared password to continue.")
    with st.form("login", clear_on_submit=True):
        pw = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Sign in", type="primary")
    if submitted:
        if pw == expected:
            st.session_state["authenticated"] = True
            st.rerun()
        else:
            st.error("Wrong password.")
    st.stop()


def render_logout() -> None:
    """Tiny logout button in the sidebar, only shown when auth is on."""
    if _required_password() is None:
        return
    if st.sidebar.button("Sign out", type="secondary"):
        st.session_state.pop("authenticated", None)
        st.rerun()


# ---------------------------------------------------------------------------
# Concurrent-scrape lock
#
# Streamlit handles each user session in its own thread, but they share one
# Python process. Without a lock, two friends who click "Pull & Score" at the
# same time would open two concurrent DIBBS sessions from the same IP --
# rude to DLA and a fast way to get rate-limited.
#
# ``st.cache_resource`` is the canonical way to hold a long-lived, shared
# object (here: a threading.Lock plus some metadata about who's running).
# ---------------------------------------------------------------------------

@st.cache_resource
def _scrape_state() -> dict[str, Any]:
    return {
        "lock": threading.Lock(),
        "in_progress": False,
        "started_at": None,
        "started_by_session": None,
    }


# ---------------------------------------------------------------------------
# One-time DB bootstrap
#
# When the app boots on a fresh container (e.g. Streamlit Community Cloud
# after a cold start, or any new clone of the repo), there's no SQLite file
# yet and no `rfqs` table -- so the first count_rfqs() call would crash with
# "no such table: rfqs". Running init_db() once on startup creates the
# schema if it's missing. The SQL inside is all CREATE TABLE IF NOT EXISTS,
# so it's a no-op when the DB already exists.
#
# cache_resource ensures this runs exactly once per Python process, not on
# every script rerun.
# ---------------------------------------------------------------------------

@st.cache_resource
def _ensure_db_initialized() -> bool:
    init_db()
    return True


# ---------------------------------------------------------------------------
# Refresh-from-DIBBS orchestrator (button-driven; no terminal needed)
# ---------------------------------------------------------------------------

def run_refresh(
    days_back: int,
    *,
    skip_fetch: bool = False,
    status,  # st.status() container; we write progress into it
) -> dict[str, int]:
    """Pull, ingest, and score in one shot.

    ``skip_fetch=True`` reuses whatever's already in the inbox (useful for a
    fast re-score after tweaking the scoring rules).

    Returns a small summary dict the caller can use for the success banner.
    """
    summary = {"files": 0, "ingested": 0, "scored": 0}

    if not skip_fetch:
        status.write(f"Connecting to DIBBS and pulling the last {days_back} day(s)...")
        session = DibbsSession()
        saved = fetch_recent_indexes(days_back, session=session)
        summary["files"] = len(saved)
        if not saved:
            status.write(
                "No new files were downloaded "
                "(weekend/holiday gap or DIBBS hasn't posted yet?)."
            )
        else:
            status.write(f"Downloaded {len(saved)} daily index file(s).")

    status.write("Parsing the inbox and upserting into the database...")
    summary["ingested"] = ingest_directory()
    status.write(f"Ingested / updated {summary['ingested']:,} RFQ row(s).")

    status.write("Scoring every RFQ...")
    summary["scored"] = score_all()
    status.write(f"Scored {summary['scored']:,} RFQ(s).")

    return summary


def _format_relative(ts: datetime | None) -> str:
    """Human-readable 'N min ago' for the last-refresh stamp."""
    if ts is None:
        return "never"
    delta = datetime.now() - ts
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return ts.strftime("%Y-%m-%d %H:%M")


def render_refresh_panel() -> None:
    """Sidebar block: 'Refresh from DIBBS' button + options.

    Rendered before ``load_data()`` so a successful refresh invalidates the
    cache in time for the rest of the page to pick up the new data.
    """
    st.sidebar.header("Refresh from DIBBS")

    days_back = st.sidebar.number_input(
        "Days back",
        min_value=1,
        max_value=30,
        value=int(st.session_state.get("last_days_back", 7)),
        help="Pull the last N daily index files from DIBBS. "
             "Each file covers one day of RFQs (~2,500 typical).",
    )
    st.session_state["last_days_back"] = days_back

    # If someone else is already running a refresh, disable the buttons and
    # show what's happening. We re-check the flag on every Streamlit rerun.
    state = _scrape_state()
    busy = state["in_progress"]

    col1, col2 = st.sidebar.columns(2)
    pull_clicked = col1.button(
        "Pull & Score", type="primary", width="stretch", disabled=busy
    )
    rescore_clicked = col2.button("Re-score", width="stretch", disabled=busy)

    if busy:
        started = state["started_at"]
        when = started.strftime("%H:%M:%S") if started else "just now"
        st.sidebar.info(
            f"Another session is already refreshing (started {when}). "
            "The buttons unlock automatically when it finishes."
        )

    if pull_clicked or rescore_clicked:
        # Try to grab the lock without blocking. If someone beat us to it
        # between the disabled-check above and now, bail out gracefully.
        acquired = state["lock"].acquire(blocking=False)
        if not acquired:
            st.sidebar.warning(
                "Another refresh just started in a different session. "
                "Please wait for it to finish."
            )
        else:
            state["in_progress"] = True
            state["started_at"] = datetime.now()
            try:
                action = "Pulling from DIBBS & scoring" if pull_clicked else "Re-scoring"
                with st.status(f"{action}...", expanded=True) as s:
                    try:
                        summary = run_refresh(
                            int(days_back),
                            skip_fetch=rescore_clicked,
                            status=s,
                        )
                        msg = (
                            f"Done. "
                            f"{summary['files']} file(s), "
                            f"{summary['ingested']:,} ingested, "
                            f"{summary['scored']:,} scored."
                        )
                        s.update(label=msg, state="complete")
                        st.session_state["last_refresh"] = datetime.now()
                        st.session_state["last_summary"] = summary
                        # Drop the cached query so the rest of the page sees new data.
                        load_data.clear()
                    except Exception as exc:  # noqa: BLE001
                        s.update(label=f"Refresh failed: {exc}", state="error")
                        st.exception(exc)
            finally:
                state["in_progress"] = False
                state["started_at"] = None
                state["lock"].release()

    # Status line at the bottom of the panel.
    last = st.session_state.get("last_refresh")
    rows_now = count_rfqs()
    st.sidebar.caption(
        f"DB has **{rows_now:,}** RFQ(s). Last refresh: **{_format_relative(last)}**."
    )
    st.sidebar.divider()


# ---------------------------------------------------------------------------
# Data loading (cached so filters stay snappy)
# ---------------------------------------------------------------------------

@st.cache_data(ttl=60)
def load_data() -> pd.DataFrame:
    """Read every RFQ + latest score into a DataFrame."""
    rows = fetch_all_rfqs_with_scores()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    # Type fixups for nicer filtering.
    for col in ("issue_date", "close_date"):
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce").dt.date
    if "technical_documents_available" in df.columns:
        df["technical_documents_available"] = df["technical_documents_available"].astype(bool)
    # Scores may be None if scoring hasn't been run yet.
    if "score" in df.columns:
        df["score"] = pd.to_numeric(df["score"], errors="coerce")
    return df


def _fsc_category(fsc: Any) -> str:
    info = classify_fsc(str(fsc) if fsc else None)
    return info.category


def _days_until_close(d: Any) -> int | None:
    if pd.isna(d) or d is None:
        return None
    if isinstance(d, datetime):
        d = d.date()
    return (d - date.today()).days


# ---------------------------------------------------------------------------
# Reusable rendering helpers
# ---------------------------------------------------------------------------

TABLE_COLUMNS = [
    "score",
    "recommended_action",
    "solicitation_number",
    "nsn",
    "fsc",
    "item_name",
    "quantity",
    "unit_of_issue",
    "estimated_capital_usd",
    "close_date",
    "set_aside",
    "approved_source_cages",
    "technical_documents_available",
]


def render_table(df: pd.DataFrame, *, key: str | None = None) -> None:
    """Render the RFQ table with a consistent column order."""
    if df.empty:
        st.info("No RFQs match the current filters.")
        return
    cols = [c for c in TABLE_COLUMNS if c in df.columns]
    view = df[cols].rename(
        columns={
            "score": "Score",
            "recommended_action": "Action",
            "solicitation_number": "Solicitation",
            "nsn": "NSN",
            "fsc": "FSC",
            "item_name": "Item",
            "quantity": "Qty",
            "unit_of_issue": "UoI",
            "estimated_capital_usd": "Est. Capital",
            "close_date": "Closes",
            "set_aside": "Set-Aside",
            "approved_source_cages": "Approved CAGEs",
            "technical_documents_available": "TDP?",
        }
    )
    st.dataframe(view, hide_index=True, width="stretch", key=key)


# Color hints for the recommended-action banner.
_ACTION_COLORS: dict[str, str] = {
    "BID IMMEDIATELY":            "#16a34a",  # green
    "INVESTIGATE SUPPLIER FIRST": "#d97706",  # amber
    "AVOID":                      "#dc2626",  # red
}


def _isna(v: Any) -> bool:
    """Robust missing-value check that handles None, pd.NA, np.nan, and strs.

    The pandas family of NaN markers (np.nan, pd.NA, pd.NaT) doesn't compare
    equal to anything, so ``v is None`` alone isn't enough once a value has
    round-tripped through a DataFrame. We feed it through ``pd.isna`` for
    everything that isn't already a string.
    """
    if v is None:
        return True
    if isinstance(v, str):
        return False
    try:
        return bool(pd.isna(v))
    except (TypeError, ValueError):
        return False


def _score_cell(v: Any, denom: int) -> str:
    """Render a subscore as '<n> / <denom>'. Uses an em-dash if missing.

    IMPORTANT: this is NOT ``v or '—'`` because a legitimate score of 0
    (e.g. ``delivery=0`` for an already-closed RFQ) is falsy in Python and
    would silently render as missing.
    """
    if _isna(v):
        return f"— / {denom}"
    return f"{int(v)} / {denom}"


def _money(v: Any) -> str:
    if _isna(v):
        return "—"
    return f"~${int(v):,}"


def _pct(v: Any) -> str:
    if _isna(v):
        return "—"
    return f"{int(round(float(v) * 100))}%"


def _margin_range(row: dict[str, Any]) -> str:
    lo = row.get("estimated_margin_low")
    hi = row.get("estimated_margin_high")
    if _isna(lo) or _isna(hi):
        return "—"
    return f"{lo:g}% – {hi:g}%"


def render_detail(row: dict[str, Any]) -> None:
    """Detail panel for a single RFQ row."""
    st.markdown(f"### {row.get('solicitation_number', '(no number)')}")
    st.caption(row.get("item_name") or "")

    # Big colored recommended-action banner up top.
    action = row.get("recommended_action")
    if action and not _isna(action):
        color = _ACTION_COLORS.get(action, "#6b7280")
        st.markdown(
            f"<div style='background:{color};color:white;padding:0.6rem 1rem;"
            f"border-radius:0.4rem;font-weight:600;font-size:1rem;'>"
            f"{action}</div>",
            unsafe_allow_html=True,
        )
    else:
        # Likely an RFQ that hasn't been scored under the v3 framework yet.
        st.warning(
            "This RFQ doesn't have a recommended action yet — it was scored "
            "under an older framework. Click **Re-score** in the sidebar to "
            "apply the new 7-subscore framework (now including Time-to-Quote "
            "and Profit/Hour) to every row in the DB."
        )

    # Headline metrics: overall score + key estimates.
    score_val = row.get("score")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Overall Score", "—" if _isna(score_val) else int(score_val))
    c2.metric("Capital Required", _money(row.get("estimated_capital_usd")))
    c3.metric("Margin Range", _margin_range(row))
    c4.metric("Win Probability", _pct(row.get("estimated_win_probability")))

    # Time + profit-per-hour row -- key for a one-person operation.
    t1, t2 = st.columns(2)
    qh = row.get("estimated_quote_hours")
    t1.metric("Est. quote time", "—" if _isna(qh) else f"~{float(qh):g}h")
    pph = row.get("estimated_profit_per_hour")
    t2.metric("Profit / hour", "—" if _isna(pph) else f"~${float(pph):,.0f}/h")

    # Seven-subscore breakdown.
    st.markdown("**Subscores**")
    s1, s2, s3, s4 = st.columns(4)
    s1.metric("Sourceability",    _score_cell(row.get("sourceability"),      18))
    s2.metric("Competition",      _score_cell(row.get("competition"),        15))
    s3.metric("Profit",           _score_cell(row.get("profit_potential"),   15))
    s4.metric("Time-to-Quote",    _score_cell(row.get("time_to_quote"),      15))
    s5, s6, s7, _ = st.columns(4)
    s5.metric("Tech Risk (inv.)", _score_cell(row.get("technical_risk"),     15))
    s6.metric("Capital",          _score_cell(row.get("capital_efficiency"), 12))
    s7.metric("Delivery",         _score_cell(row.get("delivery"),           10))

    def _show(v: Any) -> str:
        """Coerce any cell value to a string so Arrow doesn't choke on the
        mixed-type 'Value' column (dates, ints, None, strs all live here)."""
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return "—"
        return str(v)

    info_rows = {
        "NSN": _show(row.get("nsn")),
        "FSC": _show(row.get("fsc")),
        "Quantity": f"{_show(row.get('quantity'))} {row.get('unit_of_issue') or ''}".strip(),
        "Close date": _show(row.get("close_date")),
        "Set-aside": _show(row.get("set_aside")),
        "Buyer": _show(row.get("buyer")),
        "PR #": _show(row.get("purchase_request_number")),
        "Approved CAGEs": _show(row.get("approved_source_cages")),
        "Manufacturer P/Ns": _show(row.get("manufacturer_part_numbers")),
        "TDP available": "Yes" if row.get("technical_documents_available") else "No",
    }
    st.table(pd.DataFrame(info_rows.items(), columns=["Field", "Value"]))

    if row.get("url"):
        st.link_button("Open on DIBBS", row["url"])

    notes = row.get("score_notes") or row.get("notes")
    if notes:
        with st.expander("Score explanation"):
            st.code(notes, language="text")

    if row.get("raw_text"):
        with st.expander("Raw text"):
            st.code(row["raw_text"], language="text")


# ---------------------------------------------------------------------------
# Sidebar filters (applied to every tab)
# ---------------------------------------------------------------------------

def sidebar_filters(df: pd.DataFrame) -> pd.DataFrame:
    """Render filter widgets and return a filtered DataFrame."""
    st.sidebar.header("Filters")

    if df.empty:
        return df

    # "Open only" hides RFQs whose close_date has already passed. ON by
    # default because closed solicitations aren't actionable -- they're only
    # kept for history. Turn off if you want to inspect the full DB.
    open_only = st.sidebar.toggle(
        "Open only (hide closed)",
        value=True,
        help="Hide RFQs whose close date has already passed.",
    )

    # Recommended-action filter -- the most useful slice for the new scorer.
    if "recommended_action" in df.columns and df["recommended_action"].notna().any():
        action_options = sorted(df["recommended_action"].dropna().unique().tolist())
        action_pick = st.sidebar.multiselect(
            "Recommended action",
            action_options,
            help="BID IMMEDIATELY = top picks; AVOID = red flags.",
        )
    else:
        action_pick = []

    fsc_options = sorted(df["fsc"].dropna().unique().tolist())
    fsc_pick = st.sidebar.multiselect("FSC", fsc_options)

    nsn_query = st.sidebar.text_input("NSN contains")

    set_aside_options = sorted(df["set_aside"].dropna().unique().tolist())
    set_aside_pick = st.sidebar.multiselect("Set-aside", set_aside_options)

    tdp_pick = st.sidebar.selectbox(
        "Technical docs available?",
        options=("Any", "Yes", "No"),
        index=0,
    )

    # Score slider only makes sense once scoring has run.
    score_min, score_max = 0, 100
    if df["score"].notna().any():
        score_min, score_max = st.sidebar.slider(
            "Score range",
            min_value=0,
            max_value=100,
            value=(0, 100),
        )

    # Close-date filter (inclusive). Default = no constraint.
    close_dates = df["close_date"].dropna()
    if not close_dates.empty:
        default_lo, default_hi = close_dates.min(), close_dates.max()
        lo, hi = st.sidebar.date_input(
            "Close date between",
            value=(default_lo, default_hi),
        )
    else:
        lo = hi = None

    filtered = df.copy()
    if open_only and "close_date" in filtered.columns:
        today_d = date.today()
        # NaT/None close dates are kept (we can't prove they're closed).
        mask = filtered["close_date"].apply(
            lambda d: True if pd.isna(d) or d is None else d >= today_d
        )
        filtered = filtered[mask]
    if action_pick:
        filtered = filtered[filtered["recommended_action"].isin(action_pick)]
    if fsc_pick:
        filtered = filtered[filtered["fsc"].isin(fsc_pick)]
    if nsn_query:
        filtered = filtered[
            filtered["nsn"].fillna("").str.contains(nsn_query, case=False)
        ]
    if set_aside_pick:
        filtered = filtered[filtered["set_aside"].isin(set_aside_pick)]
    if tdp_pick == "Yes":
        filtered = filtered[filtered["technical_documents_available"] == True]  # noqa: E712
    elif tdp_pick == "No":
        filtered = filtered[filtered["technical_documents_available"] == False]  # noqa: E712
    if filtered["score"].notna().any():
        filtered = filtered[
            (filtered["score"].fillna(-1) >= score_min)
            & (filtered["score"].fillna(101) <= score_max)
        ]
    if lo and hi:
        filtered = filtered[
            (filtered["close_date"].fillna(date(1900, 1, 1)) >= lo)
            & (filtered["close_date"].fillna(date(2999, 1, 1)) <= hi)
        ]
    return filtered


# ---------------------------------------------------------------------------
# Main app
# ---------------------------------------------------------------------------

def main() -> None:
    # Make sure the SQLite schema exists before anything else queries it.
    # This is what protects us from "no such table: rfqs" on a fresh
    # container (e.g. first boot on Streamlit Community Cloud).
    _ensure_db_initialized()

    # Auth gate is the very first thing after the DB is ready: if a password
    # is configured and the user isn't signed in, require_login() renders a
    # login form and stops the script before any data is loaded or any
    # scrape can be triggered.
    require_login()

    st.title("dibbs-bot")
    st.caption("Local-first DIBBS RFQ search and scoring")

    # Refresh panel renders FIRST so that if the user clicks "Pull & Score",
    # the cache gets invalidated before load_data() runs below.
    render_refresh_panel()
    render_logout()

    total = count_rfqs()
    if total == 0:
        st.warning(
            "No RFQs in the database yet.\n\n"
            "Click **Pull & Score** in the sidebar to download the last 7 "
            "days of RFQs from DIBBS and score them. (No terminal commands "
            "needed — everything runs from this page.)"
        )
        return

    df = load_data()
    if df.empty:
        st.error("Database had RFQ rows but the dashboard query returned nothing.")
        return

    # Derived columns used by some tabs.
    df["fsc_category"] = df["fsc"].apply(_fsc_category)
    df["days_to_close"] = df["close_date"].apply(_days_until_close)

    filtered = sidebar_filters(df)

    # Top-level KPIs.
    has_action = "recommended_action" in filtered.columns
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("RFQs in DB", len(df))
    c2.metric("After filters", len(filtered))
    scored = filtered["score"].dropna()
    c3.metric("Avg score", f"{scored.mean():.0f}" if not scored.empty else "—")
    if has_action:
        bid = int((filtered["recommended_action"] == "BID IMMEDIATELY").sum())
        investigate = int((filtered["recommended_action"] == "INVESTIGATE SUPPLIER FIRST").sum())
        c4.metric("BID IMMEDIATELY", bid)
        c5.metric("Investigate", investigate)
    else:
        c4.metric("Preferred FSC", int((filtered["fsc_category"] == "preferred").sum()))
        c5.metric("Scored", int(filtered["score"].notna().sum()))

    tabs = st.tabs(
        [
            "Top Opportunities",
            "Needs Review",
            "Avoid / High Complexity",
            "Search by NSN",
            "Search by FSC",
            "All RFQs",
        ]
    )

    # ----- Top Opportunities --------------------------------------------------
    with tabs[0]:
        st.subheader("Top opportunities — BID IMMEDIATELY")
        st.caption(
            "Recommended-action = BID IMMEDIATELY (high score, sourceable, "
            "manageable capital, no red flags). Sorted by overall score."
        )
        if has_action:
            top = filtered[filtered["recommended_action"] == "BID IMMEDIATELY"]
        else:
            top = filtered.dropna(subset=["score"])
        top = top.sort_values("score", ascending=False).head(50)
        render_table(top, key="top_table")
        if not top.empty:
            choice = st.selectbox(
                "Inspect a top opportunity",
                options=top["solicitation_number"].tolist(),
                key="top_select",
            )
            row = top[top["solicitation_number"] == choice].iloc[0].to_dict()
            render_detail(row)

    # ----- Needs Review (mid-band) -------------------------------------------
    with tabs[1]:
        st.subheader("Needs review — INVESTIGATE SUPPLIER FIRST")
        st.caption("Promising opportunities that need supplier/pricing verification before quoting.")
        if has_action:
            mid = filtered[filtered["recommended_action"] == "INVESTIGATE SUPPLIER FIRST"]
        else:
            mid = filtered[(filtered["score"] >= 50) & (filtered["score"] < 75)]
        mid = mid.sort_values("score", ascending=False)
        render_table(mid, key="mid_table")
        if not mid.empty:
            choice = st.selectbox(
                "Inspect one",
                options=mid["solicitation_number"].head(100).tolist(),
                key="mid_select",
            )
            row = mid[mid["solicitation_number"] == choice].iloc[0].to_dict()
            render_detail(row)

    # ----- Avoid --------------------------------------------------------------
    with tabs[2]:
        st.subheader("Avoid / High Complexity")
        st.caption("Sole-source, aviation-critical, hazardous, or capital > $50K.")
        if has_action:
            avoid = filtered[filtered["recommended_action"] == "AVOID"]
        else:
            avoid = filtered[
                (filtered["score"].fillna(0) < 50) | (filtered["fsc_category"] == "risky")
            ]
        render_table(avoid.sort_values("score", ascending=True).head(200), key="avoid_table")

    # ----- Search by NSN ------------------------------------------------------
    with tabs[3]:
        st.subheader("Search by NSN")
        q = st.text_input("Enter all or part of an NSN", key="nsn_search")
        if q:
            hits = filtered[filtered["nsn"].fillna("").str.contains(q, case=False)]
        else:
            hits = filtered
        render_table(hits, key="nsn_table")

    # ----- Search by FSC ------------------------------------------------------
    with tabs[4]:
        st.subheader("Search by FSC")
        fsc_choices = sorted(
            set(filtered["fsc"].dropna()) | set(PREFERRED_FSCS) | set(RISKY_FSCS)
        )
        picked = st.multiselect(
            "Pick one or more FSCs",
            options=fsc_choices,
            default=[],
            format_func=lambda c: f"{c} — {PREFERRED_FSCS.get(c) or RISKY_FSCS.get(c) or ''}".strip(" —"),
            key="fsc_multi",
        )
        if picked:
            hits = filtered[filtered["fsc"].isin(picked)]
        else:
            hits = filtered
        render_table(hits, key="fsc_table")

    # ----- All RFQs -----------------------------------------------------------
    with tabs[5]:
        st.subheader("All RFQs (filtered)")
        render_table(filtered, key="all_table")
        st.download_button(
            "Download as CSV",
            data=filtered.to_csv(index=False).encode("utf-8"),
            file_name="dibbs_filtered.csv",
            mime="text/csv",
        )

    # Footer info.
    with st.expander("Settings & environment"):
        st.write(
            {
                "db_path": str(SETTINGS.db_path),
                "inbox_dir": str(SETTINGS.inbox_dir),
                "dibbs_base_url": SETTINGS.dibbs_base_url,
                "log_level": SETTINGS.log_level,
            }
        )


main()
