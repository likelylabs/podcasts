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
"""
import argparse
import datetime as dt
import json
import sys

from common import (BLOCKLIST_PATH, CATALOG_VERSION, CHARTS_DIR, INDEX_BUDGET_BYTES, INDEX_PATH,
                    LAST_RUN_PATH, MARKETS, MAX_FAIL_STREAK, RTHK_PATH, SEARCH_BUDGET_BYTES,
                    SEARCH_PATH, SEEDS_PATH, SHOWS_PATH, TOP_SHELF_SIZE, TOP_SHELF_ZH_ONLY,
                    has_cjk, log, now_hkt, read_json, read_yaml)

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
        out["latestEpisode"] = {k: le[k] for k in ("title", "date", "enclosure", "durationSec") if le.get(k) is not None}
    return out


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
    INDEX_PATH.write_text(index_blob + "\n", encoding="utf-8")
    SEARCH_PATH.write_text(blob + "\n", encoding="utf-8")
    log("index.json: " + ", ".join(f"{m}={[ (sh['id'], len(sh['shows'])) for sh in v['shelves'] ]}" for m, v in markets_out.items()))
    log(f"shows {len(shows_out)} ({len(rthk_shows)} rthk), index {len(index_blob.encode()) // 1024} KB, "
        f"search {len(blob.encode()) // 1024} KB ({len(rthk_eps)} rthk entries)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
