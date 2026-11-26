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

    def test_query_uses_single_clean_branch_without_or_or_quotes(self):
        # 修正 #05 驗證退回：YouTube search.list 多分支 OR 與雙引號鎖死會嚴重稀釋召回，改採單分支乾淨查詢。
        q = bf.build_youtube_query("IDOL", "防彈少年團")
        self.assertEqual(q, "IDOL BTS dance")
        self.assertNotIn("|", q)
        self.assertNotIn('"', q)

    def test_query_strips_feat_single_version_and_subtitles(self):
        q = bf.build_youtube_query("Last Christmas (Single Version)", "Wham!")
        self.assertEqual(q, "Last Christmas Wham! dance")
        self.assertNotIn("Single Version", q)
        self.assertNotIn("|", q)
        q2 = bf.build_youtube_query("seesaw (feat. IU)", "Crush")
        self.assertEqual(q2, "seesaw Crush dance")
        self.assertNotIn("feat.", q2)
        q3 = bf.build_youtube_query("Boy With Luv (feat. Halsey)", "防彈少年團")
        self.assertEqual(q3, "Boy With Luv BTS dance")
        self.assertNotIn("Halsey", q3)
        q4 = bf.build_youtube_query("沒有人像我一樣 (影集《欠妳的那場婚禮》主題曲)", "宇宙人")
        self.assertEqual(q4, "沒有人像我一樣 宇宙人 dance")
        self.assertNotIn("主題曲", q4)

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
    def test_regression_youre_under_arrest_and_neon_not_diluted_by_multibranch_query(self):
        # 重現 run 45572759481 退步：混入韓文多分支與雙引號導致 You're Under Arrest（19->4）與 NEON（6->3）跌破門檻。
        now = dt.datetime(2026, 11, 25, 8, 0, 0, tzinfo=dt.timezone.utc)

        q_yua = bf.build_youtube_query("You’re Under Arrest", "ILLIT", ["체포하겠어"], ["아일릿"])
        self.assertEqual(q_yua, "You're Under Arrest ILLIT dance")
        self.assertNotIn("|", q_yua)
        self.assertNotIn("체포하겠어", q_yua)

        yua_items = [
            _yt_item("y1", "ILLIT 'You're Under Arrest' Dance Practice", channel="ILLIT", channel_id="c1"),
            _yt_item("y2", "ILLIT Youre Under Arrest #kpopdancecover", channel="CrewA", channel_id="c2"),
            _yt_item("y3", "ILLIT #YoureUnderArrestChallenge", channel="CrewB", channel_id="c3"),
            _yt_item("y4", "아일릿 체포하겠어 챌린지", channel="CrewC", channel_id="c4"),
            _yt_item("y5", "아일릿 'You're Under Arrest' 안무 鏡面翻跳", channel="CrewD", channel_id="c5"),
            _yt_item("y6", "ILLIT You're Under Arrest Dance Reaction", channel="ReactTV", channel_id="c6"),
            _yt_item("y7", "ILLIT You're Under Arrest Lyrics", channel="LyricTV", channel_id="c7"),
        ]
        yua_stats = bf.evaluate_youtube_items(
            "You’re Under Arrest", "ILLIT", yua_items, now=now,
            alt_titles=["체포하겠어"], alt_artists=["아일릿"],
        )
        self.assertEqual(yua_stats["verified"], 5)
        self.assertGreaterEqual(yua_stats["verified"], bf.MIN_DANCE_VIDEOS)

        q_neon = bf.build_youtube_query("NEON", "A2O & A2O LITE")
        self.assertEqual(q_neon, "NEON A2O dance")
        self.assertNotIn("|", q_neon)

        neon_items = [
            _yt_item("n1", "A2O LITE 'NEON' Dance Challenge", channel="A2O Official", channel_id="c1"),
            _yt_item("n2", "A2O NEON #kpopdance Cover", channel="Dancer1", channel_id="c2"),
            _yt_item("n3", "A2O LITE #NEONdance", channel="Dancer2", channel_id="c3"),
            _yt_item("n4", "A2O & A2O LITE NEON 舞蹈翻跳", channel="Dancer3", channel_id="c4"),
            _yt_item("n5", "A2O NEON Choreography Tutorial", channel="Dancer4", channel_id="c5"),
            _yt_item("n6", "NEON Dance Challenge in Tokyo", channel="Random", channel_id="c6"),  # 缺演出者
            _yt_item("n7", "A2O NEON Vocal Cover", channel="Singer", channel_id="c7"),  # 非舞蹈
        ]
        neon_stats = bf.evaluate_youtube_items("NEON", "A2O & A2O LITE", neon_items, now=now)
        self.assertEqual(neon_stats["verified"], 5)
        self.assertGreaterEqual(neon_stats["verified"], bf.MIN_DANCE_VIDEOS)

    def test_subtitle_punctuation_and_dance_word_boundary_safety(self):
        now = dt.datetime(2026, 11, 25, 8, 0, 0, tzinfo=dt.timezone.utc)
        self.assertEqual(bf.build_youtube_query("404 (New Era)", "KiiiKiii"), "404 KiiiKiii dance")
        self.assertEqual(bf.build_youtube_query("RUDE!", "Hearts2Hearts"), "RUDE Hearts2Hearts dance")
        self.assertEqual(
            bf.build_youtube_query("The First Snow", "EXO", ["첫 눈"], ["엑소"]),
            "The First Snow EXO dance",
        )

        items = [
            _yt_item("k1", "KiiiKiii 404 Dance Challenge", channel="C1", channel_id="c1"),
            _yt_item("k2", "키키 404 댄스 커버", channel="C2", channel_id="c2"),
            _yt_item("k3", "HTTP 404 Dance Error", channel="DevTV", channel_id="c3"),  # 缺演出者
            _yt_item("k4", "KiiiKiii 404 Abundance of Joy", channel="FanTV", channel_id="c4"),  # abundance 非 dance
        ]
        st = bf.evaluate_youtube_items("404 (New Era)", "KiiiKiii", items, now=now, alt_artists=["키키"])
        self.assertEqual(st["verified"], 2)

    def test_compact_challenge_tags_still_require_artist_and_exclude_reactions(self):
        titles = ["IVE #LooksCanKillChallenge", "IVE #LooksCanKillChallengeArchive",
                  "Crew #LooksCanKillChallenge", "IVE #LooksCanKillChallenge reaction",
                  "IVE Looks Can Kill Dance Practice Reaction"]
        items = [_yt_item(str(i), title) for i, title in enumerate(titles)]
        result = bf.evaluate_youtube_items("Looks Can Kill", "IVE", items, dt.datetime.now(dt.timezone.utc))
        self.assertEqual(result["verified"], 1)

    def test_symbol_only_title_does_not_generate_bare_challenge_tag(self):
        now = dt.datetime(2026, 11, 25, 8, 0, 0, tzinfo=dt.timezone.utc)
        items = [
            _yt_item("s1", "Crew #challenge #dance", channel="Crew", channel_id="c1"),
            _yt_item("s2", "Crew #챌린지", channel="Crew", channel_id="c2"),
        ]
        st = bf.evaluate_youtube_items("!!!", "Crew", items, now=now)
        self.assertEqual(st["verified"], 0)

        underscore_items = [
            _yt_item("u1", "IVE #Looks_Can_Kill_Challenge", channel="C1", channel_id="c1"),
            _yt_item("u2", "IVE #Looks_Can_Kill_ChallengeArchive", channel="C2", channel_id="c2"),
        ]
        st2 = bf.evaluate_youtube_items("Looks Can Kill", "IVE", underscore_items, now=now)
        self.assertEqual(st2["verified"], 1)

    def test_select_query_entries_caps_first_pass_per_artist_and_backfills(self):
        pool = [
            {"item": _apple_item(f"b{i}", f"BTS Song {i}", "防彈少年團"), "twRank": i + 1}
            for i in range(6)
        ] + [
            {"item": _apple_item("bm1", "DESERVE", "BABYMONSTER"), "twRank": 7},
            {"item": _apple_item("ae1", "Whiplash", "aespa"), "twRank": 8},
        ]
        selected_five = bf.select_query_entries(pool, max_queries=5, first_pass_per_artist=3)
        ids_five = [e["item"]["id"] for e in selected_five]
        # 第一輪防彈只先查前 3 首，讓後段 BABYMONSTER 與 aespa 優先進入前 5 次配額
        self.assertEqual(ids_five, ["b0", "b1", "b2", "bm1", "ae1"])

        selected_all = bf.select_query_entries(pool, max_queries=8, first_pass_per_artist=3)
        ids_all = [e["item"]["id"] for e in selected_all]
        # 配額尚有餘裕時，剩餘的 BTS 曲目依序遞補
        self.assertEqual(ids_all, ["b0", "b1", "b2", "bm1", "ae1", "b3", "b4", "b5"])

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.out = Path(self.folder.name) / "feed.json"
        self.old = {"kind": "dance", "source": bf.YOUTUBE_SOURCE, "rank": 6, "videoCount": 12}
        self.out.write_text(json.dumps({"songs": [
            {"id": f"am:{i}", "signals": [self.old]} for i in (0, 2, 3, 4, 10)
        ]}))
        self.pool = [{"item": _apple_item(i, f"Song {i}", "Crew"), "twRank": i + 1} for i in range(11)]

    def run_feed(self, failures, raw_cache_out=""):
        def query(entry, *args):
            tid = int(entry["item"]["id"])
            if tid in failures:
                raise OSError("HTTP 429")
            count = 7 if tid == 3 else 0
            return dict(rawTotal=count, fetched=count, verified=count, uniqueChannels=count,
                        recent7d=count, matchedRaw=count, precision=float(bool(count)))

        env = {"YOUTUBE_API_KEY": "secret-fixture-key", "MAX_QUERIES": "10", "RAW_CACHE_OUT": raw_cache_out}
        with patch.object(bf, "OUT", self.out), patch.object(bf, "candidates", return_value=self.pool), \
             patch.dict(bf.os.environ, env), \
             patch.object(bf, "dance_signal", side_effect=query):
            bf.main([])

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
        raw_path = Path(self.folder.name) / "raw.json"
        with self.assertRaisesRegex(SystemExit, "4/10"):
            self.run_feed({0, 1, 2, 3}, raw_cache_out=str(raw_path))
        self.assertEqual(self.out.read_bytes(), old)
        # 即使超過 30% 失敗保留舊 feed，仍應先存下原始查詢快取供離線排錯，且絕不含 API 金鑰
        self.assertTrue(raw_path.exists())
        raw_text = raw_path.read_text(encoding="utf-8")
        self.assertNotIn("secret-fixture-key", raw_text)
        raw_doc = json.loads(raw_text)
        self.assertEqual(raw_doc["queried"], 10)
        self.assertEqual(raw_doc["failed"], 4)

    def test_raw_cache_saved_and_offline_replay_reproduces_feed_without_network(self):
        raw_path = Path(self.folder.name) / "youtube-raw-results.json"
        replay_out = Path(self.folder.name) / "replay-feed.json"
        now = dt.datetime(2026, 11, 25, 8, 0, 0, tzinfo=dt.timezone.utc)
        pool = [
            {
                "item": _apple_item("6818511627", "You’re Under Arrest", "ILLIT", "1734551937", "tw"),
                "storefront": "tw",
                "twRank": 1,
                "altTitles": ["체포하겠어", "You’re Under Arrest"],
                "altArtists": ["아일릿", "ILLIT"],
            },
            {
                "item": _apple_item("6820428730", "NEON", "A2O & A2O LITE", "999", "tw"),
                "storefront": "tw",
                "twRank": 2,
                "altTitles": ["NEON"],
                "altArtists": ["A2O & A2O LITE"],
            },
        ]

        def fake_get_json(url, *args, **kwargs):
            if "You%27re+Under+Arrest" in url or "You're+Under+Arrest" in url:
                return {
                    "pageInfo": {"totalResults": 12},
                    "items": [
                        _yt_item(f"v{i}", f"ILLIT You're Under Arrest Dance Cover #{i}",
                                 channel=f"Ch{i}", channel_id=f"ch{i}", published_at="2026-11-24T10:00:00Z")
                        for i in range(6)
                    ],
                }
            return {
                "pageInfo": {"totalResults": 2},
                "items": [
                    _yt_item("m1", "A2O NEON Dance Challenge", channel="C1", channel_id="c1", published_at="2026-11-24T10:00:00Z"),
                ],
            }

        with patch.object(bf, "OUT", self.out), patch.object(bf, "candidates", return_value=pool), \
             patch.object(bf, "get_json", side_effect=fake_get_json), \
             patch.dict(bf.os.environ, {"YOUTUBE_API_KEY": "top-secret-token", "MAX_QUERIES": "5", "RAW_CACHE_OUT": str(raw_path)}):
            bf.main([])

        raw_text = raw_path.read_text(encoding="utf-8")
        self.assertNotIn("top-secret-token", raw_text)
        raw_doc = json.loads(raw_text)
        self.assertEqual(len(raw_doc["entries"]), 2)
        self.assertEqual(raw_doc["entries"][0]["query"], "You're Under Arrest ILLIT dance")
        self.assertEqual(len(raw_doc["entries"][0]["items"]), 6)
        self.assertEqual(raw_doc["entries"][0]["stats"]["verified"], 6)

        # 離線重播模式不得呼叫 candidates 或 get_json，仍可產出相同舞蹈訊號
        with patch.object(bf, "OUT", replay_out), \
             patch.object(bf, "candidates", side_effect=AssertionError("offline replay must not hit Apple RSS")), \
             patch.object(bf, "get_json", side_effect=AssertionError("offline replay must not hit YouTube API")):
            bf.main(["--replay", str(raw_path)])

        live_feed = json.loads(self.out.read_text(encoding="utf-8"))
        replay_feed = json.loads(replay_out.read_text(encoding="utf-8"))
        self.assertEqual(replay_feed["songs"], live_feed["songs"])
        yua_song = next(s for s in replay_feed["songs"] if s["id"] == "am:6818511627")
        dance_sigs = [s for s in yua_song["signals"] if s["kind"] == "dance"]
        self.assertEqual(len(dance_sigs), 1)
        self.assertEqual(dance_sigs[0]["videoCount"], 6)


class YouTubePopularAndShortsTests(unittest.TestCase):
    def test_parse_iso8601_duration(self):
        self.assertEqual(bf.parse_iso8601_duration("PT45S"), 45)
        self.assertEqual(bf.parse_iso8601_duration("PT1M"), 60)
        self.assertEqual(bf.parse_iso8601_duration("PT1M1S"), 61)
        self.assertEqual(bf.parse_iso8601_duration("PT3M22S"), 202)
        self.assertEqual(bf.parse_iso8601_duration("PT1H2M3S"), 3723)
        self.assertEqual(bf.parse_iso8601_duration("P0D"), 0)
        self.assertIsNone(bf.parse_iso8601_duration(""))
        self.assertIsNone(bf.parse_iso8601_duration("invalid"))

    def test_parse_popular_video_title_handles_common_mv_formats(self):
        self.assertEqual(
            bf.parse_popular_video_title("[MV] IVE - Looks Can Kill (Official Music Video)", "STARSHIP"),
            ("IVE", "Looks Can Kill"),
        )
        self.assertEqual(
            bf.parse_popular_video_title("ILLIT (아일릿) 'You're Under Arrest' Official MV", "HYBE LABELS"),
            ("ILLIT (아일릿)", "You're Under Arrest"),
        )
        self.assertEqual(
            bf.parse_popular_video_title("周杰倫「晴天」官方完整版 MV", "周杰倫 Jay Chou"),
            ("周杰倫", "晴天"),
        )
        self.assertEqual(
            bf.parse_popular_video_title("APT. (Official Video)", "ROSÉ Official"),
            ("ROSÉ", "APT."),
        )
        # 聚合頻道無法拆分時不誤把唱片公司當演出者
        self.assertIsNone(bf.parse_popular_video_title("Special Stage Teaser", "HYBE LABELS"))

    def test_merge_candidates_preserves_kr_rank_alongside_tw_rank(self):
        tw = [_apple_item("100", "Looks Can Kill", "IVE", "1594159996", "tw")]
        kr = [
            _apple_item("200", "Looks Can Kill", "아이브", "1594159996", "kr"),
            _apple_item("300", "Inside Out", "RESCENE", "1732658659", "kr"),
        ]
        merged = bf.merge_candidates({"tw": tw, "kr": kr}, lookup_fn=lambda ids, c: [])
        by_id = {e["item"]["id"]: e for e in merged}
        self.assertEqual(by_id["100"]["twRank"], 1)
        self.assertEqual(by_id["100"]["krRank"], 1)
        self.assertIsNone(by_id["300"]["twRank"])
        self.assertEqual(by_id["300"]["krRank"], 2)

    def test_enrich_pool_with_youtube_popular_matches_existing_and_adds_new_song(self):
        pool = [
            {
                "item": _apple_item("100", "Looks Can Kill", "IVE", "1594159996", "tw"),
                "storefront": "tw",
                "twRank": 5,
                "krRank": 3,
                "ytPopularRank": None,
                "altTitles": ["Looks Can Kill"],
                "altArtists": ["IVE", "아이브"],
            }
        ]
        popular = [
            {
                "id": "yt1",
                "snippet": {
                    "title": "IVE 아이브 'Looks Can Kill' MV",
                    "channelTitle": "STARSHIP",
                },
                "contentDetails": {"duration": "PT3M15S"},
            },
            {
                "id": "yt2",
                "snippet": {
                    "title": "ROSÉ - APT. (Official Music Video)",
                    "channelTitle": "ROSÉ",
                },
                "contentDetails": {"duration": "PT2M53S"},
            },
        ]

        def fake_itunes_search(term, country="tw", limit=5):
            if "APT." in term:
                return [{
                    "trackId": 999001,
                    "trackName": "APT.",
                    "artistName": "ROSÉ & Bruno Mars",
                    "artistId": 12345,
                    "trackViewUrl": "https://music.apple.com/tw/album/apt/1?i=999001",
                    "artworkUrl100": "https://is1-ssl.mzstatic.com/image/thumb/apt/100x100bb.jpg",
                }]
            return []

        enriched = bf.enrich_pool_with_youtube_popular(pool, popular, search_fn=fake_itunes_search)
        self.assertEqual(len(enriched), 2)
        by_id = {e["item"]["id"]: e for e in enriched}
        self.assertEqual(by_id["100"]["ytPopularRank"], 1)
        self.assertEqual(by_id["999001"]["ytPopularRank"], 2)
        self.assertEqual(by_id["999001"]["storefront"], "yt_popular")
        self.assertIsNone(by_id["999001"]["twRank"])

    def test_shorts_signal_and_popular_signal_output_and_offline_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "feed.json"
            raw_path = Path(tmp) / "youtube-raw-results.json"
            replay_out = Path(tmp) / "replay-feed.json"

            pool = [
                {
                    "item": _apple_item("6818511627", "You’re Under Arrest", "ILLIT", "1734551937", "tw"),
                    "storefront": "tw",
                    "twRank": 10,
                    "krRank": 4,
                    "ytPopularRank": None,
                    "altTitles": ["체포하겠어", "You’re Under Arrest"],
                    "altArtists": ["아일릿", "ILLIT"],
                },
            ]

            def fake_get_json(url, *args, **kwargs):
                if "chart=mostPopular" in url:
                    return {
                        "items": [
                            {
                                "id": "pop1",
                                "snippet": {
                                    "title": "ILLIT (아일릿) 'You're Under Arrest' Official MV",
                                    "channelTitle": "HYBE LABELS",
                                },
                                "contentDetails": {"duration": "PT3M05S"},
                            }
                        ]
                    }
                if "/youtube/v3/search?" in url:
                    return {
                        "pageInfo": {"totalResults": 10},
                        "items": [
                            _yt_item(f"s{i}", f"ILLIT You're Under Arrest Dance Challenge #{i}",
                                     channel=f"Ch{i}", channel_id=f"ch{i}", published_at="2026-11-24T10:00:00Z")
                            for i in range(6)
                        ] + [
                            _yt_item("long1", "ILLIT You're Under Arrest Full Dance Practice",
                                     channel="ILLIT", channel_id="ch_long", published_at="2026-11-24T10:00:00Z")
                        ],
                    }
                if "/youtube/v3/videos?" in url and "part=contentDetails" in url:
                    return {
                        "items": [
                            {"id": f"s{i}", "contentDetails": {"duration": "PT42S"}} for i in range(5)
                        ] + [
                            {"id": "s5", "contentDetails": {"duration": "PT1M01S"}},  # 61 秒不算 Shorts
                            {"id": "long1", "contentDetails": {"duration": "PT3M12S"}},
                        ]
                    }
                return {"items": []}

            with patch.object(bf, "OUT", out_path), \
                 patch.object(bf, "candidates", return_value=pool), \
                 patch.object(bf, "get_json", side_effect=fake_get_json), \
                 patch.dict(bf.os.environ, {
                     "YOUTUBE_API_KEY": "secret-key-8",
                     "MAX_QUERIES": "5",
                     "RAW_CACHE_OUT": str(raw_path),
                 }):
                bf.main([])

            live_feed = json.loads(out_path.read_text(encoding="utf-8"))
            self.assertEqual(len(live_feed["songs"]), 1)
            sigs = {(s["kind"], s["source"]): s for s in live_feed["songs"][0]["signals"]}
            self.assertEqual(sigs[("streaming", bf.STREAMING_SOURCE)]["rank"], 10)
            self.assertEqual(sigs[("streaming", bf.KR_STREAMING_SOURCE)]["rank"], 4)
            self.assertEqual(sigs[("streaming", bf.YOUTUBE_POPULAR_SOURCE)]["rank"], 1)
            self.assertEqual(sigs[("dance", bf.YOUTUBE_SOURCE)]["videoCount"], 7)
            self.assertEqual(sigs[("dance", bf.YOUTUBE_SHORTS_SOURCE)]["videoCount"], 5)

            raw_text = raw_path.read_text(encoding="utf-8")
            self.assertNotIn("secret-key-8", raw_text)

            with patch.object(bf, "OUT", replay_out), \
                 patch.object(bf, "candidates", side_effect=AssertionError("offline replay must not hit Apple RSS")), \
                 patch.object(bf, "get_json", side_effect=AssertionError("offline replay must not hit YouTube API")):
                bf.main(["--replay", str(raw_path)])

            replay_feed = json.loads(replay_out.read_text(encoding="utf-8"))
            self.assertEqual(replay_feed["songs"], live_feed["songs"])


if __name__ == "__main__":
    unittest.main()
