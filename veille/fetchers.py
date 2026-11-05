"""Collectors for public job-board APIs, plus source auto-detection.

Every collector returns raw offers as dicts with the keys
ext_id, title, location, url, description, posted_at. Only titles that look like relevant
internships get their full description fetched, to keep the number of requests small.
"""
import html
import json
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit

from . import http
from .scoring import looks_like_internship, title_maybe_relevant

DETAIL_PAUSE = 0.15  # seconds between detail requests on the same site

SOURCE_TYPES = {
    "greenhouse": "Greenhouse",
    "lever": "Lever",
    "ashby": "Ashby",
    "workday": "Workday",
    "smartrecruiters": "SmartRecruiters",
    "workable": "Workable",
    "amazon": "Amazon Jobs",
    "avature": "Avature",
    "oracle": "Oracle Cloud (HCM)",
    "phenom": "Phenom",
    "personio": "Personio",
    "recruitee": "Recruitee",
    "getro": "Getro (portefeuille d'un fonds)",
    "rss": "Flux RSS",
    "link": "Lien à vérifier à la main",
}


def html_to_text(raw: str) -> str:
    if not raw:
        return ""
    text = re.sub(r"(?i)<\s*(br|/p|/div|/h\d|/li)\s*/?>", "\n", raw)
    text = re.sub(r"(?i)<\s*li[^>]*>", "• ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"(?m)^[ \t]*•[ \t]*(?:\n|$)", "", text)  # empty list items (icons, spacers) leave lone bullets
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _iso_from_ms(ms) -> str | None:
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).date().isoformat()


def _iso(value) -> str | None:
    if not value:
        return None
    value = str(value)
    match = re.match(r"\d{4}-\d{2}-\d{2}", value)
    if match:
        return match.group(0)
    for fmt in ("%B %d, %Y", "%d %B %Y", "%a, %d %b %Y %H:%M:%S %z"):
        try:
            return datetime.strptime(value.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return None


# --- collectors -----------------------------------------------------------------------------

def fetch_greenhouse(p: dict) -> list:
    base = f"https://boards-api.greenhouse.io/v1/boards/{p['token']}/jobs"
    out = []
    for job in http.get(base).json().get("jobs", []):
        if not title_maybe_relevant(job.get("title", "")):
            continue
        detail = http.get(f"{base}/{job['id']}").json()
        time.sleep(DETAIL_PAUSE)
        out.append({
            "ext_id": str(job["id"]),
            "title": job["title"],
            "location": (job.get("location") or {}).get("name", ""),
            "url": job.get("absolute_url"),
            "description": html_to_text(html.unescape(detail.get("content", ""))),
            "posted_at": _iso(job.get("first_published") or job.get("updated_at")),
        })
    return out


def fetch_lever(p: dict) -> list:
    host = "api.eu.lever.co" if p.get("eu") else "api.lever.co"
    out = []
    for job in http.get(f"https://{host}/v0/postings/{p['token']}?mode=json").json():
        title = job.get("text", "")
        categories = job.get("categories") or {}
        commitment = categories.get("commitment") or ""
        if not (title_maybe_relevant(title) or (re.search(r"intern", commitment, re.I) and title_maybe_relevant(f"intern {title}"))):
            continue
        sections = [job.get("descriptionPlain", "")]
        for block in job.get("lists", []):
            sections.append(f"{block.get('text', '')}\n{html_to_text(block.get('content', ''))}")
        sections.append(job.get("additionalPlain", ""))
        locations = categories.get("allLocations") or [categories.get("location", "")]
        out.append({
            "ext_id": job["id"],
            "title": title,
            "location": ", ".join(l for l in locations if l),
            "url": job.get("hostedUrl"),
            "description": "\n".join(s for s in sections if s).strip(),
            "posted_at": _iso_from_ms(job.get("createdAt")),
        })
    return out


def fetch_ashby(p: dict) -> list:
    data = http.get(f"https://api.ashbyhq.com/posting-api/job-board/{quote(p['token'])}").json()
    out = []
    for job in data.get("jobs", []):
        title = job.get("title", "")
        intern_type = job.get("employmentType") == "Intern"
        if not job.get("isListed", True):
            continue
        if not (title_maybe_relevant(title) or (intern_type and title_maybe_relevant(f"intern {title}"))):
            continue
        locations = [job.get("location", "")]
        for extra in job.get("secondaryLocations") or []:
            locations.append(extra.get("location", "") if isinstance(extra, dict) else str(extra))
        out.append({
            "ext_id": job["id"],
            "title": title,
            "location": ", ".join(l for l in locations if l),
            "url": job.get("jobUrl"),
            "description": job.get("descriptionPlain") or html_to_text(job.get("descriptionHtml", "")),
            "posted_at": _iso(job.get("publishedAt")),
        })
    return out


WORKDAY_TERMS = ("intern", "internship", "stage", "praktikum", "thesis")


def fetch_workday(p: dict) -> list:
    api = f"https://{p['host']}/wday/cxs/{p['tenant']}/{p['site']}"
    candidates = {}
    for term in WORKDAY_TERMS:
        for page in range(5):
            body = {"appliedFacets": {}, "limit": 20, "offset": page * 20, "searchText": term}
            postings = http.post(f"{api}/jobs", json=body).json().get("jobPostings", [])
            for posting in postings:
                if title_maybe_relevant(posting.get("title", "")):
                    candidates[posting["externalPath"]] = posting
            if len(postings) < 20:
                break
    out = []
    for path, posting in candidates.items():
        info = http.get(f"{api}{path}").json().get("jobPostingInfo", {})
        time.sleep(DETAIL_PAUSE)
        locations = [info.get("location") or posting.get("locationsText", "")]
        locations += info.get("additionalLocations") or []
        country = (info.get("country") or {}).get("descriptor")
        if country:
            locations.append(country)
        # bulletFields mix locations with requisition ids such as "R19935" or "JR-055141".
        locations += [b for b in posting.get("bulletFields") or [] if not re.fullmatch(r"[A-Z]{0,4}[-_]?\d[\w-]*", b)]
        out.append({
            "ext_id": info.get("jobReqId") or path,
            "title": posting["title"],
            "location": ", ".join(dict.fromkeys(l for l in locations if l)),
            "url": info.get("externalUrl") or f"https://{p['host']}/{p['site']}{path}",
            "description": html_to_text(info.get("jobDescription", "")),
            "posted_at": _iso(info.get("startDate")),
        })
    return out


SMARTRECRUITERS_TERMS = ("intern", "internship", "stage", "stagiaire", "praktikum", "thesis")


def fetch_smartrecruiters(p: dict) -> list:
    company = p["company"]
    candidates = {}
    for term in SMARTRECRUITERS_TERMS:
        for page in range(3):
            data = http.get(
                f"https://api.smartrecruiters.com/v1/companies/{company}/postings",
                params={"q": term, "limit": 100, "offset": page * 100},
            ).json()
            content = data.get("content", [])
            for posting in content:
                if title_maybe_relevant(posting.get("name", "")):
                    candidates[posting["id"]] = posting
            if len(content) < 100:
                break
    out = []
    for posting_id, posting in candidates.items():
        detail = http.get(posting["ref"]).json()
        time.sleep(DETAIL_PAUSE)
        sections = (detail.get("jobAd") or {}).get("sections") or {}
        text = "\n\n".join(
            html_to_text((sections.get(key) or {}).get("text", ""))
            for key in ("jobDescription", "qualifications", "additionalInformation")
        )
        location = (posting.get("location") or {}).get("fullLocation") or ""
        out.append({
            "ext_id": posting_id,
            "title": posting["name"],
            "location": location,
            "url": detail.get("postingUrl") or f"https://jobs.smartrecruiters.com/{company}/{posting_id}",
            "description": text.strip(),
            "posted_at": _iso(posting.get("releasedDate")),
        })
    return out


def fetch_workable(p: dict) -> list:
    account = p["account"]
    data = http.post(f"https://apply.workable.com/api/v3/accounts/{account}/jobs", json={"query": ""}).json()
    out = []
    for job in data.get("results", []):
        if not title_maybe_relevant(job.get("title", "")):
            continue
        detail = http.get(f"https://apply.workable.com/api/v2/accounts/{account}/jobs/{job['shortcode']}").json()
        time.sleep(DETAIL_PAUSE)
        loc = job.get("location") or {}
        description = "\n\n".join(html_to_text(detail.get(k, "")) for k in ("description", "requirements", "benefits"))
        out.append({
            "ext_id": job["shortcode"],
            "title": job["title"],
            "location": ", ".join(x for x in (loc.get("city"), loc.get("country")) if x),
            "url": f"https://apply.workable.com/{account}/j/{job['shortcode']}/",
            "description": description.strip(),
            "posted_at": _iso(job.get("published")),
        })
    return out


AMAZON_QUERIES = ("applied scientist intern", "machine learning intern", "data scientist intern",
                  "research scientist intern", "deep learning intern")


def fetch_amazon(p: dict) -> list:
    seen = {}
    for query in p.get("queries") or AMAZON_QUERIES:
        for page in range(3):
            data = http.get("https://www.amazon.jobs/en/search.json",
                            params={"base_query": query, "result_limit": 100, "offset": page * 100}).json()
            jobs = data.get("jobs", [])
            for job in jobs:
                title = job.get("title", "")
                if not (title_maybe_relevant(title) or (job.get("is_intern") and title_maybe_relevant(f"intern {title}"))):
                    continue
                description = "\n\n".join(html_to_text(job.get(k, "")) for k in
                                          ("description", "basic_qualifications", "preferred_qualifications"))
                seen[job["id"]] = {
                    "ext_id": str(job["id"]),
                    "title": title,
                    "location": job.get("normalized_location") or job.get("location", ""),
                    "url": "https://www.amazon.jobs" + job.get("job_path", ""),
                    "description": description.strip(),
                    "posted_at": _iso(job.get("posted_date")),
                }
            if len(jobs) < 100:
                break
    return list(seen.values())


AVATURE_QUERIES = ("stage data", "stage data scientist", "stage intelligence artificielle", "stage machine learning",
                   "stage IA", "internship data", "internship machine learning", "traineeship")
_AVATURE_FIELD_RE = re.compile(r'<dl class="article__content__view__field[^"]*">(.*?)</dl>', re.S)
_AVATURE_LABEL_RE = re.compile(r'<dt class="article__content__view__field__label">(.*?)</dt>', re.S)
_AVATURE_VALUE_RE = re.compile(r'<dd class="article__content__view__field__value">(.*?)</dd>', re.S)
_AVATURE_LOCATION_LABELS = {"country", "city", "pays", "ville", "location", "localisation", "lieu"}
_PLACE_OF_WORK_RE = re.compile(r"(?im)^(?:place of work|work location|duty station|location|lieu de travail|localisation)"
                               r"\s*:?\s*\n+\s*([^\n]{2,80})$")


def _avature_detail(page: str) -> tuple[str, str]:
    """Return (location, description) from an Avature job page's labelled fields."""
    location, parts = [], []
    for block in _AVATURE_FIELD_RE.findall(page):
        label = html_to_text((_AVATURE_LABEL_RE.search(block) or [None, ""])[1]).strip().lower()
        value = html_to_text((_AVATURE_VALUE_RE.search(block) or [None, ""])[1])
        if label in _AVATURE_LOCATION_LABELS and value:
            location.append(value.title() if value.isupper() else value)
        elif value:
            parts.append(value)
    if not parts:  # some Avature sites (Siemens, ECB) use another layout: keep the page text
        text = page_text(page)
        parts.append(text)
        if not location and (m := _PLACE_OF_WORK_RE.search(text)):
            location.append(m[1].strip())
    return ", ".join(dict.fromkeys(location)), "\n\n".join(parts)


def page_text(html_page: str, limit: int = 20000) -> str:
    """Readable text of a whole HTML page, without scripts and styles."""
    body = re.sub(r"(?is)<(script|style|noscript|svg|header|footer|nav)[^>]*>.*?</\1>", " ", html_page)
    return html_to_text(body)[:limit]


def fetch_avature(p: dict) -> list:
    """Avature career sites expose an RSS feed of their keyword search; details come from the job page."""
    base = p["base"].rstrip("/")
    candidates = {}
    for query in p.get("queries") or AVATURE_QUERIES:
        root = ET.fromstring(http.get(f"{base}/SearchJobs/feed/", params={"search": query}).content)
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            if link and title_maybe_relevant(title):
                candidates[link] = (title, item.findtext("pubDate"))
    out = []
    for link, (title, published) in candidates.items():
        location, description = _avature_detail(http.get(link).text)
        time.sleep(DETAIL_PAUSE)
        out.append({
            "ext_id": link.rstrip("/").rsplit("/", 1)[-1],
            "title": title,
            "location": location,
            "url": link,
            "description": description,
            "posted_at": _iso(published),
        })
    return out


ORACLE_QUERIES = ("intern", "internship", "summer analyst", "off-cycle", "data science", "machine learning",
                  "quantitative research")


def fetch_oracle(p: dict) -> list:
    """Oracle Recruiting Cloud sites (hcmUI/CandidateExperience) are backed by a public REST API."""
    api = f"https://{p['host']}/hcmRestApi/resources/latest"
    site = p["site"]
    candidates = {}
    for query in p.get("queries") or ORACLE_QUERIES:
        for page in range(4):
            finder = f'findReqs;siteNumber={site},keyword="{query}",limit=25,offset={page * 25},sortBy=RELEVANCY'
            # Without expand, the response only carries the total count, not the requisitions.
            items = http.get(f"{api}/recruitingCEJobRequisitions",
                             params={"onlyData": "true", "expand": "requisitionList", "finder": finder}).json().get("items", [])
            requisitions = items[0].get("requisitionList", []) if items else []
            for req in requisitions:
                if title_maybe_relevant(req.get("Title", "")):
                    candidates[req["Id"]] = req
            if len(requisitions) < 25:
                break
    out = []
    for req_id, req in candidates.items():
        finder = f'ById;Id="{req_id}",siteNumber={site}'
        items = http.get(f"{api}/recruitingCEJobRequisitionDetails",
                         params={"expand": "all", "onlyData": "true", "finder": finder}).json().get("items", [])
        time.sleep(DETAIL_PAUSE)
        detail = items[0] if items else {}
        description = "\n\n".join(html_to_text(detail.get(k) or "") for k in
                                  ("ExternalDescriptionStr", "ExternalResponsibilitiesStr", "ExternalQualificationsStr"))
        locations = [req.get("PrimaryLocation", "")] + [s.get("Name", "") for s in detail.get("secondaryLocations") or []]
        out.append({
            "ext_id": str(req_id),
            "title": req["Title"],
            "location": ", ".join(l for l in dict.fromkeys(locations) if l),
            "url": f"https://{p['host']}/hcmUI/CandidateExperience/en/sites/{site}/job/{req_id}",
            "description": description.strip(),
            "posted_at": _iso(req.get("PostedDate")),
            "deadline": _iso(req.get("PostingEndDate")),
        })
    return out


PHENOM_QUERIES = ("stage data", "stage machine learning", "stage intelligence artificielle", "stage data scientist",
                  "intern data", "internship machine learning", "internship data science")


def fetch_phenom(p: dict) -> list:
    """Phenom career sites answer their own search widget with JSON; descriptions come from the job page."""
    host, lang, country = p["host"], p["lang"], p["country"]
    candidates = {}
    for query in p.get("queries") or PHENOM_QUERIES:
        for page in range(3):
            body = {"lang": lang, "deviceType": "desktop", "country": country, "pageName": "search-results",
                    "ddoKey": "refineSearch", "sortBy": "", "subsearch": "", "from": page * 50, "jobs": True,
                    "counts": False, "all_fields": [], "size": 50, "clearAll": False, "jdsource": "facets",
                    "isSliderEnable": False, "pageId": "page20", "siteType": "external", "keywords": query,
                    "global": True, "selected_fields": {}}
            data = http.post(f"https://{host}/widgets", json=body).json().get("refineSearch", {}).get("data", {})
            jobs = data.get("jobs", [])
            for job in jobs:
                if title_maybe_relevant(job.get("title", "")):
                    candidates[job["jobId"]] = job
            if len(jobs) < 50:
                break
    out = []
    for job_id, job in candidates.items():
        url = f"{p['job_url']}{job_id}"
        description = html_to_text(job.get("descriptionTeaser", ""))
        try:
            page = http.get(url).text
            for block in re.findall(r'<script type="application/ld\+json">(.*?)</script>', page, re.S):
                ld = json.loads(block)
                if isinstance(ld, dict) and ld.get("description"):
                    description = html_to_text(html.unescape(ld["description"]))
                    break
        except Exception:  # noqa: BLE001 - keep the teaser when the page cannot be read
            pass
        time.sleep(DETAIL_PAUSE)
        out.append({
            "ext_id": job_id,
            "title": job["title"],
            "location": ", ".join(x for x in (job.get("city"), job.get("country")) if x),
            "url": url,
            "description": description,
            "posted_at": _iso(job.get("postedDate")),
        })
    return out


GETRO_QUERIES = ("intern", "internship", "stage", "stagiaire", "praktikum", "thesis")
GETRO_HEADCOUNT = {1: "1-10", 2: "11-50", 3: "51-200", 4: "201-500", 5: "501-1000", 6: "1001-5000",
                   7: "5001-10000", 8: "10000+"}
GETRO_STAGES = {"pre_seed": "Pre-seed", "seed": "Seed", "series_a": "Série A", "series_b": "Série B",
                "series_c": "Série C", "series_d": "Série D", "series_e": "Série E+", "growth": "Growth",
                "ipo": "Cotée", "public": "Cotée", "acquired": "Rachetée"}


def _getro_collection(board: str) -> int:
    page = http.get(f"{board.rstrip('/')}/jobs").text
    data = json.loads(re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', page, re.S).group(1))
    return data["props"]["pageProps"]["network"]["id"]


def fetch_getro(p: dict) -> list:
    """Getro powers the portfolio job boards of many VC funds: one source covers every startup they back."""
    collection = p.get("collection_id") or _getro_collection(p["board"])
    api = f"https://api.getro.com/api/v2/collections/{collection}/search/jobs"
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    candidates = {}
    for query in p.get("queries") or GETRO_QUERIES:
        for page in range(5):
            results = http.post(api, headers=headers, json={"hitsPerPage": 100, "page": page, "filters": "",
                                                             "query": query}).json().get("results", {})
            jobs = results.get("jobs", [])
            for job in jobs:
                if title_maybe_relevant(job.get("title", "")):
                    candidates[job["id"]] = job
            if len(jobs) < 100:
                break
    out = []
    for job_id, job in candidates.items():
        org = job.get("organization") or {}
        description = ""
        if job.get("url"):
            try:  # the board links to the startup's own posting, which carries the description
                description = page_text(http.get(job["url"], retries=0).text)
            except Exception:  # noqa: BLE001
                pass
            time.sleep(DETAIL_PAUSE)
        stipend = None
        if job.get("compensation_public") and job.get("compensation_amount_min_cents"):
            period = {"monthly": "mois", "month": "mois", "yearly": "an", "year": "an"}.get(job.get("compensation_period"), "")
            stipend = f"{job['compensation_amount_min_cents'] // 100:,} {job.get('compensation_currency') or ''}/{period}".replace(",", " ")
        out.append({
            "ext_id": str(job_id),
            "title": job["title"],
            "company": org.get("name"),
            "location": ", ".join(job.get("locations") or job.get("searchable_locations") or []),
            "url": job.get("url"),
            "description": description,
            "posted_at": _iso_from_ms((job.get("created_at") or 0) * 1000),
            "company_stage": GETRO_STAGES.get(org.get("stage"), ""),
            "company_size": GETRO_HEADCOUNT.get(org.get("head_count"), ""),
            "stipend": stipend,
        })
    return out


# Personio and Recruitee host the careers pages of many European (often German) startups and SMEs.
PERSONIO_INTERN_TYPES = {"intern", "trainee"}


def _personio_base(p: dict) -> str:
    return f"https://{p['company']}.{p.get('domain', 'jobs.personio.de')}"


def fetch_personio(p: dict) -> list:
    """Personio publishes every open position as an XML feed, descriptions included."""
    base = _personio_base(p)
    root = ET.fromstring(http.get(f"{base}/xml", params={"language": p.get("language", "en")}).content)
    out = []
    for pos in root.iter("position"):
        title = (pos.findtext("name") or "").strip()
        intern_type = (pos.findtext("employmentType") or "").strip().lower() in PERSONIO_INTERN_TYPES
        if not (title_maybe_relevant(title) or (intern_type and title_maybe_relevant(f"intern {title}"))):
            continue
        offices = [pos.findtext("office") or ""] + [o.text or "" for o in pos.findall("additionalOffices/office")]
        sections = []
        for block in pos.findall("jobDescriptions/jobDescription"):
            name = (block.findtext("name") or "").strip()
            sections.append(f"{name}\n{html_to_text(block.findtext('value') or '')}".strip())
        job_id = (pos.findtext("id") or "").strip()
        out.append({
            "ext_id": job_id,
            "title": title,
            "location": ", ".join(o.strip() for o in dict.fromkeys(offices) if o.strip()),
            "url": f"{base}/job/{job_id}",
            "description": "\n\n".join(sections),
            "posted_at": _iso(pos.findtext("createdAt")),
        })
    return out


def fetch_recruitee(p: dict) -> list:
    """Recruitee career sites answer /api/offers/ with every published offer, descriptions included."""
    data = http.get(f"https://{p['company']}.recruitee.com/api/offers/").json()
    out = []
    for job in data.get("offers", []):
        title = job.get("title", "")
        intern_type = "intern" in (job.get("employment_type_code") or "").lower()
        if job.get("status", "published") != "published":
            continue
        if not (title_maybe_relevant(title) or (intern_type and title_maybe_relevant(f"intern {title}"))):
            continue
        location = job.get("location") or ", ".join(x for x in (job.get("city"), job.get("country")) if x)
        description = "\n\n".join(html_to_text(job.get(k) or "") for k in ("description", "requirements"))
        out.append({
            "ext_id": str(job.get("id") or job.get("slug")),
            "title": title,
            "location": location,
            "url": job.get("careers_url") or f"https://{p['company']}.recruitee.com/o/{job.get('slug', '')}",
            "description": description.strip(),
            "posted_at": _iso(job.get("published_at") or job.get("created_at")),
        })
    return out


def fetch_rss(p: dict) -> list:
    root = ET.fromstring(http.get(p["url"]).content)
    out = []
    items = root.iter("item")
    atom = "{http://www.w3.org/2005/Atom}"
    entries = list(items) or list(root.iter(f"{atom}entry"))
    for item in entries:
        def text(tag):
            node = item.find(tag) if item.find(tag) is not None else item.find(atom + tag)
            return (node.text or "").strip() if node is not None else ""
        title = text("title")
        link = text("link")
        if not link:
            node = item.find(atom + "link")
            link = node.get("href", "") if node is not None else ""
        if not looks_like_internship(title):
            continue
        description = html_to_text(text("description") or text("summary") or text("content"))
        out.append({
            "ext_id": text("guid") or link or title,
            "title": title,
            "location": p.get("location", ""),
            "url": link,
            "description": description,
            "posted_at": _iso(text("pubDate") or text("updated")),
        })
    return out


COLLECTORS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "workday": fetch_workday,
    "smartrecruiters": fetch_smartrecruiters,
    "workable": fetch_workable,
    "amazon": fetch_amazon,
    "avature": fetch_avature,
    "oracle": fetch_oracle,
    "phenom": fetch_phenom,
    "getro": fetch_getro,
    "personio": fetch_personio,
    "recruitee": fetch_recruitee,
    "rss": fetch_rss,
}


def fetch(source_type: str, params: dict) -> list:
    if source_type == "link":
        return []
    return COLLECTORS[source_type](params)


# --- detection ------------------------------------------------------------------------------

_PATTERNS = [
    ("greenhouse", re.compile(r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board\?for=)?([\w-]+)", re.I),
     lambda m: {"token": m.group(1)}),
    ("greenhouse", re.compile(r"boards-api\.greenhouse\.io/v1/boards/([\w-]+)", re.I), lambda m: {"token": m.group(1)}),
    ("lever", re.compile(r"jobs\.(eu\.)?lever\.co/([\w.-]+)", re.I),
     lambda m: {"token": m.group(2), **({"eu": True} if m.group(1) else {})}),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([\w.%-]+)", re.I), lambda m: {"token": m.group(1)}),
    ("workday", re.compile(r"([\w-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([\w-]+)"),
     lambda m: {"host": f"{m.group(1)}.{m.group(2)}.myworkdayjobs.com", "tenant": m.group(1), "site": m.group(3)}),
    ("workable", re.compile(r"apply\.workable\.com/([\w-]+)", re.I), lambda m: {"account": m.group(1)}),
    ("smartrecruiters", re.compile(r"(?:careers|jobs)\.smartrecruiters\.com/([\w-]+)", re.I),
     lambda m: {"company": m.group(1)}),
    ("amazon", re.compile(r"amazon\.jobs", re.I), lambda m: {}),
    ("avature", re.compile(r"(https?://[\w.-]+(?:/[a-z]{2}_[A-Z]{2})?/(?:careers|jobs))/(?:SearchJobs|JobDetail|Home)"),
     lambda m: {"base": m.group(1)}),
    ("oracle", re.compile(r"([\w.-]+\.oraclecloud\.com)/hcmUI/CandidateExperience/[\w-]+/sites/([\w-]+)"),
     lambda m: {"host": m.group(1), "site": m.group(2)}),
    ("personio", re.compile(r"([\w-]+)\.(jobs\.personio\.(?:de|com))", re.I),
     lambda m: {"company": m.group(1).lower(), "domain": m.group(2).lower()}),
    ("recruitee", re.compile(r"([\w-]+)\.recruitee\.com", re.I), lambda m: {"company": m.group(1).lower()}),
    # Teamtailor career sites (common among French startups) publish an RSS feed of all openings.
    ("rss", re.compile(r"https?://([\w-]+)\.teamtailor\.com", re.I),
     lambda m: {"url": f"https://{m.group(1)}.teamtailor.com/jobs.rss"}),
]

_IGNORED_WORKDAY_SITES = {"login", "job", "details"}


# Whole job boards (or one employer's single site): recognised from the pasted URL only, never from a link
# found inside another careers page (a startup's page linking to its Welcome to the Jungle profile).
_URL_ONLY_TYPES = {"wttj", "google", "dassault", "thehub", "arbeitnow", "ibm"}


def _match_patterns(text: str, from_page: bool = False):
    for source_type, regex, params in _PATTERNS + providers.PATTERNS:
        if from_page and source_type in _URL_ONLY_TYPES:
            continue
        m = regex.search(text)
        if m:
            p = params(m)
            if source_type == "workday" and p["site"].lower() in _IGNORED_WORKDAY_SITES:
                continue
            return source_type, p
    return None


def count_postings(source_type: str, p: dict) -> int:
    """Total postings on the board (all jobs, not only internships) to confirm a source works."""
    if source_type == "greenhouse":
        return len(http.get(f"https://boards-api.greenhouse.io/v1/boards/{p['token']}/jobs").json().get("jobs", []))
    if source_type == "lever":
        host = "api.eu.lever.co" if p.get("eu") else "api.lever.co"
        return len(http.get(f"https://{host}/v0/postings/{p['token']}?mode=json").json())
    if source_type == "ashby":
        return len(http.get(f"https://api.ashbyhq.com/posting-api/job-board/{quote(p['token'])}").json().get("jobs", []))
    if source_type == "workday":
        body = {"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""}
        return http.post(f"https://{p['host']}/wday/cxs/{p['tenant']}/{p['site']}/jobs", json=body).json().get("total", 0)
    if source_type == "workable":
        return http.post(f"https://apply.workable.com/api/v3/accounts/{p['account']}/jobs", json={"query": ""}).json().get("total", 0)
    if source_type == "smartrecruiters":
        return http.get(f"https://api.smartrecruiters.com/v1/companies/{p['company']}/postings", params={"limit": 1}).json().get("totalFound", 0)
    if source_type == "oracle":
        finder = f"findReqs;siteNumber={p['site']},limit=1"
        items = http.get(f"https://{p['host']}/hcmRestApi/resources/latest/recruitingCEJobRequisitions",
                         params={"onlyData": "true", "finder": finder}).json().get("items", [])
        return items[0].get("TotalJobsCount", 0) if items else 0
    if source_type == "avature":
        feed = http.get(f"{p['base'].rstrip('/')}/SearchJobs/feed/").content
        return sum(1 for _ in ET.fromstring(feed).iter("item"))
    if source_type == "personio":
        root = ET.fromstring(http.get(f"{_personio_base(p)}/xml").content)
        return sum(1 for _ in root.iter("position"))
    if source_type == "recruitee":
        return len(http.get(f"https://{p['company']}.recruitee.com/api/offers/").json().get("offers", []))
    if source_type == "rss":
        root = ET.fromstring(http.get(p["url"]).content)
        return sum(1 for _ in root.iter("item")) or sum(1 for _ in root.iter("{http://www.w3.org/2005/Atom}entry"))
    if source_type in providers.COLLECTORS:
        return providers.count_postings(source_type, p)
    return 0


def _slugs(name: str) -> list:
    base = re.sub(r"[^a-z0-9 .-]", "", name.lower()).strip()
    variants = [base.replace(" ", ""), base.replace(" ", "-"), base.replace(" ", ""), f"{base.replace(' ', '')}ai",
                base.split(" ")[0] if base else ""]
    return [v for v in dict.fromkeys(variants) if v]


def detect_source(text: str) -> dict:
    """Guess source type and parameters from a careers URL or a company name."""
    text = (text or "").strip()
    if not text:
        raise ValueError("Colle une URL de page carrières ou un nom d'entreprise.")

    if re.match(r"https?://", text):
        found = _match_patterns(text)
        if not found and re.search(r"(\.xml|\.rss|/rss|/feed)(\?|$)", text, re.I):
            found = ("rss", {"url": text})
        if not found:
            try:
                page = http.get(text, retries=0).text
                found = _match_patterns(page, from_page=True)
                if not found and providers.SUCCESSFACTORS_MARKERS.search(page):
                    parts = urlsplit(text)
                    found = ("successfactors", {"base": f"{parts.scheme}://{parts.netloc}"})
            except Exception:  # noqa: BLE001 - unreachable page: fall back to a manual link
                found = None
        if not found:
            return {"type": "link", "params": {"url": text}, "count": None,
                    "message": "Aucune API reconnue : la source sera un lien à vérifier à la main."}
        source_type, params = found
        try:
            count = count_postings(source_type, params)
        except Exception as e:  # noqa: BLE001
            return {"type": source_type, "params": params, "count": None,
                    "message": f"Source reconnue mais injoignable ({type(e).__name__})."}
        return {"type": source_type, "params": params, "count": count, "message": "Source reconnue."}

    probes = [("greenhouse", "token"), ("ashby", "token"), ("lever", "token"), ("workable", "account"),
              ("personio", "company"), ("recruitee", "company"), *providers.NAME_PROBES]
    for slug in _slugs(text):
        for source_type, params in ((t, {key: slug}) for t, key in probes):
            try:
                count = count_postings(source_type, params)
            except Exception:  # noqa: BLE001 - token does not exist on this platform
                continue
            if count:
                return {"type": source_type, "params": params, "count": count,
                        "message": f"Trouvé sur {SOURCE_TYPES[source_type]}."}
    return {"type": None, "params": {}, "count": None,
            "message": "Introuvable sur Greenhouse, Lever, Ashby, Workable, Personio, Recruitee, Breezy, BambooHR, Pinpoint ou Rippling. "
                       "Colle plutôt l'URL de sa page carrières."}


# --- sources ported from career-ops (veille/providers.py) ------------------------------------------

from . import providers  # noqa: E402 - needs the helpers above

COLLECTORS.update(providers.COLLECTORS)
SOURCE_TYPES = {**{k: v for k, v in SOURCE_TYPES.items() if k not in ("rss", "link")}, **providers.SOURCE_TYPES,
                "rss": SOURCE_TYPES["rss"], "link": SOURCE_TYPES["link"]}
