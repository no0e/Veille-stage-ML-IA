"""HTTP helpers shared by the collectors (one session per thread, retries on transient errors)."""
import threading
import time
from urllib.parse import urlsplit

import requests

HEADERS = {
    "User-Agent": "Mozilla/5.0 (veille-stages; usage personnel)",
    "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
    # urllib3 fails to decode some zstd responses (amazon.jobs), so ask for gzip only.
    "Accept-Encoding": "gzip, deflate",
}
TIMEOUT = 20
RETRY_STATUSES = {429, 500, 502, 503, 504}

_local = threading.local()


def _session() -> requests.Session:
    if not hasattr(_local, "session"):
        _local.session = requests.Session()
        _local.session.headers.update(HEADERS)
    return _local.session


MAX_RETRY_AFTER = 30


class RateLimited(requests.HTTPError):
    pass


def _retry_delay(response: requests.Response, attempt: int) -> float:
    """Rate limits (429) get the server's Retry-After, or a longer wait than other transient errors."""
    if response.status_code != 429:
        return 2 * (attempt + 1)
    try:
        wait = float(response.headers.get("Retry-After", ""))
    except ValueError:
        return 10 * (attempt + 1)
    if wait > MAX_RETRY_AFTER:
        # A daily quota (Workable asks for ~24 h): waiting would only stall the collection.
        raise RateLimited(f"limite de requêtes atteinte, réessayer dans {wait / 3600:.0f} h "
                          f"(les offres déjà collectées sont conservées)", response=response)
    return wait


# Minimum gap between two requests to the same host. Workday answers 429 when a tenant is queried
# too fast (Accenture, Salesforce); spacing the requests costs a few seconds per source.
MIN_INTERVAL = {"myworkdayjobs.com": 1.0, "apply.workable.com": 1.0}
_host_lock = threading.Lock()
_host_next = {}


def _throttle(url: str) -> None:
    host = urlsplit(url).hostname or ""
    interval = next((gap for suffix, gap in MIN_INTERVAL.items() if host.endswith(suffix)), 0)
    if not interval:
        return
    with _host_lock:
        current = time.monotonic()
        slot = max(current, _host_next.get(host, 0.0))
        _host_next[host] = slot + interval
    time.sleep(slot - current)


def request(method: str, url: str, retries: int = 2, **kwargs) -> requests.Response:
    kwargs.setdefault("timeout", TIMEOUT)
    for attempt in range(retries + 1):
        _throttle(url)
        try:
            response = _session().request(method, url, **kwargs)
        except requests.RequestException:
            if attempt == retries:
                raise
            time.sleep(1.5 * (attempt + 1))
            continue
        if response.status_code in RETRY_STATUSES and attempt < retries:
            time.sleep(_retry_delay(response, attempt))
            continue
        response.raise_for_status()
        return response
    raise RuntimeError("unreachable")


def get(url: str, **kwargs) -> requests.Response:
    return request("GET", url, **kwargs)


def post(url: str, **kwargs) -> requests.Response:
    return request("POST", url, **kwargs)
