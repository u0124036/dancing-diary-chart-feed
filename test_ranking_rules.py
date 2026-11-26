"""dance-chart-daily#10（D13＋D14）單元測試：季節歌上限／降權、韓日舞曲加權、App 排名預演、
同歌名不同歌手辨識，以及演唱會 dance break／直拍計入熱度（不呼叫外部網路）。"""
from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import build_feed as bf
from test_build_feed import _apple_item, _yt_item


def _entry(tid, title, artist, alt_titles=(), alt_artists=(), genres=(), tw_rank=None):
    return {
        "item": _apple_item(tid, title, artist),
        "storefront": "tw",
        "twRank": tw_rank,
        "krRank": None,
        "ytPopularRank": None,
        "altTitles": [title, *alt_titles],
        "altArtists": [artist, *alt_artists],
        "genres": list(genres),
    }


def _song(sid, artist, dance_rank=None, video_count=None, streaming_rank=None, previous_rank=None, title=None):
    signals = []
    if streaming_rank:
        signals.append({"kind": "streaming", "source": bf.STREAMING_SOURCE, "rank": streaming_rank})
    if dance_rank:
        sig = {"kind": "dance", "source": bf.YOUTUBE_SOURCE, "rank": dance_rank}
        if video_count is not None:
            sig["videoCount"] = video_count
        if previous_rank is not None:
            sig["previousRank"] = previous_rank
        signals.append(sig)
    return {"id": sid, "title": title or sid, "artist": artist, "officialDance": [], "signals": signals}


class SeasonalAndLanguageWeightTests(unittest.TestCase):
    """〔D13〕季節歌辨識／上限與韓日舞曲加權。"""

    def test_seasonal_detection_by_title_only(self):
        seasonal = [
            ("All I Want for Christmas Is You", "Mariah Carey"),
            ("Last Christmas (Single Version)", "Wham!"),
            ("Santa Tell Me", "Ariana Grande"),
            ("The First Snow", "EXO"),
            ("첫 눈", "EXO"),
            ("聖誕結", "陳奕迅"),
            ("Let It Snow! Let It Snow! Let It Snow!", "Dean Martin"),
        ]
        for title, artist in seasonal:
            self.assertTrue(bf.is_seasonal_song(_entry("1", title, artist)), title)
        regular = [
            ("LEMONADE", "aespa"), ("With", "RESCENE"), ("Dynamite", "防彈少年團"),
            ("Snowfall", "Someone"), ("Winter Bear", "V"), ("Santana Row", "Band"),
            ("When it snows (feat. HEIZE)", "Lee Mujin"),
        ]
        for title, artist in regular:
            self.assertFalse(bf.is_seasonal_song(_entry("1", title, artist)), title)
        # 季節字詞只出現在演出者名稱時不算季節歌
        self.assertFalse(bf.is_seasonal_song(_entry("1", "Dance", "Christmas Band")))

    def test_kj_language_detection_and_exclusions(self):
        self.assertEqual(bf.kj_language(_entry("1", "NEON", "A2O & A2O LITE", genres=["韓國流行樂", "音樂"])), "ko")
        self.assertEqual(bf.kj_language(_entry("1", "Inside Out", "RESCENE", alt_titles=["백화요란 (Inside Out)"])), "ko")
        self.assertEqual(bf.kj_language(_entry("1", "Looks Can Kill", "IVE")), "ko")  # 別名表韓團
        self.assertEqual(bf.kj_language(_entry("1", "Golden", "HUNTR/X, EJAE, AUDREY NUNA & REI AMI")), "ko")
        self.assertEqual(bf.kj_language(_entry("1", "Heroine", "back number", alt_artists=["백 넘버"])), "ja")
        self.assertEqual(bf.kj_language(_entry("1", "アイドル", "YOASOBI")), "ja")
        self.assertEqual(bf.kj_language(_entry("1", "Bling-Bang-Bang-Born", "Creepy Nuts")), "ja")
        self.assertEqual(bf.kj_language(_entry("1", "Hikari", "Someone", genres=["日本流行樂"])), "ja")
        self.assertIsNone(bf.kj_language(_entry("1", "Housewife", "Tate McRae", genres=["流行樂"])))
        self.assertIsNone(bf.kj_language(_entry("1", "擱淺", "周杰倫", genres=["華語流行樂"])))
        self.assertIsNone(bf.kj_language(_entry("1", "Santa Tell Me", "Ariana Grande")))

    def test_dance_rank_weight_has_explicit_caps(self):
        self.assertEqual(bf.dance_rank_weight(_entry("1", "Housewife", "Tate McRae")), 1.0)
        # 韓日加權上限 1.20：同時有韓文字與假名也不疊加
        both = _entry("1", "Love Options", "BESTie", alt_titles=["연애의 조건", "恋愛の条件"], genres=["韓國流行樂"])
        self.assertAlmostEqual(bf.dance_rank_weight(both), 1.20)
        self.assertAlmostEqual(bf.dance_rank_weight(_entry("1", "Santa Tell Me", "Ariana Grande")), 0.80)
        self.assertAlmostEqual(bf.dance_rank_weight(_entry("1", "The First Snow", "EXO")), 0.96)

    def _run(self, pool, counts):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        out = Path(tmp.name) / "feed.json"

        def query(entry, *args):
            n = counts.get(entry["item"]["id"], 0)
            return dict(rawTotal=n, fetched=n, verified=n, uniqueChannels=n, recent7d=n,
                        matchedRaw=n, precision=1.0, verifiedVideos=[])

        with patch.object(bf, "OUT", str(out)), patch.object(bf, "candidates", return_value=pool), \
             patch.object(bf, "fetch_youtube_popular_music", return_value=[]), \
             patch.object(bf, "fetch_video_durations", return_value={}), \
             patch.dict(bf.os.environ, {"YOUTUBE_API_KEY": "k", "MAX_QUERIES": "90", "RAW_CACHE_OUT": ""}), \
             patch.object(bf, "dance_signal", side_effect=query):
            bf.main([])
        songs = json.loads(out.read_text())["songs"]
        ranks = {
            s["id"]: (sig["rank"], sig["videoCount"])
            for s in songs for sig in s["signals"] if sig["source"] == bf.YOUTUBE_SOURCE
        }
        return songs, ranks

    def test_kj_boost_reorders_close_counts_but_not_clearly_lower_heat(self):
        pool = [
            _entry("w", "Housewife", "Tate McRae"),
            _entry("k", "LEMONADE", "aespa", genres=["韓國流行樂"]),
            _entry("w2", "Big Song", "Western Star"),
        ]
        # 30×1.2=36 > 34：韓曲反超熱度相近的西洋曲；videoCount 仍是實測 30
        _, ranks = self._run(pool, {"w": 34, "k": 30, "w2": 37})
        self.assertEqual(ranks["am:k"], (2, 30))
        self.assertEqual(ranks["am:w"], (3, 34))
        # 上限 1.20：37 > 36，熱度明顯較高的西洋曲不被反超
        self.assertEqual(ranks["am:w2"], (1, 37))

    def test_seasonal_cap_keeps_only_hottest_seasonal_in_top_15_and_drops_the_rest_in_top(self):
        pool = [_entry(f"r{i}", f"Regular {i}", f"Artist {i}") for i in range(16)]
        pool += [
            _entry("x1", "All I Want for Christmas Is You", "Mariah Carey"),
            _entry("x2", "Last Christmas", "Wham!"),
            _entry("x3", "Santa Tell Me", "Ariana Grande"),
        ]
        counts = {f"r{i}": 30 - i for i in range(16)}  # 30..15
        counts.update({"x1": 90, "x2": 80, "x3": 70})
        songs, ranks = self._run(pool, counts)
        top_ids = [s["id"] for s, _ in bf.app_rank_preview(songs)]
        self.assertEqual(len(top_ids), 15)
        seasonal_in_top = [i for i in top_ids if i in ("am:x1", "am:x2", "am:x3")]
        # 前 15 名最多 1 首季節歌，且保留熱度最高的那首（不封殺：降權後仍可拿第 1 名）
        self.assertEqual(seasonal_in_top, ["am:x1"])
        self.assertEqual(top_ids[0], "am:x1")
        self.assertEqual(ranks["am:x1"], (1, 90))
        ids = {s["id"] for s in songs}
        self.assertNotIn("am:x2", ids)
        self.assertNotIn("am:x3", ids)

    def test_seasonal_song_outside_top_15_is_kept_in_feed(self):
        pool = [_entry(f"r{i}", f"Regular {i}", f"Artist {i}") for i in range(16)]
        pool += [_entry("x1", "Mistletoe", "Justin Bieber"), _entry("x2", "Santa Tell Me", "Ariana Grande")]
        counts = {f"r{i}": 60 - i for i in range(16)}
        counts.update({"x1": 20, "x2": 10})
        songs, ranks = self._run(pool, counts)
        ids = {s["id"] for s in songs}
        self.assertIn("am:x1", ids)
        self.assertIn("am:x2", ids)
        self.assertEqual(ranks["am:x2"], (18, 10))

    def test_enforce_seasonal_cap_also_covers_streaming_backfill(self):
        songs = [
            _song("s1", "Mariah Carey", streaming_rank=1, title="All I Want for Christmas Is You"),
            _song("s2", "Wham!", streaming_rank=2, title="Last Christmas"),
            _song("d1", "aespa", dance_rank=1, video_count=30, streaming_rank=10),
        ]
        kept, removed = bf.enforce_seasonal_cap(songs, {"s1", "s2"})
        self.assertEqual([s["id"] for s in removed], ["s2"])
        self.assertEqual([s["id"] for s, _ in bf.app_rank_preview(kept)], ["d1", "s1"])


class AppRankPreviewTests(unittest.TestCase):
    """App DanceChartSnapshot.rankedEntries 的 Python 預演須與 docs/dance-chart-feed.md 排名規則一致。"""

    def test_streaming_score_uses_two_best_ranked_signals_like_app(self):
        # 複驗反例（x-vchart10）：App 先依名次排序再取前兩筆串流訊號，與寫入順序無關。
        def streaming(sid, title, artist, *ranks):
            sources = [bf.STREAMING_SOURCE, "Apple Music 韓國", "YouTube 台灣熱門"]
            return {"id": sid, "title": title, "artist": artist, "officialDance": [],
                    "signals": [{"kind": "streaming", "source": src, "rank": r} for src, r in zip(sources, ranks)]}
        santa = streaming("season-two", "Santa Tell Me", "Ariana Grande", 100, 50, 1)  # 1/√1 + 1/√50
        last = streaming("season-one", "Last Christmas", "Wham!", 1)  # 1/√1
        regular = streaming("regular", "Regular Song", "Regular Artist", 4)
        dance = [_song(f"d{i}", f"Crew {i}", dance_rank=i + 1, video_count=5) for i in range(13)]
        preview = bf.app_rank_preview(dance + [last, regular, santa])
        self.assertEqual([s["id"] for s, tier in preview if tier == "backfill"], ["season-two", "season-one"])
        kept, removed = bf.enforce_seasonal_cap(dance + [last, regular, santa], {"season-one", "season-two"})
        self.assertEqual([s["id"] for s in removed], ["season-one"])
        self.assertEqual([s["id"] for s, tier in bf.app_rank_preview(kept) if tier == "backfill"],
                         ["season-two", "regular"])

    def test_signal_dedup_by_source_keeps_best_rank_case_insensitive(self):
        song = {"id": "x", "title": "x", "artist": "X", "officialDance": [], "signals": [
            {"kind": "streaming", "source": "Apple Music 台灣", "rank": 40},
            {"kind": "streaming", "source": "apple music 台灣", "rank": 1},
            {"kind": "streaming", "source": "Apple Music 韓國", "rank": 4},
        ]}
        _, _, streaming_score = bf._app_song_scores(song)
        self.assertAlmostEqual(streaming_score, 1 + 0.5)

    def test_two_tiers_artist_cap_and_limit(self):
        songs = [_song(f"a{i}", "IVE", dance_rank=i + 1, video_count=20) for i in range(4)]
        songs += [_song(f"b{i}", f"Crew {i}", dance_rank=10 + i, video_count=10) for i in range(14)]
        songs += [_song("low", "Few", dance_rank=40, video_count=4, streaming_rank=1)]  # videoCount < 5：只能遞補
        ids = [s["id"] for s, _ in bf.app_rank_preview(songs)]
        self.assertEqual(len(ids), 15)
        self.assertEqual(ids[:3], ["a0", "a1", "a2"])
        self.assertNotIn("a3", ids)  # 同演出者最多 3 首
        self.assertNotIn("low", ids)  # 第一層已滿 15 首不遞補

    def test_backfill_when_dance_tier_short_and_previous_rank_gain_cap(self):
        songs = [
            _song("d1", "A", dance_rank=1, video_count=10),
            _song("d2", "B", dance_rank=4, video_count=8, previous_rank=100),  # 50 + 上限 30 = 80 < 100
            _song("s1", "C", streaming_rank=3),
            _song("s2", "D", streaming_rank=1),
        ]
        self.assertEqual([(s["id"], tier) for s, tier in bf.app_rank_preview(songs)],
                         [("d1", "dance"), ("d2", "dance"), ("s2", "backfill"), ("s1", "backfill")])
        songs[1]["signals"][0]["rank"] = 2  # 70.7 + 30 = 100.7 > 100：竄升加分可反超
        self.assertEqual(bf.app_rank_preview(songs)[0][0]["id"], "d2")

    def test_cross_platform_multiplier_breaks_close_scores_only(self):
        a = _song("a", "A", dance_rank=4, video_count=10)  # 50
        b = _song("b", "B", dance_rank=4, video_count=10, streaming_rank=50)  # 50×1.04
        self.assertEqual([s["id"] for s, _ in bf.app_rank_preview([a, b])], ["b", "a"])
        c = _song("c", "C", dance_rank=1, video_count=10)  # 100，不被 70.7×1.04 反超
        d = _song("d", "D", dance_rank=2, video_count=10, streaming_rank=1)
        self.assertEqual([s["id"] for s, _ in bf.app_rank_preview([d, c])], ["c", "d"])


class SameTitleDifferentArtistTests(unittest.TestCase):
    """〔D13〕同歌名不同歌手：不同歌手的同名歌不誤併，合作掛名順序不同的同一首歌不誤拆。"""

    def _merge(self, tw, kr=()):
        return bf.merge_candidates({"tw": list(tw), "kr": list(kr)}, lookup_fn=lambda ids, c: [])

    def test_same_title_different_artists_not_merged(self):
        # 真實案例（2026-11-26 台灣榜）：周興哲與 A-Lin 都有《摯友》
        merged = self._merge([
            _apple_item("1", "摯友", "周興哲", "11"),
            _apple_item("2", "摯友", "A-Lin", "22"),
        ])
        self.assertEqual(len(merged), 2)
        merged = self._merge([_apple_item("3", "Love", "Artist A", "33")],
                             [_apple_item("4", "Love", "Artist B", "44", "kr")])
        self.assertEqual(len(merged), 2)

    def test_collab_credit_order_swap_is_same_song(self):
        merged = self._merge(
            [_apple_item("10", "APT.", "ROSÉ & Bruno Mars", "100")],
            [_apple_item("20", "APT.", "Bruno Mars & ROSÉ", "200", "kr")],
        )
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["krRank"], 1)
        merged = self._merge(
            [_apple_item("30", "Takedown", "HUNTR/X", "300")],
            [_apple_item("40", "Takedown", "EJAE, HUNTR/X & AUDREY NUNA", "400", "kr")],
        )
        self.assertEqual(len(merged), 1)

    def test_collab_order_swap_merges_after_storefront_localization_drops_featured(self):
        # 複驗反例（x-vchart10）：韓榜原掛名 Bruno Mars & ROSÉ，台灣店面 Lookup 只回 Bruno Mars；
        # 原掛名留在 altArtists，仍須與台灣榜 ROSÉ & Bruno Mars 併成 1 筆。
        def lookup(ids, country):
            if "2" in ids:
                return [{"trackId": "2", "trackName": "APT.", "artistName": "Bruno Mars",
                         "trackViewUrl": "https://music.apple.com/tw/song/2"}]
            return []
        merged = bf.merge_candidates({
            "tw": [_apple_item("1", "APT.", "ROSÉ & Bruno Mars", "100")],
            "kr": [_apple_item("2", "APT.", "Bruno Mars & ROSÉ", "200", "kr")],
        }, lookup_fn=lookup)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["krRank"], 1)
        self.assertEqual(merged[0]["item"]["artistName"], "ROSÉ & Bruno Mars")
        self.assertIn("Bruno Mars & ROSÉ", merged[0]["altArtists"])
        # 韓榜先出現（台灣榜在後）時亦同
        merged = bf.merge_candidates({
            "kr": [_apple_item("2", "APT.", "Bruno Mars & ROSÉ", "200", "kr")],
            "tw": [_apple_item("9", "Filler", "Someone", "900"), _apple_item("1", "APT.", "ROSÉ & Bruno Mars", "100")],
        }, lookup_fn=lookup)
        self.assertEqual(sum(1 for e in merged if e["item"]["name"] == "APT."), 1)

    def test_localized_alt_artist_does_not_merge_different_artists_same_title(self):
        def lookup(ids, country):
            return [{"trackId": "4", "trackName": "Love", "artistName": "Artist B",
                     "trackViewUrl": "https://music.apple.com/tw/song/4"}] if "4" in ids else []
        merged = bf.merge_candidates({
            "tw": [_apple_item("3", "Love", "Artist A", "33")],
            "kr": [_apple_item("4", "Love", "Artist B & Artist C", "44", "kr")],
        }, lookup_fn=lookup)
        self.assertEqual(len(merged), 2)

    def test_featured_artist_credit_merges_only_for_distinct_titles(self):
        # 主唱 A 的合作曲＝合作者 C 掛主唱的同名版本（非常見曲名）→ 同一首
        merged = self._merge(
            [_apple_item("50", "Seesaw Season", "Crush feat. IU", "500")],
            [_apple_item("60", "Seesaw Season", "IU", "600", "kr")],
        )
        self.assertEqual(len(merged), 1)
        # 常見短曲名不套用合作掛名合併，避免誤併
        merged = self._merge(
            [_apple_item("70", "Love", "Crush feat. IU", "700")],
            [_apple_item("80", "Love", "IU", "800", "kr")],
        )
        self.assertEqual(len(merged), 2)

    def test_two_songs_sharing_only_a_featured_artist_not_merged(self):
        merged = self._merge(
            [_apple_item("90", "Midnight Parade", "Artist A feat. Guest C", "900")],
            [_apple_item("91", "Midnight Parade", "Artist B feat. Guest C", "910", "kr")],
        )
        self.assertEqual(len(merged), 2)

    def test_youtube_popular_lookup_merges_collab_order_swap_into_existing(self):
        pool = [_entry("100", "APT.", "ROSÉ & Bruno Mars", tw_rank=3)]
        # 影片標題只寫 Bruno Mars，不會直接對中既有候選；反查結果掛名順序相反，仍應併回同一首
        popular = [{"id": "yt", "snippet": {"title": "Bruno Mars - APT. (Official MV)", "channelTitle": "Bruno Mars"}}]
        found = [{"trackId": 555, "trackName": "APT.", "artistName": "Bruno Mars & ROSÉ", "artistId": 9}]
        enriched = bf.enrich_pool_with_youtube_popular(pool, popular, search_fn=lambda *a: found)
        self.assertEqual(len(enriched), 1)
        self.assertEqual(enriched[0]["ytPopularRank"], 1)

    def test_dance_videos_of_same_title_other_artist_not_counted(self):
        now = dt.datetime(2026, 11, 25, tzinfo=dt.timezone.utc)
        items = [
            _yt_item("a1", "周興哲 摯友 舞蹈 cover", channel="C1", channel_id="c1"),
            _yt_item("a2", "A-Lin 摯友 舞蹈 cover", channel="C2", channel_id="c2"),
        ]
        self.assertEqual(bf.evaluate_youtube_items("摯友", "A-Lin", items, now=now)["verified"], 1)


class ConcertFancamTests(unittest.TestCase):
    """〔D14〕演唱會 dance break 與舞台直拍計入同一首歌熱度。"""

    now = dt.datetime(2026, 11, 25, tzinfo=dt.timezone.utc)

    def count(self, title, artist, video_titles, alt_artists=()):
        items = [_yt_item(f"v{i}", t, channel=f"C{i}", channel_id=f"c{i}") for i, t in enumerate(video_titles)]
        return bf.evaluate_youtube_items(title, artist, items, now=self.now, alt_artists=list(alt_artists))["verified"]

    def test_concert_dance_break_and_fancams_count(self):
        titles = [
            "BESTie 'Love Options' Dance Break @ 1st Concert 261122",
            "[4K] 261120 BESTie 'Love Options' 직캠 Fancam | 2026 MAMA",
            "[페이스캠4K] 베스티 다혜 '연애의 조건' (BESTie Da Hye 'Love Options' FaceCam) @SBS Inkigayo",
            "BESTie Love Options FULL FOCUS concert",
            "베스티 Love Options 세로캠",
            "BESTie Love Options 直拍 台北演唱會",
        ]
        self.assertEqual(self.count("Love Options", "BESTie", titles, alt_artists=["베스티"]), 6)

    def test_medley_fancam_counts_for_each_quoted_song(self):
        v = ["【2026 MMA】Hearts2Hearts (하츠투하츠) 'Intro + 15-LOVE + RUDE!' 4K Fancam 직캠"]
        self.assertEqual(self.count("RUDE!", "Hearts2Hearts", v), 1)

    def test_fancam_of_other_quoted_song_not_counted(self):
        v = ["[4k]261114 MELON MUSIC AWARDS| Hearts2Hearts CARMEN '15-LOVE' 직캠(H2H CARMEN Focus)"]
        self.assertEqual(self.count("FOCUS", "Hearts2Hearts", v), 0)
        v_ok = ["[4K] 261122 FOCUS 하츠투하츠 지우 직캠 Hearts2Hearts jiwoo fancam"]
        self.assertEqual(self.count("FOCUS", "Hearts2Hearts", v_ok), 1)

    def test_live_singing_and_reaction_fancams_not_counted(self):
        self.assertEqual(self.count("stupid song", "Olivia Rodrigo", ["Olivia Rodrigo - stupid song (live)"]), 0)
        self.assertEqual(self.count("SWIM", "BTS", ["BTS Swim Live Concert Fancam REACTION"]), 0)
        self.assertEqual(self.count("It's Me", "ILLIT", ["[2026 MMA ILLIT 'It's Me' 무대] 아이돌 리액션 직캠"]), 0)
        self.assertEqual(self.count("Good Goodbye", "HWASA", ["HWASA Good Goodbye Live Vocal fancam"]), 0)
        self.assertEqual(self.count("Angel Bite", "TOMORROW X TOGETHER", ["TXT Angel Bite Shuffle Live"]), 0)


if __name__ == "__main__":
    unittest.main()
