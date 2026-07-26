# media-downloader

HLS (m3u8) 串流下載器。搭配瀏覽器的 Video DownloadHelper（或 DevTools Network 面板）嗅探出真實的 `.m3u8` URL，貼給本工具下載合併成 mp4。

mp4 直連 URL 用一般下載器（FDM）即可；本工具只專注處理下載器吃不下的分片串流。

## 需求

- Python 3（僅用標準庫，無須 pip install）
- ffmpeg（負責解密與合併，`-c copy` 不重新編碼）

## 使用

```bash
./download.py "https://example.com/video/master.m3u8" -o out.mp4

# 常用選項
-o out.mp4        # 輸出檔名（預設 video.mp4）
-q 720            # 偏好畫質（選最接近的 variant，預設最高畫質）
-j 16             # 平行下載數（預設 16）
-r "https://..."  # Referer（有些站台驗證來源頁）
-c "key=value"    # Cookie（需要登入的站台）
--limit 10        # 只下載前 N 個分片（預覽/測試用）
```

被擋 403 時，通常是缺 `-r`（來源頁網址）或 `-c`（登入 cookie），從瀏覽器 DevTools 複製即可。

## 運作原理（仿 Video DownloadHelper）

1. 抓 master playlist，列出各畫質 variant，挑一個
2. 抓 media playlist，收集所有分片 URL
3. **平行**下載全部分片（比 ffmpeg 逐段抓快得多）；中斷後重跑會跳過已完成的分片
4. AES-128 加密的 key 一併下載，改寫成本地 playlist
5. ffmpeg `-c copy` 解密＋合併 remux 成 mp4（不重新編碼）

特殊情況（分離音軌 rendition、SAMPLE-AES、BYTERANGE）自動退回 ffmpeg 直接吃 URL 的模式 —— 較慢但保證正確。
