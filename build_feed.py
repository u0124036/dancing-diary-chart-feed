"""每日產生 DancingDiary 熱門舞曲 feed（feed.json）。

只蒐集訊號，不做最終排名：排名規則（舞蹈優先、同演出者最多 3 首、前 15 名）在 App 端。
- 串流：Apple Music 台灣最多人播放前 100、韓國前 50（公開 RSS，免金鑰）。
  透過 Apple 歌曲 ID、跨店面 artistId＋正規化曲名與 iTunes Lookup 台灣店面名稱合併同曲，
  統一使用台灣店面顯示名稱與連結，避免韓／英文重複上榜或繞過同演出者上限。
- 舞蹈：YouTube Data API search.list（part=snippet, maxResults=50），近 14 天、地區 TW。
  以引號精確查詢（歌名＋演出者＋舞蹈關鍵字）並對回傳影片標題／頻道逐筆驗證
  「曲名＋演出者＋舞蹈意圖」，排除常見字造成的 totalResults 膨脹與單頻道洗版，
  單首仍維持 1 次 search.list（100 單位），最多查 MAX_QUERIES 首，避免超過每日 10,000 單位。
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
YOUTUBE_SOURCE = "YouTube 舞蹈影片"
MIN_DANCE_VIDEOS = 5
MAX_PER_CHANNEL = 3
DEFAULT_MAX_QUERIES = 90
OUT = os.environ.get("FEED_OUT", "feed.json")

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
    r"|[\(\[\（]\s*single\s+version\s*[\)\]\）]"
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
    "crash", "aliens", "butter", "2.0", "fya", "rude", "like animals",
}

STRONG_DANCE_RE = re.compile(
    r"(?:(?<![a-z0-9])(?:dance|dancing|dancer|dancers|choreo|choreography|footwork)(?![a-z0-9])"
    r"|dance\s*(?:cover|practice|challenge|tutorial|mirror|ver|version|break)"
    r"|dancecover|dancepractice|dancechallenge|dancetutorial"
    r"|mirrored?\s*(?:dance|ver|version)"
    r"|舞蹈|練舞|練習室|翻跳|鏡面|編舞|排舞|跳舞|學舞|宅舞|街舞|舞室"
    r"|안무|댄스|커버댄스|춤"
    r"|ダンス|踊ってみた|振り付け|振付)",
    re.IGNORECASE,
)

CHALLENGE_RE = re.compile(r"(?:(?<![a-z0-9])challenge(?![a-z0-9])|challenge\b|챌린지|挑戰)", re.IGNORECASE)
NON_DANCE_RE = re.compile(
    r"(?:(?<![a-z0-9])(?:vocal|singing|lyrics|lyric|karaoke|piano|guitar|drum|drums|bass|violin|cello|flute|reaction|react|unboxing|vlog|quiz|guess|audio|instrumental)(?![a-z0-9])"
    r"|歌詞|彈唱|吉他|鋼琴|翻唱|反應|開箱|試吃|伴奏|純音樂)",
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


def _has_non_ascii_letter(s: str) -> bool:
    return any(ord(ch) > 127 and ch.isalpha() for ch in s)


def _has_ascii_letter(s: str) -> bool:
    return any("a" <= ch.lower() <= "z" for ch in s)


def title_variants(*titles: str) -> list[str]:
    """回傳用於跨榜去重與標題比對的正規化曲名變體（含雙語括號拆解）。"""
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
                # 僅在括號內外屬不同文字系統（如 韓文 (English)）時拆解，避免一般副標誤併
                if (_has_non_ascii_letter(outside) and _has_ascii_letter(inside)) or (
                    _has_ascii_letter(outside) and _has_non_ascii_letter(inside)
                ):
                    if len(re.sub(r"\W+", "", outside)) >= 2:
                        add(outside)
                    if len(re.sub(r"\W+", "", inside)) >= 3:
                        add(inside)
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


def _dedup_keys(item: dict, alt_titles: list[str]) -> list[tuple]:
    tid = str(item["id"])
    aid = str(item.get("artistId") or "")
    artist_key = canonical_artist_key(item["artistName"])
    keys: list[tuple] = [("id", tid)]
    for tv in title_variants(item["name"], *alt_titles):
        if aid:
            keys.append(("aid_title", aid, tv))
        if artist_key:
            keys.append(("artist_title", artist_key, tv))
    return keys


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
            keys = _dedup_keys(loc_item, alt_titles)
            existing = next((seen[k] for k in keys if k in seen), None)

            if existing is not None:
                for t in alt_titles:
                    if t and t not in existing["altTitles"]:
                        existing["altTitles"].append(t)
                for a in alt_artists:
                    if a and a not in existing["altArtists"]:
                        existing["altArtists"].append(a)
                if sf == "tw":
                    if existing["twRank"] is None or (i + 1) < existing["twRank"]:
                        existing["twRank"] = i + 1
                    # 台灣榜資料優先作為顯示與連結來源
                    if existing["storefront"] != "tw":
                        existing["item"] = loc_item
                        existing["storefront"] = "tw"
                for k in keys:
                    seen[k] = existing
                continue

            entry = {
                "item": loc_item,
                "storefront": sf,
                "twRank": i + 1 if sf == "tw" else None,
                "altTitles": list(dict.fromkeys(t for t in alt_titles if t)),
                "altArtists": list(dict.fromkeys(a for a in alt_artists if a)),
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
    return ARTIST_SEARCH_ALIASES.get(base, base)


def build_youtube_query(title: str, artist: str) -> str:
    """組合精確 YouTube 查詢：每個 OR 分支都帶引號包住的曲名與演出者，避免裸關鍵字造成 totalResults 暴增。"""
    q_title = clean_title(title).replace('"', "").replace("|", " ").strip()
    q_artist = search_artist_name(artist).replace('"', "").replace("|", " ").strip()
    if not q_title:
        q_title = title.replace('"', "").strip()
    if not q_artist:
        q_artist = artist.replace('"', "").strip()

    branches = [f'"{q_title}" "{q_artist}" {kw}' for kw in ("dance", "challenge", "舞蹈", "練習室")]
    query = "|".join(branches)
    if len(query) > 240:
        query = f'"{q_title}" "{q_artist}" dance|"{q_title}" "{q_artist}" 舞蹈'
    if len(query) > 240:
        query = f'"{q_title}" "{q_artist}" dance'
    return query


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
    # CJK/韓文多字詞組允許略過空白（如 첫 눈 / 첫눈）
    sep = r"\s*" if _has_non_ascii_letter(text) else r"[\s\-_]*\s+"
    # 針對一般英文多字短語改用寬鬆分隔（空白、連字號、底線）
    if not _has_non_ascii_letter(text):
        sep = r"[\s\-_]+"
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
    t_variants = title_variants(title, *(alt_titles or []))
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

    seven_days_ago = now - dt.timedelta(days=7)
    seen_videos = set()
    channel_counts: dict[str, int] = {}
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
        if not any(p.search(norm_title) for p in title_patterns):
            continue

        # 2. 演出者需出現在標題或頻道；非常見／短曲名才允許退回簡介前段
        artist_hit = any(p.search(norm_title) or p.search(norm_channel) for p in artist_patterns)
        if not artist_hit and not strict_title:
            artist_hit = any(p.search(norm_desc) for p in artist_patterns)
        if not artist_hit:
            continue

        # 3. 必須具舞蹈意圖
        if not matches_dance_intent(v_title, v_desc):
            continue

        matched_raw += 1
        ch_key = snippet.get("channelId") or norm_channel or vid or f"anon-{matched_raw}"
        used = channel_counts.get(ch_key, 0)
        if used >= MAX_PER_CHANNEL:
            continue
        channel_counts[ch_key] = used + 1
        verified += 1

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
    }


def dance_signal(entry: dict, key: str, since: str, now: dt.datetime) -> dict:
    it = entry["item"]
    query = build_youtube_query(it["name"], it["artistName"])
    q = urllib.parse.urlencode(
        {
            "part": "snippet",
            "type": "video",
            "maxResults": "50",
            "regionCode": "TW",
            "publishedAfter": since,
            "q": query,
            "key": key,
        }
    )
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
    return stats


def previous_dance_ranks() -> dict[str, int]:
    try:
        with open(OUT, encoding="utf-8") as f:
            old = json.load(f)
    except (OSError, ValueError):
        return {}
    return {
        s["id"]: sig["rank"]
        for s in old.get("songs", [])
        for sig in s.get("signals", [])
        if sig.get("source") == YOUTUBE_SOURCE
    }


def main():
    max_queries = int(os.environ.get("MAX_QUERIES") or str(DEFAULT_MAX_QUERIES))
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    pool = candidates()
    key = os.environ.get("YOUTUBE_API_KEY", "").strip()
    stats_by_id: dict[str, dict] = {}
    queried, errors = 0, 0

    if key:
        since = (now - dt.timedelta(days=14)).isoformat().replace("+00:00", "Z")
        for entry in pool[:max_queries]:
            it = entry["item"]
            try:
                st = dance_signal(entry, key, since, now)
                stats_by_id[it["id"]] = st
                print(
                    f"YT | {it['name']} — {it['artistName']} | "
                    f"rawTotal={st['rawTotal']} fetched={st['fetched']} "
                    f"matched={st['matchedRaw']} verified={st['verified']} "
                    f"ch={st['uniqueChannels']} r7d={st['recent7d']} "
                    f"prec={st['precision']:.0%}",
                    flush=True,
                )
            except Exception as exc:  # 單首失敗不中斷整份
                errors += 1
                print(f"YouTube 查詢失敗：{it['name']}：{exc}", file=sys.stderr)
            queried += 1

    ordered = sorted(
        ((st, tid) for tid, st in stats_by_id.items() if st["verified"] >= MIN_DANCE_VIDEOS),
        key=lambda x: (
            -x[0]["verified"],
            -x[0]["uniqueChannels"],
            -x[0]["recent7d"],
            -round(x[0]["precision"], 4),
            x[1],
        ),
    )
    dance_rank = {tid: r + 1 for r, (_, tid) in enumerate(ordered)}
    prev = previous_dance_ranks()

    songs = []
    for entry in pool:
        it = entry["item"]
        tid = it["id"]
        sid = "am:" + tid
        signals = []
        if entry["twRank"]:
            signals.append({"kind": "streaming", "source": STREAMING_SOURCE, "rank": entry["twRank"]})
        if tid in dance_rank:
            sig = {
                "kind": "dance",
                "source": YOUTUBE_SOURCE,
                "rank": dance_rank[tid],
                "videoCount": stats_by_id[tid]["verified"],
            }
            if sid in prev:
                sig["previousRank"] = prev[sid]
            signals.append(sig)
        if not signals:
            continue
        art = (it.get("artworkUrl100") or "").replace("100x100bb", "600x600bb")
        yt = "https://www.youtube.com/results?" + urllib.parse.urlencode(
            {"search_query": f"{clean_title(it['name'])} {search_artist_name(it['artistName'])} dance practice"}
        )
        song = {
            "id": sid,
            "title": it["name"],
            "artist": it["artistName"],
            "officialDance": [],
            "streaming": [{"title": "Apple Music", "url": it["url"]}],
            "related": [{"title": "在 YouTube 看這首歌的舞蹈影片", "url": yt}],
            "credits": [],
            "signals": signals,
        }
        if art.startswith("https://"):
            song["artwork"] = art
        songs.append(song)

    if key and queried and errors == queried:
        sys.exit("YouTube 全部查詢失敗，保留上一份 feed")
    feed = {"updatedAt": now.isoformat().replace("+00:00", "Z"), "songs": songs}
    data = json.dumps(feed, ensure_ascii=False, indent=1)
    if len(data.encode()) > 1_000_000:
        sys.exit("feed 超過 1 MB，App 會拒收")
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(data + "\n")
    print(
        f"候選 {len(pool)} 首；YouTube 查詢 {queried} 次（失敗 {errors}）；"
        f"有舞蹈訊號 {len(dance_rank)} 首；輸出 {len(songs)} 首"
    )


if __name__ == "__main__":
    main()
