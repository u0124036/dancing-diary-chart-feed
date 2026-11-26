"""一次性內部評估腳本：抓取 KKBOX 台灣（territory=TW）主要排行榜前 50 首。

僅供手動觸發（workflow_dispatch）輸出 artifact 供內部評估比對；
stdout 不印出金鑰、access token 或完整榜單內容。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

TOKEN_URL = "https://account.kkbox.com/oauth2/token"
API_BASE = "https://api.kkbox.com/v1.1"
TERRITORY = "TW"
TOP_N = 50
OUT_PATH = os.environ.get("KKBOX_PREVIEW_OUT", "kkbox-tw-preview.json")


def fetch_token(client_id: str, client_secret: str) -> str:
    body = urllib.parse.urlencode(
        {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        TOKEN_URL,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": "dancing-diary-kkbox-preview",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.load(resp)
    token = data.get("access_token")
    if not token:
        raise RuntimeError("OAuth response missing access_token")
    return str(token)


def api_get(path_or_url: str, token: str, params: dict | None = None) -> dict:
    if path_or_url.startswith("https://"):
        url = path_or_url
    else:
        url = f"{API_BASE}{path_or_url}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "dancing-diary-kkbox-preview",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def list_charts(token: str) -> list[dict]:
    charts: list[dict] = []
    offset = 0
    while True:
        payload = api_get(
            "/charts",
            token,
            {"territory": TERRITORY, "offset": offset, "limit": 50},
        )
        batch = payload.get("data") or []
        charts.extend(batch)
        next_url = (payload.get("paging") or {}).get("next")
        if not next_url or not batch or len(charts) >= 200:
            break
        offset += len(batch)
    return charts


def select_target_charts(charts: list[dict]) -> list[dict]:
    """優先挑出綜合／熱門單曲榜與綜合新歌榜（最多 4 張供評估對照）。"""
    picked: list[dict] = []
    seen_ids: set[str] = set()

    def add(chart: dict):
        cid = str(chart.get("id") or "")
        if cid and cid not in seen_ids:
            seen_ids.add(cid)
            picked.append(chart)

    # 第一順位：標題含「綜合」或「熱門」且含「單曲」／「新歌」／「日榜」
    priority_rules = [
        lambda t: ("綜合" in t and "單曲" in t) or ("Top" in t and "Singles" in t),
        lambda t: ("綜合" in t and "新歌" in t) or ("綜合" in t),
        lambda t: ("單曲" in t and ("華語" in t or "韓語" in t)) or ("熱門" in t),
    ]
    for rule in priority_rules:
        for ch in charts:
            title = str(ch.get("title") or "")
            if rule(title):
                add(ch)
                if len(picked) >= 4:
                    return picked

    # 備援：若標題命名不同，至少抓前 2 個榜單
    if not picked:
        for ch in charts[:2]:
            add(ch)
    return picked


def fetch_chart_tracks(chart_id: str, token: str, limit: int = TOP_N) -> list[dict]:
    tracks: list[dict] = []
    offset = 0
    while len(tracks) < limit:
        page_size = min(50, limit - len(tracks))
        payload = api_get(
            f"/charts/{urllib.parse.quote(chart_id, safe='')}/tracks",
            token,
            {"territory": TERRITORY, "offset": offset, "limit": page_size},
        )
        batch = payload.get("data") or []
        if not batch:
            break
        for idx, tr in enumerate(batch, start=len(tracks) + 1):
            album = tr.get("album") or {}
            artist = album.get("artist") or tr.get("artist") or {}
            tracks.append(
                {
                    "rank": idx,
                    "id": tr.get("id"),
                    "name": tr.get("name"),
                    "url": tr.get("url"),
                    "duration": tr.get("duration"),
                    "isrc": tr.get("isrc"),
                    "artist": {
                        "id": artist.get("id"),
                        "name": artist.get("name"),
                        "url": artist.get("url"),
                    },
                    "album": {
                        "id": album.get("id"),
                        "name": album.get("name"),
                        "release_date": album.get("release_date"),
                        "url": album.get("url"),
                    },
                }
            )
            if len(tracks) >= limit:
                break
        next_url = (payload.get("paging") or {}).get("next")
        if not next_url:
            break
        offset += len(batch)
    return tracks


def main() -> int:
    client_id = os.environ.get("KKBOX_CLIENT_ID", "").strip()
    client_secret = os.environ.get("KKBOX_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        print("ERROR: missing KKBOX_CLIENT_ID or KKBOX_CLIENT_SECRET", file=sys.stderr)
        return 1

    try:
        token = fetch_token(client_id, client_secret)
    except urllib.error.HTTPError as exc:
        print(f"ERROR: OAuth HTTP {exc.code}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"ERROR: OAuth failed: {type(exc).__name__}", file=sys.stderr)
        return 1

    charts = list_charts(token)
    available = [
        {
            "id": ch.get("id"),
            "title": ch.get("title"),
            "description": ch.get("description"),
            "updated_at": ch.get("updated_at"),
            "url": ch.get("url"),
        }
        for ch in charts
    ]

    selected_meta = select_target_charts(charts)
    selected_out = []
    counts = []
    for ch in selected_meta:
        cid = str(ch.get("id") or "")
        tracks = fetch_chart_tracks(cid, token, TOP_N)
        counts.append(len(tracks))
        selected_out.append(
            {
                "id": cid,
                "title": ch.get("title"),
                "description": ch.get("description"),
                "updated_at": ch.get("updated_at"),
                "url": ch.get("url"),
                "track_count": len(tracks),
                "tracks": tracks,
            }
        )

    result = {
        "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "territory": TERRITORY,
        "available_charts_count": len(available),
        "available_charts": available,
        "selected_charts": selected_out,
    }
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(
        f"OK: available_charts={len(available)}, selected={len(selected_out)}, "
        f"track_counts={counts}, out={OUT_PATH}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
