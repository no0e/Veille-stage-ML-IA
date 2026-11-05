import json
from pathlib import Path

import pytest

from veille import geo, scoring
from veille.fetchers import _avature_detail, _match_patterns, html_to_text

PROFILE = json.loads((Path(__file__).resolve().parents[1] / "config" / "profile.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("title", [
    "Applied Scientist (Internship)", "Research Intern", "Stage - Data Scientist H/F", "Praktikum Machine Learning",
    "Master Thesis: Deep Learning for Radar", "Machine Learning Engineer Intern", "Stagiaire IA générative",
])
def test_internship_titles(title):
    assert scoring.looks_like_internship(title)


@pytest.mark.parametrize("title", [
    "Senior Machine Learning Engineer", "International Sales Manager", "Two-stage recommender lead",
    "Alternance - Data Scientist", "Internal Tools Engineer",
])
def test_non_internship_titles(title):
    assert not scoring.looks_like_internship(title)


def test_classify_categories():
    assert scoring.classify("Deep Learning Research Intern", "") [0] == "DL"
    assert "Stats" in scoring.classify("Quantitative Research Intern", "time series and bayesian statistics")
    assert scoring.classify("Marketing Intern", "social media and events") == []


@pytest.mark.parametrize("location, iso, region", [
    ("Paris, France", "FR", "Europe"),
    ("ES, Barcelona", "ES", "Europe"),
    ("Munich - London", "DE", "Europe"),
    ("North Reading, Massachusetts, USA", "US", "Monde"),
    ("Remote - EMEA", None, "Europe"),
    ("Zürich", "CH", "Europe"),
])
def test_geo(location, iso, region):
    countries = geo.detect_countries(location)
    if iso:
        assert iso in countries
    assert geo.region_for(location, countries) == region


def test_reading_is_not_uk():
    assert geo.detect_countries("North Reading, Massachusetts, USA") == {"US"}


def test_trailing_us_state():
    assert geo.detect_countries("Greenwich, CT") == {"US"}
    assert geo.detect_countries("Paris, IDF, France") == {"FR"}


def test_cambridge_ma_is_us():
    countries = geo.detect_countries("Cambridge, MA, United States")
    assert countries == {"US"} and geo.region_for("Cambridge, MA", countries) == "Monde"


@pytest.mark.parametrize("title", [
    "AI & Management Consulting Intern", "Stage - SEO : Contenu & recherche IA",
    "STAGE 2027 - Achats Stratégiques - Intégration de l'IA", "GTM Intern - New Products (AI x Greentech)",
    "AI Content Manager Intern", "Assistant Graphiste & Génération IA (Stage 2 mois)",
])
def test_business_titles_are_not_ml(title):
    assert scoring.classify(title, "machine learning deep learning AI LLM generative statistics") == []


def test_technical_title_with_business_word_is_kept():
    assert scoring.classify("Intern Applied AI Engineering / Strategic Consulting", "machine learning LLM") != []


def test_iowa_is_not_ai():
    assert scoring.classify("Summer 2027 Internship: Progress (Peosta, IA)", "manufacturing plant") == []
    assert "ML" in scoring.classify("Stage - Ingénieur IA générative", "")


def test_working_student_rejected():
    assert not scoring.is_candidate_title("Working Student B2C SEO & AI Search")
    assert scoring.is_candidate_title("Werkstudent / Praktikum Machine Learning")


def test_summer_and_class_year_penalties():
    base = scoring.assess("ML Research Intern 2027", "PyTorch, 6 months, master students", {"NL"}, "Europe", "A", PROFILE)
    summer = scoring.assess("ML Research Intern - Summer 2027", "PyTorch 12 weeks", {"NL"}, "Europe", "A", PROFILE)
    promo = scoring.assess("Applied Scientist Intern", "Class of 2029 students. PyTorch", {"NL"}, "Europe", "A", PROFILE)
    ok = scoring.assess("Applied Scientist Intern", "Students graduating between December 2026 and June 2027", {"NL"}, "Europe", "A", PROFILE)
    assert summer.chance < base.chance and "Stage d'été (court)" in summer.warnings
    assert "Promo 2029" in promo.warnings
    assert not any(w.startswith("Promo") for w in ok.warnings)
    wide = scoring.assess("Quant Research Off-Cycle", "graduating between September 2026 and March 2028", {"FR"}, "Europe", "A", PROFILE)
    assert not any(w.startswith("Promo") for w in wide.warnings)


@pytest.mark.parametrize("title", ["2027 Markets Summer Analyst Program - Research", "Quantitative Research - Off-Cycle - Analyst",
                                   "Technical Student Programme - Machine Learning"])
def test_bank_and_cern_internship_titles(title):
    assert scoring.looks_like_internship(title)


@pytest.mark.parametrize("title, finance", [
    ("Quantitative Research Intern (NLP)", True), ("Campus Quantitative Trader (Intern)", True),
    ("2027 Markets Summer Analyst Program", True), ("Intern - Quantitative Research", True),
    ("PhD Research Intern, Quantum Simulation and AI", False), ("2027 Spring Co-op Quantitative Pharmacology", False),
    ("Machine Learning Engineer Intern", False),
])
def test_market_finance_titles(title, finance):
    assert scoring.is_market_finance_title(title) is finance


def test_deleted_source_is_not_reseeded(tmp_path):
    from veille.db import Database
    db = Database(tmp_path / "t.db")
    seed = [{"name": "Fonds X", "type": "greenhouse", "params": {"token": "x"}, "tier": "S"}]
    db.seed_sources(seed, "v1")
    db.delete_source(db.sources()[0]["id"])
    db.seed_sources(seed, "v2")
    assert db.sources() == []


def test_teamtailor_detected_as_rss():
    kind, params = _match_patterns("https://deezer.teamtailor.com/jobs")
    assert kind == "rss" and params["url"] == "https://deezer.teamtailor.com/jobs.rss"


def test_linkedin_link_since_last_visit():
    from datetime import datetime, timedelta, timezone
    import app
    two_days_ago = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    url = app.since_last_visit_url("https://www.linkedin.com/jobs/search/?keywords=ml&f_TPR=r604800", two_days_ago)
    seconds = int(url.split("f_TPR=r")[1].split("&")[0])
    assert 2 * 86400 <= seconds <= 2 * 86400 + 3700 and url.count("f_TPR") == 1
    assert app.since_last_visit_url("https://jobs.vinci.com/en", two_days_ago) == "https://jobs.vinci.com/en"


def test_import_is_incremental(tmp_path):
    from veille.collect import score_offer
    from veille.db import Database, now
    db = Database(tmp_path / "t.db")
    db.seed_sources([{"name": "BNP Paribas", "type": "link", "params": {"url": "https://x"}, "tier": "B"}], "v1")
    source = db.sources()[0]
    def offer(i):
        raw = {"ext_id": str(i), "title": f"Stage Data Scientist {i}", "location": "Paris, France", "url": f"https://x/{i}",
               "description": "machine learning, python", "posted_at": None}
        return score_offer(raw, source, PROFILE)
    db.upsert_jobs(source["id"], [offer(1)], now(), deactivate_missing=False)
    db.upsert_jobs(source["id"], [offer(2)], now(), deactivate_missing=False)
    assert len(db.jobs()) == 2


def test_extract_details():
    d = scoring.extract_details("Data Sciences Intern - January 2027", "Gratification : 1 500 € brut par mois. "
                                "Application deadline: 15 November 2026.")
    assert d == {"stipend": "1 500 €/mois", "start_date": "janv. 2027", "deadline": "2026-11-15"}
    d = scoring.extract_details("Stage", "Début du stage : mars 2027. Date limite de candidature : 30/11/2026")
    assert d["start_date"] == "mars 2027" and d["deadline"] == "2026-11-30" and d["stipend"] is None
    d = scoring.extract_details("Traineeship", "Place of work\n\nFrankfurt am Main, Germany\n\nClosing date\n\n20.10.2026")
    assert d["deadline"] == "2026-10-20"


def test_avature_place_of_work_fallback():
    page = "<html><body><p>Place of work</p><p>Frankfurt am Main, Germany</p><p>Closing date</p><p>20.10.2026</p></body></html>"
    location, description = _avature_detail(page)
    assert location == "Frankfurt am Main, Germany" and "Closing date" in description


def test_merge_duplicates():
    import app
    base = {"countries": ["FR"], "also": [], "url": "u"}
    views = [
        {**base, "id": "1", "company": "Shift Technology", "title": "Data Scientist Internship (H/F)", "source_id": 1, "source_type": "getro", "source_name": "Fonds Accel", "also": []},
        {**base, "id": "2", "company": "Shift Technology", "title": "Data Scientist Internship", "source_id": 2, "source_type": "greenhouse", "source_name": "Shift Technology", "also": []},
        {**base, "id": "3", "company": "Shift Technology", "title": "Data Scientist Internship", "source_id": 2, "source_type": "greenhouse", "source_name": "Shift Technology", "also": [], "location": "Lyon"},
        # Same employer, same title, same place: a republished posting, shown once.
        {**base, "id": "4", "company": "Shift Technology", "title": "Data Scientist Internship", "source_id": 2, "source_type": "greenhouse", "source_name": "Shift Technology", "also": []},
    ]
    out = app.merge_duplicates(views)
    assert [v["id"] for v in out] == ["2", "3"]
    assert {a["source"] for a in out[0]["also"]} == {"Fonds Accel", "annonce identique"}


def test_division_is_not_vision():
    assert not scoring.title_maybe_relevant("Traineeship in the Central Procurement Division")


def test_match_counts_missing_skills():
    score, matched, missing = scoring.match_score("PyTorch, deep learning, NLP, Python, C++, CUDA, Kubernetes", PROFILE["competences"])
    assert {"PyTorch", "Deep learning", "NLP", "Python"} <= set(matched)
    assert {"C++", "CUDA", "Kubernetes"} <= set(missing)
    assert 50 < score < 100


def test_phd_only_and_language_lower_chance():
    base = scoring.assess("Machine Learning Intern", "PyTorch deep learning master students 6 months", {"FR"}, "Europe", "B", PROFILE)
    phd = scoring.assess("Machine Learning Intern", "Currently enrolled in a PhD program. PyTorch deep learning", {"FR"}, "Europe", "B", PROFILE)
    german = scoring.assess("Machine Learning Intern", "PyTorch deep learning. Fluent German required.", {"DE"}, "Europe", "B", PROFILE)
    assert phd.chance < base.chance
    assert "Doctorat demandé" in phd.warnings
    assert german.chance < base.chance


def test_chance_capped_by_tier():
    elite = scoring.assess("Machine Learning Intern", "PyTorch deep learning NLP master 6 months 2027", {"FR"}, "Europe", "S", PROFILE)
    assert elite.chance <= scoring.MAX_CHANCE["S"]


def test_world_visibility():
    settings = PROFILE["reglages"]
    assert scoring.is_visible("Europe", "C", False, 5, settings)
    assert not scoring.is_visible("Monde", "B", False, 60, settings)
    assert scoring.is_visible("Monde", "S", False, 35, settings)
    assert scoring.is_visible("Monde", "B", True, 30, settings)


@pytest.mark.parametrize("url, kind, key, value", [
    ("https://jobs.ashbyhq.com/mistral.ai", "ashby", "token", "mistral.ai"),
    ("https://job-boards.greenhouse.io/helsing/jobs/123", "greenhouse", "token", "helsing"),
    ("https://jobs.lever.co/spotify", "lever", "token", "spotify"),
    ("https://criteo.wd3.myworkdayjobs.com/fr-FR/Criteo_Career_Site", "workday", "site", "Criteo_Career_Site"),
    ("https://apply.workable.com/huggingface/", "workable", "account", "huggingface"),
    ("https://careers.smartrecruiters.com/Ubisoft2", "smartrecruiters", "company", "Ubisoft2"),
    ("https://jobs.totalenergies.com/fr_FR/careers/SearchJobs/?search=data", "avature", "base",
     "https://jobs.totalenergies.com/fr_FR/careers"),
    ("https://talent.ecb.europa.eu/careers/SearchJobs", "avature", "base", "https://talent.ecb.europa.eu/careers"),
    ("https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/job/210787708", "oracle", "site", "CX_1001"),
])
def test_detect_patterns(url, kind, key, value):
    found_kind, params = _match_patterns(url)
    assert found_kind == kind and params[key] == value


def test_html_to_text():
    assert html_to_text("<p>Hello&nbsp;<b>world</b></p><ul><li>PyTorch</li></ul>") == "Hello world\n• PyTorch"
