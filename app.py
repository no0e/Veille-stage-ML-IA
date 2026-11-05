"""Veille stages : serveur local (http://127.0.0.1:5057).

Trois pages : les offres collectées (regroupées par entreprise), les sources, et le suivi des candidatures
avec le CV et la lettre envoyés.
"""
import argparse
import hashlib
import json
import os
import re
import threading
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from flask import Flask, abort, jsonify, request, send_from_directory
from werkzeug.utils import secure_filename

from veille import fetchers, scoring
from veille.collect import AGGREGATORS, Collector, employer_key, rescore_all
from veille.db import APPLICATION_STAGES, Database, now
from veille.geo import company_key

BASE = Path(__file__).resolve().parent
CONFIG = BASE / "config"
FILES = BASE / "data" / "candidatures"  # CV et lettres envoyés, un dossier par candidature
PORT = 5057

app = Flask(__name__, static_folder=str(BASE / "static"), static_url_path="/static")
app.json.sort_keys = False
db = Database(BASE / "data" / "veille.db")


def load_profile() -> dict:
    return json.loads((CONFIG / "profile.json").read_text(encoding="utf-8"))


collector = Collector(db, load_profile)


def _parse(ts: str | None):
    if not ts:
        return None
    dt = datetime.fromisoformat(ts)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _title_key(title: str) -> str:
    text = re.sub(r"\((?:[hfmwdx]\s*/\s*)+[hfmwdx]\)|\b[hfmwdx](?:\s*/\s*[hfmwdx])+\b", " ", title.lower())
    return company_key(text)


def job_view(job: dict, settings: dict, full: bool = False) -> dict:
    first_seen = _parse(job["first_seen"])
    fresh_limit = datetime.now(timezone.utc) - timedelta(days=settings.get("nouveau_jours", 7))
    view = {k: job[k] for k in ("id", "company", "title", "location", "region", "url", "posted_at", "first_seen",
                                "categories", "tier", "status", "source_type", "source_id", "source_name",
                                "stipend", "start_date", "deadline", "countries")}
    description = job.get("description") or ""
    view.update(
        company_key=company_key(job["company"]) or job["company"].lower(),
        flags=scoring.country_flags(job["countries"]),
        tier_label=scoring.TIER_LABELS.get(job["tier"], job["tier"]),
        visible=scoring.is_visible(job["region"], job["tier"], job["french_intl"], job["chance"] or 0, settings),
        is_new=bool(first_seen and first_seen >= fresh_limit),
        warnings=[w for w in job["warnings"] if w.startswith(("Début", "Stage d'été", "Promo", "Doctorat"))],
        also=[],
        description=description if full else description[:300],
    )
    return view


SOURCE_PRIORITY = {"link": 2, "getro": 1, "wttj": 1, "thehub": 1, "arbeitnow": 1}  # site de l'entreprise d'abord


def merge_duplicates(views: list) -> list:
    """La même offre vue sur plusieurs sources (site de l'entreprise, job board) n'apparaît qu'une fois."""
    groups = {}
    for v in views:
        groups.setdefault((company_key(v["company"]), _title_key(v["title"]), tuple(v["countries"])), []).append(v)
    out = []
    for group in groups.values():
        group.sort(key=lambda v: SOURCE_PRIORITY.get(v["source_type"], 0))
        primary = group[0]
        out.append(primary)
        for other in group[1:]:
            if other["source_id"] != primary["source_id"]:
                primary["also"].append({"source": other["source_name"], "url": other["url"]})
            elif other.get("location") == primary.get("location") and other.get("status") == primary.get("status"):
                primary["also"].append({"source": "annonce identique", "url": other["url"]})
            else:
                out.append(other)  # plusieurs postes chez le même employeur
    return out


# --- pages ---------------------------------------------------------------------------------

@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


# --- offres --------------------------------------------------------------------------------

@app.get("/api/state")
def state():
    return jsonify(last_refresh=db.get_meta("last_refresh"), progress=collector.progress)


@app.get("/api/jobs")
def jobs():
    settings = load_profile()["reglages"]
    return jsonify(merge_duplicates([job_view(j, settings) for j in db.jobs()]))


@app.get("/api/job")
def job_detail():
    job = db.job(request.args.get("id", ""))
    if not job:
        abort(404)
    return jsonify(job_view(job, load_profile()["reglages"], full=True))


@app.post("/api/job/status")
def job_status():
    data = request.get_json(force=True)
    try:
        db.set_status(data["id"], data["status"])
    except ValueError as e:
        return jsonify(error=str(e)), 400
    return jsonify(ok=True)


@app.post("/api/exclude-company")
def exclude_company():
    """Masque (ou réaffiche) toutes les offres d'une entreprise ; enregistré dans config/profile.json."""
    data = request.get_json(force=True)
    company = (data.get("company") or "").strip()
    if not company:
        return jsonify(error="Entreprise manquante."), 400
    path = CONFIG / "profile.json"
    prof = json.loads(path.read_text(encoding="utf-8"))
    names = prof["reglages"].setdefault("entreprises_exclues", [])
    key = company_key(company)
    names[:] = [n for n in names if company_key(n) != key]
    if not data.get("undo"):
        names.append(company)
    path.write_text(json.dumps(prof, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    rescore_all(db, prof, only=lambda j: company_key(j["company"]) == key)  # seulement cette entreprise
    db.set_meta("scoring_signature", scoring_signature())  # rien d'autre à recalculer au prochain lancement
    if data.get("undo"):
        source_ids = sorted({j["source_id"] for j in db.jobs(include_inactive=True) if company_key(j["company"]) == key})
        if source_ids:
            collector.start(source_ids)
    return jsonify(excluded=names)


@app.post("/api/refresh")
def refresh():
    data = request.get_json(silent=True) or {}
    started = collector.start(data.get("source_ids"))
    return jsonify(started=started, progress=collector.progress)


# --- sources -------------------------------------------------------------------------------

def since_last_visit_url(url: str, last_visit: str | None) -> str:
    """Pour une recherche LinkedIn, n'afficher que les offres publiées depuis le dernier passage."""
    if "linkedin.com/jobs/search" not in url:
        return url
    last = _parse(last_visit)
    seconds = int((datetime.now(timezone.utc) - last).total_seconds()) if last else 7 * 86400
    seconds = max(3600, min(seconds + 3600, 30 * 86400))
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query) if k != "f_TPR"] + [("f_TPR", f"r{seconds}")]
    return urlunsplit(parts._replace(query=urlencode(query)))


@app.get("/api/sources")
def sources():
    items = db.sources()
    for s in items:
        if s["type"] == "link":
            s["open_url"] = since_last_visit_url(s["params"]["url"], s["last_run"])
    return jsonify(types=fetchers.SOURCE_TYPES, sources=items,
                   excluded=load_profile()["reglages"].get("entreprises_exclues", []))


@app.post("/api/sources/<int:source_id>/visited")
def source_visited(source_id):
    db.update_source(source_id, last_run=now(), last_status="vérifié")
    return jsonify(ok=True)


@app.post("/api/sources/detect")
def detect():
    data = request.get_json(force=True)
    try:
        result = fetchers.detect_source(data.get("text", ""))
    except ValueError as e:
        return jsonify(error=str(e)), 400
    return jsonify(result)


@app.post("/api/sources")
def add_source():
    data = request.get_json(force=True)
    if data.get("type") not in fetchers.SOURCE_TYPES or not data.get("name"):
        return jsonify(error="Nom et type de source obligatoires."), 400
    try:
        source_id = db.add_source(data["name"].strip(), data["type"], data.get("params") or {},
                                  data.get("tier", "B"), False, "", False, False)
    except Exception as e:  # noqa: BLE001 - source déjà présente
        return jsonify(error=f"Impossible d'ajouter la source ({e})."), 400
    if data["type"] != "link":
        collector.start([source_id])
    return jsonify(id=source_id)


@app.patch("/api/sources/<int:source_id>")
def update_source(source_id):
    data = request.get_json(force=True)
    db.update_source(source_id, **{k: v for k, v in data.items() if k in ("enabled", "tier")})
    if "tier" in data:
        # Seulement les offres de cette source, et celles du même employeur trouvées sur les job boards.
        aggregators = {s["id"] for s in db.sources() if s["type"] in AGGREGATORS}
        source = next((s for s in db.sources() if s["id"] == source_id), None)
        employer = employer_key(source["name"]) if source else None
        rescore_all(db, load_profile(), only=lambda j: j["source_id"] == source_id
                    or (j["source_id"] in aggregators and employer_key(j["company"]) == employer))
    return jsonify(ok=True)


@app.delete("/api/sources/<int:source_id>")
def delete_source(source_id):
    db.delete_source(source_id)
    return jsonify(ok=True)


# --- suivi des candidatures ----------------------------------------------------------------

def app_folder(app_id: int) -> Path:
    return FILES / str(app_id)


@app.get("/api/applications")
def applications():
    return jsonify(stages=APPLICATION_STAGES, applications=db.applications())


@app.post("/api/applications")
def add_application():
    data = request.get_json(force=True)
    if not (data.get("company") or "").strip() or not (data.get("title") or "").strip():
        return jsonify(error="Entreprise et intitulé obligatoires."), 400
    return jsonify(id=db.add_application(data))


@app.patch("/api/applications/<int:app_id>")
def update_application(app_id):
    try:
        db.update_application(app_id, request.get_json(force=True))
    except ValueError as e:
        return jsonify(error=str(e)), 400
    return jsonify(ok=True)


@app.delete("/api/applications/<int:app_id>")
def delete_application(app_id):
    db.delete_application(app_id)
    return jsonify(ok=True)


@app.post("/api/applications/<int:app_id>/file/<kind>")
def upload_file(app_id, kind):
    """Joint le CV ou la lettre envoyés : le fichier est copié dans data/candidatures/<id>/."""
    if kind not in ("cv", "lettre") or not db.application(app_id):
        abort(404)
    upload = request.files.get("file")
    if not upload or not upload.filename:
        return jsonify(error="Aucun fichier reçu."), 400
    folder = app_folder(app_id)
    folder.mkdir(parents=True, exist_ok=True)
    name = secure_filename(upload.filename) or f"{kind}.pdf"
    upload.save(folder / name)
    db.set_application_file(app_id, kind, name)
    return jsonify(file=name)


@app.delete("/api/applications/<int:app_id>/file/<kind>")
def remove_file(app_id, kind):
    """Retire le fichier de la candidature (il reste dans le dossier, rien n'est effacé)."""
    if kind not in ("cv", "lettre"):
        abort(404)
    db.set_application_file(app_id, kind, None)
    return jsonify(ok=True)


@app.get("/fichiers/<int:app_id>/<path:filename>")
def application_file(app_id, filename):
    return send_from_directory(app_folder(app_id), filename)


@app.post("/api/applications/<int:app_id>/open-folder")
def open_folder(app_id):
    folder = app_folder(app_id)
    folder.mkdir(parents=True, exist_ok=True)
    if hasattr(os, "startfile"):
        os.startfile(folder)  # noqa: S606 - outil local, Windows
    return jsonify(ok=True, path=str(folder))


# --- démarrage -----------------------------------------------------------------------------

def scoring_signature() -> str:
    """Empreinte des règles de tri et des réglages : on ne recalcule tout que si elle a changé."""
    h = hashlib.sha256()
    for path in [BASE / "veille" / "scoring.py", BASE / "veille" / "collect.py", BASE / "veille" / "geo.py",
                 CONFIG / "profile.json"]:
        h.update(path.read_bytes())
    return h.hexdigest()


def startup(auto_refresh: bool = True) -> None:
    """Le serveur répond tout de suite ; le recalcul (long) et la collecte tournent en arrière-plan."""
    seeds = json.loads((CONFIG / "sources_seed.json").read_text(encoding="utf-8"))
    db.seed_sources(seeds["sources"], seeds["version"])

    def background():
        try:
            signature = scoring_signature()
            if db.get_meta("scoring_signature") != signature:
                rescore_all(db, load_profile())
                db.set_meta("scoring_signature", signature)
            hours = load_profile()["reglages"].get("rafraichissement_auto_heures", 12)
            last = _parse(db.get_meta("last_refresh"))
            if auto_refresh and hours and (last is None or datetime.now(timezone.utc) - last > timedelta(hours=hours)):
                collector.start()
        except Exception as e:  # noqa: BLE001 - ne jamais empêcher la page de s'ouvrir
            print(f"Recalcul au démarrage impossible : {e}")

    threading.Thread(target=background, daemon=True).start()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--no-refresh", action="store_true")
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()
    print(f"Veille stages : http://127.0.0.1:{args.port}  (laisse cette fenêtre ouverte)")
    startup(auto_refresh=not args.no_refresh)
    if not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(f"http://127.0.0.1:{args.port}")).start()
    app.run(host="127.0.0.1", port=args.port, debug=False, threaded=True)
