"""
show/{showId}.json (SR-04): harvest parsing → data/episodes/ → build_catalog
schema, ordering, cap, pruning. Stdlib only, no network:

    python3 -m unittest discover -s tests -v
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import build_catalog  # noqa: E402
import common  # noqa: E402
import harvest  # noqa: E402

GEN = "2026-11-12T04:00:12+08:00"


def item(title, pub, url="https://cdn.example.com/a.mp3", typ="audio/mpeg", guid=None, duration=None):
    parts = [f"<title>{title}</title>"] if title is not None else []
    if pub is not None:
        parts.append(f"<pubDate>{pub}</pubDate>")
    if url is not None:
        parts.append(f'<enclosure url="{url}" type="{typ}" length="1"/>')
    if guid is not None:
        parts.append(f"<guid>{guid}</guid>")
    if duration is not None:
        parts.append(f"<itunes:duration>{duration}</itunes:duration>")
    return "<item>" + "".join(parts) + "</item>"


def rss(*items):
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"><channel>'
            "<title>Show</title><language>zh-hk</language>" + "".join(items) +
            "</channel></rss>").encode("utf-8")


class FakeClient:
    def __init__(self, body, status=200):
        self.body, self.status = body, status

    def get(self, url, **kw):
        return (self.status, "application/rss+xml", self.body, url)


def recent(days_ago, hour=12):
    import datetime as dt
    import email.utils
    d = common.now_hkt() - dt.timedelta(days=days_ago)
    d = d.replace(hour=hour, minute=0, second=0, microsecond=0)
    return email.utils.format_datetime(d)


class HarvestEpisodeList(unittest.TestCase):
    def validate(self, body, status=200):
        return harvest.validate_feed(FakeClient(body, status), {"feedUrl": "https://feed.example.com/rss"})

    def test_newest_first_ties_keep_feed_order_and_cap(self):
        items = [item(f"Old {i}", recent(40 + i), url=f"https://cdn.example.com/old{i}.mp3") for i in range(35)]
        same = recent(1)
        items.insert(3, item("Tie A", same, url="https://cdn.example.com/ta.mp3"))
        items.insert(10, item("Tie B", same, url="https://cdn.example.com/tb.mp3"))
        items.append(item("Newest", recent(0, hour=1), url="https://cdn.example.com/new.mp3"))
        v = self.validate(rss(*items))
        eps = v["_episodes"]
        self.assertEqual(len(eps), common.SHOW_LIST_MAX)
        self.assertEqual([e["title"] for e in eps[:4]], ["Newest", "Tie A", "Tie B", "Old 0"])
        self.assertTrue(v["ok"])

    def test_only_listable_items_and_fields(self):
        v = self.validate(rss(
            item("  Good  ", recent(1), guid="  g-1  ", duration="1:02:03"),
            item("No date", None),
            item("Bad date", "not a date"),
            item("Video", recent(2), url="https://cdn.example.com/v.mp4", typ="video/mp4"),
            item("Relative", recent(3), url="/a.mp3"),
            item("   ", recent(4)),
            item("Zero length", recent(5), duration="0", guid="   "),
            item("No enclosure", recent(6), url=None),
        ))
        eps = v["_episodes"]
        self.assertEqual([e["title"] for e in eps], ["Good", "Zero length"])
        self.assertEqual(eps[0]["guid"], "g-1")
        self.assertEqual(eps[0]["durationSec"], 3723)
        self.assertNotIn("guid", eps[1])
        self.assertNotIn("durationSec", eps[1])
        self.assertEqual(set(eps[0]), {"title", "date", "enclosure", "durationSec", "guid"})
        self.assertEqual(v["latestEpisode"]["guid"], "g-1")

    def test_hk_calendar_date(self):
        # 2026-11-11 20:30 UTC is 2026-11-12 04:30 in Hong Kong.
        v = self.validate(rss(item("Late", "Wed, 11 Nov 2026 20:30:00 +0000")))
        self.assertEqual(v["_episodes"][0]["date"], "2026-11-12")

    def test_unreachable_feed_keeps_last_good_list(self):
        v = self.validate(b"", status=-1)
        self.assertNotIn("_episodes", v)
        with tempfile.TemporaryDirectory() as tmp:
            old = harvest.EPISODES_DIR, common.EPISODES_DIR
            try:
                harvest.EPISODES_DIR = common.EPISODES_DIR = Path(tmp)
                harvest.store_episode_list("itunes_1", [{"title": "a", "date": "2026-11-01",
                                                         "enclosure": "https://x/a.mp3"}])
                harvest.store_episode_list("itunes_1", None)        # fetch failed: keep
                self.assertTrue((Path(tmp) / "itunes_1.json").exists())
                harvest.store_episode_list("itunes_2", [{"title": "b", "date": "2026-11-01",
                                                         "enclosure": "https://x/b.mp3"}])
                self.assertEqual(harvest.prune_episode_lists({"itunes_1": {}}), 1)   # left the pool
                self.assertFalse((Path(tmp) / "itunes_2.json").exists())
                harvest.store_episode_list("itunes_1", [])          # parsed, nothing listable: drop
                self.assertFalse((Path(tmp) / "itunes_1.json").exists())
            finally:
                harvest.EPISODES_DIR, common.EPISODES_DIR = old


def ep(title, date, **kw):
    d = {"title": title, "date": date, "enclosure": f"https://cdn.example.com/{title}.mp3"}
    d.update(kw)
    return d


class BuildShowLists(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name) / "episodes"
        self.out = Path(self.tmp.name) / "show"
        self.data.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def stored(self, sid, episodes):
        (self.data / f"{sid}.json").write_text(json.dumps({"episodes": episodes}), encoding="utf-8")

    def build(self, ids, gen=GEN):
        return build_catalog.write_show_lists(ids, gen, self.data, self.out)

    def read(self, sid):
        return json.loads((self.out / f"{sid}.json").read_text(encoding="utf-8"))

    def test_writes_valid_document(self):
        self.stored("itunes_1", [ep("b", "2026-11-11", durationSec=60, guid="g"), ep("a", "2026-11-10")])
        self.build(["itunes_1"])
        doc = self.read("itunes_1")
        self.assertEqual(build_catalog.show_list_errors(doc, "itunes_1"), [])
        self.assertEqual(doc["showId"], "itunes_1")
        self.assertEqual(doc["generatedAt"], GEN)
        self.assertEqual(doc["catalogVersion"], common.CATALOG_VERSION)
        self.assertEqual([e["title"] for e in doc["episodes"]], ["b", "a"])
        self.assertEqual(doc["episodes"][0], ep("b", "2026-11-11", durationSec=60, guid="g"))

    def test_drops_invalid_episodes_and_unknown_fields_caps_at_30(self):
        eps = [ep(f"e{i:02d}", f"2026-10-{30 - i % 28:02d}", description="no") for i in range(40)]
        eps.insert(0, {"title": "", "date": "2026-11-11", "enclosure": "https://x/a.mp3"})
        eps.insert(1, ep("bad-date", "2026-13-01"))
        eps.insert(2, ep("bad-dur", "2026-11-11", durationSec=0))
        eps.insert(3, {"title": "ftp", "date": "2026-11-11", "enclosure": "ftp://x/a.mp3"})
        self.stored("itunes_1", eps)
        self.build(["itunes_1"])
        doc = self.read("itunes_1")
        self.assertEqual(len(doc["episodes"]), 30)
        self.assertEqual(build_catalog.show_list_errors(doc, "itunes_1"), [])
        self.assertTrue(all("description" not in e for e in doc["episodes"]))
        dates = [e["date"] for e in doc["episodes"]]
        self.assertEqual(dates, sorted(dates, reverse=True))

    def test_no_listable_episode_means_no_file(self):
        self.stored("itunes_1", [{"title": "x", "date": "bad", "enclosure": "https://x/a.mp3"}])
        self.build(["itunes_1", "itunes_2"])          # itunes_2 has no harvested list at all
        self.assertEqual(list(self.out.glob("*.json")), [])

    def test_prunes_shows_that_left_the_catalog(self):
        self.stored("itunes_1", [ep("a", "2026-11-10")])
        self.stored("itunes_2", [ep("b", "2026-11-10")])
        self.build(["itunes_1", "itunes_2"])
        self.assertEqual(sorted(p.stem for p in self.out.glob("*.json")), ["itunes_1", "itunes_2"])
        w, k, r, _ = self.build(["itunes_1"], gen="2026-11-13T04:00:00+08:00")
        self.assertEqual(sorted(p.stem for p in self.out.glob("*.json")), ["itunes_1"])
        self.assertEqual((w, k, r), (0, 1, 1))

    def test_unchanged_list_keeps_bytes_changed_list_rewrites(self):
        self.stored("itunes_1", [ep("a", "2026-11-10")])
        self.build(["itunes_1"])
        before = (self.out / "itunes_1.json").read_bytes()
        self.build(["itunes_1"], gen="2026-11-13T04:00:00+08:00")
        self.assertEqual((self.out / "itunes_1.json").read_bytes(), before)
        self.stored("itunes_1", [ep("b", "2026-11-12"), ep("a", "2026-11-10")])
        self.build(["itunes_1"], gen="2026-11-13T04:00:00+08:00")
        doc = self.read("itunes_1")
        self.assertEqual(doc["generatedAt"], "2026-11-13T04:00:00+08:00")
        self.assertEqual(doc["episodes"][0]["title"], "b")

    def test_ids_a_client_would_never_request_get_no_file(self):
        self.stored("bad.id", [ep("a", "2026-11-10")])
        self.build(["bad.id"])
        self.assertEqual(list(self.out.glob("*.json")), [])

    def test_schema_rejects(self):
        good = {"showId": "itunes_1", "generatedAt": GEN, "catalogVersion": 1, "episodes": [ep("a", "2026-11-10")]}
        self.assertEqual(build_catalog.show_list_errors(good, "itunes_1"), [])
        cases = {
            "wrong id": dict(good, showId="itunes_2"),
            "no episodes": dict(good, episodes=[]),
            "too many": dict(good, episodes=[ep("a", "2026-11-10")] * 31),
            "oldest first": dict(good, episodes=[ep("a", "2026-11-09"), ep("b", "2026-11-10")]),
            "bad generatedAt": dict(good, generatedAt="2026-11-12T04:00:12"),
            "string version": dict(good, catalogVersion="1"),
            "extra field": dict(good, feedBody="<rss/>"),
            "untrimmed guid": dict(good, episodes=[ep("a", "2026-11-10", guid=" g ")]),
            "bool duration": dict(good, episodes=[ep("a", "2026-11-10", durationSec=True)]),
        }
        for name, doc in cases.items():
            with self.subTest(name):
                self.assertNotEqual(build_catalog.show_list_errors(doc, "itunes_1"), [])

    def test_schema_file_matches_the_cap(self):
        schema = json.loads((common.REPO / "schema" / "show.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(schema["properties"]["episodes"]["maxItems"], common.SHOW_LIST_MAX)
        self.assertEqual(set(schema["properties"]["episodes"]["items"]["properties"]),
                         set(build_catalog.EPISODE_KEYS))


class PagesDeployPublishesShowLists(unittest.TestCase):
    def test_publish_step_copies_show_dir(self):
        wf = (common.REPO / ".github" / "workflows" / "pages-deploy.yml").read_text(encoding="utf-8")
        self.assertIn("cp -R show _site/show", wf)


if __name__ == "__main__":
    unittest.main()
