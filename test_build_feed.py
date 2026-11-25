"""build_feed.py 跨榜去重與常見曲名舞蹈數校正單元測試（不呼叫外部網路）。"""
from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import build_feed as bf


def _apple_item(tid, name, artist, aid="", sf="tw"):
    return {
        "id": str(tid),
        "name": name,
        "artistName": artist,
        "artistId": str(aid),
        "url": f"https://music.apple.com/{sf}/album/test/1?i={tid}",
        "artworkUrl100": "https://is1-ssl.mzstatic.com/image/thumb/test/100x100bb.jpg",
    }


def _yt_item(vid, title, channel="Dancer", channel_id="ch1", desc="", published_at="2026-11-24T12:00:00Z"):
    return {
        "id": {"videoId": vid},
        "snippet": {
            "title": title,
            "channelTitle": channel,
            "channelId": channel_id,
            "description": desc,
            "publishedAt": published_at,
        },
    }


class CrossChartDedupeTests(unittest.TestCase):
    def test_merges_same_track_id_with_localized_title_and_artist_and_prefers_tw(self):
        tw = [_apple_item("6818511627", "You’re Under Arrest", "ILLIT", "1734551937", "tw")]
        kr = [_apple_item("6818511627", "체포하겠어", "아일릿", "1734551937", "kr")]
        # KR 在前、TW 在後：KR 先進池，之後 TW 補上應升級為台灣名稱與連結並保留韓文別名
        lists = {
            "tw": [_apple_item("1", "Other", "Other", "10", "tw"), tw[0]],
            "kr": [kr[0]],
        }
        merged = bf.merge_candidates(lists, lookup_fn=lambda ids, c: [])
        illit = [e for e in merged if e["item"]["id"] == "6818511627"]
        self.assertEqual(len(illit), 1)
        self.assertEqual(illit[0]["item"]["name"], "You’re Under Arrest")
        self.assertEqual(illit[0]["item"]["artistName"], "ILLIT")
        self.assertEqual(illit[0]["twRank"], 2)
        self.assertIn("체포하겠어", illit[0]["altTitles"])
        self.assertIn("아일릿", illit[0]["altArtists"])

    def test_merges_different_track_ids_by_artist_id_and_title_looks_can_kill(self):
        # 真實案例：Looks Can Kill 在台灣與韓國榜有不同的 Apple track ID 與演出者譯名，但同 artistId
        tw = [_apple_item("6822369089", "Looks Can Kill", "IVE", "1594159996", "tw")]
        kr = [_apple_item("6820422075", "Looks Can Kill", "아이브", "1594159996", "kr")]
        lists = {
            "tw": [_apple_item("1", "SongA", "ArtA", "1", "tw"), tw[0]],
            "kr": [kr[0]],
        }
        merged = bf.merge_candidates(lists, lookup_fn=lambda ids, c: [])
        lck = [e for e in merged if "Looks Can Kill" in e["item"]["name"]]
        self.assertEqual(len(lck), 1)
        self.assertEqual(lck[0]["item"]["id"], "6822369089")
        self.assertEqual(lck[0]["item"]["artistName"], "IVE")
        self.assertEqual(lck[0]["twRank"], 2)
        self.assertIn("/tw/", lck[0]["item"]["url"])
        self.assertIn("아이브", lck[0]["altArtists"])

    def test_merges_without_artist_id_using_alias_table(self):
        tw = [_apple_item("100", "Looks Can Kill", "IVE", "", "tw")]
        kr = [_apple_item("200", "Looks Can Kill", "아이브", "", "kr")]
        merged = bf.merge_candidates({"tw": tw, "kr": kr}, lookup_fn=lambda ids, c: [])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["item"]["artistName"], "IVE")

    def test_merges_bilingual_parenthetical_title(self):
        tw = [_apple_item("100", "GiiiRIP", "BIGBANG", "318754656", "tw")]
        kr = [_apple_item("200", "기립 (GiiiRIP)", "BIGBANG", "318754656", "kr")]
        merged = bf.merge_candidates({"tw": tw, "kr": kr}, lookup_fn=lambda ids, c: [])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["item"]["name"], "GiiiRIP")

    def test_localizes_kr_only_artist_to_tw_name_for_artist_cap_consistency(self):
        # RESCENE 在台榜有 With，韓榜另有 Inside Out（리센느）；兩者應統一為 RESCENE
        tw = [_apple_item("6828335406", "With", "RESCENE", "1732658659", "tw")]
        kr = [_apple_item("6828335411", "백화요란 (Inside Out)", "리센느", "1732658659", "kr")]

        def fake_lookup(ids, country):
            if "6828335411" in ids:
                return [{
                    "trackId": 6828335411,
                    "trackName": "Inside Out",
                    "artistName": "RESCENE",
                    "trackViewUrl": "https://music.apple.com/tw/album/inside-out/1?i=6828335411",
                }]
            return []

        merged = bf.merge_candidates({"tw": tw, "kr": kr}, lookup_fn=fake_lookup)
        self.assertEqual(len(merged), 2)
        artists = {e["item"]["artistName"] for e in merged}
        self.assertEqual(artists, {"RESCENE"})
        inside_out = [e for e in merged if e["item"]["id"] == "6828335411"][0]
        self.assertEqual(inside_out["item"]["name"], "Inside Out")
        self.assertIn("백화요란 (Inside Out)", inside_out["altTitles"])
        self.assertIn("리센느", inside_out["altArtists"])


class CommonTitleCalibrationTests(unittest.TestCase):
    def setUp(self):
        self.now = dt.datetime(2026, 11, 25, 3, 0, 0, tzinfo=dt.timezone.utc)

    def test_query_quotes_title_and_artist_in_every_or_branch(self):
        q = bf.build_youtube_query("IDOL", "防彈少年團")
        branches = q.split("|")
        self.assertGreaterEqual(len(branches), 2)
        for b in branches:
            self.assertIn('"IDOL"', b)
            self.assertIn('"BTS"', b)

    def test_query_strips_feat_and_single_version(self):
        q = bf.build_youtube_query("Last Christmas (Single Version)", "Wham!")
        self.assertIn('"Last Christmas"', q)
        self.assertNotIn("Single Version", q)
        q2 = bf.build_youtube_query("seesaw (feat. IU)", "Crush")
        self.assertIn('"seesaw"', q2)
        self.assertNotIn("feat.", q2)
        q3 = bf.build_youtube_query("Boy With Luv (feat. Halsey)", "防彈少年團")
        self.assertIn('"Boy With Luv"', q3)
        self.assertIn('"BTS"', q3)
        self.assertNotIn("Halsey", q3)

    def test_common_title_idol_rejects_non_bts_and_counts_real_covers(self):
        items = [
            _yt_item("v1", "K-POP IDOL Dance Challenge Compilation", channel="MixTV", channel_id="c1"),
            _yt_item("v2", "New IDOL Group Debut Dance", channel="KpopNews", channel_id="c2"),
            _yt_item("v3", "BTS 'IDOL' Dance Cover in Public", channel="DanceCrew1", channel_id="c3"),
            _yt_item("v4", "防彈少年團 IDOL 舞蹈翻跳 鏡面", channel="TWStudio", channel_id="c4"),
            _yt_item("v5", "방탄소년단 IDOL 안무 연습", channel="KRStudio", channel_id="c5"),
            _yt_item("v6", "BTS IDOL Official MV", channel="HYBE LABELS", channel_id="c6"),  # 無舞蹈關鍵字
        ]
        st = bf.evaluate_youtube_items("IDOL", "防彈少年團", items, now=self.now)
        self.assertEqual(st["verified"], 3)
        self.assertEqual(st["uniqueChannels"], 3)

    def test_common_title_please_does_not_match_description_boilerplate(self):
        items = [
            _yt_item(
                "v1",
                "BTS Jungkook Dance Practice",
                channel="Fans",
                channel_id="c1",
                desc="Please like and subscribe! #BTS #dance",
            ),
            _yt_item("v2", "BTS 'Please' Dance Cover", channel="Crew", channel_id="c2"),
        ]
        st = bf.evaluate_youtube_items("Please", "防彈少年團", items, now=self.now)
        self.assertEqual(st["verified"], 1)

    def test_common_title_with_requires_word_boundary_and_artist(self):
        items = [
            _yt_item("v1", "Dance Without You - RESCENE", channel="C1", channel_id="c1"),
            _yt_item("v2", "Dance With Me Challenge", channel="C2", channel_id="c2"),
            _yt_item("v3", "RESCENE (리센느) 'With' Dance Practice", channel="RESCENE", channel_id="c3"),
            _yt_item("v4", "리센느 With 안무 챌린지", channel="C4", channel_id="c4"),
        ]
        st = bf.evaluate_youtube_items(
            "With", "RESCENE", items, now=self.now, alt_artists=["리센느"]
        )
        self.assertEqual(st["verified"], 2)

    def test_short_artist_ive_does_not_match_live_or_five(self):
        items = [
            _yt_item("v1", "Looks Can Kill Live Dance Cover", channel="FiveDancers", channel_id="c1"),
            _yt_item("v2", "IVE 'Looks Can Kill' Dance Challenge", channel="Starship", channel_id="c2"),
            _yt_item("v3", "#IVE_LooksCanKill 舞蹈翻跳", channel="TW", channel_id="c3"),
            _yt_item("v4", "아이브 Looks Can Kill 챌린지", channel="KR", channel_id="c4"),
        ]
        st = bf.evaluate_youtube_items(
            "Looks Can Kill", "IVE", items, now=self.now, alt_artists=["아이브"]
        )
        self.assertEqual(st["verified"], 3)

    def test_html_entities_and_apostrophe_variants_match(self):
        items = [
            _yt_item("v1", "ILLIT It&#39;s Me Dance Challenge", channel="C1", channel_id="c1"),
            _yt_item("v2", "ILLIT Its Me Dance Cover", channel="C2", channel_id="c2"),
            _yt_item("v3", "아일릿 체포하겠어 챌린지", channel="C3", channel_id="c3"),
        ]
        st1 = bf.evaluate_youtube_items("It's Me", "ILLIT", items[:2], now=self.now)
        self.assertEqual(st1["verified"], 2)
        st2 = bf.evaluate_youtube_items(
            "You’re Under Arrest", "ILLIT", items[2:], now=self.now,
            alt_titles=["체포하겠어"], alt_artists=["아일릿"]
        )
        self.assertEqual(st2["verified"], 1)

    def test_non_dance_covers_rejected(self):
        items = [
            _yt_item("v1", "周杰倫 晴天 吉他彈唱 Cover", channel="C1", channel_id="c1"),
            _yt_item("v2", "Mariah Carey All I Want for Christmas Is You Vocal Challenge", channel="C2", channel_id="c2"),
            _yt_item("v3", "Mariah Carey All I Want for Christmas Is You Lyrics", channel="C3", channel_id="c3"),
        ]
        self.assertEqual(bf.evaluate_youtube_items("晴天", "周杰倫", items[:1], now=self.now)["verified"], 0)
        self.assertEqual(
            bf.evaluate_youtube_items("All I Want for Christmas Is You", "Mariah Carey", items[1:], now=self.now)["verified"],
            0,
        )

    def test_single_channel_spam_is_capped(self):
        items = [
            _yt_item(f"v{i}", f"IVE Looks Can Kill Dance Challenge #{i}", channel="SpamBot", channel_id="same")
            for i in range(10)
        ]
        st = bf.evaluate_youtube_items("Looks Can Kill", "IVE", items, now=self.now)
        self.assertEqual(st["matchedRaw"], 10)
        self.assertEqual(st["verified"], bf.MAX_PER_CHANNEL)
        self.assertLess(st["verified"], bf.MIN_DANCE_VIDEOS)


class CalibrationAndFailureTests(unittest.TestCase):
    def test_korean_queries_keep_song_and_artist(self):
        query = bf.build_youtube_query("You’re Under Arrest", "ILLIT", ["체포하겠어"], ["아일릿"])
        self.assertIn('"체포하겠어" "아일릿" 챌린지', query.split("|"))
        self.assertIn('"You\'re Under Arrest" "ILLIT" dance', query.split("|"))

    def test_compact_challenge_tags_still_require_artist_and_exclude_reactions(self):
        titles = ["IVE #LooksCanKillChallenge", "IVE #LooksCanKillChallengeArchive",
                  "Crew #LooksCanKillChallenge", "IVE #LooksCanKillChallenge reaction",
                  "IVE Looks Can Kill Dance Practice Reaction"]
        items = [_yt_item(str(i), title) for i, title in enumerate(titles)]
        result = bf.evaluate_youtube_items("Looks Can Kill", "IVE", items, dt.datetime.now(dt.timezone.utc))
        self.assertEqual(result["verified"], 1)

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.out = Path(self.folder.name) / "feed.json"
        self.old = {"kind": "dance", "source": bf.YOUTUBE_SOURCE, "rank": 6, "videoCount": 12}
        self.out.write_text(json.dumps({"songs": [
            {"id": f"am:{i}", "signals": [self.old]} for i in (0, 2, 3, 4, 10)
        ]}))
        self.pool = [{"item": _apple_item(i, f"Song {i}", "Crew"), "twRank": i + 1} for i in range(11)]

    def run_feed(self, failures):
        def query(entry, *args):
            tid = int(entry["item"]["id"])
            if tid in failures:
                raise OSError("HTTP 429")
            count = 7 if tid == 3 else 0
            return dict(rawTotal=count, fetched=count, verified=count, uniqueChannels=count,
                        recent7d=count, matchedRaw=count, precision=float(bool(count)))

        with patch.object(bf, "OUT", self.out), patch.object(bf, "candidates", return_value=self.pool), \
             patch.dict(bf.os.environ, {"YOUTUBE_API_KEY": "fixture", "MAX_QUERIES": "10"}), \
             patch.object(bf, "dance_signal", side_effect=query):
            bf.main()

    def test_exactly_thirty_percent_carries_only_failed_ids_with_old_signals(self):
        self.run_feed({0, 1, 2})
        songs = json.loads(self.out.read_text())["songs"]
        signals = {song["id"]: [s for s in song["signals"] if s["kind"] == "dance"] for song in songs}
        self.assertEqual(signals["am:0"], [self.old])
        self.assertEqual(signals["am:1"], [])
        self.assertEqual(signals["am:2"], [self.old])
        self.assertEqual(signals["am:3"], [{**self.old, "rank": 1, "videoCount": 7, "previousRank": 6}])
        self.assertEqual(signals["am:4"], [])  # 成功查到零支
        self.assertEqual(signals["am:10"], [])  # 未列入當日查詢

    def test_above_thirty_percent_preserves_entire_file(self):
        old = self.out.read_bytes()
        with self.assertRaisesRegex(SystemExit, "4/10"):
            self.run_feed({0, 1, 2, 3})
        self.assertEqual(self.out.read_bytes(), old)


if __name__ == "__main__":
    unittest.main()
