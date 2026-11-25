# dancing-diary-chart-feed

DancingDiary App 的熱門舞曲資料。GitHub Actions 每天台北時間 06:00 執行 `build_feed.py`，抓 Apple Music 公開榜單與 YouTube 舞蹈影片量，輸出 `feed.json`。

- App 讀取：`https://raw.githubusercontent.com/u0124036/dancing-diary-chart-feed/main/feed.json`
- 只蒐集訊號；排名規則在 App 端。
- YouTube 金鑰放在 repo 的 Actions secret `YOUTUBE_API_KEY`，不在程式碼裡。沒有金鑰時只輸出串流名次。
