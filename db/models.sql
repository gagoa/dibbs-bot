-- dibbs-bot schema (SQLite)
--
-- Loaded by db/database.py via executescript(). Safe to re-run; everything
-- uses IF NOT EXISTS so an existing DB is preserved.

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- rfqs: one row per DIBBS solicitation we've ingested
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS rfqs (
    id                            INTEGER PRIMARY KEY AUTOINCREMENT,
    solicitation_number           TEXT    NOT NULL UNIQUE,
    nsn                           TEXT,
    fsc                           TEXT,
    item_name                     TEXT,
    quantity                      INTEGER,
    unit_of_issue                 TEXT,
    issue_date                    TEXT,   -- ISO 8601 date strings
    close_date                    TEXT,
    set_aside                     TEXT,
    purchase_request_number       TEXT,
    buyer                         TEXT,
    approved_source_cages         TEXT,   -- comma-separated CAGE codes
    manufacturer_part_numbers     TEXT,   -- comma-separated MPNs
    technical_documents_available INTEGER NOT NULL DEFAULT 0,  -- 0/1 bool
    url                           TEXT,
    raw_text                      TEXT,
    created_at                    TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at                    TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_rfqs_nsn        ON rfqs(nsn);
CREATE INDEX IF NOT EXISTS idx_rfqs_fsc        ON rfqs(fsc);
CREATE INDEX IF NOT EXISTS idx_rfqs_close_date ON rfqs(close_date);

-- Keep updated_at in sync automatically.
CREATE TRIGGER IF NOT EXISTS trg_rfqs_updated_at
AFTER UPDATE ON rfqs
FOR EACH ROW
BEGIN
    UPDATE rfqs SET updated_at = datetime('now') WHERE id = OLD.id;
END;

-- ---------------------------------------------------------------------------
-- opportunity_scores: scoring runs for each RFQ
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS opportunity_scores (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    rfq_id                INTEGER NOT NULL,
    score                 INTEGER NOT NULL,         -- 0..100
    margin_potential      INTEGER,                  -- 0..100
    competition_level     INTEGER,                  -- 0..100 (higher = more competition)
    sourcing_difficulty   INTEGER,                  -- 0..100 (higher = harder)
    urgency               INTEGER,                  -- 0..100 (higher = sooner)
    notes                 TEXT,
    created_at            TEXT    NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (rfq_id) REFERENCES rfqs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_scores_rfq_id ON opportunity_scores(rfq_id);
CREATE INDEX IF NOT EXISTS idx_scores_score  ON opportunity_scores(score);
