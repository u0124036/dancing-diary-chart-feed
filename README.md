# dancing-diary-chart-feed

DancingDiary App 的熱門舞曲資料。GitHub Actions 每天台北時間 06:00 執行 `build_feed.py`，抓 Apple Music 公開榜單與 YouTube 舞蹈影片量，輸出 `feed.json`。

- App 讀取：`https://raw.githubusercontent.com/u0124036/dancing-diary-chart-feed/main/feed.json`
- 最終排名在 App 端；排程端決定舞蹈訊號名次：韓文／日文舞曲 ×1.20（上限）、季節歌 ×0.80，並依 App 同規則預演，前 15 名最多 1 首季節歌（dance-chart-daily#10）。演唱會 dance break 與舞台直拍計入同一首歌熱度。
- YouTube 金鑰放在 repo 的 Actions secret `YOUTUBE_API_KEY`，不在程式碼裡。沒有金鑰時只輸出串流名次。
- 每次 Actions 執行都會將 `feed.json` 與每首歌的原始搜尋快取 `youtube-raw-results.json`（不含金鑰）上傳為 artifact，可用 `gh run download <run_id>` 下載後執行 `python3 build_feed.py --replay youtube-raw-results.json` 離線調參，不消耗 YouTube 每日配額。
