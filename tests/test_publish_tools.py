"""publish_series / feed_info against the HTTP PUT uploader model. All network is
faked via httpx.MockTransport injected through the tools_publish._make_client seam
(monkeypatched) — zero real sockets, zero NAS mount, zero NotebookLM calls (every
episode here already has a local mp3_path, so _ensure_local_mp3's re-download
branch, which needs fake_client, is never exercised)."""
import json
import os
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

import httpx
import pytest

from notebooklm_mcp import tools_publish
from notebooklm_mcp.publish import identity

_NS = {"itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd"}


@pytest.fixture(autouse=True)
def _noop_embed(monkeypatch):
    """測試用假 mp3 bytes 不是合法 MP4,不能真丟給 mutagen。把 _embed_cover seam 換成
    「回原始 bytes」——publish 的 PUT/feed 邏輯照測,真內嵌另在 test_cover_embedded 鎖。"""
    monkeypatch.setattr(tools_publish, "_embed_cover",
                        lambda mp3_path, cover_path: open(mp3_path, "rb").read())


@pytest.fixture
def env(monkeypatch):
    monkeypatch.delenv("PODCAST_FEEDS_ROOT", raising=False)  # gone in the PUT model
    monkeypatch.setenv("PODCAST_PUBLIC_BASE_URL", "https://podcast.example")
    monkeypatch.setenv("PODCAST_TOKEN_SALT", "s3cret")
    monkeypatch.setenv("PODCAST_UPLOAD_URL", "http://192.0.2.10:8086")
    monkeypatch.setenv("PODCAST_UPLOAD_TOKEN", "up-tok-xyz")


@pytest.fixture
def artwork_png(tmp_path):
    from PIL import Image

    p = tmp_path / "art.png"
    Image.new("RGB", (1500, 1500)).save(str(p))
    return str(p)


def _install_mock(monkeypatch, *, healthz_status=200, healthz_marker="1", put_status=201):
    """Monkeypatch tools_publish._make_client to hand back an AsyncClient wired to
    an httpx.MockTransport. Returns the `captured` list every PUT lands in."""
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/healthz":
            headers = {"X-Podcast-Uploader": healthz_marker} if healthz_marker is not None else {}
            return httpx.Response(healthz_status, headers=headers)
        if request.method == "PUT":
            captured.append({
                "url": str(request.url),
                "name": request.url.path.rsplit("/", 1)[-1],
                "auth": request.headers.get("authorization"),
                "content": bytes(request.content),
            })
            return httpx.Response(put_status)
        return httpx.Response(404)  # pragma: no cover - no other verb/path expected

    monkeypatch.setattr(
        tools_publish, "_make_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return captured


def _write_mp3(tmp_path, name, content):
    p = tmp_path / name
    p.write_bytes(content)
    return str(p)


def _manifest(tmp_path, episodes, filename="series_manifest.json"):
    mpath = tmp_path / filename
    mpath.write_text(
        json.dumps({"notebook_id": "nb1", "episodes": episodes}, ensure_ascii=False),
        encoding="utf-8",
    )
    return str(mpath)


def _two_episode_manifest(tmp_path, *, published_at=None, contents=None, filename="series_manifest.json"):
    contents = contents or {1: b"audio-1", 2: b"audio-2"}
    episodes = []
    for n in (1, 2):
        ep = {
            "episode": n, "title": f"第{n}集",
            "description": f"第{n}集 show notes:本集重點整理。",   # 必做且不可等於標題
            "mp3_path": _write_mp3(tmp_path, f"ep{n:02d}-{filename}.mp3", contents[n]),
            "cover_path": _valid_cover(tmp_path, f"ep{n:02d}-{filename}-cover.png"),  # 每集必做
        }
        if published_at:
            ep["published_at"] = published_at.get(n) if isinstance(published_at, dict) else published_at
        episodes.append(ep)
    return _manifest(tmp_path, episodes, filename)


async def _publish(manifest_path, artwork_png, **overrides):
    kwargs = dict(
        show_id="ai-news", notebook_id="nb1", manifest_path=manifest_path,
        show_title="AI 新聞", show_description="每日 AI 摘要", author="Audi",
        owner_name="Audi", owner_email="audi@example.com", artwork_path=artwork_png,
    )
    kwargs.update(overrides)
    return await tools_publish.publish_series(**kwargs)


async def test_posts_to_upload_url_not_feeds_root(env, tmp_path, artwork_png, monkeypatch):
    captured = _install_mock(monkeypatch)
    res = await _publish(_two_episode_manifest(tmp_path), artwork_png)
    upload_url = os.environ["PODCAST_UPLOAD_URL"]
    assert captured
    for c in captured:
        assert c["url"] == f"{upload_url}/feeds/{res['token']}/{c['name']}"


async def test_sends_upload_bearer_token(env, tmp_path, artwork_png, monkeypatch):
    captured = _install_mock(monkeypatch)
    res = await _publish(_two_episode_manifest(tmp_path), artwork_png)
    upload_token = os.environ["PODCAST_UPLOAD_TOKEN"]
    assert captured
    for c in captured:
        assert c["auth"] == f"Bearer {upload_token}"
    assert upload_token != res["token"]  # upload bearer != feed token, never conflated


async def test_puts_every_episode_artwork_state_and_derived(env, tmp_path, artwork_png, monkeypatch):
    captured = _install_mock(monkeypatch)
    await _publish(_two_episode_manifest(tmp_path), artwork_png)
    names = [c["name"] for c in captured]
    mp3s = [n for n in names if n.endswith(".mp3")]
    assert len(mp3s) == 2 and mp3s[0].startswith("EP01-") and mp3s[1].startswith("EP02-")
    assert {"show.json", "feed.xml", "index.html"} <= set(names)
    assert any(n.startswith("artwork-") and n.endswith(".png") for n in names)  # content-addressed


async def test_commit_order_media_then_state_then_derived(env, tmp_path, artwork_png, monkeypatch):
    captured = _install_mock(monkeypatch)
    await _publish(_two_episode_manifest(tmp_path), artwork_png)
    names = [c["name"] for c in captured]
    # 每集迴圈內先 mp3 再單集封面,兩集跑完才 artwork/show/feed/index
    assert names[0].startswith("EP01-") and names[0].endswith(".mp3")
    assert names[1].startswith("EP01-cover-") and names[1].endswith(".png")
    assert names[2].startswith("EP02-") and names[2].endswith(".mp3")
    assert names[3].startswith("EP02-cover-") and names[3].endswith(".png")
    assert names[4].startswith("artwork-") and names[4].endswith(".png")  # content-addressed
    assert names[5:] == ["show.json", "feed.xml", "index.html"]


async def test_auth_precheck_runs_before_any_put(env, tmp_path, artwork_png, monkeypatch):
    captured = _install_mock(monkeypatch, healthz_status=401)
    with pytest.raises(ValueError, match="precheck"):
        await _publish(_two_episode_manifest(tmp_path), artwork_png)
    assert captured == []  # not a single byte uploaded on a bad precheck


async def test_publish_is_idempotent(env, tmp_path, artwork_png, monkeypatch):
    captured = _install_mock(monkeypatch)
    pub_at = {1: "Wed, 01 Jan 2020 09:00:00 +0800", 2: "Thu, 02 Jan 2020 09:00:00 +0800"}
    manifest = _two_episode_manifest(tmp_path, published_at=pub_at)
    await _publish(manifest, artwork_png)
    await _publish(manifest, artwork_png)
    assert len(captured) == 16  # two identical publishes, 8 PUTs each (2 mp3 + 2 cover + artwork + show + feed + index)
    first, second = captured[:8], captured[8:]
    for a, b in zip(first, second):
        assert a["name"] == b["name"]
        assert a["content"] == b["content"]  # byte-identical, incl. media filenames


async def test_new_mp3_bytes_new_url_stable_guid_and_pubdate(env, tmp_path, artwork_png, monkeypatch):
    pub_at = {1: "Wed, 01 Jan 2020 09:00:00 +0800", 2: "Thu, 02 Jan 2020 09:00:00 +0800"}

    v1, v2 = tmp_path / "v1", tmp_path / "v2"
    v1.mkdir()
    v2.mkdir()

    captured1 = _install_mock(monkeypatch)
    m1 = _two_episode_manifest(v1, published_at=pub_at, filename="v1")
    await _publish(m1, artwork_png)
    show1 = json.loads(next(c["content"] for c in captured1 if c["name"] == "show.json"))

    captured2 = _install_mock(monkeypatch)
    m2 = _two_episode_manifest(
        v2, published_at=pub_at, filename="v2",
        contents={1: b"audio-1-REGENERATED", 2: b"audio-2"},
    )
    await _publish(m2, artwork_png)
    show2 = json.loads(next(c["content"] for c in captured2 if c["name"] == "show.json"))

    ep1a, ep1b = show1["episodes"]["1"], show2["episodes"]["1"]
    assert ep1a["media_file"] != ep1b["media_file"]  # content changed -> new URL
    assert ep1a["guid"] == ep1b["guid"] == identity.episode_guid("ai-news", 1)  # stable
    assert ep1a["pub_date"] == ep1b["pub_date"]  # stable


async def test_fallback_pubdate_deterministic(env, tmp_path, artwork_png, monkeypatch):
    captured = _install_mock(monkeypatch)
    manifest = _two_episode_manifest(tmp_path)  # no published_at -> fallback path
    await _publish(manifest, artwork_png)
    await _publish(manifest, artwork_png)
    shows = [json.loads(c["content"]) for c in captured if c["name"] == "show.json"]
    assert len(shows) == 2 and shows[0] == shows[1]  # byte/value-stable across republish

    eps = shows[0]["episodes"]
    d1 = parsedate_to_datetime(eps["1"]["pub_date"])
    d2 = parsedate_to_datetime(eps["2"]["pub_date"])
    assert d1 != d2
    assert d1 < d2  # EP01 older than EP02, same direction as the published_at path


async def test_missing_env_errors(env, tmp_path, artwork_png, monkeypatch):
    monkeypatch.delenv("PODCAST_UPLOAD_URL", raising=False)
    with pytest.raises(ValueError, match="PODCAST_UPLOAD_URL"):
        await _publish(_two_episode_manifest(tmp_path), artwork_png)


async def test_missing_required_metadata_errors(env, tmp_path, artwork_png, monkeypatch):
    captured = _install_mock(monkeypatch)
    with pytest.raises(ValueError, match="show_description"):
        await _publish(_two_episode_manifest(tmp_path), artwork_png, show_description="")
    assert captured == []


async def test_manifest_description_becomes_episode_description(env, tmp_path, artwork_png, monkeypatch):
    """A manifest episode's 'description' (show notes) threads verbatim into show.json."""
    captured = _install_mock(monkeypatch)
    manifest = _manifest(tmp_path, [
        {"episode": 1, "title": "第1集", "description": "本集重點:harness 七檔、loop 三步。",
         "mp3_path": _write_mp3(tmp_path, "e1.mp3", b"a"),
         "cover_path": _valid_cover(tmp_path, "n1-cover.png")},
        {"episode": 2, "title": "第2集", "description": "本集重點:subagent 派工、worktree 隔離。",
         "mp3_path": _write_mp3(tmp_path, "e2.mp3", b"b"),
         "cover_path": _valid_cover(tmp_path, "n2-cover.png")},
    ], "notes.json")
    await _publish(manifest, artwork_png)
    show = json.loads(next(c["content"] for c in captured if c["name"] == "show.json"))
    assert show["episodes"]["1"]["description"] == "本集重點:harness 七檔、loop 三步。"
    assert show["episodes"]["2"]["description"] == "本集重點:subagent 派工、worktree 隔離。"


async def test_missing_description_fails_fast(env, tmp_path, artwork_png, monkeypatch):
    """description 每集必做:缺 或 等於標題 → preflight raise,一個 byte 都不傳。"""
    captured = _install_mock(monkeypatch)
    no_desc = _manifest(tmp_path, [
        {"episode": 1, "title": "第1集",  # 缺 description
         "mp3_path": _write_mp3(tmp_path, "nd1.mp3", b"a"),
         "cover_path": _valid_cover(tmp_path, "nd1-cover.png")},
    ], "no_desc.json")
    with pytest.raises(ValueError, match="description is required"):
        await _publish(no_desc, artwork_png)

    eq_title = _manifest(tmp_path, [
        {"episode": 1, "title": "第1集", "description": "第1集",  # 等於標題
         "mp3_path": _write_mp3(tmp_path, "et1.mp3", b"a"),
         "cover_path": _valid_cover(tmp_path, "et1-cover.png")},
    ], "eq_title.json")
    with pytest.raises(ValueError, match="must not equal title"):
        await _publish(eq_title, artwork_png)

    assert captured == []


async def test_feed_info(env):
    info = await tools_publish.feed_info("ai-news")
    token = identity.make_token("ai-news", os.environ["PODCAST_TOKEN_SALT"])
    assert info == {
        "show_id": "ai-news", "token": token,
        "feed_url": f"https://podcast.example/feeds/{token}/feed.xml",
        "show_page_url": f"https://podcast.example/feeds/{token}/index.html",
    }


async def test_manifest_preflight_rejects_bad(env, tmp_path, artwork_png, monkeypatch):
    captured = _install_mock(monkeypatch)

    too_high = _manifest(tmp_path, [
        {"episode": 100, "title": "x", "mp3_path": _write_mp3(tmp_path, "a.mp3", b"a")},
    ], "too_high.json")
    with pytest.raises(ValueError, match="1..99"):
        await _publish(too_high, artwork_png)

    dup = _manifest(tmp_path, [
        {"episode": 1, "title": "x", "description": "d1",
         "mp3_path": _write_mp3(tmp_path, "b.mp3", b"b"), "cover_path": _valid_cover(tmp_path, "dup1-cover.png")},
        {"episode": 1, "title": "y", "description": "d2",
         "mp3_path": _write_mp3(tmp_path, "c.mp3", b"c"), "cover_path": _valid_cover(tmp_path, "dup2-cover.png")},
    ], "dup.json")
    with pytest.raises(ValueError, match="duplicate"):
        await _publish(dup, artwork_png)

    blank_title = _manifest(tmp_path, [
        {"episode": 1, "title": "  ", "mp3_path": _write_mp3(tmp_path, "d.mp3", b"d")},
    ], "blank_title.json")
    with pytest.raises(ValueError, match="title"):
        await _publish(blank_title, artwork_png)

    assert captured == []  # every bad manifest fails before any network activity


async def test_attachments_hosted_and_linked(env, tmp_path, artwork_png, monkeypatch):
    captured = _install_mock(monkeypatch)
    pdf = _write_mp3(tmp_path, "ep01-slides.pdf", b"%PDF-1.4 x")   # _write_mp3 只是寫 bytes
    md = tmp_path / "ep01-report.md"
    md.write_text("# 講義\n\n- 重點", encoding="utf-8")
    manifest = _manifest(tmp_path, [{
        "episode": 1, "title": "第1集", "description": "本集重點。",
        "mp3_path": _write_mp3(tmp_path, "e1.mp3", b"a"),
        "cover_path": _valid_cover(tmp_path, "att-cover.png"),
        "slides_pdf_path": pdf, "report_md_path": str(md),
    }], "att.json")

    res = await _publish(manifest, artwork_png)
    names = [c["name"] for c in captured]
    assert any(n.startswith("EP01-") and n.endswith(".pdf") for n in names)
    assert any(n.startswith("EP01-") and n.endswith(".html") for n in names)

    show = json.loads(next(c["content"] for c in captured if c["name"] == "show.json"))
    desc = show["episodes"]["1"]["description"]
    assert desc.startswith("本集重點。")
    assert "本集簡報" in desc and ".pdf" in desc
    assert "研讀講義" in desc and ".html" in desc

    # PUT 上去的 html 是渲染後的(含 <h1>),不是原始 markdown
    html_put = next(c["content"] for c in captured if c["name"].endswith(".html") and c["name"].startswith("EP01-"))
    assert b"<h1" in html_put and "講義".encode() in html_put

    # description_html(→ feed content:encoded):具名可點連結,不裸露 URL 當顯示文字
    dh = show["episodes"]["1"]["description_html"]
    assert ">本集簡報 (PDF)</a>" in dh and ">研讀講義</a>" in dh
    assert 'href="https://podcast.example/feeds/' in dh


async def test_missing_attachment_file_fails_fast(env, tmp_path, artwork_png, monkeypatch):
    captured = _install_mock(monkeypatch)
    manifest = _manifest(tmp_path, [{
        "episode": 1, "title": "第1集", "description": "本集重點。",
        "mp3_path": _write_mp3(tmp_path, "e1b.mp3", b"a"),
        "cover_path": _valid_cover(tmp_path, "attm-cover.png"),
        "slides_pdf_path": str(tmp_path / "does-not-exist.pdf"),
    }], "att_missing.json")
    with pytest.raises(ValueError, match="slides_pdf_path"):
        await _publish(manifest, artwork_png)


def _valid_cover(tmp_path, name):
    from PIL import Image

    p = tmp_path / name
    Image.new("RGB", (1500, 1500)).save(str(p))
    return str(p)


async def test_episode_cover_hosted_and_wired_into_feed(env, tmp_path, artwork_png, monkeypatch):
    captured = _install_mock(monkeypatch)
    manifest = _manifest(tmp_path, [{
        "episode": 1, "title": "第1集", "description": "本集重點。",
        "mp3_path": _write_mp3(tmp_path, "ec1.mp3", b"a"),
        "cover_path": _valid_cover(tmp_path, "ep01-cover.png"),
    }], "cover.json")
    await _publish(manifest, artwork_png)

    names = [c["name"] for c in captured]
    assert any(n.startswith("EP01-cover-") and n.endswith(".png") for n in names)   # 上傳了單集封面

    show = json.loads(next(c["content"] for c in captured if c["name"] == "show.json"))
    art = show["episodes"]["1"]["artwork_file"]
    assert art.startswith("EP01-cover-") and art.endswith(".png")

    feedxml = next(c["content"] for c in captured if c["name"] == "feed.xml")
    it = ET.fromstring(feedxml).find("channel/item")
    assert it.find("itunes:image", _NS).get("href").endswith(art)                   # feed <item> 帶該集封面


async def test_missing_cover_fails_fast(env, tmp_path, artwork_png, monkeypatch):
    """單集封面每集必做:缺 cover_path → preflight raise,不再靜默 fallback 節目封面,
    一個 byte 都不傳(避免漏封面的集數「發布成功」卻掛錯圖)。"""
    captured = _install_mock(monkeypatch)
    no_cover = _manifest(tmp_path, [{
        "episode": 1, "title": "第1集", "description": "本集重點。",
        "mp3_path": _write_mp3(tmp_path, "ncov.mp3", b"a"),  # 無 cover_path
    }], "no_cover.json")
    with pytest.raises(ValueError, match="cover_path is required"):
        await _publish(no_cover, artwork_png)
    assert captured == []


async def test_bad_episode_cover_fails_fast(env, tmp_path, artwork_png, monkeypatch):
    captured = _install_mock(monkeypatch)
    bad = tmp_path / "bad-cover.png"
    bad.write_bytes(b"not a real image")
    manifest = _manifest(tmp_path, [{
        "episode": 1, "title": "第1集",
        "mp3_path": _write_mp3(tmp_path, "ec2.mp3", b"a"),
        "cover_path": str(bad),
    }], "badcover.json")
    with pytest.raises(ValueError):
        await _publish(manifest, artwork_png)


async def test_cover_embedded_into_published_mp3(env, tmp_path, artwork_png, monkeypatch):
    """發布的 mp3 = _embed_cover 內嵌後的 bytes(不是原始檔),且 enclosure length 用內嵌後
    大小、media_file hash 也算內嵌後 bytes。覆寫掉 autouse 的 no-op seam 來當 spy。"""
    captured = _install_mock(monkeypatch)
    monkeypatch.setattr(
        tools_publish, "_embed_cover",
        lambda mp3_path, cover_path: b"COVR:" + open(mp3_path, "rb").read())
    manifest = _manifest(tmp_path, [{
        "episode": 1, "title": "第1集", "description": "本集重點。",
        "mp3_path": _write_mp3(tmp_path, "emb.mp3", b"RAWAUDIO"),
        "cover_path": _valid_cover(tmp_path, "emb-cover.png"),
    }], "emb.json")
    await _publish(manifest, artwork_png)

    mp3_put = next(c["content"] for c in captured
                   if c["name"].startswith("EP01-") and c["name"].endswith(".mp3"))
    assert mp3_put == b"COVR:RAWAUDIO"          # 上傳的是內嵌後 bytes,不是原始 mp3
    show = json.loads(next(c["content"] for c in captured if c["name"] == "show.json"))
    assert show["episodes"]["1"]["length"] == len(mp3_put)   # enclosure length = 內嵌後大小
