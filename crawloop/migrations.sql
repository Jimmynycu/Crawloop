-- Registry schema (M5). Run once at Registry construction via executescript.
-- Plain stdlib sqlite3, no ORM, no migration framework — this single file is the
-- whole schema. Every column a method ever writes is bound with a `?` placeholder
-- in registry.py; SQL is NEVER built by string-formatting values in.

-- A page family = one template on one site (e.g. books.toscrape.com/product_list).
-- url_patterns is a JSON-encoded array of regex strings (the router uses it in M6);
-- it is stored as TEXT and (de)serialized in Python.
CREATE TABLE IF NOT EXISTS families (
    family       TEXT PRIMARY KEY,
    url_patterns TEXT,                       -- JSON array of regex strings
    schema_ref   TEXT,
    status       TEXT DEFAULT 'healthy',     -- healthy | degraded | regenerating | escalated
    created_at   TEXT
);

-- One immutable code artifact per (family, n). status forms the version ladder:
--   'active'   — the version tried first at runtime (at most one per family)
--   'fallback' — kept and still tried if the active one fails (older layouts)
--   'archived' — rollback-only, not tried at runtime
-- path points at the on-disk crawlers/<family_dir(family)>/v<n>.py file (the dir
-- is the readable slug plus a hash of the raw family name, so distinct families
-- never collide onto one file). source_sha is sha256 of the exact utf-8 source
-- written; Registry.load_crawler re-hashes the on-disk bytes and raises
-- IntegrityError on any mismatch, so post-registration tampering IS detected
-- (not merely detectable) at load time.
CREATE TABLE IF NOT EXISTS versions (
    family      TEXT,
    n           INTEGER,
    status      TEXT,                        -- active | fallback | archived
    path        TEXT,
    source_sha  TEXT,
    promoted_at TEXT,
    runs        INTEGER DEFAULT 0,
    successes   INTEGER DEFAULT 0,
    PRIMARY KEY (family, n),
    FOREIGN KEY (family) REFERENCES families (family)
);

-- Append-only audit trail. data is a JSON object (event-specific payload). The
-- registry also mirrors every row as one JSON line to crawlers/audit.jsonl.
CREATE TABLE IF NOT EXISTS audit (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     TEXT,
    family TEXT,
    event  TEXT,
    data   TEXT                              -- JSON object
);

-- Persistent access store (implements the access.AccessStore port): which fetch
-- strategy is known to work for a domain, and the domain's last seen status.
CREATE TABLE IF NOT EXISTS domain_access (
    domain           TEXT PRIMARY KEY,
    working_strategy TEXT,
    status           TEXT,
    updated_at       TEXT
);

-- Per-URL extraction history (used by the M6 executor / M7 history gate). Stored
-- here so the registry owns all persistence; extracted_json is a JSON blob.
CREATE TABLE IF NOT EXISTS run_history (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    family         TEXT,
    url            TEXT,
    version        INTEGER,
    extracted_json TEXT,                      -- JSON
    ts             TEXT
);
