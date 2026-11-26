"""每日產生 DancingDiary 熱門舞曲 feed（feed.json）。

最終排名（舞蹈優先、多平台溫和交叉加權、同演出者最多 3 首、前 15 名）在 App 端；排程端只決定舞蹈訊號名次，
並依 App 同規則預演榜單以落實季節歌上限（dance-chart-daily#10，2026-11-26 D13＋D14）：
- 舞蹈／Shorts 名次以「實測影片數 × 加權」排序：韓文／日文舞曲 ×1.20（上限、不疊加），季節歌 ×0.80；videoCount 仍是實測數。
- 預演 App 前 15 名最多 1 首季節歌，保留預演名次最高者，其餘落在前 15 名內的季節歌當日不輸出。
- 跨榜同曲：同歌名只在同 artistId／同演出者時合併；合作掛名集合相同或「主唱＝對方合作者」（非常見曲名）也視為同曲。
- 演唱會 dance break、舞台直拍（fancam／직캠／focus cam／直拍）計入同一首歌舞蹈熱度；只靠直拍字樣判定時，
  標題若有引號曲名，本曲名須在引號內。
- 串流與熱門候選：
  1. Apple Music 台灣最多人播放前 100、韓國前 50（公開 RSS，免金鑰）。
     透過 Apple 歌曲 ID、跨店面 artistId＋正規化曲名與 iTunes Lookup 台灣店面名稱合併同曲，
     統一使用台灣店面顯示名稱與連結，同時保留「Apple Music 台灣」與「Apple Music 韓國」訊號。
  2. YouTube Data API videos.list(chart=mostPopular, videoCategoryId=10, regionCode=TW)（每次 1 單位），
     將台灣當下熱門音樂影片匹配既有候選或經免金鑰 iTunes Search 反查擴大候選池，
     輸出「YouTube 台灣熱門」訊號。
- 舞蹈與 Shorts 熱度：
  1. YouTube Data API search.list（part=snippet, maxResults=50），近 14 天、地區 TW。
     以乾淨單分支查詢（歌名＋演出者＋dance）搭配每演出者第一輪查詢上限分散配額，
     逐筆驗證「曲名＋演出者＋舞蹈意圖」與單頻道上限，輸出「YouTube 舞蹈影片」訊號。
  2. 對通過舞蹈驗證的影片批次呼叫 videos.list(part=contentDetails)（每 50 支 1 單位），
     篩出 ≤ 60 秒且達門檻的影片輸出「YouTube Shorts」舞蹈訊號。
  完整排程 90 首約 9,025 單位（90×100 + 1 + ~24），維持在每日 10,000 單位內。
- 快取與離線調參：設定 RAW_CACHE_OUT 可輸出每首歌原始搜尋結果、熱門影片與影片時長，
  不含 API 金鑰；設定 OFFLINE_RAW_INPUT 或傳入 --replay <檔路徑> 可離線重算驗證結果與門檻。
格式與 App 的 DanceChartSnapshot 相同（見 DancingDiary docs/dance-chart-feed.md）。
"""
from __future__ import annotations

import datetime as dt
import html
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

FEEDS = [("tw", 100), ("kr", 50)]
STREAMING_SOURCE = "Apple Music 台灣"
KR_STREAMING_SOURCE = "Apple Music 韓國"
YOUTUBE_SOURCE = "YouTube 舞蹈影片"
YOUTUBE_POPULAR_SOURCE = "YouTube 台灣熱門"
YOUTUBE_SHORTS_SOURCE = "YouTube Shorts"
MIN_DANCE_VIDEOS = 5
MIN_SHORTS_VIDEOS = 5
SHORTS_MAX_DURATION_SECONDS = 60
MAX_PER_CHANNEL = 3
DEFAULT_MAX_QUERIES = 90
FIRST_PASS_PER_ARTIST = 3
QUERY_WINDOW_DAYS = 14
QUERY_REGION_CODE = "TW"
QUERY_MAX_RESULTS = 50
MAX_POPULAR_RESULTS = 50
MAX_POPULAR_LOOKUPS = 25
YOUTUBE_MUSIC_CATEGORY_ID = "10"

# 〔2026-11-26 使用者決定 D13〕舞蹈名次加權（只影響排程計算的 YouTube 舞蹈／Shorts 名次；videoCount 仍是實測影片數）。
# 1. 韓文（K-pop）／日文（J-pop）舞曲：排序用影片數 ×1.20，上限 1.20（同時判定韓、日也不疊加）。
# 2. 季節歌（聖誕、新年、冬季節慶經典）：排序用影片數 ×0.80 溫和降權，且依 App 排名規則預演後，
#    完整榜單前 15 名最多保留 1 首季節歌；其餘季節歌當日不輸出（不封殺：降權後仍能憑熱度拿下那 1 席）。
KJ_DANCE_MULTIPLIER = 1.20
SEASONAL_DANCE_MULTIPLIER = 0.80
MAX_SEASONAL_IN_TOP = 1
# 與 App DanceChartSnapshot.rankedEntries 相同的榜單規則（預演季節歌上限與離線對照用）
APP_MAX_RANKED_SONGS = 15
APP_MAX_SONGS_PER_ARTIST = 3
APP_MIN_DANCE_VIDEOS = 5
APP_MEASURED_DANCE_SOURCES = (YOUTUBE_SOURCE, YOUTUBE_SHORTS_SOURCE)
APP_CROSS_PLATFORM_BONUS_PER_SOURCE = 0.04
APP_MAX_CROSS_PLATFORM_BONUS = 0.16

OUT = os.environ.get("FEED_OUT", "feed.json")
RAW_CACHE_OUT = os.environ.get("RAW_CACHE_OUT", "").strip()
OFFLINE_RAW_INPUT = os.environ.get("OFFLINE_RAW_INPUT", "").strip()

# 跨店面／跨語言演出者別名表（iTunes Lookup 失敗或無 artistId 時的備援，並用於 YouTube 搜尋與標題比對）。
# 每組的第一個值為台灣常用／國際識別名稱；YouTube 搜尋會優先使用 ARTIST_SEARCH_ALIASES。
ARTIST_ALIAS_GROUPS = [
    ("防彈少年團", "BTS", "방탄소년단", "BANGTANTV"),
    ("IVE", "아이브"),
    ("ILLIT", "아일릿"),
    ("RESCENE", "리센느"),
    ("ATEEZ", "에이티즈"),
    ("TOMORROW X TOGETHER", "TXT", "투모로우바이투게더"),
    ("CORTIS", "코르티스"),
    ("BESTie", "베스티"),
    ("KiiiKiii", "키키"),
    ("NewJeans", "뉴진스"),
    ("Hearts2Hearts", "H2H", "하츠투하츠"),
    ("aespa", "에스파"),
    ("LE SSERAFIM", "LESSERAFIM", "르세라핌"),
    ("BABYMONSTER", "BAEMON", "베이비몬스터"),
    ("BLACKPINK", "블랙핑크"),
    ("TWICE", "트와이스"),
    ("SEVENTEEN", "SVT", "세븐틴"),
    ("Stray Kids", "SKZ", "스트레이 키즈"),
    ("ENHYPEN", "엔하이픈"),
    ("ZEROBASEONE", "ZB1", "제로베이스원"),
    ("RIIZE", "라이즈"),
    ("BOYNEXTDOOR", "BND", "보이넥스트도어"),
    ("TWS", "투어스"),
    ("KISS OF LIFE", "KIOF", "키스오브라이프"),
    ("ITZY", "있지"),
    ("NMIXX", "엔믹스"),
    ("(G)I-DLE", "GIDLE", "i-dle", "여자아이들", "아이들"),
    ("EXO", "엑소"),
    ("NCT DREAM", "엔시티 드림"),
    ("NCT 127", "엔시티 127"),
    ("NCT WISH", "엔시티 위시"),
    ("BIGBANG", "빅뱅"),
    ("G-DRAGON", "지드래곤"),
    ("SHINee", "샤이니"),
    ("IU", "아이유"),
    ("HWASA", "화사"),
    ("JENNIE", "제니"),
    ("ROSÉ", "ROSE", "로제"),
    ("LISA", "리사"),
    ("JISOO", "지수"),
    ("SOYEON", "소연", "전소연"),
    ("GOT7", "갓세븐"),
    ("A2O", "A2O LITE", "A2O MAY"),
    ("ALLDAY PROJECT", "올데이프로젝트"),
    ("HUNTR/X", "HUNTRX"),
    ("MEOVV", "미야오"),
    ("izna", "이즈나"),
    ("The Black Skirts", "검정치마"),
    ("back number", "백 넘버"),
]

# YouTube 搜尋時把中文譯名轉為國際常用團名，提高舞蹈影片召回與精準度（顯示仍保留台灣店面名稱）。
ARTIST_SEARCH_ALIASES = {
    "防彈少年團": "BTS",
}

COLLAB_RE = re.compile(
    r"(?:[\(\[\（]\s*(?:feat\.|ft\.|(?:feat|ft|featuring|with)\b)"
    r"|\s+(?:feat\.|ft\.|(?:feat|ft|featuring|with)(?=\s))"
    r"|\s+&\s+|\s*＆\s*|\s*[,，、×]\s*|\s+/\s+|\s*／\s*)",
    re.IGNORECASE,
)

TITLE_SUFFIX_RE = re.compile(
    r"\s*(?:"
    r"[\(\[\（]\s*(?:feat\.|ft\.|(?:feat|ft|featuring|with)\b)[^)\]\）]*[\)\]\）]"
    r"|[\(\[\（]\s*(?:single|radio|album|korean|japanese|english|chinese)\s*(?:version|ver\.?|edit)\s*[\)\]\）]"
    r"|[\(\[\（][^)\]\）]*(?:影集|電影|電視劇|劇集|主題曲|片尾曲|片頭曲|插曲|original\s+soundtrack|\bost\b)[^)\]\）]*[\)\]\）]"
    r"|-\s*single\s*$"
    r")",
    re.IGNORECASE,
)

BILINGUAL_PAREN_RE = re.compile(r"^(.+?)\s*[\(\（]\s*([^)\）]+?)\s*[\)\）]\s*$")
GENERIC_PAREN_TAG_RE = re.compile(
    r"^(?:feat|ft|featuring|with|remix|inst|instrumental|ver|version|live|acoustic|sped up|slowed|single|radio|edit)\b",
    re.IGNORECASE,
)

# 常見英文短字／詞組：對這類曲名採取更嚴格的標題與演出者比對（不得只靠 description 比對演出者）。
COMMON_TITLES = {
    "idol", "please", "with", "bad", "normal", "it's me", "its me", "body to body",
    "love", "home", "stay", "run", "fire", "time", "life", "dream", "light", "night",
    "baby", "sorry", "hello", "forever", "everything", "heroine", "swim", "neon",
    "crash", "aliens", "butter", "2.0", "fya", "rude", "like animals", "404",
}

STRONG_DANCE_RE = re.compile(
    r"(?:(?<![a-z0-9])(?:(?:kpop|idol|shorts?|yt)?dance(?:cover|practice|challenge|tutorial|video|reel|reels|shorts|break|performance|ver|version)?|dancing|dancer|dancers|(?:kpop|dance)?choreo(?:graphy|video)?|footwork)(?![a-z0-9])"
    r"|dance\s*(?:cover|practice|challenge|tutorial|mirror|ver|version|break|performance|video)"
    r"|mirrored?\s*(?:dance|ver|version)"
    r"|舞蹈|練舞|練習室|翻跳|鏡面|編舞|排舞|跳舞|學舞|宅舞|街舞|舞室"
    r"|안무|댄스|커버댄스|춤"
    r"|ダンス|踊ってみた|振り付け|振付)",
    re.IGNORECASE,
)

# 〔D14〕演唱會／音樂節目／頒獎典禮的舞台直拍、dance break 與 focus cam 都屬同一首歌的舞蹈版本，計入熱度。
# 只收明確的直拍用語；單獨的 live／concert／stage 不算（多為演唱片段）。
FANCAM_RE = re.compile(
    r"(?:(?<![a-z0-9])(?:fan\s*cam|fancams|face\s*cam|focus\s*cam|full\s*focus|stage\s*cam|vertical\s*cam|"
    r"dance\s*battle|performance\s*practice|performance\s*video)(?![a-z0-9])"
    r"|직캠|세로캠|입덕직캠|안무\s*영상|直拍|飯拍|饭拍|ファンカム)",
    re.IGNORECASE,
)

# 影片標題中以引號標示的曲名片段（' ' 只在前後非英數時視為引號，避免 It's 的撇號）
QUOTED_SEGMENT_RE = re.compile(
    r"(?<![a-z0-9])'([^']{1,80}?)'(?![a-z0-9])|\"([^\"]{1,80})\"|「([^」]{1,80})」|『([^』]{1,80})』|《([^》]{1,80})》|“([^”]{1,80})”"
)

CHALLENGE_RE = re.compile(r"(?:(?<![a-z0-9])challenge(?![a-z0-9])|challenge\b|챌린지|挑戰)", re.IGNORECASE)
NON_DANCE_RE = re.compile(
    r"(?:(?<![a-z0-9])(?:vocal|singing|lyrics|lyric|karaoke|piano|guitar|drum|drums|bass|violin|cello|flute|reaction|react|unboxing|vlog|quiz|guess|audio|instrumental)(?![a-z0-9])"
    r"|歌詞|彈唱|吉他|鋼琴|翻唱|反應|開箱|試吃|伴奏|純音樂|리액션|リアクション)",
    re.IGNORECASE,
)


def _build_alias_lookups():
    canonical = {}
    group_map = {}
    for group in ARTIST_ALIAS_GROUPS:
        canon = group[0]
        normed = [norm(name) for name in group]
        for k in normed:
            canonical[k] = canon
            group_map[k] = list(group)
    return canonical, group_map


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "")
    s = s.replace("’", "'").replace("‘", "'").replace("`", "'").replace("´", "'")
    return " ".join(s.lower().split())


ARTIST_CANONICAL_TW, ARTIST_GROUP_BY_NORM = _build_alias_lookups()


def primary_artist(artist: str) -> str:
    trimmed = unicodedata.normalize("NFC", artist or "").strip()
    if not trimmed:
        return ""
    m = COLLAB_RE.search(trimmed)
    if m:
        candidate = trimmed[:m.start()].strip()
        return candidate or trimmed
    return trimmed


def canonical_artist_key(artist: str) -> str:
    base = norm(primary_artist(artist))
    if not base:
        return ""
    canon = ARTIST_CANONICAL_TW.get(base, base)
    return norm(canon)


def clean_title(title: str) -> str:
    text = unicodedata.normalize("NFKC", title or "").strip()
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    cleaned = TITLE_SUFFIX_RE.sub("", text).strip()
    return cleaned or text


def artist_member_keys(artist: str) -> list[str]:
    """拆出合作曲所有掛名演出者的正規化鍵（主要演出者在第一位），供「同歌名、合作掛名順序不同」辨識同曲。"""
    trimmed = unicodedata.normalize("NFC", artist or "").strip()
    if not trimmed:
        return []
    out: list[str] = []
    for part in COLLAB_RE.split(trimmed):
        name = (part or "").strip(" ()[]（）")
        name = re.sub(r"^(?:feat\.?|ft\.?|featuring|with)\s+", "", name, flags=re.IGNORECASE).strip()
        if not name:
            continue
        key = norm(ARTIST_CANONICAL_TW.get(norm(name), name))
        if key and key not in out:
            out.append(key)
    primary = canonical_artist_key(artist)
    if primary in out:
        out.remove(primary)
    return ([primary] if primary else []) + out


def _has_non_ascii_letter(s: str) -> bool:
    return any(ord(ch) > 127 and ch.isalpha() for ch in s)


def _has_ascii_letter(s: str) -> bool:
    return any("a" <= ch.lower() <= "z" for ch in s)


def title_variants(*titles: str, include_subtitle_head: bool = False) -> list[str]:
    """回傳用於跨榜去重與標題比對的正規化曲名變體（含雙語括號拆解；標題比對可選拆出括號前主名）。"""
    out = []
    seen = set()

    def add(val: str):
        v = norm(val).strip(" !?.,~-")
        if v and v not in seen:
            seen.add(v)
            out.append(v)

    for raw in titles:
        if not raw:
            continue
        cleaned = clean_title(raw)
        add(cleaned)
        add(raw)
        m = BILINGUAL_PAREN_RE.match(cleaned)
        if m:
            outside, inside = m.group(1).strip(), m.group(2).strip()
            if not GENERIC_PAREN_TAG_RE.match(inside):
                # 僅在括號內外屬不同文字系統（如 韓文 (English)）時做跨榜去重拆解，避免一般副標誤併
                if (_has_non_ascii_letter(outside) and _has_ascii_letter(inside)) or (
                    _has_ascii_letter(outside) and _has_non_ascii_letter(inside)
                ):
                    if len(re.sub(r"\W+", "", outside)) >= 2:
                        add(outside)
                    if len(re.sub(r"\W+", "", inside)) >= 3:
                        add(inside)
                elif include_subtitle_head:
                    # 標題驗證時接受副標前的主曲名（如 404 (New Era) -> 404）；短詞會自動套用嚴格演出者檢查
                    if len(re.sub(r"\W+", "", outside)) >= 3:
                        add(outside)
    return out


def get_json(url: str, headers=None, attempts: int = 1):
    """attempts > 1 時，逾時、連線錯誤或 5xx 會等待後重試（GitHub runner 連 Apple RSS 偶爾逾時）。"""
    req = urllib.request.Request(
        url,
        headers=headers or {"User-Agent": "chart-feed-bot", "Accept": "application/json"},
    )
    for i in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                return json.load(r)
        except (OSError, ValueError) as exc:  # URLError、TimeoutError 皆為 OSError
            if i == attempts - 1 or (isinstance(exc, urllib.error.HTTPError) and exc.code < 500):
                raise
            print(f"重試 {i + 1}/{attempts - 1}：{url.split('?')[0]}：{exc}", file=sys.stderr)
            time.sleep(10 * (i + 1))


def apple(storefront: str, limit: int) -> list[dict]:
    url = f"https://rss.marketingtools.apple.com/api/v2/{storefront}/music/most-played/{limit}/songs.json"
    return get_json(url, attempts=4)["feed"]["results"]


def itunes_lookup(ids: list[str], country: str = "tw") -> list[dict]:
    """免金鑰批次查詢 iTunes Lookup API，用於補齊台灣店面的曲名與演出者名。失敗時靜默退回。"""
    unique_ids = [str(i) for i in dict.fromkeys(ids) if i]
    results = []
    for start in range(0, len(unique_ids), 100):
        batch = unique_ids[start : start + 100]
        url = "https://itunes.apple.com/lookup?" + urllib.parse.urlencode(
            {"country": country, "id": ",".join(batch)}
        )
        try:
            data = get_json(url, attempts=2)
            results.extend(data.get("results", []))
        except Exception as exc:
            print(f"iTunes Lookup 失敗（退回別名表）：{exc}", file=sys.stderr)
    return results


def _collect_tw_localization(tw_items: list[dict], kr_items: list[dict], lookup_fn=itunes_lookup):
    """建立台灣店面曲目與演出者名稱對照，讓跨榜與韓榜獨有歌曲統一顯示並正確計入同演出者上限。"""
    tw_ids = {str(it["id"]) for it in tw_items}
    tw_artist_by_id: dict[str, str] = {}
    for it in tw_items:
        aid = str(it.get("artistId") or "")
        name = it.get("artistName") or ""
        if aid and name:
            # 同一 artistId 若同時有單人與合作標示，取較短的單人／主團名稱
            if aid not in tw_artist_by_id or len(name) < len(tw_artist_by_id[aid]):
                tw_artist_by_id[aid] = name

    kr_only_tids = [str(it["id"]) for it in kr_items if str(it["id"]) not in tw_ids]
    tw_track_lookup: dict[str, dict] = {}
    if kr_only_tids:
        for rec in lookup_fn(kr_only_tids, "tw"):
            if "trackId" in rec:
                tw_track_lookup[str(rec["trackId"])] = rec

    unresolved_aids = [
        str(it["artistId"])
        for it in kr_items
        if it.get("artistId")
        and str(it["artistId"]) not in tw_artist_by_id
        and str(it["id"]) not in tw_track_lookup
    ]
    tw_artist_lookup: dict[str, str] = {}
    if unresolved_aids:
        for rec in lookup_fn(unresolved_aids, "tw"):
            if "artistId" in rec and rec.get("artistName"):
                tw_artist_lookup[str(rec["artistId"])] = rec["artistName"]

    return tw_artist_by_id, tw_track_lookup, tw_artist_lookup


def _localize_item(sf: str, it: dict, tw_artist_by_id: dict, tw_track_lookup: dict, tw_artist_lookup: dict):
    """回傳標準化後的顯示 item，以及保留的額外曲名／演出者別名（供 YouTube 標題驗證）。"""
    tid = str(it["id"])
    aid = str(it.get("artistId") or "")
    alt_titles = [it["name"]]
    alt_artists = [it["artistName"]]

    if sf == "tw":
        return dict(it), alt_titles, alt_artists

    localized = dict(it)
    meta = tw_track_lookup.get(tid)
    if meta:
        if meta.get("trackName"):
            localized["name"] = meta["trackName"]
            alt_titles.append(meta["trackName"])
        if meta.get("artistName"):
            localized["artistName"] = meta["artistName"]
            alt_artists.append(meta["artistName"])
        if meta.get("trackViewUrl") and str(meta["trackViewUrl"]).startswith("https://"):
            localized["url"] = meta["trackViewUrl"]
    else:
        mapped_artist = (
            tw_artist_by_id.get(aid)
            or tw_artist_lookup.get(aid)
            or ARTIST_CANONICAL_TW.get(norm(it["artistName"]))
        )
        if mapped_artist:
            localized["artistName"] = mapped_artist
            alt_artists.append(mapped_artist)

    return localized, alt_titles, alt_artists


def _dedup_keys(
    item: dict, alt_titles: list[str], alt_artists=(), *, for_lookup: bool = False
) -> list[tuple]:
    """跨榜同曲辨識鍵。同歌名只在「同 Apple artistId」或「同（正規化）演出者」時合併，不同歌手的同名歌不併。

    〔D13〕合作曲掛名順序或主唱不同時：掛名集合完全相同（`ROSÉ & Bruno Mars`／`Bruno Mars & ROSÉ`）一律視為同曲；
    「一方的主要演出者＝另一方的合作掛名者」（`A feat. B`／`B`）也視為同曲，但常見／短曲名（Love、With、BAD…）不套用，避免誤併。
    for_lookup=False 回傳要登記的鍵；True 回傳要查詢的鍵（主要演出者對照對方的合作掛名，反之亦然）。
    店面在地化後的掛名可能只剩主唱（如韓榜 `Bruno Mars & ROSÉ` 經台灣店面查詢只回 `Bruno Mars`），
    因此原掛名（alt_artists）也一併產生鍵，避免同曲被拆成兩筆。
    """
    tid = str(item["id"])
    aid = str(item.get("artistId") or "")
    member_sets: list[list[str]] = []
    for name in dict.fromkeys([item["artistName"], *(a for a in alt_artists if a)]):
        members = artist_member_keys(name)
        if members and members not in member_sets:
            member_sets.append(members)
    keys: list[tuple] = [("id", tid)]
    for tv in title_variants(item["name"], *alt_titles):
        if aid:
            keys.append(("aid_title", aid, tv))
        for members in member_sets:
            artist_key = members[0]
            featured = members[1:]
            keys.append(("artist_title", artist_key, tv))
            if len(members) >= 2:
                # 掛名演出者集合完全相同（只是順序不同）時，短曲名也視為同曲（如 APT.）
                keys.append(("members_title", tuple(sorted(members)), tv))
            if _is_short_or_common_title(tv):
                continue
            if for_lookup:
                keys.append(("member_title", artist_key, tv))
                keys.extend(("artist_title", m, tv) for m in featured)
            else:
                keys.extend(("member_title", m, tv) for m in featured)
    return list(dict.fromkeys(keys))


def _genre_names(item: dict) -> list[str]:
    """Apple RSS 的 genres（台灣店面為中文，如「韓國流行樂」），用於韓日舞曲辨識；離線快取一併保存。"""
    out = []
    for g in item.get("genres") or []:
        name = (g.get("name") if isinstance(g, dict) else g) or ""
        name = str(name).strip()
        if name and name not in out:
            out.append(name)
    return out


def merge_candidates(lists: dict[str, list[dict]], lookup_fn=itunes_lookup) -> list[dict]:
    """依各榜名次交錯合併並跨店面去重：
    1. 同一 Apple track id 必合併（涵蓋中韓雙語曲名／演出者名）。
    2. 不同 track id 但同一 artistId（或正規化演出者別名）＋同一正規化曲名時合併（如 IVE／아이브 Looks Can Kill）。
    3. 任一重複項出現在台灣榜時，保留較高的跨榜順位，並一律採用台灣店面的 id、曲名、演出者、連結與封面。
    """
    tw_items = lists.get("tw", [])
    kr_items = lists.get("kr", [])
    tw_artist_by_id, tw_track_lookup, tw_artist_lookup = _collect_tw_localization(
        tw_items, kr_items, lookup_fn=lookup_fn
    )

    seen: dict[tuple, dict] = {}
    merged: list[dict] = []
    longest = max((len(v) for v in lists.values()), default=0)

    for i in range(longest):
        for sf, _ in FEEDS:
            items = lists.get(sf, [])
            if i >= len(items):
                continue
            raw = items[i]
            loc_item, alt_titles, alt_artists = _localize_item(
                sf, raw, tw_artist_by_id, tw_track_lookup, tw_artist_lookup
            )
            keys = _dedup_keys(loc_item, alt_titles, alt_artists)
            lookup = _dedup_keys(loc_item, alt_titles, alt_artists, for_lookup=True)
            existing = next((seen[k] for k in lookup if k in seen), None)

            if existing is not None:
                for t in alt_titles:
                    if t and t not in existing["altTitles"]:
                        existing["altTitles"].append(t)
                for a in alt_artists:
                    if a and a not in existing["altArtists"]:
                        existing["altArtists"].append(a)
                for g in _genre_names(raw):
                    if g not in existing.setdefault("genres", []):
                        existing["genres"].append(g)
                if sf == "tw":
                    if existing["twRank"] is None or (i + 1) < existing["twRank"]:
                        existing["twRank"] = i + 1
                    # 台灣榜資料優先作為顯示與連結來源
                    if existing["storefront"] != "tw":
                        existing["item"] = loc_item
                        existing["storefront"] = "tw"
                elif sf == "kr":
                    if existing.get("krRank") is None or (i + 1) < existing["krRank"]:
                        existing["krRank"] = i + 1
                for k in keys:
                    seen[k] = existing
                continue

            entry = {
                "item": loc_item,
                "storefront": sf,
                "twRank": i + 1 if sf == "tw" else None,
                "krRank": i + 1 if sf == "kr" else None,
                "ytPopularRank": None,
                "altTitles": list(dict.fromkeys(t for t in alt_titles if t)),
                "altArtists": list(dict.fromkeys(a for a in alt_artists if a)),
                "genres": _genre_names(raw),
            }
            for k in keys:
                seen[k] = entry
            merged.append(entry)

    return merged


def candidates() -> list[dict]:
    lists = {sf: apple(sf, n) for sf, n in FEEDS}
    return merge_candidates(lists)


def search_artist_name(artist: str) -> str:
    base = primary_artist(artist)
    canon = ARTIST_CANONICAL_TW.get(norm(base), base)
    return ARTIST_SEARCH_ALIASES.get(canon, canon)


def query_title(title: str) -> str:
    """清理供 YouTube 搜尋使用的曲名：移除 feat／版本／OST 括號與孤立尾端標點，保留核心曲名。"""
    cleaned = clean_title(title)
    m = BILINGUAL_PAREN_RE.match(cleaned)
    if m:
        outside = m.group(1).strip()
        if len(re.sub(r"\W+", "", outside)) >= 3:
            cleaned = outside
    cleaned = cleaned.replace('"', "").replace("|", " ").strip()
    stripped = cleaned.strip("!?！？")
    cleaned = stripped or cleaned
    cleaned = " ".join(cleaned.split())
    if not cleaned:
        cleaned = " ".join((title or "").replace('"', "").replace("|", " ").split())
    return cleaned


def build_youtube_query(title: str, artist: str, alt_titles=(), alt_artists=()) -> str:
    """組合乾淨單分支 YouTube 查詢：避免多分支 OR 與雙引號鎖死稀釋召回，由 evaluate_youtube_items() 嚴格把關精準度。"""
    q_title = query_title(title)
    q_artist = " ".join(search_artist_name(artist).replace('"', "").replace("|", " ").split())
    if not q_artist:
        q_artist = " ".join((artist or "").replace('"', "").replace("|", " ").split())
    parts = [p for p in (q_title, q_artist, "dance") if p]
    return " ".join(parts)


def select_query_entries(
    pool: list[dict],
    max_queries: int,
    first_pass_per_artist: int = FIRST_PASS_PER_ARTIST,
) -> list[dict]:
    """分散單演出者查詢配額：第一輪每演出者先取前 N 首，避免單團屠榜擠掉後段其他舞曲，剩餘配額再依序遞補。
    YouTube 台灣熱門新發掘（不在 Apple 榜）的候選曲優先排在第一輪中段，確保有配額查舞蹈量。
    """
    if max_queries <= 0:
        return []
    primary: list[dict] = []
    overflow: list[dict] = []
    counts: dict[str, int] = {}
    for entry in pool:
        akey = canonical_artist_key(entry["item"].get("artistName", ""))
        if not akey or first_pass_per_artist <= 0 or counts.get(akey, 0) < first_pass_per_artist:
            primary.append(entry)
            if akey:
                counts[akey] = counts.get(akey, 0) + 1
        else:
            overflow.append(entry)
    return (primary + overflow)[:max_queries]


ISO_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)

POPULAR_NOISE_BRACKET_RE = re.compile(
    r"\s*[\(\[\（【][^)\]\）】]*(?:"
    r"\bm/?v\b|official|music\s*video|lyric|lyrics|audio|visualizer|performance|"
    r"live|stage|teaser|trailer|shorts?|ver\.?|version|4k|8k|hd|uhd|full|eng\s*sub|cc|"
    r"官方|完整版|歌詞版|現場|音樂錄影帶|主題曲|片尾曲|片頭曲|插曲|電影|影集|電視劇"
    r")[^)\]\）】]*[\)\]\）】]",
    re.IGNORECASE,
)

POPULAR_QUOTED_TITLE_RE = re.compile(
    r"^(?P<artist>.+?)\s*(?:"
    r"'(?P<sq>(?:[^']|'(?=[A-Za-z])){2,80})'(?![A-Za-z])"
    r"|\"(?P<dq>[^\"]{2,80})\""
    r"|「(?P<cj1>[^」]{1,80})」"
    r"|『(?P<cj2>[^』]{1,80})』"
    r"|《(?P<cj3>[^》]{1,80})》"
    r"|“(?P<cj4>[^”]{2,80})”"
    r")"
)

AGGREGATOR_CHANNELS = {
    "hybe labels", "smtown", "jyp entertainment", "starship", "starshiptv",
    "1thek", "1thek originals", "stone music entertainment", "yg entertainment",
    "cube entertainment", "theblacklabel", "wakeone", "belift lab", "source music",
    "ador", "kq entertainment", "rbw", "genie music", "bugs", "melon",
    "warner music taiwan", "sony music taiwan", "universal music taiwan",
    "avex taiwan", "rock records", "b'in music", "相信音樂binmusic", "滾石唱片 rock records",
    "滾石唱片", "華納音樂", "索尼音樂", "環球音樂", "福茂唱片", "杰威爾音樂 jvr music",
}


def parse_iso8601_duration(duration: str) -> int | None:
    """解析 YouTube videos.list contentDetails.duration（ISO 8601）為秒數。"""
    if not duration or not isinstance(duration, str):
        return None
    m = ISO_DURATION_RE.match(duration.strip())
    if not m or not any(m.group(k) is not None for k in ("days", "hours", "minutes", "seconds")):
        return None
    days = int(m.group("days") or 0)
    hours = int(m.group("hours") or 0)
    minutes = int(m.group("minutes") or 0)
    seconds = int(m.group("seconds") or 0)
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def parse_popular_video_title(v_title: str, v_channel: str = "") -> tuple[str, str] | None:
    """從 YouTube 台灣熱門音樂影片標題／頻道解析出 (artist_hint, title_hint) 供 iTunes 反查。"""
    raw = html.unescape(v_title or "").strip()
    if not raw:
        return None
    cleaned = POPULAR_NOISE_BRACKET_RE.sub(" ", raw)
    # 去掉 | 或 ｜ 後面的節目／頻道副標（如 "Song - Artist | Official MV"）
    if "|" in cleaned or "｜" in cleaned:
        left = re.split(r"\s*[|｜]\s*", cleaned, maxsplit=1)[0].strip()
        if len(re.sub(r"\W+", "", left)) >= 2:
            cleaned = left
    cleaned = re.sub(r"\b(?:official\s*(?:music\s*video|mv|m/v|video|audio)|m/v|mv)\b", " ", cleaned, flags=re.IGNORECASE)
    cleaned = " ".join(cleaned.split()).strip(" -–—~·•")
    if not cleaned:
        return None

    # 1. Artist - Title / Artist – Title / Artist — Title
    dash_parts = re.split(r"\s+[-–—]\s+", cleaned, maxsplit=1)
    if len(dash_parts) == 2:
        left, right = dash_parts[0].strip(" '\"「」『』《》"), dash_parts[1].strip(" '\"「」『』《》")
        if left and right:
            return left, right

    # 2. Artist 'Title' / Artist 「Title」 / Artist 《Title》
    m = POPULAR_QUOTED_TITLE_RE.match(cleaned)
    if m:
        artist_part = m.group("artist").strip(" -–—:")
        title_part = next(
            (m.group(k).strip() for k in ("sq", "dq", "cj1", "cj2", "cj3", "cj4") if m.group(k)),
            "",
        )
        if artist_part and title_part:
            return artist_part, title_part

    # 3. 退回：以非聚合型頻道名作為演出者，清理後標題作為曲名
    ch = html.unescape(v_channel or "").strip()
    ch = re.sub(r"\s*-\s*Topic$", "", ch, flags=re.IGNORECASE).strip()
    ch = re.sub(r"\s*Official(?:\s+Channel)?$", "", ch, flags=re.IGNORECASE).strip()
    if ch and norm(ch) not in AGGREGATOR_CHANNELS:
        return ch, cleaned

    return None


def itunes_search_song(term: str, country: str = "tw", limit: int = 5) -> list[dict]:
    """免金鑰查詢 iTunes Search API，將 YouTube 台灣熱門音樂影片反查為台灣 Apple Music 歌曲。"""
    q = (term or "").strip()
    if not q:
        return []
    url = "https://itunes.apple.com/search?" + urllib.parse.urlencode(
        {"term": q, "country": country, "entity": "song", "limit": str(limit)}
    )
    try:
        data = get_json(url, attempts=2)
        return [r for r in data.get("results", []) if r.get("trackId") and r.get("trackName") and r.get("artistName")]
    except Exception as exc:
        print(f"iTunes Search 失敗（略過 {q}）：{exc}", file=sys.stderr)
        return []


def fetch_youtube_popular_music(key: str, max_results: int = MAX_POPULAR_RESULTS) -> list[dict]:
    """呼叫 YouTube Data API videos.list(chart=mostPopular, regionCode=TW, videoCategoryId=10)，每次僅 1 單位。"""
    params = {
        "part": "snippet,contentDetails",
        "chart": "mostPopular",
        "videoCategoryId": YOUTUBE_MUSIC_CATEGORY_ID,
        "regionCode": QUERY_REGION_CODE,
        "maxResults": str(max_results),
        "key": key,
    }
    url = "https://www.googleapis.com/youtube/v3/videos?" + urllib.parse.urlencode(params)
    try:
        resp = get_json(url, attempts=2)
        return resp.get("items", [])
    except Exception as exc:
        print(f"YouTube 台灣熱門音樂查詢失敗（略過）：{exc}", file=sys.stderr)
        return []


def compact_popular_items(items: list[dict]) -> list[dict]:
    """精簡保存 videos.list(chart=mostPopular) 回傳影片（不含金鑰），供離線重播。"""
    out = []
    for item in items:
        vid = item.get("id")
        if not isinstance(vid, str):
            continue
        snippet = item.get("snippet") or {}
        content = item.get("contentDetails") or {}
        out.append(
            {
                "id": vid,
                "snippet": {
                    "title": html.unescape(snippet.get("title") or ""),
                    "channelTitle": html.unescape(snippet.get("channelTitle") or ""),
                    "channelId": snippet.get("channelId") or "",
                    "publishedAt": snippet.get("publishedAt") or "",
                },
                "contentDetails": {
                    "duration": content.get("duration") or "",
                },
            }
        )
    return out


def _video_matches_song(v_title: str, v_channel: str, title: str, artist: str, alt_titles=(), alt_artists=()) -> bool:
    """檢查 YouTube 影片標題／頻道是否確實對應指定歌曲（供台灣熱門音樂反查與既有池比對）。"""
    norm_v_title = norm(html.unescape(v_title or ""))
    norm_v_channel = norm(html.unescape(v_channel or ""))
    if not norm_v_title:
        return False

    t_variants = title_variants(title, *(alt_titles or []), include_subtitle_head=True)
    title_patterns = [
        p
        for tv in t_variants
        if (p := _compile_phrase_pattern(tv, allow_spaceless=not _is_short_or_common_title(tv))) is not None
    ]
    if not any(p.search(norm_v_title) for p in title_patterns):
        return False

    a_names = _artist_match_candidates(artist, list(alt_artists or []))
    artist_patterns = [
        p
        for an in a_names
        if (p := _compile_phrase_pattern(an, allow_spaceless=True)) is not None
    ]
    return any(p.search(norm_v_title) or p.search(norm_v_channel) for p in artist_patterns)


def enrich_pool_with_youtube_popular(
    pool: list[dict],
    popular_items: list[dict],
    search_fn=itunes_search_song,
    max_lookups: int = MAX_POPULAR_LOOKUPS,
) -> list[dict]:
    """用 YouTube 台灣熱門音樂影片補齊候選池與 ytPopularRank：
    1. 先匹配既有 Apple 候選池中的歌曲，補上 ytPopularRank（1-based 最高名次）。
    2. 未匹配者以 iTunes Search 反查台灣曲目，確認曲名＋演出者吻合後合併或新增為候選歌。
    """
    if not popular_items:
        return pool

    seen: dict[tuple, dict] = {}
    for entry in pool:
        for k in _dedup_keys(entry["item"], entry.get("altTitles") or [], entry.get("altArtists") or []):
            seen[k] = entry

    new_entries: list[dict] = []
    lookups = 0

    for rank, video in enumerate(popular_items, start=1):
        snippet = video.get("snippet") or {}
        v_title = html.unescape(snippet.get("title") or "")
        v_channel = html.unescape(snippet.get("channelTitle") or "")
        if not v_title:
            continue

        matched_entry = None
        for entry in pool:
            it = entry["item"]
            if _video_matches_song(
                v_title, v_channel, it["name"], it["artistName"],
                alt_titles=entry.get("altTitles") or [],
                alt_artists=entry.get("altArtists") or [],
            ):
                matched_entry = entry
                break

        if matched_entry is not None:
            if matched_entry.get("ytPopularRank") is None or rank < matched_entry["ytPopularRank"]:
                matched_entry["ytPopularRank"] = rank
            continue

        if lookups >= max_lookups or search_fn is None:
            continue
        parsed = parse_popular_video_title(v_title, v_channel)
        if not parsed:
            continue
        artist_hint, title_hint = parsed
        lookups += 1
        results = search_fn(f"{title_hint} {artist_hint}", "tw", 5)

        chosen = None
        for rec in results:
            rec_title = rec.get("trackName") or ""
            rec_artist = rec.get("artistName") or ""
            if _video_matches_song(v_title, v_channel, rec_title, rec_artist):
                chosen = rec
                break
        if chosen is None:
            continue

        loc_item = {
            "id": str(chosen["trackId"]),
            "name": chosen["trackName"],
            "artistName": chosen["artistName"],
            "artistId": str(chosen.get("artistId") or ""),
            "url": chosen.get("trackViewUrl") or f"https://music.apple.com/tw/song/{chosen['trackId']}",
            "artworkUrl100": chosen.get("artworkUrl100") or "",
        }
        alt_titles = list(dict.fromkeys(t for t in (chosen["trackName"], title_hint) if t))
        alt_artists = list(dict.fromkeys(a for a in (chosen["artistName"], artist_hint) if a))
        keys = _dedup_keys(loc_item, alt_titles)
        lookup = _dedup_keys(loc_item, alt_titles, for_lookup=True)
        existing = next((seen[k] for k in lookup if k in seen), None)
        if existing is not None:
            if existing.get("ytPopularRank") is None or rank < existing["ytPopularRank"]:
                existing["ytPopularRank"] = rank
            for t in alt_titles:
                if t not in existing["altTitles"]:
                    existing["altTitles"].append(t)
            for a in alt_artists:
                if a not in existing["altArtists"]:
                    existing["altArtists"].append(a)
            for k in keys:
                seen[k] = existing
            continue

        new_entry = {
            "item": loc_item,
            "storefront": "yt_popular",
            "twRank": None,
            "krRank": None,
            "ytPopularRank": rank,
            "altTitles": alt_titles,
            "altArtists": alt_artists,
            "genres": [chosen["primaryGenreName"]] if chosen.get("primaryGenreName") else [],
        }
        for k in keys:
            seen[k] = new_entry
        new_entries.append(new_entry)

    if not new_entries:
        return pool

    # 將 YouTube 台灣熱門新發掘的歌曲插入候選池前中段（第 40 位之後），確保能進當日查詢配額而不擠掉台／韓前段強曲
    insert_at = min(40, len(pool))
    return pool[:insert_at] + new_entries + pool[insert_at:]


def fetch_video_durations(video_ids: list[str], key: str) -> dict[str, int]:
    """批次查詢 YouTube videos.list(part=contentDetails) 取得每支影片秒數（每 50 支僅 1 單位）。"""
    unique_ids = [vid for vid in dict.fromkeys(video_ids) if vid]
    durations: dict[str, int] = {}
    for start in range(0, len(unique_ids), 50):
        batch = unique_ids[start : start + 50]
        params = {
            "part": "contentDetails",
            "id": ",".join(batch),
            "key": key,
        }
        url = "https://www.googleapis.com/youtube/v3/videos?" + urllib.parse.urlencode(params)
        try:
            resp = get_json(url, attempts=2)
            for item in resp.get("items", []):
                vid = item.get("id")
                dur = parse_iso8601_duration((item.get("contentDetails") or {}).get("duration") or "")
                if isinstance(vid, str) and vid and dur is not None:
                    durations[vid] = dur
        except Exception as exc:
            print(f"YouTube videos.list 時長查詢失敗（略過批次 {start // 50 + 1}）：{exc}", file=sys.stderr)
    return durations


def evaluate_shorts_from_verified(
    verified_videos: list[tuple[str, str]],
    durations: dict[str, int],
    max_seconds: int = SHORTS_MAX_DURATION_SECONDS,
) -> dict:
    """從已通過舞蹈驗證與頻道節流的影片清單中，計算 ≤ 60 秒的 Shorts 舞蹈影片數與頻道數。"""
    shorts_verified = 0
    shorts_channels: set[str] = set()
    for vid, ch_key in verified_videos:
        sec = durations.get(vid)
        if sec is not None and 0 < sec <= max_seconds:
            shorts_verified += 1
            shorts_channels.add(ch_key)
    return {
        "shortsVerified": shorts_verified,
        "shortsUniqueChannels": len(shorts_channels),
    }


def _is_short_or_common_title(norm_title: str) -> bool:
    compact = re.sub(r"[^a-z0-9]+", "", norm_title)
    if norm_title in COMMON_TITLES:
        return True
    if not _has_non_ascii_letter(norm_title) and len(compact) <= 6:
        return True
    return False


def _compile_phrase_pattern(phrase: str, allow_spaceless: bool = False) -> re.Pattern | None:
    text = norm(phrase).strip(" !?.,~-")
    if not text:
        return None
    # 處理英文縮寫撇號可選（You're -> you'?re）與空白彈性
    parts = []
    tokens = re.split(r"\s+", text)
    for tok in tokens:
        if not tok:
            continue
        escaped = "'?".join(re.escape(seg) for seg in tok.split("'"))
        # 允許中英混排間可有可無的空白（例如 甲乙丙丁Strangers）
        parts.append(escaped)
    if not parts:
        return None
    # CJK/韓文多字詞組允許略過空白（如 첫 눈 / 첫눈）；英文多字短語允許空白、連字號、底線分隔
    sep = r"\s*" if _has_non_ascii_letter(text) else r"[\s\-_]+"
    body = sep.join(parts)
    prefix = r"(?<![a-z0-9])" if text[0].isascii() and text[0].isalnum() else ""
    suffix = r"(?![a-z0-9])" if text[-1].isascii() and text[-1].isalnum() else ""
    patterns = [f"{prefix}{body}{suffix}"]

    if allow_spaceless and not _has_non_ascii_letter(text):
        compact = re.sub(r"[^a-z0-9]+", "", text)
        if len(compact) >= 7:
            patterns.append(rf"(?<![a-z0-9]){re.escape(compact)}(?![a-z0-9])")

    return re.compile("|".join(patterns), re.IGNORECASE)


def _artist_match_candidates(artist: str, alt_artists: list[str]) -> list[str]:
    names = []
    seen = set()

    def add(v: str):
        v = (v or "").strip()
        k = norm(v)
        if k and k not in seen:
            seen.add(k)
            names.append(v)

    for raw in [artist, primary_artist(artist), *(alt_artists or [])]:
        if not raw:
            continue
        add(raw)
        prim = primary_artist(raw)
        add(prim)
        for alias in ARTIST_GROUP_BY_NORM.get(norm(prim), []):
            add(alias)
        for alias in ARTIST_GROUP_BY_NORM.get(norm(raw), []):
            add(alias)
    return names


def matches_dance_intent(v_title: str, v_desc: str = "") -> bool:
    """確認影片具備明確舞蹈意圖，排除純歌詞、吉他翻唱、開箱反應等非舞蹈影片。"""
    if _strong_dance_intent(v_title, v_desc):
        return True
    # 〔D14〕演唱會／舞台直拍（fancam、직캠、focus cam、dance break）也算同一首歌的舞蹈版本
    return bool(FANCAM_RE.search(v_title))


def _strong_dance_intent(v_title: str, v_desc: str = "") -> bool:
    if STRONG_DANCE_RE.search(v_title):
        return True
    if CHALLENGE_RE.search(v_title) and not NON_DANCE_RE.search(v_title):
        return True
    # 若標題沒寫舞字但前段簡介有明確舞蹈關鍵字，且標題不是非舞蹈類型，才採計
    if v_desc and STRONG_DANCE_RE.search(v_desc[:240]) and not NON_DANCE_RE.search(v_title):
        return True
    return False


def evaluate_youtube_items(
    title: str,
    artist: str,
    items: list[dict],
    now: dt.datetime,
    alt_titles: list[str] | None = None,
    alt_artists: list[str] | None = None,
) -> dict:
    """逐筆驗證 search.list 回傳影片是否真的屬於該首歌的舞蹈影片，並依頻道上限節流。"""
    t_variants = title_variants(title, *(alt_titles or []), include_subtitle_head=True)
    strict_title = any(_is_short_or_common_title(tv) for tv in t_variants)
    title_patterns = [
        p
        for tv in t_variants
        if (p := _compile_phrase_pattern(tv, allow_spaceless=not _is_short_or_common_title(tv))) is not None
    ]

    a_names = _artist_match_candidates(artist, alt_artists or [])
    artist_patterns = [
        p
        for an in a_names
        if (p := _compile_phrase_pattern(an, allow_spaceless=True)) is not None
    ]

    # 舞者常把歌名和 challenge/dance 連成單一 hashtag。只接受完整標籤，並避免純符號曲名產生裸 #challenge。
    compact_dance_tags = set()
    for tv in t_variants:
        slug = re.sub(r"[^\w]+", "", tv).replace("_", "")
        if len(slug) >= 2:
            for suffix in ("challenge", "챌린지", "dance", "dancecover", "dancechallenge", "dancepractice"):
                compact_dance_tags.add("#" + slug + suffix)

    seven_days_ago = now - dt.timedelta(days=7)
    seen_videos = set()
    channel_counts: dict[str, int] = {}
    verified_videos: list[tuple[str, str]] = []
    matched_raw = 0
    verified = 0
    recent_7d = 0

    for item in items:
        vid = (item.get("id") or {}).get("videoId")
        if vid:
            if vid in seen_videos:
                continue
            seen_videos.add(vid)

        snippet = item.get("snippet") or {}
        v_title = html.unescape(snippet.get("title") or "")
        v_channel = html.unescape(snippet.get("channelTitle") or "")
        v_desc = html.unescape(snippet.get("description") or "")
        norm_title = norm(v_title)
        norm_channel = norm(v_channel)
        norm_desc = norm(v_desc[:300])

        # 1. 曲名必須出現在影片標題（不看 description，避免「Please subscribe」「with me」誤命中）
        title_hashtags = {"#" + tag[1:].replace("_", "") for tag in re.findall(r"#[\w]+", norm_title)}
        tagged_dance = bool(compact_dance_tags.intersection(title_hashtags))
        if not tagged_dance and not any(p.search(norm_title) for p in title_patterns):
            continue
        # 1b. 〔D14〕只靠直拍用語判定舞蹈的影片，常以引號標出實際曲名（含 'Intro + 15-LOVE + RUDE!' 這類組曲）：
        #     標題有引號曲名時，本曲名必須在某個引號內，避免「'15-LOVE' 직캠 (… Focus)」被算成 FOCUS。
        if not tagged_dance and not _strong_dance_intent(v_title, v_desc):
            quoted = [next(g for g in m.groups() if g) for m in QUOTED_SEGMENT_RE.finditer(norm_title)]
            if quoted and not any(p.search(q) for q in quoted for p in title_patterns):
                continue

        # 2. 演出者需出現在標題或頻道；非常見／短曲名才允許退回簡介前段
        artist_hit = any(p.search(norm_title) or p.search(norm_channel) for p in artist_patterns)
        if not artist_hit and not strict_title:
            artist_hit = any(p.search(norm_desc) for p in artist_patterns)
        if not artist_hit:
            continue

        # 3. 必須具舞蹈意圖
        if NON_DANCE_RE.search(v_title) or not (tagged_dance or matches_dance_intent(v_title, v_desc)):
            continue

        matched_raw += 1
        ch_key = snippet.get("channelId") or norm_channel or vid or f"anon-{matched_raw}"
        used = channel_counts.get(ch_key, 0)
        if used >= MAX_PER_CHANNEL:
            continue
        channel_counts[ch_key] = used + 1
        verified += 1
        if vid:
            verified_videos.append((vid, ch_key))

        pub = snippet.get("publishedAt") or ""
        try:
            pub_dt = dt.datetime.fromisoformat(pub.replace("Z", "+00:00"))
            if pub_dt >= seven_days_ago:
                recent_7d += 1
        except ValueError:
            pass

    total_fetched = len(items)
    precision = (matched_raw / total_fetched) if total_fetched else 0.0
    return {
        "verified": verified,
        "matchedRaw": matched_raw,
        "fetched": total_fetched,
        "uniqueChannels": len(channel_counts),
        "recent7d": recent_7d,
        "precision": precision,
        "strictTitle": strict_title,
        "verifiedVideos": verified_videos,
    }


def compact_youtube_items(items: list[dict]) -> list[dict]:
    """精簡保存 search.list 原始回傳影片，供離線調參重播（與 evaluate_youtube_items 格式相容）。"""
    compacted = []
    for item in items:
        snippet = item.get("snippet") or {}
        compacted.append(
            {
                "id": {"videoId": (item.get("id") or {}).get("videoId") or ""},
                "snippet": {
                    "title": html.unescape(snippet.get("title") or ""),
                    "channelTitle": html.unescape(snippet.get("channelTitle") or ""),
                    "channelId": snippet.get("channelId") or "",
                    "publishedAt": snippet.get("publishedAt") or "",
                    "description": html.unescape(snippet.get("description") or "")[:400],
                },
            }
        )
    return compacted


def dance_signal(entry: dict, key: str, since: str, now: dt.datetime) -> dict:
    it = entry["item"]
    query = build_youtube_query(it["name"], it["artistName"], entry.get("altTitles"), entry.get("altArtists"))
    params = {
        "part": "snippet",
        "type": "video",
        "maxResults": str(QUERY_MAX_RESULTS),
        "regionCode": QUERY_REGION_CODE,
        "publishedAfter": since,
        "q": query,
    }
    q = urllib.parse.urlencode({**params, "key": key})
    resp = get_json("https://www.googleapis.com/youtube/v3/search?" + q)
    items = resp.get("items", [])
    raw_total = int((resp.get("pageInfo") or {}).get("totalResults", 0) or 0)
    stats = evaluate_youtube_items(
        it["name"],
        it["artistName"],
        items,
        now=now,
        alt_titles=entry.get("altTitles"),
        alt_artists=entry.get("altArtists"),
    )
    stats["rawTotal"] = raw_total
    stats["query"] = query
    stats["items"] = compact_youtube_items(items)
    stats["params"] = {k: params[k] for k in ("regionCode", "publishedAfter", "maxResults")}
    return stats


def _stat_summary(st: dict) -> dict:
    return {
        k: st[k]
        for k in (
            "verified", "matchedRaw", "fetched", "uniqueChannels", "recent7d",
            "precision", "strictTitle", "shortsVerified", "shortsUniqueChannels",
        )
        if k in st
    }


def write_raw_cache(
    path: str | os.PathLike,
    *,
    now: dt.datetime,
    max_queries: int,
    pool_size: int,
    queried: int,
    failed: int,
    since: str,
    records: list[dict],
    popular_videos: list[dict] | None = None,
    video_durations: dict[str, int] | None = None,
) -> None:
    """輸出每首歌原始搜尋結果快取（絕不含 API 金鑰），供 Actions artifact 下載與離線調參。"""
    parent = os.path.dirname(os.fspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    payload = {
        "generatedAt": now.isoformat().replace("+00:00", "Z"),
        "params": {
            "maxQueries": max_queries,
            "firstPassPerArtist": FIRST_PASS_PER_ARTIST,
            "windowDays": QUERY_WINDOW_DAYS,
            "regionCode": QUERY_REGION_CODE,
            "maxResults": QUERY_MAX_RESULTS,
            "publishedAfter": since,
            "minDanceVideos": MIN_DANCE_VIDEOS,
            "minShortsVideos": MIN_SHORTS_VIDEOS,
            "shortsMaxSeconds": SHORTS_MAX_DURATION_SECONDS,
            "maxPerChannel": MAX_PER_CHANNEL,
        },
        "poolSize": pool_size,
        "queried": queried,
        "failed": failed,
        "popularVideos": popular_videos or [],
        "videoDurations": video_durations or {},
        "entries": records,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
        f.write("\n")


def replay_raw_cache(path: str | os.PathLike) -> tuple[dt.datetime, list[dict], dict[str, dict], set[str], int]:
    """從原始搜尋快取檔離線重算每首驗證統計與 Shorts 數量（零網路與 YouTube 額度消耗）。"""
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    gen_at = raw.get("generatedAt") or ""
    try:
        now = dt.datetime.fromisoformat(gen_at.replace("Z", "+00:00"))
    except ValueError:
        now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)

    durations: dict[str, int] = {
        str(k): int(v) for k, v in (raw.get("videoDurations") or {}).items() if isinstance(v, (int, float))
    }
    pool: list[dict] = []
    stats_by_id: dict[str, dict] = {}
    failed_ids: set[str] = set()
    queried = 0

    for rec in raw.get("entries", []):
        tid = str(rec["id"])
        item = {
            "id": tid,
            "name": rec["title"],
            "artistName": rec["artist"],
            "url": rec.get("url") or f"https://music.apple.com/tw/song/{tid}",
            "artworkUrl100": rec.get("artworkUrl100") or "",
        }
        entry = {
            "item": item,
            "storefront": rec.get("storefront") or "tw",
            "twRank": rec.get("twRank"),
            "krRank": rec.get("krRank"),
            "ytPopularRank": rec.get("ytPopularRank"),
            "altTitles": rec.get("altTitles") or [rec["title"]],
            "altArtists": rec.get("altArtists") or [rec["artist"]],
            "genres": rec.get("genres") or [],
        }
        pool.append(entry)
        queried += 1
        if rec.get("error"):
            failed_ids.add(tid)
            continue
        items = rec.get("items") or []
        st = evaluate_youtube_items(
            item["name"],
            item["artistName"],
            items,
            now=now,
            alt_titles=entry["altTitles"],
            alt_artists=entry["altArtists"],
        )
        shorts_st = evaluate_shorts_from_verified(st.get("verifiedVideos") or [], durations)
        st.update(shorts_st)
        st["rawTotal"] = int(rec.get("rawTotal") or 0)
        st["query"] = rec.get("query") or ""
        stats_by_id[tid] = st
        print(
            f"YT(replay) | {item['name']} — {item['artistName']} | "
            f"rawTotal={st['rawTotal']} fetched={st['fetched']} "
            f"matched={st['matchedRaw']} verified={st['verified']} "
            f"shorts={st['shortsVerified']} ch={st['uniqueChannels']} r7d={st['recent7d']} "
            f"prec={st['precision']:.0%}",
            flush=True,
        )

    return now, pool, stats_by_id, failed_ids, queried


# ---- 〔D13〕季節歌與韓日舞曲辨識 ----

# 季節歌：只看曲名（含別名），不看演出者。以節慶字詞＋少數冬季節慶經典曲名為準，不用單字 snow／winter 以免誤傷一般歌。
SEASONAL_TITLE_RE = re.compile(
    r"(?:(?<![a-z0-9])(?:christmas|xmas|x-mas|santa|mistletoe|jingle\s*bells?|sleigh\s*ride|reindeer|navidad|"
    r"let\s+it\s+snow|winter\s+wonderland|underneath\s+the\s+tree|happy\s+new\s+year|"
    r"snowman|first\s+snow|holly\s+jolly|silent\s+night)(?![a-z0-9])"
    r"|聖誕|圣诞|耶誕|新年快樂|恭喜發財|恭喜恭喜|賀新年|過新年|크리스마스|캐롤|첫\s*눈|クリスマス|サンタ)",
    re.IGNORECASE,
)

KPOP_GENRES = {"k-pop", "kpop", "韓國流行樂", "韓國流行音樂", "케이팝", "가요"}
JPOP_GENRES = {"j-pop", "jpop", "日本流行樂", "日本流行音樂", "動畫", "anime", "アニメ"}
# 別名表多數為韓團，但 back number 等日團也有韓文譯名，須先判日文
JPOP_ARTISTS = {
    norm(a) for a in (
        "back number", "YOASOBI", "Ado", "Creepy Nuts", "米津玄師", "Kenshi Yonezu", "Mrs. GREEN APPLE",
        "King Gnu", "Official髭男dism", "Vaundy", "藤井風", "Fujii Kaze", "imase", "Number_i",
        "ATARASHII GAKKO!", "新しい学校のリーダーズ", "こっちのけんと", "tuki.", "優里",
    )
}
# 別名表外、無韓文別名但屬 K-pop 舞曲的演出者
KPOP_EXTRA_ARTISTS = {norm("HUNTR/X"), norm("HUNTRX")}

HANGUL_RE = re.compile(r"[가-힣ᄀ-ᇿ㄰-㆏]")
KANA_RE = re.compile(r"[぀-ゟ゠-ヿㇰ-ㇿ]")


def is_seasonal_song(entry: dict) -> bool:
    it = entry.get("item") or {}
    titles = [it.get("name") or "", *(entry.get("altTitles") or [])]
    return any(SEASONAL_TITLE_RE.search(norm(t)) for t in titles if t)


def kj_language(entry: dict) -> str | None:
    """判定韓文（ko）／日文（ja）舞曲：依序看 Apple 曲風、日團名單與假名、韓文字、韓團別名表。判定不出回傳 None。"""
    it = entry.get("item") or {}
    genres = {norm(g) for g in entry.get("genres") or []}
    if genres & KPOP_GENRES:
        return "ko"
    if genres & JPOP_GENRES:
        return "ja"
    artists = [a for a in (it.get("artistName") or "", *(entry.get("altArtists") or [])) if a]
    titles = [t for t in (it.get("name") or "", *(entry.get("altTitles") or [])) if t]
    artist_keys = {k for a in artists for k in artist_member_keys(a)}
    if artist_keys & JPOP_ARTISTS or any(KANA_RE.search(t) for t in titles + artists):
        return "ja"
    if any(HANGUL_RE.search(t) for t in titles + artists) or artist_keys & KPOP_EXTRA_ARTISTS:
        return "ko"
    for key in artist_keys:
        if any(HANGUL_RE.search(alias) for alias in ARTIST_GROUP_BY_NORM.get(key, [])):
            return "ko"
    return None


def dance_rank_weight(entry: dict) -> float:
    """舞蹈名次排序權重：韓日 ×1.20（上限，不疊加）、季節歌 ×0.80；兩者同時成立時相乘（0.96）。"""
    weight = 1.0
    if kj_language(entry):
        weight *= KJ_DANCE_MULTIPLIER
    if is_seasonal_song(entry):
        weight *= SEASONAL_DANCE_MULTIPLIER
    return weight


# ---- App 排名預演（與 DancingDiary DanceChartSnapshot.rankedEntries 同規則；docs/dance-chart-feed.md「排名規則」）----

def _distinct_by_source(signals: list[dict]) -> list[dict]:
    """與 App `distinctBySource` 一致：先依名次排序，再依來源（不分大小寫）各取名次最佳的一筆。
    串流參考分數取前兩筆時因此依訊號名次而非寫入順序。"""
    seen, out = set(), []
    for sig in sorted(signals, key=lambda s: s["rank"]):
        source = str(sig["source"]).lower()
        if source not in seen:
            seen.add(source)
            out.append(sig)
    return out


def _app_song_scores(song: dict) -> tuple[bool, float, float]:
    """回傳 (是否入第一層, 舞蹈主分數, 串流參考分數)。"""
    sigs = song.get("signals") or []
    dance = _distinct_by_source([
        s for s in sigs
        if s.get("kind") == "dance" and (s.get("rank") or 0) > 0
        and not (s.get("videoCount") is not None and s["videoCount"] < APP_MIN_DANCE_VIDEOS)
    ])
    streaming = _distinct_by_source([s for s in sigs if s.get("kind") == "streaming" and (s.get("rank") or 0) > 0])
    measured = any(s["source"] in APP_MEASURED_DANCE_SOURCES for s in dance)
    eligible = measured or (bool(song.get("officialDance")) and bool(dance))
    base = max(
        (
            100 / (s["rank"] ** 0.5)
            + (min(30, s["previousRank"] - s["rank"]) if (s.get("previousRank") or 0) > s["rank"] else 0)
            for s in dance
        ),
        default=0.0,
    )
    platforms = len({s["source"].lower() for s in dance + streaming})
    mult = 1.0 + min(APP_MAX_CROSS_PLATFORM_BONUS, max(0, platforms - 1) * APP_CROSS_PLATFORM_BONUS_PER_SOURCE)
    streaming_score = sum(1 / (s["rank"] ** 0.5) for s in streaming[:2])
    return eligible, base * mult, streaming_score


def app_rank_preview(songs: list[dict], limit: int = APP_MAX_RANKED_SONGS) -> list[tuple[dict, str]]:
    """預演 App 完整榜單：回傳 [(song, "dance"|"backfill")]，含同演出者最多 3 首（跨兩層）與 15 首上限。"""
    scored = [(song, *_app_song_scores(song)) for song in songs]
    dance_pool = sorted((x for x in scored if x[1]), key=lambda x: (-x[2], -x[3], x[0]["id"]))
    backfill_pool = sorted(
        (
            x for x in scored
            if not x[1] and any(s.get("kind") == "streaming" and (s.get("rank") or 0) > 0 for s in x[0].get("signals") or [])
        ),
        key=lambda x: (-x[3], x[0]["id"]),
    )
    result: list[tuple[dict, str]] = []
    seen: set[str] = set()
    artist_counts: dict[str, int] = {}
    for pool_items, tier in ((dance_pool, "dance"), (backfill_pool, "backfill")):
        for song, *_ in pool_items:
            if len(result) >= limit:
                break
            if song["id"] in seen:
                continue
            seen.add(song["id"])
            akey = primary_artist(song.get("artist") or "").lower()
            if akey:
                if artist_counts.get(akey, 0) >= APP_MAX_SONGS_PER_ARTIST:
                    continue
                artist_counts[akey] = artist_counts.get(akey, 0) + 1
            result.append((song, tier))
    return result


def enforce_seasonal_cap(
    songs: list[dict],
    seasonal_ids: set[str],
    max_seasonal: int = MAX_SEASONAL_IN_TOP,
    limit: int = APP_MAX_RANKED_SONGS,
) -> tuple[list[dict], list[dict]]:
    """依 App 排名預演讓前 `limit` 名最多 `max_seasonal` 首季節歌：保留預演名次最高者，
    其餘落在前 `limit` 名內的季節歌當日不輸出（逐首移除後重算直到符合）。回傳 (保留歌曲, 被移除歌曲)。"""
    kept = list(songs)
    removed: list[dict] = []
    while True:
        top = app_rank_preview(kept, limit)
        seasonal_in_top = [song for song, _ in top if song["id"] in seasonal_ids]
        if len(seasonal_in_top) <= max_seasonal:
            return kept, removed
        drop = seasonal_in_top[-1]
        kept = [s for s in kept if s["id"] != drop["id"]]
        removed.append(drop)


def previous_dance_signals() -> dict[tuple[str, str], dict]:
    try:
        with open(OUT, encoding="utf-8") as f:
            old = json.load(f)
    except (OSError, ValueError):
        return {}
    return {
        (s["id"], sig["source"]): sig
        for s in old.get("songs", [])
        for sig in s.get("signals", [])
        if sig.get("kind") == "dance" and sig.get("source") in (YOUTUBE_SOURCE, YOUTUBE_SHORTS_SOURCE)
    }


def _resolve_replay_path(argv: list[str] | None = None) -> str:
    args = list(sys.argv[1:] if argv is None else argv)
    for idx, arg in enumerate(args):
        if arg == "--replay" and idx + 1 < len(args):
            return args[idx + 1].strip()
        if arg.startswith("--replay="):
            return arg.split("=", 1)[1].strip()
    return os.environ.get("OFFLINE_RAW_INPUT", OFFLINE_RAW_INPUT).strip()


def main(argv: list[str] | None = None):
    replay_path = _resolve_replay_path(argv)
    popular_units = 0
    duration_units = 0
    if replay_path:
        now, pool, stats_by_id, failed_ids, queried = replay_raw_cache(replay_path)
    else:
        max_queries = int(os.environ.get("MAX_QUERIES") or str(DEFAULT_MAX_QUERIES))
        now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        pool = candidates()
        key = os.environ.get("YOUTUBE_API_KEY", "").strip()
        raw_cache_out = os.environ.get("RAW_CACHE_OUT", RAW_CACHE_OUT).strip()
        stats_by_id = {}
        failed_ids = set()
        raw_records: list[dict] = []
        popular_items: list[dict] = []
        video_durations: dict[str, int] = {}
        queried = 0
        since = (now - dt.timedelta(days=QUERY_WINDOW_DAYS)).isoformat().replace("+00:00", "Z")

        if key:
            popular_items = fetch_youtube_popular_music(key)
            popular_units = 1
            if popular_items:
                before_len = len(pool)
                pool = enrich_pool_with_youtube_popular(pool, popular_items)
                added = len(pool) - before_len
                matched_pop = sum(1 for e in pool if e.get("ytPopularRank") is not None)
                print(
                    f"YouTube 台灣熱門音樂：取得 {len(popular_items)} 支，對應 {matched_pop} 首（新增候選 {added} 首）",
                    flush=True,
                )

            for entry in select_query_entries(pool, max_queries):
                it = entry["item"]
                q_preview = build_youtube_query(
                    it["name"], it["artistName"], entry.get("altTitles"), entry.get("altArtists")
                )
                rec = {
                    "id": str(it["id"]),
                    "title": it["name"],
                    "artist": it["artistName"],
                    "storefront": entry.get("storefront"),
                    "twRank": entry.get("twRank"),
                    "krRank": entry.get("krRank"),
                    "ytPopularRank": entry.get("ytPopularRank"),
                    "url": it.get("url") or "",
                    "artworkUrl100": it.get("artworkUrl100") or "",
                    "altTitles": entry.get("altTitles") or [it["name"]],
                    "altArtists": entry.get("altArtists") or [it["artistName"]],
                    "genres": entry.get("genres") or [],
                    "query": q_preview,
                    "params": {
                        "regionCode": QUERY_REGION_CODE,
                        "publishedAfter": since,
                        "maxResults": str(QUERY_MAX_RESULTS),
                    },
                    "error": None,
                    "rawTotal": 0,
                    "stats": None,
                    "items": [],
                }
                try:
                    st = dance_signal(entry, key, since, now)
                    stats_by_id[it["id"]] = st
                    rec["query"] = st.get("query") or q_preview
                    rec["rawTotal"] = int(st.get("rawTotal") or 0)
                    rec["stats"] = _stat_summary(st)
                    rec["items"] = st.get("items") or []
                    print(
                        f"YT | {it['name']} — {it['artistName']} | "
                        f"rawTotal={st['rawTotal']} fetched={st['fetched']} "
                        f"matched={st['matchedRaw']} verified={st['verified']} "
                        f"ch={st['uniqueChannels']} r7d={st['recent7d']} "
                        f"prec={st['precision']:.0%}",
                        flush=True,
                    )
                except Exception as exc:  # 單首失敗不中斷整份
                    failed_ids.add(it["id"])
                    rec["error"] = str(exc)
                    print(f"YouTube 查詢失敗：{it['name']}：{exc}", file=sys.stderr)
                raw_records.append(rec)
                queried += 1

            # 批次查詢所有通過舞蹈驗證的影片時長（每 50 支 1 單位），計算近 14 天 Shorts（≤ 60 秒）舞蹈影片量
            all_verified_vids = [
                vid
                for st in stats_by_id.values()
                for vid, _ in (st.get("verifiedVideos") or [])
            ]
            unique_vids = list(dict.fromkeys(v for v in all_verified_vids if v))
            if unique_vids:
                duration_units = (len(unique_vids) + 49) // 50
                video_durations = fetch_video_durations(unique_vids, key)
            for rec in raw_records:
                tid = rec["id"]
                st = stats_by_id.get(tid)
                if st is not None:
                    shorts_st = evaluate_shorts_from_verified(st.get("verifiedVideos") or [], video_durations)
                    st.update(shorts_st)
                    rec["stats"] = _stat_summary(st)

        if raw_cache_out:
            write_raw_cache(
                raw_cache_out,
                now=now,
                max_queries=max_queries,
                pool_size=len(pool),
                queried=queried,
                failed=len(failed_ids),
                since=since,
                records=raw_records,
                popular_videos=compact_popular_items(popular_items),
                video_durations=video_durations,
            )

    if queried and len(failed_ids) * 100 > queried * 30:
        sys.exit(f"YouTube 查詢失敗 {len(failed_ids)}/{queried}，超過 30%，保留上一份 feed")

    # 〔D13〕舞蹈／Shorts 名次以「實測影片數 × 加權」排序（韓日 ×1.20、季節歌 ×0.80）；門檻與 videoCount 仍用實測數
    weight_by_id = {e["item"]["id"]: dance_rank_weight(e) for e in pool}
    ordered = sorted(
        ((st, tid) for tid, st in stats_by_id.items() if st["verified"] >= MIN_DANCE_VIDEOS),
        key=lambda x: (
            -round(x[0]["verified"] * weight_by_id.get(x[1], 1.0), 6),
            -x[0]["verified"],
            -x[0]["uniqueChannels"],
            -x[0]["recent7d"],
            -round(x[0]["precision"], 4),
            x[1],
        ),
    )
    dance_rank = {tid: r + 1 for r, (_, tid) in enumerate(ordered)}

    shorts_ordered = sorted(
        ((st, tid) for tid, st in stats_by_id.items() if st.get("shortsVerified", 0) >= MIN_SHORTS_VIDEOS),
        key=lambda x: (
            -round(x[0].get("shortsVerified", 0) * weight_by_id.get(x[1], 1.0), 6),
            -x[0].get("shortsVerified", 0),
            -x[0].get("shortsUniqueChannels", 0),
            -x[0]["verified"],
            x[1],
        ),
    )
    shorts_rank = {tid: r + 1 for r, (_, tid) in enumerate(shorts_ordered)}
    prev = previous_dance_signals()

    songs = []
    for entry in pool:
        it = entry["item"]
        tid = it["id"]
        sid = "am:" + tid
        signals = []
        if entry.get("twRank"):
            signals.append({"kind": "streaming", "source": STREAMING_SOURCE, "rank": entry["twRank"]})
        if entry.get("krRank"):
            signals.append({"kind": "streaming", "source": KR_STREAMING_SOURCE, "rank": entry["krRank"]})
        if entry.get("ytPopularRank"):
            signals.append({"kind": "streaming", "source": YOUTUBE_POPULAR_SOURCE, "rank": entry["ytPopularRank"]})
        if tid in dance_rank:
            sig = {
                "kind": "dance",
                "source": YOUTUBE_SOURCE,
                "rank": dance_rank[tid],
                "videoCount": stats_by_id[tid]["verified"],
            }
            if (sid, YOUTUBE_SOURCE) in prev:
                sig["previousRank"] = prev[(sid, YOUTUBE_SOURCE)]["rank"]
            signals.append(sig)
        elif tid in failed_ids and (sid, YOUTUBE_SOURCE) in prev:
            signals.append(prev[(sid, YOUTUBE_SOURCE)])
        if tid in shorts_rank:
            sig = {
                "kind": "dance",
                "source": YOUTUBE_SHORTS_SOURCE,
                "rank": shorts_rank[tid],
                "videoCount": stats_by_id[tid]["shortsVerified"],
            }
            if (sid, YOUTUBE_SHORTS_SOURCE) in prev:
                sig["previousRank"] = prev[(sid, YOUTUBE_SHORTS_SOURCE)]["rank"]
            signals.append(sig)
        elif tid in failed_ids and (sid, YOUTUBE_SHORTS_SOURCE) in prev:
            signals.append(prev[(sid, YOUTUBE_SHORTS_SOURCE)])
        if not signals:
            continue
        art = (it.get("artworkUrl100") or "").replace("100x100bb", "600x600bb")
        yt = "https://www.youtube.com/results?" + urllib.parse.urlencode(
            {"search_query": f"{clean_title(it['name'])} {search_artist_name(it['artistName'])} dance practice"}
        )
        streaming_links = []
        if (it.get("url") or "").startswith("https://"):
            streaming_links.append({"title": "Apple Music", "url": it["url"]})
        song = {
            "id": sid,
            "title": it["name"],
            "artist": it["artistName"],
            "officialDance": [],
            "streaming": streaming_links,
            "related": [{"title": "在 YouTube 看這首歌的舞蹈影片", "url": yt}],
            "credits": [],
            "signals": signals,
        }
        if art.startswith("https://"):
            song["artwork"] = art
        songs.append(song)

    seasonal_ids = {"am:" + e["item"]["id"] for e in pool if is_seasonal_song(e)}
    songs, capped = enforce_seasonal_cap(songs, seasonal_ids)
    for song in capped:
        print(f"季節歌上限：{song['title']} — {song['artist']} 當日不輸出（前 {APP_MAX_RANKED_SONGS} 名最多 {MAX_SEASONAL_IN_TOP} 首）")

    feed = {"updatedAt": now.isoformat().replace("+00:00", "Z"), "songs": songs}
    data = json.dumps(feed, ensure_ascii=False, indent=1)
    if len(data.encode()) > 1_000_000:
        sys.exit("feed 超過 1 MB，App 會拒收")
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(data + "\n")
    est_units = queried * 100 + popular_units + duration_units
    print(
        f"候選 {len(pool)} 首；YouTube 查詢 {queried} 次（失敗 {len(failed_ids)}）；"
        f"舞蹈訊號 {len(dance_rank)} 首；Shorts 訊號 {len(shorts_rank)} 首；季節歌移出 {len(capped)} 首；"
        f"估算額度 {est_units} 單位；輸出 {len(songs)} 首"
    )


if __name__ == "__main__":
    main()
