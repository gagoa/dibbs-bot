"""Live DIBBS scraper.

DIBBS (https://www.dibbs.bsm.dla.mil) is an ASP.NET WebForms site sitting
behind a click-through "Logon Acknowledgement" page. Public RFQ search does
not require a login but does require accepting the agreement (which sets a
session cookie). Paginated result tables are driven by ``__VIEWSTATE`` /
``__EVENTVALIDATION`` postbacks, so a stateful session is required.

The scraper is structured so that the parts most likely to need adjusting
(URL paths, agreement-form field names, result-table column ids) are
constants near the top of the file. Everything else is generic.

Flow:
    1. Build a session, replaying any persisted cookies.
    2. GET the target page; if we land on the agreement page, POST it and
       retry.
    3. Save the raw HTML into ``SETTINGS.inbox_dir`` so ``parse_rfqs.py``
       can ingest it unchanged.
    4. For result pages, optionally follow each row's detail link.

This module is safe to import from anywhere (no side effects). To actually
hit DIBBS, run it as a CLI:

    python scraper/fetch_rfqs.py --recent
    python scraper/fetch_rfqs.py --fsc 5305,5310,5340
    python scraper/fetch_rfqs.py --solicitation SPE7L1-26-T-0001
    python scraper/fetch_rfqs.py --recent --dry-run --no-detail
"""

from __future__ import annotations

import argparse
import logging
import pickle
import re
import sys
import time
from datetime import date, datetime, timedelta
from http.cookiejar import LWPCookieJar
from pathlib import Path
from typing import Iterable

# Allow running directly: python scraper/fetch_rfqs.py
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from urllib.parse import urljoin  # noqa: E402

import requests  # noqa: E402
from bs4 import BeautifulSoup  # noqa: E402

from utils.config import SETTINGS  # noqa: E402
from utils.logging_config import configure_logging  # noqa: E402

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants likely to need adjusting once you've seen real DIBBS pages.
# Everything here is intentionally easy to override.
# ---------------------------------------------------------------------------

# Path DIBBS uses for the click-through "DoD Warning and Consent" banner.
# The real form action is dodwarning.aspx; this constant is informational --
# detection is by page content, not URL.
AGREEMENT_PATH = "/dodwarning.aspx"

# Substrings that, when present in a response body, signal "you're on the
# agreement page and need to accept before proceeding." Verified against the
# live DIBBS DoD Warning and Consent banner.
AGREEMENT_MARKERS: tuple[str, ...] = (
    "dod warning and consent",
    "dod notice and consent banner",
    "usg-authorized use only",
    "consent to the following conditions",
    "warning and consent",
    "logon acknowledgement",  # kept for older variants
    "logon acknowledgment",
)

# Possible names for the "I Agree" submit button, in priority order. Live
# DIBBS uses ``butAgree``. The other names are defensive fallbacks for older
# layouts and other DLA properties.
AGREEMENT_BUTTON_NAMES: tuple[str, ...] = (
    "butAgree",
    "ctl00$cphMainContent$btnOK",
    "ctl00$cphMainContent$btnAccept",
    "ctl00$cphMainContent$btnAgree",
    "ctl00$cphMain$btnOK",
    "btnOK",
    "btnAccept",
)

# Search / listing pages, verified against the live DIBBS navigation menu.
# DIBBS exposes three "by date" categories on a single endpoint:
#   /RFQ/RFQDates.aspx?category=issue   -- RFQs by Issue Date
#   /RFQ/RFQDates.aspx?category=close   -- RFQs by Return-By Date
#   /RFQ/RFQDates.aspx?category=recent  -- "RFQs Recent" (the firehose)
RFQ_DATES_PATH = "/RFQ/RFQDates.aspx"
RECENT_RFQS_PATH = f"{RFQ_DATES_PATH}?category=recent"

# Daily batch downloads. DIBBS posts a fixed-width text file per day on a
# CDN host: in<YYMMDD>.txt covering every RFQ issued that day.
# (See https://www.dibbs.bsm.dla.mil/Rfq/RfqFileDefs.aspx for the spec.)
DOWNLOAD_BASE_URL = "https://dibbs2.bsm.dla.mil/Downloads/RFQ/Archive"
DAILY_INDEX_URL_TEMPLATE = DOWNLOAD_BASE_URL + "/in{yymmdd}.txt"

# Database-search endpoints (also verified from the nav dropdown).
SEARCH_BY_NSN_PATH = "/RFQ/RFQNsn.aspx"     # by NSN
SEARCH_BY_SOL_PATH = "/RFQ/RFQSol.aspx"     # by solicitation number
SEARCH_BY_PR_PATH = "/RFQ/RFQPr.aspx"       # by purchase request number

# NOTE: DIBBS does not expose a public "search by FSC" endpoint. To filter by
# FSC, scrape one of the RFQDates listings and rely on local FSC filtering
# (the scoring module already weights FSC heavily). The constant below is kept
# as a defensive fallback in case a future DIBBS release adds one.
SEARCH_BY_FSC_PATH = "/RFQ/RFQFsc.aspx"     # untested -- may 404


# ASP.NET hidden field names we have to round-trip on every postback.
# DIBBS splits its (very large) VIEWSTATE across multiple fields:
# ``__VIEWSTATE``, ``__VIEWSTATE1``, ... up to ``__VIEWSTATEFIELDCOUNT - 1``.
# We harvest all of them.
_HIDDEN_FIELDS: tuple[str, ...] = (
    "__VIEWSTATE",
    "__VIEWSTATEGENERATOR",
    "__VIEWSTATEENCRYPTED",
    "__VIEWSTATEFIELDCOUNT",
    "__EVENTVALIDATION",
    "__EVENTTARGET",
    "__EVENTARGUMENT",
    "__LASTFOCUS",
    "__SCROLLPOSITIONX",
    "__SCROLLPOSITIONY",
)

# Regex for the numbered viewstate parts (``__VIEWSTATE1``, ``__VIEWSTATE2``, ...).
_VIEWSTATE_PART_RE = re.compile(r"^__VIEWSTATE\d+$")

# Regex matching a DIBBS-style solicitation number (e.g., SPE7L1-26-T-0001).
_SOL_RE = re.compile(r"\b[A-Z]{2,4}[A-Z0-9]{1,4}-\d{2}-[A-Z]-\d{3,5}\b")

# Markers that mean "DIBBS routed us to its custom 404 page". When we see
# this we know the URL constant is wrong; surface it as a clear log entry.
_NOT_FOUND_MARKERS: tuple[str, ...] = (
    "<title>\n\tpage not found",
    "page not found",
    "pagenotfound.aspx",
    "did not find the requested page",
)


def _looks_like_not_found(html: str) -> bool:
    """Return True if the response body is the DIBBS 'Page Not Found' page."""
    lower = html.lower()
    # Need at least 2 markers to avoid false positives ("page not found"
    # could appear in unrelated copy on a legitimate page).
    return sum(1 for m in _NOT_FOUND_MARKERS if m in lower) >= 2


# ---------------------------------------------------------------------------
# Session wrapper
# ---------------------------------------------------------------------------

class DibbsSession:
    """Stateful HTTP session that handles the DIBBS click-through agreement.

    - Persists cookies to disk so we don't re-agree on every run.
    - Sleeps for ``SETTINGS.scrape_delay_seconds`` between requests.
    - Auto-detects the agreement page and replays the POST that accepts it.
    """

    def __init__(
        self,
        base_url: str | None = None,
        cookie_path: Path | None = None,
        delay_seconds: float | None = None,
        user_agent: str | None = None,
    ) -> None:
        self.base_url = (base_url or SETTINGS.dibbs_base_url).rstrip("/")
        self.delay = delay_seconds if delay_seconds is not None else SETTINGS.scrape_delay_seconds
        self.cookie_path = cookie_path or (SETTINGS.db_path.parent / "dibbs_cookies.lwp")
        self.cookie_path.parent.mkdir(parents=True, exist_ok=True)

        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": user_agent or SETTINGS.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )

        # Use an LWPCookieJar so we can persist cookies across runs in a
        # human-readable format (handy when debugging the agreement flow).
        jar = LWPCookieJar(filename=str(self.cookie_path))
        if self.cookie_path.exists():
            try:
                jar.load(ignore_discard=True, ignore_expires=True)
                logger.debug("Loaded cookies from %s", self.cookie_path)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not load cookies (%s); starting fresh", exc)
        self.session.cookies = jar  # type: ignore[assignment]

        self._agreed: bool = False
        self._last_request_ts: float = 0.0

    # ------------------------------------------------------------------ utils

    def _absolutize(self, url: str) -> str:
        """Turn a relative path into a full URL using the configured base."""
        if url.startswith("http://") or url.startswith("https://"):
            return url
        if not url.startswith("/"):
            url = "/" + url
        return f"{self.base_url}{url}"

    def _polite_sleep(self) -> None:
        """Block until ``delay`` seconds have passed since the last request."""
        if self.delay <= 0:
            return
        elapsed = time.monotonic() - self._last_request_ts
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)

    def save_cookies(self) -> None:
        """Persist cookies to disk for the next run."""
        try:
            self.session.cookies.save(ignore_discard=True, ignore_expires=True)  # type: ignore[attr-defined]
            logger.debug("Saved cookies to %s", self.cookie_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not save cookies: %s", exc)

    # ------------------------------------------------------------- low-level

    def _raw_request(
        self, method: str, url: str, *, data: dict[str, str] | None = None, **kwargs
    ) -> requests.Response:
        """One HTTP call with polite-delay enforcement."""
        full = self._absolutize(url)
        self._polite_sleep()
        logger.debug("%s %s", method, full)
        response = self.session.request(method, full, data=data, timeout=30, **kwargs)
        self._last_request_ts = time.monotonic()
        response.raise_for_status()
        return response

    # ---------------------------------------------------------- agreement

    @staticmethod
    def looks_like_agreement(html: str) -> bool:
        """Heuristic: does this HTML look like the click-through page?"""
        lower = html.lower()
        return any(marker in lower for marker in AGREEMENT_MARKERS)

    @staticmethod
    def extract_hidden_fields(html: str) -> dict[str, str]:
        """Pull every standard ASP.NET hidden field out of the HTML.

        Also handles DIBBS's multi-part viewstate: when
        ``__VIEWSTATEFIELDCOUNT`` is present, all numbered ``__VIEWSTATEn``
        fields are collected too.
        """
        soup = BeautifulSoup(html, "lxml")
        out: dict[str, str] = {}
        for name in _HIDDEN_FIELDS:
            el = soup.find("input", {"name": name})
            if el and el.get("value") is not None:
                out[name] = el["value"]

        # Capture any __VIEWSTATEn parts (n >= 1).
        for el in soup.find_all("input"):
            name = el.get("name")
            if name and _VIEWSTATE_PART_RE.match(name) and el.get("value") is not None:
                out[name] = el["value"]
        return out

    @staticmethod
    def _pick_form_action(html: str) -> str | None:
        """Return the form's action URL, or None if no form found."""
        soup = BeautifulSoup(html, "lxml")
        form = soup.find("form")
        if not form:
            return None
        return form.get("action") or None

    @staticmethod
    def _pick_agreement_button(html: str) -> str | None:
        """Find the 'I Agree' button name in the agreement form."""
        soup = BeautifulSoup(html, "lxml")
        # Prefer one of our known names, fall back to any submit input.
        for name in AGREEMENT_BUTTON_NAMES:
            if soup.find("input", {"name": name}):
                return name
        # Fallback: first submit-type input on the page.
        el = soup.find("input", {"type": "submit"})
        if el and el.get("name"):
            return el["name"]
        return None

    def accept_agreement(self, response: requests.Response) -> requests.Response:
        """POST the agreement form to set the acceptance cookie.

        Returns the response that comes back after acceptance. If the form
        couldn't be located, we log a warning and return the original
        response unchanged.
        """
        html = response.text
        hidden = self.extract_hidden_fields(html)
        button_name = self._pick_agreement_button(html)
        raw_action = self._pick_form_action(html) or response.url
        # Resolve relative form actions (e.g. ``./dodwarning.aspx?goto=...``)
        # against the URL we actually fetched.
        action = urljoin(response.url, raw_action)

        if not button_name or not hidden.get("__VIEWSTATE"):
            logger.warning(
                "Couldn't find agreement form fields. "
                "viewstate=%s button=%s — site layout may have changed.",
                bool(hidden.get("__VIEWSTATE")),
                button_name,
            )
            return response

        data = dict(hidden)
        # ASP.NET expects the button's name=value pair on the form post.
        # The live DIBBS button has value "OK"; older variants used
        # "I Agree". Either works as long as the name is right.
        data[button_name] = "OK"
        data.setdefault("__EVENTTARGET", "")
        data.setdefault("__EVENTARGUMENT", "")

        logger.info(
            "Accepting DIBBS agreement (button=%s, %d hidden fields)",
            button_name,
            len(hidden),
        )
        accepted = self._raw_request("POST", action, data=data)
        self._agreed = True
        self.save_cookies()
        return accepted

    # ------------------------------------------------------------------ get

    def get(self, url: str, **kwargs) -> requests.Response:
        """GET with auto-handle of the agreement page."""
        response = self._raw_request("GET", url, **kwargs)
        if self.looks_like_agreement(response.text):
            logger.debug("Agreement page detected on GET %s; accepting…", url)
            response = self.accept_agreement(response)
            # Retry the original URL once now that we've agreed.
            if self.looks_like_agreement(response.text):
                logger.warning("Still on agreement page after accept — bailing")
            else:
                response = self._raw_request("GET", url, **kwargs)
        self.save_cookies()
        return response

    def postback(
        self, url: str, event_target: str, html_with_state: str, extra: dict[str, str] | None = None
    ) -> requests.Response:
        """Do an ASP.NET postback against ``url`` using the hidden fields
        present in ``html_with_state``.

        ``event_target`` is the WebForms control id you want to trigger (e.g.
        the Page-2 link). ``extra`` lets callers add arbitrary form fields.
        """
        data = self.extract_hidden_fields(html_with_state)
        data["__EVENTTARGET"] = event_target
        data["__EVENTARGUMENT"] = data.get("__EVENTARGUMENT", "")
        if extra:
            data.update(extra)
        return self._raw_request("POST", url, data=data)


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

def _safe_name(s: str) -> str:
    """Make ``s`` safe to use in a filename."""
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in s)[:120]


def save_raw_page(name: str, html: str, *, inbox: Path | None = None) -> Path:
    """Persist raw HTML into the inbox for later parsing/debugging."""
    target_dir = inbox or SETTINGS.inbox_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
    out_path = target_dir / f"{stamp}_{_safe_name(name)}.html"
    out_path.write_text(html, encoding="utf-8")
    logger.info("Saved raw page to %s", out_path)
    return out_path


# ---------------------------------------------------------------------------
# Result-page extraction
# ---------------------------------------------------------------------------

def extract_solicitation_links(html: str) -> list[tuple[str, str | None]]:
    """Find every (solicitation_number, detail_url) pair on a results page.

    Two strategies are tried:
      1. Look for anchor tags whose text matches a solicitation pattern.
      2. Fall back to a regex sweep over the full document text.

    The detail URL may be None when only the bare solicitation number is
    available (e.g. plain-text results).
    """
    soup = BeautifulSoup(html, "lxml")
    found: dict[str, str | None] = {}

    for a in soup.find_all("a"):
        text = a.get_text(" ", strip=True)
        m = _SOL_RE.search(text)
        if m:
            href = a.get("href")
            found.setdefault(m.group(0), href)

    if not found:
        for m in _SOL_RE.finditer(soup.get_text(" ", strip=True)):
            found.setdefault(m.group(0), None)

    return list(found.items())


def find_pagination_targets(html: str) -> list[str]:
    """Return WebForms ``__EVENTTARGET`` ids that look like pagination links.

    DIBBS result grids typically render pagers as ``javascript:__doPostBack``
    anchors. We extract the first argument of those calls.
    """
    targets: list[str] = []
    seen: set[str] = set()
    pattern = re.compile(r"__doPostBack\(['\"]([^'\"]+)['\"]")
    for match in pattern.finditer(html):
        target = match.group(1)
        if target in seen:
            continue
        seen.add(target)
        # Heuristic: pager controls usually contain 'Pager', 'Page', or end
        # with '$<digit>'. Keep it loose; the caller decides what to follow.
        if any(tok in target for tok in ("Pager", "Page", "$ctl")):
            targets.append(target)
    return targets


# ---------------------------------------------------------------------------
# High-level flows
# ---------------------------------------------------------------------------

def fetch_rfqs_by_date(
    category: str = "recent",
    *,
    session: DibbsSession | None = None,
    max_pages: int = 1,
    follow_details: bool = True,
    dry_run: bool = False,
) -> list[Path]:
    """Scrape one of DIBBS's RFQ-by-date listings.

    ``category`` must be one of ``"recent"``, ``"issue"``, or ``"close"``,
    matching the three options DIBBS exposes on /RFQ/RFQDates.aspx.

    Returns the list of file paths written into the inbox. When ``dry_run``
    is True nothing is saved.
    """
    if category not in ("recent", "issue", "close"):
        raise ValueError(f"category must be recent/issue/close, got {category!r}")

    session = session or DibbsSession()
    saved: list[Path] = []

    url = f"{RFQ_DATES_PATH}?category={category}"
    response = session.get(url)
    page_html = response.text

    if _looks_like_not_found(page_html):
        logger.error(
            "DIBBS routed us to its 'Page Not Found' page for %s. "
            "The URL constant is likely wrong -- inspect the saved HTML.",
            url,
        )

    if not dry_run:
        saved.append(save_raw_page(f"rfqs_{category}_page1", page_html))

    page_index = 1
    while True:
        rows = extract_solicitation_links(page_html)
        logger.info("Page %d: found %d solicitation row(s)", page_index, len(rows))

        if follow_details:
            for sol, href in rows:
                detail_url = href or f"{SEARCH_BY_NSN_PATH}?value={sol}"
                try:
                    detail = session.get(detail_url)
                except requests.HTTPError as exc:
                    logger.warning("Skipping %s (HTTP %s)", sol, exc.response.status_code)
                    continue
                if not dry_run:
                    saved.append(save_raw_page(f"rfq_{sol}", detail.text))

        if page_index >= max_pages:
            break
        page_index += 1

        pager_targets = find_pagination_targets(page_html)
        if not pager_targets:
            logger.info("No more pagination targets; stopping at page %d", page_index - 1)
            break

        # Pick the pager target whose suffix matches the next page index
        # (e.g. "...$ctl02" for page 2). Fall back to the first one we found.
        next_target = next(
            (t for t in pager_targets if t.endswith(f"${page_index:02d}") or t.endswith(f"${page_index}")),
            pager_targets[0],
        )
        try:
            response = session.postback(url, next_target, page_html)
        except requests.HTTPError as exc:
            logger.warning("Pagination postback failed (%s); stopping", exc)
            break
        page_html = response.text
        if not dry_run:
            saved.append(save_raw_page(f"rfqs_{category}_page{page_index}", page_html))

    return saved


def fetch_recent_rfqs(
    *,
    session: DibbsSession | None = None,
    max_pages: int = 1,
    follow_details: bool = True,
    dry_run: bool = False,
) -> list[Path]:
    """Convenience alias for ``fetch_rfqs_by_date('recent', ...)``."""
    return fetch_rfqs_by_date(
        "recent",
        session=session,
        max_pages=max_pages,
        follow_details=follow_details,
        dry_run=dry_run,
    )


def fetch_by_fsc(
    fsc_codes: Iterable[str],
    *,
    session: DibbsSession | None = None,
    follow_details: bool = True,
    dry_run: bool = False,
    max_pages_per_fsc: int = 1,
) -> list[Path]:
    """Search DIBBS for each FSC in turn and (optionally) follow detail links."""
    session = session or DibbsSession()
    saved: list[Path] = []

    for fsc in fsc_codes:
        fsc = fsc.strip()
        if not fsc:
            continue
        logger.info("Searching FSC %s…", fsc)
        try:
            response = session.get(f"{SEARCH_BY_FSC_PATH}?value={fsc}")
        except requests.HTTPError as exc:
            logger.warning("FSC %s search failed: %s", fsc, exc)
            continue

        page_html = response.text
        if not dry_run:
            saved.append(save_raw_page(f"fsc_{fsc}_page1", page_html))

        for page_index in range(1, max_pages_per_fsc + 1):
            rows = extract_solicitation_links(page_html)
            logger.info("FSC %s page %d: %d row(s)", fsc, page_index, len(rows))

            if follow_details:
                for sol, href in rows:
                    detail_url = href or f"{SEARCH_BY_NSN_PATH}?value={sol}"
                    try:
                        detail = session.get(detail_url)
                    except requests.HTTPError as exc:
                        logger.warning("Skipping %s (HTTP %s)", sol, exc.response.status_code)
                        continue
                    if not dry_run:
                        saved.append(save_raw_page(f"rfq_{sol}", detail.text))

            if page_index >= max_pages_per_fsc:
                break
            pager_targets = find_pagination_targets(page_html)
            if not pager_targets:
                break
            next_target = pager_targets[0]
            try:
                response = session.postback(SEARCH_BY_FSC_PATH, next_target, page_html)
            except requests.HTTPError as exc:
                logger.warning("FSC %s pagination failed: %s", fsc, exc)
                break
            page_html = response.text
            if not dry_run:
                saved.append(save_raw_page(f"fsc_{fsc}_page{page_index + 1}", page_html))

    return saved


def fetch_daily_index(
    target_date: str | date,
    *,
    session: DibbsSession | None = None,
    dry_run: bool = False,
) -> Path | None:
    """Download ONE daily DIBBS RFQ index file (in<YYMMDD>.txt).

    Accepts either an ISO date string ('2026-06-01') or a ``datetime.date``.
    Returns the local Path where the file was saved (``None`` on dry-run or
    a fetch failure). The file is dropped into the inbox so ``parse_rfqs.py``
    picks it up unchanged.

    Note: the download host (dibbs2.bsm.dla.mil) requires the same DoD
    Warning agreement as the main site. The session warms itself up against
    the main site first so the agreement cookie is set for the parent domain.
    """
    session = session or DibbsSession()
    if isinstance(target_date, str):
        target_date = datetime.strptime(target_date, "%Y-%m-%d").date()
    yymmdd = target_date.strftime("%y%m%d")

    # Make sure we've accepted the warning at least once; the cookie is
    # scoped to .bsm.dla.mil so it also unlocks the dibbs2 download host.
    if not session._agreed:
        try:
            session.get("/")
        except requests.HTTPError as exc:
            logger.warning("Warm-up GET / failed: %s", exc)

    url = DAILY_INDEX_URL_TEMPLATE.format(yymmdd=yymmdd)
    try:
        response = session.get(url)
    except requests.HTTPError as exc:
        logger.error(
            "HTTP %s fetching daily index for %s",
            exc.response.status_code if exc.response else "?",
            target_date,
        )
        return None

    body = response.text
    if DibbsSession.looks_like_agreement(body):
        logger.error("Daily index for %s still on agreement page", target_date)
        return None

    # DIBBS returns 200 + an HTML "Page Not Found" for dates with no postings
    # (weekends, holidays). Detect by content rather than status code.
    if "<html" in body.lower() and "page not found" in body.lower():
        logger.warning("No daily index posted for %s (likely weekend/holiday)", target_date)
        return None
    if len(body) < 140:
        logger.warning("Daily index for %s looks empty (%d chars)", target_date, len(body))
        return None

    if dry_run:
        return None

    out_path = SETTINGS.inbox_dir / f"in{yymmdd}.txt"
    out_path.write_text(body, encoding="utf-8")
    logger.info(
        "Saved daily index to %s (%d lines, %d bytes)",
        out_path, len(body.splitlines()), len(body),
    )
    return out_path


def fetch_recent_indexes(
    days_back: int = 7,
    *,
    session: DibbsSession | None = None,
    dry_run: bool = False,
    end_date: date | None = None,
) -> list[Path]:
    """Download the last ``days_back`` daily index files (today backwards).

    Days with no postings (weekends, holidays) are skipped gracefully.
    """
    session = session or DibbsSession()
    saved: list[Path] = []
    last_day = end_date or date.today()
    for offset in range(days_back):
        d = last_day - timedelta(days=offset)
        path = fetch_daily_index(d, session=session, dry_run=dry_run)
        if path:
            saved.append(path)
    return saved


def fetch_rfq_detail(
    solicitation_number: str,
    *,
    session: DibbsSession | None = None,
    dry_run: bool = False,
) -> Path | None:
    """Download a single RFQ detail page by solicitation number."""
    session = session or DibbsSession()
    try:
        response = session.get(f"{SEARCH_BY_NSN_PATH}?value={solicitation_number}")
    except requests.HTTPError as exc:
        logger.error("HTTP %s fetching %s", exc.response.status_code, solicitation_number)
        return None
    if dry_run:
        return None
    return save_raw_page(f"rfq_{solicitation_number}", response.text)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="fetch_rfqs",
        description=(
            "Scrape DLA DIBBS for RFQs. By default does nothing -- pass --recent, "
            "--fsc, or --solicitation to actually fetch."
        ),
    )
    p.add_argument(
        "--daily",
        metavar="YYYY-MM-DD",
        help="Download ONE daily DIBBS index file (in<YYMMDD>.txt). "
             "This is the fastest way to ingest a day's worth of RFQs.",
    )
    p.add_argument(
        "--last-n-days",
        type=int,
        metavar="N",
        help="Download the last N daily DIBBS index files. Recommended for "
             "the regular daily refresh workflow.",
    )
    p.add_argument(
        "--recent",
        action="store_true",
        help="Scrape the 'RFQs Recent' listing (/RFQ/RFQDates.aspx?category=recent). "
             "This is mostly a directory of daily download files; use --daily / "
             "--last-n-days for the actual RFQ data.",
    )
    p.add_argument(
        "--by-issue-date",
        action="store_true",
        help="Scrape RFQs by Issue Date (/RFQ/RFQDates.aspx?category=issue).",
    )
    p.add_argument(
        "--by-close-date",
        action="store_true",
        help="Scrape RFQs by Return-By Date (/RFQ/RFQDates.aspx?category=close).",
    )
    p.add_argument(
        "--fsc",
        metavar="LIST",
        help="Comma-separated list of FSC codes to search (e.g. 5305,5310,5340).",
    )
    p.add_argument(
        "--solicitation",
        metavar="NUMBER",
        help="Fetch a single RFQ detail page by solicitation number.",
    )
    p.add_argument(
        "--max-pages",
        type=int,
        default=1,
        help="Max paginated result pages per query (default: 1).",
    )
    p.add_argument(
        "--no-detail",
        action="store_true",
        help="Skip following each row's detail link; only save the listing pages.",
    )
    p.add_argument(
        "--ingest",
        action="store_true",
        help="After fetching, run parse_rfqs to load the inbox into the DB.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Do everything except save HTML or hit the DB.",
    )
    p.add_argument(
        "--delay",
        type=float,
        default=None,
        help="Override DIBBS_SCRAPE_DELAY for this run.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    args = _build_parser().parse_args(argv)

    if not (
        args.daily
        or args.last_n_days
        or args.recent
        or args.by_issue_date
        or args.by_close_date
        or args.fsc
        or args.solicitation
    ):
        logger.error(
            "Nothing to do. Pass --daily / --last-n-days / --recent / "
            "--by-issue-date / --by-close-date / --fsc / --solicitation. "
            "Use --help for full options."
        )
        return 2

    session = DibbsSession(delay_seconds=args.delay)
    saved: list[Path] = []

    if args.daily:
        path = fetch_daily_index(args.daily, session=session, dry_run=args.dry_run)
        if path:
            saved.append(path)

    if args.last_n_days:
        saved.extend(
            fetch_recent_indexes(
                args.last_n_days, session=session, dry_run=args.dry_run
            )
        )

    for flag, category in (
        (args.recent, "recent"),
        (args.by_issue_date, "issue"),
        (args.by_close_date, "close"),
    ):
        if flag:
            saved.extend(
                fetch_rfqs_by_date(
                    category,
                    session=session,
                    max_pages=args.max_pages,
                    follow_details=not args.no_detail,
                    dry_run=args.dry_run,
                )
            )

    if args.fsc:
        codes = [c for c in re.split(r"[,\s]+", args.fsc) if c]
        saved.extend(
            fetch_by_fsc(
                codes,
                session=session,
                follow_details=not args.no_detail,
                dry_run=args.dry_run,
                max_pages_per_fsc=args.max_pages,
            )
        )

    if args.solicitation:
        out = fetch_rfq_detail(
            args.solicitation, session=session, dry_run=args.dry_run
        )
        if out:
            saved.append(out)

    logger.info("Done. %d file(s) written to %s", len(saved), SETTINGS.inbox_dir)
    print(f"Saved {len(saved)} file(s) into {SETTINGS.inbox_dir}")

    if args.ingest and not args.dry_run:
        # Import lazily so this module remains useful without the DB layer.
        from scraper.parse_rfqs import ingest_directory

        n = ingest_directory()
        print(f"Ingested {n} RFQ row(s) into the database.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
