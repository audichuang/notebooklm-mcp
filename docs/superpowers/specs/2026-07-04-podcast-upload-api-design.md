# Spec:podcast 發布改為讀寫分離(內網 uploader + 唯讀 Caddy)

## 1. 目的與背景

`publish_series` 目前(git 現況 `notebooklm_mcp/tools_publish.py`)直接把整季檔案寫進掛載的 `$PODCAST_FEEDS_ROOT/feeds/<token>/`,前提是「MCP 所在機掛得到 NAS 且可寫」。`podcast-feed-host`(Caddy)再唯讀掛同一目錄、經使用者既有的 Cloudflare Tunnel 對外服務。耦合點是**共用可寫檔案系統**。

本次改動把寫入路徑從「檔案系統掛載」換成「**內網 HTTP PUT 到 NAS 上一支 token 保護的 uploader**」,做到讀寫分離:

- **MCP 端**不再需要任何 NAS 掛載,也不再在本機落任何持久檔;它在記憶體裡組好整季,逐檔 PUT 到 uploader。
- **NAS 端**同一份 `feeds/` 目錄由**兩個容器**服務:`caddy`(唯讀、對外、走既有 tunnel)+ `uploader`(可寫、**僅內網**、不進 tunnel)。原子落地(temp→fsync→replace)整個下放到 uploader。

**關鍵事實(定調全篇):MCP 機器與 NAS 都在同一內網,MCP 用內網 IP 直接 HTTP 連到 NAS。** 因此:

- 寫埠(uploader)**只綁內網、不進 Cloudflare Tunnel**。`PODCAST_UPLOAD_URL = http://<NAS內網IP>:<寫port>`(例 `http://192.0.2.10:8086`)。
- 只有讀埠(caddy)經既有 tunnel 對外(唯讀,`podcast.域名`)。**沒有第二條 tunnel ingress、沒有 `podcast-upload.域名`。**
- Bearer upload token 仍保留,但定位是**縱深防禦**(內網其他機器/某台被入侵時的第二道),不是唯一防線 —— 第一道是「寫埠只在內網、不對公網曝露」。
- 因寫埠不經 Cloudflare,原本「CF proxy body ~100MB 上限」的顧慮**不存在**;`MAX_UPLOAD_BYTES` 依 NAS 訂為 500MB。
- **未來彈性**:若 MCP 哪天搬到雲端,只要在既有 tunnel 加第二條 ingress 曝露寫埠即可(token 機制已就位);屆時 token 升為唯一防線,需再補 IP allowlist / Cloudflare Access。

使用模式仍是:**單人自用 / 一次發整季 / 事後不補集**。token 推導(`identity.make_token` = HMAC-SHA256(salt, show_id) 前 15 bytes → base32 lower,24 字元 `[a-z2-7]`)不變。

## 2. 使用模式與範圍(砍掉什麼、為何)

錨定「自用 / 一次整季 / 不補集」,以下項目全案移除:

| 砍掉 | 位置 | 理由 |
|---|---|---|
| `PODCAST_FEEDS_ROOT` 掛載寫入 | `tools_publish.py` | 改為內網 HTTP PUT;MCP 不再需要 NAS 掛載 |
| `publish/state.py` 整檔(`load_show`/`save_show`) | `publish/state.py` | MCP 不再讀寫舊 `show.json`;`episode_guid` 搬進 `identity.py` |
| 讀回舊 state 合併(prior_eps、`prev.get("guid")`/`prev.get("pub_date")`) | `publish_series` | 無掛載可讀;guid 決定性、pubDate 改由 manifest 承載(§7) |
| `layout.py` 的 `atomic_write_bytes/text`、`atomic_copy` + fsync/replace | `publish/layout.py` | 原子寫下放給 uploader;MCP 不落持久檔 |
| tombstone 全套(`tombstone` 欄位、復活防護、縮季保留舊檔、`live_episodes` 的過濾分支) | `rss_models.py`、`tools_publish.py` | 一次整季 + 不補集 → 沒有「manifest 少一集」的增量情境要防 |
| `feed_list` 整支工具 | `tools_publish.py` | 無掛載可掃;token 單向 HMAC 無法反列舉 show_id |
| `episode_overrides` 參數 | `publish_series` 簽章 | 自用 YAGNI;description 直接取 `ep["title"]`(manifest 無 brief) |
| `_now_rfc2822()`「共用 now」fallback | `tools_publish.py` | 自相矛盾(製造同季 tie + 每次執行變動破壞冪等);改決定性 per-episode fallback(§7) |
| MCP 端本機 scratch 目錄 + `finally` 清理 | `publish_series` | mp3 直接從原路徑讀 bytes PUT;feed.xml/index.html/show.json 記憶體 bytes 直接 PUT;artwork 從原路徑讀 |

**保留且不動**:`identity.make_token`/`validate_show_id`、`feed.py`(`build_feed_xml`/`build_index_html`,其讀的 key 是硬契約,見 §7)、`artwork.validate_artwork`、`layout.content_hash8`/`media_filename`、`_ensure_local_mp3`。

## 3. 架構(ASCII)

```
內網 LAN(MCP 與 NAS 同網段)
┌─────────────────────┐                              ┌─────────────────────────────────────────┐
│ MCP host(任一台)   │  PUT /feeds/<t>/<file>       │ NAS: podcast-feed-host(兩容器,共用目錄)│
│  publish_series     │  Authorization: Bearer …     │  ┌───────────────────────────────────┐  │
│                     │──── http,LAN 直連 ─────────▶│  │ uploader  :8086  (rw)   僅內網       │  │
│                     │  http://<NAS內網IP>:8086     │  │   → $FEEDS_ROOT_HOST 原子寫入        │  │
│                     │  (先 GET /healthz 探 token)  │  └───────────────────────────────────┘  │
│                     │                              │              ▲ 同一目錄 ▼               │
│                     │                              │  ┌───────────────────────────────────┐  │
│                     │                              │  │ caddy     :8080  (ro)   對外         │  │
│                     │                              │  │   → $FEEDS_ROOT_HOST 唯讀靜態服務    │  │
└─────────────────────┘                              │  └───────────────────────────────────┘  │
        ▲ 回傳 feed_url 給使用者                      └──────────────────▲──────────────────────┘
        │                                                                │ 既有 Cloudflare Tunnel(只這一條)
  公網 Apple Podcast ◀── https ── podcast.域名 ── CF Tunnel ────────────┘   GET /feeds/<t>/feed.xml、*.mp3
```

提交順序契約(client 保證):**媒體(mp3 + artwork)→ show.json → feed.xml/index.html**。uploader 每檔原子落地且**永不刪檔**。

## 4. 兩 repo 各改什麼

### 4.1 `notebooklm-skill`(MCP 端)

| 檔案 | 動作 |
|---|---|
| `notebooklm_mcp/tools_publish.py` | **整檔重寫**(§5 全文):HTTP PUT 模型、砍 state/tombstone/overrides/feed_list、`feed_info(show_id)` 純計算 |
| `notebooklm_mcp/publish/identity.py` | 檔尾**新增** `episode_guid`(從刪除的 state.py 搬來) |
| `notebooklm_mcp/publish/layout.py` | **砍到只剩** `content_hash8` + `media_filename`(移除三個 atomic 函式) |
| `notebooklm_mcp/publish/rss_models.py` | `live_episodes` **簡化**(移除 tombstone 過濾) |
| `notebooklm_mcp/publish/state.py` | **刪整檔** |
| `notebooklm_mcp/publish/__init__.py` | docstring 的「pure logic lives in identity/layout/state/…」移除 `state`;措辭改「發布到 NAS 上的 uploader」 |
| `notebooklm_mcp/tools_podcast.py` | `_run_episode` return 加 `published_at`(companion change,§7) |
| `pyproject.toml` | `dependencies` 加 `"httpx>=0.27,<1"`(已是 `mcp[cli]`/`notebooklm-py` 的 transitive dep,零新安裝;僅宣告直接依賴) |
| `notebooklm_mcp/app.py` | **不動**(仍 `from . import tools_publish`;`feed_list` 整支刪除,app.py 因不具名引用故不受影響) |
| `SKILL.md` | **改**:刪工具表 `feed_list` 行;`feed_info` 描述改「純計算,回 show_id/token/feed_url/show_page_url,不含各集細節」、參數 `show_id_or_token`→`show_id`;發布段同步(四 secret、內網 PUT) |
| `AGENTS.md` / `README.md` | 更新發布段:三 secret → 四 secret、掛載寫入 → 內網 PUT、「重跑=重發整季非增量」 |

### 4.2 `podcast-feed-host`(NAS 端)

| 檔案 | 動作 |
|---|---|
| `uploader/server.py` | **新檔**:stdlib http.server,token 閘、path 白名單、原子寫、`GET /healthz` 帶 auth 探測(§6) |
| `uploader/Dockerfile` | **新檔**:`python:3.12-alpine` + COPY 一檔 |
| `uploader/test_server.py` | **新檔**:離線自檢(path 白名單 + 原子寫 roundtrip) |
| `docker-compose.yml` | **改**:兩 service(caddy ro + uploader rw),新增 `UPLOAD_PORT`/`UPLOAD_TOKEN` |
| `.env.example` | **改**:新增 `UPLOAD_PORT`、`UPLOAD_TOKEN` |
| `Caddyfile` | **改**:加 `respond /feeds/*/show.json 403`(show.json 含 owner_email/notebook_id,不對外) |
| `.github/workflows/build.yml` | **改**:matrix 兩 image(`podcast-feed-host` 讀 + `podcast-feed-uploader` 寫),multi-arch 保留 |
| `Caddy` 讀 image / 讀站 Caddyfile 其餘規則 | **不動**(mp3 immutable、feed.xml no-cache、Range 206 皆沿用) |
| `README.md` | 補:第二個 GHCR package 設 Public;`.env` 多填 `UPLOAD_TOKEN`/`UPLOAD_PORT`;寫埠**不加** tunnel ingress |

## 5. 元件與職責 + `notebooklm-skill` 定版程式碼

### 5.1 `publish/identity.py`(檔尾新增)

```python
def episode_guid(show_id: str, episode_n: int) -> str:
    """Stable per-episode GUID, decoupled from the content source: same
    (show_id, episode_n) -> same GUID forever, so regenerating an episode reads
    as an update, not a new item. (Moved here from the deleted state.py.)"""
    validate_show_id(show_id)
    return hashlib.sha1(f"{show_id}:{episode_n}".encode("utf-8")).hexdigest()
```

(`hashlib` 已 import。)

### 5.2 `publish/layout.py`(砍到剩兩純函式)

```python
"""Content-addressed media naming. Atomic writes moved to the feed host's
uploader: the MCP renders in memory and PUTs, so it never writes durable files."""
from __future__ import annotations

import hashlib

_CHUNK = 1 << 20


def content_hash8(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()[:8]


def media_filename(episode_n: int, hash8: str) -> str:
    return f"EP{episode_n:02d}-{hash8}.mp3"
```

### 5.3 `publish/rss_models.py`(移除 tombstone)

```python
"""Episodes in episode-number order (tombstone removed project-wide)."""
from __future__ import annotations


def live_episodes(show: dict) -> list[tuple[int, dict]]:
    eps = [(int(k), ep) for k, ep in show.get("episodes", {}).items()]
    eps.sort(key=lambda pair: pair[0])
    return eps
```

`feed.py` 完全不動(它只呼叫 `live_episodes` 並讀每集固定 key;show dict 不再帶 `tombstone`,自然無效)。

### 5.4 `tools_podcast.py`(companion change:寫 `published_at`)

`_run_episode` return dict(現為 6 欄)加第 7 欄。時間戳在**首次生成當下**寫入 manifest,manifest 是這一季的持久權威紀錄(使用者自留、resume 也讀回):

```python
# 檔頭補 import
from datetime import datetime, timezone, timedelta
from email.utils import format_datetime
_TZ = timezone(timedelta(hours=8))

# _run_episode 的 return(取代現有)
return {
    "episode": episode_n,
    "title": title.strip(),
    "label": label,
    "task_id": status.task_id,
    "artifact_id": artifact_id,
    "mp3_path": mp3_path,
    "published_at": format_datetime(datetime.now(_TZ)),  # 首次生成時間 → 進 manifest
}
```

`_write_manifest` 原封存整個 ep dict,`published_at` 自然帶進 `series_manifest.json`。重生某集會覆寫成新時間戳(重生=換內容,可接受;見 §13 T2)。

### 5.5 `tools_publish.py`(整檔重寫)

```python
"""Podcast publishing MCP tool. publish_series renders the whole season in memory
and PUTs each file to the feed host's LAN-only uploader (token-gated). The MCP
host writes nothing durable and needs no NAS mount — it reaches the NAS over the
internal network (PODCAST_UPLOAD_URL = http://<nas-lan-ip>:<port>).

Commit order (the uploader is a dumb landing zone, so the CLIENT enforces order):
media (mp3 + artwork) -> show.json -> feed.xml/index.html. Combined with "the
uploader never deletes", a reader on the public read port never sees a feed.xml
that points at a missing enclosure."""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx

from . import runtime
from .app import mcp
from .publish import artwork as artwork_mod
from .publish import feed as feed_mod
from .publish import identity
from .publish.layout import media_filename

_TZ = timezone(timedelta(hours=8))          # Asia/Taipei, RFC-2822 +0800
_TIMEOUT = 600.0                            # a season of mp3 PUTs can take a while
# Deterministic fallback pubDate for legacy manifests without published_at:
# a fixed base PLUS (n-1) days -> distinct per episode, higher episode = later
# date (SAME direction as the published_at path: EP01 oldest, EPn newest), and
# byte-stable across republish (never a wall clock, so two publishes render an
# identical feed.xml). base 2020-01-01 + 98 days is still 2020 -> always past.
_FALLBACK_BASE = datetime(2020, 1, 1, 9, 0, 0, tzinfo=_TZ)


def _require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise ValueError(f"{name} is required (inject via Doppler) but is unset/empty")
    return val


def _require(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required and must be non-empty")
    return value


def _fallback_pub_date(n: int) -> str:
    return format_datetime(_FALLBACK_BASE + timedelta(days=n - 1))


def _make_client() -> httpx.AsyncClient:
    """One seam: tests monkeypatch this to inject an httpx.MockTransport."""
    return httpx.AsyncClient(timeout=_TIMEOUT)


async def _ensure_local_mp3(notebook_id: str, ep: dict) -> str:
    """Local path to the episode mp3. If the manifest's mp3_path is gone
    (output_dir cleaned), re-download via artifact_id; else fail-fast."""
    path = ep.get("mp3_path")
    if path and os.path.exists(path) and os.path.getsize(path) > 0:
        return path
    artifact_id = ep.get("artifact_id")
    if not artifact_id:
        raise ValueError(
            f"episode {ep.get('episode')}: mp3_path missing and no artifact_id to re-download"
        )
    staging = path or os.path.join(
        os.environ.get("TMPDIR", "/tmp"), f"ep{ep['episode']:02d}.mp3"
    )
    os.makedirs(os.path.dirname(staging), exist_ok=True)
    client = runtime.get_client()
    await client.artifacts.download_audio(notebook_id, staging, artifact_id)
    if not (os.path.exists(staging) and os.path.getsize(staging) > 0):
        raise ValueError(
            f"episode {ep.get('episode')}: re-download via artifact {artifact_id} produced no file"
        )
    return staging


async def _auth_precheck(client, base: str, upload_token: str) -> None:
    """Probe GET /healthz WITH the bearer before sending any big mp3, so a wrong
    token fails fast (clean 401) instead of surfacing as a mid-PUT connection reset."""
    r = await client.get(f"{base}/healthz",
                         headers={"Authorization": f"Bearer {upload_token}"})
    if r.status_code != 200:
        raise ValueError(
            f"uploader auth precheck failed ({r.status_code}): check that Doppler "
            "PODCAST_UPLOAD_TOKEN matches the NAS .env UPLOAD_TOKEN"
        )


async def _put(client, base: str, token: str, upload_token: str, name: str, data: bytes) -> None:
    url = f"{base}/feeds/{token}/{name}"
    r = await client.put(url, content=data,
                         headers={"Authorization": f"Bearer {upload_token}"})
    if r.status_code != 201:
        raise ValueError(f"upload failed: PUT {name} -> {r.status_code} {r.text[:300]}")


@mcp.tool()
async def publish_series(
    show_id: str,
    notebook_id: str,
    manifest_path: str,
    show_title: str,
    show_description: str,
    author: str,
    owner_name: str,
    owner_email: str,
    artwork_path: str,
    category: str = "Technology",
    explicit: bool = False,
) -> dict:
    """Publish a whole podcast series (one topic = one feed) as a static RSS feed.

    Reads series_manifest.json, content-hashes each episode mp3, renders an
    Apple-compliant feed.xml + index.html in memory, and PUTs the season to the
    feed host's uploader. Deterministic: same show_id -> same URL/token; manifest
    published_at -> stable pubDates; mp3 content -> stable enclosure URL. The
    uploader lands each file atomically and never deletes, so a regenerated
    episode gets a NEW immutable mp3 URL while old cached URLs keep working."""
    base_url = _require_env("PODCAST_PUBLIC_BASE_URL")
    salt = _require_env("PODCAST_TOKEN_SALT")
    upload_url = _require_env("PODCAST_UPLOAD_URL").rstrip("/")
    upload_token = _require_env("PODCAST_UPLOAD_TOKEN")

    identity.validate_show_id(show_id)
    _require(notebook_id, "notebook_id")
    _require(show_title, "show_title")
    _require(show_description, "show_description")
    _require(author, "author")
    _require(owner_name, "owner_name")
    _require(owner_email, "owner_email")
    # Validate artwork up front (fail-fast before any upload). Extension follows
    # the real format so a JPEG is never served as .png.
    art_info = artwork_mod.validate_artwork(artwork_path)
    artwork_file = "artwork.jpg" if art_info["format"] == "JPEG" else "artwork.png"

    token = identity.make_token(show_id, salt)

    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)
    manifest_eps = manifest.get("episodes", [])
    if not manifest_eps:
        raise ValueError(f"manifest has no episodes: {manifest_path}")

    # One mp3 in RAM at a time: read -> hash -> PUT -> drop. NEVER accumulate the
    # whole season (8-12 episodes x tens of MB = 300-600MB resident on a possibly
    # small VM). Commit order still holds: every mp3 + artwork is PUT inside this
    # loop, and show.json/feed.xml are rendered and PUT only AFTER it.
    new_eps: dict[str, dict] = {}
    published = []
    async with _make_client() as client:
        await _auth_precheck(client, upload_url, upload_token)

        for ep in manifest_eps:                                    # 1) media: mp3
            n = int(ep["episode"])
            local = await _ensure_local_mp3(notebook_id, ep)
            with open(local, "rb") as f:
                mp3_bytes = f.read()
            hash8 = hashlib.sha256(mp3_bytes).hexdigest()[:8]      # hash bytes we already read
            mfile = media_filename(n, hash8)
            await _put(client, upload_url, token, upload_token, mfile, mp3_bytes)
            new_eps[str(n)] = {
                "title": ep["title"],
                "description": ep["title"],       # manifest carries no separate brief
                "guid": identity.episode_guid(show_id, n),
                "pub_date": ep.get("published_at") or _fallback_pub_date(n),
                "media_file": mfile,
                "length": len(mp3_bytes),
            }
            published.append({
                "n": n, "title": ep["title"], "guid": new_eps[str(n)]["guid"],
                "url": f"{base_url.rstrip('/')}/feeds/{token}/{mfile}",
            })
            del mp3_bytes                                          # free before next episode

        with open(artwork_path, "rb") as f:                        # 1) media: artwork
            await _put(client, upload_url, token, upload_token, artwork_file, f.read())

        show = {
            "show_id": show_id, "token": token, "notebook_id": notebook_id,
            "title": show_title.strip(), "description": show_description,
            "language": "zh-Hant", "author": author,
            "owner_name": owner_name, "owner_email": owner_email,
            "category": category, "explicit": bool(explicit),
            "artwork_file": artwork_file, "episodes": new_eps,
        }
        show_json = json.dumps(show, ensure_ascii=False, indent=2).encode("utf-8")
        await _put(client, upload_url, token, upload_token, "show.json", show_json)    # 2) state
        await _put(client, upload_url, token, upload_token,                            # 3) derived
                   "feed.xml", feed_mod.build_feed_xml(show, base_url).encode("utf-8"))
        await _put(client, upload_url, token, upload_token,
                   "index.html", feed_mod.build_index_html(show, base_url).encode("utf-8"))

    return {
        "feed_url": f"{base_url.rstrip('/')}/feeds/{token}/feed.xml",
        "show_page_url": f"{base_url.rstrip('/')}/feeds/{token}/index.html",
        "token": token,
        "episode_count": len(new_eps),
        "episodes": sorted(published, key=lambda e: e["n"]),
    }


@mcp.tool()
async def feed_info(show_id: str) -> dict:
    """Deterministic feed identity + public URLs for a show_id. Pure computation
    (token = HMAC(salt, show_id)); the MCP keeps no state, so per-episode detail
    is NOT returned — fetch feed.xml over the read port for that."""
    base_url = _require_env("PODCAST_PUBLIC_BASE_URL")
    salt = _require_env("PODCAST_TOKEN_SALT")
    token = identity.make_token(show_id, salt)     # also validates show_id
    base = f"{base_url.rstrip('/')}/feeds/{token}"
    return {
        "show_id": show_id,
        "token": token,
        "feed_url": f"{base}/feed.xml",
        "show_page_url": f"{base}/index.html",
    }
```

## 6. NAS uploader API 契約

### 6.1 端點總覽

| 方法/路徑 | 用途 | 認證 |
|---|---|---|
| `PUT /feeds/{token}/{name}` | 落地單一檔(原子) | `Authorization: Bearer <UPLOAD_TOKEN>` |
| `GET /healthz`(帶 `Authorization`) | token 探測(client 上傳前預檢) | 驗 token → 200/401 |
| `GET /healthz`(不帶) | 容器 liveness | 一律 200 |

- **path `{token}`**:目標目錄名,不是密鑰(salt 持有者可算、且出現在公開 URL)。進任何路徑前**必先**驗形狀 `^[a-z2-7]{24}$`,不符 → 404。這是防路徑穿越(`..`、`/`)的信任邊界。
- **path `{name}`**:嚴格白名單,只允許 `publish_series` 會送的檔名;不符 → 404,不落地。
  ```
  feed.xml | index.html | show.json | artwork.(png|jpg) | EP\d{2}-[0-9a-f]{8}\.mp3
  ```
  `EP\d{2}` = 兩位集號 → **單 feed ≤ 99 集**的 ceiling(自用足夠;要破限改 regex 為 `\d{2,3}` 並同步 client `media_filename`)。
- **header `X-`/Bearer token**:真正的寫入認證。`secrets.compare_digest`(常數時間)比對。缺/空/錯 → 401。

### 6.2 落地語意(每檔原子、永不刪)

- 每個 PUT:`temp(唯一名) → 逐 chunk 寫 → flush+fsync → os.replace → dir fsync`。temp 開在**目標目錄內**(同 FS,`os.replace` 才不會 `EXDEV`)。
- **只有 mp3 是 content-addressed immutable**(`EP{n}-{hash8}.mp3`);`artwork.png/jpg`、`show.json`、`feed.xml`、`index.html` 是**固定檔名、原地覆寫**,非 immutable。
- **uploader 永不刪任何檔**。重生某集 → 新 `EP{n}-<newhash>.mp3` 著陸、舊 hash 檔留著(孤兒)→ 已 cache 舊 feed.xml 的 client 仍抓得到舊 mp3(不 404)。
- **契約不變量**:reader 在任一瞬間讀到的 `feed.xml`(不論新舊),其引用的每個 enclosure mp3 **必定已存在**。成立於 (1) client 先送媒體再送 feed.xml,(2) 從不刪舊 mp3。

### 6.3 HTTP 狀態碼

| 碼 | 情境 |
|---|---|
| 201 | 檔已原子落地 |
| 401 | Bearer 缺/空/錯(在讀 body 前回) |
| 404 | token 不符 `[a-z2-7]{24}`;name 不在白名單;未知路徑 |
| 411 | 缺 `Content-Length` |
| 413 | `Content-Length` > `MAX_UPLOAD_BYTES` 或 < 0 |
| 500 | `ENOSPC`/fsync/replace 失敗(temp 清掉,目標維持舊值不變) |

### 6.4 `uploader/server.py`(新檔,定版)

```python
#!/usr/bin/env python3
"""Token-gated WRITE endpoint for the podcast feed directory. LAN-only: the MCP
hosts share the NAS's internal network and PUT here directly (this port is NOT in
the Cloudflare Tunnel — only Caddy's read port is). Caddy serves /srv read-only;
this PUTs into /srv.

Deliberately tiny: single user, one shared bearer token, stdlib only. Every write
is temp->fsync->os.replace so Caddy never serves a half-written file. Never deletes
-> content-addressed mp3 URLs stay valid forever (immutable, no 404 for cached
clients)."""
from __future__ import annotations

import os
import re
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.environ.get("FEEDS_ROOT", "/srv")
TOKEN = os.environ["UPLOAD_TOKEN"]                       # fail fast if unset
PORT = int(os.environ.get("PORT", "80"))
MAX_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(500 * 1024 * 1024)))  # 500 MB/file

# Path = /feeds/<token>/<name>. Both segments are a trust boundary — allowlist.
#   token: base32 lower of 15 bytes = exactly 24 chars of [a-z2-7]
#   name : exactly the set publish_series renders (EP\d{2} => <=99 episodes/feed)
_TOKEN = r"[a-z2-7]{24}"
_NAME = r"(?:feed\.xml|index\.html|show\.json|artwork\.(?:png|jpg)|EP\d{2}-[0-9a-f]{8}\.mp3)"
_PATH_RE = re.compile(rf"^/feeds/({_TOKEN})/({_NAME})$")
_CHUNK = 1 << 20


def atomic_write(dst: str, rfile, length: int) -> None:
    directory = os.path.dirname(dst)
    os.makedirs(directory, exist_ok=True)
    tmp = os.path.join(
        directory, f".tmp-{os.path.basename(dst)}-{os.getpid()}-{secrets.token_hex(8)}"
    )
    try:
        remaining = length
        with open(tmp, "wb") as f:
            while remaining > 0:
                chunk = rfile.read(min(_CHUNK, remaining))
                if not chunk:
                    raise IOError("short read from client")
                f.write(chunk)
                remaining -= len(chunk)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, dst)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    # Directory fsync makes the rename itself durable, but it's a nicety: once
    # os.replace returned, the write is committed. Some network FS reject dir
    # fsync -> best-effort, never turn a succeeded write into a 500.
    try:
        dfd = os.open(directory, os.O_DIRECTORY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _reply(self, code: int, body: bytes = b"") -> None:
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        if code >= 400:
            # Don't keep-alive a connection whose request body we may not have
            # drained — it would desync the next request on the socket.
            self.close_connection = True
            self.send_header("Connection", "close")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        if self.path != "/healthz":
            return self._reply(404)
        auth = self.headers.get("Authorization")
        if auth is None:
            return self._reply(200, b"ok\n")              # docker healthcheck
        ok = secrets.compare_digest(auth, f"Bearer {TOKEN}")  # client token probe
        return self._reply(200 if ok else 401)

    def do_PUT(self):
        if not secrets.compare_digest(
            self.headers.get("Authorization", ""), f"Bearer {TOKEN}"
        ):
            return self._reply(401)
        m = _PATH_RE.match(self.path)
        if not m:
            return self._reply(404)
        try:
            length = int(self.headers["Content-Length"])
        except (KeyError, TypeError, ValueError):
            return self._reply(411)
        if not 0 <= length <= MAX_BYTES:
            return self._reply(413)
        dst = os.path.join(ROOT, "feeds", m.group(1), m.group(2))
        try:
            atomic_write(dst, self.rfile, length)
        except Exception:
            return self._reply(500)
        self._reply(201)

    def log_message(self, fmt, *args):
        print(f"{self.command} {self.path} {fmt % args}", flush=True)


if __name__ == "__main__":
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()
```

### 6.5 `uploader/test_server.py`(新檔,離線自檢)

```python
"""Offline check for the two non-trivial bits: the path allowlist and the atomic
write roundtrip. `python3 uploader/test_server.py` — asserts, no deps."""
import io, os, tempfile
os.environ.setdefault("UPLOAD_TOKEN", "x")
import server  # noqa: E402

T = "abcdefghijklmnopqrstuvwx"  # 24 chars matching [a-z2-7]{24}

def test_path_allowlist():
    ok = [f"/feeds/{T}/feed.xml", f"/feeds/{T}/index.html", f"/feeds/{T}/show.json",
          f"/feeds/{T}/artwork.png", f"/feeds/{T}/artwork.jpg", f"/feeds/{T}/EP01-deadbeef.mp3"]
    bad = [f"/feeds/{T}/../../etc/passwd", f"/feeds/{T}/evil.sh", "/feeds/SHORT/feed.xml",
           f"/feeds/{T}/feed.xml/..", f"/{T}/feed.xml", f"/feeds/{T}/EP1-deadbeef.mp3"]
    assert all(server._PATH_RE.match(p) for p in ok)
    assert not any(server._PATH_RE.match(p) for p in bad)

def test_atomic_write_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        dst = os.path.join(d, "feeds", T, "feed.xml")
        payload = b"<rss/>" * 1000
        server.atomic_write(dst, io.BytesIO(payload), len(payload))
        assert open(dst, "rb").read() == payload
        assert not any(n.startswith(".tmp-") for n in os.listdir(os.path.dirname(dst)))

if __name__ == "__main__":
    test_path_allowlist(); test_atomic_write_roundtrip(); print("ok")
```

### 6.6 `uploader/Dockerfile`(新檔)

```dockerfile
# Token-gated podcast uploader: stdlib-only Python, no third-party deps, so the
# image is just the interpreter + one file. Content is bind-mounted at runtime.
FROM python:3.12-alpine
COPY server.py /app/server.py
ENV PORT=80
EXPOSE 80
CMD ["python3", "/app/server.py"]
```

## 7. 資料流 + 硬契約 schema

1. `publish_series` 讀 4 個 env(`PODCAST_PUBLIC_BASE_URL`/`PODCAST_TOKEN_SALT`/`PODCAST_UPLOAD_URL`/`PODCAST_UPLOAD_TOKEN`)。
2. 驗 show_id、必填 metadata、artwork(全在任何上傳前 fail-fast)。
3. `token = make_token(show_id, salt)`。
4. 讀 `manifest_path` → `episodes[]`(空則報錯)。
5. 每集:`_ensure_local_mp3` → 讀 bytes + `content_hash8` → `media_file` → 組 ep dict。
6. 組 show dict → 序列化 `show.json`、render `feed.xml`/`index.html`(皆記憶體 bytes)。
7. 開 httpx client → **auth 預檢 `GET /healthz`** → 依序 PUT:媒體(各 mp3 + artwork)→ show.json → feed.xml → index.html。
8. 回傳 `{feed_url, show_page_url, token, episode_count, episodes[]}`。

### show/channel dict schema(feed.py 讀,硬契約)

| key | 型別 | 來源 | feed.py 用途 |
|---|---|---|---|
| `token` | str | `make_token` | 組所有 URL |
| `title` | str | 參數 `.strip()` | `<title>` |
| `description` | str | 參數 | `<description>`/`<itunes:summary>` |
| `language` | str | 固定 `"zh-Hant"` | `<language>` |
| `author` | str | 參數 | `<itunes:author>` |
| `owner_name` | str | 參數 | `<itunes:owner><itunes:name>` |
| `owner_email` | str | 參數 | `<itunes:owner><itunes:email>` |
| `category` | str | 參數,預設 `"Technology"` | `<itunes:category>` |
| `explicit` | bool | 參數,預設 `False` | `<itunes:explicit>` |
| `artwork_file` | str | `"artwork.jpg"`/`"artwork.png"`(依實際格式) | `<itunes:image>` |
| `episodes` | dict[str, ep] | 見下 | items |
| `show_id`、`notebook_id` | str | 參數 | **不被 feed.py 讀**;僅存 show.json 供 audit/GC |

### episode ep dict schema(`episodes[str(n)]`,feed.py 讀,硬契約)

| key | 型別 | 來源 |
|---|---|---|
| `title` | str | `ep["title"]`(manifest) |
| `description` | str | `= ep["title"]`(manifest 無獨立 brief) |
| `guid` | str | `identity.episode_guid(show_id, n)` |
| `pub_date` | str(RFC-2822 +0800) | `ep.get("published_at")` **or** `_fallback_pub_date(n)` |
| `media_file` | str | `EP{n:02d}-{hash8}.mp3` |
| `length` | int | `len(mp3_bytes)`(= 檔案大小) |

**pubDate 穩定性**:主來源是 manifest 的 `published_at`(首次生成寫入、跨重發不變)。缺欄位(舊 manifest)時走**決定性且每集相異**的 fallback:`base = 2020-01-01T09:00:00+0800`,第 n 集 = `base − (n−1) 天` → 集序正確、跨重發 byte 穩定、無同季 tie。**任何情況下同季連發兩次,feed.xml byte 完全一致。**

## 8. 安全模型(兩 port + 兩 token)

### 8.1 三信任區,兩道邊界

```
[公開網際網路 — 敵意]
   │  邊界 A:Cloudflare Tunnel —— 只讓「讀埠」穿過
[內網 LAN — 半信任]
   │  邊界 B:寫埠 Bearer token —— 縱深防禦;寫埠本就只在內網、不進 tunnel
[MCP host — 信任(持有 Doppler secret)]
```

| | 讀(caddy) | 寫(uploader) |
|---|---|---|
| host port | `HOST_PORT`(既有,例 8080) | `UPLOAD_PORT`(新,例 8086) |
| 對外 | 進既有 CF Tunnel → 公開 HTTPS | **僅內網,絕不進 tunnel** |
| mount | `/srv:ro`(核心層唯讀) | `/srv`(rw) |
| 認證 | 無(公開讀,靠不可猜 token URL 遮蔽) | Bearer `UPLOAD_TOKEN`(常數時間比對) |
| 供誰 | Apple Podcasts / 任何 app | 只有內網 MCP host |

### 8.2 兩個 token 是兩種東西(不可合一)

| | feed token(HMAC) | upload token(Bearer) |
|---|---|---|
| 目的 | feed URL 的**遮蔽**(capability URL) | 寫入**身分驗證** |
| 產生 | `base32(HMAC(salt, show_id)[:15])`,決定性,每 show 一個 | 高熵隨機(`secrets.token_urlsafe(32)`),全系統一個 |
| 出現在 | **公開 feed URL 路徑**、Apple、CF log、手機 app | **只在** MCP→寫埠的 `Authorization` header |
| 是秘密? | 否(一定被看見) | 是 |
| 洩漏後果 | 那一個 show 可被讀(唯讀) | 可竄改任一 feed —— 但**只能從內網送達** |
| 輪替成本 | 高(換 URL = 訂閱者全斷) | 低(改 Doppler + NAS `.env` 重啟,訂閱者無感) |

**誠實標示**:feed token 不是隱私保證,是 capability URL(遮蔽)。**真正的讀取皇冠寶石是 `PODCAST_TOKEN_SALT`** —— 持 salt 者可用 show_id 字典枚舉出所有 feed URL。README/SKILL 措辭統一為「capability URL / 遮蔽」,並點明「salt 才是讀取隱私的關鍵秘密,勿放機密內容」。

### 8.3 洩漏影響矩陣(寫埠僅內網)

| 洩漏物 | 攻擊者能做 | 唯讀? | 範圍 | 補救 |
|---|---|---|---|---|
| 單一 feed token(URL) | 讀該 show 全集 | 是 | 單一 show | 換 URL(301 + `itunes:new-feed-url`) |
| `PODCAST_TOKEN_SALT` | 枚舉**所有** feed URL | 是 | 全 show 的讀 | 換 salt(全體訂閱者重訂,貴) |
| `PODCAST_UPLOAD_TOKEN` | 竄改任一 feed,**但需人已在內網才送得到** | 否 | 全 host 寫入 | 輪替 token(訂閱者無感,便宜) |
| Doppler config 存取 | 全部(salt + upload token + 帳號) | 否 | 全域 | 視同操作者淪陷,全面輪替 |
| `NOTEBOOKLM_AUTH_JSON` | 操作 NotebookLM 帳號 | 否 | 帳號 | 重登、重 sync |

關鍵不對稱:upload token 洩漏原本最危險,但因**寫埠只在內網、不進 tunnel**,實際利用還需攻擊者先進到 LAN → 兩道屏障;且補救便宜。這正是把兩 token、兩 port 分開的價值。

## 9. 設定(Doppler)與完整部署順序

### 9.1 Doppler(`-p notebooklm -c dev`)

| 變數 | 動作 | 用途 | 範例 |
|---|---|---|---|
| `PODCAST_PUBLIC_BASE_URL` | 保留 | 公開(讀)URL 根,無尾斜線 | `https://podcast.你的域名` |
| `PODCAST_TOKEN_SALT` | 保留 | token HMAC salt(只在 MCP;NAS 不需要) | (秘密) |
| `PODCAST_UPLOAD_URL` | **新增** | 寫埠內網 base,無尾斜線 | `http://192.0.2.10:8086` |
| `PODCAST_UPLOAD_TOKEN` | **新增** | Bearer,**必須 = NAS `.env` 的 `UPLOAD_TOKEN`** | (秘密) |
| `PODCAST_FEEDS_ROOT` | **移除(最後一步)** | 舊掛載輸出目錄 | — |

### 9.2 部署順序(嚴格照序,末項最後做)

```bash
# ① Doppler 設兩個新 secret(先產 token)
TOK=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
doppler secrets set PODCAST_UPLOAD_URL   "http://192.0.2.10:8086" -p notebooklm -c dev
doppler secrets set PODCAST_UPLOAD_TOKEN "$TOK"                        -p notebooklm -c dev
echo "$TOK"   # 貼到 NAS .env 的 UPLOAD_TOKEN(逐字元相同)

# ② NAS:.env 填同一個 UPLOAD_TOKEN + UPLOAD_PORT,部署兩容器,驗上傳
#    cd podcast-feed-host && docker compose pull && docker compose up -d
#    跑 §12 驗收 C(寫)/ D(讀)

# ③ 部署新 publish_series 到各 MCP(git pull + 重啟 MCP),用真 env 跑一次 publish 驗端到端

# ④ 確認上面全綠後,才刪舊 var(舊碼還在跑時刪會 ValueError: PODCAST_FEEDS_ROOT is required)
doppler secrets delete PODCAST_FEEDS_ROOT --yes -p notebooklm -c dev
```

### 9.3 `podcast-feed-host` 設定檔

**`.env.example`(取代)**
```bash
# Copy to `.env` (gitignored). Used by docker-compose.yml only.

# NAS path holding your feeds. caddy mounts it read-only; uploader read-write.
FEEDS_ROOT_HOST=/volume1/podcasts

# READ side: Caddy static server host port. Your EXISTING Cloudflare Tunnel
# ingress points at http://<this-host-lan-ip>:${HOST_PORT}. (unchanged)
HOST_PORT=8080

# WRITE side: uploader host port. LAN-ONLY — do NOT add it to the tunnel.
# MCP's PODCAST_UPLOAD_URL = http://<this-host-lan-ip>:${UPLOAD_PORT}
UPLOAD_PORT=8086

# Shared bearer token for uploads. MUST equal Doppler PODCAST_UPLOAD_TOKEN
# (project notebooklm, config dev). Rotate = change in BOTH places.
UPLOAD_TOKEN=
```

**`docker-compose.yml`(取代)**
```yaml
# Podcast feed hosting on a NAS, read/write split. Your EXISTING Cloudflare
# Tunnel points ONE hostname at the read port:
#   podcast.你的域名 -> http://<lan-ip>:${HOST_PORT}   (caddy, read, public)
# The write port is LAN-only and is NOT in the tunnel; MCP hosts on the same
# network PUT to it directly. Both services share one dir: caddy ro, uploader rw.
services:
  caddy:                       # READ half — public static serving, read-only
    image: ghcr.io/audichuang/podcast-feed-host:latest
    restart: unless-stopped
    pull_policy: always
    ports:
      - "${HOST_PORT:-8080}:80"
    volumes:
      - ${FEEDS_ROOT_HOST:?set FEEDS_ROOT_HOST in .env}:/srv:ro

  uploader:                    # WRITE half — LAN-only, token-gated, read-write
    image: ghcr.io/audichuang/podcast-feed-uploader:latest
    restart: unless-stopped
    pull_policy: always
    environment:
      UPLOAD_TOKEN: ${UPLOAD_TOKEN:?set UPLOAD_TOKEN in .env}
    ports:
      - "${UPLOAD_PORT:-8086}:80"     # LAN-only; never add to the tunnel ingress
    volumes:
      - ${FEEDS_ROOT_HOST:?set FEEDS_ROOT_HOST in .env}:/srv
    healthcheck:
      test: ["CMD", "python3", "-c",
             "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:80/healthz',timeout=3).status==200 else 1)"]
      interval: 30s
      timeout: 5s
      retries: 3
```

**`Caddyfile`(讀站,加一行)**
```caddyfile
	# Internal state — never expose publicly (contains owner_email / notebook_id).
	respond /feeds/*/show.json 403
```
(feed_info 已改純計算、不經 HTTP 讀 show.json,故此擋無副作用。)

## 10. 錯誤處理(fail-fast)

**Client(`publish_series`)** —— 全部在送出大檔前擋掉:
- 任一 env 缺/空 → `ValueError`。
- show_id 非法 / 必填 metadata 空 → `ValueError`。
- artwork 不合規 → `ValueError`(任何上傳前)。
- manifest 無 episodes → `ValueError`。
- **auth 預檢 `GET /healthz` 非 200 → `ValueError`**(不送任何大 mp3)。
- mp3 遺失且無 `artifact_id` / 重抓後仍無檔 → `ValueError`。
- 任一 PUT 非 201 → `raise ValueError`(**不 retry**;冪等,使用者重跑即收斂)。

**Uploader** —— 見 §6.3。phase 語意:寫入途中失敗 → temp 清掉、目標不變;因每檔獨立原子 + client 提交順序,reader 最多見到「一致的舊狀態」(舊 feed.xml → 舊 mp3 仍在),不會 dangling enclosure。

**冪等**:同季重發 → 同 token 目錄;mp3 內容定址(同內容同檔名,覆寫 byte 一致)、show.json/feed.xml/index.html 原地覆寫;manifest 不變則 feed.xml byte 一致。5xx/逾時 → 使用者重跑;4xx → 修錯再跑。

## 11. 測試策略(全離線,零網路/NAS/NotebookLM)

三條紅線:MCP 端網路走 `httpx.MockTransport`(monkeypatch `tools_publish._make_client`);uploader 走純函式自檢;NotebookLM 僅 `_ensure_local_mp3` 重抓分支,用既有 `fake_client` 假掉(其餘測試直接給本機 mp3,不進該分支)。

**沿用不動(回歸基線)**:`test_publish_identity.py`(+ 併入 `episode_guid` 三條決定性測試)、`test_publish_feed.py`(**移除 tombstone 案例**)、`test_publish_artwork.py`、`test_publish_layout.py`(**只留 `content_hash8` + `media_filename`**,刪 atomic 測試)。

**刪除**:`tests/test_publish_state.py`(`episode_guid` 測試搬去 identity 測試)。

**重寫 `tests/test_publish_tools.py`**(env fixture 改設四個 `PODCAST_*`、**移除 `PODCAST_FEEDS_ROOT`**;monkeypatch `_make_client` 回傳裝了 `httpx.MockTransport` 的 client,handler 把每個 PUT 收進 `captured` dict):
- `test_posts_to_upload_url_not_feeds_root` —— PUT 目標是 `PODCAST_UPLOAD_URL/feeds/<token>/…`。
- `test_sends_upload_bearer_token` —— header `Authorization: Bearer <PODCAST_UPLOAD_TOKEN>`,且**不等於** feed token。
- `test_puts_every_episode_artwork_state_and_derived` —— 收到 `{EP01-*.mp3, EP02-*.mp3, artwork.png, show.json, feed.xml, index.html}`。
- `test_commit_order_media_then_state_then_derived` —— 記錄 PUT 順序,斷言 `[…媒體…, show.json, feed.xml, index.html]`。
- `test_auth_precheck_runs_before_any_put` —— 先 `GET /healthz`;handler 回 401 時 → `ValueError`、**零 PUT**。
- `test_publish_is_idempotent` —— manifest 帶 `published_at`,連發兩次 → feed.xml byte 一致、media 檔名一致。
- `test_new_mp3_bytes_new_url_stable_guid_and_pubdate` —— 改某集 mp3 內容 → `media_file`/enclosure URL 變、`guid` 不變、`pub_date` 不變。
- `test_fallback_pubdate_deterministic` —— manifest **無** `published_at` → 每集 pubDate 相異、**方向正確(EP01 pubDate < EP02,集號越大越新,與 published_at 主路徑同向)**、且兩次發布 byte 一致。
- `test_missing_env_errors` —— 刪 `PODCAST_UPLOAD_URL` → `ValueError match="PODCAST_UPLOAD_URL"`。
- `test_missing_required_metadata_errors` —— `show_description=""` → `ValueError`,handler 不被呼叫。
- `test_feed_info` —— `feed_info("ai-news")` 回 `{show_id, token, feed_url, show_page_url}`,`token == make_token(...)`。
- **刪** `feed_list`、tombstone 復活測試。

**uploader**:`podcast-feed-host/uploader/test_server.py`(§6.5)—— path 白名單 + 原子寫 roundtrip。auth 預檢與 traversal 拒絕由 §12 部署驗收 `curl` 覆蓋(http.server handler 難純單元測)。

**執行**:`uv run pytest -q` 全綠;`asyncio_mode="auto"` 沿用。DoD:全綠 + 上述具名測試存在通過 + 全程無真網路/NAS/NotebookLM。

## 12. 部署與 CI(兩 image)

### 12.1 `.github/workflows/build.yml`(取代:matrix 兩 image,multi-arch 保留)

```yaml
name: build-and-push
# Build BOTH images (read: Caddy static server; write: token-gated uploader)
# and push to GHCR. NAS then `docker compose pull && up -d`.
on:
  push:
    branches: [main]
    paths: [Dockerfile, Caddyfile, "uploader/**", .github/workflows/build.yml]
  workflow_dispatch:
permissions: {contents: read, packages: write}
jobs:
  build:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        include:
          - {name: podcast-feed-host,     context: .,          file: ./Dockerfile}
          - {name: podcast-feed-uploader, context: ./uploader, file: ./uploader/Dockerfile}
    steps:
      - uses: actions/checkout@v4
      - uses: docker/setup-qemu-action@v3
      - uses: docker/setup-buildx-action@v3
      - uses: docker/login-action@v3
        with: {registry: ghcr.io, username: "${{ github.actor }}", password: "${{ secrets.GITHUB_TOKEN }}"}
      - id: meta
        uses: docker/metadata-action@v5
        with:
          images: ghcr.io/${{ github.repository_owner }}/${{ matrix.name }}
          tags: |
            type=raw,value=latest
            type=sha,format=short
      - uses: docker/build-push-action@v6
        with:
          context: ${{ matrix.context }}
          file: ${{ matrix.file }}
          platforms: linux/amd64,linux/arm64
          push: true
          tags: ${{ steps.meta.outputs.tags }}
          labels: ${{ steps.meta.outputs.labels }}
          cache-from: type=gha,scope=${{ matrix.name }}
          cache-to: type=gha,mode=max,scope=${{ matrix.name }}
```

> **保留 multi-arch 與 build cache(不採納「砍掉」的 critique)**:讀 image 現況已 multi-arch 且部署成功;uploader 是 `python:3.12-alpine` + COPY 一檔,即使 QEMU 也秒級。保留 multi-arch 避免 NAS arch 不符踩雷;cache `scope` 分開兩 image 不互洗,無害。用 `github.repository_owner`(非 `github.repository`)才能在同 repo 推兩個不同 image 名。

README 補三點:第二個 GHCR package `podcast-feed-uploader` 首次 build 後設 Public(或 NAS `docker login ghcr.io`);`.env` 多填 `UPLOAD_TOKEN`/`UPLOAD_PORT`;**寫埠不加 tunnel ingress**(讀埠 ingress 維持既有那一條)。

### 12.2 部署驗收(實跑,非程式碼)

```bash
READ=https://podcast.你的域名
WRITE=http://192.0.2.10:8086          # 內網,不經 tunnel
TOK=$(doppler secrets get PODCAST_UPLOAD_TOKEN -p notebooklm -c dev --plain)
```
**A. token 一致**:Doppler `PODCAST_UPLOAD_TOKEN` 與 NAS `.env` `UPLOAD_TOKEN` 逐字元相同;`PODCAST_FEEDS_ROOT` 已從 Doppler 刪除;`PODCAST_PUBLIC_BASE_URL`/`PODCAST_TOKEN_SALT` 仍在。
**B. 兩容器**:`docker compose ps` 兩個 `Up`,uploader `healthy`。
**C. 寫(內網直打)**:
- 無 token PUT → `401`;帶 token 打非白名單檔名 → `404`;traversal `…/../../x` → `404`。
- `GET $WRITE/healthz`(不帶)→ `200`;帶正確 Bearer → `200`;帶錯 Bearer → `401`。
- 合法 PUT 真 token 的 `feed.xml` → `201`,NAS 上檔在、無殘留 `.tmp-*`。
**D. 讀(經 tunnel,沿用既有)**:`feed.xml` → `application/rss+xml` + `no-cache`;`*.mp3` → `audio/mpeg` + `immutable` + `Range 206`;`/healthz` → `200`;`show.json` → `403`。
**E. 端到端**:真 env 跑一次 `publish_series`(**務必用 throwaway show_id,勿打既有真節目**——舊 manifest 無 `published_at`,重發會使 pubDate 塌回 fallback,見 T2 (d))→ `feed_url` 讀得到、mp3 播得動 → Apple「Validate your podcast RSS feed」通過 → 刪掉 throwaway 測試目錄。

**Troubleshooting**:publish 全失敗(尤其 auth 預檢 401)→ **先比對 Doppler `PODCAST_UPLOAD_TOKEN` 與 NAS `.env` `UPLOAD_TOKEN` 是否逐字元相同**(輪替時兩處都要改)。

## 13. 已知取捨 / 未決(YAGNI 標注)

**設計取捨(已定案)**
- **T1 — uploader 永不刪,孤兒 mp3 只增不減。** 換來「immutable URL 永不 404」。以「自用/不補集/極少重生」規模可忽略。真塞爆再做**離線、獨立** GC(刪不被當前 `show.json`/`feed.xml` 引用**且** >N 天的 mp3);**不進上傳路徑**。`show.json` 保留作為 server 端 audit record + 未來 GC 的引用集(不是 optional,是無 state 後唯一的落地紀錄)。
- **T2 — pubDate 全押在 manifest `published_at`。** manifest 是這一季的持久紀錄(勿刪)。(a) 刪 manifest → 走決定性 fallback(集序仍正確、仍 byte 穩定,只是日期非真實生成時間);(b) companion change(§5.4)須與本重構**同批上線**,否則舊 manifest 無 `published_at`;(c) 重生某集會覆寫該集 `published_at`(重生=換內容,可接受);(d) **既有(遷移前)節目重發**:舊 manifest 由舊 `_run_episode` 產生、無 `published_at` → pubDate 從真實日期塌回 2020 fallback(guid 穩定故**不會重複上架**,但 pubDate/lastBuildDate 對既有訂閱者可見變動)。要保真實日期,一次性把各集 `published_at` 回填進舊 manifest 再重發。README 明寫「重跑 = 重發整季,不是增量補集」。
- **T3 — tombstone/縮季語意消失。** 從 manifest 拿掉某集再重發 → 該集 item 從 feed 消失(其 mp3 因不刪而留著,但不再被引用)。自用可接受,README 標明「送的是完整當下版本;拿掉的集會從清單消失」。要優雅下架單集再引回 `itunes:block`(YAGNI)。
- **T4 — `feed_list` 砍除、`feed_info` 純計算。** 無掛載可掃、token 單向不可反列舉。要清單靠使用者自維 show_id;現階段不做。
- **T5 — mp3 整檔進 RAM 後 PUT。** 單集數十 MB,一次一集,可接受;`# ponytail:` 已標,超過數百 MB 再改 streaming(需顯式 Content-Length,因 uploader 只吃 Content-Length、不解 chunked)。

**安全/運維(接受並明說)**
- **T6 — Bearer 在 LAN 明文傳輸。** 「LAN 半信任」下可接受;LAN 不可信則走 WireGuard/自簽 TLS,或回退 SMB/NFS 掛載。
- **T7 — 單一共用 token 無法逐台撤銷。** 任一台洩漏就全體輪替(改 Doppler + NAS `.env` 重啟 uploader);訂閱者無感。文件標明。
- **T8 — salt 與 upload token 同住一個 Doppler config。** 協定層兩者分工(讀 URL vs 寫 header),但 Doppler 一破兩者皆失。自用可接受(Doppler = 操作者本人)。要真正分權可拆到獨立 config(低優先)。
- **T9 — 寫埠 compose 綁 `${UPLOAD_PORT}:80`(0.0.0.0)。** NAS 在家用路由 NAT 後 = 實際僅 LAN 可達;縱深防禦是「不進 tunnel + Bearer」。若 NAS 直接對公網曝或多網段需收斂,可改綁 `${LAN_IP}:${UPLOAD_PORT}:80`(硬化選項,非必需)。
- **T10 — artwork 只在 client 端驗。** uploader 是 dumb landing zone,不驗 feed 完整性、不重驗 artwork(避免把 Pillow 拉進 uploader、把 uploader 耦合進 feed 模型)。「整季齊全 + artwork 合規」是 client 責任(已 fail-fast)。要 uploader 再驗 bytes 才收(防出錯 client)= 加依賴,YAGNI。
- **T11 — 每檔原子、整份 feed 非原子。** 唯一非原子視窗靠 client 提交順序(媒體→show.json→feed.xml)+ uploader 不刪檔化解為「一致的舊狀態」;client 對失敗直接 raise、使用者重跑收斂,故 MCP 端不做 retry。
- **T12 — 併發同 token。** 自用「一次發一個」,不加鎖;`# ponytail:` 無鎖,真出現併發發布再加 uploader per-token `fcntl.flock`。

**未來彈性(現在不做)**
- **T13 — MCP 搬雲端。** 加第二條 tunnel ingress 曝露寫埠即可(token 已就位);屆時 token 升為唯一防線,須補 IP allowlist / Cloudflare Access,並重估 body 大小限制。