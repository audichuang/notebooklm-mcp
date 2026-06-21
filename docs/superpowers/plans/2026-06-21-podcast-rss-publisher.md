# Podcast RSS Publisher Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在既有 NotebookLM 薄 MCP 上加一層發布工具,把 `podcast_series` 產出的整季音檔轉成 Apple-Podcast-合規的 RSS feed,寫到 NAS 對外目錄由 reverse proxy 靜態服務。

**Architecture:** 純邏輯模組(identity / layout / state / artwork / rss_models / feed)各司其職、可離線測試;`tools_publish.py` 薄包做 I/O 編排(讀 manifest、複製 mp3、原子寫狀態與 feed),掛進同一個 MCP。token 由 `HMAC(salt, show_id)` 決定性產生(無 registry 競態);媒體檔內容版本化 `EP{n}-<hash>.mp3` 且 GUID 穩定。

**Tech Stack:** Python 3.12, `notebooklm-py 0.3.4`, FastMCP, `Pillow`(artwork 驗證), 標準庫 `hmac`/`base64`/`hashlib`/`xml.sax.saxutils`/`email.utils`, pytest。

**Spec:** `docs/superpowers/specs/2026-06-21-podcast-rss-publisher-design.md`

---

## File Structure

```
notebooklm_mcp/
  publish/
    __init__.py
    identity.py     show_id 驗證 + 決定性 token(HMAC→base32)         [純]
    layout.py       內容 hash + 原子寫(temp+fsync+replace)+ 媒體檔名  [純/fs]
    state.py        show.json schema 讀寫 + guid + tombstone           [純/fs]
    artwork.py      artwork 規格驗證(Pillow)                          [純]
    rss_models.py   Channel / Item dataclass                           [純]
    feed.py         show state → RSS XML + index.html(XML escape)      [純]
  tools_publish.py  publish_series / feed_list / feed_info(I/O 編排)   [async, 用 runtime]
  app.py            +1 行註冊 tools_publish
tests/
  test_publish_identity.py
  test_publish_layout.py
  test_publish_state.py
  test_publish_artwork.py
  test_publish_feed.py
  test_publish_tools.py
pyproject.toml      + Pillow 依賴
```

每個純模組一個 Task、自含 TDD。`tools_publish.py` 在純模組就緒後編排。

---

### Task 1: 依賴與套件骨架

**Files:**
- Modify: `pyproject.toml:6-9`
- Create: `notebooklm_mcp/publish/__init__.py`

- [ ] **Step 1: 加 Pillow 到 dependencies**

把 `pyproject.toml` 的 dependencies 改成:

```toml
dependencies = [
    "notebooklm-py>=0.3,<0.4",
    "mcp[cli]>=1.0.0",
    "Pillow>=10,<12",
]
```

- [ ] **Step 2: 建立 publish 套件**

Create `notebooklm_mcp/publish/__init__.py`:

```python
"""Podcast RSS publishing layer: turn a podcast_series manifest into an
Apple-Podcast-compliant static RSS feed on the NAS.

Pure logic lives in identity/layout/state/artwork/rss_models/feed; I/O
orchestration lives in ``notebooklm_mcp.tools_publish``.
"""
```

- [ ] **Step 3: 安裝依賴**

Run: `uv pip install -e ".[dev]"`
Expected: 成功,Pillow 被裝入。

- [ ] **Step 4: 確認現有測試仍綠**

Run: `uv run pytest -q`
Expected: 既有測試全 PASS(本步驟尚未加新測試)。

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml notebooklm_mcp/publish/__init__.py
git commit -m "feat(publish): add Pillow dep + publish package skeleton"
```

---

### Task 2: identity.py — show_id 驗證 + 決定性 token

**Files:**
- Create: `notebooklm_mcp/publish/identity.py`
- Test: `tests/test_publish_identity.py`

- [ ] **Step 1: 寫失敗測試**

Create `tests/test_publish_identity.py`:

```python
import re
import pytest
from notebooklm_mcp.publish import identity


def test_make_token_is_deterministic():
    a = identity.make_token("ai-news", salt="s3cret")
    b = identity.make_token("ai-news", salt="s3cret")
    assert a == b


def test_make_token_differs_by_show_id():
    assert identity.make_token("ai-news", salt="s3cret") != identity.make_token(
        "rust-deep", salt="s3cret"
    )


def test_make_token_differs_by_salt():
    assert identity.make_token("ai-news", salt="s1") != identity.make_token(
        "ai-news", salt="s2"
    )


def test_token_charset_is_base32_lower():
    token = identity.make_token("ai-news", salt="s3cret")
    assert re.fullmatch(r"[a-z2-7]+", token)
    assert len(token) == 24  # 15 bytes -> 24 base32 chars, no padding


def test_make_token_requires_salt():
    with pytest.raises(ValueError):
        identity.make_token("ai-news", salt="")


@pytest.mark.parametrize("bad", ["", "A", "UPPER", "has space", "trailing-", "-lead", "x" * 100])
def test_validate_show_id_rejects_bad(bad):
    with pytest.raises(ValueError):
        identity.validate_show_id(bad)


@pytest.mark.parametrize("ok", ["ai-news", "rust-deep", "s2", "a1b2-c3"])
def test_validate_show_id_accepts_good(ok):
    assert identity.validate_show_id(ok) == ok
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_publish_identity.py -q`
Expected: FAIL（`module identity has no attribute ...` / import error）。

- [ ] **Step 3: 實作**

Create `notebooklm_mcp/publish/identity.py`:

```python
"""Stable feed identity. The feed's token is derived deterministically from a
stable ``show_id`` via HMAC, so the same show always maps to the same URL with
NO global registry (hence no multi-VM read-modify-write race)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import re

# Lowercase, digits, internal hyphens; 2..64 chars; no leading/trailing hyphen.
_SHOW_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])$")


def validate_show_id(show_id: str) -> str:
    if not isinstance(show_id, str) or not _SHOW_ID_RE.fullmatch(show_id):
        raise ValueError(
            "show_id must be 2-64 chars of [a-z0-9-], no leading/trailing hyphen "
            f"(got: {show_id!r})"
        )
    return show_id


def make_token(show_id: str, salt: str) -> str:
    """Deterministic, unguessable feed token: base32(lower) of HMAC-SHA256.

    15 raw bytes -> 24 base32 chars with no '=' padding, charset [a-z2-7]
    (URL/filename safe; avoids '-' '_' '+' '/')."""
    validate_show_id(show_id)
    if not salt:
        raise ValueError("PODCAST_TOKEN_SALT is required and must be non-empty")
    digest = hmac.new(salt.encode("utf-8"), show_id.encode("utf-8"), hashlib.sha256).digest()
    return base64.b32encode(digest[:15]).decode("ascii").lower().rstrip("=")
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/test_publish_identity.py -q`
Expected: PASS（全部）。

- [ ] **Step 5: Commit**

```bash
git add notebooklm_mcp/publish/identity.py tests/test_publish_identity.py
git commit -m "feat(publish): deterministic feed token from HMAC(salt, show_id)"
```

---

### Task 3: layout.py — 內容 hash + 原子寫 + 媒體檔名

**Files:**
- Create: `notebooklm_mcp/publish/layout.py`
- Test: `tests/test_publish_layout.py`

- [ ] **Step 1: 寫失敗測試**

Create `tests/test_publish_layout.py`:

```python
import os
from notebooklm_mcp.publish import layout


def test_content_hash8_is_stable_and_content_addressed(tmp_path):
    p = tmp_path / "a.mp3"
    p.write_bytes(b"hello world")
    h1 = layout.content_hash8(str(p))
    h2 = layout.content_hash8(str(p))
    assert h1 == h2
    assert len(h1) == 8 and all(c in "0123456789abcdef" for c in h1)

    p.write_bytes(b"different")
    assert layout.content_hash8(str(p)) != h1


def test_media_filename():
    assert layout.media_filename(1, "ab12cd34") == "EP01-ab12cd34.mp3"
    assert layout.media_filename(12, "deadbeef") == "EP12-deadbeef.mp3"


def test_atomic_write_text_creates_dirs_and_no_tmp_left(tmp_path):
    target = tmp_path / "feeds" / "tok" / "feed.xml"
    layout.atomic_write_text(str(target), "<rss/>")
    assert target.read_text() == "<rss/>"
    # No leftover temp files in the directory.
    assert [n for n in os.listdir(target.parent) if n.startswith(".tmp-")] == []


def test_atomic_copy_matches_bytes(tmp_path):
    src = tmp_path / "src.mp3"
    src.write_bytes(b"\x00\x01\x02audio")
    dst = tmp_path / "out" / "EP01-aaaaaaaa.mp3"
    layout.atomic_copy(str(src), str(dst))
    assert dst.read_bytes() == b"\x00\x01\x02audio"
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_publish_layout.py -q`
Expected: FAIL（import error / 屬性不存在）。

- [ ] **Step 3: 實作**

Create `notebooklm_mcp/publish/layout.py`:

```python
"""File layout + atomic writes. Every write goes temp -> fsync -> os.replace so
a static file server never serves a half-written file. Media filenames are
content-addressed (EP{n}-{hash8}.mp3) so regenerated episodes get a NEW url
while the GUID stays stable (clients re-download but treat it as the same ep)."""
from __future__ import annotations

import hashlib
import os
import uuid

_CHUNK = 1 << 20


def content_hash8(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()[:8]


def media_filename(episode_n: int, hash8: str) -> str:
    return f"EP{episode_n:02d}-{hash8}.mp3"


def atomic_write_bytes(path: str, data: bytes) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    # Unique temp name so concurrent writers to the same target never collide
    # on the temp file (a fixed name would let one writer's os.replace race the
    # other's temp). pid + uuid is unique per process and per call.
    tmp = os.path.join(directory, f".tmp-{os.path.basename(path)}-{os.getpid()}-{uuid.uuid4().hex}")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    # Best-effort directory fsync so the rename itself is durable.
    try:
        dfd = os.open(directory, os.O_DIRECTORY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


def atomic_write_text(path: str, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def atomic_copy(src: str, dst: str) -> None:
    with open(src, "rb") as f:
        data = f.read()
    atomic_write_bytes(dst, data)
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/test_publish_layout.py -q`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add notebooklm_mcp/publish/layout.py tests/test_publish_layout.py
git commit -m "feat(publish): content-hashed media names + atomic write helpers"
```

---

### Task 4: state.py — show.json 權威狀態 + guid + tombstone

**Files:**
- Create: `notebooklm_mcp/publish/state.py`
- Test: `tests/test_publish_state.py`

- [ ] **Step 1: 寫失敗測試**

Create `tests/test_publish_state.py`:

```python
import json
from notebooklm_mcp.publish import state


def test_episode_guid_is_stable_and_source_decoupled():
    g1 = state.episode_guid("ai-news", 1)
    g2 = state.episode_guid("ai-news", 1)
    assert g1 == g2
    assert g1 != state.episode_guid("ai-news", 2)
    assert g1 != state.episode_guid("other", 1)


def test_load_show_returns_none_when_absent(tmp_path):
    assert state.load_show(str(tmp_path)) is None


def test_save_then_load_round_trips(tmp_path):
    show = {
        "show_id": "ai-news",
        "token": "tok",
        "notebook_id": "nb1",
        "title": "AI 新聞",
        "episodes": {"1": {"title": "EP1", "guid": "g1", "tombstone": False}},
    }
    state.save_show(str(tmp_path), show)
    loaded = state.load_show(str(tmp_path))
    assert loaded == show
    # File is real JSON, UTF-8, human-readable.
    raw = json.loads((tmp_path / "show.json").read_text(encoding="utf-8"))
    assert raw["title"] == "AI 新聞"


def test_save_show_is_atomic_no_tmp_left(tmp_path):
    state.save_show(str(tmp_path), {"show_id": "x", "episodes": {}})
    import os
    assert [n for n in os.listdir(tmp_path) if n.startswith(".tmp-")] == []
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_publish_state.py -q`
Expected: FAIL。

- [ ] **Step 3: 實作**

Create `notebooklm_mcp/publish/state.py`:

```python
"""Authoritative per-show state (show.json). The feed.xml/index.html are derived
FROM this file, so we write state first (atomically) then render outputs from the
committed state. GUIDs are derived from (show_id, episode_n) and never change."""
from __future__ import annotations

import hashlib
import json
import os

from . import layout

SHOW_JSON = "show.json"


def episode_guid(show_id: str, episode_n: int) -> str:
    return hashlib.sha1(f"{show_id}:{episode_n}".encode("utf-8")).hexdigest()


def load_show(feed_dir: str) -> dict | None:
    path = os.path.join(feed_dir, SHOW_JSON)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_show(feed_dir: str, show: dict) -> None:
    path = os.path.join(feed_dir, SHOW_JSON)
    text = json.dumps(show, ensure_ascii=False, indent=2, sort_keys=False)
    layout.atomic_write_text(path, text)
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/test_publish_state.py -q`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add notebooklm_mcp/publish/state.py tests/test_publish_state.py
git commit -m "feat(publish): authoritative show.json state + stable episode guid"
```

---

### Task 5: artwork.py — Apple Show Cover 規格驗證

**Files:**
- Create: `notebooklm_mcp/publish/artwork.py`
- Test: `tests/test_publish_artwork.py`

- [ ] **Step 1: 寫失敗測試**

Create `tests/test_publish_artwork.py`:

```python
import pytest
from PIL import Image
from notebooklm_mcp.publish import artwork


def _make(tmp_path, name, size, mode="RGB", fmt="PNG"):
    p = tmp_path / name
    Image.new(mode, size).save(str(p), format=fmt)
    return str(p)


def test_valid_square_png_passes(tmp_path):
    info = artwork.validate_artwork(_make(tmp_path, "ok.png", (1500, 1500)))
    assert info["width"] == 1500 and info["height"] == 1500
    assert info["format"] == "PNG"


def test_valid_jpeg_passes(tmp_path):
    info = artwork.validate_artwork(_make(tmp_path, "ok.jpg", (1400, 1400), fmt="JPEG"))
    assert info["format"] == "JPEG"


def test_non_square_rejected(tmp_path):
    with pytest.raises(ValueError, match="square"):
        artwork.validate_artwork(_make(tmp_path, "rect.png", (1500, 1400)))


def test_too_small_rejected(tmp_path):
    with pytest.raises(ValueError, match="1400"):
        artwork.validate_artwork(_make(tmp_path, "small.png", (1000, 1000)))


def test_too_large_rejected(tmp_path):
    with pytest.raises(ValueError, match="3000"):
        artwork.validate_artwork(_make(tmp_path, "big.png", (3200, 3200)))


def test_alpha_channel_rejected(tmp_path):
    with pytest.raises(ValueError, match="alpha"):
        artwork.validate_artwork(_make(tmp_path, "rgba.png", (1500, 1500), mode="RGBA"))


def test_grayscale_rejected(tmp_path):
    with pytest.raises(ValueError, match="RGB"):
        artwork.validate_artwork(_make(tmp_path, "gray.png", (1500, 1500), mode="L"))


def test_cmyk_rejected(tmp_path):
    with pytest.raises(ValueError, match="RGB"):
        artwork.validate_artwork(_make(tmp_path, "cmyk.jpg", (1500, 1500), mode="CMYK", fmt="JPEG"))


def test_wrong_format_rejected(tmp_path):
    with pytest.raises(ValueError, match="PNG or JPEG"):
        artwork.validate_artwork(_make(tmp_path, "x.gif", (1500, 1500), fmt="GIF"))


def test_missing_file_rejected(tmp_path):
    with pytest.raises(ValueError, match="not found"):
        artwork.validate_artwork(str(tmp_path / "nope.png"))
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_publish_artwork.py -q`
Expected: FAIL。

- [ ] **Step 3: 實作**

Create `notebooklm_mcp/publish/artwork.py`:

```python
"""Validate podcast artwork against Apple's Show Cover requirements:
square, 1400-3000 px per side, PNG or JPEG, RGB, no alpha channel."""
from __future__ import annotations

import os

from PIL import Image

_MIN, _MAX = 1400, 3000


def validate_artwork(path: str) -> dict:
    if not os.path.exists(path):
        raise ValueError(f"artwork file not found: {path}")
    try:
        with Image.open(path) as img:
            fmt = img.format
            width, height = img.size
            bands = img.getbands()
            mode = img.mode
    except OSError as exc:
        raise ValueError(f"artwork is not a readable image: {exc}") from None

    if fmt not in ("PNG", "JPEG"):
        raise ValueError(f"artwork must be PNG or JPEG (got: {fmt})")
    if width != height:
        raise ValueError(f"artwork must be square (got: {width}x{height})")
    if width < _MIN:
        raise ValueError(f"artwork side must be >= {_MIN}px (got: {width})")
    if width > _MAX:
        raise ValueError(f"artwork side must be <= {_MAX}px (got: {width})")
    if "A" in bands:
        raise ValueError("artwork must not have an alpha channel (use flat RGB)")
    if mode != "RGB":
        # Reject grayscale (L), CMYK, palette (P) etc. Apple wants flat RGB.
        raise ValueError(f"artwork must be RGB color space (got mode: {mode})")

    return {"width": width, "height": height, "format": fmt}
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/test_publish_artwork.py -q`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add notebooklm_mcp/publish/artwork.py tests/test_publish_artwork.py
git commit -m "feat(publish): Apple Show Cover artwork validation"
```

---

### Task 6: rss_models.py + feed.py — RSS XML + index.html

**Files:**
- Create: `notebooklm_mcp/publish/rss_models.py`
- Create: `notebooklm_mcp/publish/feed.py`
- Test: `tests/test_publish_feed.py`

- [ ] **Step 1: 寫失敗測試**

Create `tests/test_publish_feed.py`:

```python
import xml.etree.ElementTree as ET
from notebooklm_mcp.publish import feed

NS = {
    "itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd",
    "atom": "http://www.w3.org/2005/Atom",
}

SHOW = {
    "show_id": "ai-news",
    "token": "tok123",
    "title": "AI 新聞 & 觀察",  # 含 & 測 XML escape
    "description": "每日 AI 摘要",
    "language": "zh-Hant",
    "author": "Audi",
    "owner_name": "Audi",
    "owner_email": "audi@example.com",
    "category": "Technology",
    "explicit": False,
    "artwork_file": "artwork.png",
    "episodes": {
        "1": {
            "title": "心法篇",
            "description": "心法篇",
            "guid": "g1",
            "pub_date": "Sat, 21 Jun 2026 09:00:00 +0800",
            "media_file": "EP01-ab12cd34.mp3",
            "length": 12345,
            "tombstone": False,
        },
        "2": {
            "title": "實戰篇 <重點>",  # 含 < 測 escape
            "description": "實戰篇",
            "guid": "g2",
            "pub_date": "Sun, 22 Jun 2026 09:00:00 +0800",
            "media_file": "EP02-deadbeef.mp3",
            "length": 22222,
            "tombstone": False,
        },
        "3": {  # tombstone:不應出現在 feed
            "title": "壞集",
            "guid": "g3",
            "pub_date": "Mon, 23 Jun 2026 09:00:00 +0800",
            "media_file": "EP03-00000000.mp3",
            "length": 1,
            "tombstone": True,
        },
    },
}
BASE = "https://podcast.example"


def _feed():
    return ET.fromstring(feed.build_feed_xml(SHOW, BASE))


def test_channel_required_fields():
    ch = _feed().find("channel")
    assert ch.findtext("title") == "AI 新聞 & 觀察"
    assert ch.findtext("description") == "每日 AI 摘要"
    assert ch.findtext("language") == "zh-Hant"
    assert ch.findtext("itunes:author", namespaces=NS) == "Audi"
    owner = ch.find("itunes:owner", NS)
    assert owner.findtext("itunes:email", namespaces=NS) == "audi@example.com"
    assert ch.find("itunes:image", NS).get("href") == f"{BASE}/feeds/tok123/artwork.png"
    assert ch.findtext("itunes:explicit", namespaces=NS) == "false"  # 小寫
    assert ch.find("atom:link", NS).get("href") == f"{BASE}/feeds/tok123/feed.xml"
    assert ch.findtext("link") == f"{BASE}/feeds/tok123/index.html"
    # lastBuildDate = 最後一集 live(EP02;EP03 tombstone)的 pubDate,穩定
    assert ch.findtext("lastBuildDate") == "Sun, 22 Jun 2026 09:00:00 +0800"


def test_items_exclude_tombstones_and_escape_xml():
    items = _feed().find("channel").findall("item")
    assert len(items) == 2  # tombstone EP03 排除
    ep1 = items[0]
    assert ep1.findtext("title") == "心法篇"
    assert ep1.findtext("guid") == "g1"
    assert ep1.find("guid").get("isPermaLink") == "false"
    enc = ep1.find("enclosure")
    assert enc.get("url") == f"{BASE}/feeds/tok123/EP01-ab12cd34.mp3"
    assert enc.get("length") == "12345"
    assert enc.get("type") == "audio/mpeg"
    assert ep1.findtext("pubDate") == "Sat, 21 Jun 2026 09:00:00 +0800"
    # XML escaping round-trips: parser yields the raw chars.
    assert items[1].findtext("title") == "實戰篇 <重點>"


def test_namespaces_declared():
    raw = feed.build_feed_xml(SHOW, BASE)
    assert 'xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"' in raw
    assert 'xmlns:content="http://purl.org/rss/1.0/modules/content/"' in raw
    assert 'xmlns:atom="http://www.w3.org/2005/Atom"' in raw
    assert raw.startswith("<?xml")


def test_index_html_lists_live_episodes_only():
    html = feed.build_index_html(SHOW, BASE)
    assert "心法篇" in html and "實戰篇" in html
    assert "壞集" not in html  # tombstone 不列
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_publish_feed.py -q`
Expected: FAIL。

- [ ] **Step 3a: 實作 rss_models.py**

Create `notebooklm_mcp/publish/rss_models.py`:

```python
"""Plain helpers for selecting live (non-tombstone) episodes in episode order."""
from __future__ import annotations


def live_episodes(show: dict) -> list[tuple[int, dict]]:
    """Return [(episode_n, ep_dict), ...] sorted by episode number, excluding
    tombstoned episodes."""
    out: list[tuple[int, dict]] = []
    for key, ep in show.get("episodes", {}).items():
        if ep.get("tombstone"):
            continue
        out.append((int(key), ep))
    out.sort(key=lambda pair: pair[0])
    return out
```

- [ ] **Step 3b: 實作 feed.py**

Create `notebooklm_mcp/publish/feed.py`:

```python
"""Render the authoritative show state into an RSS 2.0 feed (with the iTunes +
atom namespaces Apple Podcasts needs) and a minimal index.html for <link>.
Pure function of (show, base_url): no I/O, no clock — fully deterministic."""
from __future__ import annotations

from xml.sax.saxutils import escape, quoteattr

from .rss_models import live_episodes

_ITUNES = "http://www.itunes.com/dtds/podcast-1.0.dtd"
_CONTENT = "http://purl.org/rss/1.0/modules/content/"
_ATOM = "http://www.w3.org/2005/Atom"


def _feed_dir_url(base_url: str, token: str) -> str:
    return f"{base_url.rstrip('/')}/feeds/{token}"


def build_feed_xml(show: dict, base_url: str) -> str:
    token = show["token"]
    base = _feed_dir_url(base_url, token)
    feed_url = f"{base}/feed.xml"
    explicit = "true" if show.get("explicit") else "false"
    eps = live_episodes(show)

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<rss version="2.0" xmlns:itunes="{_ITUNES}" '
        f'xmlns:content="{_CONTENT}" xmlns:atom="{_ATOM}">',
        "  <channel>",
        f"    <title>{escape(show['title'])}</title>",
        f"    <link>{escape(base + '/index.html')}</link>",
        f"    <description>{escape(show['description'])}</description>",
        f"    <language>{escape(show.get('language', 'zh-Hant'))}</language>",
        f"    <itunes:author>{escape(show['author'])}</itunes:author>",
        f"    <itunes:summary>{escape(show['description'])}</itunes:summary>",
        f"    <itunes:explicit>{explicit}</itunes:explicit>",
        f'    <itunes:category text={quoteattr(show.get("category", "Technology"))}/>',
        f'    <itunes:image href={quoteattr(base + "/" + show["artwork_file"])}/>',
        "    <itunes:owner>",
        f"      <itunes:name>{escape(show['owner_name'])}</itunes:name>",
        f"      <itunes:email>{escape(show['owner_email'])}</itunes:email>",
        "    </itunes:owner>",
        f'    <atom:link href={quoteattr(feed_url)} rel="self" type="application/rss+xml"/>',
    ]

    # lastBuildDate = the most recent live episode's (stable) pubDate, so the feed
    # stays byte-identical across rebuilds when nothing changed (no wall clock).
    if eps:
        lines.append(f"    <lastBuildDate>{escape(eps[-1][1]['pub_date'])}</lastBuildDate>")

    for n, ep in eps:
        media_url = f"{base}/{ep['media_file']}"
        lines += [
            "    <item>",
            f"      <title>{escape(ep['title'])}</title>",
            f"      <description>{escape(ep.get('description', ep['title']))}</description>",
            f"      <pubDate>{escape(ep['pub_date'])}</pubDate>",
            f'      <guid isPermaLink="false">{escape(ep["guid"])}</guid>',
            f'      <enclosure url={quoteattr(media_url)} length="{int(ep["length"])}" type="audio/mpeg"/>',
            "    </item>",
        ]

    lines += ["  </channel>", "</rss>", ""]
    return "\n".join(lines)


def build_index_html(show: dict, base_url: str) -> str:
    base = _feed_dir_url(base_url, show["token"])
    rows = "\n".join(
        f"    <li>EP{n:02d} — {escape(ep['title'])} "
        f'(<a href={quoteattr(base + "/" + ep["media_file"])}>mp3</a>)</li>'
        for n, ep in live_episodes(show)
    )
    return (
        "<!doctype html>\n"
        '<html lang="zh-Hant"><head><meta charset="utf-8">\n'
        f"<title>{escape(show['title'])}</title></head>\n"
        f"<body>\n  <h1>{escape(show['title'])}</h1>\n"
        f"  <p>{escape(show['description'])}</p>\n"
        f'  <p>RSS: <a href={quoteattr(base + "/feed.xml")}>{escape(base)}/feed.xml</a></p>\n'
        f"  <ul>\n{rows}\n  </ul>\n</body></html>\n"
    )
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/test_publish_feed.py -q`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add notebooklm_mcp/publish/rss_models.py notebooklm_mcp/publish/feed.py tests/test_publish_feed.py
git commit -m "feat(publish): RSS 2.0 + iTunes feed and index.html rendering"
```

---

### Task 7: tools_publish.py — publish_series / feed_list / feed_info 編排

**Files:**
- Create: `notebooklm_mcp/tools_publish.py`
- Test: `tests/test_publish_tools.py`

說明:`publish_series` 是 async MCP 工具。各純模組已測過,本 Task 測「編排」:
讀 manifest 檔、複製 mp3、套 guid/pubDate 穩定性、寫 show.json + feed.xml。
mp3 都存在時不需 NotebookLM client(只有檔案遺失才走 artifact 重抓)。測試走「檔案都在」路徑。

- [ ] **Step 1: 寫失敗測試**

Create `tests/test_publish_tools.py`:

```python
import json
import os
import xml.etree.ElementTree as ET

import pytest

from notebooklm_mcp import tools_publish

NS = {"itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    feeds_root = tmp_path / "nas"
    monkeypatch.setenv("PODCAST_FEEDS_ROOT", str(feeds_root))
    monkeypatch.setenv("PODCAST_PUBLIC_BASE_URL", "https://podcast.example")
    monkeypatch.setenv("PODCAST_TOKEN_SALT", "s3cret")
    return feeds_root


@pytest.fixture
def artwork_png(tmp_path):
    from PIL import Image
    p = tmp_path / "art.png"
    Image.new("RGB", (1500, 1500)).save(str(p))
    return str(p)


def _manifest(tmp_path):
    out = tmp_path / "series"
    out.mkdir(exist_ok=True)
    for n in (1, 2):
        (out / f"ep{n:02d}.mp3").write_bytes(f"audio-{n}".encode())
    manifest = {
        "notebook_id": "nb1",
        "episodes": [
            {"episode": 1, "title": "心法篇", "label": "EP01 心法篇",
             "task_id": "t1", "artifact_id": "a1", "mp3_path": str(out / "ep01.mp3")},
            {"episode": 2, "title": "實戰篇", "label": "EP02 實戰篇",
             "task_id": "t2", "artifact_id": "a2", "mp3_path": str(out / "ep02.mp3")},
        ],
    }
    mpath = out / "series_manifest.json"
    mpath.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return str(mpath)


async def _publish(tmp_path, artwork_png):
    return await tools_publish.publish_series(
        show_id="ai-news",
        notebook_id="nb1",
        manifest_path=_manifest(tmp_path),
        show_title="AI 新聞",
        show_description="每日 AI 摘要",
        author="Audi",
        owner_name="Audi",
        owner_email="audi@example.com",
        artwork_path=artwork_png,
    )


async def test_publish_creates_feed_and_media(env, tmp_path, artwork_png):
    res = await _publish(tmp_path, artwork_png)
    token = res["token"]
    feed_dir = env / "feeds" / token
    assert (feed_dir / "feed.xml").exists()
    assert (feed_dir / "index.html").exists()
    assert (feed_dir / "artwork.png").exists()
    # content-hashed media files
    mp3s = sorted(p for p in os.listdir(feed_dir) if p.endswith(".mp3"))
    assert len(mp3s) == 2 and mp3s[0].startswith("EP01-")
    assert res["episode_count"] == 2
    assert res["feed_url"] == f"https://podcast.example/feeds/{token}/feed.xml"

    ch = ET.fromstring((feed_dir / "feed.xml").read_text()).find("channel")
    assert ch.findtext("title") == "AI 新聞"
    assert len(ch.findall("item")) == 2


async def test_publish_is_idempotent(env, tmp_path, artwork_png):
    r1 = await _publish(tmp_path, artwork_png)
    feed1 = (env / "feeds" / r1["token"] / "feed.xml").read_text()
    r2 = await _publish(tmp_path, artwork_png)
    feed2 = (env / "feeds" / r2["token"] / "feed.xml").read_text()
    assert r1["token"] == r2["token"]
    assert feed1 == feed2  # 同 guid/pubDate/URL → 完全一致


async def test_regenerated_episode_gets_new_url_same_guid(env, tmp_path, artwork_png):
    r1 = await _publish(tmp_path, artwork_png)
    feed_dir = env / "feeds" / r1["token"]
    before = ET.fromstring((feed_dir / "feed.xml").read_text())
    g1_before = before.find("channel").find("item").findtext("guid")
    pub_before = before.find("channel").find("item").findtext("pubDate")

    # Regenerate EP01 with different content, same manifest shape.
    mpath = _manifest(tmp_path)
    data = json.loads(open(mpath, encoding="utf-8").read())
    ep1_path = data["episodes"][0]["mp3_path"]
    open(ep1_path, "wb").write(b"audio-1-REGENERATED")
    await tools_publish.publish_series(
        show_id="ai-news", notebook_id="nb1", manifest_path=mpath,
        show_title="AI 新聞", show_description="每日 AI 摘要", author="Audi",
        owner_name="Audi", owner_email="audi@example.com", artwork_path=artwork_png,
    )
    after = ET.fromstring((feed_dir / "feed.xml").read_text())
    ep1 = after.find("channel").find("item")
    assert ep1.findtext("guid") == g1_before          # GUID 穩定
    assert ep1.findtext("pubDate") == pub_before        # pubDate 穩定
    # enclosure URL 變了(內容 hash 不同)
    assert ep1.find("enclosure").get("url") != before.find("channel").find("item").find("enclosure").get("url")


async def test_missing_required_metadata_errors(env, tmp_path, artwork_png):
    with pytest.raises(ValueError, match="show_description"):
        await tools_publish.publish_series(
            show_id="ai-news", notebook_id="nb1", manifest_path=_manifest(tmp_path),
            show_title="AI 新聞", show_description="", author="Audi",
            owner_name="Audi", owner_email="audi@example.com", artwork_path=artwork_png,
        )


async def test_missing_env_errors(tmp_path, artwork_png, monkeypatch):
    monkeypatch.delenv("PODCAST_FEEDS_ROOT", raising=False)
    monkeypatch.setenv("PODCAST_PUBLIC_BASE_URL", "https://x")
    monkeypatch.setenv("PODCAST_TOKEN_SALT", "s")
    with pytest.raises(ValueError, match="PODCAST_FEEDS_ROOT"):
        await _publish(tmp_path, artwork_png)


async def test_feed_list_and_info(env, tmp_path, artwork_png):
    res = await _publish(tmp_path, artwork_png)
    listing = await tools_publish.feed_list()
    assert any(s["show_id"] == "ai-news" for s in listing)
    info = await tools_publish.feed_info("ai-news")
    assert info["token"] == res["token"]
    assert len(info["episodes"]) == 2
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_publish_tools.py -q`
Expected: FAIL（`tools_publish` 不存在）。

- [ ] **Step 3: 實作**

Create `notebooklm_mcp/tools_publish.py`:

```python
"""Podcast publishing MCP tools. Thin I/O orchestration over the pure publish
modules. publish_series is the one-command "publish a whole season" entry point.

Commit order (crash-safe): write all media files -> write show.json (authoritative
state) -> render feed.xml + index.html from the committed state."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

from . import runtime
from .app import mcp
from .publish import artwork as artwork_mod
from .publish import feed as feed_mod
from .publish import identity, layout, state

_TZ = timezone(timedelta(hours=8))  # Asia/Taipei, RFC-2822 +0800


def _require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise ValueError(f"{name} is required (inject via Doppler) but is unset/empty")
    return val


def _require(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required and must be non-empty")
    return value


def _now_rfc2822() -> str:
    from email.utils import format_datetime
    return format_datetime(datetime.now(_TZ))


async def _ensure_local_mp3(notebook_id: str, ep: dict) -> str:
    """Return a local path to the episode mp3. If the manifest's mp3_path is gone
    (output_dir cleaned), re-download via artifact_id; if that fails, error out."""
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


@mcp.tool()
async def publish_series(
    show_id: str,
    notebook_id: str,
    show_description: str,
    author: str,
    owner_name: str,
    owner_email: str,
    artwork_path: str,
    manifest_path: str,
    show_title: str,
    category: str = "Technology",
    explicit: bool = False,
    episode_overrides: dict | None = None,
) -> dict:
    """Publish a whole podcast series (one topic = one feed) as a static RSS feed.

    Reads the series_manifest.json, copies each episode mp3 to a content-hashed
    public filename, and renders an Apple-compliant feed.xml + index.html under
    a deterministic token directory. Idempotent: re-running keeps the same URL,
    GUIDs, and pubDates; only new/changed episodes move."""
    base_url = _require_env("PODCAST_PUBLIC_BASE_URL")
    feeds_root = _require_env("PODCAST_FEEDS_ROOT")
    salt = _require_env("PODCAST_TOKEN_SALT")

    identity.validate_show_id(show_id)
    _require(notebook_id, "notebook_id")
    _require(show_description, "show_description")
    _require(show_title, "show_title")
    _require(author, "author")
    _require(owner_name, "owner_name")
    _require(owner_email, "owner_email")
    # Validate artwork once, up front (fail-fast before any writes). Pick the
    # output extension from the real format so a JPEG is never served as .png.
    art_info = artwork_mod.validate_artwork(artwork_path)
    artwork_file = "artwork.jpg" if art_info["format"] == "JPEG" else "artwork.png"

    token = identity.make_token(show_id, salt)
    feed_dir = os.path.join(feeds_root, "feeds", token)

    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)
    manifest_eps = manifest.get("episodes", [])
    if not manifest_eps:
        raise ValueError(f"manifest has no episodes: {manifest_path}")

    overrides = episode_overrides or {}
    prior = state.load_show(feed_dir) or {"episodes": {}}
    prior_eps = prior.get("episodes", {})

    # 1) Write all media files first (content-hashed, atomic).
    new_eps: dict[str, dict] = {}
    published = []
    for ep in manifest_eps:
        n = int(ep["episode"])
        local = await _ensure_local_mp3(notebook_id, ep)
        hash8 = layout.content_hash8(local)
        media_file = layout.media_filename(n, hash8)
        dst = os.path.join(feed_dir, media_file)
        if not os.path.exists(dst):
            layout.atomic_copy(local, dst)
        length = os.path.getsize(dst)

        prev = prior_eps.get(str(n), {})
        ov = overrides.get(n) or overrides.get(str(n)) or {}
        new_eps[str(n)] = {
            "title": ep["title"],
            "description": ov.get("description", ep["title"]),
            "guid": prev.get("guid") or state.episode_guid(show_id, n),
            "pub_date": prev.get("pub_date") or _now_rfc2822(),
            "media_file": media_file,
            "length": length,
            "tombstone": False,
        }
        published.append({"n": n, "title": ep["title"],
                          "url": f"{base_url.rstrip('/')}/feeds/{token}/{media_file}",
                          "guid": new_eps[str(n)]["guid"]})

    # Episodes that existed before but are no longer in the manifest -> tombstone
    # (keep their state + file so already-cached clients don't 404; never reuse n).
    for key, prev in prior_eps.items():
        if key not in new_eps:
            tomb = dict(prev)
            tomb["tombstone"] = True
            new_eps[key] = tomb

    # 2) Write authoritative state.
    show = {
        "show_id": show_id,
        "token": token,
        "notebook_id": notebook_id,
        "title": show_title.strip(),
        "description": show_description,
        "language": "zh-Hant",
        "author": author,
        "owner_name": owner_name,
        "owner_email": owner_email,
        "category": category,
        "explicit": bool(explicit),
        "artwork_file": artwork_file,
        "episodes": new_eps,
    }
    layout.atomic_copy(artwork_path, os.path.join(feed_dir, artwork_file))
    state.save_show(feed_dir, show)

    # 3) Render outputs from committed state.
    layout.atomic_write_text(os.path.join(feed_dir, "feed.xml"),
                             feed_mod.build_feed_xml(show, base_url))
    layout.atomic_write_text(os.path.join(feed_dir, "index.html"),
                             feed_mod.build_index_html(show, base_url))

    return {
        "feed_url": f"{base_url.rstrip('/')}/feeds/{token}/feed.xml",
        "show_page_url": f"{base_url.rstrip('/')}/feeds/{token}/index.html",
        "token": token,
        "episode_count": len([e for e in new_eps.values() if not e.get("tombstone")]),
        "episodes": sorted(published, key=lambda e: e["n"]),
    }


@mcp.tool()
async def feed_list() -> list:
    """List all published shows by scanning feeds/*/show.json."""
    feeds_root = _require_env("PODCAST_FEEDS_ROOT")
    base_url = _require_env("PODCAST_PUBLIC_BASE_URL")
    root = os.path.join(feeds_root, "feeds")
    shows = []
    if os.path.isdir(root):
        for token in sorted(os.listdir(root)):
            show = state.load_show(os.path.join(root, token))
            if not show:
                continue
            shows.append({
                "show_id": show.get("show_id"),
                "show_title": show.get("title"),
                "token": show.get("token", token),
                "feed_url": f"{base_url.rstrip('/')}/feeds/{token}/feed.xml",
                "episode_count": len(
                    [e for e in show.get("episodes", {}).values() if not e.get("tombstone")]
                ),
            })
    return shows


@mcp.tool()
async def feed_info(show_id_or_token: str) -> dict:
    """Return full state for a show, looked up by show_id (preferred) or token."""
    feeds_root = _require_env("PODCAST_FEEDS_ROOT")
    salt = _require_env("PODCAST_TOKEN_SALT")
    # Try as show_id first (deterministic token), then as a raw token directory.
    candidates = []
    try:
        candidates.append(identity.make_token(show_id_or_token, salt))
    except ValueError:
        pass
    candidates.append(show_id_or_token)
    for token in candidates:
        show = state.load_show(os.path.join(feeds_root, "feeds", token))
        if show:
            return show
    raise ValueError(f"no published show found for: {show_id_or_token}")
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/test_publish_tools.py -q`
Expected: PASS（全部 7 個)。

注意:`feed_info` / `feed_list` / `publish_series` 是 async,測試檔靠 `pyproject.toml`
的 `asyncio_mode = "auto"`(已設定)自動跑 async 測試,無需額外 decorator。

- [ ] **Step 5: Commit**

```bash
git add notebooklm_mcp/tools_publish.py tests/test_publish_tools.py
git commit -m "feat(publish): publish_series/feed_list/feed_info orchestration tools"
```

---

### Task 8: 註冊工具 + 全套測試 + 文件

**Files:**
- Modify: `notebooklm_mcp/app.py:41`
- Modify: `README.md`(使用段)
- Modify: `CLAUDE.md`(Architecture + 設定段)

- [ ] **Step 1: 在 app.py 註冊 tools_publish**

把 `notebooklm_mcp/app.py:41` 那行:

```python
from . import tools_basic, tools_podcast  # noqa: E402,F401
```

改成:

```python
from . import tools_basic, tools_podcast, tools_publish  # noqa: E402,F401
```

- [ ] **Step 2: 跑全套測試**

Run: `uv run pytest -q`
Expected: 既有 51 + 新增(identity/layout/state/artwork/feed/tools)全 PASS。

- [ ] **Step 3: 啟動冒煙測試(確認工具註冊、無 import 錯)**

Run:
```bash
timeout 12 doppler run -p notebooklm -c dev -- \
  uv run python -m notebooklm_mcp.server --transport stdio < /dev/null 2>&1 | tail -5
```
Expected: 無 traceback / import error(會看到 NotebookLM 200 OK 或正常啟動日誌)。
若 Doppler 尚未有 `PODCAST_*` 三個 secret,server 仍應正常啟動(只有呼叫 publish 工具時才需要)。

- [ ] **Step 4: 更新文件**

在 `README.md` 的「使用」段後補一節,在 `CLAUDE.md` 的 Architecture 加 `tools_publish.py`
與 publish 套件、在設定段補三個 `PODCAST_*` 變數:

`README.md` 新增段落:

```markdown
### 發布成 podcast RSS(Apple Podcast 訂閱)

一主題 = 一節目 = 一 feed。跑完 `podcast_series` 後:

1. `publish_series(show_id, notebook_id, show_description, author, owner_name, owner_email, artwork_path, ...)`
   - `show_id`:穩定 slug(feed identity,決定 URL,**永不改**)
   - `artwork_path`:正方形 1400–3000px、PNG/JPG、無 alpha(Apple Show Cover 規格)
2. 回傳 `feed_url` → 在 Apple Podcast「加入節目(用 URL)」貼上即可訂閱。

需在 Doppler 設三個 secret:`PODCAST_PUBLIC_BASE_URL`、`PODCAST_FEEDS_ROOT`、`PODCAST_TOKEN_SALT`。
reverse proxy 需把 `{BASE}/feeds/` 指向 `$PODCAST_FEEDS_ROOT/feeds/`(靜態服務)。
部署驗收見設計文件「部署驗收」段。
```

`CLAUDE.md` 設定段補:

```markdown
### 發布(podcast RSS)設定

publish 工具讀三個 Doppler secret:
- `PODCAST_PUBLIC_BASE_URL`(公開 URL 根)
- `PODCAST_FEEDS_ROOT`(NAS 輸出目錄,MCP 所在機掛載得到)
- `PODCAST_TOKEN_SALT`(token HMAC salt;洩漏會讓 feed URL 可被推算)
```

- [ ] **Step 5: Commit**

```bash
git add notebooklm_mcp/app.py README.md CLAUDE.md
git commit -m "feat(publish): register publish tools + docs for podcast RSS feed"
```

---

## Self-Review

**Spec coverage:**
- token 決定性 / 無 registry → Task 2 ✓
- 原子寫 + 內容版本化檔名 → Task 3 ✓
- show.json 權威 state + guid + tombstone → Task 4 / Task 7 ✓
- artwork 規格驗證 → Task 5 ✓
- Apple 合規 RSS(namespaces/atom self/explicit 小寫/owner email/image)→ Task 6 ✓
- index.html for `<link>` → Task 6 ✓
- 必填 metadata + fail-fast → Task 7 ✓
- mp3_path 不存在 → artifact_id 重抓 fallback → Task 7 `_ensure_local_mp3` ✓
- 冪等 / 壞集重生新 URL 同 guid → Task 7 測試 ✓
- 讀 manifest **檔案**(非 run 回傳)→ Task 7 ✓
- feed_list 掃描 / feed_info → Task 7 ✓
- 提交順序(媒體→state→render)→ Task 7 ✓
- 部署驗收 → 文件(非程式碼,Task 8 指向設計文件)✓
- 設定三變數 → Task 7 `_require_env` + Task 8 文件 ✓

**Placeholder scan:** 無 TBD/TODO;每個 code step 都有完整程式碼。

**Type consistency:** `make_token(show_id, salt)`、`content_hash8`、`media_filename`、
`atomic_write_text`、`atomic_copy`、`load_show`/`save_show`、`episode_guid`、
`build_feed_xml(show, base_url)`、`build_index_html(show, base_url)`、`live_episodes(show)`
跨 Task 命名一致;`tools_publish` 的呼叫與各模組簽名相符。

**已知取捨(非阻擋):**
- `atomic_copy` 一次讀整個 mp3 進記憶體;podcast 單集數十 MB 可接受,若日後檔案極大再改串流。
- 原子寫的 temp 檔名帶 pid+uuid,併發寫同一 target 不撞 temp;最終 `os.replace` 為最後寫入者勝,
  同一全新 show 在兩台 VM「同一瞬間」首發內容相同、token 相同,覆蓋無害。
- artwork 依實際格式輸出 `artwork.png`/`artwork.jpg`,`show.json` 記 `artwork_file` 供 feed 引用。
