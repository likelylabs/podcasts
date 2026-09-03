"""
Shared plumbing for the podcasts harvester: paths, market config, and one
polite HTTP client (serial, paced, backoff, budget, circuit breaker) — the
same client as likelylabs/replay.

Stdlib only for the pipeline; PyYAML is used only to read seeds.yaml /
blocklist.yaml (preinstalled on GitHub's ubuntu runners; `pip install pyyaml`
locally if missing).
"""
import datetime as dt
import hashlib
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DATA = REPO / "data"
SHOWS_PATH = DATA / "shows.json"           # resolved + validated show pool
CHARTS_DIR = DATA / "charts"               # per-market chart + keyword results
RTHK_PATH = DATA / "rthk.json"             # REPLAY-derived programmes + episode titles
LAST_RUN_PATH = DATA / "last-run.json"
SEEDS_PATH = REPO / "seeds.yaml"
BLOCKLIST_PATH = REPO / "blocklist.yaml"
INDEX_PATH = REPO / "index.json"
SEARCH_PATH = REPO / "search.json"

CATALOG_VERSION = 1
HKT = dt.timezone(dt.timedelta(hours=8))

# Storefront + the keyword harvest that stands in for Apple's missing language
# filter (PODCASTS.md §2.1.2). The keyword shelf is market-specific.
MARKETS = {
    "hk": {"cc": "hk", "terms": ["廣東話", "粵語", "香港"],
           "keyword_shelf": {"id": "cantonese", "title_zh": "廣東話", "title_en": "Cantonese"}},
    "tw": {"cc": "tw", "terms": ["台灣", "台灣 podcast"],
           "keyword_shelf": {"id": "local", "title_zh": "台灣節目", "title_en": "Taiwan"}},
    "sg": {"cc": "sg", "terms": ["新加坡", "华语"],
           "keyword_shelf": {"id": "local", "title_zh": "本地華語", "title_en": "Local"}},
    "my": {"cc": "my", "terms": ["马来西亚", "华语"],
           "keyword_shelf": {"id": "local", "title_zh": "本地華語", "title_en": "Local"}},
}
CHART_LIMIT = 100
TOP_SHELF_SIZE = 50
TOP_SHELF_ZH_ONLY = True        # the section promises Chinese-language shows
FRESH_DAYS = 90                 # a show whose latest episode is older is dead
MAX_FAIL_STREAK = 3             # consecutive failed validations before a show drops
INGEST_DEAD_DAYS = 180          # Apple says the newest episode predates this → never ingest.
                                # Twice FRESH_DAYS, so Apple metadata lag cannot bury a live show.
FORGET_FAIL_STREAK = 12         # an unreachable feed is forgotten after this many days
FEED_MAX_BYTES = 30 * 1024 * 1024   # TED/FT-sized back-catalogues run 6–20 MB
SEARCH_BUDGET_BYTES = 1500 * 1024   # search.json cap (PODCASTS §3.3)
INDEX_BUDGET_BYTES = 400 * 1024     # soft warning threshold for index.json

APPLE = "https://itunes.apple.com"
PODCASTINDEX = "https://api.podcastindex.org/api/1.0"
UA = "LikelyLabsPodcastsHarvester/1.0 (+https://likelylabs.com; catalog crawler, contact hello@likelylabs.com)"
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
              "(KHTML, like Gecko) Version/17.5 Safari/605.1.15")


class BudgetExhausted(Exception):
    pass


class CircuitOpen(Exception):
    pass


class Client:
    def __init__(self, pace_s=0.7, budget=3000, breaker_ratio=0.5, breaker_min=60, log=print):
        self.pace_s, self.budget = pace_s, budget
        self.requests = self.failures = 0
        self.breaker_ratio, self.breaker_min = breaker_ratio, breaker_min
        self.log = log
        self._last = 0.0

    def _pace(self):
        wait = self.pace_s - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def get(self, url, headers=None, method="GET", timeout=30, tries=2, max_bytes=None, ua=UA):
        """(status, content_type, body, final_url). 404/410 return at once;
        other non-200s and transport errors retry with backoff."""
        if self.requests >= self.budget:
            raise BudgetExhausted(f"request budget {self.budget} exhausted")
        hdrs = {"User-Agent": ua, "Accept": "*/*"}
        hdrs.update(headers or {})
        last = (-1, "", b"", url)
        for attempt in range(tries):
            self._pace()
            self.requests += 1
            req = urllib.request.Request(url, method=method, headers=hdrs)
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    body = b"" if method == "HEAD" else (r.read(max_bytes + 1) if max_bytes else r.read())
                    if max_bytes and len(body) > max_bytes:
                        return (413, "", b"", r.geturl())
                    return (r.status, r.headers.get("Content-Type", ""), body, r.geturl())
            except urllib.error.HTTPError as e:
                last = (e.code, "", b"", url)
                if e.code in (404, 410, 401, 403):
                    return last
            except Exception as e:
                last = (-1, repr(e), b"", url)
            self.failures += 1
            if self.requests >= self.breaker_min and self.failures / self.requests > self.breaker_ratio:
                raise CircuitOpen(f"{self.failures}/{self.requests} requests failed")
            if attempt < tries - 1:
                time.sleep(2 ** (attempt + 1))
        return last

    def get_json(self, url, headers=None, ua=UA):
        st, _, body, _ = self.get(url, headers, ua=ua)
        if st != 200:
            return st, None
        try:
            return st, json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return st, None


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path, obj, compact=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if compact:
        text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    else:
        text = json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=True)
    path.write_text(text + "\n", encoding="utf-8")


def read_yaml(path, default=None):
    try:
        import yaml
    except ImportError:
        raise SystemExit("PyYAML is required to read seeds.yaml/blocklist.yaml: pip install pyyaml")
    try:
        return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or default
    except OSError:
        return default


def show_id_for(itunes_id=None, feed_url=None):
    if itunes_id:
        return f"itunes_{int(itunes_id)}"
    return "feed_" + hashlib.sha1(feed_url.strip().encode()).hexdigest()[:12]


def has_cjk(s):
    return any("一" <= c <= "鿿" or "㐀" <= c <= "䶿" for c in (s or ""))


def now_hkt():
    return dt.datetime.now(HKT)


def log(msg):
    print(msg, flush=True)
