"""Turn free-text job locations into ISO country codes and a region (Europe / Monde / Inconnu)."""
import re
import unicodedata

EUROPE = {
    # EU 27
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE", "IT",
    "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE",
    # Also in scope: Switzerland, UK, EEA
    "CH", "GB", "NO", "IS", "LI",
}

COUNTRY_ALIASES = {
    "AT": ["austria", "autriche", "osterreich"],
    "BE": ["belgium", "belgique", "belgie"],
    "BG": ["bulgaria", "bulgarie"],
    "HR": ["croatia", "croatie"],
    "CY": ["cyprus", "chypre"],
    "CZ": ["czech republic", "czechia", "tchequie", "republique tcheque"],
    "DK": ["denmark", "danemark"],
    "EE": ["estonia", "estonie"],
    "FI": ["finland", "finlande"],
    "FR": ["france"],
    "DE": ["germany", "allemagne", "deutschland"],
    "GR": ["greece", "grece"],
    "HU": ["hungary", "hongrie"],
    "IE": ["ireland", "irlande"],
    "IT": ["italy", "italie", "italia"],
    "LV": ["latvia", "lettonie"],
    "LT": ["lithuania", "lituanie"],
    "LU": ["luxembourg"],
    "MT": ["malta", "malte"],
    "NL": ["netherlands", "the netherlands", "pays-bas", "pays bas", "nederland", "holland"],
    "PL": ["poland", "pologne", "polska"],
    "PT": ["portugal"],
    "RO": ["romania", "roumanie"],
    "SK": ["slovakia", "slovaquie"],
    "SI": ["slovenia", "slovenie"],
    "ES": ["spain", "espagne", "espana"],
    "SE": ["sweden", "suede", "sverige"],
    "CH": ["switzerland", "suisse", "schweiz"],
    "GB": ["united kingdom", "uk", "royaume-uni", "royaume uni", "england", "scotland", "wales",
           "great britain"],
    "NO": ["norway", "norvege"],
    "IS": ["iceland", "islande"],
    "US": ["united states", "usa", "etats-unis", "etats unis", "california", "massachusetts", "texas",
           "illinois", "washington state", "pennsylvania", "new jersey"],
    "MY": ["malaysia", "malaisie"],
    "VN": ["vietnam", "viet nam"],
    "RS": ["serbia", "serbie"],
    "UA": ["ukraine"],
    "PH": ["philippines"],
    "TH": ["thailand", "thailande"],
    "ID": ["indonesia", "indonesie"],
    "EG": ["egypt", "egypte"],
    "AR": ["argentina", "argentine"],
    "CL": ["chile", "chili"],
    "CO": ["colombia", "colombie"],
    "NZ": ["new zealand", "nouvelle-zelande"],
    "SA": ["saudi arabia", "arabie saoudite"],
    "QA": ["qatar"],
    "TN": ["tunisia", "tunisie"],
    "CA": ["canada"],
    "JP": ["japan", "japon"],
    "SG": ["singapore", "singapour"],
    "IN": ["india", "inde"],
    "CN": ["china", "chine"],
    "IL": ["israel"],
    "AE": ["united arab emirates", "uae"],
    "AU": ["australia", "australie"],
    "KR": ["south korea", "korea", "coree"],
    "BR": ["brazil", "bresil"],
    "MX": ["mexico", "mexique"],
    "HK": ["hong kong"],
    "TW": ["taiwan"],
    "MA": ["morocco", "maroc"],
    "TR": ["turkey", "turquie"],
}

CITIES = {
    "FR": ["paris", "lyon", "toulouse", "grenoble", "rennes", "nice", "sophia antipolis", "bordeaux",
           "lille", "nantes", "marseille", "montpellier", "strasbourg", "saclay", "palaiseau",
           "velizy", "massy", "issy-les-moulineaux", "boulogne-billancourt", "la defense",
           "courbevoie", "puteaux", "nanterre", "valbonne", "biot", "cannes", "sophia",
           "guyancourt", "elancourt", "marignane", "blagnac", "colomiers", "bruz"],
    # Cambridge is left out on purpose: "Cambridge, MA" is as common as Cambridge, UK.
    "GB": ["london", "oxford", "edinburgh", "manchester", "bristol", "belfast", "glasgow"],
    "DE": ["berlin", "munich", "munchen", "hamburg", "frankfurt", "stuttgart", "cologne", "koln",
           "aachen", "heidelberg", "tubingen", "dresden", "dusseldorf", "karlsruhe", "nuremberg",
           "ulm", "bremen", "hanover", "leipzig", "ingolstadt", "wolfsburg", "darmstadt"],
    "NL": ["amsterdam", "eindhoven", "delft", "rotterdam", "utrecht", "the hague", "den haag",
           "leiden", "veldhoven", "nijmegen", "groningen"],
    "CH": ["zurich", "geneva", "geneve", "lausanne", "basel", "bern", "zug", "lugano"],
    "ES": ["madrid", "barcelona", "valencia", "seville", "malaga", "bilbao"],
    "PT": ["lisbon", "lisboa", "porto"],
    "IT": ["milan", "milano", "rome", "roma", "turin", "torino", "bologna", "naples", "pisa"],
    "BE": ["brussels", "bruxelles", "leuven", "louvain", "ghent", "gent", "antwerp", "anvers",
           "liege", "mechelen"],
    "LU": ["luxembourg city"],
    "AT": ["vienna", "wien", "graz", "linz", "innsbruck"],
    "CZ": ["prague", "praha", "brno"],
    "PL": ["warsaw", "warszawa", "krakow", "cracovie", "wroclaw", "gdansk", "poznan"],
    "HU": ["budapest"],
    "RO": ["bucharest", "bucarest", "cluj"],
    "BG": ["sofia"],
    "GR": ["athens", "athenes"],
    "IE": ["dublin", "cork", "galway"],
    "SE": ["stockholm", "gothenburg", "goteborg", "malmo", "lund"],
    "DK": ["copenhagen", "copenhague", "aarhus"],
    "NO": ["oslo", "trondheim"],
    "FI": ["helsinki", "espoo", "tampere"],
    "EE": ["tallinn"],
    "LV": ["riga"],
    "LT": ["vilnius"],
    "HR": ["zagreb"],
    "SI": ["ljubljana"],
    "SK": ["bratislava"],
    "MT": ["valletta"],
    "US": ["new york", "san francisco", "seattle", "mountain view", "palo alto", "menlo park",
           "sunnyvale", "san jose", "los angeles", "boston", "austin", "chicago", "pittsburgh",
           "redmond", "bellevue", "washington dc", "atlanta", "denver", "san diego", "santa clara",
           "cupertino", "north reading", "miami"],
    "CA": ["toronto", "montreal", "vancouver", "waterloo", "ottawa"],
    "JP": ["tokyo", "osaka"],
    "SG": [],
    "IN": ["bangalore", "bengaluru", "hyderabad", "pune", "mumbai", "chennai", "gurgaon", "noida"],
    "CN": ["beijing", "shanghai", "shenzhen", "hangzhou"],
    "IL": ["tel aviv", "haifa", "jerusalem"],
    "AE": ["dubai", "abu dhabi"],
    "AU": ["sydney", "melbourne"],
    "KR": ["seoul"],
    "BR": ["sao paulo"],
    "TW": ["taipei"],
    "MY": ["kuala lumpur", "penang"],
    "VN": ["hanoi", "ho chi minh"],
    "RS": ["belgrade", "novi sad"],
    "UA": ["kyiv", "kiev", "lviv"],
}

EUROPE_GENERIC = ["emea", "europe", "european union", "union europeenne", "eu remote", "remote eu"]

ISO3 = {"FRA": "FR", "DEU": "DE", "GBR": "GB", "USA": "US", "ESP": "ES", "ITA": "IT", "NLD": "NL",
        "CHE": "CH", "BEL": "BE", "POL": "PL", "IRL": "IE", "SWE": "SE", "LUX": "LU", "CAN": "CA",
        "IND": "IN", "JPN": "JP", "SGP": "SG", "AUT": "AT", "PRT": "PT", "DNK": "DK", "ROU": "RO",
        "CZE": "CZ", "FIN": "FI", "NOR": "NO", "ISR": "IL", "CHN": "CN"}

KNOWN_ISO2 = set(COUNTRY_ALIASES)


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return text.lower()


def _compile(words):
    alternation = "|".join(sorted((re.escape(w) for w in words), key=len, reverse=True))
    return re.compile(rf"(?<![a-z]){'(?:' + alternation + ')'}(?![a-z])")


_ALIAS_RES = {iso: _compile(names) for iso, names in COUNTRY_ALIASES.items() if names}
_CITY_RES = {iso: _compile(names) for iso, names in CITIES.items() if names}
_EUROPE_GENERIC_RE = _compile(EUROPE_GENERIC)


def _codes(location: str) -> set:
    """ISO codes written as codes, e.g. 'ES, Barcelona' or 'Barcelona, Catalonia, ESP'.

    Two-letter codes only count as the leading token: elsewhere they are usually US states
    ('Mountain View, CA' or 'Wilmington, DE'), not countries.
    """
    tokens = [t.strip() for t in re.split(r"[,/;|()\-–]+|\s{2,}", location or "") if t.strip()]
    found = {ISO3[t] for t in tokens if t in ISO3}
    if tokens and tokens[0] in KNOWN_ISO2 and tokens[0].isupper():
        found.add(tokens[0])
    return found


US_STATES = ("AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC "
             "ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC").split()
_US_STATE_RE = re.compile(rf",\s*(?:{'|'.join(US_STATES)})\b(?!\s*,?\s*[A-Za-z]{{3,}})")


def detect_countries(location: str) -> set:
    text = normalize(location)
    found = _codes(location)
    for iso, regex in _ALIAS_RES.items():
        if regex.search(text):
            found.add(iso)
    for iso, regex in _CITY_RES.items():
        if regex.search(text):
            found.add(iso)
    # "Greenwich, CT": a trailing US state code when nothing else identified the country.
    if not found and _US_STATE_RE.search(location or ""):
        found.add("US")
    return found


def region_for(location: str, countries: set) -> str:
    if countries & EUROPE:
        return "Europe"
    if countries:
        return "Monde"
    if _EUROPE_GENERIC_RE.search(normalize(location)):
        return "Europe"
    return "Inconnu"


def flag(iso: str) -> str:
    if len(iso) != 2 or not iso.isalpha():
        return ""
    return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in iso.upper())


# --- clean-up of location strings ----------------------------------------------------------------

# Workday "bulletFields" mix the place with the contract type, the job family and the legal entity:
# "Lille, France, Intern/Trainee (Fixed Term) (Trainee), 20 - SOFTWARE, Thales SIX GTS France S.A.S."
_LOCATION_NOISE_RE = re.compile(
    r"^\d{1,3}\s*-\s"                                    # job family: "21 - ENGINEERING AND ..."
    r"|intern|trainee|stagiaire|fixed term|permanent|full[- ]time|part[- ]time|^cw\b|temporary|contract\b"
    r"|location negotiable|multiple locations?$"
    r"|(?:^|\s)(?:s\.?a\.?s\.?u?|s\.a\.|gmbh|ltd\.?|limited|inc\.?|s\.?p\.?a\.?|b\.?v\.?|plc|llc|s\.?r\.?l\.?|ag)$",
    re.I,
)


def clean_location(location: str) -> str:
    """Drop the non-place parts of a location and repeated names, keeping their order."""
    parts = [p.strip() for p in (location or "").split(",")]
    kept = [p for p in parts if p and not _LOCATION_NOISE_RE.search(p)]
    return ", ".join(dict.fromkeys(kept))


_LEGAL_WORDS = r"\b(sa|sas|sasu|se|group|groupe|inc|ltd|limited|gmbh|ag|bv|nv|plc|llc|france|paris|europe|the)\b"


def company_key(name: str) -> str:
    """Comparable company name: "Thales SIX GTS France SAS" and "thales six gts" give the same key."""
    text = normalize(name or "")
    text = re.sub(r"\(.*?\)", " ", text)
    text = re.sub(_LEGAL_WORDS, " ", text)
    return re.sub(r"[^a-z0-9]+", " ", text).strip()
