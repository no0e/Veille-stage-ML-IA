"""Collectors ported from career-ops (github.com/career-ops-hq/career-ops, MIT licence, providers/*.mjs).

career-ops only lists postings (title, link, place) and leaves the description to its AI evaluation. Here the
scoring needs the description, so every collector reads it for the titles that look relevant: from the list
itself when the platform returns it, otherwise from the posting page (schema.org JobPosting JSON-LD, which
also gives the publication date, the deadline, the place and sometimes the stipend).

Each collector returns the same raw offers as fetchers.py: ext_id, title, location, url, description,
posted_at, and optionally company, deadline, stipend.
"""
import html
import json
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import urlencode, urljoin, urlsplit

from . import http
from .fetchers import DETAIL_PAUSE, _iso, _iso_from_ms, html_to_text, page_text
from .scoring import title_maybe_relevant

INTERN_QUERIES = ("intern", "internship", "stage", "stagiaire", "praktikum", "thesis")
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/126.0 Safari/537.36")


def relevant(title: str, intern_flag: bool = False) -> bool:
    """A relevant internship title; when the platform itself says "internship", the title may omit it."""
    return title_maybe_relevant(title) or (intern_flag and title_maybe_relevant(f"intern {title}"))


# --- posting pages: schema.org JobPosting --------------------------------------------------------

_LD_RE = re.compile(r'<script\b[^>]*type\s*=\s*["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.S | re.I)
_PERIODS = {"MONTH": "mois", "YEAR": "an", "WEEK": "semaine", "DAY": "jour", "HOUR": "h"}


def _find_posting(node):
    if isinstance(node, list):
        for item in node:
            found = _find_posting(item)
            if found:
                return found
        return None
    if not isinstance(node, dict):
        return None
    kind = node.get("@type")
    if kind == "JobPosting" or (isinstance(kind, list) and "JobPosting" in kind):
        return node
    if "@graph" in node:
        return _find_posting(node["@graph"])
    return None


def job_posting(page: str) -> dict | None:
    """The first schema.org JobPosting embedded in a page, if any."""
    for block in _LD_RE.findall(page or ""):
        block = block.strip()
        for candidate in (block, html.unescape(block)):
            try:
                found = _find_posting(json.loads(candidate))
            except ValueError:
                continue
            if found:
                return found
    return None


def _place(location) -> str:
    places = location if isinstance(location, list) else [location]
    out = []
    for place in places:
        if not isinstance(place, dict):
            continue
        address = place.get("address") or {}
        if isinstance(address, str):
            out.append(address)
            continue
        country = address.get("addressCountry")
        if isinstance(country, dict):
            country = country.get("name")
        parts = [address.get("addressLocality"), address.get("addressRegion"), country]
        text = ", ".join(str(p).strip() for p in parts if p and str(p).strip())
        if text and text not in out:
            out.append(text)
    return " / ".join(out)


def _stipend(salary) -> str | None:
    if not isinstance(salary, dict):
        return None
    value = salary.get("value") if isinstance(salary.get("value"), dict) else {"value": salary.get("value")}
    amount = value.get("minValue") or value.get("value") or value.get("maxValue")
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return None
    if amount <= 0:
        return None
    period = _PERIODS.get(str(value.get("unitText") or salary.get("unitText") or "").upper(), "")
    currency = salary.get("currency") or ""
    text = f"{amount:,.0f}".replace(",", " ")
    return f"{text} {currency}{'/' + period if period else ''}".strip()


def posting_fields(posting: dict) -> dict:
    description = posting.get("description") or ""
    if isinstance(description, str) and "&lt;" in description:
        description = html.unescape(description)
    kind = posting.get("employmentType") or ""
    return {
        "description": html_to_text(description if isinstance(description, str) else ""),
        "posted_at": _iso(posting.get("datePosted")),
        "deadline": _iso(posting.get("validThrough")),
        "location": _place(posting.get("jobLocation")),
        "stipend": _stipend(posting.get("baseSalary")),
        "intern": "INTERN" in (" ".join(kind) if isinstance(kind, list) else str(kind)).upper(),
    }


def fetch_detail(url: str, **kwargs) -> dict:
    """Description and dates of one posting page: JSON-LD first, the page text otherwise."""
    page = http.get(url, retries=1, **kwargs).text
    posting = job_posting(page)
    if posting:
        fields = posting_fields(posting)
        if fields["description"]:
            return fields
    # schema.org microdata (SuccessFactors pages): the description starts at its itemprop.
    marks = [m.end() for m in re.finditer(r'itemprop="description"[^>]*>', page)]
    if marks:
        return {"description": page_text(page[marks[-1]:])[:15000]}
    return {"description": page_text(page)}


def enrich(jobs: list, **kwargs) -> list:
    """Fill the missing description, dates and place of each offer from its posting page."""
    for job in jobs:
        if job.get("description") and job.get("posted_at"):
            continue
        try:
            detail = fetch_detail(job["url"], **kwargs)
        except Exception:  # noqa: BLE001 - keep the listing row without its description
            continue
        # validThrough is not used as a deadline: most sites fill it with an automatic expiry (WTTJ: +90 days).
        for key in ("description", "posted_at", "location", "stipend"):
            if detail.get(key) and not job.get(key):
                job[key] = detail[key]
        time.sleep(DETAIL_PAUSE)
    return jobs


def _epoch(seconds) -> str | None:
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    if value > 1e11:  # milliseconds
        value /= 1000
    return datetime.fromtimestamp(value, tz=timezone.utc).date().isoformat()


def _short_date(raw: str | None, month_first: bool = False) -> str | None:
    """"4.7.26", "18.06.2026", "6/18/26" (month first in en_US) → ISO date."""
    if not raw:
        return None
    raw = raw.strip()
    if m := re.fullmatch(r"(\d{4})[./-](\d{1,2})[./-](\d{1,2})\.?", raw):
        year, month, day = m.groups()
    elif m := re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{2,4})", raw):
        day, month, year = m.groups()
    elif m := re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", raw):
        first, second, year = m.groups()
        month, day = (first, second) if month_first else (second, first)
    else:
        return None
    year = int(year) + (2000 if int(year) < 100 else 0)
    try:
        return datetime(year, int(month), int(day)).date().isoformat()
    except ValueError:
        return None


# --- Welcome to the Jungle ----------------------------------------------------------------------

WTTJ_SITE = "https://www.welcometothejungle.com"
WTTJ_ENV = f"{WTTJ_SITE}/api/env"
WTTJ_INDEX = "wttj_jobs_production_fr"
WTTJ_QUERIES = ("machine learning", "data scientist", "data science", "deep learning", "intelligence artificielle",
                "computer vision", "NLP", "LLM", "statistique", "recherche IA")
WTTJ_FILTERS = ("contract_type:internship AND (offices.country_code:FR OR offices.country_code:BE OR "
                "offices.country_code:CH OR offices.country_code:LU OR offices.country_code:GB OR "
                "offices.country_code:DE OR offices.country_code:NL OR offices.country_code:ES)")
WTTJ_ATTRIBUTES = ("objectID,name,slug,organization,offices,remote,published_at,published_at_timestamp,contract_type,"
                   "salary_minimum,salary_maximum,salary_period,salary_currency,summary,key_missions,profile,"
                   "contract_duration_minimum,contract_duration_maximum,start_date")


def wttj_keys(text: str) -> tuple:
    """Algolia application id and public search key from the /api/env payload (they rotate)."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("wttj : /api/env sans objet JSON")
    env = json.loads(text[start:end + 1])
    app_id = str(env.get("PUBLIC_ALGOLIA_APPLICATION_ID") or "").strip()
    key = str(env.get("PUBLIC_ALGOLIA_API_KEY_CLIENT") or "").strip()
    if not re.fullmatch(r"[A-Z0-9]{6,16}", app_id, re.I) or not 16 <= len(key) <= 500:
        raise ValueError("wttj : identifiants Algolia inattendus")
    return app_id, key


def _wttj_text(value) -> str:
    if isinstance(value, list):
        return "\n".join(_wttj_text(v) for v in value)
    return html_to_text(value) if isinstance(value, str) else ""


def wttj_offer(hit: dict) -> dict | None:
    title = (hit.get("name") or "").strip()
    slug = (hit.get("slug") or "").strip()
    org = hit.get("organization") or {}
    org_slug = (org.get("slug") or "").strip()
    if not (title and re.fullmatch(r"[\w-]+", slug) and re.fullmatch(r"[\w-]+", org_slug)):
        return None
    office = (hit.get("offices") or [{}])[0] or {}
    place = [office.get("city"), office.get("country")] + (["Remote"] if hit.get("remote") == "fulltime" else [])
    stipend = None
    if hit.get("salary_minimum") and hit.get("salary_period") in ("monthly", "month"):
        stipend = f"{int(hit['salary_minimum']):,} {hit.get('salary_currency') or 'EUR'}/mois".replace(",", " ")
    sections = [("", hit.get("summary")), ("Missions", hit.get("key_missions")), ("Profil recherché", hit.get("profile"))]
    description = "\n\n".join(f"{name}\n{_wttj_text(v)}".strip() for name, v in sections if _wttj_text(v))
    return {
        "ext_id": str(hit.get("objectID") or slug),
        "title": title,
        "company": (org.get("name") or "").strip() or None,
        "location": ", ".join(p for p in place if p),
        "url": f"{WTTJ_SITE}/fr/companies/{org_slug}/jobs/{slug}",
        "description": description,
        "posted_at": _epoch(hit.get("published_at_timestamp")) or _iso(hit.get("published_at")),
        "stipend": stipend,
        "company_size": str(org["nb_employees"]) if org.get("nb_employees") else None,
        "intern": hit.get("contract_type") == "internship",
    }


def fetch_wttj(p: dict) -> list:
    """Welcome to the Jungle: its own public Algolia search index, narrowed server-side to internships."""
    app_id, key = wttj_keys(http.get(WTTJ_ENV).text)
    url = f"https://{app_id}-dsn.algolia.net/1/indexes/{p.get('index', WTTJ_INDEX)}/query"
    headers = {"x-algolia-application-id": app_id, "x-algolia-api-key": key, "Referer": f"{WTTJ_SITE}/",
               "Origin": WTTJ_SITE, "Content-Type": "application/json"}
    filters = p.get("filters", WTTJ_FILTERS)
    found = {}
    for query in p.get("queries") or WTTJ_QUERIES:
        params = {"query": query, "hitsPerPage": min(int(p.get("max_hits", 500)), 1000),
                  "attributesToRetrieve": WTTJ_ATTRIBUTES}
        if filters:
            params["filters"] = filters
        data = http.post(url, headers=headers, json={"params": urlencode(params)}).json()
        for hit in data.get("hits", []):
            offer = wttj_offer(hit)
            if offer and offer["url"] not in found and relevant(offer["title"], offer.pop("intern")):
                found[offer["url"]] = offer
    return enrich(list(found.values()))


# --- Google Careers ------------------------------------------------------------------------------

GOOGLE_BASE = "https://www.google.com/about/careers/applications/jobs/results/"
GOOGLE_QUERIES = ("machine learning", "research", "data scientist", "software engineering intern", "student researcher")
_G_ANCHOR = re.compile(r'<a\b[^>]*?\bhref="(?:[^"]*/)?jobs/results/(\d+)-([a-z0-9-]+)[^"]*"[^>]*>')
_G_H3 = re.compile(r"<h3\b[^>]*>([^<]+)</h3>")
_G_EMPLOYER = re.compile(r'<p class="l103df">(.*?)</p>', re.S)
_G_ARIA = re.compile(r'\baria-label="Learn more about ([^"]+)"')


def google_cards(page: str) -> list:
    """One card per posting; the employer line is "Google | Paris, France; Zurich, Switzerland; +2 more"."""
    cards, previous_end = [], 0
    for anchor in _G_ANCHOR.finditer(page):
        block = page[previous_end:anchor.start()]
        previous_end = anchor.end()
        h3 = _G_H3.findall(block)
        aria = _G_ARIA.search(anchor.group(0))
        title = html_to_text(h3[-1]) if h3 else html_to_text(aria[1]) if aria else anchor[2].replace("-", " ").title()
        employer, places = "", []
        lines = _G_EMPLOYER.findall(block)
        if lines and "|" in lines[-1]:
            employer, rest = lines[-1].split("|", 1)
            employer = html_to_text(employer)
            places = [x.strip() for x in html_to_text(rest).split(";") if x.strip() and not re.fullmatch(r"\+\d+ more", x.strip())]
        cards.append({"id": anchor[1], "slug": anchor[2], "title": title, "employer": employer, "locations": places})
    return cards


def fetch_google(p: dict) -> list:
    """Google Careers is server-rendered; only the first page of each search is read (robots.txt forbids the others)."""
    target = p.get("target_level", "INTERN_AND_APPRENTICE")
    found = {}
    for query in p.get("queries") or GOOGLE_QUERIES:
        params = {"q": query, "sort_by": "date"}
        if target:
            params["target_level"] = target
        if p.get("location"):
            params["location"] = p["location"]
        for card in google_cards(http.get(f"{GOOGLE_BASE}?{urlencode(params)}").text):
            if card["id"] in found or not relevant(card["title"], bool(target and "INTERN" in target)):
                continue
            found[card["id"]] = {
                "ext_id": card["id"], "title": card["title"], "company": card["employer"] or None,
                "location": "; ".join(card["locations"]), "url": f"{GOOGLE_BASE}{card['id']}-{card['slug']}",
                "description": "", "posted_at": None,
            }
    for job in found.values():  # no JSON-LD on Google's pages: keep the text from the qualifications on
        try:
            text = page_text(http.get(job["url"], retries=1).text, limit=200000)
        except Exception:  # noqa: BLE001
            continue
        start = text.find("Minimum qualifications")
        job["description"] = text[start:start + 8000] if start >= 0 else text[:8000]
        time.sleep(DETAIL_PAUSE)
    return list(found.values())


# --- Dassault Systèmes (Exalead search behind 3ds.com/careers) ----------------------------------

DASSAULT_API = "https://www.3ds.com/apisearch/card_search_api"
_DS_HIT = re.compile(r"<Hit\b.*?</Hit>", re.S)
_DS_META = re.compile(r'<Meta name="([^"]+)"[^>]*>\s*<MetaString[^>]*name="value"[^>]*>(.*?)</MetaString>', re.S)
_DS_LABELS = ("Category", "Type", "Country", "City", "Products", "Year", "Catégorie", "Pays", "Ville", "Produits", "Année")
_DS_LABEL_RE = re.compile(r"(^|\s)(" + "|".join(_DS_LABELS) + r")/")


def dassault_hits(xml_text: str) -> list:
    out = {}
    for hit in _DS_HIT.findall(xml_text or ""):
        meta = {}
        for name, value in _DS_META.findall(hit):
            meta.setdefault(name, html.unescape(value))
        title = html_to_text(meta.get("content_title", ""))
        url = meta.get("content_cta_1_url", "").strip()
        host = (urlsplit(url).hostname or "").lower()
        if not title or not (host == "3ds.com" or host.endswith(".3ds.com")):
            continue
        categories = meta.get("content_categories", "")
        marks = list(_DS_LABEL_RE.finditer(categories))
        city = country = ""
        for i, mark in enumerate(marks):
            end = marks[i + 1].start(2) if i + 1 < len(marks) else len(categories)
            value = categories[mark.end():end].strip()
            if mark[2] in ("City", "Ville") and not city:
                city = value
            elif mark[2] in ("Country", "Pays") and not country:
                country = value
        stamp = re.match(r"(\d{4})/(\d{2})/(\d{2})", meta.get("content_start_datetime") or meta.get("card_update_timestamp") or "")
        key = meta.get("card_id", "").strip() or url
        out.setdefault(key, {"ext_id": key, "title": title, "location": ", ".join(x for x in (city, country) if x),
                             "url": url, "description": "", "posted_at": "-".join(stamp.groups()) if stamp else None,
                             "intern": bool(re.search(r"Type/(?:Internship|Intern|Stage)", categories, re.I))})
    return list(out.values())


def fetch_dassault(p: dict) -> list:
    found = {}
    for page in range(int(p.get("max_pages", 90))):
        params = [("lang", "en"), ("r", "f/card_content_type/career"),
                  ("r", "f/card_content_categories_facet/cards language/en"), ("start", page * 10)]
        hits = dassault_hits(http.get(f"{DASSAULT_API}?{urlencode(params)}").text)
        fresh = [h for h in hits if h["ext_id"] not in found]
        if not fresh:
            break
        for h in fresh:
            found[h["ext_id"]] = h
    return enrich([h for h in found.values() if relevant(h["title"], h.pop("intern"))])


# --- SAP SuccessFactors career sites (Recruiting Marketing / Career Site Builder) ---------------

SF_PAGE_LIMIT = 20
_SF_TILE = re.compile(r'<li class="job-tile job-id-(\d+)\b.*?</li>', re.S)


def successfactors_tiles(page: str, origin: str) -> list:
    out = []
    for match in _SF_TILE.finditer(page or ""):
        block = match.group(0)
        path = re.search(r'data-url="([^"]+)"', block)
        title = re.search(r'class="jobTitle-link[^"]*"[^>]*>(.*?)</a>', block, re.S)
        if not path or not title or not html_to_text(title[1]):
            continue
        city = re.search(r'id="[^"]*-section-city-value">(.*?)</div>', block, re.S)
        link = html.unescape(path[1])
        out.append({"ext_id": match[1], "title": html_to_text(title[1]),
                    "location": html_to_text(city[1]) if city else "",
                    "url": link if link.startswith("http") else origin + ("" if link.startswith("/") else "/") + link,
                    "description": "", "posted_at": None})
    return out


def _sf_csb(base: str, query: str, locale: str) -> list:
    """Career Site Builder tenants load their jobs from a JSON API instead of the tile fragment."""
    out = []
    for page in range(SF_PAGE_LIMIT):
        data = http.post(f"{base}/services/recruiting/v1/jobs", headers={"Accept": "application/json"},
                         json={"keywords": query, "locale": locale, "location": "", "pageNumber": page, "sortBy": "date"}).json()
        rows = data.get("jobSearchResult") or []
        for row in rows:
            r = row.get("response") or {}
            title = html_to_text(str(r.get("unifiedStandardTitle") or r.get("jobTitle") or ""))
            if not r.get("id") or not title:
                continue
            slug = re.sub(r"[?#&]+", "-", html.unescape(str(r.get("unifiedUrlTitle") or r.get("urlTitle") or "job")))
            places = r.get("jobLocationShort") or []
            out.append({"ext_id": str(r["id"]), "title": title,
                        "location": " / ".join(dict.fromkeys(html_to_text(str(x)) for x in (places if isinstance(places, list) else [places]))),
                        "url": f"{base}/job/{slug}/{r['id']}-{locale}", "description": "",
                        "posted_at": _short_date(r.get("unifiedStandardStart"), month_first=locale in ("en_US", "es_US"))})
        if len(rows) < 10:
            break
    return out


def _sf_locale(base: str) -> str:
    """The search page links to every locale the site publishes in; French first, then English."""
    try:
        found = set(re.findall(r"locale=([a-z]{2}_[A-Z]{2})", http.get(f"{base}/search/", retries=1).text))
    except Exception:  # noqa: BLE001
        found = set()
    return next((loc for loc in ("fr_FR", "en_GB", "en_US", "de_DE") if loc in found), sorted(found)[0] if found else "en_US")


def fetch_successfactors(p: dict) -> list:
    """Branded SuccessFactors sites (jobs.engie.com, jobs.esa.int…): keyword search on the job tiles, or on the
    JSON API of the newer Career Site Builder sites when the tiles come back empty."""
    base = p["base"].rstrip("/")
    origin = "{0.scheme}://{0.netloc}".format(urlsplit(base))
    locale = p.get("locale") or _sf_locale(base)  # also opens the session (cookies) the JSON API wants
    csb = p.get("variant") == "csb"
    found = {}
    for query in p.get("queries") or INTERN_QUERIES:
        rows = []
        if not csb:
            start = 0
            for _ in range(SF_PAGE_LIMIT):
                params = {"q": query, "startrow": start, "locale": locale}
                fragment = http.get(f"{base}/tile-search-results/?{urlencode(params)}", headers={"Accept": "text/html"}).text
                if start == 0 and len(fragment.strip()) < 200:
                    csb = True  # an empty HTML shell (not "no result"): a Career Site Builder site
                    break
                tiles = successfactors_tiles(fragment, origin)
                known = {r["ext_id"] for r in rows}
                fresh = [t for t in tiles if t["ext_id"] not in known]
                if not fresh:
                    break
                rows += fresh
                start += len(tiles)
        if csb:
            rows = _sf_csb(base, query, locale)
        for row in rows:
            if row["ext_id"] not in found and relevant(row["title"]):
                found[row["ext_id"]] = row
    return enrich(list(found.values()))


# --- Oracle Taleo Enterprise career sections ----------------------------------------------------

_TALEO_URL = re.compile(r"https://([a-z0-9-]+\.taleo\.net)/careersection/([\w.-]+)/(?:jobsearch|joblist|jobdetail|moresearch)\.ftl[^\s\"'<>]*", re.I)


def taleo_headings(shell: str) -> list:
    table = re.search(r'<table\b[^>]*id=["\']jobs["\'][^>]*>.*?<thead\b[^>]*>(.*?)</thead>', shell, re.S | re.I)
    labels = [html_to_text(x) for x in re.findall(r"<(?:th|label)[^>]*>(.*?)</(?:th|label)>", table[1] if table else shell, re.S | re.I)]
    return [x for x in labels if x and not re.fullmatch(r"icons?|actions?", x, re.I)]


def _taleo_value(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (str, int, float)):
        text = str(value).strip()
        if text.startswith("["):
            try:
                return "; ".join(html_to_text(v) for v in json.loads(text) if isinstance(v, str))
            except ValueError:
                pass
        return html_to_text(text)
    if isinstance(value, list):
        return ", ".join(filter(None, (_taleo_value(v) for v in value)))
    if isinstance(value, dict):
        for key in ("value", "text", "label", "name", "displayValue"):
            if value.get(key) is not None:
                return _taleo_value(value[key])
    return ""


def taleo_rows(data: dict, host: str, section: str, lang: str, headings: list) -> list:
    def index(patterns, positional):
        for i, label in enumerate(headings):
            if any(re.search(p, label, re.I) for p in patterns):
                return i
        return positional if not headings else -1

    t_i = index([r"requisition\s*title", r"job\s*title", r"^title$", r"intitul", r"titre"], 0)
    l_i = index([r"location", r"city", r"lieu", r"localisation"], 1)
    d_i = index([r"posting\s*date", r"date\s*posted", r"posted", r"date de publication"], 2)
    out = []
    for row in data.get("requisitionList") or []:
        job_id = str(row.get("contestNo") or row.get("jobId") or "").strip()
        values = [_taleo_value(c) for c in row.get("column") or []]
        title = values[t_i] if 0 <= t_i < len(values) else _taleo_value(row.get("title"))
        if not job_id or not title:
            continue
        posted = values[d_i] if 0 <= d_i < len(values) else ""
        out.append({"ext_id": job_id, "title": title,
                    "location": values[l_i] if 0 <= l_i < len(values) else "",
                    "url": f"https://{host}/careersection/{section}/jobdetail.ftl?{urlencode({'job': job_id, 'lang': lang})}",
                    "description": "", "posted_at": _iso(posted) or _short_date(posted)})
    return out


def fetch_taleo(p: dict) -> list:
    m = _TALEO_URL.match(p["url"])
    if not m:
        raise ValueError("URL Taleo attendue : https://<entreprise>.taleo.net/careersection/<section>/jobsearch.ftl")
    host, section = m[1], m[2]
    lang = dict(x.split("=", 1) for x in urlsplit(p["url"]).query.split("&") if "=" in x).get("lang", "en")
    shell = http.get(p["url"], headers={"User-Agent": BROWSER_UA}).text
    portal = re.search(r"[?&]portal=(\d+)", shell) or re.search(r"[\"']portal(?:Id)?[\"']?\s*[:=]\s*[\"']?(\d+)", shell)
    if not portal:
        raise ValueError("section Taleo privée ou sans identifiant de portail public")
    headings = taleo_headings(shell)
    api = f"https://{host}/careersection/rest/jobboard/searchjobs?{urlencode({'lang': lang, 'portal': portal[1]})}"
    headers = {"User-Agent": BROWSER_UA, "Accept": "application/json, text/javascript, */*; q=0.01",
               "X-Requested-With": "XMLHttpRequest", "tz": "Europe/Paris", "tzname": "Europe/Paris"}
    found = {}
    for query in p.get("queries") or INTERN_QUERIES:
        for page in range(1, 11):
            body = {"multilineEnabled": False,
                    "sortingSelection": {"sortBySelectionParam": "5", "ascendingSortingOrder": "true"},
                    "fieldData": {"fields": {"KEYWORD": query, "LOCATION": ""}, "valid": True},
                    "filterSelectionParam": {"searchFilterSelections": []},
                    "advancedSearchFiltersSelectionParam": {"searchFilterSelections": []}, "pageNo": page}
            data = http.post(api, headers=headers, json=body).json()
            rows = taleo_rows(data, host, section, lang, headings)
            for row in rows:
                if relevant(row["title"]):
                    found.setdefault(row["ext_id"], row)
            total = (data.get("pagingData") or {}).get("totalCount")
            if not rows or (isinstance(total, int) and page * max(len(rows), 1) >= total):
                break
            time.sleep(0.2)
    return enrich(list(found.values()), headers={"User-Agent": BROWSER_UA})


# --- iCIMS hosted portals ------------------------------------------------------------------------

def icims_cards(page: str, origin: str) -> list:
    out = []
    for card in (page or "").split("iCIMS_JobCardItem")[1:]:
        href = re.search(r'href="([^"]*/jobs/\d+/[^"/]+/job[^"]*)"', card)
        title = re.search(r"<h3\b[^>]*>\s*(.*?)</h3>", card, re.S)
        if not href or not title or not title[1].strip():
            continue
        link = urljoin(origin + "/", html.unescape(href[1]))
        parts = urlsplit(link)
        if f"{parts.scheme}://{parts.netloc}" != origin:
            continue
        place = re.search(r'class=["\'][^"\']*field-label[^"\']*["\'][^>]*>\s*Location\s*</span>\s*<span\b[^>]*>\s*(.*?)</span>', card, re.S)
        job_id = re.search(r"/jobs/(\d+)/", parts.path)[1]
        out.append({"ext_id": job_id, "title": html_to_text(title[1]), "location": html_to_text(place[1]) if place else "",
                    "url": f"{origin}{parts.path}", "description": "", "posted_at": None})
    return out


def fetch_icims(p: dict) -> list:
    origin = p["origin"].rstrip("/")
    headers = {"User-Agent": BROWSER_UA, "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8"}
    found = {}
    for query in p.get("queries") or INTERN_QUERIES:
        first = None
        for page in range(10):
            params = {"ss": 1, "searchKeyword": query, "pr": page, "in_iframe": 1}
            cards = icims_cards(http.get(f"{origin}/jobs/search?{urlencode(params)}", headers=headers).text, origin)
            if not cards or cards[0]["url"] == first:
                break
            first = cards[0]["url"]
            for card in cards:
                if relevant(card["title"]):
                    found.setdefault(card["ext_id"], card)
            time.sleep(0.25)
    jobs = list(found.values())
    for job in jobs:  # the posting page carries the description and date only inside the iframe view
        try:
            detail = fetch_detail(f"{job['url']}?in_iframe=1", headers=headers)
        except Exception:  # noqa: BLE001
            continue
        for key in ("description", "posted_at", "deadline", "location", "stipend"):
            if detail.get(key) and not job.get(key):
                job[key] = detail[key]
        time.sleep(DETAIL_PAUSE)
    return jobs


# --- Eightfold AI career sites -------------------------------------------------------------------

def eightfold_positions(data: dict) -> tuple:
    """(positions, total) from the v2 API or, for migrated tenants, the PCSX API."""
    if isinstance(data.get("data"), dict):
        data = data["data"]
    return data.get("positions") or [], data.get("count")


def eightfold_offer(position: dict, host: str, domain: str | None) -> dict | None:
    title = (position.get("name") or position.get("posting_name") or "").strip()
    pid = position.get("id") or position.get("position_id")
    if not title or pid is None:
        return None
    url = position.get("canonicalPositionUrl") or position.get("publicUrl") or position.get("positionUrl") or ""
    if url.startswith("/"):
        url = f"https://{host}{url}"
    if not url.startswith("https://"):
        url = f"https://{host}/careers?{urlencode({'pid': pid, **({'domain': domain} if domain else {})})}"
    places = [position.get("location")] + list(position.get("locations") or [])
    kind = " ".join(str(position.get(k) or "") for k in ("type", "efcustomTextEmploymentType", "efcustomTextRoletype"))
    return {"ext_id": str(pid), "title": title, "location": " · ".join(dict.fromkeys(x for x in places if isinstance(x, str) and x)),
            "url": url, "description": html_to_text(position.get("job_description") or position.get("jobDescription") or ""),
            "posted_at": _epoch(position.get("t_create") or position.get("postedTs") or position.get("creationTs")
                                or position.get("t_update")),
            "intern": "intern" in kind.lower()}


def fetch_eightfold(p: dict) -> list:
    """Eightfold career sites (Microsoft, Bayer, Vodafone…): the v2 API, or PCSX for the tenants that moved to it."""
    host, domain = p["host"], p.get("domain")
    headers = {"User-Agent": BROWSER_UA, "Accept": "application/json"}
    scope = {"domain": domain} if domain else {}
    api = "v2"
    found = {}
    for query in p.get("queries") or ("intern", "internship", "stage", "thesis"):
        for page in range(15):
            params = {**scope, "query": query, "start": page * 10, **({"num": 10} if api == "v2" else {})}
            try:
                data = http.get(f"https://{host}/{'api/apply/v2/jobs' if api == 'v2' else 'api/pcsx/search'}?{urlencode(params)}",
                                headers=headers).json()
            except Exception as e:  # noqa: BLE001
                status = getattr(getattr(e, "response", None), "status_code", None)
                if api != "v2" or status != 403:
                    raise
                api = "pcsx"  # "Not authorized for PCSX": the tenant moved to the newer API
                params.pop("num", None)
                data = http.get(f"https://{host}/api/pcsx/search?{urlencode(params)}", headers=headers).json()
            positions, total = eightfold_positions(data)
            for position in positions:
                offer = eightfold_offer(position, host, domain)
                if offer and relevant(offer["title"], offer.pop("intern")):
                    found.setdefault(offer["ext_id"], offer)
            if len(positions) < 10 or (isinstance(total, int) and (page + 1) * 10 >= total):
                break
            time.sleep(0.4)
    for offer in found.values():
        if offer["description"]:
            continue
        for path in (f"api/apply/v2/jobs/{offer['ext_id']}?{urlencode(scope)}",
                     f"api/pcsx/position_details?{urlencode({'position_id': offer['ext_id'], **scope, 'hl': 'en'})}"):
            try:
                detail = http.get(f"https://{host}/{path}", headers=headers, retries=1).json()
            except Exception:  # noqa: BLE001
                continue
            detail = detail.get("data") if isinstance(detail.get("data"), dict) else detail
            offer["description"] = html_to_text(detail.get("job_description") or detail.get("jobDescription") or "")
            if offer["description"]:
                break
        time.sleep(DETAIL_PAUSE)
    return list(found.values())


# --- Cornerstone OnDemand (CSOD) career sites ---------------------------------------------------

def csod_requisitions(data: dict, origin: str, site_id: int, corp: str) -> list:
    out = []
    for r in (data.get("data") or {}).get("requisitions") or []:
        rid, title = str(r.get("requisitionId") or ""), html_to_text(str(r.get("displayJobTitle") or ""))
        if not rid or not title:
            continue
        places = []
        for loc in r.get("locations") or []:
            text = ", ".join(x for x in (str(loc.get("city") or "").strip(), str(loc.get("country") or "").strip()) if x)
            if text and text not in places:
                places.append(text)
        out.append({"ext_id": rid, "title": title, "location": " / ".join(places),
                    "url": f"{origin}/ux/ats/careersite/{site_id}/home/requisition/{rid}?{urlencode({'c': corp})}",
                    "description": html_to_text(r.get("externalDescription") or ""),
                    "posted_at": _short_date(r.get("postingEffectiveDate"), month_first=True)})
    return out


def fetch_csod(p: dict) -> list:
    """The career-site home page embeds an anonymous token for the public search API."""
    origin, site_id, corp = p["origin"].rstrip("/"), int(p["site_id"]), p["corp"]
    home = http.get(f"{origin}/ux/ats/careersite/{site_id}/home?{urlencode({'c': corp})}", headers={"Accept": "text/html"}).text
    token = re.search(r'"token"\s*:\s*"([A-Za-z0-9._-]+)"', home)
    if not token:
        raise ValueError("pas de jeton anonyme sur la page carrières Cornerstone")
    headers = {"Authorization": f"Bearer {token[1]}", "Accept": "application/json"}
    found = {}
    for query in p.get("queries") or INTERN_QUERIES:
        for page in range(1, 11):
            body = {"careerSiteId": site_id, "careerSitePageId": site_id, "pageNumber": page, "pageSize": 25,
                    "cultureId": p.get("culture_id", 1), "cultureName": p.get("culture", "en-US"), "searchText": query,
                    "states": [], "countryCodes": [], "cities": [], "placeID": "", "radius": None,
                    "postingsWithinDays": None, "customFieldCheckboxKeys": [], "customFieldDropdowns": [],
                    "customFieldRadios": []}
            data = http.post(f"{origin}/services/x/career-site/v1/search", headers=headers, json=body).json()
            rows = csod_requisitions(data, origin, site_id, corp)
            for row in rows:
                if relevant(row["title"]):
                    found.setdefault(row["ext_id"], row)
            if len(rows) < 25:
                break
    return enrich(list(found.values()))


# --- softgarden widgets --------------------------------------------------------------------------

def softgarden_rows(page: str, widget: str) -> list:
    out, seen = [], set()
    for block in (page or "").split('<div class="matchElement" id="job_id_')[1:]:
        job_id = re.match(r"(\d+)\"", block)
        link = re.search(r'<a href="([^"]*/job/[^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not job_id or not link or job_id[1] in seen or not html_to_text(link[2]):
            continue
        seen.add(job_id[1])
        cities = list(dict.fromkeys(html_to_text(c) for c in re.findall(r'class="location-view-item"[^>]*>(.*?)</span>', block, re.S)))
        date = re.search(r'class="matchValue date"[^>]*>(.*?)</div>', block, re.S)
        out.append({"ext_id": job_id[1], "title": html_to_text(link[2]), "location": " / ".join(c for c in cities if c),
                    "url": urljoin(widget, html.unescape(link[1])), "description": "",
                    "posted_at": _short_date(html_to_text(date[1]), month_first=True) if date else None})
    return out


def fetch_softgarden(p: dict) -> list:
    widget = p["widget"]
    rows = softgarden_rows(http.get(widget, headers={"Accept": "text/html"}).text, widget)
    return enrich([r for r in rows if relevant(r["title"])])


# --- JOIN (join.com) -----------------------------------------------------------------------------

def _next_data(page: str) -> dict:
    m = re.search(r"<script[^>]+__NEXT_DATA__[^>]*>(.*?)</script>", page or "", re.S)
    return json.loads(m[1]) if m else {}


def fetch_join(p: dict) -> list:
    base = f"https://join.com/companies/{p['slug']}"
    state = _next_data(http.get(base).text).get("props", {}).get("pageProps", {}).get("initialState") or {}
    if "jobs" not in state:
        raise ValueError("join.com : données de la page introuvables")
    items = list(state["jobs"].get("items") or [])
    pages = min(int((state["jobs"].get("pagination") or {}).get("pageCount") or 1), 20)
    for page in range(2, pages + 1):
        more = _next_data(http.get(f"{base}?page={page}").text).get("props", {}).get("pageProps", {}).get("initialState") or {}
        items += (more.get("jobs") or {}).get("items") or []
    company_slug = (state.get("company") or {}).get("domain") or p["slug"]
    jobs = []
    for item in items:
        title = (item.get("title") or "").strip()
        kind = str((item.get("employmentType") or {}).get("name") if isinstance(item.get("employmentType"), dict) else item.get("employmentType") or "")
        if not title or not relevant(title, "intern" in kind.lower()):
            continue
        jobs.append({"ext_id": str(item.get("idParam") or item.get("id")), "title": title,
                     "location": ((item.get("city") or {}).get("cityName") or ""),
                     "url": f"https://join.com/companies/{company_slug}/jobs/{item.get('idParam')}",
                     "description": "", "posted_at": _iso(item.get("createdAt"))})
    return enrich(jobs)


# --- BambooHR, Breezy HR, Pinpoint, Rippling, JibeApply, Jobvite --------------------------------

def fetch_bamboohr(p: dict) -> list:
    origin = f"https://{p['tenant']}.bamboohr.com"
    jobs = []
    for row in http.get(f"{origin}/careers/list", allow_redirects=False).json().get("result") or []:
        title = str(row.get("jobOpeningName") or "").strip()
        kind = str(row.get("employmentStatusLabel") or "")
        if not title or row.get("id") is None or not relevant(title, "intern" in kind.lower() or "stage" in kind.lower()):
            continue
        loc = row.get("location") or {}
        job = {"ext_id": str(row["id"]), "title": title, "url": f"{origin}/careers/{row['id']}", "description": "",
               "location": ", ".join(x for x in (loc.get("city"), loc.get("state"), loc.get("country"),
                                                  "Remote" if row.get("isRemote") else None) if x),
               "posted_at": None}
        try:
            opening = http.get(f"{origin}/careers/{row['id']}/detail", retries=1).json().get("result", {}).get("jobOpening") or {}
            job["description"] = html_to_text(opening.get("description") or "")
            job["posted_at"] = _iso(opening.get("datePosted"))
        except Exception:  # noqa: BLE001
            pass
        time.sleep(DETAIL_PAUSE)
        jobs.append(job)
    return jobs


def breezy_offer(row: dict) -> dict | None:
    title, url = str(row.get("name") or "").strip(), str(row.get("url") or "")
    if not title or not url.startswith("https://"):
        return None
    loc = row.get("location") or {}
    country = (loc.get("country") or {}).get("name") if isinstance(loc.get("country"), dict) else loc.get("country")
    place = loc.get("name") or ", ".join(x for x in (loc.get("city"), loc.get("state")) if x)
    if country and country.lower() not in str(place).lower():
        place = f"{place}, {country}" if place else country
    if loc.get("is_remote") and "remote" not in str(place).lower():
        place = f"{place}, Remote" if place else "Remote"
    kind = row.get("type") or {}
    kind = f"{kind.get('id', '')} {kind.get('name', '')}" if isinstance(kind, dict) else str(kind)
    return {"ext_id": str(row.get("id") or row.get("friendly_id") or url), "title": title, "location": place, "url": url,
            "description": "", "posted_at": _iso(row.get("published_date")), "intern": "intern" in kind.lower()}


def fetch_breezy(p: dict) -> list:
    rows = http.get(f"https://{p['tenant']}.breezy.hr/json").json()
    offers = [o for o in (breezy_offer(r) for r in rows if isinstance(r, dict)) if o]
    return enrich([o for o in offers if relevant(o["title"], o.pop("intern"))])


def pinpoint_offer(row: dict) -> dict | None:
    title, url = str(row.get("title") or "").strip(), str(row.get("url") or "").strip()
    description = row.get("description") or ""
    if not title or not url.startswith("https://") or "youtube.com/embed/https://www.youtube.com/embed/pFxm6fszrpw" in description:
        return None  # Pinpoint's demo posting, present on every new tenant
    loc = row.get("location") or {}
    parts = [description] + [row.get(k) or "" for k in ("key_responsibilities", "skills_knowledge_expertise", "benefits")]
    kind = str(row.get("employment_type") or row.get("employment_type_text") or "")
    return {"ext_id": str(row.get("id") or row.get("path") or url), "title": title,
            "location": loc.get("name") or ", ".join(x for x in (loc.get("city"), loc.get("province")) if x),
            "url": url, "description": "\n\n".join(html_to_text(x) for x in parts if x),
            "posted_at": _iso(row.get("created_at")),
            "deadline": _iso(row.get("deadline_at")), "intern": "intern" in kind.lower()}


def fetch_pinpoint(p: dict) -> list:
    rows = http.get(f"https://{p['slug']}.pinpointhq.com/postings.json").json().get("data") or []
    offers = [o for o in (pinpoint_offer(r) for r in rows if isinstance(r, dict)) if o]
    return [o for o in offers if relevant(o["title"], o.pop("intern"))]


def fetch_rippling(p: dict) -> list:
    jobs = []
    for page in range(5):
        data = http.get(f"https://ats.rippling.com/api/v2/board/{p['slug']}/jobs?{urlencode({'page': page, 'pageSize': 1000})}").json()
        items = data.get("items") or []
        for item in items:
            title, url = str(item.get("name") or "").strip(), str(item.get("url") or "")
            if not title or not url.startswith("https://ats.rippling.com/") or not relevant(title):
                continue
            jobs.append({"ext_id": str(item.get("id") or url), "title": title, "url": url, "description": "",
                         "location": " · ".join(l.get("name", "") for l in item.get("locations") or [] if l.get("name")),
                         "posted_at": None})
        if len(items) < 1000:
            break
    return enrich(jobs)


def fetch_jibeapply(p: dict) -> list:
    """Jibe (iCIMS) career sites answer /api/jobs with descriptions included."""
    origin = p["origin"].rstrip("/")
    found = {}
    for query in p.get("queries") or INTERN_QUERIES:
        for page in range(1, 6):
            data = http.get(f"{origin}/api/jobs?{urlencode({'keywords': query, 'page': page, 'limit': 100})}").json()
            rows = data.get("jobs") or []
            for row in rows:
                d = row.get("data") or row
                title = str(d.get("title") or "").strip()
                kind = " ".join(str(x) for x in (d.get("employment_type"), d.get("categories")) if x)
                if not title or not d.get("slug") or not relevant(title, "intern" in kind.lower()):
                    continue
                found.setdefault(str(d.get("req_id") or d["slug"]), {
                    "ext_id": str(d.get("req_id") or d["slug"]), "title": title,
                    "company": str(d.get("hiring_organization") or "").strip() or None,
                    "location": d.get("full_location") or ", ".join(x for x in (d.get("city"), d.get("country")) if x),
                    "url": f"{origin}{p.get('job_path', '/jobs/')}{d['slug']}",
                    "description": html_to_text(d.get("description") or ""),
                    "posted_at": _iso(d.get("posted_date") or d.get("create_date"))})
            if len(rows) < 100:
                break
    return enrich(list(found.values()))


def _tag(block: str, name: str) -> str:
    m = re.search(rf"<{name}>(.*?)</{name}>", block, re.S)
    value = (m[1] if m else "").strip()
    if value.startswith("<![CDATA[") and value.endswith("]]>"):
        value = value[9:-3].strip()
    return html.unescape(value)


def jobvite_jobs(xml_text: str) -> list:
    out = []
    for block in re.findall(r"<job>(.*?)</job>", xml_text or "", re.S):
        title = _tag(block, "title")
        url = (_tag(block, "detail-url") or _tag(block, "apply-url")).replace("http://", "https://", 1)
        if not title or not url.startswith("https://"):
            continue
        out.append({"ext_id": _tag(block, "id") or url, "title": title, "location": _tag(block, "location"), "url": url,
                    "description": html_to_text(_tag(block, "description")), "posted_at": _iso(_tag(block, "date")),
                    "intern": "intern" in _tag(block, "jobtype").lower()})
    return out


def fetch_jobvite(p: dict) -> list:
    eid = p.get("eid")
    if not eid:
        board = http.get(f"https://jobs.jobvite.com/{p['slug']}?fr=true&nl=1").text
        m = re.search(r"companyEId\s*[:=]\s*['\"]([A-Za-z0-9_-]{4,40})['\"]", board)
        if not m:
            raise ValueError("identifiant Jobvite (companyEId) introuvable sur la page")
        eid = m[1]
    xml_text = http.get(f"https://app.jobvite.com/CompanyJobs/Xml.aspx?{urlencode({'c': eid})}", timeout=45).text
    jobs = [j for j in jobvite_jobs(xml_text) if relevant(j["title"], j.pop("intern"))]
    return enrich(jobs)


# --- job boards: The Hub (Nordic / EU startups), Arbeitnow (EU / DACH), IBM ----------------------

def hub_offer(row: dict) -> dict | None:
    title, job_id = str(row.get("title") or "").strip(), row.get("id")
    if not title or not job_id or not re.fullmatch(r"[\w-]+", str(job_id)):
        return None
    loc = row.get("location") or {}
    place = loc.get("address") or ", ".join(x for x in (loc.get("locality"), loc.get("country")) if x)
    kinds = " ".join(str(x) for x in (row.get("jobPositionTypes") or row.get("positionTypes") or []))
    return {"ext_id": str(job_id), "title": title, "company": ((row.get("company") or {}).get("name") or "").strip() or None,
            "location": ", ".join(x for x in (place, "Remote" if row.get("isRemote") else "") if x),
            "url": f"https://thehub.io/jobs/{job_id}", "description": html_to_text(row.get("description") or ""),
            "posted_at": _iso(row.get("publishedAt") or row.get("createdAt")), "intern": "intern" in kinds.lower()}


def fetch_thehub(p: dict) -> list:
    found = {}
    for page in range(1, int(p.get("max_pages", 15)) + 1):
        data = http.get(f"https://thehub.io/api/v2/jobsandfeatured?{urlencode({'page': page, 'countryCode': p.get('country', 'EU')})}").json()
        jobs = data.get("jobs") or {}
        for row in jobs.get("docs") or []:
            offer = hub_offer(row)
            if offer and relevant(offer["title"], offer.pop("intern")):
                found.setdefault(offer["ext_id"], offer)
        if page >= int(jobs.get("pages") or 1):
            break
    return enrich(list(found.values()))


def arbeitnow_offer(row: dict) -> dict | None:
    title, url = str(row.get("title") or "").strip(), str(row.get("url") or "")
    if not title or not url.startswith("https://www.arbeitnow.com/"):
        return None
    kinds = " ".join(str(x) for x in (row.get("job_types") or []) + (row.get("tags") or []))
    return {"ext_id": str(row.get("slug") or url), "title": title, "company": (row.get("company_name") or "").strip() or None,
            "location": ", ".join(x for x in (row.get("location"), "Remote" if row.get("remote") else "") if x), "url": url,
            "description": html_to_text(row.get("description") or ""), "posted_at": _epoch(row.get("created_at")),
            "intern": bool(re.search(r"intern|praktikum", kinds, re.I))}


def fetch_arbeitnow(p: dict) -> list:
    found = {}
    for page in range(1, int(p.get("max_pages", 5)) + 1):
        rows = http.get(f"https://www.arbeitnow.com/api/job-board-api?page={page}").json().get("data") or []
        for row in rows:
            offer = arbeitnow_offer(row)
            if offer and relevant(offer["title"], offer.pop("intern")):
                found.setdefault(offer["ext_id"], offer)
        if len(rows) < 100:
            break
    return list(found.values())


def fetch_ibm(p: dict) -> list:
    """IBM careers search API; its free-text query is ignored, so it is narrowed to the "Internship" level."""
    found = {}
    must = [{"term": {"field_keyword_18": p.get("level", "Internship")}}]
    if p.get("country"):
        must.append({"term": {"field_keyword_05": p["country"]}})
    for start in range(0, 600, 30):
        body = {"appId": "careers", "scopes": ["careers2"], "query": {"bool": {"must": []}},
                "post_filter": {"bool": {"must": must}}, "size": 30, "from": start,
                "sort": [{"_score": "desc"}, {"pageviews": "desc"}], "lang": "zz", "localeSelector": {},
                "sm": {"query": "", "lang": "zz"},
                "_source": ["_id", "title", "url", "description", "field_keyword_05", "field_keyword_18", "field_keyword_19"]}
        hits = ((http.post("https://www-api.ibm.com/search/api/v2", json=body).json().get("hits") or {}).get("hits")) or []
        for hit in hits:
            s_ = hit.get("_source") or {}
            title, url = str(s_.get("title") or "").strip(), str(s_.get("url") or "").strip()
            if not title or not url.startswith("http") or not relevant(title, True):
                continue
            found.setdefault(url, {"ext_id": str(hit.get("_id") or url), "title": title, "url": url,
                                   "location": ", ".join(x for x in (s_.get("field_keyword_19"), s_.get("field_keyword_05")) if x),
                                   "description": "", "posted_at": None})
        if len(hits) < 30:
            break
    return enrich(list(found.values()))


# --- registry ------------------------------------------------------------------------------------

SOURCE_TYPES = {
    "wttj": "Welcome to the Jungle",
    "google": "Google Careers",
    "dassault": "Dassault Systèmes",
    "successfactors": "SAP SuccessFactors",
    "taleo": "Oracle Taleo",
    "icims": "iCIMS",
    "eightfold": "Eightfold",
    "csod": "Cornerstone (CSOD)",
    "softgarden": "softgarden",
    "join": "JOIN",
    "bamboohr": "BambooHR",
    "breezy": "Breezy HR",
    "pinpoint": "Pinpoint",
    "rippling": "Rippling",
    "jibeapply": "Jibe (iCIMS)",
    "jobvite": "Jobvite",
    "thehub": "The Hub (startups nordiques / UE)",
    "arbeitnow": "Arbeitnow (UE / DACH)",
    "ibm": "IBM Careers",
}

COLLECTORS = {
    "wttj": fetch_wttj, "google": fetch_google, "dassault": fetch_dassault, "successfactors": fetch_successfactors,
    "taleo": fetch_taleo, "icims": fetch_icims, "eightfold": fetch_eightfold, "csod": fetch_csod,
    "softgarden": fetch_softgarden, "join": fetch_join, "bamboohr": fetch_bamboohr, "breezy": fetch_breezy,
    "pinpoint": fetch_pinpoint, "rippling": fetch_rippling, "jibeapply": fetch_jibeapply, "jobvite": fetch_jobvite,
    "thehub": fetch_thehub, "arbeitnow": fetch_arbeitnow, "ibm": fetch_ibm,
}

# Boards that list many employers: an employer's own source wins when both carry the same offer.
AGGREGATORS = {"wttj", "thehub", "arbeitnow", "getro"}

PATTERNS = [
    # One company's page on Welcome to the Jungle: only its internships; the board itself: every internship.
    ("wttj", re.compile(r"welcometothejungle\.com/[a-z]{2}/companies/([\w-]+)", re.I),
     lambda m: {"filters": f"organization.slug:{m.group(1).lower()}", "queries": [""]}),
    ("wttj", re.compile(r"https://([\w-]+)\.welcomekit\.co", re.I),
     lambda m: {"filters": f"organization.slug:{m.group(1).lower()}", "queries": [""]}),
    ("wttj", re.compile(r"welcometothejungle\.com", re.I), lambda m: {}),
    ("google", re.compile(r"google\.com/about/careers", re.I), lambda m: {}),
    ("dassault", re.compile(r"(?:www\.)?3ds\.com/careers", re.I), lambda m: {}),
    ("taleo", _TALEO_URL, lambda m: {"url": m.group(0)}),
    ("icims", re.compile(r"(https://[\w-]+\.icims\.com)", re.I), lambda m: {"origin": m.group(1).lower()}),
    ("eightfold", re.compile(r"(?:apply|jobs)\.careers\.microsoft\.com", re.I),
     lambda m: {"host": "apply.careers.microsoft.com", "domain": "microsoft.com"}),
    ("eightfold", re.compile(r"https://([\w-]+\.eightfold\.ai)(?:/careers)?(?:\?[^\s\"'<>]*?domain=([\w.-]+))?", re.I),
     lambda m: {"host": m.group(1).lower(), **({"domain": m.group(2)} if m.group(2) else {})}),
    ("csod", re.compile(r"(https://[\w-]+\.csod\.com)/ux/ats/careersite/(\d+)/home(?:/[^\s\"'<>?]*)?\?c=([\w-]+)", re.I),
     lambda m: {"origin": m.group(1).lower(), "site_id": int(m.group(2)), "corp": m.group(3)}),
    ("softgarden", re.compile(r"(https://[\w-]+\.softgarden\.io)(?:/([a-z]{2}))?", re.I),
     lambda m: {"widget": f"{m.group(1).lower()}/{m.group(2) or 'de'}/widgets/jobs"}),
    ("join", re.compile(r"join\.com/companies/([\w-]+)", re.I), lambda m: {"slug": m.group(1)}),
    ("bamboohr", re.compile(r"https://([a-z0-9][\w-]*)\.bamboohr\.com", re.I), lambda m: {"tenant": m.group(1).lower()}),
    ("breezy", re.compile(r"https://([a-z0-9][\w-]*)\.breezy\.hr", re.I), lambda m: {"tenant": m.group(1).lower()}),
    ("pinpoint", re.compile(r"https://([a-z0-9][\w-]*)\.pinpointhq\.com", re.I), lambda m: {"slug": m.group(1).lower()}),
    ("rippling", re.compile(r"ats\.rippling\.com/(?:api/v2/board/)?([A-Za-z0-9][\w-]*)", re.I), lambda m: {"slug": m.group(1)}),
    ("jibeapply", re.compile(r"(https://[\w-]+\.jibeapply\.com)", re.I), lambda m: {"origin": m.group(1).lower()}),
    ("jobvite", re.compile(r"jobs\.jobvite\.com/([\w-]+)", re.I), lambda m: {"slug": m.group(1)}),
    ("thehub", re.compile(r"thehub\.io", re.I), lambda m: {}),
    ("arbeitnow", re.compile(r"arbeitnow\.com", re.I), lambda m: {}),
    ("ibm", re.compile(r"ibm\.com/careers", re.I), lambda m: {}),
]

# Branded career sites run on SuccessFactors without saying so in the URL; the page source gives it away.
SUCCESSFACTORS_MARKERS = re.compile(r"rmkcdn\.successfactors|jobs2web|tile-search-results|successfactors\.(?:eu|com)", re.I)


def count_postings(source_type: str, p: dict) -> int | None:
    """A cheap check that a detected source answers; None for the types without a cheap count."""
    if source_type == "breezy":
        return len(http.get(f"https://{p['tenant']}.breezy.hr/json").json())
    if source_type == "bamboohr":
        return len(http.get(f"https://{p['tenant']}.bamboohr.com/careers/list", allow_redirects=False).json().get("result") or [])
    if source_type == "pinpoint":
        return len(http.get(f"https://{p['slug']}.pinpointhq.com/postings.json").json().get("data") or [])
    if source_type == "rippling":
        return http.get(f"https://ats.rippling.com/api/v2/board/{p['slug']}/jobs?page=0&pageSize=1").json().get("totalItems", 0)
    if source_type == "softgarden":
        return len(softgarden_rows(http.get(p["widget"]).text, p["widget"]))
    if source_type == "eightfold":
        scope = {"domain": p["domain"]} if p.get("domain") else {}
        try:
            data = http.get(f"https://{p['host']}/api/apply/v2/jobs?{urlencode({**scope, 'num': 1})}").json()
        except Exception:  # noqa: BLE001 - tenants moved to PCSX refuse v2
            data = http.get(f"https://{p['host']}/api/pcsx/search?{urlencode(scope)}").json()
        return eightfold_positions(data)[1] or 0
    if source_type == "successfactors":
        return len(successfactors_tiles(http.get(f"{p['base'].rstrip('/')}/tile-search-results/?startrow=0").text, p["base"])) or None
    if source_type == "icims":
        return len(icims_cards(http.get(f"{p['origin']}/jobs/search?ss=1&in_iframe=1", headers={"User-Agent": BROWSER_UA}).text, p["origin"]))
    if source_type == "join":
        state = _next_data(http.get(f"https://join.com/companies/{p['slug']}").text).get("props", {}).get("pageProps", {}).get("initialState") or {}
        return len((state.get("jobs") or {}).get("items") or [])
    return None


# Platforms probed when the user types a company name (after the ones fetchers.py already tries).
NAME_PROBES = (("breezy", "tenant"), ("bamboohr", "tenant"), ("pinpoint", "slug"), ("rippling", "slug"))
