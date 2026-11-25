"""每日產生 DancingDiary 熱門舞曲 feed（feed.json）。

只蒐集訊號，不做最終排名：排名規則（舞蹈優先、同演出者最多 3 首、前 15 名）在 App 端。
- 串流：Apple Music 台灣最多人播放前 100、韓國前 50（公開 RSS，免金鑰）。
- 舞蹈：YouTube Data API search.list，近 14 天、地區 TW 的舞蹈影片量；需環境變數 YOUTUBE_API_KEY。
  每次搜尋 100 單位，最多查 MAX_QUERIES 首，避免超過每日 10,000 單位。
格式與 App 的 DanceChartSnapshot 相同（見 DancingDiary docs/dance-chart-feed.md）。
"""
import datetime as dt, json, os, sys, urllib.parse, urllib.request

FEEDS = [("tw", 100), ("kr", 50)]
STREAMING_SOURCE = "Apple Music 台灣"
YOUTUBE_SOURCE = "YouTube 舞蹈影片"
MIN_DANCE_VIDEOS = 5
MAX_QUERIES = 90
OUT = os.environ.get("FEED_OUT", "feed.json")


def get_json(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "chart-feed-bot"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def apple(storefront, limit):
    url = f"https://rss.marketingtools.apple.com/api/v2/{storefront}/music/most-played/{limit}/songs.json"
    return get_json(url)["feed"]["results"]


def norm(s):
    return " ".join(s.lower().split())


def candidates():
    """依各榜名次交錯合併、以「歌名＋演出者」去重；保留台灣名次當串流訊號。"""
    lists = {sf: apple(sf, n) for sf, n in FEEDS}
    seen, merged = {}, []
    longest = max(len(v) for v in lists.values())
    for i in range(longest):
        for sf, _ in FEEDS:
            items = lists[sf]
            if i >= len(items):
                continue
            it = items[i]
            key = (norm(it["name"]), norm(it["artistName"]))
            if key in seen:
                if sf == "tw":
                    seen[key]["twRank"] = i + 1
                continue
            entry = {"item": it, "twRank": i + 1 if sf == "tw" else None}
            seen[key] = entry
            merged.append(entry)
    return merged


def dance_count(title, artist, key, since):
    q = urllib.parse.urlencode({
        "part": "id", "type": "video", "maxResults": "1", "regionCode": "TW",
        "publishedAfter": since, "q": f"{title} {artist} dance|舞蹈|練習室|challenge", "key": key})
    return get_json("https://www.googleapis.com/youtube/v3/search?" + q)["pageInfo"]["totalResults"]


def previous_dance_ranks():
    try:
        with open(OUT, encoding="utf-8") as f:
            old = json.load(f)
    except (OSError, ValueError):
        return {}
    return {s["id"]: sig["rank"] for s in old.get("songs", []) for sig in s.get("signals", [])
            if sig.get("source") == YOUTUBE_SOURCE}


def main():
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    pool = candidates()
    key = os.environ.get("YOUTUBE_API_KEY", "").strip()
    counts, queried, errors = {}, 0, 0
    if key:
        since = (now - dt.timedelta(days=14)).isoformat().replace("+00:00", "Z")
        for entry in pool[:MAX_QUERIES]:
            it = entry["item"]
            try:
                counts[it["id"]] = dance_count(it["name"], it["artistName"], key, since)
            except Exception as exc:  # 單首失敗不中斷整份
                errors += 1
                print(f"YouTube 查詢失敗：{it['name']}：{exc}", file=sys.stderr)
            queried += 1
    ordered = sorted(((c, i) for i, c in counts.items() if c >= MIN_DANCE_VIDEOS), key=lambda x: (-x[0], x[1]))
    dance_rank = {i: r + 1 for r, (_, i) in enumerate(ordered)}
    prev = previous_dance_ranks()

    songs = []
    for entry in pool:
        it = entry["item"]
        sid = "am:" + it["id"]
        signals = []
        if entry["twRank"]:
            signals.append({"kind": "streaming", "source": STREAMING_SOURCE, "rank": entry["twRank"]})
        if it["id"] in dance_rank:
            sig = {"kind": "dance", "source": YOUTUBE_SOURCE, "rank": dance_rank[it["id"]]}
            if sid in prev:
                sig["previousRank"] = prev[sid]
            signals.append(sig)
        if not signals:
            continue
        art = (it.get("artworkUrl100") or "").replace("100x100bb", "600x600bb")
        yt = "https://www.youtube.com/results?" + urllib.parse.urlencode(
            {"search_query": f"{it['name']} {it['artistName']} dance practice"})
        song = {"id": sid, "title": it["name"], "artist": it["artistName"],
                "officialDance": [], "streaming": [{"title": "Apple Music", "url": it["url"]}],
                "related": [{"title": "在 YouTube 看這首歌的舞蹈影片", "url": yt}],
                "credits": [], "signals": signals}
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
    print(f"候選 {len(pool)} 首；YouTube 查詢 {queried} 次（失敗 {errors}）；有舞蹈訊號 {len(dance_rank)} 首；輸出 {len(songs)} 首")


if __name__ == "__main__":
    main()
