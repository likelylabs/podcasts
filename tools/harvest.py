#!/usr/bin/env python3
"""
FETCH leg of the podcasts pipeline (PODCASTS.md §2.1–2.2). Talks to Apple,
Podcast Index (optional), publisher RSS feeds and our own REPLAY catalog;
writes only under data/. tools/build_catalog.py is the only writer of
index.json / search.json (tenet #5).

Per market (hk/tw/sg/my):
  1. Apple top chart (100) → batched lookup for feedUrl/artwork/explicit.
  2. Apple keyword searches — the language filter Apple lacks.
  3. Seeds (seeds.yaml) resolved the same way.
  4. Podcast Index trending lang=zh* — only when PODCASTINDEX_KEY/SECRET
     are set (owner registers the key; harvester is Apple-only until then).
Candidates Apple already reports as long abandoned (releaseDate older than
INGEST_DEAD_DAYS) never enter the pool; seeds are exempt. Every remaining
candidate feed is fetched and validated (RSS, ≥1 audio enclosure, latest
episode within FRESH_DAYS) and its latestEpisode + enclosureHost are recorded.
Shows we confirm dead are forgotten so they stop costing a request a run.
Finally the REPLAY catalog (REPLAY_BASE_URL, http(s) or a local path) is read
into data/rthk.json for the RTHK shelves + episode search.

Usage: python3 tools/harvest.py [--markets hk,tw] [--budget N] [--pace S]
                                [--replay-base URL|PATH] [--skip-feeds]
"""
import argparse
import datetime as dt
import email.utils
import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path

from common import (APPLE, BROWSER_UA, BudgetExhausted, CHART_LIMIT, CHARTS_DIR, CircuitOpen,
                    Client, FEED_MAX_BYTES, FORGET_FAIL_STREAK, FRESH_DAYS, HKT, INGEST_DEAD_DAYS,
                    LAST_RUN_PATH, MARKETS, MAX_FAIL_STREAK, PODCASTINDEX, RTHK_PATH, SEEDS_PATH,
                    SHOWS_PATH, log, now_hkt, read_json, read_yaml, show_id_for, write_json)

ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"
AUDIO_EXT = (".mp3", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".wav", ".flac")


# ---------------------------------------------------------------------------
# Apple
# ---------------------------------------------------------------------------
def apple_chart(client, cc):
    st, d = client.get_json(f"{APPLE}/{cc}/rss/toppodcasts/limit={CHART_LIMIT}/json", ua=BROWSER_UA)
    if st != 200 or not d:
        return None
    entries = (d.get("feed") or {}).get("entry") or []
    if isinstance(entries, dict):
        entries = [entries]
    out = []
    for i, e in enumerate(entries):
        try:
            out.append({"itunesId": int(e["id"]["attributes"]["im:id"]),
                        "name": e["im:name"]["label"],
                        "artist": (e.get("im:artist") or {}).get("label", ""),
                        "category": ((e.get("category") or {}).get("attributes") or {}).get("label", ""),
                        "rank": i + 1})
        except (KeyError, TypeError, ValueError):
            continue
    return out


def apple_lookup(client, cc, ids):
    found = {}
    ids = [str(i) for i in ids]
    for k in range(0, len(ids), 100):
        chunk = ",".join(ids[k:k + 100])
        st, d = client.get_json(f"{APPLE}/lookup?id={chunk}&country={cc}&entity=podcast", ua=BROWSER_UA)
        for r in ((d or {}).get("results") or []):
            if r.get("kind") == "podcast" or r.get("wrapperType") == "track":
                found[int(r["collectionId"])] = r
    return found


def apple_search(client, cc, term):
    q = urllib.parse.quote(term)
    st, d = client.get_json(f"{APPLE}/search?term={q}&entity=podcast&country={cc}&limit=50", ua=BROWSER_UA)
    return [r for r in ((d or {}).get("results") or []) if r.get("feedUrl")]


def dead_on_arrival(r):
    """True when Apple's own metadata says this show stopped publishing long ago.

    The charts and keyword searches return a long tail of abandoned podcasts —
    they cost a validation request every run and pad the publish gate's
    last-good baseline until MAX_FAIL_STREAK finally evicts them all at once.
    Refusing them at the door keeps the pool honest. The cut is twice
    FRESH_DAYS so a lagging releaseDate cannot bury a show that is still live,
    and an absent or unparseable date defers to real feed validation.
    """
    d = (r.get("releaseDate") or "")[:10]
    if not d:
        return False
    try:
        return (now_hkt().date() - dt.date.fromisoformat(d)).days > INGEST_DEAD_DAYS
    except ValueError:
        return False


def absorb_apple(pool, r, market, source, rank=None, term=None):
    """Merge one Apple result (lookup/search shape) into the show pool."""
    if not r.get("feedUrl") or not r.get("collectionId"):
        return None
    sid = show_id_for(itunes_id=r["collectionId"])
    # Seeds are curated by hand and always welcome. Otherwise a *new* candidate
    # must look alive: for shows we already track, our own feed validation is the
    # authority and Apple's releaseDate — which does lag by a year on some live
    # shows — must not knock them off the shelves. The prune reclaims the ones we
    # confirm dead ourselves.
    if sid not in pool and not source.startswith("seed:") and dead_on_arrival(r):
        return None
    s = pool.setdefault(sid, {"id": sid, "itunesId": int(r["collectionId"]), "markets": {}, "sources": []})
    s["feedUrl"] = r["feedUrl"].strip()
    s["title"] = r.get("collectionName") or r.get("trackName") or s.get("title", "")
    s["author"] = r.get("artistName") or s.get("author", "")
    s["artworkApple"] = r.get("artworkUrl600") or r.get("artworkUrl100") or s.get("artworkApple")
    s["genre"] = r.get("primaryGenreName") or s.get("genre", "")
    s["explicitApple"] = (r.get("collectionExplicitness") == "explicit")
    s["appleReleaseDate"] = (r.get("releaseDate") or "")[:10]
    s["trackCount"] = r.get("trackCount")
    m = s["markets"].setdefault(market, {})
    if rank is not None:
        m["rank"] = rank
    if term is not None:
        m.setdefault("terms", [])
        if term not in m["terms"]:
            m["terms"].append(term)
    if source not in s["sources"]:
        s["sources"].append(source)
    s["lastSeenAt"] = now_hkt().date().isoformat()
    return sid


def absorb_feed_only(pool, feed_url, market, source, title=""):
    sid = show_id_for(feed_url=feed_url)
    s = pool.setdefault(sid, {"id": sid, "markets": {}, "sources": []})
    s["feedUrl"] = feed_url.strip()
    s.setdefault("title", title)
    s["markets"].setdefault(market, {})
    if source not in s["sources"]:
        s["sources"].append(source)
    s["lastSeenAt"] = now_hkt().date().isoformat()
    return sid


# ---------------------------------------------------------------------------
# Podcast Index (optional)
# ---------------------------------------------------------------------------
def podcastindex_headers(key, secret):
    ts = str(int(time.time()))
    auth = hashlib.sha1((key + secret + ts).encode()).hexdigest()
    return {"X-Auth-Key": key, "X-Auth-Date": ts, "Authorization": auth}


def podcastindex_trending(client, key, secret):
    st, d = client.get_json(f"{PODCASTINDEX}/podcasts/trending?max=100&lang=zh,zh-cn,zh-tw,zh-hk,yue",
                            podcastindex_headers(key, secret))
    if st != 200 or not d:
        return None, st
    return d.get("feeds") or [], st


# ---------------------------------------------------------------------------
# Feed validation
# ---------------------------------------------------------------------------
def parse_duration(s):
    if not s:
        return None
    s = s.strip()
    if s.isdigit():
        return int(s)
    parts = s.split(":")
    try:
        parts = [int(float(p)) for p in parts]
    except ValueError:
        return None
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    return None


def parse_pubdate(s):
    if not s:
        return None
    try:
        d = email.utils.parsedate_to_datetime(s.strip())
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return d
    except (TypeError, ValueError, IndexError):
        pass
    try:
        return dt.datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    except ValueError:
        return None


def is_audio(enc):
    t = (enc.get("type") or "").lower()
    u = (enc.get("url") or "").lower().split("?")[0]
    return t.startswith("audio/") or u.endswith(AUDIO_EXT)


def validate_feed(client, s):
    """Fetch + parse the publisher RSS; returns the validation record."""
    v = {"checkedAt": now_hkt().isoformat(timespec="seconds"), "ok": False}
    st, ct, body, final = client.get(s["feedUrl"], timeout=30, max_bytes=FEED_MAX_BYTES, ua=BROWSER_UA)
    v["status"] = st
    if st != 200:
        v["error"] = f"http {st}"
        return v
    try:
        root = ET.fromstring(body)
    except ET.ParseError as e:
        v["error"] = f"xml: {e}"
        return v
    ch = root.find("channel")
    if ch is None:
        v["error"] = "no <channel>"
        return v
    itn = "{%s}" % ITUNES_NS
    v["title"] = (ch.findtext("title") or "").strip()
    v["author"] = (ch.findtext(itn + "author") or "").strip()
    v["lang"] = (ch.findtext("language") or "").strip()
    v["explicit"] = (ch.findtext(itn + "explicit") or "").strip().lower() in ("true", "yes", "explicit")
    img = ch.find(itn + "image")
    v["image"] = (img.get("href") if img is not None else ch.findtext("image/url")) or ""
    v["description"] = re.sub(r"<[^>]+>", "", (ch.findtext(itn + "summary") or ch.findtext("description") or "")).strip()[:400]
    latest = None
    n_audio = 0
    for it in ch.findall("item"):
        enc = it.find("enclosure")
        if enc is None or not is_audio(enc.attrib):
            continue
        n_audio += 1
        pub = parse_pubdate(it.findtext("pubDate"))
        if pub and (latest is None or pub > latest["_dt"]):
            latest = {"_dt": pub, "title": (it.findtext("title") or "").strip(),
                      "date": pub.astimezone(HKT).date().isoformat(),
                      "enclosure": enc.get("url"),
                      "type": enc.get("type") or "",
                      "durationSec": parse_duration(it.findtext(itn + "duration"))}
    v["itemCount"] = n_audio
    if n_audio == 0 or latest is None:
        v["error"] = "no audio enclosure with a date"
        return v
    age = (now_hkt() - latest["_dt"]).days
    v["latestAgeDays"] = age
    latest.pop("_dt")
    v["latestEpisode"] = latest
    v["enclosureHost"] = urllib.parse.urlsplit(latest["enclosure"] or "").hostname or ""
    if age > FRESH_DAYS:
        v["error"] = f"stale: latest episode {age}d old"
        return v
    v["ok"] = True
    return v


# ---------------------------------------------------------------------------
# REPLAY catalog → data/rthk.json
# ---------------------------------------------------------------------------
def read_replay(client, base, run):
    base = base.rstrip("/")
    def get(rel):
        if base.startswith("http://") or base.startswith("https://"):
            st, d = client.get_json(f"{base}/{rel}")
            return d if st == 200 else None
        return read_json(Path(base) / rel)
    idx = get("index.json")
    if not idx:
        run["warnings"].append(f"replay catalog unreadable at {base}")
        return None
    out = {"fetchedAt": now_hkt().isoformat(timespec="seconds"),
           "replayGeneratedAt": idx.get("generatedAt"), "streamTemplate": idx.get("streamTemplate"),
           "streamVersion": idx.get("streamVersion"), "channels": [], "programmes": {}}
    for chn in idx.get("channels", []):
        if chn.get("source") != "rthk":
            continue
        out["channels"].append({"id": chn["id"], "name_zh": chn["name_zh"], "name_en": chn["name_en"],
                                "programmes": [p["slug"] for p in chn.get("programmes", [])]})
        for p in chn.get("programmes", []):
            doc = get(f"prog/{chn['id']}/{p['slug']}.json") or {}
            eps = [{"id": e["id"], "title": e.get("title", ""), "date": e["date"]} for e in doc.get("episodes", [])]
            out["programmes"][f"{chn['id']}/{p['slug']}"] = {
                "channel": chn["id"], "slug": p["slug"], "title_zh": p["title_zh"],
                "title_en": p.get("title_en") or p["title_zh"], "logo": p.get("logo"),
                "active": p.get("active", False), "latestDate": p.get("latestDate"),
                "episodeCount": p.get("episodeCount", len(eps)), "episodes": eps}
    run["rthk_programmes"] = len(out["programmes"])
    run["rthk_episodes"] = sum(len(p["episodes"]) for p in out["programmes"].values())
    return out


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--markets", default=",".join(MARKETS))
    ap.add_argument("--budget", type=int, default=3000)
    ap.add_argument("--pace", type=float, default=0.7)
    ap.add_argument("--replay-base", default=os.environ.get("REPLAY_BASE_URL", ""))
    ap.add_argument("--skip-feeds", action="store_true", help="dev: skip feed validation")
    ap.add_argument("--max-feeds", type=int, default=0, help="dev: cap feed validations")
    args = ap.parse_args()
    markets = [m for m in args.markets.split(",") if m in MARKETS]

    client = Client(pace_s=args.pace, budget=args.budget, log=log)
    pool = read_json(SHOWS_PATH, {})
    seeds = read_yaml(SEEDS_PATH, {}) or {}
    run = {"startedAt": now_hkt().isoformat(timespec="seconds"), "budget": args.budget,
           "warnings": [], "aborted": None, "markets": {}}
    t0 = time.monotonic()
    pi_key, pi_secret = os.environ.get("PODCASTINDEX_KEY", ""), os.environ.get("PODCASTINDEX_SECRET", "")
    try:
        for market in markets:
            cfg = MARKETS[market]
            cc = cfg["cc"]
            mrun = run["markets"][market] = {}
            log(f"== {market}: chart")
            chart = apple_chart(client, cc)
            if chart is None:
                run["warnings"].append(f"{market}: chart unavailable")
                chart = []
            mrun["chart"] = len(chart)
            looked = apple_lookup(client, cc, [c["itunesId"] for c in chart]) if chart else {}
            chart_ids = []
            for c in chart:
                r = looked.get(c["itunesId"])
                sid = absorb_apple(pool, r, market, "apple_chart", rank=c["rank"]) if r else None
                if sid:
                    chart_ids.append(sid)
            mrun["chart_resolved"] = len(chart_ids)
            log(f"== {market}: keyword searches {cfg['terms']}")
            keyword_ids = {}
            for term in cfg["terms"]:
                ids = []
                for r in apple_search(client, cc, term):
                    sid = absorb_apple(pool, r, market, "apple_search", term=term)
                    if sid:
                        ids.append(sid)
                keyword_ids[term] = ids
            mrun["keyword_hits"] = {t: len(v) for t, v in keyword_ids.items()}
            # seeds for this market
            seed_ids = []
            for sd in ((seeds.get("markets") or {}).get(market) or []):
                shelf = sd.get("shelf", "curated")
                if sd.get("itunesId"):
                    r = apple_lookup(client, cc, [sd["itunesId"]]).get(int(sd["itunesId"]))
                    sid = absorb_apple(pool, r, market, f"seed:{shelf}") if r else None
                    if not sid:
                        run["warnings"].append(f"{market}: seed itunesId {sd['itunesId']} did not resolve")
                        continue
                elif sd.get("feedUrl"):
                    sid = absorb_feed_only(pool, sd["feedUrl"], market, f"seed:{shelf}", sd.get("title", ""))
                else:
                    continue
                pool[sid]["markets"][market].setdefault("seedShelves", [])
                if shelf not in pool[sid]["markets"][market]["seedShelves"]:
                    pool[sid]["markets"][market]["seedShelves"].append(shelf)
                seed_ids.append(sid)
            mrun["seeds"] = len(seed_ids)
            write_json(CHARTS_DIR / f"{market}.json",
                       {"fetchedAt": now_hkt().isoformat(timespec="seconds"), "chart": chart_ids,
                        "keywords": keyword_ids, "seeds": seed_ids})

        # Podcast Index (optional): global zh trending, cross-check by feedUrl.
        if pi_key and pi_secret:
            log("== podcast index: trending zh")
            feeds, st = podcastindex_trending(client, pi_key, pi_secret)
            if feeds is None:
                run["warnings"].append(f"podcast index trending: http {st}")
            else:
                pi_ids = []
                for f in feeds:
                    if not f.get("url"):
                        continue
                    if f.get("itunesId"):
                        sid = show_id_for(itunes_id=f["itunesId"])
                        s = pool.setdefault(sid, {"id": sid, "itunesId": int(f["itunesId"]), "markets": {}, "sources": []})
                    else:
                        sid = show_id_for(feed_url=f["url"])
                        s = pool.setdefault(sid, {"id": sid, "markets": {}, "sources": []})
                    s.setdefault("feedUrl", f["url"])
                    s.setdefault("title", f.get("title", ""))
                    s.setdefault("author", f.get("author", ""))
                    s.setdefault("artworkApple", f.get("artwork") or f.get("image"))
                    s["piLanguage"] = f.get("language", "")
                    if "podcastindex_trending" not in s["sources"]:
                        s["sources"].append("podcastindex_trending")
                    s["lastSeenAt"] = now_hkt().date().isoformat()
                    pi_ids.append(sid)
                write_json(CHARTS_DIR / "podcastindex.json",
                           {"fetchedAt": now_hkt().isoformat(timespec="seconds"), "trending": pi_ids})
                run["podcastindex_trending"] = len(pi_ids)
        else:
            run["podcastindex"] = "skipped (no PODCASTINDEX_KEY/SECRET)"

        # Validate feeds: everything seen today first, then the rest of the
        # pool (re-check at most once a day), newest-unchecked first.
        if not args.skip_feeds:
            today = now_hkt().date().isoformat()
            todo = [s for s in pool.values() if s.get("feedUrl")
                    and (s.get("validation") or {}).get("checkedAt", "")[:10] != today]
            todo.sort(key=lambda s: (s.get("lastSeenAt", "") != today, s.get("validation", {}).get("checkedAt", "")))
            if args.max_feeds:
                todo = todo[:args.max_feeds]
            log(f"== validating {len(todo)} feeds")
            ok = bad = 0
            for s in todo:
                v = validate_feed(client, s)
                prev = s.get("validation") or {}
                v["failStreak"] = 0 if v["ok"] else prev.get("failStreak", 0) + 1
                if not v["ok"] and prev.get("ok"):
                    # keep the last-good episode/host so the show can ride out a blip
                    for k in ("latestEpisode", "enclosureHost", "image", "lang", "title", "author", "description"):
                        if prev.get(k) and not v.get(k):
                            v[k] = prev[k]
                s["validation"] = v
                ok += v["ok"]
                bad += not v["ok"]
            run["feeds_validated"] = len(todo)
            run["feeds_ok"] = ok
            run["feeds_bad"] = bad
            log(f"   ok {ok} / bad {bad}")

        if args.replay_base:
            log(f"== replay catalog from {args.replay_base}")
            rthk = read_replay(client, args.replay_base, run)
            if rthk:
                write_json(RTHK_PATH, rthk, compact=True)
                log(f"   {run.get('rthk_programmes')} programmes / {run.get('rthk_episodes')} episodes")
        else:
            run["replay"] = "skipped (no REPLAY_BASE_URL) — RTHK shelves absent until set"
    except (BudgetExhausted, CircuitOpen) as e:
        run["aborted"] = f"{type(e).__name__}: {e}"
        log(f"!! {run['aborted']}")
    finally:
        # Forget shows not seen by any source for 60 days, and shows we have
        # confirmed dead ourselves — a feed that reads fine but stopped publishing
        # months ago, or one unreachable for FORGET_FAIL_STREAK runs running. A
        # show validating OK is never forgotten, whatever Apple's metadata claims.
        cutoff = (now_hkt() - dt.timedelta(days=60)).date().isoformat()

        def forgettable(s):
            v = s.get("validation") or {}
            if v.get("ok"):
                return False
            if s.get("lastSeenAt", "") < cutoff:
                return True
            age = v.get("latestAgeDays")
            if age is not None and age > INGEST_DEAD_DAYS:
                return True
            return v.get("failStreak", 0) >= FORGET_FAIL_STREAK

        stale = [k for k, s in pool.items() if forgettable(s)]
        for k in stale:
            pool.pop(k)
        run["pool"] = len(pool)
        run["pool_forgotten"] = len(stale)
        run["requests"] = client.requests
        run["failures"] = client.failures
        run["seconds"] = round(time.monotonic() - t0)
        run["finishedAt"] = now_hkt().isoformat(timespec="seconds")
        write_json(SHOWS_PATH, pool)
        write_json(LAST_RUN_PATH, run)
        log(f"== done: {client.requests} requests, {client.failures} failures, {run['seconds']}s, pool {len(pool)}")
    return 1 if run["aborted"] and run["aborted"].startswith("CircuitOpen") else 0


if __name__ == "__main__":
    sys.exit(main())
