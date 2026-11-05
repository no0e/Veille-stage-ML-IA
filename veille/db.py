"""SQLite storage: sources, collected offers and the user's status on each offer."""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    params TEXT NOT NULL DEFAULT '{}',
    tier TEXT NOT NULL DEFAULT 'B',
    french_intl INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    note TEXT NOT NULL DEFAULT '',
    last_run TEXT,
    last_status TEXT,
    last_count INTEGER NOT NULL DEFAULT 0,
    UNIQUE (type, params)
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    company TEXT NOT NULL,
    title TEXT NOT NULL,
    location TEXT,
    countries TEXT,
    region TEXT,
    url TEXT,
    description TEXT,
    posted_at TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    categories TEXT,
    match INTEGER,
    matched TEXT,
    missing TEXT,
    chance INTEGER,
    reasons TEXT,
    warnings TEXT
);
CREATE INDEX IF NOT EXISTS jobs_source ON jobs(source_id);
CREATE TABLE IF NOT EXISTS job_state (
    job_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS deleted_sources (
    type TEXT NOT NULL,
    params TEXT NOT NULL,
    name TEXT,
    deleted_at TEXT NOT NULL,
    PRIMARY KEY (type, params)
);
CREATE TABLE IF NOT EXISTS applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT UNIQUE,
    company TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT,
    location TEXT,
    applied_at TEXT,
    stage TEXT NOT NULL DEFAULT 'envoyee',
    next_step TEXT NOT NULL DEFAULT '',
    next_date TEXT,
    contact TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

STATUSES = ("nouveau", "interesse", "secondaire", "postule", "ecarte")
APPLICATION_STAGES = {
    "envoyee": "Candidature envoyée",
    "test": "Test / étude de cas",
    "entretien_rh": "Entretien RH",
    "entretien_tech": "Entretien technique",
    "entretien_final": "Entretien final",
    "offre": "Offre reçue",
    "acceptee": "Acceptée",
    "refusee": "Refusée",
    "sans_reponse": "Sans réponse",
    "desiste": "Désisté",
}
APPLICATION_FIELDS = ("company", "title", "url", "location", "applied_at", "stage", "next_step", "next_date",
                      "contact", "notes")
# Columns added after the first release, created on start-up when missing.
MIGRATIONS = {
    "sources": {"high_pay": "INTEGER NOT NULL DEFAULT 0", "startup": "INTEGER NOT NULL DEFAULT 0"},
    "jobs": {"company_stage": "TEXT", "company_size": "TEXT", "stipend": "TEXT", "start_date": "TEXT", "deadline": "TEXT",
             "company_tier": "TEXT", "liveness": "TEXT", "liveness_reason": "TEXT", "liveness_at": "TEXT"},
    "applications": {"followups": "INTEGER NOT NULL DEFAULT 0", "last_followup": "TEXT", "stage_at": "TEXT",
                     "cv_file": "TEXT", "lettre_file": "TEXT"},
}
JOB_DETAIL_FIELDS = ("company_stage", "company_size", "stipend", "start_date", "deadline", "company_tier")
SOURCE_FLAGS = ("high_pay", "startup")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            for table, columns in MIGRATIONS.items():
                existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
                for column, definition in columns.items():
                    if column not in existing:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # --- meta ---
    def get_meta(self, key: str, default=None):
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self.connect() as conn:
            conn.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                         (key, value))

    # --- sources ---
    def seed_sources(self, seeds: list, version: str) -> int:
        """Insert the bundled sources once per seed version, so deleted sources stay deleted."""
        if self.get_meta("seed_version") == version:
            return 0
        added = 0
        with self.connect() as conn:
            deleted = {(r["type"], r["params"]) for r in conn.execute("SELECT type, params FROM deleted_sources")}
            for s in seeds:
                params = json.dumps(s.get("params", {}), sort_keys=True)
                if (s["type"], params) in deleted:
                    continue
                if s.get("remplace"):
                    # A manual link that became an automatic source: convert the user's row in place, so its
                    # prestige, labels and on/off switch are kept.
                    old = (s["remplace"]["type"], json.dumps(s["remplace"].get("params", {}), sort_keys=True))
                    if old in deleted:
                        continue
                    exists = conn.execute("SELECT 1 FROM sources WHERE type = ? AND params = ?", (s["type"], params)).fetchone()
                    cur = conn.execute("UPDATE sources SET type = ?, params = ?, note = ?, last_run = NULL, last_status = NULL "
                                       "WHERE type = ? AND params = ?", (s["type"], params, s.get("note", ""), *old))
                    if cur.rowcount or exists:
                        continue
                cur = conn.execute(
                    "INSERT OR IGNORE INTO sources(name, type, params, tier, french_intl, enabled, note, high_pay, startup) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (s["name"], s["type"], params, s.get("tier", "B"), int(s.get("french_intl", False)),
                     int(s.get("enabled", True)), s.get("note", ""), int(s.get("high_pay", False)),
                     int(s.get("startup", False))),
                )
                added += cur.rowcount
                if not cur.rowcount:
                    # Flag sources the user already had; their own edits (tier, notes) are left alone.
                    for flag in SOURCE_FLAGS:
                        if s.get(flag):
                            conn.execute(f"UPDATE sources SET {flag} = 1 WHERE type = ? AND params = ?", (s["type"], params))
        self.set_meta("seed_version", version)
        return added

    def sources(self) -> list:
        with self.connect() as conn:
            rows = conn.execute("""
                SELECT s.*, (SELECT COUNT(*) FROM jobs j WHERE j.source_id = s.id AND j.active = 1) AS active_jobs
                FROM sources s ORDER BY s.type = 'link', s.name COLLATE NOCASE
            """).fetchall()
        return [self._source_dict(r) for r in rows]

    def source(self, source_id: int) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT *, 0 AS active_jobs FROM sources WHERE id = ?", (source_id,)).fetchone()
        return self._source_dict(row) if row else None

    @staticmethod
    def _source_dict(row) -> dict:
        d = dict(row)
        d["params"] = json.loads(d["params"])
        d["french_intl"] = bool(d["french_intl"])
        d["enabled"] = bool(d["enabled"])
        d["high_pay"] = bool(d.get("high_pay"))
        d["startup"] = bool(d.get("startup"))
        return d

    def add_source(self, name: str, source_type: str, params: dict, tier: str, french_intl: bool, note: str = "",
                   high_pay: bool = False, startup: bool = False) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO sources(name, type, params, tier, french_intl, note, high_pay, startup) VALUES (?,?,?,?,?,?,?,?)",
                (name, source_type, json.dumps(params, sort_keys=True), tier, int(french_intl), note, int(high_pay),
                 int(startup)),
            )
            return cur.lastrowid

    def update_source(self, source_id: int, **fields) -> None:
        allowed = {"name", "tier", "french_intl", "enabled", "note", "high_pay", "startup", "last_run", "last_status",
                   "last_count"}
        sets = {k: (int(v) if isinstance(v, bool) else v) for k, v in fields.items() if k in allowed}
        if not sets:
            return
        with self.connect() as conn:
            conn.execute(f"UPDATE sources SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ?",
                         (*sets.values(), source_id))

    def delete_source(self, source_id: int) -> None:
        """Delete a source and its offers, and remember it so a seed update never brings it back."""
        with self.connect() as conn:
            row = conn.execute("SELECT type, params, name FROM sources WHERE id = ?", (source_id,)).fetchone()
            if row:
                conn.execute("INSERT OR REPLACE INTO deleted_sources(type, params, name, deleted_at) VALUES (?,?,?,?)",
                             (row["type"], row["params"], row["name"], now()))
            conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))

    # --- jobs ---
    def upsert_jobs(self, source_id: int, jobs: list, run_started: str, deactivate_missing: bool = True) -> None:
        stamp = now()
        with self.connect() as conn:
            for j in jobs:
                conn.execute("""
                    INSERT INTO jobs(id, source_id, company, title, location, countries, region, url, description,
                                     posted_at, first_seen, last_seen, active, categories, match, matched, missing,
                                     chance, reasons, warnings, company_stage, company_size, stipend, start_date, deadline,
                                     company_tier)
                    VALUES (:id, :source_id, :company, :title, :location, :countries, :region, :url, :description,
                            :posted_at, :stamp, :stamp, 1, :categories, :match, :matched, :missing,
                            :chance, :reasons, :warnings, :company_stage, :company_size, :stipend, :start_date, :deadline,
                            :company_tier)
                    ON CONFLICT(id) DO UPDATE SET
                        company = excluded.company, title = excluded.title, location = excluded.location,
                        countries = excluded.countries, region = excluded.region, url = excluded.url,
                        description = excluded.description, posted_at = COALESCE(excluded.posted_at, jobs.posted_at),
                        last_seen = excluded.last_seen, active = 1, categories = excluded.categories,
                        match = excluded.match, matched = excluded.matched, missing = excluded.missing,
                        chance = excluded.chance, reasons = excluded.reasons, warnings = excluded.warnings,
                        company_stage = excluded.company_stage, company_size = excluded.company_size,
                        stipend = excluded.stipend, start_date = excluded.start_date, deadline = excluded.deadline,
                        company_tier = excluded.company_tier
                """, {**{k: None for k in JOB_DETAIL_FIELDS}, **j, "source_id": source_id, "stamp": stamp})
            if deactivate_missing:
                conn.execute("UPDATE jobs SET active = 0 WHERE source_id = ? AND last_seen < ?", (source_id, run_started))

    def jobs(self, include_inactive: bool = False) -> list:
        query = """
            SELECT j.*, s.name AS source_name, s.type AS source_type, COALESCE(j.company_tier, s.tier) AS tier,
                   s.french_intl, s.high_pay, s.startup, s.note AS source_note,
                   COALESCE(st.status, 'nouveau') AS status
            FROM jobs j JOIN sources s ON s.id = j.source_id
            LEFT JOIN job_state st ON st.job_id = j.id
        """
        if not include_inactive:
            query += " WHERE j.active = 1 AND s.enabled = 1"
        with self.connect() as conn:
            return [self._job_dict(r) for r in conn.execute(query).fetchall()]

    def job(self, job_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("""
                SELECT j.*, s.name AS source_name, s.type AS source_type, COALESCE(j.company_tier, s.tier) AS tier,
                   s.french_intl, s.high_pay, s.startup, s.note AS source_note,
                       COALESCE(st.status, 'nouveau') AS status
                FROM jobs j JOIN sources s ON s.id = j.source_id
                LEFT JOIN job_state st ON st.job_id = j.id WHERE j.id = ?
            """, (job_id,)).fetchone()
        return self._job_dict(row) if row else None

    @staticmethod
    def _job_dict(row) -> dict:
        d = dict(row)
        for key in ("countries", "categories", "matched", "missing", "reasons", "warnings"):
            d[key] = json.loads(d[key]) if d.get(key) else []
        d["french_intl"] = bool(d.get("french_intl"))
        d["high_pay"] = bool(d.get("high_pay"))
        d["startup"] = bool(d.get("startup"))
        return d

    def update_scores(self, job_id: str, fields: dict) -> None:
        with self.connect() as conn:
            conn.execute("""
                UPDATE jobs SET location = :location, countries = :countries, region = :region, categories = :categories, match = :match,
                    matched = :matched, missing = :missing, chance = :chance, reasons = :reasons, warnings = :warnings,
                    stipend = :stipend, start_date = :start_date, deadline = :deadline, company_tier = :company_tier
                WHERE id = :id
            """, {"stipend": None, "start_date": None, "deadline": None, "company_tier": None, **fields, "id": job_id})

    def update_scores_many(self, updates: list, deactivated: list) -> None:
        """Les nouveaux scores de nombreuses offres en une seule transaction (bien plus rapide sous Windows)."""
        with self.connect() as conn:
            conn.executemany("""
                UPDATE jobs SET location = :location, countries = :countries, region = :region, categories = :categories, match = :match,
                    matched = :matched, missing = :missing, chance = :chance, reasons = :reasons, warnings = :warnings,
                    stipend = :stipend, start_date = :start_date, deadline = :deadline, company_tier = :company_tier
                WHERE id = :id
            """, [{"stipend": None, "start_date": None, "deadline": None, "company_tier": None, **f, "id": job_id}
                  for job_id, f in updates])
            conn.executemany("UPDATE jobs SET active = 0 WHERE id = ?", [(job_id,) for job_id in deactivated])

    def deactivate(self, job_id: str) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE jobs SET active = 0 WHERE id = ?", (job_id,))

    def set_status(self, job_id: str, status: str) -> None:
        """Set the user's status on an offer; "postule" opens a row in the application tracker."""
        if status not in STATUSES:
            raise ValueError(f"statut inconnu : {status}")
        job = self.job(job_id)
        with self.connect() as conn:
            conn.execute("""
                INSERT INTO job_state(job_id, status, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET status = excluded.status, updated_at = excluded.updated_at
            """, (job_id, status, now()))
            if status == "postule" and job:
                stamp = now()
                conn.execute("""
                    INSERT OR IGNORE INTO applications(job_id, company, title, url, location, applied_at, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (job_id, job["company"], job["title"], job["url"], job["location"], stamp[:10], stamp, stamp))
            elif status != "postule":
                conn.execute("DELETE FROM applications WHERE job_id = ?", (job_id,))

    # --- application tracker ---
    def applications(self) -> list:
        with self.connect() as conn:
            rows = conn.execute("""
                SELECT a.*, COALESCE(j.company_tier, s.tier) AS tier, s.high_pay FROM applications a
                LEFT JOIN jobs j ON j.id = a.job_id LEFT JOIN sources s ON s.id = j.source_id
                ORDER BY COALESCE(a.applied_at, a.created_at) DESC
            """).fetchall()
        return [dict(r) | {"high_pay": bool(r["high_pay"])} for r in rows]

    def add_application(self, fields: dict) -> int:
        """Track an application made outside the offer list (for example on a manual source)."""
        stamp = now()
        values = {k: fields.get(k) for k in APPLICATION_FIELDS}
        values["stage"] = values["stage"] if values["stage"] in APPLICATION_STAGES else "envoyee"
        values["applied_at"] = values["applied_at"] or stamp[:10]
        for key in ("next_step", "contact", "notes"):
            values[key] = values[key] or ""
        with self.connect() as conn:
            cur = conn.execute(
                f"INSERT INTO applications({', '.join(APPLICATION_FIELDS)}, created_at, updated_at) "
                f"VALUES ({', '.join('?' * len(APPLICATION_FIELDS))}, ?, ?)",
                (*values.values(), stamp, stamp),
            )
            return cur.lastrowid

    def update_application(self, app_id: int, fields: dict) -> None:
        sets = {k: v for k, v in fields.items() if k in APPLICATION_FIELDS}
        if "stage" in sets and sets["stage"] not in APPLICATION_STAGES:
            raise ValueError(f"étape inconnue : {sets['stage']}")
        if not sets:
            return
        if "stage" in sets:
            sets["stage_at"] = now()[:10]
        with self.connect() as conn:
            conn.execute(f"UPDATE applications SET {', '.join(f'{k} = ?' for k in sets)}, updated_at = ? WHERE id = ?",
                         (*sets.values(), now(), app_id))

    def record_followup(self, app_id: int, undo: bool = False) -> None:
        """A follow-up message was sent today (or the last one is cancelled)."""
        with self.connect() as conn:
            if undo:
                conn.execute("UPDATE applications SET followups = MAX(followups - 1, 0), updated_at = ? WHERE id = ?",
                             (now(), app_id))
            else:
                conn.execute("UPDATE applications SET followups = followups + 1, last_followup = ?, updated_at = ? "
                             "WHERE id = ?", (now()[:10], now(), app_id))

    def set_application_file(self, app_id: int, kind: str, filename: str | None) -> None:
        """Record the CV or cover letter file attached to an application (kind: "cv" or "lettre")."""
        if kind not in ("cv", "lettre"):
            raise ValueError(f"type de fichier inconnu : {kind}")
        with self.connect() as conn:
            conn.execute(f"UPDATE applications SET {kind}_file = ?, updated_at = ? WHERE id = ?", (filename, now(), app_id))

    def application(self, app_id: int) -> dict | None:
        return next((a for a in self.applications() if a["id"] == app_id), None)

    # --- liveness ---
    def set_liveness(self, job_id: str, verdict: dict, deactivate: bool = False) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE jobs SET liveness = ?, liveness_reason = ?, liveness_at = ? WHERE id = ?",
                         (verdict["result"], verdict["reason"], now(), job_id))
            if deactivate:
                conn.execute("UPDATE jobs SET active = 0 WHERE id = ?", (job_id,))

    def all_postings(self) -> list:
        """Every offer ever collected, active or not (to spot republished postings)."""
        with self.connect() as conn:
            rows = conn.execute("SELECT id, company, title, first_seen FROM jobs").fetchall()
        return [dict(r) for r in rows]

    def delete_application(self, app_id: int) -> None:
        with self.connect() as conn:
            row = conn.execute("SELECT job_id FROM applications WHERE id = ?", (app_id,)).fetchone()
            conn.execute("DELETE FROM applications WHERE id = ?", (app_id,))
            if row and row["job_id"]:
                conn.execute("UPDATE job_state SET status = 'interesse', updated_at = ? WHERE job_id = ?",
                             (now(), row["job_id"]))
