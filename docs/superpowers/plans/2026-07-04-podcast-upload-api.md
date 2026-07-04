# Podcast 發布改讀寫分離(內網 uploader)Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 podcast 發布從「MCP 直接寫掛載的 NAS 檔案系統」改成「MCP 用內網 HTTP PUT 到 NAS 上一支 token 保護的 uploader,Caddy 唯讀對外」,實現讀寫分離解耦。

**Architecture:** NAS 端新增 stdlib `http.server` uploader(可寫、僅內網、bearer 保護、原子落地),與現有唯讀 Caddy 兩容器共用 `feeds/` 目錄;MCP 端 `publish_series` 改成在記憶體渲染整季、逐檔 PUT(一次一支 mp3 讀→PUT→丟)。純模組砍到只留發布必需(state.py 整刪、tombstone/atomic/feed_list 移除)。

**Tech Stack:** Python 3.12;MCP 端 FastMCP + notebooklm-py 0.3.4 + httpx;uploader 純 stdlib(`http.server`/`hashlib`/`secrets`/`re`);Docker(python:3.12-alpine + caddy)、GitHub Actions matrix、pytest。

**Spec:** `docs/superpowers/specs/2026-07-04-podcast-upload-api-design.md`(含每個檔案的逐字定版 code;各 task 的實作步驟直接落地 spec 對應小節的 code,不要自行改寫)

## Global Constraints

- **兩 repo**:MCP 端 `notebooklm-skill`(本機 `/home/user/research/audiskill/notebooklm-skill`,分支 `master`);NAS 端 `podcast-feed-host`(`/home/user/research/audiskill/podcast-feed-host`,分支 `main`,public GHCR)。**各自 commit 到各自 repo**,不要跨 repo commit。
- **wire contract(兩端逐字一致,不可漂移)**:`PUT /feeds/{token}/{name}`;header `Authorization: Bearer <UPLOAD_TOKEN>`;`{token}` = `[a-z2-7]{24}`;`{name}` 白名單 = `feed.xml|index.html|show.json|artwork.(png|jpg)|EP\d{2}-[0-9a-f]{8}.mp3`;成功回 `201`,auth 錯 `401`,路徑不符 `404`,缺 Content-Length `411`,超限 `413`,寫入失敗 `500`;healthz 回應必帶 header `X-Podcast-Uploader: 1`。
- **feed token**(目錄名,非密鑰)= `base32_lower(HMAC_SHA256(PODCAST_TOKEN_SALT, show_id)[:15])` → 24 字元 `[a-z2-7]`;**upload token** 是另一個 shared bearer 密鑰,兩者絕不混用。
- **Python 3.12**;MCP `pyproject.toml` dependencies 加 `httpx>=0.27,<1`(已是 transitive dep,零新裝);uploader **只用 stdlib**。
- 全程**繁體中文**註解/文件;TDD 紅→綠;每 task 一個(或數個)commit,訊息寫清楚「做什麼 + 為何」。
- MCP 端測試**全離線**:網路走 `httpx.MockTransport`(monkeypatch `tools_publish._make_client`)、NAS 用 `tmp_path`、NotebookLM 用既有 `fake_client` fixture。`uv run pytest -q` 全綠。

---

## File Structure

**NAS 端 `podcast-feed-host`:**
- Create `uploader/server.py` — stdlib 上傳服務(bearer + path 白名單 + 原子寫 + healthz marker)
- Create `uploader/Dockerfile` — `python:3.12-alpine` + COPY server.py
- Create `uploader/test_server.py` — 離線自檢(path 白名單 + 原子寫 + loopback handler 端到端)
- Modify `docker-compose.yml` — caddy(ro)+ uploader(rw,綁 LAN IP)兩 service
- Modify `.env.example` — 加 `UPLOAD_BIND`/`UPLOAD_PORT`/`UPLOAD_TOKEN`
- Modify `Caddyfile` — 加 `respond /feeds/*/show.json 403`
- Modify `.github/workflows/build.yml` — matrix build 兩 image
- Modify `README.md` — 第二個 GHCR package 設 Public、`.env` 新欄、防火牆驗收

**MCP 端 `notebooklm-skill`:**
- Modify `notebooklm_mcp/publish/identity.py` — 加 `episode_guid`
- Modify `notebooklm_mcp/publish/layout.py` — 砍到剩 `content_hash8`/`media_filename`
- Modify `notebooklm_mcp/publish/rss_models.py` — `live_episodes` 移除 tombstone
- Delete `notebooklm_mcp/publish/state.py`
- Modify `notebooklm_mcp/publish/__init__.py` — docstring 移除 state
- Modify `notebooklm_mcp/tools_podcast.py` — `_run_episode` return 加 `published_at`
- Rewrite `notebooklm_mcp/tools_publish.py` — HTTP PUT 模型
- Modify `pyproject.toml` — 加 httpx 直接依賴
- Delete `tests/test_publish_state.py`;Modify `tests/test_publish_{identity,feed,layout}.py`;Rewrite `tests/test_publish_tools.py`
- Modify `SKILL.md` / `AGENTS.md` / `README.md`

---

## Task 1: NAS uploader 服務(server + Dockerfile + 自檢)

**Repo:** `podcast-feed-host`

**Files:**
- Create: `uploader/server.py`(spec §6.2–6.4 定版全文)
- Create: `uploader/Dockerfile`(spec §12.3 附近定版)
- Create: `uploader/test_server.py`(spec §6.5 + §11 handler 端到端)

**Interfaces:**
- Produces(wire,MCP 端 Task 5 依賴):`PUT /feeds/{token}/{name}` + `Authorization: Bearer`;`GET /healthz`(帶 auth 驗 token、回應含 `X-Podcast-Uploader: 1`);狀態碼 201/401/404/411/413/500;`server._PATH_RE`、`server.atomic_write(dst, rfile, length)`、`server._MARKER`。

- [ ] **Step 1: 寫 path 白名單 + 原子寫 roundtrip 的失敗測試**

Create `uploader/test_server.py` 的前半(先只放這兩個 pure 測試 + main):

```python
"""Offline check: path allowlist + atomic write + live handler (auth/marker/413/traversal).
`python3 uploader/test_server.py` — asserts, no third-party deps."""
import io, os, tempfile, threading, urllib.request, urllib.error
os.environ.setdefault("UPLOAD_TOKEN", "testtoken")
import server  # noqa: E402

T = "abcdefghijklmnopqrstuvwx"  # 24 chars in [a-z2-7]

def test_path_allowlist():
    ok = [f"/feeds/{T}/feed.xml", f"/feeds/{T}/index.html", f"/feeds/{T}/show.json",
          f"/feeds/{T}/artwork.png", f"/feeds/{T}/artwork.jpg", f"/feeds/{T}/EP01-deadbeef.mp3"]
    bad = [f"/feeds/{T}/../../etc/passwd", f"/feeds/{T}/evil.sh", "/feeds/SHORT/feed.xml",
           f"/feeds/{T}/feed.xml/..", f"/{T}/feed.xml", f"/feeds/{T}/EP1-deadbeef.mp3",
           f"/feeds/{T}/EP100-deadbeef.mp3"]
    assert all(server._PATH_RE.match(p) for p in ok)
    assert not any(server._PATH_RE.match(p) for p in bad)

def test_atomic_write_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        dst = os.path.join(d, "feeds", T, "feed.xml")
        payload = b"<rss/>" * 1000
        server.atomic_write(dst, io.BytesIO(payload), len(payload))
        assert open(dst, "rb").read() == payload
        assert not any(n.startswith(".tmp-") for n in os.listdir(os.path.dirname(dst)))
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `cd podcast-feed-host && python3 uploader/test_server.py`
Expected: FAIL(`ModuleNotFoundError: server` 或 `AttributeError`)。

- [ ] **Step 3: 實作 server.py(落地 spec §6.2–6.4 定版全文)**

把 spec `docs/…/2026-07-04-podcast-upload-api-design.md` §6.2–6.4 的 `uploader/server.py` **逐字**寫入 `podcast-feed-host/uploader/server.py`。關鍵不可漏:模組頂 `TOKEN = os.environ.get("UPLOAD_TOKEN", "")` + `if not TOKEN: raise RuntimeError(...)`;`_TOKEN`/`_NAME`/`_PATH_RE`;`atomic_write`(temp→fsync→`os.replace`,目錄 fsync 為獨立 `try/except OSError: pass`);`_MARKER=("X-Podcast-Uploader","1")` 且 `_reply` 每次 `send_header(*_MARKER)`;`do_GET` healthz(無 auth→200、有 auth→驗 token,皆帶 marker);`do_PUT`(bearer→路徑白名單→Content-Length→MAX_BYTES→`atomic_write`→201);`if __name__=="__main__": ThreadingHTTPServer(("", PORT), Handler).serve_forever()`。

- [ ] **Step 4: 補 handler 端到端測試(auth/marker/traversal/411/413)**

在 `uploader/test_server.py` 追加(用 loopback server,零依賴):

```python
def _serve():
    from http.server import ThreadingHTTPServer
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"

def _req(method, url, data=None, token=None, extra_len=None):
    r = urllib.request.Request(url, data=data, method=method)
    if token is not None:
        r.add_header("Authorization", f"Bearer {token}")
    if extra_len is not None:
        r.add_header("Content-Length", str(extra_len))
    try:
        resp = urllib.request.urlopen(r, timeout=5)
        return resp.status, resp.headers
    except urllib.error.HTTPError as e:
        return e.code, e.headers

def test_handler_end_to_end():
    import server as s
    old_root = s.ROOT
    with tempfile.TemporaryDirectory() as d:
        s.ROOT = d
        httpd, base = _serve()
        try:
            # healthz: 帶對 token → 200 + marker;錯 token → 401
            code, hdr = _req("GET", f"{base}/healthz", token="testtoken")
            assert code == 200 and hdr.get("X-Podcast-Uploader") == "1"
            code, _ = _req("GET", f"{base}/healthz", token="WRONG")
            assert code == 401
            # 合法 PUT → 201 且落地
            code, _ = _req("PUT", f"{base}/feeds/{T}/feed.xml", data=b"<rss/>", token="testtoken")
            assert code == 201
            assert open(os.path.join(d, "feeds", T, "feed.xml"), "rb").read() == b"<rss/>"
            # 無 token → 401;白名單外 → 404
            code, _ = _req("PUT", f"{base}/feeds/{T}/feed.xml", data=b"x")
            assert code == 401
            code, _ = _req("PUT", f"{base}/feeds/{T}/evil.sh", data=b"x", token="testtoken")
            assert code == 404
            # 超限 → 413(謊報 Content-Length 超過 MAX)
            big = s.MAX_BYTES + 1
            code, _ = _req("PUT", f"{base}/feeds/{T}/feed.xml", data=b"x", token="testtoken", extra_len=big)
            assert code == 413
        finally:
            httpd.shutdown(); s.ROOT = old_root

if __name__ == "__main__":
    test_path_allowlist(); test_atomic_write_roundtrip(); test_handler_end_to_end()
    print("ok")
```

- [ ] **Step 5: 跑測試確認通過**

Run: `cd podcast-feed-host && python3 uploader/test_server.py`
Expected: `ok`(全部 assert 通過)。

- [ ] **Step 6: 實作 Dockerfile**

Create `podcast-feed-host/uploader/Dockerfile`:

```dockerfile
# stdlib-only Python uploader: interpreter + one file. Content bind-mounted at runtime.
FROM python:3.12-alpine
COPY server.py /app/server.py
ENV PORT=80
EXPOSE 80
CMD ["python3", "/app/server.py"]
```

- [ ] **Step 7: Commit**

```bash
cd podcast-feed-host
git add uploader/
git commit -m "feat(uploader): stdlib token-gated upload server + offline handler tests"
```

---

## Task 2: NAS compose / Caddyfile / CI / .env / README

**Repo:** `podcast-feed-host`

**Files:**
- Modify: `docker-compose.yml`(spec §12.2 定版)
- Modify: `.env.example`(spec §12「.env.example」定版)
- Modify: `Caddyfile`(spec §12.2「Caddyfile」加一行)
- Modify: `.github/workflows/build.yml`(spec §12.1 定版)
- Modify: `README.md`

**Interfaces:**
- Consumes: Task 1 的 `uploader/`(image `ghcr.io/audichuang/podcast-feed-uploader`)。
- Produces: 部署拓樸(讀 caddy `${HOST_PORT}`、寫 uploader 綁 `${UPLOAD_BIND}:${UPLOAD_PORT}`)。

- [ ] **Step 1: 改 docker-compose.yml(落地 spec §12.2)**

把現有單一 caddy service 換成 spec §12.2 的兩 service 定版:`caddy`(`${HOST_PORT:-8080}:80`、`:/srv:ro`)+ `uploader`(image `podcast-feed-uploader`、`environment: UPLOAD_TOKEN`、ports `"${UPLOAD_BIND:?set UPLOAD_BIND to the NAS LAN IP in .env}:${UPLOAD_PORT:-8086}:80"`、`:/srv` rw、healthcheck 打 `/healthz`)。**務必保留 `${UPLOAD_BIND}` 這段**——綁 LAN IP 而非 0.0.0.0 是寫埠不外曝的第一道防線。

- [ ] **Step 2: 改 .env.example(落地 spec §12「.env.example」)**

寫入 spec 定版:`FEEDS_ROOT_HOST`、`HOST_PORT=8080`、`UPLOAD_BIND=192.0.2.10`(註明:NAS LAN IP,compose 只綁這個介面)、`UPLOAD_PORT=8086`、`UPLOAD_TOKEN=`(註明 = Doppler `PODCAST_UPLOAD_TOKEN`)。

- [ ] **Step 3: 改 Caddyfile(加 show.json 403)**

在既有 `:80 { … }` block 內加(spec §12.2):

```caddyfile
	# Internal audit state — never expose (contains notebook_id / internal state).
	# (owner_email is already public in feed.xml; this 403 is about notebook_id.)
	respond /feeds/*/show.json 403
```

- [ ] **Step 4: 改 CI 成 matrix 兩 image(落地 spec §12.1)**

把 `.github/workflows/build.yml` 換成 spec §12.1 定版:`strategy.matrix.include` 兩項(`{name: podcast-feed-host, context: ., file: ./Dockerfile}` 與 `{name: podcast-feed-uploader, context: ./uploader, file: ./uploader/Dockerfile}`);`images: ghcr.io/${{ github.repository_owner }}/${{ matrix.name }}`;`platforms: linux/amd64,linux/arm64`;gha cache `scope=${{ matrix.name }}`;`paths` 加 `"uploader/**"`。

- [ ] **Step 5: 本機驗證 compose 語法**

Run: `cd podcast-feed-host && FEEDS_ROOT_HOST=/tmp UPLOAD_BIND=127.0.0.1 UPLOAD_TOKEN=x docker compose config -q && echo COMPOSE_OK`
Expected: `COMPOSE_OK`(無語法錯)。

- [ ] **Step 6: 更新 README(第二 package + .env + 防火牆驗收)**

在 `README.md` 補:(a) uploader 是寫端、只綁內網、**不要**加進 tunnel ingress、且 firewall 擋 WAN;(b) `.env` 多填 `UPLOAD_BIND`/`UPLOAD_PORT`/`UPLOAD_TOKEN`;(c) 首次 CI 後第二個 GHCR package `podcast-feed-uploader` 也要設 Public(或 NAS `docker login`);(d) 驗收:`curl -H "Authorization: Bearer $TOK" http://<lan>:8086/healthz` 回 200 且含 `X-Podcast-Uploader: 1`,無 token → 401。

- [ ] **Step 7: Commit**

```bash
cd podcast-feed-host
git add docker-compose.yml .env.example Caddyfile .github/workflows/build.yml README.md
git commit -m "feat: two-service compose (read caddy + write uploader) + matrix CI + docs"
```

---

## Task 3: MCP 純模組重構(identity/layout/rss_models/刪 state)

**Repo:** `notebooklm-skill`

**Files:**
- Modify: `notebooklm_mcp/publish/identity.py`(加 `episode_guid`,spec §5.1)
- Modify: `notebooklm_mcp/publish/layout.py`(spec §5.2 定版,只留兩函式)
- Modify: `notebooklm_mcp/publish/rss_models.py`(spec §5.3 定版)
- Delete: `notebooklm_mcp/publish/state.py`
- Modify: `notebooklm_mcp/publish/__init__.py`(docstring 移除 state)
- Modify: `tests/test_publish_identity.py`(併入 episode_guid 測試)
- Modify: `tests/test_publish_layout.py`(刪 atomic 測試)
- Modify: `tests/test_publish_feed.py`(移除 tombstone 案例)
- Delete: `tests/test_publish_state.py`

**Interfaces:**
- Produces(Task 5 依賴):`identity.episode_guid(show_id, n) -> str`(sha1 `"{show_id}:{n}"`);`layout.content_hash8(path)`、`layout.media_filename(n, hash8)`;`rss_models.live_episodes(show)`(無 tombstone 過濾)。

- [ ] **Step 1: 寫 episode_guid 的失敗測試(搬進 identity 測試)**

在 `tests/test_publish_identity.py` 加(這三條原在 test_publish_state.py):

```python
def test_episode_guid_stable_and_source_decoupled():
    from notebooklm_mcp.publish import identity
    g1 = identity.episode_guid("ai-news", 1)
    assert g1 == identity.episode_guid("ai-news", 1)
    assert g1 != identity.episode_guid("ai-news", 2)
    assert g1 != identity.episode_guid("other", 1)
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `cd notebooklm-skill && uv run pytest tests/test_publish_identity.py::test_episode_guid_stable_and_source_decoupled -q`
Expected: FAIL(`AttributeError: module 'identity' has no attribute 'episode_guid'`)。

- [ ] **Step 3: 落地 spec §5.1 的 episode_guid**

在 `notebooklm_mcp/publish/identity.py` 檔尾加 spec §5.1 定版 `episode_guid`(`validate_show_id(show_id)` 後 `hashlib.sha1(f"{show_id}:{episode_n}".encode()).hexdigest()`;`hashlib` 已 import)。

- [ ] **Step 4: 落地 layout.py / rss_models.py 定版,刪 state.py**

- 把 `notebooklm_mcp/publish/layout.py` 換成 spec §5.2 定版(只留 `content_hash8` + `media_filename`,刪 `atomic_write_bytes/text`/`atomic_copy`)。
- 把 `notebooklm_mcp/publish/rss_models.py` 換成 spec §5.3 定版(`live_episodes` 移除 tombstone 過濾)。
- `rm notebooklm_mcp/publish/state.py`;`rm tests/test_publish_state.py`。
- `notebooklm_mcp/publish/__init__.py` docstring 把 `identity/layout/state/…` 的 `state` 移除。
- `tests/test_publish_layout.py` 刪掉針對 `atomic_write_*`/`atomic_copy` 的測試(只留 content_hash8/media_filename)。
- `tests/test_publish_feed.py` 移除任何帶 `tombstone` 的案例(改為所有集皆 live)。

- [ ] **Step 5: 跑受影響測試確認綠**

Run: `cd notebooklm-skill && uv run pytest tests/test_publish_identity.py tests/test_publish_layout.py tests/test_publish_feed.py -q`
Expected: PASS(不含 state.py;feed/layout 測試已去除 tombstone/atomic)。

- [ ] **Step 6: Commit**

```bash
cd notebooklm-skill
git add notebooklm_mcp/publish/ tests/test_publish_identity.py tests/test_publish_layout.py tests/test_publish_feed.py
git rm notebooklm_mcp/publish/state.py tests/test_publish_state.py
git commit -m "refactor(publish): move episode_guid to identity, drop state.py/tombstone/atomic (upload-api)"
```

---

## Task 4: tools_podcast 寫入 published_at

**Repo:** `notebooklm-skill`

**Files:**
- Modify: `notebooklm_mcp/tools_podcast.py`(`_run_episode` return,spec §5.4)
- Modify: `tests/test_tools_podcast.py`(斷言 return 帶 published_at)

**Interfaces:**
- Produces:`series_manifest.json` 每集帶 `published_at`(RFC-2822 +0800),供 Task 5 的 `publish_series` 當 pubDate 主來源。

- [ ] **Step 1: 寫失敗測試**

在 `tests/test_tools_podcast.py` 既有 `_run_episode`/`podcast_series` 測試處加斷言:每集 result dict 含非空 `published_at`(字串)。若既有測試已檢查 return dict,擴充其斷言即可。

```python
# 於既有 _run_episode 成功案例後:
assert isinstance(res["published_at"], str) and res["published_at"]
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `cd notebooklm-skill && uv run pytest tests/test_tools_podcast.py -q`
Expected: FAIL(`KeyError: 'published_at'`)。

- [ ] **Step 3: 落地 spec §5.4 的 companion change**

在 `notebooklm_mcp/tools_podcast.py` 的 `_run_episode` return dict 加 `"published_at": format_datetime(datetime.now(_TZ))`(spec §5.4;檔頭補 `from email.utils import format_datetime`、`from datetime import datetime, timezone, timedelta`、`_TZ = timezone(timedelta(hours=8))`,若尚未有)。

- [ ] **Step 4: 跑測試確認通過**

Run: `cd notebooklm-skill && uv run pytest tests/test_tools_podcast.py -q`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
cd notebooklm-skill
git add notebooklm_mcp/tools_podcast.py tests/test_tools_podcast.py
git commit -m "feat(podcast): stamp published_at into episode result for stable pubDate"
```

---

## Task 5: tools_publish.py 重寫成 HTTP PUT 模型

**Repo:** `notebooklm-skill`

**Files:**
- Rewrite: `notebooklm_mcp/tools_publish.py`(spec §5.5 全文)
- Modify: `pyproject.toml`(dependencies 加 `httpx>=0.27,<1`)
- Rewrite: `tests/test_publish_tools.py`(spec §11)

**Interfaces:**
- Consumes:`identity.make_token`/`episode_guid`、`layout.content_hash8`/`media_filename`、`feed.build_feed_xml`/`build_index_html`、`artwork.validate_artwork`、`runtime`。
- Produces(MCP tools):`publish_series(show_id, notebook_id, manifest_path, show_title, show_description, author, owner_name, owner_email, artwork_path, category="Technology", explicit=False) -> dict`;`feed_info(show_id) -> {show_id, token, feed_url, show_page_url}`。`feed_list` **不再存在**。內部縫 `_make_client()`(測試 monkeypatch 注入 `httpx.MockTransport`)、`_auth_precheck`、`_put`、`_ensure_local_mp3`、`_fallback_pub_date`。

- [ ] **Step 1: 加 httpx 直接依賴**

`pyproject.toml` 的 `dependencies` 加 `"httpx>=0.27,<1"`,然後 `cd notebooklm-skill && uv pip install -e ".[dev]"`(應零新裝,httpx 已在 lock)。

- [ ] **Step 2: 寫核心失敗測試(落地 spec §11 具名測試)**

Rewrite `tests/test_publish_tools.py`:env fixture 設 `PODCAST_PUBLIC_BASE_URL`/`PODCAST_TOKEN_SALT`/`PODCAST_UPLOAD_URL`/`PODCAST_UPLOAD_TOKEN`(**無** `PODCAST_FEEDS_ROOT`);一個 fixture monkeypatch `tools_publish._make_client` 回傳裝了 `httpx.MockTransport` 的 `AsyncClient`,transport handler 對 `GET /healthz` 回 `200` + header `X-Podcast-Uploader: 1`、對 `PUT` 收進 `captured` list 並回 `201`。至少實作 spec §11 這幾條:
`test_posts_to_upload_url_not_feeds_root`、`test_sends_upload_bearer_token`(且 ≠ feed token)、`test_puts_every_episode_artwork_state_and_derived`、`test_commit_order_media_then_state_then_derived`、`test_auth_precheck_runs_before_any_put`(healthz 回 401 或缺 marker → `ValueError`、零 PUT)、`test_publish_is_idempotent`、`test_new_mp3_bytes_new_url_stable_guid_and_pubdate`、`test_fallback_pubdate_deterministic`(每集相異 **且 EP01<EP02**、兩次 byte 一致)、`test_missing_env_errors`、`test_missing_required_metadata_errors`、`test_feed_info`、`test_manifest_preflight_rejects_bad`(episode>99 / 重複 / title 空 → `ValueError`、零 PUT)。

- [ ] **Step 3: 跑測試確認失敗**

Run: `cd notebooklm-skill && uv run pytest tests/test_publish_tools.py -q`
Expected: FAIL(舊 tools_publish 還是 FS 模型 / 新測試 import 不到新符號)。

- [ ] **Step 4: 落地 spec §5.5 的 tools_publish.py 全文**

把 `notebooklm_mcp/tools_publish.py` **整檔換成** spec §5.5 定版。核對關鍵點:import(`hashlib`、`httpx`、`from .publish.layout import media_filename`,**不** import content_hash8/state);`_fallback_pub_date` 用 `_FALLBACK_BASE + timedelta(days=n-1)`;`_auth_precheck` 驗 `status==200 AND header X-Podcast-Uploader=="1"`;`publish_series` 讀 manifest 後**先 preflight**(episode 1..99 整數/不重複/title 非空)再進上傳;`async with _make_client()` 內先 `_auth_precheck` → 逐集 `read→hash(hashlib.sha256(mp3_bytes))→_put→del mp3_bytes` → PUT artwork → 渲染 show/feed/index → PUT `show.json`→`feed.xml`→`index.html`;`feed_info(show_id)` 純計算;`feed_list` 不定義。

- [ ] **Step 5: 跑測試確認通過**

Run: `cd notebooklm-skill && uv run pytest tests/test_publish_tools.py -q`
Expected: PASS(全部具名測試)。

- [ ] **Step 6: Commit**

```bash
cd notebooklm-skill
git add notebooklm_mcp/tools_publish.py tests/test_publish_tools.py pyproject.toml uv.lock
git commit -m "feat(publish): rewrite publish_series to HTTP PUT uploader model (read/write split)"
```

---

## Task 6: 文件同步 + 全套測試綠 + 冒煙

**Repo:** `notebooklm-skill`

**Files:**
- Modify: `SKILL.md`(工具表刪 feed_list、feed_info 改純計算/參數 show_id、發布段四 secret)
- Modify: `AGENTS.md`(發布段:內網 PUT、四 secret、architecture 去 state.py)
- Modify: `README.md`(發布段:內網 PUT、四 secret、重跑=重發整季)

**Interfaces:**
- Consumes: Task 3–5 的最終工具集。
- Produces: 一致的文件 + 全綠測試(Definition of Done)。

- [ ] **Step 1: 改 SKILL.md**

工具表:刪 `feed_list` 行;`feed_info` 描述改「純計算,回 `{show_id, token, feed_url, show_page_url}`,不含各集細節」、參數 `show_id`;發布段「Auth/設定」由三 secret 改四 secret(`PODCAST_PUBLIC_BASE_URL`/`PODCAST_TOKEN_SALT`/`PODCAST_UPLOAD_URL`/`PODCAST_UPLOAD_TOKEN`,移除 `PODCAST_FEEDS_ROOT`),托管改「MCP 內網 PUT 到 NAS uploader」。

- [ ] **Step 2: 改 AGENTS.md 與 README.md 的發布段**

- `AGENTS.md`:Architecture 的 `publish/` 條目移除 `state.py`;發布設定段三 secret → 四 secret、「掛載寫入」→「內網 HTTP PUT 到 NAS uploader(讀寫分離)」。
- `README.md`:發布段同步(四 secret、內網 PUT、`publish_series` 簽章無 `PODCAST_FEEDS_ROOT`)。

- [ ] **Step 3: 跑全套測試**

Run: `cd notebooklm-skill && uv run pytest -q`
Expected: 全綠(state 測試已刪、tombstone/feed_list 測試已移除、新增 upload-api 測試通過)。

- [ ] **Step 4: 啟動冒煙(確認工具註冊、無 import 錯)**

Run:
```bash
cd notebooklm-skill && uv run python -c "
from notebooklm_mcp.app import mcp
import asyncio
names = sorted(t.name for t in asyncio.run(mcp.list_tools()))
print(names)
assert 'publish_series' in names and 'feed_info' in names, 'publish tools missing'
assert 'feed_list' not in names, 'feed_list should be removed'
print('OK')
"
```
Expected: 印出工具清單 + `OK`(`publish_series`/`feed_info` 在、`feed_list` 不在)。

- [ ] **Step 5: Commit**

```bash
cd notebooklm-skill
git add SKILL.md AGENTS.md README.md
git commit -m "docs: sync SKILL/AGENTS/README to upload-api (4 secrets, internal PUT, no feed_list)"
```

---

## Self-Review

**Spec coverage:**
- §5.1 episode_guid → Task 3 ✓；§5.2 layout → Task 3 ✓；§5.3 rss_models → Task 3 ✓；刪 state.py → Task 3 ✓
- §5.4 published_at → Task 4 ✓
- §5.5 tools_publish(PUT/preflight/precheck marker/fallback 方向/一次一 mp3)→ Task 5 ✓
- §6 uploader(server/白名單/原子寫/healthz marker/狀態碼)→ Task 1 ✓
- §8 安全(compose LAN bind、兩 token)→ Task 2(compose)+ Task 5(token 分離)✓
- §9 設定(四 secret;`PODCAST_FEEDS_ROOT` 移除)→ Task 5(讀取)+ Task 6(文件)✓
- §11 測試(MCP MockTransport + uploader handler 端到端)→ Task 5 + Task 1 ✓
- §12 部署/CI(兩 image matrix、compose 兩 service、Caddyfile 403、.env)→ Task 2 ✓

**Placeholder scan:** 實作步驟以「落地 spec §X 定版 code」指向同 repo 的 spec source of truth(非跨 task 引用);所有 test code 於 plan 內完整給出;無 TBD/TODO。

**Type consistency:** `episode_guid(show_id, n)`、`content_hash8(path)`、`media_filename(n, hash8)`、`live_episodes(show)`、`publish_series(...)`、`feed_info(show_id)`、`_auth_precheck`/`_put`/`_make_client`、wire 檔名 regex `EP\d{2}-[0-9a-f]{8}.mp3` 跨 Task 1/3/5 與 spec 一致。

**部署順序提醒(給執行者,非 code):** 先部署 NAS 兩容器(Task 1+2)並驗 `/healthz` marker,再設 Doppler `PODCAST_UPLOAD_URL`/`PODCAST_UPLOAD_TOKEN`、部署新 MCP(Task 3–6),最後才 `doppler secrets delete PODCAST_FEEDS_ROOT`(舊碼還在跑時刪會 ValueError)。
