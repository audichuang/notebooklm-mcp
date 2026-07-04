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


async def test_tombstoned_episode_is_not_resurrected(env, tmp_path, artwork_png):
    # Publish EP1+EP2 -> drop EP2 (tombstone) -> re-add EP2; the retired number 2
    # must stay tombstoned and NOT reappear as a live item (spec iron rule).
    full = _manifest(tmp_path)
    common = dict(
        show_id="ai-news", notebook_id="nb1", show_title="AI 新聞",
        show_description="每日 AI 摘要", author="Audi", owner_name="Audi",
        owner_email="audi@example.com", artwork_path=artwork_png,
    )
    await tools_publish.publish_series(manifest_path=full, **common)

    data = json.loads(open(full, encoding="utf-8").read())
    only1 = dict(data, episodes=[data["episodes"][0]])
    p1 = tmp_path / "series" / "only1.json"
    p1.write_text(json.dumps(only1, ensure_ascii=False), encoding="utf-8")
    await tools_publish.publish_series(manifest_path=str(p1), **common)  # EP2 -> tombstone

    res = await tools_publish.publish_series(manifest_path=full, **common)  # re-add EP2
    info = await tools_publish.feed_info("ai-news")
    assert info["episodes"]["2"]["tombstone"] is True   # 不復活
    assert res["episode_count"] == 1
    ch = ET.fromstring((env / "feeds" / res["token"] / "feed.xml").read_text()).find("channel")
    assert len(ch.findall("item")) == 1


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
