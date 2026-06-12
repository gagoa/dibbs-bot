# dibbs-bot

A local-first toolkit for searching, ingesting, scoring, and triaging DLA/DIBBS
RFQs from your laptop. Everything runs on top of SQLite, with a small Streamlit
dashboard for day-to-day use.

> **Status:** MVP. Live DIBBS scraping is implemented (best-effort,
> well-instrumented) in `scraper/fetch_rfqs.py`. The system also works with
> manually downloaded CSV/HTML/TXT files and the included sample data.

---

## What this tool does

1. Stores RFQs (Request for Quotes) from DIBBS in a local SQLite database.
2. Parses CSV / HTML / TXT files dropped into `scraper/sample_data/` into
   normalized rows.
3. Scores each RFQ from 0–100 using transparent, tunable heuristics (FSC
   category, quantity, close date, approved sources, TDP, set-aside, etc.).
4. Surfaces results in a Streamlit dashboard with tabs for **Top
   Opportunities**, **Needs Review**, **Avoid / High Complexity**, and search
   by NSN / FSC.

---

## Project layout

```
dibbs-bot/
├── README.md
├── requirements.txt
├── .env.example                # copy to .env to override defaults
├── data/                       # dibbs.db created here at runtime
├── scraper/
│   ├── fetch_rfqs.py           # stub for live DIBBS scraping
│   ├── parse_rfqs.py           # ingest CSV/HTML/TXT into the DB
│   └── sample_data/            # drop manually-downloaded files here
├── db/
│   ├── database.py             # SQLite helpers + init script
│   └── models.sql              # schema (rfqs + opportunity_scores)
├── analysis/
│   ├── nsn_tools.py            # FSC classification + keyword lists
│   └── score_opportunities.py  # 0-100 scoring with explanations
├── app/
│   └── dashboard.py            # Streamlit UI
└── utils/
    ├── config.py               # env-driven settings via python-dotenv
    └── logging_config.py       # rotating-file + console logger
```

---

## Quickstart (one-time setup, then everything runs from the dashboard)

```bash
# 1. Create + activate a virtualenv
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. (Optional) Copy and edit configuration
cp .env.example .env

# 4. Create the SQLite schema (idempotent — safe to re-run)
python db/database.py

# 5. Launch the dashboard
streamlit run app/dashboard.py
```

The dashboard opens at <http://localhost:8501>. From there:

1. In the left sidebar, find the **Refresh from DIBBS** panel.
2. Pick how many days back to pull (default 7) and click **Pull & Score**.
3. The status panel streams progress as it downloads daily index files,
   ingests them into SQLite, and recomputes every score.
4. When it finishes, the tables and KPIs on the page refresh automatically.

The **Re-score** button skips the network call and just re-runs the scoring
pass — handy after you tweak the `WEIGHTS` dict in
`analysis/score_opportunities.py`.

> The CLI scripts (`python scraper/fetch_rfqs.py …`,
> `python scraper/parse_rfqs.py`, `python analysis/score_opportunities.py`)
> still work and are documented below — but you don't need them for normal
> day-to-day use.

---

## Sharing the dashboard with friends

The easiest way to host this is **Streamlit Community Cloud** — free, takes
~5 minutes, and was built for exactly this. Steps:

1. Push the repo to GitHub:

   ```bash
   git init
   git add .
   git commit -m "initial commit"
   git remote add origin https://github.com/YOUR_USER/dibbs-bot.git
   git branch -M main
   git push -u origin main
   ```

   Double-check that `.env`, `.streamlit/secrets.toml`, and `data/*.db` are
   **not** in the diff — they're all in `.gitignore` already.

2. Go to <https://share.streamlit.io>, sign in with GitHub, click **New
   app**. Point it at your repo, branch `main`, file path
   `app/dashboard.py`. Click **Deploy**.

3. **Set a shared password.** Once the app is up, open it on
   share.streamlit.io and click **Settings → Secrets**. Paste:

   ```toml
   dibbs_password = "pick-something-private"
   ```

   The dashboard automatically reads this on next page load and refuses to
   render anything until visitors sign in. With no `dibbs_password` set, the
   gate is a no-op (so local `streamlit run` still works without setup).

4. Share the URL + password with your friends. They'll see one login screen,
   then the full dashboard.

### Things to know about hosted use

- **The SQLite DB is ephemeral on Community Cloud.** When the container
  restarts (or wakes from sleep), `data/dibbs.db` resets. Your friends just
  click **Pull & Score** once and it rebuilds in a couple minutes. If you
  want truly persistent storage, see "Where to go from here" below.
- **Concurrent scrapes are prevented.** If two users click **Pull & Score**
  at the same time, one runs and the other sees a "another session is
  already refreshing" message until the first finishes. This keeps you from
  opening multiple parallel DIBBS sessions from the same IP.
- **Keep the scrape delay polite.** `DIBBS_SCRAPE_DELAY=2` is the default
  and a good citizen on DLA's infrastructure. Don't lower it on a hosted
  deployment.

### Local dev with a password

If you want to test the login gate locally, copy
`.streamlit/secrets.toml.example` to `.streamlit/secrets.toml` and set
`dibbs_password`. The real `secrets.toml` is gitignored.

---

## How to use each piece

### Initialize the database

```bash
python db/database.py
```

Creates `data/dibbs.db` (override with `DIBBS_DB_PATH` in `.env`). Idempotent
— safe to re-run.

### Import RFQ data

Drop files into `scraper/sample_data/` (or whatever `DIBBS_INBOX_DIR` points
to). Supported file types:

- **CSV** — column headers are case-insensitive; common DIBBS aliases like
  `Solicitation`, `RFQ`, `Item Description`, `Return By` are recognized.
- **HTML** — saved DIBBS detail pages. The parser walks every `<tr>` and
  treats the first cell as a label and the second as the value.
- **TXT** — free-form `Key: Value` lines (one RFQ per file).

Then run:

```bash
python scraper/parse_rfqs.py
```

This is also where you'd plug in any custom export flow — anything that drops
files in the inbox will get picked up.

### Score the database

```bash
python analysis/score_opportunities.py
```

Reads every RFQ in the DB, computes a fresh score, and inserts a new row into
`opportunity_scores`. The dashboard always shows the most recent score per
RFQ.

#### The scoring framework

Every RFQ gets a **0–100 overall score** built from seven weighted subscores
plus derived estimates and a recommended action:

| Subscore           | Max | What it measures                                                  |
| ------------------ | --- | ----------------------------------------------------------------- |
| Sourceability      | 18  | Can we find suppliers? Tier-A/B FSC, open CAGEs, friendly keywords|
| Competition        | 15  | How crowded is the bid? (Higher = LESS competition)               |
| Profit potential   | 15  | Estimated gross margin range                                      |
| Time-to-Quote      | 15  | How fast can we research + quote? (Higher = FASTER quote)         |
| Technical risk     | 15  | Testing / TDP / FAT / hazmat (Higher = LESS risk)                 |
| Capital efficiency | 12  | Fit for a ~$50K-working-capital firm                              |
| Delivery           | 10  | Sweet spot 30–60 days (too short = rushed, too long = ties capital) |

The scorer also produces:

- **Estimated capital required** (quantity × rough unit price by FSC)
- **Estimated margin range** (low % – high %)
- **Estimated quote hours** (15 min – 6+ hours)
- **Estimated profit per hour of procurement effort** — the key throughput
  metric for a one-person operation
- **Estimated win probability** (0–1)
- **Recommended action**: `BID IMMEDIATELY`, `INVESTIGATE SUPPLIER FIRST`, or `AVOID`

Calibration notes:

- **Universe is broad.** Every item a small distributor could realistically
  source is scored. Fasteners are *not* preferentially favored — they're
  commodity (Tier C). Unknown FSCs default to Tier B (sourceable).
- **Aggressive penalties** for sole-source CAGEs, TDP/build-to-print,
  aerospace-critical keywords, capital > $25K, custom manufacturing, etc.
- **Hard ceilings** prevent inflation: a sole-source RFQ caps at 84, TDP at
  89, Tier-D (weapons/medical/hazmat) at 74, custom-engineered at 49.
- **Perfect-score gating**: a score ≥95 requires *all* of: multi-supplier,
  margin top ≥25%, low competition, capital <20% of budget, delivery in
  30–60d, low tech risk, no compliance concerns, and quote-time ≤1h.
- **Target distribution**: avg 55–70, top 5% ≥85, top 0.5% ≥95.

Each score comes with a detailed plain-text explanation (visible in the
dashboard under "Score explanation"). Tune the FSC tiers and unit-price
table in `analysis/nsn_tools.py`, and the band thresholds + ceilings in
`analysis/score_opportunities.py`, as you learn what wins are repeatable.

### Launch the dashboard

```bash
streamlit run app/dashboard.py
```

Tabs:

- **Top Opportunities** — RFQs flagged `BID IMMEDIATELY`
- **Needs Review** — RFQs flagged `INVESTIGATE SUPPLIER FIRST`
- **Avoid / High Complexity** — RFQs flagged `AVOID` (red flags or low score)
- **Search by NSN** — substring search across all NSNs
- **Search by FSC** — multi-select with friendly FSC labels
- **All RFQs** — full table with a CSV download button

Filters in the sidebar (recommended action, FSC, NSN, set-aside, score
range, close date, TDP availability) apply to every tab.

---

## Configuration

All settings come from environment variables, optionally loaded from `.env`.
Defaults work out of the box; you only need a `.env` to override.

| Variable               | Default                                              | Purpose                                      |
| ---------------------- | ---------------------------------------------------- | -------------------------------------------- |
| `DIBBS_DB_PATH`        | `data/dibbs.db`                                      | SQLite file location                         |
| `DIBBS_INBOX_DIR`      | `scraper/sample_data`                                | Folder scanned by `parse_rfqs.py`            |
| `DIBBS_BASE_URL`       | `https://www.dibbs.bsm.dla.mil`                      | Used by the (stubbed) live scraper           |
| `DIBBS_SCRAPE_DELAY`   | `2.0`                                                | Seconds between scraper requests             |
| `DIBBS_USER_AGENT`     | `dibbs-bot/0.1 (local research tool)`                | UA header for outbound HTTP                  |
| `DIBBS_LOG_LEVEL`      | `INFO`                                               | Console + file log level                     |
| `DIBBS_LOG_FILE`       | `logs/dibbs-bot.log`                                 | Rotating-file destination                    |
| `SAM_GOV_API_KEY`      | *(blank)*                                            | Reserved for optional sam.gov integration    |

Secrets must come from `.env` or the shell environment. Never commit `.env`.

---

## Live DIBBS scraping

`scraper/fetch_rfqs.py` is a real (best-effort) scraper. It does **not** run
unless you invoke it — there are no background tasks or auto-fetching.

### What it handles

- **Click-through agreement.** On the first request it detects the "Logon
  Acknowledgement" page, POSTs the agreement form, and stores the resulting
  cookie in `data/dibbs_cookies.lwp` so subsequent runs skip the prompt.
- **ASP.NET WebForms postbacks.** Harvests `__VIEWSTATE`,
  `__VIEWSTATEGENERATOR`, `__EVENTVALIDATION`, etc. and replays them for
  paginated searches.
- **Polite rate-limiting.** Sleeps `DIBBS_SCRAPE_DELAY` seconds between
  requests (default 2.0s).
- **Observable.** Every fetched page is saved as raw HTML into
  `scraper/sample_data/` with a timestamped filename, so when a selector
  doesn't match you can inspect exactly what came back.

### Recommended daily workflow (uses the official DIBBS bulk download)

DIBBS publishes a fixed-width "daily index" file for each business day
covering every RFQ issued that day (typically ~2,500 solicitations per
file). One HTTP request → thousands of RFQs. This is the fastest, most
polite, and most complete way to ingest real data.

```bash
# Pull the last 7 daily index files and ingest them in one shot
python scraper/fetch_rfqs.py --last-n-days 7 --ingest
python analysis/score_opportunities.py
streamlit run app/dashboard.py
```

The files land in `scraper/sample_data/` as `in<YYMMDD>.txt` (e.g.
`in260601.txt` for 2026-06-01). The parser auto-detects the 140-char
fixed-width signature.

### CLI

```bash
# Daily-index downloads (preferred)
python scraper/fetch_rfqs.py --daily 2026-06-01 --ingest
python scraper/fetch_rfqs.py --last-n-days 7 --ingest

# HTML-scraping paths (useful for rich detail-page data)
python scraper/fetch_rfqs.py --recent              # listing of daily download files
python scraper/fetch_rfqs.py --by-issue-date       # /RFQ/RFQDates.aspx?category=issue
python scraper/fetch_rfqs.py --by-close-date       # /RFQ/RFQDates.aspx?category=close
python scraper/fetch_rfqs.py --solicitation SPE7L1-26-T-0001

# Useful flags
python scraper/fetch_rfqs.py --last-n-days 30 --dry-run    # see what would download
python scraper/fetch_rfqs.py --last-n-days 7 --delay 5     # be extra polite
python scraper/fetch_rfqs.py --recent --max-pages 3 --ingest
```

What the daily-index file contains (from DIBBS's own
[RfqFileDefs.aspx](https://www.dibbs.bsm.dla.mil/Rfq/RfqFileDefs.aspx)):
solicitation number, NSN (or manufacturer part #), purchase request #,
return-by date, file name, quantity, unit of issue, nomenclature, buyer
code, AMSC, item type, set-aside indicator (Y/H/R/L/A/E/N), and set-aside
percentage. **It does NOT contain approved-source CAGEs, MPNs (other than
the primary), or the technical-docs flag** — for those, follow up with
`--solicitation <NUMBER>` on the candidates you actually care about.

### Tunable selectors

The values most likely to drift over time are constants at the top of
`scraper/fetch_rfqs.py`:

| Constant                  | Purpose                                                                 |
| ------------------------- | ----------------------------------------------------------------------- |
| `AGREEMENT_PATH`          | Path of the click-through page                                          |
| `AGREEMENT_MARKERS`       | Text snippets that mean "you're on the agreement page"                  |
| `AGREEMENT_BUTTON_NAMES`  | Possible names of the "I Agree" submit input                            |
| `RECENT_RFQS_PATH`        | URL path for the recently-issued RFQs listing                           |
| `SEARCH_BY_NSN_PATH`      | URL path for NSN / solicitation search (also used for detail pages)     |
| `SEARCH_BY_FSC_PATH`      | URL path for FSC search                                                 |

If the live site doesn't match, change a string or two and rerun. The
parser in `scraper/parse_rfqs.py` is shape-tolerant — it walks every
`<table>` and either treats it as a results grid (multiple RFQs) or a
detail key/value layout (single RFQ), so the same code handles both
listing pages and detail pages.

### What to expect on first live run

1. The first GET will hit the agreement page; the session accepts it,
   saves the cookie, then retries your target URL automatically.
2. Each result row is saved as a separate `*.html` file in
   `scraper/sample_data/`. The parser dedupes by solicitation number on
   upsert, so re-running is safe.
3. If a selector is wrong, you'll see `WARNING` logs and the saved HTML
   will be right there for inspection. Tweak the constant, rerun.

### Optional: sam.gov source

A `SAM_GOV_API_KEY` setting is already plumbed through `utils.config`.
Build a sibling module under `scraper/` that calls sam.gov, normalizes
rows, and feeds them through `db.database.bulk_upsert_rfqs`.

---

## Required commands (must all work)

```bash
python db/database.py
python scraper/parse_rfqs.py
python analysis/score_opportunities.py
streamlit run app/dashboard.py
```

---

## Roadmap / suggested next steps

- Implement real DIBBS scraping in `scraper/fetch_rfqs.py`.
- Add a sam.gov source as an optional path (paid API, isolated behind
  `SAM_GOV_API_KEY`).
- Persist user notes and "watching" status per RFQ (new table).
- Track historical scores per RFQ over time (already supported by schema —
  add a "history" view to the dashboard).
- Plug in simple ML once you have a few months of win/loss data.

---

## License

Internal use. No license granted.
