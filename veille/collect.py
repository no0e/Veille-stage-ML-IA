"""Run every enabled source, score the offers and store them."""
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import fetchers, geo, scoring
from .geo import company_key
from .db import Database, now
from .providers import AGGREGATORS

MAX_WORKERS = 6


def employer_key(name: str) -> str:
    """"Google / Google DeepMind" and "Naver Labs Europe (stages)" are matched on their first name."""
    return company_key(re.split(r" / | \(", name or "")[0])


def known_employers(sources: list) -> dict:
    """Prestige of each employer that has its own source, to rate its offers found on job boards."""
    return {employer_key(s["name"]): s["tier"] for s in sources if s["type"] not in AGGREGATORS}


def is_excluded(company: str, profile: dict) -> bool:
    """Companies the user never wants to see (useful on job boards, where a source cannot be deleted)."""
    key = company_key(company)
    return bool(key) and any(company_key(name) == key for name in profile["reglages"].get("entreprises_exclues", []))


def score_offer(raw: dict, source: dict, profile: dict, known: dict | None = None) -> dict | None:
    """Return the stored representation of an offer, or None when it is not ML / DL / Stats."""
    if not scoring.is_candidate_title(raw["title"]):
        return None
    if profile["reglages"].get("exclure_finance_de_marche") and scoring.is_market_finance_title(raw["title"]):
        return None
    company = raw.get("company") or source["name"]
    if is_excluded(company, profile):
        return None
    # Offers the profile can never get (PhD only, other nationality, US work permit, other promo...).
    if profile["reglages"].get("masquer_inaccessibles") and scoring.inaccessible_reason(
            raw["title"], raw.get("description", ""), int(profile.get("annee_diplome", 2027))):
        return None
    # On a job board, an employer the user follows elsewhere keeps the prestige set on its own source.
    company_tier = (known or {}).get(employer_key(company)) if source["type"] in AGGREGATORS else None
    location = geo.clean_location(raw.get("location", ""))
    countries = geo.detect_countries(location)
    region = geo.region_for(location, countries)
    details = scoring.extract_details(raw["title"], raw.get("description", ""), raw.get("posted_at"))
    result = scoring.assess(raw["title"], raw.get("description", ""), countries, region, company_tier or source["tier"],
                            profile, start_date=details["start_date"])
    if not result.categories:
        return None
    return {
        "id": f"{source['id']}:{raw['ext_id']}",
        # Portfolio boards (Getro) and job boards list many companies under one source.
        "company": company,
        "company_tier": company_tier,
        "company_stage": raw.get("company_stage") or None,
        "company_size": raw.get("company_size") or None,
        "stipend": raw.get("stipend") or details["stipend"],
        "start_date": details["start_date"],
        "deadline": raw.get("deadline") or details["deadline"],
        "title": raw["title"].strip(),
        "location": location,
        "url": raw.get("url"),
        "description": raw.get("description", ""),
        "posted_at": raw.get("posted_at"),
        "countries": json.dumps(sorted(countries)),
        "region": region,
        "categories": json.dumps(result.categories),
        "match": result.match,
        "matched": json.dumps(result.matched),
        "missing": json.dumps(result.missing),
        "chance": result.chance,
        "reasons": json.dumps(result.reasons, ensure_ascii=False),
        "warnings": json.dumps(result.warnings, ensure_ascii=False),
    }


class Collector:
    """Refreshes sources in a background thread and exposes progress for the UI."""

    def __init__(self, db: Database, profile_loader):
        self.db = db
        self.profile_loader = profile_loader
        self.lock = threading.Lock()
        self._started = {}  # source id -> timestamp when its fetch began
        self.progress = {"running": False, "done": 0, "total": 0, "current": [], "errors": [],
                         "started_at": None, "finished_at": None, "new_offers": 0}

    def start(self, source_ids: list | None = None) -> bool:
        with self.lock:
            if self.progress["running"]:
                return False
            self.progress.update(running=True, done=0, total=0, current=[], errors=[],
                                 started_at=now(), finished_at=None, new_offers=0)
        threading.Thread(target=self._run, args=(source_ids,), daemon=True).start()
        return True

    def _run(self, source_ids):
        profile = self.profile_loader()
        all_sources = self.db.sources()
        known = known_employers(all_sources)
        sources = [s for s in all_sources if s["enabled"] and s["type"] != "link"
                   and (source_ids is None or s["id"] in source_ids)]
        known_ids = {j["id"] for j in self.db.jobs(include_inactive=True)}
        self.progress["total"] = len(sources)
        try:
            with ThreadPoolExecutor(MAX_WORKERS) as pool:
                futures = {}
                for s in sources:
                    futures[pool.submit(self._fetch, s)] = s
                for future in as_completed(futures):
                    source = futures[future]
                    run_started = self._started.get(source["id"], now())
                    try:
                        raw_offers = future.result()
                        scored = [o for o in (score_offer(r, source, profile, known) for r in raw_offers) if o]
                        self.db.upsert_jobs(source["id"], scored, run_started)
                        self.progress["new_offers"] += sum(1 for o in scored if o["id"] not in known_ids)
                        self.db.update_source(source["id"], last_run=now(), last_status="ok", last_count=len(scored))
                    except Exception as e:  # noqa: BLE001 - one broken source must not stop the others
                        message = f"{type(e).__name__}: {str(e)[:160]}"
                        self.progress["errors"].append({"source": source["name"], "error": message})
                        self.db.update_source(source["id"], last_run=now(), last_status=message)
                    finally:
                        self.progress["done"] += 1
                        self.progress["current"] = [n for n in self.progress["current"] if n != source["name"]]
        finally:
            self.db.set_meta("last_refresh", now())
            self.progress.update(running=False, finished_at=now())

    def _fetch(self, source: dict) -> list:
        self._started[source["id"]] = now()
        self.progress["current"].append(source["name"])
        return fetchers.fetch(source["type"], source["params"])


def rescore_all(db: Database, profile: dict, only=None) -> int:
    """Recompute scores after a profile or tier change, without refetching anything.

    only : filtre facultatif (offre -> bool) pour ne recalculer que les offres concernées.
    """
    sources = {s["id"]: s for s in db.sources()}
    known = known_employers(list(sources.values()))
    updates, deactivated = [], []
    for job in db.jobs(include_inactive=True):
        source = sources.get(job["source_id"])
        if not source or (only is not None and not only(job)):
            continue
        raw = {"ext_id": job["id"].split(":", 1)[1], "title": job["title"], "location": job["location"] or "",
               "description": job["description"] or "", "url": job["url"], "posted_at": job["posted_at"],
               # Values that came from the source itself, not from the description, are kept as they are.
               "company": job["company"] if source["type"] in AGGREGATORS or job["company"] != source["name"] else None,
               "company_stage": job.get("company_stage"),
               "company_size": job.get("company_size"), "stipend": job.get("stipend"), "deadline": job.get("deadline")}
        scored = score_offer(raw, source, profile, known)
        if scored is None:
            if job.get("active", 1):
                deactivated.append(job["id"])
            continue
        updates.append((job["id"], scored))
    db.update_scores_many(updates, deactivated)
    return len(updates)
