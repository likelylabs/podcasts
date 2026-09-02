# likelylabs/podcasts

Server-side catalog for the radio app's **Podcasts / 播客** section: a daily
harvest of third-party Chinese-language podcasts discovered through public
directories, validated against each publisher's own RSS, and published as a
tiny static catalog + a pre-built local search index on GitHub Pages. We host
**no audio and no show RSS** — the app plays enclosures straight from the
publisher's CDN, and reads RTHK material from our sibling REPLAY catalog.

Spec, tenets and decisions live in the private coordination repo
(`~/localdev/radioapp-hq`: `PODCASTS.md`, `CLAUDE.md`). **No secrets here,
ever** — the optional Podcast Index key lives only in Actions secrets.

## Pipeline

```
 Apple charts + keyword search (hk/tw/sg/my) ─┐
 Podcast Index trending zh* (optional key)    ─┼─► tools/harvest.py → data/     tools/build_catalog.py → index.json + search.json
 seeds.yaml (editorial) / blocklist.yaml      ─┤        (validates every feed)        (shelves, gate, size guard)
 replay.likelylabs.com (RTHK, our own CDN)    ─┘                                       pages-deploy.yml → Pages (last-good)
```

- `tools/harvest.py` — the only code that talks to anything external.
  Serial, paced, budgeted, circuit-broken. Records `latestEpisode` and
  `enclosureHost` per show (the in-region reachability key).
- `tools/build_catalog.py` — the only writer of `index.json` + `search.json`.
  Shelves per market: `top` (chart, Chinese-language), a market keyword shelf
  (`cantonese` for hk), `curated` (seeds), `trending` (Podcast Index, when
  present), and for hk one `rthk_<channel>` shelf per RTHK channel flagged
  `gate: "replay"` — the client renders those only when its ReplayGate
  allows. Refuses to publish if a market's top shelf collapses or the pool
  shrinks past 80% of last-good. `search.json` is capped (~1.5 MB); RTHK
  episode entries are trimmed oldest-month-first to fit.
- `seeds.yaml` / `blocklist.yaml` — the editorial and takedown levers, in git.
- Repository variable `REPLAY_BASE_URL` turns the RTHK shelves on; secrets
  `PODCASTINDEX_KEY` + `PODCASTINDEX_SECRET` turn Podcast Index on. Both are
  no-ops until set.

## Catalog contract (app-facing)

- `index.json` — `markets.<hk|tw|sg|my>.shelves[]` (ids into `shows`) and the
  shared `shows` pool. Open-RSS shows carry `feedUrl` (the app parses episodes
  from the publisher RSS on tap) + `latestEpisode` (so "play latest" needs no
  fetch). RTHK shows carry `source: "rthk"` + `episodesUrl` with a
  `{replayBase}` placeholder the client fills from its `replayCatalogBaseUrl`.
  Schema: `schema/index.schema.json`.
- `search.json` — per-market entries `{t, type, id[, eid, date]}` for
  client-side contains-match. Schema: `schema/search.schema.json`.

## Local dev

```bash
pip install pyyaml   # only for seeds/blocklist; pipeline itself is stdlib
python3 tools/harvest.py --markets hk --budget 200 --max-feeds 30 --replay-base ../likelylabs-replay
python3 tools/build_catalog.py
```

## Go-live checklist (owner)

1. Flip the repo **public** (Pages is public-repo-only on this org's plan).
2. Settings → Pages → Source = **GitHub Actions**.
3. Repo variables: `PAGES_ENABLED=true`; `PUBLIC_BASE_URL=https://likelylabs.github.io/podcasts`;
   `REPLAY_BASE_URL=https://replay.likelylabs.com` once REPLAY is live.
4. Run `pages-deploy` → verify `index.json` + `search.json` on the github.io URL.
5. Cloudflare DNS: `podcasts CNAME likelylabs.github.io` (DNS-only / grey cloud).
6. Commit `CNAME` (`podcasts.likelylabs.com`), enforce HTTPS, update `PUBLIC_BASE_URL`.
7. Optional: register a Podcast Index key → add the two secrets.
