"""Is a posting still open? Ported from career-ops (liveness-core.mjs, MIT licence).

Offers collected from an API disappear from it when they close, so the collection already deactivates them.
This check is for the others: offers imported by hand or by Claude (sources without an API), and an offer
you are about to apply to. It reads the page once, without a browser, and answers:

- "expired": closed for sure (404 / 410, "no longer accepting applications", "offre pourvue"…)
- "active": the page shows an apply button
- "uncertain": blocked by an anti-bot page, a server error, a redirect away from the posting, or nothing
  conclusive (pages rendered by JavaScript): never treated as closed.
"""
import re
import unicodedata

import requests

from . import http

HARD_EXPIRED = [
    r"job (is )?no longer available",
    r"job.*no longer open",
    r"\b(?:job|jobs|position|role|posting|opening|vacancy|requisition|req|listing)\b.{0,60}?(?<!application )(?<!form )has been filled\b(?! out)",
    r"this job has expired",
    r"job posting has expired",
    r"no longer accepting applications",
    r"this (position|role|job) (is )?no longer",
    r"this (?:job|role|position)(?: listing)? is closed\b(?!-)",
    r"job (listing )?not found",
    r"can't view this job because it's not available",
    r"the page you are looking for doesn.t exist",
    r"applications?\s+(?:(?:have|are|is)\s+)?closed",
    r"diese stelle (ist )?(nicht mehr|bereits) besetzt",
    r"deze vacature is niet meer gepubliceerd",
    r"offre (expiree|n'est plus disponible)",
    r"(cette )?offre n'est plus (disponible|en ligne|active)",
    r"(offre|poste|annonce) (est |a ete )?(deja )?pourvu(e)?",
    r"offre (cloturee|desactivee|terminee)",
    r"ce poste n'est plus (disponible|a pourvoir|ouvert)",
    r"recrutement (termine|cloture)",
    r"candidatures (closes|cloturees)",
]
LISTING_PAGE = [r"\d+\s+jobs?\s+found", r"search for jobs page is loaded", r"\d+\s+offres? (?:d'emploi )?trouvees?"]
SOFT_EXPIRED = [r"\bjob expired\b"]
BOT_CHALLENGE = [
    r"just a moment", r"performing security verification", r"checking your browser before",
    r"verify you are (a |not a )?human", r"enable javascript and cookies to continue", r"attention required.*cloudflare",
    r"\bray id\b", r"\bcf-ray\b", r"please complete the security check",
]
APPLY = [r"\bapply\b", r"\bpostuler\b", r"\bcandidater\b", r"\bbewerben\b", r"\bsolicitar\b", r"submit application",
         r"easy apply", r"start application", r"ich bewerbe mich", r"\bsollicit(?:eer|eren)\b"]
# "Applications close when the position is filled" states a condition, not a closure.
CONDITION_CLAUSE = re.compile(r"\b(?:until|till|once|when|whenever|after|before|unless|if|whether|as soon as|"
                              r"jusqu'a|des que|lorsque|quand|si) (?:\S+ ){0,9}\S+$", re.I)
MIN_CONTENT_CHARS = 300
JOB_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|\d{5,}", re.I)
_CONTROL_RE = re.compile(r"<(a|button)\b[^>]*>(.*?)</\1>|<input\b[^>]*\bvalue=\"([^\"]*)\"", re.S | re.I)

LABELS = {"active": "En ligne", "expired": "Fermée", "uncertain": "Non vérifiable"}


def normalize(text: str) -> str:
    text = re.sub(r"[‘’ʼ′´`]", "'", text or "")
    text = "".join(c for c in unicodedata.normalize("NFD", text) if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", text)


def _first(patterns: list, text: str):
    return next((p for p in patterns if re.search(p, text, re.I)), None)


def _stated(patterns: list, text: str):
    """A closure notice that is not part of a condition ("closed once filled")."""
    for pattern in patterns:
        for m in re.finditer(pattern, text, re.I):
            if not CONDITION_CLAUSE.search(text[:m.end()][-200:]):
                return pattern
    return None


def classify(status: int = 0, requested_url: str = "", final_url: str = "", body_text: str = "",
             apply_controls: list | None = None) -> dict:
    body = normalize(body_text)
    if status in (404, 410):
        return {"result": "expired", "code": "http_gone", "reason": f"HTTP {status}"}
    if pattern := _first(BOT_CHALLENGE, body):
        return {"result": "uncertain", "code": "bot_challenge", "reason": "page anti-robots"}
    if status in (403, 429, 503):
        return {"result": "uncertain", "code": "access_blocked", "reason": f"HTTP {status} (accès bloqué)"}
    if status >= 500:
        return {"result": "uncertain", "code": "server_error", "reason": f"HTTP {status}"}
    if re.search(r"[?&]error=true", final_url or "", re.I):
        return {"result": "expired", "code": "expired_url", "reason": f"redirigée vers {final_url}"}
    if pattern := _stated(HARD_EXPIRED, body):
        return {"result": "expired", "code": "expired_body", "reason": f"la page indique que l'offre est fermée ({pattern})"}
    ids = JOB_ID.findall(requested_url or "")
    if ids and final_url and ids[-1].lower() not in final_url.lower():
        return {"result": "uncertain", "code": "redirected_off_posting", "reason": f"redirigée vers {final_url}"}
    controls = [normalize(c) for c in apply_controls or []]
    if any(re.search(p, c, re.I) for c in controls for p in APPLY):
        return {"result": "active", "code": "apply_control_visible", "reason": "bouton « Postuler » présent"}
    if _first(SOFT_EXPIRED, body):
        return {"result": "expired", "code": "expired_body_soft", "reason": "la page indique « job expired »"}
    if _first(LISTING_PAGE, body):
        return {"result": "expired", "code": "listing_page", "reason": "renvoie vers une liste d'offres"}
    if len(body.strip()) < MIN_CONTENT_CHARS:
        # Mostly a page rendered by JavaScript: unreadable without a browser, not a closure.
        return {"result": "uncertain", "code": "insufficient_content", "reason": "page vide sans navigateur (JavaScript)"}
    return {"result": "uncertain", "code": "no_apply_control", "reason": "contenu présent mais pas de bouton « Postuler »"}


def apply_controls(page: str) -> list:
    """Text of the links, buttons and submit inputs of a page."""
    out = []
    for m in _CONTROL_RE.finditer(page or ""):
        text = re.sub(r"<[^>]+>", " ", m[2] or m[3] or "")
        text = re.sub(r"\s+", " ", text).strip()
        if text and len(text) <= 60:
            out.append(text)
    return out


def check(url: str) -> dict:
    """Fetch a posting page and classify it."""
    from .fetchers import page_text  # avoid a circular import at load time
    if not url:
        return {"result": "uncertain", "code": "no_url", "reason": "pas de lien"}
    try:
        response = http.get(url, retries=1)
        status, final, page = response.status_code, response.url, response.text
    except requests.HTTPError as e:
        response = e.response
        status = response.status_code if response is not None else 0
        final = response.url if response is not None else ""
        page = response.text if response is not None else ""
    except requests.RequestException as e:
        return {"result": "uncertain", "code": "network", "reason": f"injoignable ({type(e).__name__})"}
    return classify(status, url, final, page_text(page, limit=40000), apply_controls(page))
