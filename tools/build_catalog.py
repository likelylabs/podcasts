#!/usr/bin/env python3
"""
PROCESS leg: data/ → index.json + search.json (PODCASTS.md §3) behind the
publish gate (§2.3). The ONLY writer of those two files (tenet #5). No
network.

Shelves per market: top (Apple chart, Chinese-language), the market's keyword
shelf (Cantonese for hk), curated picks (seeds.yaml), trending (Podcast Index,
when present) and — hk only — one RTHK shelf per channel from the REPLAY
catalog, flagged gate:"replay" so the app renders them only when ReplayGate
allows (§5.4a).

Gate: a show must have validated today-or-recently with < MAX_FAIL_STREAK
consecutive failures and not be blocklisted; every market's top shelf must
keep ≥ MIN_TOP shows; the show pool must be ≥ 80% of last-good — a floor that
relaxes as last-good ages so the gate cannot wedge itself shut, see
effective_floor(); search.json must fit the size budget (RTHK episodes trimmed
oldest-month-first).

Also writes show/{showId}.json — our own copy of each open-RSS catalog show's
newest SHOW_LIST_MAX episodes (metadata only: title, date, publisher enclosure
URL, duration, guid), so a client whose publisher feed is unreachable (mainland
China) still gets a list (SR-04; schema/show.schema.json). Written only when
the gate passes; a show that leaves the catalog loses its file in the same run.
"""
import argparse
import datetime as dt
import json
import re
import sys
import urllib.parse
from pathlib import Path

from common import (BLOCKLIST_PATH, CATALOG_VERSION, CHARTS_DIR, EPISODES_DIR, INDEX_BUDGET_BYTES,
                    INDEX_PATH, LAST_RUN_PATH, MARKETS, MAX_FAIL_STREAK, RTHK_PATH, SEARCH_BUDGET_BYTES,
                    SEARCH_PATH, SEEDS_PATH, SHOW_DIR, SHOW_ID_RE, SHOW_LIST_MAX, SHOWS_PATH,
                    TOP_SHELF_SIZE, TOP_SHELF_ZH_ONLY, has_cjk, log, now_hkt, read_json, read_yaml)

MIN_TOP = 10
FLOOR_SHOWS = 0.80
FLOOR_RELAX_PER_DAY = 0.10     # ...loosened per day that last-good goes unrefreshed
FLOOR_SHOWS_MIN = 0.50         # ...but never below this
STALE_VALIDATION_DAYS = 7      # a show unvalidated for this long is dropped


def fail(msg):
    print(f"GATE FAILED — catalog NOT written: {msg}", file=sys.stderr)
    return 1


def effective_floor(last, today):
    """The safety floor, relaxed by how long last-good has stood unrefreshed.

    The floor measures this run against index.json, which the gate only rewrites
    when it passes. A flat floor therefore wedges shut on any genuine one-time
    contraction: a cohort of dead feeds ageing out together shrinks the catalog
    once, the gate refuses it, last-good stays frozen above the new steady-state
    count, and every later run reproduces the same shortfall forever.

    Decaying the floor with the age of last-good keeps the protection where it
    is worth having — a transient fetch outage that halves the pool for a day is
    still refused — while guaranteeing the pipeline always re-baselines on its
    own. A healthy daily cadence leaves last-good one day old, i.e. the full
    floor; only a gate that has already refused a run starts to yield.
    """
    gen = (last.get("generatedAt") or "")[:10]
    try:
        days = (today - dt.date.fromisoformat(gen)).days
    except ValueError:
        return FLOOR_SHOWS
    return max(FLOOR_SHOWS_MIN, FLOOR_SHOWS - FLOOR_RELAX_PER_DAY * max(0, days - 1))


def load_blocklist():
    bl = read_yaml(BLOCKLIST_PATH, {}) or {}
    ids, feeds, hosts, rthk = set(), set(), set(), set()
    for e in bl.get("shows") or []:
        if e.get("itunesId"):
            ids.add(int(e["itunesId"]))
        if e.get("feedUrl"):
            feeds.add(e["feedUrl"].strip())
    for e in bl.get("hosts") or []:
        if e.get("host"):
            hosts.add(e["host"].strip().lower())
    for e in bl.get("rthk") or []:
        if e.get("programme"):
            rthk.add(e["programme"].strip())
    return ids, feeds, hosts, rthk


def show_is_live(s, blocked, today):
    ids, feeds, hosts, _ = blocked
    v = s.get("validation") or {}
    if s.get("itunesId") in ids or s.get("feedUrl", "").strip() in feeds:
        return False
    if (v.get("enclosureHost") or "").lower() in hosts:
        return False
    if not v.get("checkedAt"):
        return False
    checked = dt.date.fromisoformat(v["checkedAt"][:10])
    if (today - checked).days > STALE_VALIDATION_DAYS:
        return False
    if v.get("ok"):
        return True
    return v.get("failStreak", 0) < MAX_FAIL_STREAK and bool(v.get("latestEpisode"))


def looks_chinese(s):
    v = s.get("validation") or {}
    lang = (v.get("lang") or s.get("piLanguage") or "").lower()
    return lang.startswith("zh") or lang.startswith("yue") or has_cjk(s.get("title")) or has_cjk(v.get("title"))


def public_show(s):
    v = s.get("validation") or {}
    out = {"title": (s.get("title") or v.get("title") or "").strip(),
           "author": (s.get("author") or v.get("author") or "").strip(),
           "artwork": s.get("artworkApple") or v.get("image") or "",
           "feedUrl": s["feedUrl"],
           "lang": v.get("lang") or s.get("piLanguage") or "",
           "explicit": bool(s.get("explicitApple") or v.get("explicit")),
           "category": s.get("genre") or "",
           "enclosureHost": v.get("enclosureHost") or ""}
    if s.get("itunesId"):
        out["itunesId"] = s["itunesId"]
    if v.get("description"):
        out["description"] = v["description"]
    le = v.get("latestEpisode")
    if le:
        out["latestEpisode"] = {k: le[k] for k in ("title", "date", "enclosure", "durationSec", "guid") if le.get(k) is not None}
    return out


# ---------------------------------------------------------------------------
# show/{showId}.json (SR-04) — schema/show.schema.json
# ---------------------------------------------------------------------------
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
GENERATED_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$")
EPISODE_KEYS = ("title", "date", "enclosure", "durationSec", "guid")


def episode_errors(ep):
    """Schema check for one show/{showId}.json episode; [] when valid."""
    if not isinstance(ep, dict):
        return ["episode is not an object"]
    errs = [f"unknown field {k!r}" for k in ep if k not in EPISODE_KEYS]
    title = ep.get("title")
    if not isinstance(title, str) or not title.strip() or title != title.strip():
        errs.append("title must be a trimmed non-empty string")
    date = ep.get("date")
    if not isinstance(date, str) or not DATE_RE.match(date):
        errs.append("date must be YYYY-MM-DD")
    else:
        try:
            dt.date.fromisoformat(date)
        except ValueError:
            errs.append("date is not a calendar date")
    enc = ep.get("enclosure")
    try:
        parts = urllib.parse.urlsplit(enc) if isinstance(enc, str) else None
    except ValueError:   # e.g. "http://[bad/x.mp3" (Invalid IPv6 URL)
        parts = None
    if not parts or parts.scheme not in ("http", "https") or not parts.netloc:
        errs.append("enclosure must be an absolute http(s) URL")
    if "durationSec" in ep and (type(ep["durationSec"]) is not int or ep["durationSec"] <= 0):
        errs.append("durationSec must be an integer > 0")
    if "guid" in ep and (not isinstance(ep["guid"], str) or not ep["guid"].strip()
                         or ep["guid"] != ep["guid"].strip()):
        errs.append("guid must be a trimmed non-empty string")
    return errs


def show_list_errors(doc, sid):
    """Schema check for a whole show/{sid}.json document; [] when valid."""
    if not isinstance(doc, dict):
        return ["document is not an object"]
    errs = [f"unknown field {k!r}" for k in doc if k not in ("showId", "generatedAt", "catalogVersion", "episodes")]
    if doc.get("showId") != sid or not SHOW_ID_RE.match(sid or ""):
        errs.append("showId must equal the file's id and match ^[A-Za-z0-9_-]{1,80}$")
    if not isinstance(doc.get("generatedAt"), str) or not GENERATED_AT_RE.match(doc["generatedAt"]):
        errs.append("generatedAt must be ISO-8601 with an offset")
    if type(doc.get("catalogVersion")) is not int:
        errs.append("catalogVersion must be an integer")
    eps = doc.get("episodes")
    if not isinstance(eps, list) or not eps:
        return errs + ["episodes must be a non-empty array"]
    if len(eps) > SHOW_LIST_MAX:
        errs.append(f"episodes holds {len(eps)} > {SHOW_LIST_MAX}")
    for i, ep in enumerate(eps):
        errs += [f"episodes[{i}]: {e}" for e in episode_errors(ep)]
    dates = [ep.get("date", "") for ep in eps if isinstance(ep, dict)]
    if dates != sorted(dates, reverse=True):
        errs.append("episodes must be newest first")
    return errs


def build_show_list(sid, stored, generated_at):
    """The show/{sid}.json document from a harvested data/episodes/{sid}.json,
    or None when nothing listable survives. Invalid episodes are dropped, the
    rest keep their harvested order (newest first by publish time, ties in feed
    order — the stable sort below only repairs a list that is out of order)."""
    if not SHOW_ID_RE.match(sid) or not isinstance(stored, dict) or not isinstance(stored.get("episodes"), list):
        return None
    eps = [{k: ep[k] for k in EPISODE_KEYS if k in ep} for ep in stored["episodes"] if isinstance(ep, dict)]
    eps = [ep for ep in eps if not episode_errors(ep)]   # project to the schema, then drop the invalid
    eps.sort(key=lambda ep: ep["date"], reverse=True)
    if not eps:
        return None
    return {"showId": sid, "generatedAt": generated_at, "catalogVersion": CATALOG_VERSION,
            "episodes": eps[:SHOW_LIST_MAX]}


def write_show_lists(show_ids, generated_at, episodes_dir=EPISODES_DIR, out_dir=SHOW_DIR):
    """Write show/{sid}.json for every id in show_ids that has a listable
    harvested list, and delete every other show/*.json (shows that left the
    catalog, or whose list emptied). A file whose episodes and catalogVersion
    are unchanged is left untouched — generatedAt then still names the run that
    produced that list, the bytes stay put for clients' conditional requests,
    and the daily commit carries only real changes. Returns (written, kept,
    removed, total bytes)."""
    episodes_dir, out_dir = Path(episodes_dir), Path(out_dir)
    wanted = {}
    for sid in sorted(show_ids):
        try:
            doc = build_show_list(sid, read_json(episodes_dir / f"{sid}.json"), generated_at)
            if doc is None:
                continue
            errs = show_list_errors(doc, sid)
        except Exception as e:   # one show's bad data never blocks its siblings or the index
            log(f"::warning::show/{sid}.json not built: {type(e).__name__}: {e}")
            continue
        if errs:   # a build bug, not bad feed data — never block the index for it
            log(f"::warning::show/{sid}.json failed its schema and was not written: {errs[:3]}")
            continue
        wanted[sid] = doc
    out_dir.mkdir(parents=True, exist_ok=True)
    written = kept = removed = total = 0
    for sid, doc in list(wanted.items()):
        path = out_dir / f"{sid}.json"
        try:
            prev = read_json(path)
            if (isinstance(prev, dict) and prev.get("episodes") == doc["episodes"]
                    and prev.get("catalogVersion") == doc["catalogVersion"] and not show_list_errors(prev, sid)):
                kept += 1
            else:
                path.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
                written += 1
            total += path.stat().st_size
        except Exception as e:
            log(f"::warning::show/{sid}.json not written: {type(e).__name__}: {e}")
            wanted.pop(sid)   # a half-written file is pruned below rather than published
    for path in sorted(out_dir.glob("*.json")):
        if path.stem not in wanted:
            try:
                path.unlink()
                removed += 1
            except OSError as e:
                log(f"::warning::stale {path.name} not removed: {e}")
    return written, kept, removed, total


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--force-baseline", action="store_true",
                    help="publish even if the show count is under the safety floor, making it "
                         "the new last-good. For a contraction you have inspected and accepted.")
    args = ap.parse_args(argv)
    force_baseline = args.force_baseline
    today = now_hkt().date()
    run = read_json(LAST_RUN_PATH, {})
    if str(run.get("aborted") or "").startswith("CircuitOpen"):
        return fail(f"last harvest tripped the circuit breaker: {run['aborted']}")
    pool = read_json(SHOWS_PATH, {})
    seeds = read_yaml(SEEDS_PATH, {}) or {}
    blocked = load_blocklist()
    rthk = read_json(RTHK_PATH)
    if rthk and (today - dt.date.fromisoformat((rthk.get("fetchedAt") or "2000-01-01")[:10])).days > 3:
        log(f"::warning::data/rthk.json is stale ({rthk.get('fetchedAt')}) — RTHK shelves omitted this build")
        rthk = None
    pi = read_json(CHARTS_DIR / "podcastindex.json")

    live = {sid: s for sid, s in pool.items() if s.get("feedUrl") and show_is_live(s, blocked, today)}
    markets_out, used = {}, set()
    search = {}
    for market, cfg in MARKETS.items():
        charts = read_json(CHARTS_DIR / f"{market}.json", {})
        shelves = []

        def rank_of(sid):
            return (pool[sid].get("markets", {}).get(market, {}) or {}).get("rank", 10 ** 6)

        def latest_of(sid):
            return ((pool[sid].get("validation") or {}).get("latestEpisode") or {}).get("date", "")

        top = [sid for sid in charts.get("chart", []) if sid in live and (not TOP_SHELF_ZH_ONLY or looks_chinese(live[sid]))]
        top = top[:TOP_SHELF_SIZE]
        if len(top) < MIN_TOP and charts.get("chart"):
            return fail(f"{market}: top shelf has {len(top)} shows (< {MIN_TOP}) — chart or validation broke")
        if top:
            shelves.append({"id": "top", "title_zh": "熱門", "title_en": "Top Podcasts", "shows": top})

        kw = []
        for term, ids in (charts.get("keywords") or {}).items():
            kw += ids
        for sid in charts.get("seeds", []):
            if cfg["keyword_shelf"]["id"] in (pool.get(sid, {}).get("markets", {}).get(market, {}) or {}).get("seedShelves", []):
                kw.append(sid)
        kw = [sid for sid in dict.fromkeys(kw) if sid in live and looks_chinese(live[sid])]
        kw.sort(key=lambda sid: (rank_of(sid), latest_of(sid) and -int(latest_of(sid).replace("-", ""))))
        if kw:
            shelves.append({"id": cfg["keyword_shelf"]["id"], "title_zh": cfg["keyword_shelf"]["title_zh"],
                            "title_en": cfg["keyword_shelf"]["title_en"], "shows": kw[:TOP_SHELF_SIZE]})

        curated = [sid for sid in charts.get("seeds", [])
                   if sid in live and "curated" in ((pool[sid].get("markets", {}).get(market, {}) or {}).get("seedShelves", []))]
        if curated:
            shelves.append({"id": "curated", "title_zh": "編輯精選", "title_en": "Picks", "shows": curated})

        if pi and pi.get("trending"):
            tr = [sid for sid in pi["trending"] if sid in live and looks_chinese(live[sid])]
            if tr:
                shelves.append({"id": "trending", "title_zh": "華語熱話", "title_en": "Trending", "shows": tr[:TOP_SHELF_SIZE]})

        entries = []
        for sh in shelves:
            for sid in sh["shows"]:
                used.add(sid)
        for sid in sorted({sid for sh in shelves for sid in sh["shows"]}):
            s = live[sid]
            v = s.get("validation") or {}
            entries.append({"t": " ".join(x for x in ((s.get("title") or v.get("title") or ""), (s.get("author") or v.get("author") or "")) if x).strip(),
                            "type": "show", "id": sid})

        # RTHK shelves (hk only) — data ships, the client gates on ReplayGate.
        rthk_ids = []
        if market == "hk" and rthk:
            _, _, _, blocked_rthk = blocked
            for chn in rthk.get("channels", []):
                progs = [rthk["programmes"].get(f"{chn['id']}/{slug}") for slug in chn["programmes"]]
                progs = [p for p in progs if p and p["episodes"] and p["slug"] not in blocked_rthk]
                progs.sort(key=lambda p: (not p["active"], -(int(p["latestDate"].replace("-", "")) if p.get("latestDate") else 0)))
                ids = [f"rthk_{p['channel']}_{p['slug']}" for p in progs]
                if ids:
                    shelves.append({"id": f"rthk_{chn['id']}", "title_zh": f"港台{chn['name_zh']}",
                                    "title_en": f"RTHK {chn['name_en']}", "gate": "replay",
                                    "source": "rthk", "shows": ids})
                    rthk_ids += ids
        markets_out[market] = {"shelves": shelves}
        search[market] = {"entries": entries, "_rthk": rthk_ids}

    shows_out = {sid: public_show(live[sid]) for sid in sorted(used)}
    rthk_shows = {}
    if rthk:
        for key, p in rthk["programmes"].items():
            sid = f"rthk_{p['channel']}_{p['slug']}"
            if sid not in search.get("hk", {}).get("_rthk", []):
                continue
            latest = p["episodes"][0] if p["episodes"] else None
            rthk_shows[sid] = {"source": "rthk", "channel": p["channel"], "slug": p["slug"],
                               "title_zh": p["title_zh"], "title_en": p["title_en"],
                               "artwork": p.get("logo") or "", "active": p["active"],
                               "episodesUrl": f"{{replayBase}}/prog/{p['channel']}/{p['slug']}.json",
                               "episodeCount": p["episodeCount"]}
            if latest:
                rthk_shows[sid]["latestEpisode"] = {"title": latest["title"], "date": latest["date"]}
    shows_out.update(rthk_shows)

    # Safety floor vs last-good.
    last = read_json(INDEX_PATH)
    if last:
        last_n = sum(1 for s in last.get("shows", {}).values() if s.get("source") != "rthk")
        n = sum(1 for s in shows_out.values() if s.get("source") != "rthk")
        floor = effective_floor(last, today)
        if last_n and n < floor * last_n:
            if not force_baseline:
                return fail(f"open-RSS shows {n} < {floor:.0%} of last-good {last_n} "
                            f"(last-good generated {(last.get('generatedAt') or '?')[:10]})")
            log(f"::warning::--force-baseline: publishing {n} shows against last-good {last_n} "
                f"({n / last_n:.0%}), under the {floor:.0%} floor")
        elif floor < FLOOR_SHOWS:
            log(f"::warning::safety floor relaxed to {floor:.0%} — last-good has stood since "
                f"{(last.get('generatedAt') or '?')[:10]} without a successful publish")
    if not shows_out:
        return fail("no shows survived validation")

    index = {"generatedAt": now_hkt().isoformat(timespec="seconds"), "catalogVersion": CATALOG_VERSION,
             "replay": {"generatedAt": rthk.get("replayGeneratedAt"), "streamTemplate": rthk.get("streamTemplate"),
                        "streamVersion": rthk.get("streamVersion")} if rthk else None,
             "markets": markets_out, "shows": shows_out}

    # search.json — RTHK episode titles for hk, trimmed oldest-month-first to budget.
    rthk_eps = []
    if rthk and search.get("hk"):
        for key, p in rthk["programmes"].items():
            sid = f"rthk_{p['channel']}_{p['slug']}"
            if sid not in search["hk"]["_rthk"]:
                continue
            for e in p["episodes"]:
                if e.get("title"):
                    rthk_eps.append({"t": f"{e['title']} · {p['title_zh']} {e['date']}", "type": "episode",
                                     "id": sid, "eid": e["id"], "date": e["date"]})
            rthk_eps.append({"t": " ".join(dict.fromkeys(x for x in (p['title_zh'], p['title_en']) if x)), "type": "show", "id": sid})
    rthk_eps.sort(key=lambda e: e.get("date", "9999"), reverse=True)   # shows (no date) first

    def build_search(eps):
        out = {"generatedAt": index["generatedAt"], "markets": {}}
        for m, v in search.items():
            out["markets"][m] = {"entries": v["entries"] + (eps if m == "hk" else [])}
        return out

    trimmed_to = None
    while True:
        doc = build_search(rthk_eps)
        blob = json.dumps(doc, ensure_ascii=False, separators=(",", ":"))
        if len(blob.encode()) <= SEARCH_BUDGET_BYTES or not any(e.get("date") for e in rthk_eps):
            break
        oldest = min(e["date"][:7] for e in rthk_eps if e.get("date"))
        rthk_eps = [e for e in rthk_eps if not e.get("date") or e["date"][:7] != oldest]
        trimmed_to = oldest
    if trimmed_to:
        log(f"search.json over budget — RTHK episodes trimmed; oldest month dropped: {trimmed_to}")
        doc["rthkEpisodesTrimmed"] = True

    index_blob = json.dumps(index, ensure_ascii=False, indent=1)
    if len(index_blob.encode()) > INDEX_BUDGET_BYTES:
        log(f"::warning::index.json is {len(index_blob.encode()) // 1024} KB — over the {INDEX_BUDGET_BYTES // 1024} KB soft target")
    open_rss = [sid for sid, s in shows_out.items() if s.get("source") != "rthk"]
    try:   # the gate has passed: a show-list failure never blocks index.json / search.json
        w, k, r, nbytes = write_show_lists(open_rss, index["generatedAt"], EPISODES_DIR, SHOW_DIR)
        log(f"show/: {w + k} lists ({w} written, {k} unchanged, {r} removed), {nbytes // 1024} KB")
    except Exception as e:
        log(f"::warning::show/ lists not written this build: {type(e).__name__}: {e}")
    INDEX_PATH.write_text(index_blob + "\n", encoding="utf-8")
    SEARCH_PATH.write_text(blob + "\n", encoding="utf-8")
    log("index.json: " + ", ".join(f"{m}={[ (sh['id'], len(sh['shows'])) for sh in v['shelves'] ]}" for m, v in markets_out.items()))
    log(f"shows {len(shows_out)} ({len(rthk_shows)} rthk), index {len(index_blob.encode()) // 1024} KB, "
        f"search {len(blob.encode()) // 1024} KB ({len(rthk_eps)} rthk entries)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
