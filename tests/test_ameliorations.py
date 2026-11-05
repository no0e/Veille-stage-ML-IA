"""Location clean-up, start-date cycle, priority score, Personio / Recruitee collectors, throttling."""
import json
from datetime import date
from pathlib import Path

import pytest

from veille import fetchers, geo, http, scoring

PROFILE = json.loads((Path(__file__).resolve().parents[1] / "config" / "profile.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("raw, clean", [
    ("Lille, France, Intern/Trainee (Fixed Term) (Trainee), 20 - SOFTWARE, Thales SIX GTS France S.A.S.", "Lille, France"),
    ("Gorgonzola, Italy, CW Intern/Trainee, 08 - QUALITY & CUSTOMER SATISFACTION", "Gorgonzola, Italy"),
    ("Bordeaux, France, Intern/Trainee (Fixed Term) (Trainee), 21 - ENGINEERING, Thales Dms France Sas", "Bordeaux, France"),
    ("Milano, Roma, Torino, Italy, Location Negotiable", "Milano, Roma, Torino, Italy"),
    ("Taiwan, Taipei, Taiwan", "Taiwan, Taipei"),
    ("Cambridge, MA, United States of America", "Cambridge, MA, United States of America"),
    ("Paris, IDF, France", "Paris, IDF, France"),
    ("", ""),
])
def test_clean_location(raw, clean):
    assert geo.clean_location(raw) == clean


def test_title_start_beats_stale_description():
    d = scoring.extract_details("Machine Learning Engineer Intern - Paris - January 2027",
                                "Intern for 6 months from July 2026.")
    assert d["start_date"] == "janv. 2027"


def test_month_without_year_uses_publication_date():
    d = scoring.extract_details("Data Scientist Internship (November, 6 months)", "", "2026-10-05")
    assert d["start_date"] == "nov. 2026"
    d = scoring.extract_details("ML Intern starting February", "", None, date(2026, 10, 20))
    assert d["start_date"] == "févr. 2027"
    # "Mars" is also a company: never read as a month without a year.
    assert scoring.extract_details("Stage Data - Mars Wrigley", "", None, date(2026, 10, 20))["start_date"] is None


@pytest.mark.parametrize("label, period", [
    ("janv. 2027", (2027, 1)), ("été 2027", (2027, 7)), ("hiver 2026", (2027, 1)), ("nov. 2026", (2026, 11)),
    (None, None), ("bientôt", None),
])
def test_start_period(label, period):
    assert scoring.start_period(label) == period


def _without_earliest(profile):
    return {**profile, "reglages": {k: v for k, v in profile.get("reglages", {}).items() if k != "debut_au_plus_tot"}}


def test_start_outside_graduation_year_lowers_chance():
    args = ("Data Scientist Intern", "PyTorch deep learning master 6 months", {"FR"}, "Europe", "B", _without_earliest(PROFILE))
    ok = scoring.assess(*args, start_date="févr. 2027")
    early = scoring.assess(*args, start_date="nov. 2026")
    winter = scoring.assess(*args, start_date="hiver 2027")  # North-American co-op term: Jan.-Apr. 2027
    assert early.chance == ok.chance - 20 and "Début nov. 2026" in early.warnings
    assert winter.chance == ok.chance and not any(w.startswith("Début") for w in winter.warnings)


def test_priority_orders_expected_value():
    today = date(2026, 10, 20)
    elite_long_shot, _ = scoring.priority("S", 12, 80, today=today)
    solid_good_fit, _ = scoring.priority("B", 70, 95, today=today)
    assert solid_good_fit > elite_long_shot
    assert scoring.priority("A", 50, 90, high_pay=True, today=today)[0] > scoring.priority("A", 50, 90, today=today)[0]


def test_priority_deadline():
    today = date(2026, 10, 20)
    normal, _ = scoring.priority("A", 40, 80, deadline="2026-12-31", today=today)
    urgent, reasons = scoring.priority("A", 40, 80, deadline="2026-10-27", today=today)
    late, late_reasons = scoring.priority("A", 40, 80, deadline="2026-10-01", today=today)
    assert urgent > normal and any("date limite dans 7 j" in r for r in reasons)
    assert late == 0 and "date limite dépassée" in late_reasons
    assert 0 <= scoring.priority("S", 45, 100, high_pay=True, today=today)[0] <= 100


def test_priority_labels():
    assert [scoring.priority_label(s) for s in (90, 60, 20)] == ["Haute", "Moyenne", "Basse"]


class FakeResponse:
    def __init__(self, payload=None, content=b""):
        self.payload, self.content = payload, content

    def json(self):
        return self.payload


PERSONIO_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<workzag-jobs>
  <position>
    <id>1234</id><office>Berlin</office>
    <additionalOffices><office>Munich</office></additionalOffices>
    <name>Machine Learning Intern (f/m/d)</name>
    <employmentType>intern</employmentType>
    <createdAt>2026-10-01T09:00:00+00:00</createdAt>
    <jobDescriptions>
      <jobDescription><name>Your tasks</name><value><![CDATA[<ul><li>Train PyTorch models</li></ul>]]></value></jobDescription>
    </jobDescriptions>
  </position>
  <position>
    <id>99</id><office>Berlin</office><name>Office Manager</name><employmentType>permanent</employmentType>
  </position>
  <position>
    <id>100</id><office>Berlin</office><name>Data Science</name><employmentType>intern</employmentType>
  </position>
</workzag-jobs>"""


def test_fetch_personio(monkeypatch):
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return FakeResponse(content=PERSONIO_XML)

    monkeypatch.setattr(http, "get", fake_get)
    jobs = fetchers.fetch_personio({"company": "acme", "domain": "jobs.personio.de"})
    assert calls[0][0] == "https://acme.jobs.personio.de/xml"
    assert [j["ext_id"] for j in jobs] == ["1234", "100"]  # the intern type rescues "Data Science"
    first = jobs[0]
    assert first["location"] == "Berlin, Munich" and first["url"] == "https://acme.jobs.personio.de/job/1234"
    assert "Train PyTorch models" in first["description"] and first["posted_at"] == "2026-10-01"


def test_fetch_recruitee(monkeypatch):
    payload = {"offers": [
        {"id": 7, "slug": "ml-intern", "title": "ML Research Intern", "status": "published",
         "location": "Amsterdam, Netherlands", "careers_url": "https://acme.recruitee.com/o/ml-intern",
         "description": "<p>Deep learning</p>", "requirements": "<p>Python</p>",
         "employment_type_code": "internship", "published_at": "2026-09-30 10:00:00 UTC"},
        {"id": 8, "slug": "sales", "title": "Sales Manager", "status": "published"},
        {"id": 9, "slug": "old", "title": "Data Science Intern", "status": "closed"},
    ]}
    monkeypatch.setattr(http, "get", lambda url, **kw: FakeResponse(payload))
    jobs = fetchers.fetch_recruitee({"company": "acme"})
    assert len(jobs) == 1
    job = jobs[0]
    assert job["ext_id"] == "7" and job["location"] == "Amsterdam, Netherlands"
    assert job["posted_at"] == "2026-09-30" and "Python" in job["description"]


@pytest.mark.parametrize("url, kind, params", [
    ("https://acme-gmbh.jobs.personio.de/job/123?language=en", "personio", {"company": "acme-gmbh", "domain": "jobs.personio.de"}),
    ("https://acme.jobs.personio.com/", "personio", {"company": "acme", "domain": "jobs.personio.com"}),
    ("https://Acme.recruitee.com/o/ml-intern", "recruitee", {"company": "acme"}),
])
def test_detect_personio_recruitee(url, kind, params):
    assert fetchers._match_patterns(url) == (kind, params)


def test_new_types_are_registered():
    for kind in ("personio", "recruitee"):
        assert kind in fetchers.SOURCE_TYPES and kind in fetchers.COLLECTORS


def test_throttle_spaces_workday_requests(monkeypatch):
    slept = []
    clock = [100.0]
    monkeypatch.setattr(http.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(http.time, "sleep", lambda s: slept.append(s))
    http._host_next.clear()
    for _ in range(3):
        http._throttle("https://acme.wd3.myworkdayjobs.com/wday/cxs/acme/site/jobs")
    http._throttle("https://boards-api.greenhouse.io/v1/boards/x/jobs")  # not throttled
    assert slept == [0.0, 1.0, 2.0]


def test_html_to_text_drops_empty_bullets():
    assert fetchers.html_to_text("<ul><li></li><li> </li><li>Python</li></ul>") == "• Python"


def test_start_before_earliest_month_is_negotiable_not_excluded():
    prof = {**PROFILE, "reglages": {**PROFILE.get("reglages", {}), "debut_au_plus_tot": "2027-03"}}
    args = ("Data Scientist Intern", "PyTorch deep learning master 6 months", {"FR"}, "Europe", "B", prof)
    march = scoring.assess(*args, start_date="mars 2027")
    feb = scoring.assess(*args, start_date="févr. 2027")
    jan = scoring.assess(*args, start_date="janv. 2027")
    winter = scoring.assess(*args, start_date="hiver 2027")
    unknown = scoring.assess(*args, start_date=None)
    assert feb.chance == march.chance - 10 and "Début févr. 2027 (avant mars)" in feb.warnings
    assert jan.chance == march.chance - 10
    assert winter.chance == march.chance - 10
    assert unknown.chance == march.chance and not any(w.startswith("Début") for w in unknown.warnings)
    # Without the setting, nothing changes.
    assert scoring.assess(*args[:5], _without_earliest(PROFILE), start_date="févr. 2027").chance == march.chance


@pytest.mark.parametrize("title, description, reason", [
    ("Research Scientist PhD Intern, 2027", "Deep learning research.", "doctorants"),
    ("Research Intern - ML", "Currently enrolled in a Doctorate Degree program in Computer Science.", "doctorants"),
    ("Stage - Thèse CIFRE en vision", "", "thèse"),
    ("Estonian National Graduate Trainee in AI", "", "ressortissants"),
    ("ML Intern", "Must be authorized to work in the United States.", "américaine"),
    ("Applied Scientist Intern", "Class of 2029 students only.", "promo 2029"),
    ("Data Scientist Intern", "You have 3+ years of professional experience.", "demandés"),
])
def test_inaccessible_offers_are_named(title, description, reason):
    assert reason in (scoring.inaccessible_reason(title, description) or "")


@pytest.mark.parametrize("title, description", [
    ("Data Processing Developer Technology Intern - 2027", "Currently pursuing a PhD or Master degree in Computer Science."),
    ("AI Developer Intern", "Pursuing MS or PhD from a leading University."),
    ("2027 Summer Intern, MS/PhD, Machine Learning", "Currently enrolled in a Master's or PhD program."),
    ("Student Researcher, BS/MS, 2027", "Research with Google."),
    ("Research Internship", "You must be pursuing a PhD. We will also consider exceptional non-PhD candidates."),
    ("Stage de fin d'études Data Science", "Élève en école d'ingénieurs, bac+5."),
    ("Machine Learning Intern", "PyTorch, 6 months, 2027."),
])
def test_accessible_offers_are_kept(title, description):
    assert scoring.inaccessible_reason(title, description) is None


def test_collect_hides_inaccessible_only_when_enabled():
    from veille.collect import score_offer
    raw = {"ext_id": "1", "title": "Research Scientist PhD Intern, 2027", "description": "Deep learning PyTorch research.",
           "location": "Paris, France", "url": "https://example.com/1"}
    source = {"id": 1, "name": "Example", "type": "greenhouse", "tier": "A"}
    on = {**PROFILE, "reglages": {**PROFILE["reglages"], "masquer_inaccessibles": True}}
    off = {**PROFILE, "reglages": {**PROFILE["reglages"], "masquer_inaccessibles": False}}
    assert score_offer(raw, source, on) is None
    assert score_offer(raw, source, off) is not None
