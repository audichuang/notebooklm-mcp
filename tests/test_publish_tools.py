"""publish_series / feed_info against the HTTP PUT uploader model. All network is
faked via httpx.MockTransport injected through the tools_publish._make_client seam
(monkeypatched) — zero real sockets, zero NAS mount, zero NotebookLM calls (every
episode here already has a local mp3_path, so _ensure_local_mp3's re-download
branch, which needs fake_client, is never exercised)."""
import json
import os
from email.utils import parsedate_to_datetime

import httpx
import pytest

from notebooklm_mcp import tools_publish
from notebooklm_mcp.publish import identity


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
            "mp3_path": _write_mp3(tmp_path, f"ep{n:02d}-{filename}.mp3", contents[n]),
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
    assert {"artwork.png", "show.json", "feed.xml", "index.html"} <= set(names)


async def test_commit_order_media_then_state_then_derived(env, tmp_path, artwork_png, monkeypatch):
    captured = _install_mock(monkeypatch)
    await _publish(_two_episode_manifest(tmp_path), artwork_png)
    names = [c["name"] for c in captured]
    assert names[0].startswith("EP01-") and names[0].endswith(".mp3")
    assert names[1].startswith("EP02-") and names[1].endswith(".mp3")
    assert names[2:] == ["artwork.png", "show.json", "feed.xml", "index.html"]


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
    assert len(captured) == 12  # two identical publishes, 6 PUTs each
    first, second = captured[:6], captured[6:]
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
    """A manifest episode carrying a 'description' (show notes) threads into
    show.json; an episode without one falls back to its title."""
    captured = _install_mock(monkeypatch)
    manifest = _manifest(tmp_path, [
        {"episode": 1, "title": "第1集", "description": "本集重點:harness 七檔、loop 三步。",
         "mp3_path": _write_mp3(tmp_path, "e1.mp3", b"a")},
        {"episode": 2, "title": "第2集",  # no description -> falls back to title
         "mp3_path": _write_mp3(tmp_path, "e2.mp3", b"b")},
    ], "notes.json")
    await _publish(manifest, artwork_png)
    show = json.loads(next(c["content"] for c in captured if c["name"] == "show.json"))
    assert show["episodes"]["1"]["description"] == "本集重點:harness 七檔、loop 三步。"
    assert show["episodes"]["2"]["description"] == "第2集"


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
        {"episode": 1, "title": "x", "mp3_path": _write_mp3(tmp_path, "b.mp3", b"b")},
        {"episode": 1, "title": "y", "mp3_path": _write_mp3(tmp_path, "c.mp3", b"c")},
    ], "dup.json")
    with pytest.raises(ValueError, match="duplicate"):
        await _publish(dup, artwork_png)

    blank_title = _manifest(tmp_path, [
        {"episode": 1, "title": "  ", "mp3_path": _write_mp3(tmp_path, "d.mp3", b"d")},
    ], "blank_title.json")
    with pytest.raises(ValueError, match="title"):
        await _publish(blank_title, artwork_png)

    assert captured == []  # every bad manifest fails before any network activity
