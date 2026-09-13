"""publish_series / feed_info against the HTTP PUT uploader model. All network is
faked via httpx.MockTransport injected through the tools_publish._make_client seam
(monkeypatched) — zero real sockets, zero NAS mount, zero NotebookLM calls (every
episode here already has a local mp3_path, so _ensure_local_mp3's re-download
branch, which needs fake_client, is never exercised)."""
import hashlib
import json
import os
import shutil
import struct
import subprocess
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

import httpx
import pytest

from notebooklm_mcp import tools_publish
from notebooklm_mcp.publish import identity

# Bind the REAL _embed_cover at import time — the autouse no-op fixture below only
# rebinds the module attribute, so this local name stays the un-patched function.
from notebooklm_mcp.tools_publish import _embed_cover as _real_embed

_NS = {"itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd"}


def _atoms(path):
    """Top-level MP4 atom types in order (enough to check faststart / de-frag)."""
    out = []
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        pos = 0
        while pos < size and len(out) < 16:
            f.seek(pos)
            hdr = f.read(8)
            if len(hdr) < 8:
                break
            asize = struct.unpack(">I", hdr[:4])[0]
            out.append(hdr[4:8].decode("latin1"))
            if asize <= 1:
                break
            pos += asize
    return out


@pytest.fixture(autouse=True)
def _noop_embed(monkeypatch):
    """多數 publish 測試用假 audio bytes，不適合真丟給 ffprobe/ffmpeg/mutagen。
    把 _embed_cover seam 換成「回原始 bytes」；真媒體契約由檔案下方 regression tests 鎖住。"""
    monkeypatch.setattr(tools_publish, "_embed_cover",
                        lambda mp3_path, cover_path: open(mp3_path, "rb").read())
    monkeypatch.setattr(tools_publish, "_require_media_binaries", lambda: None)


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


def _numbered_manifest(tmp_path, **kw):
    """`_two_episode_manifest` 的連載版:標題帶 `EP{NN}. ` 前綴。

    serial 節目的標題前綴是 publish 的硬契約(見 §serial 節目的標題必須帶集號),
    所以每個傳 `itunes_type="serial"` 的測試都得用這一份,不能用裸的那個。
    """
    manifest = _two_episode_manifest(tmp_path, **kw)
    stored = json.loads(open(manifest, encoding="utf-8").read())
    for ep in stored["episodes"]:
        ep["title"] = f"EP{ep['episode']:02d}. {ep['title']}"
    open(manifest, "w", encoding="utf-8").write(json.dumps(stored, ensure_ascii=False))
    return manifest


def _show_kwargs(manifest_path, artwork_png):
    return dict(
        show_id="ai-news", notebook_id="nb1", manifest_path=manifest_path,
        show_title="AI 新聞", show_description="每日 AI 摘要", author="Audi",
        owner_name="Audi", owner_email="audi@example.com", artwork_path=artwork_png,
    )


async def _publish(manifest_path, artwork_png, **overrides):
    # v0.3.3 起附件 fail-closed(require_slides/require_report 未傳 = 視為 True)。這裡的
    # fixture 都不帶附件,而這些測試驗的是上傳機制/feed 內容,不是附件政策——所以 helper
    # 顯式關掉。要測 production defaults 的請用 _publish_with_defaults。
    kwargs = _show_kwargs(manifest_path, artwork_png)
    kwargs.update(require_slides=False, require_report=False)
    kwargs.update(overrides)
    return await tools_publish.publish_series(**kwargs)


async def _publish_with_defaults(manifest_path, artwork_png, **overrides):
    """完全不傳 require_*,讓 production 預設值自己生效——把預設改掉,呼叫這個的測試才會紅。"""
    kwargs = _show_kwargs(manifest_path, artwork_png)
    kwargs.update(overrides)
    return await tools_publish.publish_series(**kwargs)


async def test_missing_media_binary_fails_before_any_put(
    env, tmp_path, artwork_png, monkeypatch
):
    captured = _install_mock(monkeypatch)
    manifest = _two_episode_manifest(tmp_path)

    def missing_binary():
        raise ValueError("required podcast media tool is missing: ffmpeg")

    monkeypatch.setattr(
        tools_publish, "_require_media_binaries", missing_binary
    )
    with pytest.raises(ValueError, match="media tool.*ffmpeg"):
        await _publish(manifest, artwork_png)
    assert captured == []


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


def test_require_url_env_rejects_non_http_scheme(monkeypatch):
    """L5：PODCAST_UPLOAD_URL 若打錯成非 http(s) scheme，要在讀 env 當下(_require_url_env)
    就給清楚的設定錯誤——不是留給 httpx 在 client.get() 開 socket 前丟原生
    UnsupportedProtocol(那個原生例外本來就已經防住 token 外洩，scheme check 的價值
    是「訊息看得懂」，不是多一層安全防線)。"""
    monkeypatch.setenv("PODCAST_UPLOAD_URL", "ftp://evil.example")
    with pytest.raises(ValueError, match="http"):
        tools_publish._require_url_env("PODCAST_UPLOAD_URL")


def test_require_url_env_error_does_not_leak_url(monkeypatch):
    """訊息只印 scheme，不印(可能是內網的)URL 本身。"""
    monkeypatch.setenv("PODCAST_UPLOAD_URL", "ftp://192.0.2.10:8086/secret-path")
    with pytest.raises(ValueError) as exc_info:
        tools_publish._require_url_env("PODCAST_UPLOAD_URL")
    assert "192.0.2.10" not in str(exc_info.value)
    assert "ftp" in str(exc_info.value)


async def test_publish_rejects_non_http_upload_url_before_any_put(env, tmp_path, artwork_png, monkeypatch):
    """整合驗證:壞 scheme 的 PODCAST_UPLOAD_URL 在 publish_series 裡於讀 env 當下就
    raise,連 manifest 都還沒動、更不會發出任何 PUT。"""
    captured = _install_mock(monkeypatch)
    monkeypatch.setenv("PODCAST_UPLOAD_URL", "ftp://192.0.2.10:8086")
    with pytest.raises(ValueError, match="http"):
        await _publish(_two_episode_manifest(tmp_path), artwork_png)
    assert captured == []


async def test_return_episodes_bad_type_fails_before_any_put(
    env, tmp_path, artwork_png, monkeypatch
):
    """`return_episodes` 只是回傳過濾器,但舊版拖到所有 PUT + manifest 回寫都完成後
    才 `set(return_episodes)`——傳個 [[1]] 之類的壞型別(list 不可 hash)會在「發布其實
    已成功」之後才 unhashable TypeError,呼叫端誤以為整批失敗。要在任何遠端副作用前
    就 ValueError。"""
    captured = _install_mock(monkeypatch)
    manifest = _two_episode_manifest(tmp_path)
    with pytest.raises(ValueError, match="return_episodes must be a list of ints"):
        await _publish(manifest, artwork_png, return_episodes=[[1]])
    assert captured == []


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


# ---- v0.2.9 token-diet:P3 show 欄位存 manifest,之後只傳 manifest_path ------------

async def test_publish_persists_show_config_then_manifest_path_alone_suffices(
        env, tmp_path, artwork_png, monkeypatch):
    """首次顯式發布 → show 七欄寫進 manifest["show"];之後只傳 manifest_path,
    feed 輸出 byte-identical(滾動 feed 加一集不用再重打七欄)。"""
    captured = _install_mock(monkeypatch)
    pub_at = {1: "Wed, 01 Jan 2020 09:00:00 +0800", 2: "Thu, 02 Jan 2020 09:00:00 +0800"}
    manifest = _two_episode_manifest(tmp_path, published_at=pub_at)
    await _publish(manifest, artwork_png)                       # 顯式傳齊(現行姿勢)

    stored = json.loads(open(manifest, encoding="utf-8").read())
    assert stored["schema_version"] == 2
    assert stored["revision"] == 1
    saved = stored["show"]
    assert saved["show_id"] == "ai-news" and saved["show_title"] == "AI 新聞"
    assert saved["owner_email"] == "audi@example.com"
    assert saved["artwork_path"] == artwork_png
    assert saved["category"] == "Technology" and saved["explicit"] is False

    # 真的只傳 manifest_path —— show 七欄與附件政策都必須從 manifest 沿用。
    res2 = await tools_publish.publish_series(manifest_path=manifest)
    assert res2["token"] == identity.make_token("ai-news", "s3cret")
    first, second = captured[:8], captured[8:]
    for a, b in zip(first, second):
        assert a["name"] == b["name"] and a["content"] == b["content"]  # byte-identical


async def test_publish_missing_show_config_raises_helpfully(env, tmp_path, artwork_png, monkeypatch):
    _install_mock(monkeypatch)
    manifest = _two_episode_manifest(tmp_path)     # 無 manifest["show"]
    with pytest.raises(ValueError, match="show_id"):
        await tools_publish.publish_series(manifest_path=manifest)


async def test_publish_explicit_param_overrides_manifest_show(env, tmp_path, artwork_png, monkeypatch):
    captured = _install_mock(monkeypatch)
    manifest = _two_episode_manifest(tmp_path)
    await _publish(manifest, artwork_png)                                    # 寫入 show 區塊
    await tools_publish.publish_series(                                          # 顯式覆蓋
        manifest_path=manifest, show_title="改名後",
        require_slides=False, require_report=False,
    )
    show2 = json.loads([c["content"] for c in captured if c["name"] == "show.json"][-1])
    assert show2["title"] == "改名後"
    saved = json.loads(open(manifest, encoding="utf-8").read())["show"]
    assert saved["show_title"] == "改名後"          # 覆蓋值也回寫,下次沿用


# ---- P1-4:feed 身分(show_id × PODCAST_TOKEN_SALT)沒有機器圍籬 --------------------
# token = HMAC(salt, show_id) 已經內含這兩個分支;sha256(token) 存成
# manifest["show"]["feed_identity_sha256"](存雜湊不存 token —— manifest 可能進
# podcast-lab 的版控),一條比對同時擋「show_id 打錯」與「PODCAST_TOKEN_SALT 漂移」。

def _identity_hash(show_id, salt):
    return hashlib.sha256(identity.make_token(show_id, salt).encode("utf-8")).hexdigest()


async def test_publish_first_ever_publish_stores_feed_identity_hash(
    env, tmp_path, artwork_png, monkeypatch
):
    """legacy manifest(無 `show` 鍵,從未發布過)照發,這次發布把身分指紋寫回去,
    之後才擋得住換身分。"""
    _install_mock(monkeypatch)
    manifest = _two_episode_manifest(tmp_path)          # 無 manifest["show"]
    await _publish(manifest, artwork_png)                # show_id="ai-news"(_show_kwargs 預設)
    saved = json.loads(open(manifest, encoding="utf-8").read())["show"]
    assert saved["feed_identity_sha256"] == _identity_hash("ai-news", "s3cret")


async def test_publish_refuses_show_id_change_on_same_manifest(
    env, tmp_path, artwork_png, monkeypatch
):
    """同一份 manifest 換 show_id 沒有警告會讓整季無警告落到一條新 feed,既有訂閱者
    收不到——這裡測 show_id 打錯/刻意換的那一半。"""
    captured = _install_mock(monkeypatch)
    manifest = _two_episode_manifest(tmp_path)
    await _publish(manifest, artwork_png)
    first_count = len(captured)
    assert first_count == 8   # 2 mp3 + 2 cover + artwork + show.json + feed.xml + index.html

    with pytest.raises(ValueError, match="feed identity"):
        await _publish(manifest, artwork_png, show_id="ai-news-v2")

    assert len(captured) == first_count       # 第二次零 PUT
    saved = json.loads(open(manifest, encoding="utf-8").read())["show"]
    assert saved["show_id"] == "ai-news"      # manifest 沒被覆寫


async def test_publish_refuses_salt_change_on_same_manifest(
    env, tmp_path, artwork_png, monkeypatch
):
    """同 show_id、salt 漂移(換 Doppler 環境等)一樣要擋——salt 分支原本零痕跡。"""
    captured = _install_mock(monkeypatch)
    manifest = _two_episode_manifest(tmp_path)
    await _publish(manifest, artwork_png)
    captured.clear()

    monkeypatch.setenv("PODCAST_TOKEN_SALT", "s3cret-dev")
    with pytest.raises(ValueError, match="feed identity"):
        await _publish(manifest, artwork_png)
    assert captured == []


async def test_publish_same_identity_republish_is_fine(
    env, tmp_path, artwork_png, monkeypatch
):
    """同 show_id、同 salt 重發(滾動加集的日常情況)不受這道閘影響。"""
    captured = _install_mock(monkeypatch)
    manifest = _two_episode_manifest(tmp_path)
    await _publish(manifest, artwork_png)
    first_hash = json.loads(open(manifest, encoding="utf-8").read())["show"]["feed_identity_sha256"]

    await _publish(manifest, artwork_png)
    second_hash = json.loads(open(manifest, encoding="utf-8").read())["show"]["feed_identity_sha256"]
    assert first_hash == second_hash == _identity_hash("ai-news", "s3cret")
    assert len(captured) == 16   # 兩次都成功,各 8 個 PUT


async def test_publish_allows_identity_change_after_removing_feed_identity_hash(
    env, tmp_path, artwork_png, monkeypatch
):
    """拿掉 `feed_identity_sha256` 欄位 = 顯式承認要換身分(復活要被看見,不是自動發生)
    ——之後才准換 show_id 重發。"""
    from notebooklm_mcp.manifest_store import ManifestStore

    captured = _install_mock(monkeypatch)
    manifest = _two_episode_manifest(tmp_path)
    await _publish(manifest, artwork_png)                 # show_id="ai-news"

    ManifestStore(manifest).update(lambda m: m["show"].pop("feed_identity_sha256", None))

    captured.clear()
    res = await _publish(manifest, artwork_png, show_id="ai-news-v2")
    assert res["token"] == identity.make_token("ai-news-v2", "s3cret")
    saved = json.loads(open(manifest, encoding="utf-8").read())["show"]
    assert saved["show_id"] == "ai-news-v2"
    assert saved["feed_identity_sha256"] == _identity_hash("ai-news-v2", "s3cret")


async def test_publish_return_includes_duration(env, tmp_path, artwork_png, monkeypatch):
    """step 8 對帳要 duration;publish 本來就算了,回傳帶上省一次 feed 抓取。"""
    _install_mock(monkeypatch)
    monkeypatch.setattr(tools_publish, "_audio_duration_hms", lambda p: "00:16:26")
    res = await _publish(_two_episode_manifest(tmp_path), artwork_png)
    assert all(e["duration"] == "00:16:26" for e in res["episodes"])


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


def _live_ep(tmp_path, n=1):
    return {
        "episode": n,
        "title": f"第{n}集",
        "description": f"第{n}集 show notes:本集重點整理。",
        "mp3_path": _write_mp3(tmp_path, f"live{n}.mp3", f"audio-live{n}".encode()),
        "cover_path": _valid_cover(tmp_path, f"live{n}-cover.png"),
    }


def _deferred_ep_production_shape(tmp_path, n=2):
    """**生產上真實的 deferred 形狀**(`podcast-lab` 的 EP46):description / cover /
    簡報 / 講義**全部齊全且檔案存在**,只缺 `mp3_path` 與 `artifact_id`。

    這個 fixture 存在的理由是一個具體的假綠:把過濾條件收窄成
    `state in _WITHHELD... and not ep.get("cover_path")` 之類「順手多看一個欄位」的寫法,
    對「只有 title + 稽核欄位」的極簡 fixture 仍然全綠,對真的 EP46 卻會再次擋掉整季。
    判準只能是 `publication_state` 本身。"""
    return {
        "episode": n,
        "title": f"第{n}集",
        "description": f"第{n}集 show notes:本集尚未公開,但簡介早就寫好了。",
        "cover_path": _valid_cover(tmp_path, f"deferred{n}-cover.png"),
        "slides_pdf_path": _slides(tmp_path, f"deferred{n}"),
        "report_md_path": _report(tmp_path, f"deferred{n}"),
        "publication_state": "deferred",
        "publication_state_reason": "音檔經五次 semantic QA 拒收且全部 attempt 已撤回",
        "publication_state_at": "2026-08-14T00:05:36.139052+00:00",
    }


async def test_deferred_episode_is_withheld_and_never_preflighted(
    env, tmp_path, artwork_png, monkeypatch
):
    """QA 撤回的 deferred 集留在 manifest 當 audit,不能擋整季重發,也不能進 feed。

    刻意用**最小形狀**(只有 title + 稽核欄位):它缺 description / cover,只要有進
    preflight 迴圈就會 raise,所以這條同時鎖住「整集跳過 preflight」與「一個 byte
    都不上傳」,不只是「不進 show.json」。生產形狀由
    `test_production_shaped_deferred_episode_is_still_withheld` 守。"""
    captured = _install_mock(monkeypatch)
    deferred = {
        "episode": 2,
        "title": "第2集",
        "publication_state": "deferred",
        "publication_state_reason": "音檔經五次 semantic QA 拒收且全部 attempt 已撤回",
    }
    manifest = _manifest(tmp_path, [_live_ep(tmp_path), deferred], "with_deferred.json")
    res = await _publish(manifest, artwork_png)
    assert res["episode_count"] == 1
    assert [ep["n"] for ep in res["episodes"]] == [1]
    # 被扣下的集號一定要回報:episode_count 少一集卻不說是哪一集,看起來像發布漏集。
    assert res["deferred_episodes"] == [2]
    show = json.loads(next(c["content"] for c in captured if c["name"] == "show.json"))
    assert set(show["episodes"]) == {"1"}
    assert not any(c["name"].startswith("EP02") for c in captured)


async def test_production_shaped_deferred_episode_is_still_withheld(
    env, tmp_path, artwork_png, monkeypatch
):
    """判準是 `publication_state` 本身,不是「剛好缺哪個檔」——見 fixture docstring。

    真 EP46 的附件都在,所以這條也順帶鎖住「deferred 集的簡報/講義不會被 host 上去」:
    `require_slides` / `require_report` 對它完全不適用(它不進 feed)。"""
    captured = _install_mock(monkeypatch)
    manifest = _manifest(
        tmp_path,
        [_live_ep(tmp_path), _deferred_ep_production_shape(tmp_path)],
        "prod_deferred.json",
    )
    res = await _publish(manifest, artwork_png)
    assert res["episode_count"] == 1
    assert res["deferred_episodes"] == [2]
    assert not any(c["name"].startswith("EP02") for c in captured)


@pytest.mark.parametrize(
    "state",
    [
        "defered",              # 拼錯
        "",                     # 空字串
        None,                   # 顯式 null:與「欄位不存在」同形,但意圖無從得知
        "published",            # 未來可能新增、但這一版沒有語意的狀態
        ["deferred"],           # unhashable:不能漏 TypeError 出去
    ],
    ids=["typo", "empty", "explicit_null", "unsupported", "unhashable"],
)
async def test_unknown_publication_state_fails_before_any_put(
    env, tmp_path, artwork_png, monkeypatch, state
):
    """未知 publication_state 一律 raise,不 fall through 成照發。

    這個欄位唯一的用途就是「別公開這一集」,任何值靜默公開都是在它該生效的時候失效
    —— 而 **feed host 永不刪檔**,送出去的 mp3 收不回來。所以「缺席」才是照發,
    欄位一旦出現就只認明列的值。"""
    captured = _install_mock(monkeypatch)
    bad = {**_live_ep(tmp_path, 2), "publication_state": state}
    manifest = _manifest(tmp_path, [_live_ep(tmp_path), bad], "bad_state.json")
    with pytest.raises(ValueError, match="unknown publication_state"):
        await _publish(manifest, artwork_png)
    assert captured == []


async def test_all_episodes_deferred_fails_instead_of_publishing_empty_feed(
    env, tmp_path, artwork_png, monkeypatch
):
    """全季都被扣下 → raise。空 feed 會把既有 show.json 的集數整批清掉。

    **兩集**都 deferred(不是一集):`len(all_eps) == 1` 之類綁在集數上的守衛,對單集
    fixture 會假綠。"""
    captured = _install_mock(monkeypatch)
    manifest = _manifest(
        tmp_path,
        [
            {"episode": 1, "title": "第1集", "publication_state": "deferred"},
            _deferred_ep_production_shape(tmp_path),
        ],
        "all_deferred.json",
    )
    with pytest.raises(ValueError, match="no publishable episodes"):
        await _publish(manifest, artwork_png)
    assert captured == []


def _retired_manifest(tmp_path, value):
    """一份**完全合法、能發**的兩集 manifest,只多了頂層 `retired`。合法是重點:閘要證明的是
    「內容再對也不發」,拿一份本來就會被別的 preflight 擋下的 manifest 測不出這件事。"""
    manifest = _two_episode_manifest(tmp_path, filename=f"retired-{value!r}.json")
    stored = json.loads(open(manifest, encoding="utf-8").read())
    stored["retired"] = value
    open(manifest, "w", encoding="utf-8").write(json.dumps(stored, ensure_ascii=False))
    return manifest


async def test_retired_manifest_is_refused_before_any_network(env, tmp_path, artwork_png, monkeypatch):
    """manifest 頂層 `retired: true` → 在任何網路動作(連 `/healthz` probe)之前 raise。

    存在的理由(podcast-lab graphify 2026-08-30 審查):兩份退役快照搬進 `archive/`
    之後仍通過 ManifestStore schema、`show_id` 仍能打到第一季的舊 feed;只靠位置與 README
    擋,`mp3_path` 哪天被「修好」就會把別節目的音檔以舊節目名義送上公網(legacy 集不比
    sha,`_mp3_provenance_gap` 攔不到)。位置不是安全邊界,machine-readable 旗標才是。

    `healthz_status=500`:若閘沒有排在最前面,會先撞 auth precheck 的錯誤訊息而非本閘的。"""
    captured = _install_mock(monkeypatch, healthz_status=500)
    manifest = _retired_manifest(tmp_path, True)
    with pytest.raises(ValueError, match="is retired"):
        await _publish(manifest, artwork_png)
    assert captured == []


@pytest.mark.parametrize("value", [False, None, "true", 1, "yes"])
async def test_retired_accepts_only_true_and_never_falls_through(
    env, tmp_path, artwork_png, monkeypatch, value
):
    """`retired` 一旦出現只認 `true`;`false`/`null`/字串/`1` 一律 raise,**不 fall through
    成照發**(與 `publication_state`、`published_at` 同一條:缺席才是照發,要發就移除欄位)。
    `1` 特別列入:`1 == True` 在 Python 成立,寫成 `== True` 會放行一個沒人打算寫的值。"""
    captured = _install_mock(monkeypatch, healthz_status=500)
    manifest = _retired_manifest(tmp_path, value)
    with pytest.raises(ValueError, match="retired 只能是 true"):
        await _publish(manifest, artwork_png)
    assert captured == []


async def test_manifest_without_retired_field_publishes(env, tmp_path, artwork_png, monkeypatch):
    """對照組:沒有這個欄位的 manifest 行為不變(既有幾十條 publish 測試都是這種形狀,
    這條只是把「缺席=放行」講成一條看得見的契約)。"""
    captured = _install_mock(monkeypatch)
    manifest = _two_episode_manifest(tmp_path)
    assert "retired" not in json.loads(open(manifest, encoding="utf-8").read())
    out = await _publish(manifest, artwork_png)
    assert out["episode_count"] == 2
    assert captured


async def test_writer_tool_round_trips_with_the_publisher(
    env, tmp_path, artwork_png, monkeypatch
):
    """`episode_set_publication_state` 寫進去的狀態,`publish_series` 真的要扣下 —— 而且
    解除之後真的要放行。

    兩邊共用同一份白名單常數有另一條測試守,但**欄位名**是各自寫死的字面值:writer 改成
    `publicationState` 之類的話,那條測試仍會綠,而扣下從此完全無效(那一集直接公開)。
    這條把 lifecycle 的兩端接起來。"""
    from notebooklm_mcp import tools_artifacts

    captured = _install_mock(monkeypatch)
    manifest = _manifest(
        tmp_path, [_live_ep(tmp_path, 1), _live_ep(tmp_path, 2)], "round_trip.json"
    )
    await tools_artifacts.episode_set_publication_state(
        manifest, 2, "deferred", reason="音檔 QA 拒收"
    )
    withheld_run = await _publish(manifest, artwork_png)
    assert withheld_run["deferred_episodes"] == [2]
    assert [ep["n"] for ep in withheld_run["episodes"]] == [1]

    await tools_artifacts.episode_set_publication_state(manifest, 2, None)
    released = await _publish(manifest, artwork_png)
    assert released["deferred_episodes"] == []
    assert [ep["n"] for ep in released["episodes"]] == [1, 2]
    show = json.loads(
        [c["content"] for c in captured if c["name"] == "show.json"][-1]
    )
    assert set(show["episodes"]) == {"1", "2"}


async def test_deferred_episode_skips_the_attachment_gate_under_production_defaults(
    env, tmp_path, artwork_png, monkeypatch
):
    """「整集跳過 preflight」要在**沒關掉** require_slides/require_report 的情況下成立。

    `_publish` helper 顯式把兩個旗標關掉,所以其他 deferred 測試證明不了這件事:把
    deferred 分支寫成「先檢查 required 附件再跳過」的話,那些測試與既有附件測試**全都
    不會紅**,而生產上的預設呼叫正好會撞到 —— live 集帶齊附件、deferred 集刻意不帶,
    這樣附件 gate 只可能因為 deferred 那一集而 raise。"""
    captured = _install_mock(monkeypatch)
    live = {
        **_live_ep(tmp_path, 1),
        "slides_pdf_path": _slides(tmp_path, "live1"),
        "report_md_path": _report(tmp_path, "live1"),
    }
    bare_deferred = {"episode": 2, "title": "第2集", "publication_state": "deferred"}
    manifest = _manifest(tmp_path, [live, bare_deferred], "defaults_deferred.json")
    res = await _publish_with_defaults(manifest, artwork_png)
    assert res["deferred_episodes"] == [2]
    assert res["episode_count"] == 1
    assert not any(c["name"].startswith("EP02") for c in captured)


async def test_return_episodes_does_not_shrink_the_withheld_report(
    env, tmp_path, artwork_png, monkeypatch
):
    """`return_episodes` 只縮 `episodes`,**不縮 `deferred_episodes`**。

    把 withheld 清單也拿去跟 return_episodes 取交集的話,滾動加集的常見呼叫
    (`return_episodes=[新集]`)就永遠回空的 withheld —— 呼叫端看到 episode_count
    少一集卻沒有任何解釋,正好是這個欄位存在要防的「看起來像漏集」。"""
    _install_mock(monkeypatch)
    manifest = _manifest(
        tmp_path,
        [_live_ep(tmp_path, 1), _deferred_ep_production_shape(tmp_path, 2)],
        "return_filter_deferred.json",
    )
    res = await _publish(manifest, artwork_png, return_episodes=[1])
    assert [ep["n"] for ep in res["episodes"]] == [1]
    assert res["deferred_episodes"] == [2]


async def test_both_sides_read_the_shared_whitelist_not_a_hardcoded_string(
    env, tmp_path, artwork_png, monkeypatch
):
    """writer 與 publisher 都必須真的**用**那份共用白名單,不是各自寫死 `"deferred"`。

    identity 測試只證明常數是同一個物件 —— 兩邊同時改成硬編碼字面值、常數留著沒人用,
    它照樣綠。這裡塞一個不存在的 sentinel 狀態進**兩個參照點**,然後跑一次完整的
    writer → publisher 往返:writer 用它自己 `from … import` 進來的那份;publisher(以及
    兩支修 `published_at` 的腳本)走 `publish/state.is_withheld`,它在執行時查 state 模組的
    全域。任一邊改成寫死 `"deferred"`,對應那個 patch 就不生效 → 紅。"""
    from notebooklm_mcp import tools_artifacts
    from notebooklm_mcp.publish import state as state_mod

    sentinel = frozenset({"embargoed"})
    monkeypatch.setattr(tools_artifacts, "WITHHELD_PUBLICATION_STATES", sentinel)
    monkeypatch.setattr(state_mod, "WITHHELD_PUBLICATION_STATES", sentinel)

    _install_mock(monkeypatch)
    manifest = _manifest(
        tmp_path, [_live_ep(tmp_path, 1), _live_ep(tmp_path, 2)], "sentinel_state.json"
    )
    # writer 認 sentinel、不認原本的 "deferred"
    with pytest.raises(ValueError, match="unknown publication_state"):
        await tools_artifacts.episode_set_publication_state(
            manifest, 2, "deferred", reason="原本的值現在不在白名單裡"
        )
    await tools_artifacts.episode_set_publication_state(
        manifest, 2, "embargoed", reason="sentinel 狀態"
    )
    # publisher 也認 sentinel
    res = await _publish(manifest, artwork_png)
    assert res["deferred_episodes"] == [2]
    assert [ep["n"] for ep in res["episodes"]] == [1]


async def test_every_withheld_episode_is_reported_not_just_the_last(
    env, tmp_path, artwork_png, monkeypatch
):
    """`deferred_episodes` 要列**全部**被扣下的集號。

    只回報最後一筆(單元素覆寫)在單一 deferred 集的測試裡看不出來,而整季 republish
    正是多集 deferred 的場合 —— 少報就是「發布漏集」重新長回來。"""
    _install_mock(monkeypatch)
    manifest = _manifest(
        tmp_path,
        [
            _deferred_ep_production_shape(tmp_path, 1),
            _live_ep(tmp_path, 2),
            {"episode": 3, "title": "第3集", "publication_state": "deferred"},
        ],
        "multi_deferred.json",
    )
    res = await _publish(manifest, artwork_png)
    assert res["deferred_episodes"] == [1, 3]
    assert res["episode_count"] == 1


async def test_forbidden_xml_character_fails_before_any_put(
    env, tmp_path, artwork_png, monkeypatch
):
    captured = _install_mock(monkeypatch)
    manifest = _two_episode_manifest(tmp_path)

    with pytest.raises(ValueError, match=r"XML 1\.0.*U\+0001"):
        await _publish(manifest, artwork_png, show_title="AI\x01新聞")

    assert captured == []


async def test_forbidden_xml_character_in_non_feed_field_does_not_block_publish(
    env, tmp_path, artwork_png, monkeypatch
):
    captured = _install_mock(monkeypatch)
    manifest = _two_episode_manifest(tmp_path)
    with open(manifest, encoding="utf-8") as manifest_file:
        data = json.load(manifest_file)
    data["episodes"][0]["brief"] = "internal\x01note"
    with open(manifest, "w", encoding="utf-8") as manifest_file:
        json.dump(data, manifest_file, ensure_ascii=False)
    monkeypatch.setattr(tools_publish, "_audio_duration_hms", lambda _path: None)

    async def run_inline(function, *args):
        return function(*args)

    monkeypatch.setattr(tools_publish.asyncio, "to_thread", run_inline)

    result = await _publish(manifest, artwork_png)

    assert result["episode_count"] == 2
    assert any(item["name"] == "feed.xml" for item in captured)


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
    """已填路徑但檔案不存在:即使 require_* 關掉也要擋,而且在任何 PUT 之前。"""
    captured = _install_mock(monkeypatch)
    manifest = _manifest(tmp_path, [{
        "episode": 1, "title": "第1集", "description": "本集重點。",
        "mp3_path": _write_mp3(tmp_path, "e1b.mp3", b"a"),
        "cover_path": _valid_cover(tmp_path, "attm-cover.png"),
        "slides_pdf_path": str(tmp_path / "does-not-exist.pdf"),
    }], "att_missing.json")
    with pytest.raises(ValueError, match="slides_pdf_path"):
        await _publish(manifest, artwork_png)
    assert captured == []            # 檢查已提前,不再是「跑到那一集才爆」


# ---- v0.3.3:fail-closed required-deliverable preflight gate --------------------
# EP36:三個生成(音檔/簡報/講義)是獨立背景呼叫,完成訊號分散,manifest 是唯一匯流點。
# 舊行為「缺路徑就靜默不附」讓「還在生成」與「使用者不要」長得一樣,於是發布早於交付完成。


def _ep_with(tmp_path, tag, **extra):
    ep = {
        "episode": 1, "title": "第1集", "description": "本集重點整理。",
        "mp3_path": _write_mp3(tmp_path, f"{tag}.mp3", b"a"),
        "cover_path": _valid_cover(tmp_path, f"{tag}-cover.png"),
    }
    ep.update(extra)
    return _manifest(tmp_path, [ep], f"{tag}.json")


def _slides(tmp_path, tag="gate"):
    return _write_mp3(tmp_path, f"{tag}-slides.pdf", b"%PDF-1.4 x")


def _report(tmp_path, tag="gate"):
    md = tmp_path / f"{tag}-report.md"
    md.write_text("# 講義\n\n- 重點", encoding="utf-8")
    return str(md)


async def test_missing_slides_path_blocks_publish_before_any_put(env, tmp_path, artwork_png, monkeypatch):
    captured = _install_mock(monkeypatch)
    manifest = _ep_with(tmp_path, "no_slides", report_md_path=_report(tmp_path, "no_slides"))
    with pytest.raises(ValueError, match="slides_pdf_path 未回寫"):
        await _publish(manifest, artwork_png, require_slides=True, require_report=True)
    assert captured == []


async def test_missing_report_path_blocks_publish_before_any_put(env, tmp_path, artwork_png, monkeypatch):
    captured = _install_mock(monkeypatch)
    manifest = _ep_with(tmp_path, "no_report", slides_pdf_path=_slides(tmp_path, "no_report"))
    with pytest.raises(ValueError, match="report_md_path 未回寫"):
        await _publish(manifest, artwork_png, require_slides=True, require_report=True)
    assert captured == []


async def test_require_slides_false_still_requires_report(env, tmp_path, artwork_png, monkeypatch):
    """兩個旗標必須各自獨立——被錯接成同一條件時這個測試會紅。"""
    _install_mock(monkeypatch)
    # 缺簡報但關掉了 → 放行
    ok = _ep_with(tmp_path, "slides_off_ok", report_md_path=_report(tmp_path, "slides_off_ok"))
    await _publish(ok, artwork_png, require_slides=False, require_report=True)
    # 缺講義而 require_report 仍開 → 擋
    bad = _ep_with(tmp_path, "slides_off_bad", slides_pdf_path=_slides(tmp_path, "slides_off_bad"))
    with pytest.raises(ValueError, match="report_md_path 未回寫"):
        await _publish(bad, artwork_png, require_slides=False, require_report=True)


async def test_require_report_false_still_requires_slides(env, tmp_path, artwork_png, monkeypatch):
    _install_mock(monkeypatch)
    ok = _ep_with(tmp_path, "report_off_ok", slides_pdf_path=_slides(tmp_path, "report_off_ok"))
    await _publish(ok, artwork_png, require_slides=True, require_report=False)
    bad = _ep_with(tmp_path, "report_off_bad", report_md_path=_report(tmp_path, "report_off_bad"))
    with pytest.raises(ValueError, match="slides_pdf_path 未回寫"):
        await _publish(bad, artwork_png, require_slides=True, require_report=False)


async def test_both_attachments_present_passes_default_gate(env, tmp_path, artwork_png, monkeypatch):
    """兩附件齊備時,**production 預設**必須正常發布——刻意不傳 require_*,否則把預設改成
    False 這個測試還會綠,就鎖不住「預設 fail-closed」。"""
    captured = _install_mock(monkeypatch)
    manifest = _ep_with(
        tmp_path, "both",
        slides_pdf_path=_slides(tmp_path, "both"),
        report_md_path=_report(tmp_path, "both"),
    )
    res = await _publish_with_defaults(manifest, artwork_png)
    assert res["episode_count"] == 1
    names = [c["name"] for c in captured]
    assert any(n.endswith(".pdf") for n in names) and any(n.endswith(".html") for n in names)


async def test_unresolvable_mp3_blocks_publish_before_any_put(env, tmp_path, artwork_png, monkeypatch):
    """MP3 在任何 PUT 之前就 resolve:EP02 抓不到時,EP01 的 mp3/封面不該已經上傳。

    只檢查 artifact_id 存在不夠——_ensure_local_mp3 還要 notebook_id,而且遠端下載
    本身可能失敗;所以整季一律先 resolve 成真正存在的本機檔。"""
    import json
    captured = _install_mock(monkeypatch)
    mpath = _two_episode_manifest(tmp_path, filename="mp3_gate.json")
    data = json.loads(open(mpath, encoding="utf-8").read())
    os.unlink(data["episodes"][1]["mp3_path"])          # EP02 音檔不見、也沒有 artifact_id
    open(mpath, "w", encoding="utf-8").write(json.dumps(data, ensure_ascii=False))
    with pytest.raises(ValueError, match="no artifact_id"):
        await _publish(mpath, artwork_png)
    assert captured == []                               # EP01 一個 blob 都沒落地


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


def test_embed_cover_normalizes_notebooklm_mp4_to_real_mp3(tmp_path):
    """NotebookLM 的 fragmented MP4/AAC 即使副檔名叫 .mp3，發布器也必須輸出真正
    MP3 + ID3 APIC，讓副檔名與 RSS audio/mpeg 不再說謊；同輸入輸出必須決定性。"""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("需要 ffmpeg")
    # 造一個 fragmented AAC MP4，模擬 NotebookLM 的 DASH 下載檔。
    src = tmp_path / "dash.mp3"
    subprocess.run(
        [ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
         "-t", "1", "-c:a", "aac",
         "-movflags", "frag_keyframe+empty_moov+default_base_moof", "-f", "mp4", str(src)],
        check=True,
    )
    assert "moof" in _atoms(str(src))                    # 前置:輸入真的是分段檔
    cover = _valid_cover(tmp_path, "c.png")

    out = _real_embed(str(src), cover)
    fixed = tmp_path / "fixed.mp3"
    fixed.write_bytes(out)
    probe = json.loads(subprocess.check_output(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "format=format_name:stream=codec_name,bit_rate,sample_rate,channels",
         "-of", "json", str(fixed)],
        text=True,
    ))
    stream = probe["streams"][0]
    assert probe["format"]["format_name"] == "mp3"
    assert stream["codec_name"] == "mp3"
    assert stream["bit_rate"] == "256000"
    assert stream["sample_rate"] == "44100"
    assert stream["channels"] == 2
    from mutagen.id3 import ID3
    assert ID3(str(fixed)).getall("APIC")
    assert _real_embed(str(src), cover) == out           # 決定性:同輸入同 bytes


def test_embed_cover_accepts_mp4_with_leading_free_box(tmp_path):
    """MP4 不保證 ftyp 固定在 byte 4；leading free box 仍應由 ffprobe 正確辨識。"""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("需要 ffmpeg")
    plain = tmp_path / "plain.m4a"
    subprocess.run(
        [ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
         "-t", "1", "-c:a", "aac", str(plain)],
        check=True,
    )
    prefixed = tmp_path / "prefixed.mp3"
    prefixed.write_bytes(b"\x00\x00\x00\x08free" + plain.read_bytes())

    out = _real_embed(str(prefixed), _valid_cover(tmp_path, "free-cover.jpg"))
    fixed = tmp_path / "free-fixed.mp3"
    fixed.write_bytes(out)
    probe = subprocess.check_output(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "format=format_name:stream=codec_name",
         "-of", "default=nw=1", str(fixed)], text=True)
    assert "format_name=mp3" in probe and "codec_name=mp3" in probe


def test_embed_cover_preserves_real_mp3_and_adds_id3_artwork(tmp_path):
    """若呼叫端已把 NotebookLM AAC 轉成真正 MP3，發布器不能再把它 remux 回 MP4
    卻仍用 `.mp3`/`audio/mpeg` 宣告。輸出必須維持 MP3、內嵌 ID3 APIC，且 bytes 決定性。"""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("需要 ffmpeg")
    src = tmp_path / "real.mp3"
    subprocess.run(
        [ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
         "-t", "1", "-c:a", "libmp3lame", "-b:a", "128k", str(src)],
        check=True,
    )
    cover = _valid_cover(tmp_path, "mp3-cover.jpg")

    out = _real_embed(str(src), cover)
    fixed = tmp_path / "fixed.mp3"
    fixed.write_bytes(out)
    codec = subprocess.check_output(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=codec_name", "-of", "default=nw=1:nk=1", str(fixed)],
        text=True,
    ).strip()
    assert codec == "mp3"
    from mutagen.id3 import ID3
    assert ID3(str(fixed)).getall("APIC")
    assert _real_embed(str(src), cover) == out


def test_embed_cover_rejects_unsupported_adts_aac(tmp_path):
    """不是 MP3、也不是 NotebookLM MP4/AAC 的輸入必須 fail-closed，不能只因
    bytes[4:8] != ftyp 就被當成 MP3 後以 audio/mpeg 發布。"""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("需要 ffmpeg")
    src = tmp_path / "raw.aac"
    subprocess.run(
        [ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
         "-t", "1", "-c:a", "aac", "-f", "adts", str(src)],
        check=True,
    )
    with pytest.raises(ValueError, match="unsupported audio container/codec"):
        _real_embed(str(src), _valid_cover(tmp_path, "unsupported-cover.jpg"))


def test_embed_cover_reports_missing_ffprobe(tmp_path, monkeypatch):
    src = tmp_path / "source.mp3"
    src.write_bytes(b"audio")
    cover = _valid_cover(tmp_path, "missing-ffprobe-cover.jpg")

    def missing_ffprobe(*_args, **_kwargs):
        raise FileNotFoundError("ffprobe")

    monkeypatch.setattr(tools_publish.subprocess, "check_output", missing_ffprobe)

    with pytest.raises(ValueError, match="ffprobe.*required"):
        _real_embed(str(src), cover)


def test_embed_cover_reports_missing_ffmpeg(tmp_path, monkeypatch):
    src = tmp_path / "source.mp3"
    src.write_bytes(b"audio")
    cover = _valid_cover(tmp_path, "missing-ffmpeg-cover.jpg")
    monkeypatch.setattr(
        tools_publish.subprocess,
        "check_output",
        lambda *_args, **_kwargs: json.dumps(
            {
                "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2"},
                "streams": [{"codec_name": "aac"}],
            }
        ),
    )

    def missing_ffmpeg(*_args, **_kwargs):
        raise FileNotFoundError("ffmpeg")

    monkeypatch.setattr(tools_publish.subprocess, "run", missing_ffmpeg)

    with pytest.raises(ValueError, match="ffmpeg.*required"):
        _real_embed(str(src), cover)


def test_embed_cover_ffmpeg_call_includes_timeout(tmp_path, monkeypatch):
    """M4:ffprobe(30s)/Chrome(180s)都有 timeout,ffmpeg 是唯一沒有的外部命令——
    補上避免卡住的轉檔行程讓整季發布永遠掛著。"""
    src = tmp_path / "source.mp3"
    src.write_bytes(b"audio")
    cover = _valid_cover(tmp_path, "ffmpeg-timeout-cover.jpg")
    monkeypatch.setattr(
        tools_publish.subprocess, "check_output",
        lambda *_a, **_k: json.dumps({
            "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2"},
            "streams": [{"codec_name": "aac"}],
        }),
    )
    captured_kwargs = {}

    def fake_run(*_args, **kwargs):
        captured_kwargs.update(kwargs)
        raise FileNotFoundError("ffmpeg")   # 沿用既有「找不到 ffmpeg」錯誤路徑

    monkeypatch.setattr(tools_publish.subprocess, "run", fake_run)

    with pytest.raises(ValueError, match="ffmpeg.*required"):
        _real_embed(str(src), cover)
    assert captured_kwargs.get("timeout") == 600


def test_embed_cover_ffmpeg_timeout_raises_value_error(tmp_path, monkeypatch):
    """ffmpeg 卡住超過 timeout 必須 fail-closed 成 ValueError,與 ffprobe 那支的
    錯誤契約一致,不是讓原生 subprocess.TimeoutExpired 往外傳。"""
    src = tmp_path / "source.mp3"
    src.write_bytes(b"audio")
    cover = _valid_cover(tmp_path, "ffmpeg-hang-cover.jpg")
    monkeypatch.setattr(
        tools_publish.subprocess, "check_output",
        lambda *_a, **_k: json.dumps({
            "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2"},
            "streams": [{"codec_name": "aac"}],
        }),
    )

    def hanging_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs.get("timeout"))

    monkeypatch.setattr(tools_publish.subprocess, "run", hanging_run)

    with pytest.raises(ValueError, match="ffmpeg.*timed out"):
        _real_embed(str(src), cover)


def test_embed_cover_ffmpeg_nonzero_exit_raises_value_error(tmp_path, monkeypatch):
    """ffmpeg 非 0 退出碼(check=True 觸發的 CalledProcessError)必須 fail-closed 成
    ValueError 且帶 returncode,與 ffprobe 那支的錯誤契約一致,不是讓原生
    subprocess.CalledProcessError 往外傳。"""
    src = tmp_path / "source.mp3"
    src.write_bytes(b"audio")
    cover = _valid_cover(tmp_path, "ffmpeg-nonzero-cover.jpg")
    monkeypatch.setattr(
        tools_publish.subprocess, "check_output",
        lambda *_a, **_k: json.dumps({
            "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2"},
            "streams": [{"codec_name": "aac"}],
        }),
    )

    def failing_run(*args, **kwargs):
        raise subprocess.CalledProcessError(returncode=1, cmd=args[0])

    monkeypatch.setattr(tools_publish.subprocess, "run", failing_run)

    with pytest.raises(ValueError, match="returncode=1"):
        _real_embed(str(src), cover)


async def test_embed_cover_runs_off_event_loop_thread(env, tmp_path, artwork_png, monkeypatch):
    """M4:整季逐集轉檔期間不能凍住事件迴圈——_embed_cover 必須跑在
    asyncio.to_thread,不是同步卡在 publish_series 的協程裡(比照
    tools_basic.py:159-161 對便宜檔案 I/O 的既有理由)。"""
    import threading
    _install_mock(monkeypatch)
    main_thread = threading.current_thread()
    seen = {}

    def spy(mp3_path, cover_path):
        seen["thread"] = threading.current_thread()
        return open(mp3_path, "rb").read()

    monkeypatch.setattr(tools_publish, "_embed_cover", spy)
    await _publish(_two_episode_manifest(tmp_path), artwork_png)
    assert seen["thread"] is not main_thread


async def test_audio_duration_hms_runs_off_event_loop_thread(env, tmp_path, artwork_png, monkeypatch):
    """ffprobe 是同步 subprocess,跟 _embed_cover 同理——上傳迴圈內呼叫
    _audio_duration_hms 必須跑在 asyncio.to_thread,不是同步卡在協程裡。"""
    import threading
    _install_mock(monkeypatch)
    main_thread = threading.current_thread()
    seen = {}

    def spy(path):
        seen["thread"] = threading.current_thread()
        return "00:00:01"

    monkeypatch.setattr(tools_publish, "_audio_duration_hms", spy)
    await _publish(_two_episode_manifest(tmp_path), artwork_png)
    assert seen["thread"] is not main_thread


async def test_publish_real_notebooklm_mp4_uploads_genuine_mp3(
        env, tmp_path, artwork_png, monkeypatch):
    """端到端鎖住發布契約：真 NotebookLM-like MP4/AAC 經 publish_series 後，
    上傳 bytes 必須是 MP3，且 enclosure 同時使用 .mp3 與 audio/mpeg。"""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("需要 ffmpeg")
    captured = _install_mock(monkeypatch)
    monkeypatch.setattr(tools_publish, "_embed_cover", _real_embed)
    src = tmp_path / "notebooklm.mp3"
    subprocess.run(
        [ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
         "-t", "1", "-c:a", "aac",
         "-movflags", "frag_keyframe+empty_moov+default_base_moof", "-f", "mp4", str(src)],
        check=True,
    )
    manifest = _manifest(tmp_path, [{
        "episode": 1, "title": "第1集", "description": "本集重點。",
        "mp3_path": str(src), "cover_path": _valid_cover(tmp_path, "e2e-cover.jpg"),
    }], "real-media.json")

    await _publish(manifest, artwork_png)
    media = next(c for c in captured
                 if c["name"].startswith("EP01-") and c["name"].endswith(".mp3"))
    published = tmp_path / media["name"]
    published.write_bytes(media["content"])
    probe = subprocess.check_output(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "format=format_name:stream=codec_name",
         "-of", "default=nw=1", str(published)], text=True)
    assert "format_name=mp3" in probe and "codec_name=mp3" in probe
    feedxml = next(c["content"] for c in captured if c["name"] == "feed.xml")
    enclosure = ET.fromstring(feedxml).find("channel/item/enclosure")
    assert enclosure is not None
    url = enclosure.get("url")
    length = enclosure.get("length")
    assert url is not None and url.endswith(media["name"])
    assert enclosure.get("type") == "audio/mpeg"
    assert length is not None and int(length) == len(media["content"])


# ── v0.2.8:notebook_id 選填 + return_episodes 回傳過濾 ──────────────────────────

async def test_publish_without_notebook_id(env, tmp_path, artwork_png, monkeypatch):
    """mp3 都在本機時 notebook_id 完全不需要(它只是重抓 fallback)。"""
    _install_mock(monkeypatch)
    res = await _publish(_two_episode_manifest(tmp_path), artwork_png, notebook_id=None)
    assert res["episode_count"] == 2 and len(res["episodes"]) == 2


async def test_missing_mp3_prefers_episode_notebook_id(env, tmp_path, artwork_png, monkeypatch, fake_client):
    """掉檔重抓:該集自己的 manifest notebook_id 優先於呼叫端傳的 show 層 fallback
    (每集獨立筆記本,單一 notebook_id 會抓錯本)。fake download 不落檔 → 以
    「produced no file」錯誤證明 download 已對正確筆記本發出。"""
    import json
    _install_mock(monkeypatch)
    mpath = _two_episode_manifest(tmp_path)
    data = json.loads(open(mpath, encoding="utf-8").read())
    ep1 = data["episodes"][0]
    os.unlink(ep1["mp3_path"])                      # 模擬 output/ 被清掉
    ep1["artifact_id"] = "a-1"
    ep1["notebook_id"] = "nb-ep1"                   # 每集自己的筆記本
    open(mpath, "w", encoding="utf-8").write(json.dumps(data, ensure_ascii=False))
    fake_client.artifacts.download_audio_bytes = None  # 此案例刻意模擬 SDK 沒落檔
    with pytest.raises(ValueError, match="produced no file"):
        await _publish(mpath, artwork_png, notebook_id="nb-show-level")
    dl = next(c[1] for c in fake_client.artifacts.calls if c[0] == "download")
    assert dl["notebook_id"] == "nb-ep1"            # 不是 nb-show-level


async def test_missing_mp3_redownloads_when_parent_directory_is_gone(
    env, tmp_path, artwork_png, monkeypatch, fake_client
):
    """取代版 mp3 落在 `attempts/<attempt_id>/`,那層目錄最容易被整個清掉(或把 manifest
    搬到另一台發布機)。download_atomically 在 dirname(final) 裡 mkstemp,父目錄不在就
    FileNotFoundError——而「mp3 不見了就重抓」正是 _ensure_local_mp3 宣稱要處理的情境。"""
    import json
    _install_mock(monkeypatch)
    mpath = _two_episode_manifest(tmp_path)
    data = json.loads(open(mpath, encoding="utf-8").read())
    ep1 = data["episodes"][0]
    os.unlink(ep1["mp3_path"])
    # 整個 attempts/<id>/ 目錄都不存在,只有 manifest 記得那個路徑
    ep1["mp3_path"] = str(tmp_path / "attempts" / "att-1" / "ep01.mp3")
    ep1["artifact_id"] = "a-1"
    ep1["notebook_id"] = "nb-ep1"
    open(mpath, "w", encoding="utf-8").write(json.dumps(data, ensure_ascii=False))

    await _publish(mpath, artwork_png)                  # 不得 FileNotFoundError

    assert os.path.getsize(ep1["mp3_path"]) > 0         # 真的重抓落檔了


async def test_download_audio_value_error_propagates_unrewritten(
    env, tmp_path, artwork_png, monkeypatch, fake_client
):
    """download_audio 自己丟的 ValueError(artifact 不存在等)不該被 _ensure_local_mp3
    改寫成「produced no file」——那個訊息只留給 _atomic 的空檔案語意,否則遠端真正的
    錯誤原因被蓋掉,誤導診斷。"""
    import json
    _install_mock(monkeypatch)
    mpath = _two_episode_manifest(tmp_path)
    data = json.loads(open(mpath, encoding="utf-8").read())
    ep1 = data["episodes"][0]
    os.unlink(ep1["mp3_path"])
    ep1["artifact_id"] = "a-1"
    ep1["notebook_id"] = "nb-ep1"
    open(mpath, "w", encoding="utf-8").write(json.dumps(data, ensure_ascii=False))
    fake_client.artifacts.download_audio_exc = ValueError("artifact a-1 not found in notebook nb-ep1")
    with pytest.raises(ValueError, match="not found in notebook"):
        await _publish(mpath, artwork_png)


async def test_interrupted_mp3_redownload_leaves_no_partial_file(
    env, tmp_path, artwork_png, monkeypatch, fake_client
):
    """M1:重抓 mp3 中斷(寫半個檔就拋例外)必須是原子換檔——最終路徑不得殘留 partial
    檔,也不得被半份檔案頂替(這裡最終路徑等於「已刪除」的舊 mp3_path)。"""
    import json
    _install_mock(monkeypatch)
    mpath = _two_episode_manifest(tmp_path)
    data = json.loads(open(mpath, encoding="utf-8").read())
    ep1 = data["episodes"][0]
    mp3_path = ep1["mp3_path"]
    os.unlink(mp3_path)                              # 模擬 output/ 被清掉，需要重抓
    ep1["artifact_id"] = "a-1"
    ep1["notebook_id"] = "nb-ep1"
    open(mpath, "w", encoding="utf-8").write(json.dumps(data, ensure_ascii=False))

    fake_client.artifacts.download_audio_partial_bytes = b"only-half-a-file"
    fake_client.artifacts.download_audio_exc = ConnectionError("dropped mid-transfer")

    with pytest.raises(ConnectionError, match="dropped mid-transfer"):
        await _publish(mpath, artwork_png)

    assert not os.path.exists(mp3_path)               # 沒有半份檔頂替（原檔已刪除）
    leftovers = [f for f in os.listdir(os.path.dirname(mp3_path)) if f.endswith(".part")]
    assert leftovers == []                            # temp 檔也清乾淨了


async def test_staging_dir_is_cleaned_up_after_publish(env, tmp_path, artwork_png, monkeypatch, fake_client):
    """staging 目錄由 publish_series 用 TemporaryDirectory 包住整個 preflight+上傳段,
    離開(成功或例外)都自動清乾淨——不是每次缺檔各自 mkdtemp() 留一個目錄無界累積。"""
    _install_mock(monkeypatch)
    mpath = _two_episode_manifest(tmp_path)
    data = json.loads(open(mpath, encoding="utf-8").read())
    ep1 = data["episodes"][0]
    os.unlink(ep1["mp3_path"])                      # 觸發重抓分支,才會用到 staging_dir
    ep1["artifact_id"] = "a-1"
    ep1["notebook_id"] = "nb-ep1"
    open(mpath, "w", encoding="utf-8").write(json.dumps(data, ensure_ascii=False))

    seen: dict = {}
    real_ensure = tools_publish._ensure_local_mp3

    async def spy(ep, fallback_notebook_id, staging_dir):
        seen["dir"] = staging_dir
        assert os.path.isdir(staging_dir)           # 用到的當下目錄確實存在
        return await real_ensure(ep, fallback_notebook_id, staging_dir)

    monkeypatch.setattr(tools_publish, "_ensure_local_mp3", spy)
    await _publish(mpath, artwork_png)
    assert seen.get("dir")
    assert not os.path.exists(seen["dir"])           # publish 結束後自動清掉


async def test_missing_mp3_without_any_notebook_fails_clearly(env, tmp_path, artwork_png, monkeypatch):
    import json
    _install_mock(monkeypatch)
    mpath = _two_episode_manifest(tmp_path)
    data = json.loads(open(mpath, encoding="utf-8").read())
    ep1 = data["episodes"][0]
    os.unlink(ep1["mp3_path"])
    ep1["artifact_id"] = "a-1"                      # 有 artifact 但無任何 notebook_id
    open(mpath, "w", encoding="utf-8").write(json.dumps(data, ensure_ascii=False))
    with pytest.raises(ValueError, match="no notebook_id"):
        await _publish(mpath, artwork_png, notebook_id=None)


async def test_return_episodes_filters_response_only(env, tmp_path, artwork_png, monkeypatch):
    """return_episodes 只縮回傳:整季照常 PUT(含未列的 EP01),episode_count 仍全季。"""
    captured = _install_mock(monkeypatch)
    res = await _publish(_two_episode_manifest(tmp_path), artwork_png, return_episodes=[2])
    assert [e["n"] for e in res["episodes"]] == [2]
    assert res["episode_count"] == 2
    names = [c["name"] for c in captured]
    assert any(n.startswith("EP01-") and n.endswith(".mp3") for n in names)


# ---- v0.3.3 review fixes:附件政策要沿用,preflight 要真的擋在第一個 PUT 前 -----------


async def test_attachment_policy_persists_for_rolling_publish(env, tmp_path, artwork_png, monkeypatch):
    """季級政策必須存進 manifest。否則首發合法地用 require_slides=False 發完之後,照文件
    只傳 manifest_path 做滾動加集,新呼叫會回到預設 True、掃到缺簡報的舊集直接 raise。"""
    _install_mock(monkeypatch)
    manifest = _ep_with(tmp_path, "policy", report_md_path=_report(tmp_path, "policy"))

    await _publish(manifest, artwork_png, require_slides=False, require_report=True)

    saved = json.loads(open(manifest, encoding="utf-8").read())["show"]
    assert saved["require_slides"] is False and saved["require_report"] is True

    # 滾動加集:照文件只傳 manifest_path,必須沿用而不是回到預設 True
    res = await tools_publish.publish_series(manifest_path=manifest)
    assert res["episode_count"] == 1


async def test_explicit_policy_overrides_and_rewrites_saved_value(env, tmp_path, artwork_png, monkeypatch):
    """顯式參數永遠優先並回寫(與 show 七欄同一套沿用規則)。"""
    _install_mock(monkeypatch)
    manifest = _ep_with(tmp_path, "policy2", report_md_path=_report(tmp_path, "policy2"))
    await _publish(manifest, artwork_png, require_slides=False, require_report=True)

    with pytest.raises(ValueError, match="slides_pdf_path 未回寫"):
        await tools_publish.publish_series(manifest_path=manifest, require_slides=True)


async def test_later_episode_report_render_failure_uploads_nothing(env, tmp_path, artwork_png, monkeypatch):
    """render_report_html 對 script/外部資源 fail-closed,那是本機可預判的失敗。若它留在
    上傳迴圈內,EP01 的 mp3/封面會先落到 NAS 上才輪到 EP02 爆掉。"""
    captured = _install_mock(monkeypatch)
    eps = []
    for n in (1, 2):
        md = tmp_path / f"render{n}-report.md"
        md.write_text("# 講義\n\n<script>alert(1)</script>\n" if n == 2 else "# 講義\n\n- 重點",
                      encoding="utf-8")
        eps.append({
            "episode": n, "title": f"第{n}集", "description": f"第{n}集重點整理。",
            "mp3_path": _write_mp3(tmp_path, f"render{n}.mp3", f"audio-{n}".encode()),
            "cover_path": _valid_cover(tmp_path, f"render{n}-cover.png"),
            "slides_pdf_path": _slides(tmp_path, f"render{n}"),
            "report_md_path": str(md),
        })
    manifest = _manifest(tmp_path, eps, "render.json")

    with pytest.raises(Exception):
        await _publish(manifest, artwork_png, require_slides=True, require_report=True)
    assert captured == []            # EP01 一個 blob 都沒落地


async def test_later_episode_notes_render_failure_uploads_nothing(env, tmp_path, artwork_png, monkeypatch):
    """render_episode_notes_html 對 markdown 圖片等危險 body fail-closed,那也是本機可
    預判的失敗,guard 的呼叫點必須在 preflight,不能只在上傳迴圈內——否則 EP01 的
    mp3/封面會先落到 NAS 上才輪到 EP02 的 description 爆掉。"""
    captured = _install_mock(monkeypatch)
    eps = []
    for n in (1, 2):
        desc = "![t](https://evil.example/pixel.png)" if n == 2 else f"第{n}集重點整理。"
        eps.append({
            "episode": n, "title": f"第{n}集", "description": desc,
            "mp3_path": _write_mp3(tmp_path, f"notes{n}.mp3", f"audio-{n}".encode()),
            "cover_path": _valid_cover(tmp_path, f"notes{n}-cover.png"),
        })
    manifest = _manifest(tmp_path, eps, "notes_render.json")

    with pytest.raises(ValueError, match="自包含"):
        await _publish(manifest, artwork_png)
    assert captured == []            # EP01 一個 blob 都沒落地


async def test_defaults_are_fail_closed(env, tmp_path, artwork_png, monkeypatch):
    """鎖住「預設就是 fail-closed」本身。前一個測試兩附件齊備,預設翻成 False 也會綠,
    所以真正把預設值釘住的是這個:缺附件 + 完全不傳 require_*,必須擋。"""
    captured = _install_mock(monkeypatch)

    only_report = _ep_with(tmp_path, "defclosed_s", report_md_path=_report(tmp_path, "defclosed_s"))
    with pytest.raises(ValueError, match="slides_pdf_path 未回寫"):
        await _publish_with_defaults(only_report, artwork_png)

    only_slides = _ep_with(tmp_path, "defclosed_r", slides_pdf_path=_slides(tmp_path, "defclosed_r"))
    with pytest.raises(ValueError, match="report_md_path 未回寫"):
        await _publish_with_defaults(only_slides, artwork_png)

    assert captured == []


# ---- itunes:type 是季級設定(連載節目的排序根因) -----------------------------------


def _feed_of(captured):
    return next(c["content"].decode("utf-8") for c in captured if c["name"] == "feed.xml")


async def test_itunes_type_defaults_to_apple_implicit_episodic(
    env, tmp_path, artwork_png, monkeypatch
):
    """沒傳就是 `episodic` —— **不替呼叫端改節目語意**(那是 Apple 缺這個標籤時的預設)。

    但仍然**顯式輸出**:靠隱含預設等於把語意交給播放器猜,而這正是兩個連載 feed 排序
    錯亂的根因。
    """
    captured = _install_mock(monkeypatch)
    await _publish(_two_episode_manifest(tmp_path), artwork_png)
    assert "<itunes:type>episodic</itunes:type>" in _feed_of(captured)


async def test_serial_is_declared_and_persisted_as_a_season_setting(
    env, tmp_path, artwork_png, monkeypatch
):
    """傳一次就存進 manifest['show'],之後只傳 manifest_path 也沿用(同 show 七欄)。"""
    captured = _install_mock(monkeypatch)
    manifest = _numbered_manifest(tmp_path)
    await _publish(manifest, artwork_png, itunes_type="serial")
    assert "<itunes:type>serial</itunes:type>" in _feed_of(captured)

    stored = json.loads(open(manifest, encoding="utf-8").read())
    assert stored["show"]["itunes_type"] == "serial"

    # 滾動加集只傳 manifest_path:不能悄悄退回 episodic。
    captured.clear()
    await tools_publish.publish_series(manifest_path=manifest)
    assert "<itunes:type>serial</itunes:type>" in _feed_of(captured)


async def test_serial_does_not_change_episode_order_or_guids(
    env, tmp_path, artwork_png, monkeypatch
):
    """**只補宣告,不動排序與身分。** 重跑不需要重生音檔,Apple 視為同一節目更新。"""
    captured = _install_mock(monkeypatch)
    manifest = _numbered_manifest(tmp_path)
    before = await _publish(manifest, artwork_png)
    episodic_feed = _feed_of(captured)

    captured.clear()
    after = await _publish(manifest, artwork_png, itunes_type="serial")
    serial_feed = _feed_of(captured)

    assert before["token"] == after["token"]
    assert [e["guid"] for e in before["episodes"]] == [e["guid"] for e in after["episodes"]]
    assert [e["url"] for e in before["episodes"]] == [e["url"] for e in after["episodes"]]
    # 兩份 feed 的差異只有那一行宣告
    assert episodic_feed.replace(
        "<itunes:type>episodic</itunes:type>", "<itunes:type>serial</itunes:type>"
    ) == serial_feed


async def test_bad_itunes_type_is_refused_before_any_put(
    env, tmp_path, artwork_png, monkeypatch
):
    """值域驗證要在**任何遠端副作用之前**:feed.xml 是最後一個 PUT,拖到渲染才擋的話
    artwork/media 已經上傳了,呼叫端拿到「發布失敗」但遠端留下一半的檔案。"""
    captured = _install_mock(monkeypatch)
    with pytest.raises(ValueError, match="itunes_type"):
        await _publish(_two_episode_manifest(tmp_path), artwork_png, itunes_type="series")
    assert captured == []


async def test_explicit_itunes_type_overrides_the_persisted_one(
    env, tmp_path, artwork_png, monkeypatch
):
    """**反向 precedence 也要鎖。** 只驗「顯式 → 之後沿用」的話,把解析改壞成 saved 永遠
    優先(改回連載節目就再也切不回 episodic)照樣全綠。顯式參數永遠優先並回寫。"""
    captured = _install_mock(monkeypatch)
    manifest = _numbered_manifest(tmp_path)
    await _publish(manifest, artwork_png, itunes_type="serial")
    assert json.loads(open(manifest, encoding="utf-8").read())["show"]["itunes_type"] == "serial"

    captured.clear()
    await _publish(manifest, artwork_png, itunes_type="episodic")

    assert "<itunes:type>episodic</itunes:type>" in _feed_of(captured)
    assert json.loads(open(manifest, encoding="utf-8").read())["show"]["itunes_type"] == "episodic"


async def test_bad_itunes_type_persisted_in_the_manifest_still_blocks_every_put(
    env, tmp_path, artwork_png, monkeypatch
):
    """壞值**來自 manifest** 時也要在任何 PUT 之前擋掉。

    只驗顯式參數的話,「只驗參數、漏驗 saved_show」這個改壞法會讓壞 manifest 一路拖到
    渲染邊界才擋 —— 那時候 artwork/media 已經上傳出去了。
    """
    captured = _install_mock(monkeypatch)
    manifest = _two_episode_manifest(tmp_path)
    await _publish(manifest, artwork_png)

    stored = json.loads(open(manifest, encoding="utf-8").read())
    stored["show"]["itunes_type"] = "series"          # 手改/跨版本留下的壞值
    open(manifest, "w", encoding="utf-8").write(json.dumps(stored, ensure_ascii=False))

    captured.clear()
    with pytest.raises(ValueError, match="itunes_type"):
        await tools_publish.publish_series(manifest_path=manifest)
    assert captured == []


# ------------------------------------------------- serial 節目的標題必須帶集號
# `<itunes:episode>` 已經在 feed 裡,但**多數播放器不顯示它** —— 訂閱者在清單上看到的
# 只有標題。連載節目的價值就是順序,所以集號要進標題才是給人看的。擋在 publish 而不是
# 靠人記:同一個 feed 半數有前綴半數沒有,只有訂閱者看得見,host 這邊完全無感。

async def test_serial_refuses_a_title_without_its_episode_number(
    env, tmp_path, artwork_png, monkeypatch
):
    """連載節目缺前綴 → 在**任何 PUT 之前** raise。

    拖到渲染才擋沒有意義:feed.xml 是最後一個 PUT,那時候整季 mp3 已經上傳完了。
    """
    captured = _install_mock(monkeypatch)
    with pytest.raises(ValueError, match="EP01"):
        await _publish(_two_episode_manifest(tmp_path), artwork_png, itunes_type="serial")
    assert captured == []


async def test_serial_refuses_a_prefix_that_names_another_episode(
    env, tmp_path, artwork_png, monkeypatch
):
    """前綴的集號必須**等於**該集集號。

    只驗「有沒有 EP 開頭」的話,複製上一集 brief 時留下的 `EP07. ` 會原封不動上線,
    而 feed 裡 `<itunes:episode>8</itunes:episode>` 與標題各說各話 —— 那比沒有集號更糟。
    """
    captured = _install_mock(monkeypatch)
    manifest = _numbered_manifest(tmp_path)
    stored = json.loads(open(manifest, encoding="utf-8").read())
    stored["episodes"][1]["title"] = "EP07. 你在介面上看到的每個字"     # 這是第 2 集
    open(manifest, "w", encoding="utf-8").write(json.dumps(stored, ensure_ascii=False))

    with pytest.raises(ValueError, match="EP02"):
        await _publish(manifest, artwork_png, itunes_type="serial")
    assert captured == []


async def test_episodic_titles_are_never_touched_by_the_number_gate(
    env, tmp_path, artwork_png, monkeypatch
):
    """**射程只到 `serial`。** `episodic` 是時事型、由新到舊聽,集號沒有意義 ——
    Audicast 五十集都沒有前綴,把閘門套上去等於讓那個 feed 從此發不出去。
    """
    captured = _install_mock(monkeypatch)
    await _publish(_two_episode_manifest(tmp_path), artwork_png, itunes_type="episodic")
    assert "<title>第1集</title>" in _feed_of(captured)


async def test_serial_accepts_the_numbered_titles_verbatim(
    env, tmp_path, artwork_png, monkeypatch
):
    """通過閘門的標題原樣進 feed —— 閘門只驗,不改寫(改寫會讓 manifest 與 feed 不一致)。"""
    captured = _install_mock(monkeypatch)
    await _publish(_numbered_manifest(tmp_path), artwork_png, itunes_type="serial")
    feed = _feed_of(captured)
    assert "<title>EP01. 第1集</title>" in feed
    assert "<title>EP02. 第2集</title>" in feed


# ---------------------------------------------------------------- pubDate 排序
# `published_at` 存的是**生成完成時間**,亂序生成的季度會讓播放器(照 pubDate 排)把
# 集數顯示成亂序 —— 真實事故:EP01 最後才補,於是它在 App 裡變成「最新一集」。
# 這一區鎖住 preflight 擋在**任何 PUT 之前**,以及 scripts/reorder_published_at.py 的修法。

def _dated_live_ep(tmp_path, n, published_at=None):
    ep = _live_ep(tmp_path, n)
    if published_at is not None:
        ep["published_at"] = published_at
    return ep


async def test_out_of_order_pub_dates_block_every_put(
    env, tmp_path, artwork_png, monkeypatch
):
    """EP01 的 published_at 晚於 EP02(亂序生成的真實形狀)→ 一個 byte 都不准上傳。

    擋在渲染邊界是不夠的:feed host 永不刪檔,亂序的 feed.xml 一旦 PUT 出去,訂閱者的
    App 就已經拿到錯的順序了。
    """
    captured = _install_mock(monkeypatch)
    manifest = _two_episode_manifest(tmp_path, published_at={
        1: "Sat, 16 Aug 2026 21:33:00 +0800",       # 最後才補的 EP01
        2: "Sat, 16 Aug 2026 21:15:00 +0800",
    })
    with pytest.raises(ValueError, match="published_at 必須隨集號遞增"):
        await _publish(manifest, artwork_png)
    assert captured == []


async def test_identical_pub_dates_are_rejected(env, tmp_path, artwork_png, monkeypatch):
    """相等也要擋:同一秒的兩集在播放器裡順序不定,而「不定」不是可接受的發布結果。"""
    captured = _install_mock(monkeypatch)
    same = "Sat, 16 Aug 2026 21:15:00 +0800"
    manifest = _two_episode_manifest(tmp_path, published_at={1: same, 2: same})
    with pytest.raises(ValueError, match="published_at 必須隨集號遞增"):
        await _publish(manifest, artwork_png)
    assert captured == []


async def test_every_offending_pair_is_named_not_just_the_first(
    env, tmp_path, artwork_png, monkeypatch
):
    """訊息要列出**所有**違規的相鄰對。

    只報第一對的話,12 集的季度得跑十幾輪 publish 才知道要改哪些 —— 而每一輪都是一次
    「以為修好了」。真實那次要動 11 集。
    """
    _install_mock(monkeypatch)
    manifest = _manifest(
        tmp_path,
        [
            _dated_live_ep(tmp_path, 1, "Sat, 16 Aug 2026 23:00:00 +0800"),   # 比 EP02 晚
            _dated_live_ep(tmp_path, 2, "Sat, 16 Aug 2026 21:00:00 +0800"),
            _dated_live_ep(tmp_path, 3, "Sat, 16 Aug 2026 20:00:00 +0800"),   # 又比 EP02 早
        ],
        "multi_bad.json",
    )
    with pytest.raises(ValueError) as exc:
        await _publish(manifest, artwork_png)
    msg = str(exc.value)
    # 斷言**兩對完整的**「左 >= 右」。只檢查 EP01/EP02/EP03 三個字串出現的話,把第二對
    # 誤報成 `EP01 >= EP03`(漏掉真正的 `EP02 >= EP03`)照樣綠 —— 而那正好會讓人改錯集。
    assert "EP01 Sat, 16 Aug 2026 23:00:00 +0800 >= EP02" in msg
    assert "EP02 Sat, 16 Aug 2026 21:00:00 +0800 >= EP03" in msg


async def test_fallback_and_real_pub_dates_mixed_still_publish(
    env, tmp_path, artwork_png, monkeypatch
):
    """缺 published_at 的舊集走 2020 的 fallback,新集帶真實時間 —— 只要集號小的在前就
    仍然遞增,不能被這道檢查誤殺(滾動加集的常見形狀)。"""
    _install_mock(monkeypatch)
    manifest = _manifest(
        tmp_path,
        [
            _dated_live_ep(tmp_path, 1),                                      # 無 published_at
            _dated_live_ep(tmp_path, 2, "Sat, 16 Aug 2026 21:00:00 +0800"),
        ],
        "mixed_ok.json",
    )
    res = await _publish(manifest, artwork_png)
    assert res["episode_count"] == 2


async def test_unparseable_published_at_blocks_every_put(
    env, tmp_path, artwork_png, monkeypatch
):
    """壞掉的 published_at 原本會**原樣**進 `<pubDate>`(播放器解不出就自己編一個時間,
    或整條 item 掉)。單調性檢查本來就得 parse,所以順手在 PUT 之前 fail-loud。"""
    captured = _install_mock(monkeypatch)
    manifest = _two_episode_manifest(tmp_path, published_at={
        1: "2026-08-16T21:15:00+08:00",              # ISO-8601,不是 RFC-2822
        2: "Sat, 16 Aug 2026 21:33:00 +0800",
    })
    with pytest.raises(ValueError, match="episode 1: published_at"):
        await _publish(manifest, artwork_png)
    assert captured == []


async def test_withheld_episode_pub_date_never_gates_the_season(
    env, tmp_path, artwork_png, monkeypatch
):
    """deferred 集不進 feed → 它的時間戳不參與排序。

    三集的形狀是刻意的:兩集的話 withheld 掉一集就只剩單集,「有沒有排除它」測不出來。
    這裡 deferred EP02 的時間戳若被納入,EP02(23:00) >= EP03(11:00) 就會擋掉整季。
    """
    _install_mock(monkeypatch)
    deferred = _deferred_ep_production_shape(tmp_path, 2)
    deferred["published_at"] = "Sat, 16 Aug 2026 23:00:00 +0800"
    manifest = _manifest(
        tmp_path,
        [
            _dated_live_ep(tmp_path, 1, "Sat, 16 Aug 2026 10:00:00 +0800"),
            deferred,
            _dated_live_ep(tmp_path, 3, "Sat, 16 Aug 2026 11:00:00 +0800"),
        ],
        "deferred_dates.json",
    )
    res = await _publish(manifest, artwork_png)
    assert res["episode_count"] == 2 and res["deferred_episodes"] == [2]


def _reorder_script():
    """從路徑載入 scripts/reorder_published_at.py(它不是 package 的一部分)。"""
    import importlib.util

    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "scripts", "reorder_published_at.py",
    )
    spec = importlib.util.spec_from_file_location("reorder_published_at", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_reorder(monkeypatch, manifest_path, *args):
    import sys as _sys

    mod = _reorder_script()
    monkeypatch.setattr(_sys, "argv", ["reorder_published_at.py", manifest_path, *args])
    return mod.main()


def test_reorder_script_repairs_the_order_without_inventing_timestamps(monkeypatch, tmp_path):
    """重排 = 把**現有**時間戳依集號重新配對。集合不變(稽核上「這季何時做的」不變),
    順序變對。dry-run 不落地,--apply 才寫。"""
    manifest = _manifest(
        tmp_path,
        [
            _dated_live_ep(tmp_path, 1, "Sat, 16 Aug 2026 21:33:00 +0800"),
            _dated_live_ep(tmp_path, 2, "Sat, 16 Aug 2026 21:15:00 +0800"),
            _dated_live_ep(tmp_path, 3, "Sat, 16 Aug 2026 21:20:00 +0800"),
        ],
        "reorder.json",
    )
    before = json.loads(open(manifest, encoding="utf-8").read())
    original = sorted(ep["published_at"] for ep in before["episodes"])

    assert _run_reorder(monkeypatch, manifest) == 0                     # dry-run
    assert json.loads(open(manifest, encoding="utf-8").read()) == before

    assert _run_reorder(monkeypatch, manifest, "--apply") == 0
    after = json.loads(open(manifest, encoding="utf-8").read())["episodes"]
    dates = [parsedate_to_datetime(ep["published_at"]) for ep in after]
    assert dates == sorted(dates)                                       # 依集號遞增
    assert sorted(ep["published_at"] for ep in after) == original       # 沒發明新時間

    # 冪等:已經對了就不再改(revision 也不該再跳)。
    settled = json.loads(open(manifest, encoding="utf-8").read())
    assert _run_reorder(monkeypatch, manifest, "--apply") == 0
    assert json.loads(open(manifest, encoding="utf-8").read()) == settled


def test_reorder_script_refuses_a_manifest_with_a_missing_timestamp(monkeypatch, tmp_path):
    """缺 published_at 的集在 feed 裡走 2020 的 fallback,與真實時間混在一起時「重排現有
    值」修不了順序 —— 那要**發明**一個時間戳,不在這支的授權範圍內,所以報錯不猜。"""
    manifest = _manifest(
        tmp_path,
        [
            _dated_live_ep(tmp_path, 1, "Sat, 16 Aug 2026 21:33:00 +0800"),
            _dated_live_ep(tmp_path, 2),                                # 沒有 published_at
        ],
        "reorder_missing.json",
    )
    with pytest.raises(SystemExit, match="沒有 published_at"):
        _run_reorder(monkeypatch, manifest, "--apply")


def test_reorder_script_leaves_withheld_episodes_alone(monkeypatch, tmp_path):
    """deferred 集不進 feed → 不參與重排,它的時間戳原封不動。

    若把它也丟進池子,live 集會拿到不屬於自己的時間,而 deferred 那格的稽核時間被改掉。
    """
    deferred = _deferred_ep_production_shape(tmp_path, 2)
    deferred["published_at"] = "Sat, 16 Aug 2026 23:00:00 +0800"
    manifest = _manifest(
        tmp_path,
        [
            _dated_live_ep(tmp_path, 1, "Sat, 16 Aug 2026 12:00:00 +0800"),
            deferred,
            _dated_live_ep(tmp_path, 3, "Sat, 16 Aug 2026 11:00:00 +0800"),
        ],
        "reorder_deferred.json",
    )
    assert _run_reorder(monkeypatch, manifest, "--apply") == 0
    eps = {ep["episode"]: ep["published_at"]
           for ep in json.loads(open(manifest, encoding="utf-8").read())["episodes"]}
    assert eps[2] == "Sat, 16 Aug 2026 23:00:00 +0800"                  # 未被動到
    assert parsedate_to_datetime(eps[1]) < parsedate_to_datetime(eps[3])


# --- codex 獨立複審(v0.9.19)抓到的漏網:輸入驗證、值一致性、腳本的鎖語意 --------

@pytest.mark.parametrize(
    "raw",
    [
        "Sat, 16 Aug 2026 21:00:00",          # 完全沒有 offset
        "Sat, 16 Aug 2026 21:00:00 -0000",    # RFC-2822 的「時區未知」,Python parse 成 naive
    ],
    ids=["no_offset", "minus_0000"],
)
async def test_published_at_without_a_real_offset_is_rejected(
    env, tmp_path, artwork_png, monkeypatch, raw
):
    """缺 offset 不能就地假設一個。

    補 `+0800` 只修好「比較」那一半:渲染端送出的仍是原字串,於是 preflight 與播放器
    看到的是不同的時刻 —— 真的亂序的 feed 會被判成遞增而放行。具體形狀:
    EP01 `10:00`(無 offset)、EP02 `03:00 +0000` —— 當成 +0800 是 02:00Z < 03:00Z 放行,
    播放器按 UTC 讀卻是 10:00Z > 03:00Z,亂序照樣出去。
    """
    captured = _install_mock(monkeypatch)
    manifest = _two_episode_manifest(tmp_path, published_at={
        1: raw, 2: "Sun, 17 Aug 2026 03:00:00 +0000",
    })
    with pytest.raises(ValueError, match="缺時區 offset"):
        await _publish(manifest, artwork_png)
    assert captured == []


@pytest.mark.parametrize(
    "raw",
    [False, 0, [], {}, "", "   ", 123, [1], {"x": 1}],
    ids=["false", "zero", "empty_list", "empty_dict", "empty_str", "blank_str",
         "int", "list", "dict"],
)
async def test_non_string_published_at_never_falls_back_silently(
    env, tmp_path, artwork_png, monkeypatch, raw
):
    """欄位**存在**就必須是能解析的字串;缺席才是唯一該走 fallback 的情形。

    兩種漏網各有一半:falsy 的值(`false` / `0` / `[]` / `""`)被 `or` 當成缺值 → 靜默發布
    2020 的 fallback(那一集的 pubDate 被偷偷改掉);truthy 的非字串(`123` / `[1]` / dict)
    讓 `parsedate_to_datetime` 漏一個沒被接住的 `AttributeError` 出去。
    """
    captured = _install_mock(monkeypatch)
    manifest = _two_episode_manifest(tmp_path, published_at={
        1: raw, 2: "Sun, 17 Aug 2026 03:00:00 +0800",
    })
    with pytest.raises(ValueError, match="episode 1: published_at"):
        await _publish(manifest, artwork_png)
    assert captured == []


async def test_rendered_pub_date_is_the_same_value_the_preflight_checked(
    env, tmp_path, artwork_png, monkeypatch
):
    """preflight 與渲染必須看**同一顆值**。

    這條的存在理由是一個具體的假綠:把渲染端改成永遠 `_fallback_pub_date(n)`,新的排序
    gate 仍然正確執行(它看的是 manifest),但合法 manifest 會輸出 2020 的日期 —— 而原本
    那條混合測試只斷言 `episode_count`,完全抓不到。這裡直接鎖 `show.json` 的投影值。
    """
    captured = _install_mock(monkeypatch)
    explicit = "Sun, 16 Aug 2026 21:00:00 +0800"
    manifest = _manifest(
        tmp_path,
        [
            _dated_live_ep(tmp_path, 1),                    # 無 published_at → fallback
            _dated_live_ep(tmp_path, 2, explicit),          # 顯式值
        ],
        "same_value.json",
    )
    await _publish(manifest, artwork_png)
    eps = [json.loads(c["content"]) for c in captured if c["name"] == "show.json"][0]["episodes"]
    assert eps["1"]["pub_date"] == tools_publish._fallback_pub_date(1)
    assert eps["2"]["pub_date"] == explicit                 # 逐字,不是「解析後相等」
    feed = _feed_of(captured)
    assert f"<pubDate>{explicit}</pubDate>" in feed


def test_reorder_script_refuses_duplicate_timestamps(monkeypatch, tmp_path):
    """池子裡有重複時間戳 → 嚴格遞增靠換配對修不了,報錯不猜。

    這個分支沒測試會紅的話,刪掉它會讓腳本回「不需重排」,而 `publish_series` 仍永久
    擋著 —— 呼叫端會卡在「腳本說沒事、發布說不行」的死循環。
    """
    same = "Sat, 16 Aug 2026 21:00:00 +0800"
    manifest = _manifest(
        tmp_path,
        [_dated_live_ep(tmp_path, 1, same), _dated_live_ep(tmp_path, 2, same)],
        "reorder_dupe.json",
    )
    with pytest.raises(SystemExit, match="重複值"):
        _run_reorder(monkeypatch, manifest, "--apply")


def test_reorder_script_does_not_bump_revision_when_nothing_changes(monkeypatch, tmp_path):
    """冪等重放不准平白 +1 revision —— 那會撞掉別的 writer 的 `expected_revision` CAS。

    而且「要不要寫」必須在**鎖內**的 snapshot 上判斷:先 `read()` 再決定 `update()` 的話,
    兩次呼叫之間別的 writer 插進來,判斷就過期了。這裡用 revision 當觀測面。
    """
    manifest = _manifest(
        tmp_path,
        [
            _dated_live_ep(tmp_path, 1, "Sat, 16 Aug 2026 21:00:00 +0800"),
            _dated_live_ep(tmp_path, 2, "Sat, 16 Aug 2026 22:00:00 +0800"),
        ],
        "reorder_noop.json",
    )
    from notebooklm_mcp.manifest_store import ManifestStore

    store = ManifestStore(manifest)
    store.update(lambda data: None)                          # revision 1，模擬既有寫入
    before = store.read()["revision"]
    assert _run_reorder(monkeypatch, manifest, "--apply") == 0
    assert store.read()["revision"] == before                # 沒變更 → 一個 byte 都不寫


def _backfill_script():
    import importlib.util

    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "scripts", "backfill_published_at.py",
    )
    spec = importlib.util.spec_from_file_location("backfill_published_at", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _retracted_ep(tmp_path, n, top, first):
    """重生過的集:頂層 pubDate 是 `top`,而首發時間的另一份記錄在撤回快照裡(`first`)。
    `_first_published_at` 讀的是後者,`backfill_published_at.py` 會照它回填。"""
    ep = _dated_live_ep(tmp_path, n, top)
    ep["attempts"] = [{
        "attempt_id": f"att-{n}",
        "retraction": {"retracted_output": {"published_at": first}},
    }]
    return ep


def test_backfill_refuses_a_reorder_breaking_backfill(monkeypatch, tmp_path, capsys):
    """backfill 與 reorder 會互相打架,所以後跑的那支要看得出來。

    真實形狀(podcast-lab 那份 50 集的 EP42/EP43):頂層被 reorder 修成遞增,但 attempt
    首發歷史記著的仍是原本(同樣亂序的)那組值 —— 照著回填就把排序再拆掉,而那要等到
    下次發布才會發現(preflight 擋下**整季**)。拒絕時一個 byte 都不能寫。
    """
    manifest = _manifest(
        tmp_path,
        [
            _retracted_ep(tmp_path, 1, "Fri, 31 Jul 2026 07:41:43 +0800",
                          "Fri, 31 Jul 2026 07:18:43 +0800"),
            _retracted_ep(tmp_path, 2, "Fri, 31 Jul 2026 07:42:32 +0800",
                          "Fri, 31 Jul 2026 07:17:36 +0800"),   # 歷史值比 EP01 的還早
        ],
        "backfill_conflict.json",
    )
    before = open(manifest, encoding="utf-8").read()

    import sys as _sys

    mod = _backfill_script()
    monkeypatch.setattr(_sys, "argv", ["backfill_published_at.py", manifest, "--apply"])
    assert mod.main() == 2
    assert "拒絕回填" in capsys.readouterr().err
    assert open(manifest, encoding="utf-8").read() == before      # 零寫入


def test_backfill_still_repairs_a_season_it_does_not_break(monkeypatch, tmp_path):
    """守衛不能誤殺 backfill 原本的用途:回填後仍然遞增就照回填(EP01 早於 EP02)。"""
    manifest = _manifest(
        tmp_path,
        [
            _retracted_ep(tmp_path, 1, "Fri, 31 Jul 2026 09:00:00 +0800",
                          "Fri, 31 Jul 2026 07:00:00 +0800"),
            _dated_live_ep(tmp_path, 2, "Fri, 31 Jul 2026 08:00:00 +0800"),
        ],
        "backfill_ok.json",
    )
    import sys as _sys

    mod = _backfill_script()
    monkeypatch.setattr(_sys, "argv", ["backfill_published_at.py", manifest, "--apply"])
    assert mod.main() == 0
    eps = {ep["episode"]: ep["published_at"]
           for ep in json.loads(open(manifest, encoding="utf-8").read())["episodes"]}
    assert eps[1] == "Fri, 31 Jul 2026 07:00:00 +0800"            # 回填成首發時間
    assert eps[2] == "Fri, 31 Jul 2026 08:00:00 +0800"


# ---------------------------------------------------------------------------
# mp3 provenance:preflight 比對 promoted attempt 的 finalize.download.sha256
#
# 實測(podcast-lab retracted-slot-rename 2026-08-19):QA 拒收的音檔坐在預設檔名槽,
# 而 preflight 只 resolve「檔案存在」——存在即過,拒收版就能冒充 canonical 上公網。
# 根層 AGENTS 的「認 canonical 一律比 sha」一直是 host 手動紀律;這裡把它下沉成
# fail-closed 閘:有 sha 不符即 raise(任何 PUT 之前)、canonical 歧義即 raise、
# legacy(無 attempts)放行但回傳 sha_unverified_episodes 標註「沒驗」。
# ---------------------------------------------------------------------------

def _attach_attempt(manifest_path, n, *, sha, attempt_id=None, point=True):
    """給第 n 集掛一顆帶 finalize.download.sha256 的 attempt;point=False 不寫
    output_attempt_id(模擬「有 attempts 卻分不出 canonical」的歧義形狀)。"""
    attempt_id = attempt_id or f"att-{n}"
    stored = json.loads(open(manifest_path, encoding="utf-8").read())
    for ep in stored["episodes"]:
        if ep["episode"] == n:
            ep.setdefault("attempts", []).append({
                "attempt_id": attempt_id,
                "finalize": {"download": {"status": "completed", "sha256": sha}},
            })
            if point:
                ep["output_attempt_id"] = attempt_id
    open(manifest_path, "w", encoding="utf-8").write(json.dumps(stored, ensure_ascii=False))


def _sha256_hex(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


async def test_preflight_rejects_mp3_that_is_not_the_promoted_attempts_bytes(
    env, tmp_path, artwork_png, monkeypatch
):
    """冒充檔在任何 PUT 之前被擋下,訊息指名是哪一集。"""
    captured = _install_mock(monkeypatch)
    manifest = _two_episode_manifest(tmp_path)
    _attach_attempt(manifest, 1, sha=_sha256_hex(b"audio-1"))
    _attach_attempt(manifest, 2, sha=_sha256_hex(b"the real canonical bytes"))

    with pytest.raises(ValueError, match="episode 2.*sha256"):
        await _publish(manifest, artwork_png)
    assert captured == []


async def test_preflight_passes_when_mp3_matches_the_promoted_attempt(
    env, tmp_path, artwork_png, monkeypatch
):
    _install_mock(monkeypatch)
    manifest = _two_episode_manifest(tmp_path)
    _attach_attempt(manifest, 1, sha=_sha256_hex(b"audio-1"))
    _attach_attempt(manifest, 2, sha=_sha256_hex(b"audio-2"))

    res = await _publish(manifest, artwork_png)
    assert res["sha_unverified_episodes"] == []


async def test_legacy_episodes_without_attempts_publish_but_are_flagged(
    env, tmp_path, artwork_png, monkeypatch
):
    """legacy 整季 republish 不能因為驗不動就死(deferred 事故的形狀);
    但「沒驗」必須明講,不能讀成「驗過了」——check_episode.py 的同一條紀律。"""
    _install_mock(monkeypatch)
    manifest = _two_episode_manifest(tmp_path)

    res = await _publish(manifest, artwork_png)
    assert res["sha_unverified_episodes"] == [1, 2]


async def test_attempts_without_a_promoted_pointer_are_ambiguous_and_rejected(
    env, tmp_path, artwork_png, monkeypatch
):
    """attempts 非空卻沒有 output_attempt_id = 分不出 canonical——這正是本閘要消滅的
    情境,靜默挑一顆等於把 bug 換個地方藏。"""
    captured = _install_mock(monkeypatch)
    manifest = _two_episode_manifest(tmp_path)
    _attach_attempt(manifest, 1, sha=_sha256_hex(b"audio-1"))
    _attach_attempt(manifest, 2, sha=_sha256_hex(b"audio-2"), point=False)

    with pytest.raises(ValueError, match="episode 2.*output_attempt_id"):
        await _publish(manifest, artwork_png)
    assert captured == []


async def test_promoted_pointer_to_a_missing_attempt_is_rejected(
    env, tmp_path, artwork_png, monkeypatch
):
    """幽靈指標在 ManifestStore 載入驗證就 fail-closed(先於本閘、先於任何 PUT)——
    鎖住這層既有防線;_mp3_provenance_gap 裡的同型 raise 是給非 store 路徑的備援。"""
    captured = _install_mock(monkeypatch)
    manifest = _two_episode_manifest(tmp_path)
    _attach_attempt(manifest, 1, sha=_sha256_hex(b"audio-1"))
    stored = json.loads(open(manifest, encoding="utf-8").read())
    stored["episodes"][1]["output_attempt_id"] = "att-ghost"
    open(manifest, "w", encoding="utf-8").write(json.dumps(stored, ensure_ascii=False))

    with pytest.raises(ValueError, match="output_attempt_id does not reference"):
        await _publish(manifest, artwork_png)
    assert captured == []


async def test_promoted_attempt_without_a_recorded_sha_is_flagged_not_fatal(
    env, tmp_path, artwork_png, monkeypatch
):
    """舊版 finalize 形狀可能沒記 sha256:驗不動 ≠ 不符,放行但標註。"""
    _install_mock(monkeypatch)
    manifest = _two_episode_manifest(tmp_path)
    _attach_attempt(manifest, 1, sha=_sha256_hex(b"audio-1"))
    _attach_attempt(manifest, 2, sha=None)

    res = await _publish(manifest, artwork_png)
    assert res["sha_unverified_episodes"] == [2]
