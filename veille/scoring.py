"""Relevance, profile match and chance estimation for internship offers.

Everything here is a transparent heuristic: each score comes with the reasons that produced it,
so the UI can show why an offer ranks where it does.
"""
import re
from dataclasses import dataclass, field
from datetime import date

from . import geo

TIER_ORDER = {"S": 0, "A": 1, "B": 2, "C": 3}
TIER_LABELS = {"S": "Élite", "A": "Très prestigieux", "B": "Solide", "C": "Correct"}

# --- internship detection -------------------------------------------------------------------

# Working-student (Werkstudent) jobs are part-time positions, not end-of-studies internships.
# Banks call internships "Summer Analyst" or "Off-cycle"; CERN calls them "Technical Student".
_INTERN_RE = re.compile(
    r"\b(interns?|internships?|stagiaires?|praktikum|praktikant(?:in)?|trainee(?:ship)?s?|"
    r"thesis|masterarbeit|tfe|pfe|co-?op|summer (?:analyst|associate)|off-cycle|technical student)\b"
    r"|(?<![-\w])stages?\b",
    re.I,
)
_EXCLUDE_RE = re.compile(
    r"alternance|alternant|apprenti|apprenticeship|work[- ]study|duales studium|ausbildung|"
    r"stage de 3e|high school|lyc[ée]en",
    re.I,
)
# Cheap title filter applied before fetching full descriptions.
# ", IA" is the US state code for Iowa ("Peosta, IA"), not "intelligence artificielle".
_BROAD_RE = re.compile(
    r"data|\bai\b|\ba\.i\.|\bki\b|(?<!, )\bia\b|\bml\b|machine|learning|deep|neural|\bvision|\bnlp\b|\bllm|"
    r"language model|research|scien|statist|quant|analytic|algorithm|model|intelligen|forecast|"
    r"robot|perception|recommend|generative|g[ée]n[ée]rati|apprentissage|donn[ée]es|daten|speech|"
    r"signal|optimi[sz]|econometr|actuar|bayes|probabil|computer vision|recherche",
    re.I,
)


def looks_like_internship(title: str) -> bool:
    return bool(_INTERN_RE.search(title or "")) and not _EXCLUDE_RE.search(title or "")


def title_maybe_relevant(title: str) -> bool:
    return looks_like_internship(title) and bool(_BROAD_RE.search(title or ""))


# --- categories -----------------------------------------------------------------------------

CATEGORY_TERMS = {
    "DL": [
        r"deep learning", r"apprentissage profond", r"neural networks?", r"r[ée]seaux? de neurones",
        r"computer vision", r"vision par ordinateur", r"\bnlp\b", r"natural language",
        r"traitement (?:automatique )?du langage", r"\bllms?\b", r"large language models?",
        r"transformers?\b", r"generative", r"g[ée]n[ée]rati(?:f|ve)", r"diffusion models?",
        r"foundation models?", r"reinforcement learning", r"speech", r"multimodal", r"pytorch",
        r"tensorflow", r"\bjax\b", r"\bcnns?\b", r"\bvlms?\b",
    ],
    "ML": [
        r"machine learning", r"apprentissage automatique", r"\bml\b", r"\bai\b",
        r"artificial intelligence", r"intelligence artificielle", r"(?<!, )\bia\b", r"applied scientist",
        r"research scientist", r"recommend(?:er|ation)", r"syst[èe]mes? de recommandation",
        r"mlops", r"data scien(?:ce|tist)", r"scikit-learn", r"xgboost", r"predictive model",
        r"mod[èe]les? pr[ée]dictifs?", r"\bki\b",
    ],
    "Stats": [
        r"statistic", r"statistique", r"econometr", r"quantitative research", r"\bquant\b",
        r"biostatisti", r"causal inference", r"inf[ée]rence causale", r"time[- ]series",
        r"s[ée]ries temporelles", r"forecasting", r"pr[ée]vision", r"a/b test", r"experimentation",
        r"bayesian", r"bay[ée]sien", r"probabilis", r"actuar",
    ],
}
_CATEGORY_RES = {cat: [re.compile(t, re.I) for t in terms] for cat, terms in CATEGORY_TERMS.items()}

# Business roles that merely mention AI (purchasing, SEO, consulting...) are not ML internships,
# unless the title also names a technical job.
_NON_TECH_TITLE_RE = re.compile(
    r"achats?|purchas|procurement|marketing|\bseo\b|sales|ventes?\b|commercial|consult|gestion de projet|"
    r"project manag|coordination|qualit[ée]|quality|supply|cha[iî]ne d['’]approvisionnement|communication|"
    r"\brh\b|\bhr\b|human resources|finance|legal|juridique|business develop|lean|am[ée]lioration continue|"
    r"\bgtm\b|go-to-market|content|graphi|retouche|community|social media|copywrit|\bsdr\b|customer success|"
    r"account (?:manager|executive)",
    re.I,
)
_TECH_TITLE_RE = re.compile(
    r"machine learning|deep learning|\bml\b|data scien|scientist|engineer|ing[ée]nieur|developer|"
    r"d[ée]veloppeur|computer vision|\bnlp\b|\bllm|algorithm|statisti|biostat|quant|neural|research",
    re.I,
)


def is_technical_title(title: str) -> bool:
    title = title or ""
    if not _NON_TECH_TITLE_RE.search(title):
        return True
    # "Value Engineering" is a consulting practice, not an engineering job.
    return bool(_TECH_TITLE_RE.search(re.sub(r"value engineering", "", title, flags=re.I)))


def classify(title: str, description: str) -> list:
    """Categories (ML / DL / Stats) ordered by strength; empty list means not relevant."""
    if not is_technical_title(title):
        return []
    scored = []
    for cat, regexes in _CATEGORY_RES.items():
        in_title = sum(1 for r in regexes if r.search(title or ""))
        in_desc = sum(1 for r in regexes if r.search(description or ""))
        strength = 3 * in_title + in_desc
        if in_title or in_desc >= 3:
            scored.append((strength, cat))
    return [cat for _, cat in sorted(scored, reverse=True)]


# --- skills vocabulary ----------------------------------------------------------------------

@dataclass(frozen=True)
class Skill:
    label: str
    pattern: str
    weight: int = 1
    case_sensitive: bool = False


VOCAB = [
    Skill("Python", r"\bpython\b", 3),
    Skill("PyTorch", r"pytorch", 3),
    Skill("TensorFlow", r"tensorflow|keras", 2),
    Skill("JAX", r"\bjax\b", 2),
    Skill("Scikit-learn", r"scikit-learn|sklearn", 2),
    Skill("Hugging Face", r"hugging ?face", 2),
    Skill("Pandas / NumPy", r"\bpandas\b|\bnumpy\b", 1),
    Skill("Deep learning", r"deep learning|apprentissage profond|neural networks?|r[ée]seaux? de neurones", 3),
    Skill("Machine learning", r"machine learning|apprentissage automatique", 3),
    Skill("NLP", r"\bnlp\b|natural language|traitement (?:automatique )?du langage|language models?|\bllms?\b", 3),
    Skill("Computer vision", r"computer vision|vision par ordinateur|object detection|image segmentation|image classification", 3),
    Skill("Recommender systems", r"recommend(?:er|ation) systems?|syst[èe]mes? de recommandation|recsys", 3),
    Skill("Time series", r"time[- ]series|s[ée]ries temporelles|forecasting|pr[ée]vision", 3),
    Skill("Statistics", r"statistic|statistique", 3),
    Skill("Probabilistic modeling", r"probabilis|uncertainty|incertitude|bayesian|bay[ée]sien|conformal", 2),
    Skill("Causal inference", r"causal inference|inf[ée]rence causale|causality|causalit[ée]", 2),
    Skill("A/B testing", r"a/b test|\bab test|experimentation|test a/b", 2),
    Skill("Optimization", r"optimi[sz]ation|optimisation", 1),
    Skill("Transformers", r"\btransformers?\b", 2),
    Skill("Generative AI / LLM", r"generative ai|\bgenai\b|ia g[ée]n[ée]rative|\brag\b|retrieval[- ]augmented|agentic", 2),
    Skill("Multimodal", r"multimodal|vision[- ]language", 2),
    Skill("MLOps", r"mlops|model deployment|d[ée]ploiement de mod[èe]les|ml pipelines?", 2),
    Skill("SQL", r"\bsql\b", 2),
    Skill("Git", r"\bgit\b|github", 1),
    Skill("Linux", r"\blinux\b|\bunix\b|\bbash\b", 1),
    Skill("Java", r"\bjava\b(?!script)", 1),
    Skill("FastAPI", r"fastapi", 1),
    Skill("PostgreSQL", r"postgres", 1),
    Skill("AWS", r"\baws\b|amazon web services", 1),
    Skill("GCP", r"\bgcp\b|google cloud", 1),
    Skill("Distributed systems", r"distributed (?:systems|training|computing)|syst[èe]mes distribu[ée]s", 1),
    Skill("Data visualization", r"data visuali[sz]ation|visualisation de donn[ée]es|tableau\b|power ?bi", 1),
    Skill("SAS", r"\bSAS\b", 1, case_sensitive=True),
    Skill("R", r"(?<![\w-])R(?=[,/ ]+(?:Python|SQL|Julia|Matlab|Stata))|(?<=Python, )R\b|\bR (?:programming|language)", 1, case_sensitive=True),
    Skill("C++", r"c\+\+", 2),
    Skill("CUDA", r"\bcuda\b", 2),
    Skill("Docker", r"\bdocker\b", 1),
    Skill("Kubernetes", r"kubernetes|\bk8s\b", 1),
    Skill("Spark", r"\bspark\b|pyspark", 2),
    Skill("Airflow", r"airflow", 1),
    Skill("Azure", r"\bazure\b", 1),
    Skill("Scala", r"\bscala\b", 1),
    Skill("Rust", r"\brust\b", 1),
    Skill("JavaScript / TypeScript", r"javascript|typescript", 1),
    Skill("Reinforcement learning", r"reinforcement learning|apprentissage par renforcement", 2),
    Skill("Robotics", r"robotics|robotique|\bros2?\b", 1),
    Skill("Signal processing", r"signal processing|traitement du signal", 1),
    Skill("MATLAB", r"matlab", 1),
]
VOCAB_BY_LABEL = {s.label: s for s in VOCAB}
_VOCAB_RES = [(s, re.compile(s.pattern, 0 if s.case_sensitive else re.I)) for s in VOCAB]


def extract_skills(text: str) -> list:
    return [s.label for s, regex in _VOCAB_RES if regex.search(text or "")]


def match_score(text: str, have: dict) -> tuple:
    """Return (score 0-100, matched labels, missing labels) for an offer against the profile.

    The score is the weighted share of the offer's skills the profile covers, pulled towards 50
    when the offer names few skills (little evidence either way).
    """
    found = extract_skills(text)
    matched = [label for label in found if label in have]
    missing = [label for label in found if label not in have]
    matched_w = sum(have[label] for label in matched)
    missing_w = sum(VOCAB_BY_LABEL[label].weight for label in missing)
    total = matched_w + missing_w
    if total == 0:
        return 50, matched, missing
    coverage = 100 * matched_w / total
    confidence = min(1.0, total / 10)
    return round(50 + (coverage - 50) * confidence), matched, missing


# --- chance estimation ----------------------------------------------------------------------

_PHD_RE = re.compile(
    r"(?:enrolled|pursuing|candidate)[^.\n]{0,40}ph\.?\s?d|ph\.?\s?d\.?\s*(?:student|candidate|program|intern)"
    r"|doctoral (?:student|candidate)|doctorant|\bphd\b[^.\n]{0,30}required",
    re.I,
)
_MASTER_RE = re.compile(
    r"master'?s?\b|\bmsc\b|m\.sc|bac\s*\+\s*5|fin d['’][ée]tudes|end[- ]of[- ]studies|final[- ]year|"
    r"engineering school|[ée]cole d['’]ing[ée]nieurs?|graduate student",
    re.I,
)
_US_AUTH_RE = re.compile(
    r"authori[sz]ed to work in the (?:us|u\.s\.|united states)|us work authori[sz]ation|"
    r"u\.s\. citizenship|green card|us person",
    re.I,
)
_LONG_RE = re.compile(r"\b(?:5|6)\s*(?:-|to|à)?\s*(?:6)?\s*(?:months?|mois)\b|six months|6-month", re.I)
_SHORT_TITLE_RE = re.compile(r"\bsummer\b|\b[ée]t[ée]\b|\b[1-3]\s*(?:mois|months?)\b", re.I)
_SHORT_RE = re.compile(r"\b(?:8|10|12)\s*weeks?\b|\b3\s*(?:months?|mois)\b|summer intern|summer 20\d\d", re.I)
_CLASS_RE = re.compile(r"class of|graduat\w*|promo(?:tion)?\s+20", re.I)
_YEAR_RE = re.compile(r"\b20[2-3]\d\b")
_PART_TIME_RE = re.compile(r"werkstudent|working student", re.I)


def target_class_years(text: str) -> set:
    """Graduation years an offer targets ('Class of 2029', 'graduating between 2026 and 2027')."""
    years = set()
    for m in _CLASS_RE.finditer(text or ""):
        years.update(_YEAR_RE.findall(text[m.start():m.end() + 60]))
    return years


# Market-finance roles (trading, quant research, investment banking), excluded on request.
_MARKET_FINANCE_RE = re.compile(
    r"\btrad(?:er|ers|ing)\b|quantitative (?:research|researcher|analyst|trader|trading|developer|strateg)|"
    r"\bquants?\b|investment bank|banque d['’]investissement|asset management|hedge fund|\bm&a\b|"
    r"private equity|corporate finance|market making|front office|structured products|"
    r"summer analyst|off-cycle",
    re.I,
)


def is_market_finance_title(title: str) -> bool:
    return bool(_MARKET_FINANCE_RE.search(title or ""))


# --- offers out of reach for the profile (hidden when reglages.masquer_inaccessibles is on) ----------

_PHD_TITLE_RE = re.compile(r"\bph\.?\s?d\b|\bdoctora(?:l|te)\b|\bdoctorant", re.I)
_NON_PHD_TITLE_RE = re.compile(r"\b(?:M\.?S\.?c?|B\.?S\.?|masters?|bachelors?|undergrad\w*)\b", re.I)
# Degrees below a PhD accepted in the description ("Pursuing MS or PhD", "Master's students", "BS, MS, or PhD").
_NON_PHD_DESC_RE = re.compile(
    r"\b(?:M\.S\.?|MS|MSc|M\.Sc|B\.S\.?|BS|BSc)\b|(?i:master'?s?\b|bachelor|fin d['’][ée]tudes|bac\s*\+\s*5|"
    r"[ée]cole d['’]ing[ée]nieurs?|engineering school|non-phd|end[- ]of[- ]studies|final[- ]year|ingénieur)"
)
_THESIS_TITLE_RE = re.compile(r"\bth[eè]se\b|\bcifre\b|\bdoctorat\b|\bpost-?doc", re.I)
_NGT_RE = re.compile(r"(\w+)\s+national graduate trainee", re.I)
_EXPERIENCE_RE = re.compile(
    r"\b([3-9]|1\d)\s*\+?\s*(?:years?|ans)\s+(?:of\s+)?(?:professional\s+|industry\s+|relevant\s+|work\s+)?"
    r"(?:experience|d['’]exp[ée]rience)", re.I)


def phd_required(title: str, description: str) -> bool:
    """A PhD is required: the title names a PhD and no lower degree, or the description asks for a PhD
    without ever accepting a master's / MS / BS student."""
    if _PHD_TITLE_RE.search(title or "") and not _NON_PHD_TITLE_RE.search(title or ""):
        return True
    desc = description or ""
    asks_phd = _PHD_RE.search(desc) or re.search(r"doctora(?:te|l) degree", desc, re.I)
    return bool(asks_phd) and not _NON_PHD_DESC_RE.search(f"{title}\n{desc}")


def inaccessible_reason(title: str, description: str, grad_year: int = 2027) -> str | None:
    """Why the profile can never get this offer, or None. Deliberately narrow: a doubt keeps the offer."""
    text = f"{title}\n{description or ''}"
    if _THESIS_TITLE_RE.search(title or ""):
        return "poste de thèse ou de postdoc, pas un stage"
    if phd_required(title, description):
        return "réservé aux doctorants"
    m = _NGT_RE.search(title or "")
    if m and not re.fullmatch(r"french|fran[çc]ais\w*", m[1], re.I):
        return f"programme réservé aux ressortissants ({m[1]})"
    if _US_AUTH_RE.search(text):
        return "autorisation de travail américaine exigée"
    class_years = sorted(int(y) for y in target_class_years(text))
    if class_years and not class_years[0] <= grad_year <= class_years[-1]:
        return f"vise la promo {class_years[0]}"
    if m := _EXPERIENCE_RE.search(text):
        return f"{m[0]} demandés"
    return None


def is_candidate_title(title: str) -> bool:
    """Reject part-time working-student jobs and apprenticeships that slipped through collection."""
    if _EXCLUDE_RE.search(title or ""):
        return False
    return not (_PART_TIME_RE.search(title or "") and not _INTERN_RE.search(title or ""))
_START_2027_RE = re.compile(r"2027", re.I)
_FR_NATIONALITY_RE = re.compile(r"nationalit[ée] (?:fran[çc]aise|europ[ée]enne)|habilitation", re.I)

_STOPWORDS = {
    "fr": {"le", "la", "les", "des", "et", "pour", "avec", "nous", "vous", "une", "dans", "sur", "est"},
    "en": {"the", "and", "for", "with", "you", "our", "will", "are", "this", "your", "of", "to"},
    "de": {"und", "der", "die", "das", "mit", "für", "wir", "sie", "ist", "auf", "eine", "zu"},
    "nl": {"het", "een", "van", "voor", "wij", "met", "jij", "bij", "zijn", "naar", "ons"},
    "es": {"el", "los", "las", "para", "con", "una", "por", "del", "nuestro", "como"},
    "it": {"il", "della", "per", "con", "una", "che", "del", "sono", "nostro", "alla"},
    "sv": {"och", "att", "för", "med", "som", "vi", "är", "på", "en", "av"},
}
LANGUAGE_NAMES = {"de": "allemand", "nl": "néerlandais", "es": "espagnol", "it": "italien", "sv": "suédois"}
_REQUIRED_LANG_RE = {
    "de": re.compile(r"(?:fluent|business|native|very good|excellent)[^.\n]{0,25}german|german[^.\n]{0,25}(?:required|mandatory|must|fluen)|deutschkenntnisse", re.I),
    "nl": re.compile(r"(?:fluent|native)[^.\n]{0,25}dutch|dutch[^.\n]{0,25}(?:required|mandatory|must|fluen)", re.I),
    "es": re.compile(r"(?:fluent|native)[^.\n]{0,25}spanish|spanish[^.\n]{0,25}(?:required|mandatory|must)", re.I),
    "it": re.compile(r"(?:fluent|native)[^.\n]{0,25}italian|italian[^.\n]{0,25}(?:required|mandatory|must)", re.I),
}


def posting_language(text: str) -> str:
    words = re.findall(r"[a-zäöüßéèàçñ]+", (text or "").lower()[:4000])
    if len(words) < 30:
        return "en"
    counts = {lang: sum(1 for w in words if w in stop) for lang, stop in _STOPWORDS.items()}
    return max(counts, key=counts.get)


@dataclass
class Assessment:
    categories: list
    match: int
    matched: list
    missing: list
    chance: int
    reasons: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


BASE_CHANCE = {"S": 12, "A": 25, "B": 40, "C": 55}
# Even a perfect fit stays a long shot at the most selective employers.
MAX_CHANCE = {"S": 45, "A": 60, "B": 75, "C": 85}


def assess(title: str, description: str, countries: set, region: str, tier: str, profile: dict,
           start_date: str | None = None) -> Assessment:
    text = f"{title}\n{description}"
    categories = classify(title, description)
    match, matched, missing = match_score(text, profile["competences"])
    spoken = set(profile.get("langues", ["fr", "en"]))

    chance = BASE_CHANCE.get(tier, 40)
    reasons = [f"Base {TIER_LABELS.get(tier, tier)} : {chance} %"]
    warnings = []

    def adjust(delta: int, why: str) -> None:
        nonlocal chance
        chance += delta
        reasons.append(f"{'+' if delta >= 0 else ''}{delta} : {why}")

    adjust(round((match - 50) * 0.3), f"correspondance profil {match} %")

    phd = bool(_PHD_RE.search(text)) or bool(re.search(r"\bph\.?\s?d\b", title, re.I))
    master = bool(_MASTER_RE.search(text))
    if phd and not master:
        adjust(-25, "réservé aux doctorants")
        warnings.append("Doctorat demandé")
    elif phd:
        adjust(-5, "doctorants et masters acceptés")

    lang = posting_language(description)
    if lang not in spoken and lang in LANGUAGE_NAMES:
        adjust(-20, f"offre rédigée en {LANGUAGE_NAMES[lang]}")
        warnings.append(f"Offre en {LANGUAGE_NAMES[lang]}")
    else:
        for code, regex in _REQUIRED_LANG_RE.items():
            if code not in spoken and regex.search(description or ""):
                adjust(-20, f"{LANGUAGE_NAMES[code]} exigé")
                warnings.append(f"{LANGUAGE_NAMES[code].capitalize()} exigé")
                break

    if _US_AUTH_RE.search(text):
        adjust(-30, "autorisation de travail US exigée")
        warnings.append("Autorisation US")

    if "FR" in countries:
        adjust(5, "en France")
    elif region == "Monde":
        adjust(-15, "hors Europe (visa)")

    if _SHORT_TITLE_RE.search(title) or (_SHORT_RE.search(description or "") and not _LONG_RE.search(text)):
        adjust(-10, "stage d'été court, pas un stage de fin d'études de 6 mois")
        warnings.append("Stage d'été (court)")
    elif master or _LONG_RE.search(text):
        adjust(6, "stage master / 6 mois")

    grad_year = int(profile.get("annee_diplome", 2027))
    class_years = sorted(int(y) for y in target_class_years(text))
    # "graduating between September 2026 and March 2028" is a range that includes 2027.
    if class_years and not class_years[0] <= grad_year <= class_years[-1]:
        year = class_years[0]
        adjust(-15, f"vise la promo {year} (toi : {grad_year})")
        warnings.append(f"Promo {year}")
    if _START_2027_RE.search(text):
        adjust(4, "démarrage 2027")
    if _FR_NATIONALITY_RE.search(text):
        adjust(3, "nationalité française / habilitation (moins de concurrence)")

    if re.search(r"\b2026\b", title) and "2027" not in title:
        warnings.append("Cycle 2026")

    # An end-of-studies internship starts in the graduation year; earlier or later does not fit.
    period = start_period(start_date)
    # "Winter 2027" (North-American co-op term) and "hiver 2026" (Dec. 2026 - Feb. 2027) both fit.
    winter_ok = (start_date or "").startswith("hiver") and period and period[0] in (grad_year, grad_year + 1)
    if period and period[0] != grad_year and not winter_ok:
        adjust(-20, f"début {start_date} : hors de ton année de stage ({grad_year})")
        warnings.append(f"Début {start_date}")
    else:
        # Earliest possible start inside the graduation year ("debut_au_plus_tot": "2027-03"): an earlier
        # start is only possible if negotiated, so it costs chances without excluding the offer.
        earliest = _earliest_start(profile)
        effective = (grad_year, 1) if winter_ok else period
        if earliest and effective and effective[0] == earliest[0] and effective < earliest:
            month = _MONTH_NAMES_FR[earliest[1] - 1]
            adjust(-10, f"début {start_date} : avant {month} {earliest[0]}, seulement si négociable")
            warnings.append(f"Début {start_date} (avant {month})")

    cap = MAX_CHANCE.get(tier, 75)
    if chance > cap:
        reasons.append(f"plafonné à {cap} % (niveau de sélection {TIER_LABELS.get(tier, tier)})")
    chance = max(3, min(cap, chance))
    return Assessment(categories, match, matched, missing, chance, reasons, warnings)


# --- details read from the description: stipend, start date, application deadline -------------

_MONTHS = {
    "janvier": 1, "january": 1, "jan": 1, "février": 2, "fevrier": 2, "february": 2, "feb": 2, "mars": 3, "march": 3,
    "mar": 3, "avril": 4, "april": 4, "apr": 4, "mai": 5, "may": 5, "juin": 6, "june": 6, "jun": 6, "juillet": 7,
    "july": 7, "jul": 7, "août": 8, "aout": 8, "august": 8, "aug": 8, "septembre": 9, "september": 9, "sep": 9,
    "sept": 9, "octobre": 10, "october": 10, "oct": 10, "novembre": 11, "november": 11, "nov": 11, "décembre": 12,
    "decembre": 12, "december": 12, "dec": 12,
}
_MONTH_NAMES_FR = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."]
_MONTH_RE = "|".join(sorted(_MONTHS, key=len, reverse=True))
_SEASONS = {"spring": "printemps", "printemps": "printemps", "summer": "été", "été": "été", "autumn": "automne",
            "fall": "automne", "automne": "automne", "winter": "hiver", "hiver": "hiver"}
_AMOUNT = r"(\d{1,2}[  .,]?\d{3}|\d{3,4})"
_STIPEND_RES = [
    re.compile(_AMOUNT + r"\s?(?:€|eur(?:os)?)\s?(?:bruts?|gross|net)?\s?(?:/|par|per|a|an)\s?(?:mois|month)", re.I),
    re.compile(r"(?:€|eur)\s?" + _AMOUNT + r"\s?(?:gross\s|brut\s)?(?:/|per|par)\s?(?:month|mois)", re.I),
    re.compile(r"gratification[^.\n]{0,60}?" + _AMOUNT + r"\s?(?:€|eur)", re.I),
    re.compile(r"(?:£|gbp)\s?" + _AMOUNT + r"\s?(?:/|per)\s?month", re.I),
    re.compile(_AMOUNT + r"\s?(?:chf)\s?(?:/|per|par)\s?(?:month|mois)", re.I),
]
_START_RE = re.compile(
    r"(?:start(?:ing)?(?:\s+date)?|d[ée]but(?:\s+du\s+stage)?|date\s+de\s+d[ée]but|[àa]\s+partir\s+de|from|commencing)"
    r"\s*(?:[:\-–]|in|en|le|:)?\s*(?:le\s+)?(?:\d{1,2}(?:er)?\s+)?"
    rf"({_MONTH_RE}|{'|'.join(_SEASONS)})\.?\s+(20\d\d)", re.I)
_TITLE_START_RE = re.compile(rf"\b({_MONTH_RE}|{'|'.join(_SEASONS)})\.?\s+(202[6-9])\b", re.I)
_DEADLINE_RE = re.compile(
    r"(?:application\s+deadline|deadline|apply\s+(?:by|before)|closing\s+date|date\s+limite(?:\s+de\s+candidature)?|"
    r"candidatures?\s+(?:jusqu['’]au|avant\s+le))\s*[:\-–]?\s*"
    rf"(\d{{1,2}}(?:st|nd|rd|th|er)?\s+(?:{_MONTH_RE})\.?\s+20\d\d|(?:{_MONTH_RE})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+20\d\d|"
    r"\d{1,2}[/.]\d{1,2}[/.]20\d\d|20\d\d-\d\d-\d\d)", re.I)


def _format_amount(raw: str) -> str:
    digits = re.sub(r"\D", "", raw)
    return f"{int(digits):,}".replace(",", " ") if digits else raw


def _parse_date(text: str) -> str | None:
    text = text.lower().replace(",", " ")
    if m := re.fullmatch(r"(20\d\d)-(\d\d)-(\d\d)", text.strip()):
        return text.strip()
    if m := re.fullmatch(r"(\d{1,2})[/.](\d{1,2})[/.](20\d\d)", text.strip()):
        return f"{m[3]}-{int(m[2]):02d}-{int(m[1]):02d}"
    day = re.search(r"\b(\d{1,2})(?:st|nd|rd|th|er)?\b", text)
    month = next((n for name, n in _MONTHS.items() if re.search(rf"\b{name}\b", text)), None)
    year = re.search(r"20\d\d", text)
    if day and month and year:
        return f"{year[0]}-{month:02d}-{int(day[1]):02d}"
    return None

# Full month names only: "Mar", "May", "Mai" or "Mars" (the company) alone in a title are too ambiguous.
_FULL_MONTHS = [m for m in _MONTHS if len(m) > 4 or m in ("juin", "june", "july", "août", "aout")]
_TITLE_MONTH_ONLY_RE = re.compile(
    rf"\b({'|'.join(sorted(_FULL_MONTHS, key=len, reverse=True))})\b(?!\.?\s+20\d\d)", re.I)
_SEASON_MONTH = {"printemps": 4, "été": 7, "automne": 10, "hiver": 1}


def _reference_date(posted_at: str | None, today: date | None) -> date:
    try:
        return date.fromisoformat((posted_at or "")[:10])
    except ValueError:
        return today or date.today()


def _earliest_start(profile: dict) -> tuple | None:
    """(year, month) from reglages.debut_au_plus_tot ("2027-03"), or None when unset or malformed."""
    raw = str((profile.get("reglages") or {}).get("debut_au_plus_tot") or "")
    m = re.fullmatch(r"(20\d\d)-(0?[1-9]|1[0-2])", raw.strip())
    return (int(m[1]), int(m[2])) if m else None


def start_period(label: str | None) -> tuple | None:
    """(year, month) of a start date label such as "janv. 2027" or "été 2027"; "hiver 2026" counts as
    January 2027, since a winter runs into the next year."""
    if not label or not (m := re.fullmatch(r"(.+?)\s+(20\d\d)", label.strip())):
        return None
    word, year = m[1], int(m[2])
    if word in _SEASON_MONTH:
        return (year + 1, 1) if word == "hiver" else (year, _SEASON_MONTH[word])
    if word in _MONTH_NAMES_FR:
        return year, _MONTH_NAMES_FR.index(word) + 1
    return None


def extract_details(title: str, description: str, posted_at: str | None = None, today: date | None = None) -> dict:
    """Stipend, start date and application deadline when the offer states them."""
    text = description or ""
    stipend = None
    for i, regex in enumerate(_STIPEND_RES):
        if m := regex.search(text):
            unit = "£/mois" if i == 3 else "CHF/mois" if i == 4 else "€/mois"
            stipend = f"{_format_amount(m[1])} {unit}"
            break
    start = None
    # The title is updated for each hiring cycle; descriptions are often copied from last year's posting.
    m = _TITLE_START_RE.search(title or "") or _START_RE.search(text)
    if m:
        word, year = m[1].lower(), m[2]
        start = f"{_SEASONS[word]} {year}" if word in _SEASONS else f"{_MONTH_NAMES_FR[_MONTHS[word] - 1]} {year}"
    elif m := _TITLE_MONTH_ONLY_RE.search(title or ""):
        # "Data Scientist Internship (November, 6 months)": the next such month after publication.
        month = _MONTHS[m[1].lower()]
        ref = _reference_date(posted_at, today)
        year = ref.year if month >= ref.month else ref.year + 1
        start = f"{_MONTH_NAMES_FR[month - 1]} {year}"
    deadline = None
    if m := _DEADLINE_RE.search(text):
        deadline = _parse_date(m[1])
    return {"stipend": stipend, "start_date": start, "deadline": deadline}


def chance_label(chance: int) -> str:
    if chance >= 45:
        return "Bonne"
    if chance >= 20:
        return "Moyenne"
    return "Faible"


def is_visible(region: str, tier: str, french_intl: bool, chance: int, settings: dict) -> bool:
    """Outside Europe, only keep offers worth the move (prestigious or French international groups)."""
    if region != "Monde":
        return True
    if tier in ("S", "A") and chance >= settings.get("monde_chance_min_prestige", 30):
        return True
    return bool(french_intl) and chance >= settings.get("monde_chance_min_groupe_fr", 30)


def country_flags(countries) -> str:
    return " ".join(geo.flag(c) for c in sorted(countries))


# --- priority: which offers to apply to first --------------------------------------------------

# How much landing the internship is worth, by prestige; 💰 employers are worth a bit more.
TIER_VALUE = {"S": 100, "A": 85, "B": 65, "C": 45}
URGENT_DAYS = 14


def priority(tier: str, chance: int, match: int, high_pay: bool = False, deadline: str | None = None,
             today: date | None = None) -> tuple:
    """Return (score 0-100, reasons). Expected value of applying: what the internship is worth times
    the odds of getting it, weighted by fit, and pushed up when the deadline is close."""
    today = today or date.today()
    value = TIER_VALUE.get(tier, 60) + (10 if high_pay else 0)
    raw = value * (chance or 0) / 100 * (0.6 + 0.4 * (match or 0) / 100)
    reasons = [f"valeur {TIER_LABELS.get(tier, tier)}{' + 💰' if high_pay else ''} : {value}",
               f"× chances {chance} %", f"× correspondance {match} %"]
    if deadline:
        try:
            days = (date.fromisoformat(deadline[:10]) - today).days
        except ValueError:
            days = None
        if days is not None and days < 0:
            return 0, reasons + ["date limite dépassée"]
        if days is not None and days <= URGENT_DAYS:
            raw *= 1.25
            reasons.append(f"× 1,25 : date limite dans {days} j")
    # 50 is about the best realistic case (Élite at its cap, or Solide with a perfect fit).
    return round(min(100, raw * 2)), reasons


def priority_label(score: int) -> str:
    # Thresholds set on the October 2026 offers: about a third "Haute", a third "Moyenne".
    if score >= 75:
        return "Haute"
    if score >= 50:
        return "Moyenne"
    return "Basse"


# --- posting legitimacy (career-ops "Block G", the deterministic part) ---------------------------

LEGITIMACY_LABELS = ("Fiable", "Prudence", "Douteuse")


def legitimacy(posted_at: str | None, first_seen: str | None, description: str, skills: int, reposts: int,
               liveness: str | None = None, deadline: str | None = None, today: date | None = None) -> dict:
    """Is the posting likely a real, open internship? Observations with their innocent explanations; it never
    changes the chances or the priority. Returns {"label", "signals": [(kind, text)]} with kind ok / warn / bad."""
    today = today or date.today()
    signals = []
    published = posted_at or (first_seen or "")[:10]
    try:
        age = (today - date.fromisoformat(published[:10])).days if published else None
    except ValueError:
        age = None
    since = "publiée" if posted_at else "vue pour la première fois"
    if age is None:
        signals.append(("warn", "date de publication inconnue (rien d'anormal : beaucoup de sites ne la donnent pas)"))
    elif age <= 30:
        signals.append(("ok", f"{since} il y a {age} j"))
    elif age <= 60:
        signals.append(("warn", f"{since} il y a {age} j (fréquent pour les stages publiés tôt)"))
    elif posted_at:
        signals.append(("bad", f"{since} il y a {age} j : peut-être déjà pourvue, ou un vivier de candidatures"))
    else:
        signals.append(("warn", f"{since} il y a {age} j et toujours en ligne (vivier de candidatures ?)"))
    length = len(description or "")
    if length < 400:
        signals.append(("warn", "description très courte ou non récupérée"))
    elif skills == 0:
        signals.append(("warn", "aucune compétence technique précise citée (offre générique ?)"))
    else:
        signals.append(("ok", f"description précise ({skills} compétence(s) citée(s))"))
    if reposts >= 2:
        signals.append(("warn", f"republiée {reposts} fois en 90 jours (poste difficile à pourvoir, ou annonce permanente)"))
    closed = False
    if liveness == "expired":
        signals.append(("bad", "la page de l'offre indique qu'elle est fermée"))
        closed = True
    elif liveness == "active":
        signals.append(("ok", "bouton « Postuler » actif"))
    if deadline:
        try:
            if date.fromisoformat(deadline[:10]) < today:
                signals.append(("bad", "date limite dépassée"))
                closed = True
        except ValueError:
            pass
    bad = sum(1 for kind, _ in signals if kind == "bad")
    warn = sum(1 for kind, _ in signals if kind == "warn")
    # One concerning signal alone (an old posting at a big employer) is common: "Douteuse" needs a closure or
    # several signals together, as in career-ops ("multiple ghost indicators").
    label = "Douteuse" if closed or bad >= 2 or (bad and warn) or warn >= 3 else "Prudence" if bad or warn else "Fiable"
    return {"label": label, "signals": signals}
