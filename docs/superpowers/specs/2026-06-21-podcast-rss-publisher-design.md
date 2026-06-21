# Podcast RSS Publisher — 設計文件

- 日期:2026-06-21
- 狀態:設計定稿(已納入 Codex 第一輪對抗審查),待寫實作計畫
- 所在 repo:`notebooklm-skill`
- 審查紀錄:Codex 找出 2 blocker + 6 high,本版已逐項處理(見文末「審查回應對照」)

## 目的

在既有的 NotebookLM 薄 MCP 之上,**疊加一層「發布」能力**:把 `podcast_series`
產出的整季音檔轉成 podcast RSS feed,放到 NAS 對外目錄,由既有 reverse proxy 以
HTTPS 靜態服務,讓手機 Apple Podcast 用 feed URL 訂閱、自動收新集。

音檔生成(NotebookLM)已完整可用且不在本設計範圍。本設計只處理**發布管線**。

## 心智模型:一主題 = 一節目 = 一 feed

```
主題 A (show_id="ai-news") → feed_A → EP01, EP02, EP03
主題 B (show_id="rust-deep") → feed_B → EP01, EP02
```

一個「主題」用 `podcast_series` 跑出多集,那份 `series_manifest.json` 是該節目的集數
清單。發布層吃這份 manifest,轉成該主題專屬的一個 feed。**天生多 feed,一主題一 feed。**

### feed identity = 穩定的 `show_id`(不是 notebook_id)

feed 的身分由使用者指定的穩定 slug `show_id` 決定,**不綁 `notebook_id`**。
理由:同一主題若日後在 NotebookLM 重建 notebook,`notebook_id` 會變,但訂閱者的
feed URL 不能變。`notebook_id` 只是「目前的內容來源」,記在 show.json 供追溯。

## 已鎖定決策

| 項目 | 決定 |
|---|---|
| 部署拓樸 | agent / MCP 與 NAS 同一 LAN;MCP 所在機可掛載 NAS 檔案系統 |
| 隱私 | 隨機 token URL:`/feeds/<token>/feed.xml`(不公開列出、猜不到、可直接訂閱) |
| NAS 能力 | 能跑 Docker、已有公網域名 + HTTPS + reverse proxy |
| 架構 | 靜態 feed 產生器,不自寫 HTTP 服務 |
| 發布粒度 | 一個指令整包發布整個系列(manifest → feed) |
| feed 來源 | 讀 `series_manifest.json` **檔案**(整季)+ 各集 mp3 |
| feed identity | 穩定 `show_id`;token 由它決定性產生 |

## Token 機制(決定性,無 registry 競態)

```
token = base32_lower( HMAC_SHA256( $PODCAST_TOKEN_SALT, show_id )[:15 bytes] )  → 24 字元 [a-z2-7]
```

- **決定性**:同 `show_id` 永遠得同 token → feed URL 永久穩定,**不需要 registry.json**,
  因此沒有「多 VM 同時 read-modify-write 全域檔導致 lost update」的 blocker。
- **不可猜**:salt 是 Doppler 秘密;沒有 salt 無法從 show_id 反推 token。
- **字元安全**:base32 小寫只含 `[a-z2-7]`,無 `-`/`_`/`+`/`/`,避開 Apple 對 URL/檔名的保守限制。
- `feed_list` 改為**掃描** `feeds/*/show.json`(個人規模數量小),不依賴全域索引。

## 檔案佈局(NAS 對外目錄)

```
$PODCAST_FEEDS_ROOT/                 # 例 /nas/podcasts
  feeds/
    <token>/                         # 一個主題/節目
      feed.xml                       # 衍生輸出,原子重建
      index.html                     # 極簡節目頁(供 RSS <link> 指向,避免指到不存在 URL)
      show.json                      # 節目級 metadata + 各集 state(guid/pubDate/檔名/tombstone)
      artwork.png|artwork.jpg        # 節目封面(必填;副檔名依實際格式 PNG/JPEG)
      EP01-<hash8>.mp3               # 內容版本化檔名(immutable)
      EP02-<hash8>.mp3
```

無全域 `registry.json`。每個 show 自有 `show.json`,各自原子寫,互不競態。

## 穩定性鐵律(已納入 Codex 發現)

1. **媒體檔內容版本化、immutable**:對外檔名 `EP{n:02d}-<hash8>.mp3`,`hash8` = 該 mp3
   內容 sha256 前 8 hex。重生壞集 → 內容變 → hash 變 → **新檔名 → 新 enclosure URL**,
   client/CDN 必定重抓;**GUID 仍穩定**(見下)所以 app 視為「同一集更新」而非新集。
   舊版檔保留(不刪,避免已 cache 的 client 404)。
2. **GUID 穩定且與來源解耦**:`guid = sha1(show_id + ":" + episode_n)`,
   `isPermaLink="false"`。集號不重編、不重用 → app 不會把更新誤判成新集、也不會順序錯亂。
3. **pubDate 跨重建穩定**:`show.json` 記每集「首次發布時間戳」;重建讀回舊值,只有新集補
   新時間。輸出 RFC-2822 含時區(`+0800`)。
4. **所有寫入原子化 + 提交順序**:媒體檔、`show.json`、`feed.xml`、`index.html` 一律
   「寫 target dir 內 temp → fsync → `os.replace()`」。提交順序固定:
   **先寫好所有媒體檔 → 寫 show.json(權威 state)→ 由已提交 state 產生 feed.xml/index.html**。
   任何步驟 crash 都不留半套;靜態伺服器永不讀到寫一半的檔。
5. **刪集語意明確**:刪集 = 從 feed 移除該 item + **保留 mp3 檔(不 404 已 cache client)**
   + 在 show.json 記 tombstone(集號永久作廢、不重用)。可選對該 item 出 `<itunes:block>`。

## RSS 內容(Apple 合規,必填欄位不可空)

### Channel(節目級,來自 `show.json`)

| 欄位 | 規則 |
|---|---|
| `<title>` | **必填非空**(預設帶 notebook 標題,可指定) |
| `<description>` / `<itunes:summary>` | **必填非空**(Apple validation 會擋空描述) |
| `<language>` | `zh-Hant`(部署時以 Apple validator 實測確認;備案 `zh-TW`) |
| `<itunes:author>` | **必填** |
| `<itunes:owner>` (`itunes:name` + `itunes:email`) | **必填**(Apple 需要 owner email) |
| `<itunes:image href>` | **必填**;artwork 需通過規格驗證(見下) |
| `<itunes:category>` | 預設 `Technology`(可改) |
| `<itunes:explicit>` | 輸出**小寫** `true`/`false`(預設 `false`) |
| `<link>` | 指向同目錄 `index.html` 的公開 URL(真實存在) |
| `<atom:link rel="self" href type="application/rss+xml">` | feed 自身 URL |
| `<lastBuildDate>` | 取最後一集 live 的 `pubDate`(穩定值,保持 feed 跨重建 byte 一致;不用 wall clock) |

XML root 宣告 namespaces:`itunes`、`content`、`atom`。所有文字欄位做 XML escaping。

### Artwork 規格驗證(Apple Show Cover)

`artwork_path` **必填**。發布前驗證:正方形、邊長 **1400–3000 px**、PNG 或 JPG、
RGB 色彩空間、**無 alpha 通道**。不符直接報錯(不靜默略過)。輸出依實際格式複製為
`artwork.png`(PNG)或 `artwork.jpg`(JPEG),檔名記入 `show.json` 的 `artwork_file`,
feed 的 `<itunes:image>` 一律引用該值,避免 JPEG bytes 配 `.png` 副檔名。

### Item(每集,來自 manifest + show.json 穩定欄位)

| 欄位 | 來源 |
|---|---|
| `<title>` | manifest 的 `title`(中文,如「心法篇」),XML escaped |
| `<description>` | 預設 = `title`;支援 `episode_overrides[n].description` 覆寫(**不用 brief**) |
| `<pubDate>` | show.json 記錄的首次發布時間(RFC-2822 + `+0800`) |
| `<guid isPermaLink="false">` | `sha1(show_id:episode_n)`,穩定 |
| `<enclosure url length type>` | `EP{n}-<hash8>.mp3` 公開 URL · **檔案 byte 數** · `audio/mpeg` |
| `<itunes:duration>` | 選填(能算到才填) |

### 公開 URL 組法

由 `$PODCAST_PUBLIC_BASE_URL`(例 `https://podcast.example`)拼:
- feed:`{BASE}/feeds/<token>/feed.xml`
- 音檔:`{BASE}/feeds/<token>/EP01-<hash8>.mp3`
- 節目頁:`{BASE}/feeds/<token>/index.html`

## 模組切分(設計隔離)

```
notebooklm_mcp/
  publish/
    identity.py     show_id 驗證、token 決定性產生(HMAC→base32)
    feed.py         純邏輯:show.json + episodes → RSS XML 字串 + index.html(離線可測)
    artwork.py      artwork 規格驗證(尺寸/格式/色彩/alpha)
    layout.py       檔案佈局、內容 hash、原子寫(temp+fsync+replace)、提交順序
    state.py        show.json 讀寫(權威 state:guid/pubDate/檔名/tombstone)
    rss_models.py   Channel / Item 資料類別
  tools_publish.py  薄 MCP 包,接成工具(對齊 tools_basic/tools_podcast 風格)
```

掛進**同一個 notebooklm MCP**(共用 lifespan),不另起 server。

## MCP 工具介面

```text
publish_series(
  show_id,                      # 必填:穩定 slug,feed identity;決定 token
  notebook_id,                  # 必填:目前內容來源(記入 show.json 供追溯)
  manifest_path,                # 必填:series_manifest.json 路徑(無法從 notebook_id 推 output_dir)
  show_title,                   # 必填非空:節目標題(channel <title>;manifest 無 title 可帶)
  show_description,             # 必填非空
  author,                       # 必填
  owner_name, owner_email,      # 必填(Apple)
  artwork_path,                 # 必填,過規格驗證
  category = "Technology",
  explicit = False,
  episode_overrides = None,     # {n: {description?, ...}} 選填
) -> { feed_url, show_page_url, token, episode_count, episodes:[{n,title,url,guid}] }

feed_list() -> [{ show_id, show_title, token, feed_url, episode_count }]   # 掃描 feeds/*/show.json
feed_info(show_id|token) -> 該節目完整 state(含各集 guid/pubDate/檔名/tombstone)
```

## 發布流程(`publish_series` 內部,純程式碼、無 LLM)

```
1. 驗證 show_id;token = HMAC(salt, show_id) → base32(決定性,無 registry)
2. 讀 series_manifest.json 檔案(整季,非 podcast_series 的 run 回傳)→ EP 清單
3. 驗證 artwork_path 規格;依格式複製為 artwork.png/artwork.jpg(原子)
4. 每集:
   a. stat manifest 的 mp3_path;若不存在 → 用 artifact_id 重新 download_audio 到 staging;
      仍失敗 → fail-fast 明確報錯(指出哪一集、原路徑、artifact_id)
   b. 算內容 sha256 → hash8 → 對外檔名 EP{n:02d}-<hash8>.mp3
   c. 若該檔名已存在(內容相同)→ 跳過;否則 temp+fsync+os.replace 寫入
5. 讀回 show.json:沿用既有 guid/pubDate;新集補時間戳;處理 tombstone
6. 原子寫 show.json(權威 state)
7. 由已提交的 show.json 產生 feed.xml + index.html(各自 temp+fsync+replace)
8. 回傳 feed_url 等
```

冪等:同系列重發 → 同 token/URL、同 guid、同舊集 pubDate;新集帶新時間。
重生壞集 → 內容 hash 變 → 新 enclosure URL(client 重抓)+ 同 guid(視為更新)。

## 設定(走 Doppler,對齊現有 MCP)

| 變數 | 用途 | 範例 |
|---|---|---|
| `PODCAST_PUBLIC_BASE_URL` | 組公開 URL 的根 | `https://podcast.example` |
| `PODCAST_FEEDS_ROOT` | NAS 輸出目錄(MCP 所在機掛載得到) | `/nas/podcasts` |
| `PODCAST_TOKEN_SALT` | token HMAC 的秘密 salt | (Doppler 秘密) |

三者由既有 `doppler run -p notebooklm -c dev` 注入,不新增認證機制。

## 錯誤處理(fail-fast,沿用 `_status.py` 精神)

- 必填 metadata(title/description/author/owner_email/artwork)缺或空 → 報錯。
- artwork 不符 Apple 規格 → 報錯(列出實際尺寸/格式/問題)。
- manifest 缺集、mp3_path 不存在且 artifact 重抓失敗 → 報錯(指出哪一集)。
- `PODCAST_FEEDS_ROOT`/`PODCAST_TOKEN_SALT` 缺 → 呼叫時即明確報錯。
- 任何寫入失敗都留在 temp、不污染已提交 state/feed。
- show_id 與既有 show.json 的 notebook_id 不同 → 視為「換來源」,更新記錄但保留 token/feed。

## 測試(全離線,對齊現有 51 passed 風格)

- `identity.py`:同 show_id → 同 token(決定性);不同 show_id → 不同;字元集 `[a-z2-7]`。
- `artwork.py`:合格圖過、非方形/過小/過大/帶 alpha/錯格式各自報錯(用 Pillow 造小圖)。
- `feed.py`:假 show.json + episodes → 解析 XML,斷言:
  - channel/item 必填欄位齊全、namespaces 正確、`atom:link rel=self` 存在、`explicit` 小寫
  - `enclosure length` = 實際 byte 數、`type=audio/mpeg`、`pubDate` RFC-2822 帶 `+0800`
  - **冪等性**:同輸入兩次 → guid/pubDate/URL 完全一致
  - 重生某集(換 mp3 內容)→ enclosure 檔名變、guid 不變、其他集 pubDate 不變
  - 刪集 → item 消失、舊檔仍在、集號 tombstone、不重用
  - XML escaping:標題含 `&`/`<` 正確跳脫
- `layout.py`/`state.py`:內容 hash、原子寫、提交順序、show.json round-trip。
- 不碰網路、不碰真 NAS、不碰 NotebookLM。

## 部署驗收(非程式碼,部署時跑)

- `curl -I` feed 與 mp3:200、正確 MIME(`application/rss+xml`、`audio/mpeg`)。
- `curl -r 0-1`:mp3 回 206 Partial Content(拖曳/續播)。
- Cache-Control:`feed.xml` 短 cache / `no-cache`;`*.mp3` immutable 長 cache。
- HTTPS 憑證鏈完整、公網可達。
- 用 Apple「Validate your podcast RSS feed」實測整份 feed。

## 不做(YAGNI)

- 不自寫 HTTP 服務 / 認證層(靠 reverse proxy 靜態服務)。
- 不做多人帳號、web 後台、自動資料夾掃描。
- 不在發布迴圈內放任何 LLM 判斷。
- 不用 `brief` 當 episode 摘要(它是生成指令,不是聽眾文案)。

## 待後續決定 / 操作備忘(不阻擋本設計)

- 節目「內容主題」本身(使用者仍在構思)— 不影響發布管線形狀。
- reverse proxy 路由實際設定(Caddy / nginx / 群暉)— 部署時填,跑「部署驗收」。
- **Feed 搬遷**:若日後必須換 feed URL,用 HTTP 301 + 舊 feed 內 `<itunes:new-feed-url>`
  指向新 URL(Apple 規範);本設計的決定性 token 已大幅降低搬遷需求。
- 舊版媒體檔的清理策略(目前保留,避免 404)。

## 審查回應對照(Codex 第一輪)

| Codex 發現 | 嚴重度 | 本版處理 |
|---|---|---|
| Apple 必填 metadata 被設成可空 | blocker | description/author/owner_email/artwork 全改必填;explicit 小寫;artwork 規格驗證 |
| registry.json 多 VM RMW lost update | blocker | 改決定性 token(HMAC+show_id),**移除 registry**;feed_list 改掃描 |
| 重生壞集同 URL,client 不重抓 | high | 內容版本化檔名 `EP{n}-<hash8>.mp3`,GUID 仍穩定 |
| mp3 複製非 atomic | high | temp+fsync+os.replace;內容 hash 檔名等同 immutable |
| 刪集 / 集號重用無語意 | high | 禁重編號、tombstone、保留舊檔、可選 itunes:block |
| manifest mp3_path 非長期保證 | high | 發布前 stat;失敗用 artifact_id 重抓;再失敗 fail-fast |
| feed identity 綁 notebook_id 矛盾 | high | 改用穩定 show_id 作 identity;notebook_id 僅記來源 |
| 狀態檔 atomic + 提交順序未定義 | medium | 全狀態檔 temp+fsync+replace;固定提交順序 |
| 靜態服務假設未驗證 | medium | 加「部署驗收」清單(curl -I / range / MIME / cache / TLS) |
| token charset 未規範 | medium | base32 `[a-z2-7]`,避開特殊字元 |
| `<link>`/namespace/self-link 未落地 | medium | 產生 index.html 供 link;root 加 namespaces + atom:link self |
| episode description 用 title | OK | 維持,並加 episode_overrides 覆寫 |
| XML escaping / 時區 / feed 搬遷 | 補強 | XML escaping、RFC-2822 +0800、301+new-feed-url 備忘 |
