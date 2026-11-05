"""Collect every source without starting the web page; run each morning by the Windows scheduled task.

Usage: python collecte.py
"""
import json
import sys
from datetime import datetime
from pathlib import Path

from veille import liveness
from veille.collect import Collector, rescore_all
from veille.db import Database

BASE = Path(__file__).resolve().parent
LOG = BASE / "data" / "collecte.log"


def load_profile() -> dict:
    return json.loads((BASE / "config" / "profile.json").read_text(encoding="utf-8"))


CLOSED_CODES = {"http_gone", "expired_url", "expired_body", "listing_page"}


def check_imported(db: Database) -> int:
    """Offers imported by hand or by Claude never leave their source by themselves: check that their page is
    still open (career-ops liveness check) and hide the ones that say they are closed."""
    closed = 0
    for job in db.jobs():
        if job["source_type"] != "link":
            continue
        verdict = liveness.check(job.get("url") or "")
        is_closed = verdict.get("code") in CLOSED_CODES
        db.set_liveness(job["id"], verdict, deactivate=is_closed)
        closed += is_closed
    return closed


def main() -> int:
    db = Database(BASE / "data" / "veille.db")
    seeds = json.loads((BASE / "config" / "sources_seed.json").read_text(encoding="utf-8"))
    db.seed_sources(seeds["sources"], seeds["version"])
    collector = Collector(db, load_profile)
    collector.progress.update(running=True)
    collector._run(None)  # synchronous: the scheduled task waits for the end
    rescore_all(db, load_profile())
    closed = check_imported(db)
    progress = collector.progress
    line = (f"{datetime.now():%Y-%m-%d %H:%M} · {progress['done']} sources · {progress['new_offers']} nouvelle(s) offre(s)"
            f" · {len(progress['errors'])} erreur(s) · {closed} offre(s) importée(s) fermée(s)")
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as log:
        log.write(line + "\n")
        for error in progress["errors"]:
            log.write(f"    {error['source']}: {error['error']}\n")
    print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
