# Podcast RSS Publisher — 設計文件

- 日期:2026-06-21
- 狀態:設計定稿,待寫實作計畫
- 所在 repo:`notebooklm-skill`

## 目的

在既有的 NotebookLM 薄 MCP 之上,**疊加一層「發布」能力**:把 `podcast_series`
產出的整季音檔轉成 podcast RSS feed,放到 NAS 對外目錄,由既有 reverse proxy 以
HTTPS 靜態服務,讓手機的 Apple Podcast 用 feed URL 訂閱、自動收新集。

音檔生成(NotebookLM)已完整可用且不在本設計範圍。本設計只處理**發布管線**。

## 心智模型:一主題 = 一節目 = 一 feed

```
主題 A → NotebookLM notebook/series → feed_A → EP01, EP02, EP03
主題 B → NotebookLM notebook/series → feed_B → EP01, EP02
```

一個「主題」用 `podcast_series` 跑出多集,那份 `series_manifest.json` 就是該節目的
集數清單。發布層吃這份 manifest,轉成該主題專屬的一個 feed。**天生多 feed,一主題一 feed。**

## 已鎖定決策

| 項目 | 決定 |
|---|---|
| 部署拓樸 | agent / MCP 與 NAS 同一 LAN;MCP 所在機可掛載 NAS 檔案系統 |
| 隱私 | 隨機 token URL:`/feeds/<token>/feed.xml`(不公開列出、猜不到、可直接訂閱) |
| NAS 能力 | 能跑 Docker、已有公網域名 + HTTPS + reverse proxy |
| 架構 | **選項 A:靜態 feed 產生器**,不自寫 HTTP 服務 |
| 發布粒度 | 一個指令整包發布整個系列(manifest → feed) |
| feed 來源 | 直接吃 `podcast_series` 的 `series_manifest.json` + 各集 mp3 |

### 為何選 A(靜態產生器)

所有條件都指向「feed 是靜態衍生物 + agent 在 LAN 能寫檔 + NAS 已有 reverse proxy」。
A 用最少活動零件達標:不必寫、顧、上鎖第二個服務。range request / Content-Length /
MIME 都由靜態檔案伺服器原生支援。發布邏輯獨立成純模組,將來若 agent 移到外網需要
HTTP 上傳前端(選項 B),只是加一層,不用重寫。

## 既有 manifest 形狀(實裝事實,設計據此)

`podcast_series` 寫出的 `series_manifest.json`:

```json
{
  "notebook_id": "...",
  "episodes": [
    {
      "episode": 1,
      "title": "心法篇",
      "label": "EP01 心法篇",
      "task_id": "...",
      "artifact_id": "...",
      "mp3_path": "<output_dir>/ep01.mp3"
    }
  ]
}
```

要點:
- 每集**有 `mp3_path`**(本機檔,名為 `ep{n:02d}.mp3`)→ 發布層拿得到音檔,**不需補欄位**。
- manifest **沒有 `brief`**,且 `brief` 是生成指令(人設/風格/承接),**不適合**當聽眾摘要 →
  episode `<description>` 預設用 `title`,需要再開選填覆寫。

## 檔案佈局(NAS 對外目錄)

```
$PODCAST_FEEDS_ROOT/                 # 例 /nas/podcasts
  registry.json                      # 全域:notebook_id → token(URL 穩定性的關鍵)
  feeds/
    <token>/                         # 一個主題/節目
      feed.xml                       # 衍生輸出,每次發布原子重建
      show.json                      # 節目級 metadata + 各集 guid/首次發布時間
      EP01.mp3                       # 對外檔名,URL-safe(不含中文)
      EP02.mp3
      artwork.png                    # 選填封面
```

## 三個穩定性鐵律

1. **音檔對外檔名走 `EP{n:02d}.mp3`(不含中文標題)** — 保證 URL-safe;中文標題只進 feed 的
   `<title>`。內部 `ep{n:02d}.mp3` → 對外 `EP{n:02d}.mp3` 由發布層複製時對應。
2. **token 一次產生、永久重用** — `registry.json` 記 `notebook_id → token`。同主題重發
   (加集、重生壞集)一律復用同 token。feed URL 給出去後不可變(podcast app 狠 cache)。
3. **`pubDate` / `guid` 跨重建穩定** — `show.json` 記每集「首次發布時間戳」與 `guid`
   (由 `token + 集號` 決定)。重建時讀回舊值;只有新集才補新時間。重生第 N 集只換檔內容,
   其他集日期/guid 不動,app 不會誤判成大量新集。

## RSS 內容

### Channel(節目級,來自 `show.json`)

| 欄位 | 來源 / 預設 |
|---|---|
| `<title>` | 主題名稱(預設帶 notebook 標題,可發布時指定) |
| `<description>` / `<itunes:summary>` | 節目簡介(發布時提供,可空) |
| `<language>` | `zh-Hant`(對齊 MCP 的 `zh_Hant` 預設) |
| `<itunes:author>` / `<itunes:owner>` | 作者(首次設定存 show.json,之後沿用) |
| `<itunes:image>` | `artwork.png` 公開 URL(選填,沒給就略) |
| `<itunes:category>` | 預設 `Technology` |
| `<itunes:explicit>` | 預設 `false` |
| `<link>` | feed 公開頁 URL |

### Item(每集,來自 manifest + show.json 穩定欄位)

| 欄位 | 來源 |
|---|---|
| `<title>` | manifest 的 `title`(中文,如「心法篇」) |
| `<description>` | 預設 = `title`;可選填覆寫(不用 brief) |
| `<pubDate>` | show.json 記錄的首次發布時間(RFC-822,穩定) |
| `<guid isPermaLink="false">` | `token + 集號`,穩定不變 |
| `<enclosure url length type>` | `EP{n}.mp3` 公開 URL · **檔案 byte 數** · `audio/mpeg` |
| `<itunes:duration>` | 選填(能算到才填) |

### 公開 URL 組法

由 `$PODCAST_PUBLIC_BASE_URL`(例 `https://podcast.example`)拼:
- feed:`{BASE}/feeds/<token>/feed.xml`
- 音檔:`{BASE}/feeds/<token>/EP01.mp3`

### 靜態服務

不自寫 HTTP 服務。在 NAS 的 reverse proxy 加一條路由,把 `{BASE}/feeds/` 指向
`$PODCAST_FEEDS_ROOT/feeds/`。range request / Content-Length / MIME 由靜態伺服器原生處理。

### 原子重建

`feed.xml` 先寫 `feed.xml.tmp` 再 `os.replace()` 改名,app 永不抓到寫一半的檔。

## 模組切分(設計隔離)

發布邏輯獨立成純模組,不綁 NotebookLM,可離線測試:

```
notebooklm_mcp/
  publish/
    feed.py         純邏輯:manifest + show.json → RSS XML 字串(離線可測)
    layout.py       檔案佈局、registry/token、原子寫、複製 mp3
    rss_models.py   Channel / Item 資料類別
  tools_publish.py  薄 MCP 包,接成工具(對齊 tools_basic/tools_podcast 風格)
```

掛進**同一個 notebooklm MCP**(共用 lifespan),不另起 server。

## MCP 工具介面(刻意最小)

```text
publish_series(
  notebook_id,                  # 必填:對應主題;決定/復用 token
  manifest_path = None,         # 預設讀該 notebook output_dir 的 series_manifest.json
  show_title = None,            # 預設帶 notebook 標題
  show_description = "",
  author = None,                # 首次設定存 show.json,之後沿用
  artwork_path = None,          # 選填封面
  category = "Technology",
  explicit = False,
) -> { feed_url, token, episode_count, episodes:[{n,title,url}] }

feed_list() -> [{ notebook_id, show_title, token, feed_url, episode_count }]

feed_info(notebook_id|token) -> 該節目完整狀態(含各集 guid/pubDate)
```

## 發布流程(`publish_series` 內部,純程式碼、無 LLM)

```
1. 讀 registry → 取得/新建此 notebook 的 token(URL 穩定)
2. 讀 series_manifest.json → EP 清單(episode/title/mp3_path)
3. 每集:複製 mp3_path → feeds/<token>/EP{n:02d}.mp3,讀 byte 數
4. 讀回 show.json → 沿用既有 pubDate/guid;新集才補時間戳
5. 產生 feed.xml(原子寫);更新 show.json、registry.json
6. 回傳 feed_url 等
```

冪等:同系列重發 → 同 URL、同 guid、同舊集日期;只有新集帶新日期。重生壞集
(reject-then-delete)→ 換檔內容,集號/URL 不變。

## 設定(走 Doppler,對齊現有 MCP)

| 變數 | 用途 | 範例 |
|---|---|---|
| `PODCAST_PUBLIC_BASE_URL` | 組公開 URL 的根 | `https://podcast.example` |
| `PODCAST_FEEDS_ROOT` | NAS 輸出目錄(MCP 所在機掛載得到) | `/nas/podcasts` |

兩者由既有 `doppler run -p notebooklm -c dev` 注入,不新增認證機制。

## 錯誤處理(fail-fast,沿用 `_status.py` 精神)

- manifest 缺集、`mp3_path` 不存在 / 0 byte → 直接報錯,不產半套 feed。
- `PODCAST_FEEDS_ROOT` 不存在或不可寫 → 呼叫時即明確報錯。
- token 衝突(機率極低)→ 重生直到不撞 registry。
- feed.xml 一律 temp+rename,任何步驟失敗都不留壞 feed。
- `artwork_path` 給了但檔案不在 → 報錯(而非靜默略過)。

## 測試(全離線,對齊現有 51 passed 風格)

- `feed.py`:假 manifest + 暫存假 mp3 → 解析產出 XML,斷言:
  - channel / item 必要欄位齊全、`language=zh-Hant`
  - `enclosure length` = 實際 byte 數、`type=audio/mpeg`
  - **冪等性**:同輸入跑兩次 → guid / pubDate / URL 完全一致
  - 重生某集(換 mp3 內容)→ 其他集 pubDate 不變
- `layout.py`:token 復用(同 notebook 兩次 → 同 token)、原子寫、registry 合併。
- 不碰網路、不碰真 NAS、不碰 NotebookLM。

## 不做(YAGNI)

- 不自寫 HTTP 服務 / 認證層(靠 reverse proxy 靜態服務)。
- 不做多人帳號、web 後台、自動資料夾掃描。
- 不在發布迴圈內放任何 LLM 判斷。
- 不用 `brief` 當 episode 摘要(它是生成指令,不是聽眾文案)。

## 待後續決定(不阻擋本設計)

- 節目「內容主題」本身(使用者仍在構思)— 不影響發布管線形狀。
- reverse proxy 那條路由的實際設定(Caddy / nginx / 群暉)— 部署時填。
- 是否要做 episode 級 `<description>` 覆寫的輸入形狀 — 先用預設(= title)。
