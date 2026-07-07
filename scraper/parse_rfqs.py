"""Parse RFQ records from local files (CSV / HTML / TXT) into dicts.

The output dicts use the same key names as ``db.database.RFQ_COLUMNS`` so they
can be passed straight to ``upsert_rfq``.

Run as a script (``python scraper/parse_rfqs.py``) to ingest every file in the
inbox directory (``DIBBS_INBOX_DIR``) into the SQLite database.
"""

from __future__ import annotations

import csv
import logging
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

# Allow running directly: python scraper/parse_rfqs.py
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from bs4 import BeautifulSoup  # noqa: E402

from db.database import bulk_upsert_rfqs  # noqa: E402
from utils.config import SETTINGS  # noqa: E402
from utils.logging_config import configure_logging  # noqa: E402

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Field-level helpers (used by every parser path)
# ---------------------------------------------------------------------------

# Truthy strings we accept for boolean-ish columns like "technical_documents_available"
_TRUE_TOKENS = {"y", "yes", "true", "1", "available", "t"}

# Regex matching standard 13-character NSNs (4 + 2 + 3 + 4) with or without dashes.
_NSN_RE = re.compile(r"\b(\d{4})[-\s]?(\d{2})[-\s]?(\d{3})[-\s]?(\d{4})\b")

# Solicitation numbers look like SPE7L1-26-T-0001 (alnum + dashes). Loose match.
_SOL_RE = re.compile(r"\b[A-Z]{2,4}[A-Z0-9]{1,4}-\d{2}-[A-Z]-\d{3,5}\b")

# Date formats we'll try in order. Add more here if real DIBBS data needs them.
_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%d-%b-%Y", "%b %d, %Y")


def _to_bool(value: Any) -> int:
    """Coerce a value to 0/1 for technical_documents_available."""
    if value is None:
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(bool(value))
    return int(str(value).strip().lower() in _TRUE_TOKENS)


def _to_int(value: Any) -> int | None:
    """Pull the first integer out of a string, returning None if not found."""
    if value is None or value == "":
        return None
    if isinstance(value, int):
        return value
    m = re.search(r"-?\d+", str(value).replace(",", ""))
    return int(m.group()) if m else None


def _to_iso_date(value: Any) -> str | None:
    """Normalize various date strings to ISO (YYYY-MM-DD). None on failure."""
    if not value:
        return None
    s = str(value).strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    logger.debug("Could not parse date: %r", value)
    return None


def normalize_nsn(value: str | None) -> str | None:
    """Return a canonical NSN like '5305-00-123-4567' or None."""
    if not value:
        return None
    m = _NSN_RE.search(value)
    if not m:
        return None
    return "-".join(m.groups())


def fsc_from_nsn(nsn: str | None) -> str | None:
    """First 4 digits of an NSN are the FSC (Federal Supply Class)."""
    if not nsn:
        return None
    digits = re.sub(r"\D", "", nsn)
    return digits[:4] if len(digits) >= 4 else None


def _clean_list(value: Any) -> str | None:
    """Normalize a delimited list (CAGEs, MPNs) into comma-separated string."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    parts = [p.strip() for p in re.split(r"[;,]\s*", s) if p.strip()]
    return ", ".join(parts) if parts else None


# ---------------------------------------------------------------------------
# Build a normalized RFQ dict from a raw record (CSV row, HTML extract, etc.)
# ---------------------------------------------------------------------------

# Friendly aliases -> canonical column names. Add new aliases when real DIBBS
# exports use slightly different labels.
_ALIASES: dict[str, str] = {
    "solicitation": "solicitation_number",
    "solicitation_no": "solicitation_number",
    "solicitation_#": "solicitation_number",
    "rfq": "solicitation_number",
    "national_stock_number": "nsn",
    "fed_supply_class": "fsc",
    "item": "item_name",
    "item_description": "item_name",
    "nomenclature": "item_name",
    "qty": "quantity",
    "uoi": "unit_of_issue",
    "ui": "unit_of_issue",
    "issued": "issue_date",
    "return_by": "close_date",
    "close": "close_date",
    "due_date": "close_date",
    "setaside": "set_aside",
    "pr_number": "purchase_request_number",
    "purchase_request": "purchase_request_number",
    "approved_sources": "approved_source_cages",
    "approved_cage": "approved_source_cages",
    "cages": "approved_source_cages",
    "mpn": "manufacturer_part_numbers",
    "manufacturer_pn": "manufacturer_part_numbers",
    "manufacturer_part_number": "manufacturer_part_numbers",
    "tdp_available": "technical_documents_available",
    "tech_docs": "technical_documents_available",
    "technical_documents": "technical_documents_available",
    "link": "url",
}


def _canonical_key(raw_key: str) -> str:
    """Convert a CSV/HTML label into our canonical column name."""
    key = raw_key.strip().lower().replace(" ", "_").replace("-", "_")
    key = re.sub(r"[^a-z0-9_]", "", key)
    return _ALIASES.get(key, key)


def build_rfq(record: dict[str, Any], raw_text: str | None = None) -> dict[str, Any]:
    """Normalize a free-form record into the canonical RFQ shape.

    Robust to missing fields: anything not present becomes None.
    """
    # Re-key into canonical column names.
    canon: dict[str, Any] = {}
    for k, v in record.items():
        canon[_canonical_key(str(k))] = v

    nsn = normalize_nsn(canon.get("nsn") or canon.get("national_stock_number"))
    # If FSC wasn't given explicitly, derive it from the NSN.
    fsc = canon.get("fsc") or fsc_from_nsn(nsn)

    rfq = {
        "solicitation_number": (canon.get("solicitation_number") or "").strip() or None,
        "nsn": nsn,
        "fsc": str(fsc).zfill(4) if fsc else None,
        "item_name": canon.get("item_name"),
        "quantity": _to_int(canon.get("quantity")),
        "unit_of_issue": canon.get("unit_of_issue"),
        "issue_date": _to_iso_date(canon.get("issue_date")),
        "close_date": _to_iso_date(canon.get("close_date")),
        "set_aside": canon.get("set_aside"),
        "purchase_request_number": canon.get("purchase_request_number"),
        "buyer": canon.get("buyer"),
        "approved_source_cages": _clean_list(canon.get("approved_source_cages")),
        "manufacturer_part_numbers": _clean_list(canon.get("manufacturer_part_numbers")),
        "amsc": (str(canon.get("amsc") or "").strip().upper() or None),
        "technical_documents_available": _to_bool(
            canon.get("technical_documents_available")
        ),
        "url": canon.get("url"),
        "raw_text": raw_text,
    }
    return rfq


# ---------------------------------------------------------------------------
# File-format parsers
# ---------------------------------------------------------------------------

def parse_csv(path: Path) -> list[dict[str, Any]]:
    """Parse a DIBBS-style CSV export into normalized RFQ dicts."""
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        for raw in reader:
            rfq = build_rfq(raw)
            if rfq["solicitation_number"]:
                rows.append(rfq)
    logger.info("Parsed %d row(s) from CSV %s", len(rows), path.name)
    return rows


# ---------------------------------------------------------------------------
# DIBBS "daily index" files (in<YYMMDD>.txt) -- fixed-width batch downloads
# ---------------------------------------------------------------------------
#
# Field layout per https://www.dibbs.bsm.dla.mil/Rfq/RfqFileDefs.aspx
# (every record is exactly 140 chars).
DAILY_INDEX_FIELDS: tuple[tuple[str, int, int], ...] = (
    ("solicitation_number",     0,  13),
    ("nsn_or_part",            13,  59),  # 13-char NSN or up to 46-char part #
    ("purchase_request_number",59,  72),
    ("return_by",              72,  80),  # MM/DD/YY
    ("file_name",              80,  99),  # e.g. SPE1C126T1287.pdf
    ("qty",                    99, 106),  # zero-padded
    ("unit_of_issue",         106, 108),
    ("nomenclature",          108, 129),
    ("buyer_code",            129, 134),
    ("amsc",                  134, 135),  # Acquisition Method Suffix Code
    ("item_type",             135, 136),  # 1 = NSN, 2 = Part Number
    ("sb_indicator",          136, 137),  # Y/H/R/L/A/E/N (set-aside type)
    ("sb_percentage",         137, 140),
)

# Map the one-char Small Business indicator to a readable set-aside label.
_SB_INDICATOR_MAP: dict[str, str] = {
    "Y": "Small Business Set-Aside",
    "H": "HUBZone Set-Aside",
    "R": "SDVOSB Set-Aside",
    "L": "WOSB Set-Aside",
    "A": "8(a) Set-Aside",
    "E": "EDWOSB Set-Aside",
    "N": "Unrestricted",
}


def _parse_mmddyy(s: str) -> str | None:
    """DIBBS daily-index dates are MM/DD/YY; return ISO 'YYYY-MM-DD'."""
    s = s.strip()
    if not s:
        return None
    try:
        return datetime.strptime(s, "%m/%d/%y").date().isoformat()
    except ValueError:
        logger.debug("Could not parse MM/DD/YY date: %r", s)
        return None


def _format_nsn(thirteen_digits: str) -> str | None:
    """Convert a 13-digit NSN like '8465016203362' to '8465-01-620-3362'."""
    digits = thirteen_digits.strip()
    if len(digits) != 13 or not digits.isdigit():
        return None
    return f"{digits[:4]}-{digits[4:6]}-{digits[6:9]}-{digits[9:]}"


def _parse_daily_index_line(line: str) -> dict[str, Any] | None:
    """Slice one 140-char line into the canonical RFQ shape."""
    if len(line) < 140:
        return None

    raw = {name: line[start:end] for name, start, end in DAILY_INDEX_FIELDS}
    sol = raw["solicitation_number"].strip()
    if not sol:
        return None

    # Item Type 1 = the nsn_or_part field starts with a 13-digit NSN.
    # Item Type 2 = it's a manufacturer part number (no NSN).
    item_type = raw["item_type"].strip()
    nsn = None
    mpn = None
    if item_type == "1":
        nsn = _format_nsn(raw["nsn_or_part"][:13])
    else:
        mpn = raw["nsn_or_part"].strip() or None

    # Derive FSC from the first 4 digits of the NSN when possible.
    fsc: str | None = None
    if nsn:
        digits = re.sub(r"\D", "", nsn)
        if len(digits) >= 4:
            fsc = digits[:4]

    # Quantity is zero-padded; treat blank as None.
    qty_raw = raw["qty"].strip().lstrip("0")
    quantity = int(qty_raw) if qty_raw.isdigit() else None

    # Set-aside: combine indicator letter + percentage when meaningful.
    sb_ind = raw["sb_indicator"].strip()
    sb_pct = raw["sb_percentage"].strip().lstrip("0")
    set_aside = _SB_INDICATOR_MAP.get(sb_ind)
    if set_aside and sb_pct and sb_pct != "0":
        set_aside = f"{set_aside} ({sb_pct}%)"

    # Build a stable solicitation URL we can link from the dashboard. The
    # public 'RFQs by Solicitation' search accepts the bare number.
    url = f"https://www.dibbs.bsm.dla.mil/RFQ/RFQNsn.aspx?value={sol}"

    return {
        "solicitation_number": sol,
        "nsn": nsn,
        "fsc": fsc,
        "item_name": raw["nomenclature"].strip() or None,
        "quantity": quantity,
        "unit_of_issue": raw["unit_of_issue"].strip() or None,
        "issue_date": None,            # not in this file
        "close_date": _parse_mmddyy(raw["return_by"]),
        "set_aside": set_aside,
        "purchase_request_number": raw["purchase_request_number"].strip() or None,
        "buyer": raw["buyer_code"].strip() or None,
        "approved_source_cages": None,  # not in this file (detail-page only)
        "manufacturer_part_numbers": mpn,
        "amsc": raw["amsc"].strip().upper() or None,
        "technical_documents_available": 0,
        "url": url,
        "raw_text": line,
    }


def _looks_like_daily_index(content: str) -> bool:
    """Detect DIBBS daily-index files by their fixed-width signature.

    Heuristic: at least 1 non-empty line, every non-empty line is exactly
    140 chars, and the first 13 chars look like a solicitation number.
    """
    lines = [ln for ln in content.splitlines() if ln.strip()]
    if not lines:
        return False
    if not all(len(ln) == 140 for ln in lines[:20]):
        return False
    return bool(re.match(r"^[A-Z0-9]{13}$", lines[0][:13]))


def parse_daily_index(path: Path) -> list[dict[str, Any]]:
    """Parse a DIBBS in<YYMMDD>.txt daily index file.

    Because a single solicitation can appear on multiple lines (one per
    CLIN), we de-duplicate on solicitation_number, keeping the last row
    seen for that solicitation. The line items are preserved in raw_text.
    """
    # DIBBS serves these files in ISO-8859-1; tolerate odd bytes.
    content = path.read_text(encoding="latin-1", errors="replace")
    by_sol: dict[str, dict[str, Any]] = {}
    for line in content.splitlines():
        if len(line) < 140:
            continue
        rfq = _parse_daily_index_line(line)
        if rfq:
            by_sol[rfq["solicitation_number"]] = rfq
    rows = list(by_sol.values())
    logger.info(
        "Parsed %d RFQ row(s) (from %d line(s)) in daily index %s",
        len(rows), len(content.splitlines()), path.name,
    )
    return rows


def parse_text(path: Path) -> list[dict[str, Any]]:
    """Parse a text file -- either a DIBBS daily index or a Key:Value dump.

    Detection is by content: if every non-empty line is exactly 140 chars
    and starts with a solicitation pattern, we treat it as a daily index
    (DIBBS's bulk download format). Otherwise, fall back to parsing it as
    a free-form ``Key: Value`` dump.
    """
    content = path.read_text(encoding="utf-8", errors="replace")

    if _looks_like_daily_index(content):
        return parse_daily_index(path)

    record: dict[str, Any] = {}
    for line in content.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        record[key.strip()] = value.strip()

    # Last-ditch: scan the whole blob for a solicitation/NSN if labels were odd.
    if "solicitation_number" not in {_canonical_key(k) for k in record}:
        m = _SOL_RE.search(content)
        if m:
            record["solicitation_number"] = m.group(0)

    rfq = build_rfq(record, raw_text=content)
    if not rfq["solicitation_number"]:
        logger.warning("Skipping %s: no solicitation number found", path.name)
        return []
    logger.info("Parsed text RFQ %s from %s", rfq["solicitation_number"], path.name)
    return [rfq]


def _looks_like_results_grid(table) -> bool:
    """Heuristic: does this <table> look like a DIBBS search-results grid?

    Results grids have a header row with several recognizable column labels
    (Solicitation, NSN, Nomenclature, etc.) and multiple data rows, each of
    which contains a solicitation number somewhere.
    """
    rows = table.find_all("tr")
    if len(rows) < 2:
        return False

    header_text = rows[0].get_text(" ", strip=True).lower()
    header_hits = sum(
        1
        for h in ("solicitation", "nsn", "nomenclature", "item", "return by", "close")
        if h in header_text
    )
    if header_hits < 2:
        return False

    # Need at least one data row that looks like a solicitation.
    sol_rows = sum(1 for r in rows[1:] if _SOL_RE.search(r.get_text(" ", strip=True)))
    return sol_rows >= 1


def _parse_results_grid(table) -> list[dict[str, Any]]:
    """Extract one RFQ dict per data row of a results grid."""
    rows = table.find_all("tr")
    headers = [c.get_text(" ", strip=True) for c in rows[0].find_all(["th", "td"])]

    records: list[dict[str, Any]] = []
    for row in rows[1:]:
        cells = row.find_all(["th", "td"])
        if not cells:
            continue
        text = row.get_text(" ", strip=True)
        if not _SOL_RE.search(text):
            continue  # skip pager/footer rows

        record: dict[str, Any] = {}
        for header, cell in zip(headers, cells):
            value = cell.get_text(" ", strip=True)
            if header and value:
                record[header] = value

            # Capture any href on the row as a candidate detail URL.
            link = cell.find("a", href=True)
            if link and "url" not in record:
                record["url"] = link["href"]

        # Make sure the solicitation number is set even if the grid header
        # used an unexpected label.
        if "solicitation_number" not in {_canonical_key(k) for k in record}:
            m = _SOL_RE.search(text)
            if m:
                record["solicitation_number"] = m.group(0)

        records.append(record)
    return records


def parse_html(path: Path) -> list[dict[str, Any]]:
    """Parse a saved DIBBS HTML page.

    Handles two shapes:
      1. **Results grid** -- a <table> with many solicitation rows. Each row
         becomes its own RFQ dict.
      2. **Detail page** -- a key/value layout (``<th>label</th><td>value</td>``).
         A single RFQ dict is returned.

    Detail pages are preferred when both shapes appear, since they carry more
    fields than the summary grid.
    """
    html = path.read_text(encoding="utf-8", errors="replace")
    soup = BeautifulSoup(html, "lxml")
    raw_text = soup.get_text("\n", strip=True)

    # ---- (1) detail-page sweep: collect label/value pairs from every table.
    detail_record: dict[str, Any] = {}
    for row in soup.find_all("tr"):
        cells = row.find_all(["th", "td"])
        if len(cells) >= 2:
            label = cells[0].get_text(" ", strip=True).rstrip(":")
            value = cells[1].get_text(" ", strip=True)
            if label and value:
                detail_record.setdefault(label, value)

    detail_rfq = build_rfq(detail_record, raw_text=raw_text) if detail_record else None

    # ---- (2) results-grid sweep: emit one RFQ per data row.
    grid_records: list[dict[str, Any]] = []
    for table in soup.find_all("table"):
        if _looks_like_results_grid(table):
            grid_records.extend(_parse_results_grid(table))

    # If we found grid rows AND the detail sweep only yielded a sparse record
    # (or no solicitation number at all), prefer the grid rows.
    if grid_records:
        if (
            detail_rfq
            and detail_rfq.get("solicitation_number")
            and any(detail_rfq.get(k) for k in ("quantity", "approved_source_cages", "manufacturer_part_numbers"))
        ):
            # Detail page wins; keep the rich record.
            logger.debug("Detail-page record preferred over grid in %s", path.name)
            return [detail_rfq]

        out = [build_rfq(r, raw_text=None) for r in grid_records]
        out = [r for r in out if r.get("solicitation_number")]
        logger.info("Parsed %d grid row(s) from %s", len(out), path.name)
        return out

    # ---- (3) fallback: single-record detail page.
    if detail_rfq and detail_rfq.get("solicitation_number"):
        return [detail_rfq]

    # Last-ditch: scan the whole blob for a solicitation number.
    m = _SOL_RE.search(raw_text)
    if m:
        return [build_rfq({"solicitation_number": m.group(0)}, raw_text=raw_text)]

    logger.warning("Skipping %s: no solicitation number found", path.name)
    return []


# Mapping of suffix -> handler. Add new file types here.
_PARSERS = {
    ".csv": parse_csv,
    ".txt": parse_text,
    ".html": parse_html,
    ".htm": parse_html,
}


def parse_file(path: Path) -> list[dict[str, Any]]:
    """Dispatch to the right parser based on file suffix."""
    handler = _PARSERS.get(path.suffix.lower())
    if not handler:
        logger.debug("Skipping unsupported file: %s", path.name)
        return []
    try:
        return handler(path)
    except Exception as exc:  # noqa: BLE001 -- want to log + continue
        logger.exception("Failed to parse %s: %s", path.name, exc)
        return []


def parse_directory(directory: Path | None = None) -> list[dict[str, Any]]:
    """Parse every supported file in ``directory`` (defaults to inbox)."""
    directory = directory or SETTINGS.inbox_dir
    rfqs: list[dict[str, Any]] = []
    for path in sorted(directory.iterdir()) if directory.exists() else []:
        if path.is_file():
            rfqs.extend(parse_file(path))
    logger.info("Parsed %d total RFQ(s) from %s", len(rfqs), directory)
    return rfqs


def ingest_directory(
    directory: Path | None = None, rfqs: Iterable[dict[str, Any]] | None = None
) -> int:
    """Parse and write to DB. Returns rows upserted."""
    records = list(rfqs) if rfqs is not None else parse_directory(directory)
    if not records:
        logger.warning("No RFQ records to ingest")
        return 0
    return bulk_upsert_rfqs(records)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    configure_logging()
    logger.info("Inbox directory: %s", SETTINGS.inbox_dir)
    count = ingest_directory()
    print(f"Ingested {count} RFQ record(s) from {SETTINGS.inbox_dir}")


if __name__ == "__main__":
    main()
