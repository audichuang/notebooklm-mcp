"""Podcast publishing MCP tool. publish_series renders the whole season in memory
and PUTs each file to the feed host's LAN-only uploader (token-gated). The MCP
host writes nothing durable and needs no NAS mount — it reaches the NAS over the
internal network (PODCAST_UPLOAD_URL = http://<nas-lan-ip>:<port>).

Commit order (the uploader is a dumb landing zone, so the CLIENT enforces order):
media (mp3 + artwork) -> show.json -> feed.xml/index.html. Combined with "the
uploader never deletes", a reader on the public read port never sees a feed.xml
that points at a missing enclosure."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime, parsedate_to_datetime
from urllib.parse import urlsplit

import httpx
from mcp.types import ToolAnnotations

from . import runtime
from ._atomic import download_atomically
from .manifest_store import ManifestStore
from .app import mcp
from .publish import artwork as artwork_mod
from .publish import feed as feed_mod
from .publish import identity
from .publish.layout import attachment_filename, cover_filename, media_filename
from .publish import notes_html
from .publish import state as state_mod

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


def _require_url_env(name: str) -> str:
    """讀 env 且驗證 scheme 是 http(s)。**這不是在防 bearer token 外洩**——httpx 對
    非 http/https scheme 本來就在開 socket 前先丟 `UnsupportedProtocol`,token 從未
    送出去;這裡純粹是把那個原生例外換成講得清楚「哪個環境變數打錯了」的設定錯誤,
    讓打錯字這種最粗糙的失誤在讀 env 當下就 fail-fast,不必等到 `_auth_precheck`
    真的發 GET 才炸。訊息只印 scheme,不印(可能是內網的)URL 本身。"""
    val = _require_env(name)
    scheme = urlsplit(val).scheme
    if scheme not in ("http", "https"):
        raise ValueError(f"{name} must use http(s), got scheme {scheme!r}")
    return val


def _require_media_binaries() -> None:
    missing = [
        binary for binary in ("ffprobe", "ffmpeg")
        if shutil.which(binary) is None
    ]
    if missing:
        raise ValueError(f"required podcast media tool is missing: {', '.join(missing)}")


def _fallback_pub_date(n: int) -> str:
    return format_datetime(_FALLBACK_BASE + timedelta(days=n - 1))


def _effective_pub_date(ep: dict, n: int) -> str:
    """該集真正會投影進 `<pubDate>` 的字串。**preflight 與渲染必須看同一顆值** ——
    各自算一次的話,擋下的與發出去的可以是不同的東西。

    判準是**欄位在不在**,不是 `or` 的 truthiness(與 `publication_state` 同一條紀律):
    `published_at` 填成 `false` / `0` / `[]` 時,`or` 會讓它與「根本沒這個欄位」同形而
    靜默退到 2020 的 fallback —— 那正好在這個欄位該生效的時候失效,而 feed host 永不刪檔。
    缺席是唯一該走 fallback 的情形;填了就必須是能解析的字串。"""
    if "published_at" not in ep:
        return _fallback_pub_date(n)
    raw = ep["published_at"]
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(
            f"episode {n}: published_at 必須是 RFC-2822 時間字串,得到 {raw!r}"
            " —— 要走預設時間就把整個欄位移除,不要填空值/null/非字串"
        )
    return raw


def _parse_pub_date(raw: object, n: int) -> datetime:
    """把 pubDate 字串 parse 成可比較的 datetime;解不出就 fail-loud。

    順手補掉一個既有的洞:`published_at` 現在**原樣**進 `<pubDate>`,壞字串(或非
    字串)會讓播放器解不出時間、自己編一個或整條 item 掉,而 feed host 永不刪檔。
    單調性檢查本來就得 parse 才比得出大小,所以判準放這裡不多花一分錢。"""
    try:
        dt = parsedate_to_datetime(raw)                     # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"episode {n}: published_at 不是可解析的 RFC-2822 時間({raw!r}):{exc}"
        ) from exc
    # **缺 offset 一律拒絕,不就地假設一個。** 補 `+0800` 只修好「比較」這一半:渲染端送出
    # 的仍是原字串,於是 preflight 與播放器看到的是不同的時刻 —— 真的亂序的 feed 會被判成
    # 遞增而放行(播放器多半按 UTC 解讀無 offset 的時間)。RFC-2822 的 `-0000` 語意是
    # 「時區未知」,Python 也 parse 成 naive,同樣拒絕。本 repo 寫入時一律帶 +0800,
    # 所以擋下的只會是手改或跨工具帶進來的資料。
    if dt.tzinfo is None:
        raise ValueError(
            f"episode {n}: published_at 缺時區 offset({raw!r})。RFC-2822 的 -0000"
            "(時區未知)也算 —— 補上真正的 offset(本 repo 一律 +0800)再發布"
        )
    return dt


def _assert_pub_dates_ascend(eps: list[dict]) -> None:
    """pubDate 必須隨集號**嚴格**遞增,不然擋在任何 PUT 之前。

    真實事故(saa-* 12 集):`published_at` 存的是**生成完成時間**,而整季幾乎都是
    亂序生成的 —— EP01 最後才補(21:33,比 EP12 的 21:15 還晚),於是 App 裡 EP01
    變成「最新一集」卡在列表第一個,EP04/EP05 也因生成順序顛倒而互換。
    `itunes:type=serial` 救不了:Apple 文件說 serial 照 `itunes:episode` 排,實測多數
    播放器(含 Apple 自己的列表視圖)仍以 pubDate 為準;而 `build_feed_xml` 的
    `lastBuildDate` 取 `eps[-1]`(集號最大的那集),非單調時它也不是真正最新的時間。

    這條「RSS 排序假設 pubDate 隨集號遞增」的前提一直只寫在
    `tools_podcast._first_published_at` 的註解裡,**沒有任何 preflight 驗過它** ——
    它在「一集一集依序生成」時才自動成立。相等也擋:同一秒的兩集在播放器裡順序不定。

    修法不是放寬這道檢查,而是把時間戳依集號重新配對(不發明新時間):
    `scripts/reorder_published_at.py`。GUID 不變 → Apple 視為同集更新,媒體 URL 不動。
    """
    ordered = sorted(eps, key=lambda ep: int(ep["episode"]))
    bad: list[str] = []
    prev: tuple[int, datetime, str] | None = None
    for ep in ordered:
        n = int(ep["episode"])
        raw = _effective_pub_date(ep, n)
        cur = _parse_pub_date(raw, n)
        if prev is not None and cur <= prev[1]:
            bad.append(f"EP{prev[0]:02d} {prev[2]} >= EP{n:02d} {raw}")
        prev = (n, cur, str(raw))
    if bad:
        raise ValueError(
            "published_at 必須隨集號遞增,否則 Apple 與多數播放器(照 pubDate 排序)會把"
            "集數顯示成亂序;以下相鄰對違反:\n  " + "\n  ".join(bad)
            + "\n修法:把現有時間戳依集號重新配對(不發明新的),再重跑 publish_series"
            " —— GUID 不變,Apple 視為同集更新,媒體 URL 也不動。notebooklm-mcp 的 git clone 裡"
            "有 `uv run python scripts/reorder_published_at.py <manifest>`(dry-run 預設);"
            "只裝了 @latest 的機器沒有這支腳本,照 notebooklm skill 發布前 checklist 的等價寫法做。"
        )


def _make_client() -> httpx.AsyncClient:
    """One seam: tests monkeypatch this to inject an httpx.MockTransport."""
    return httpx.AsyncClient(timeout=_TIMEOUT)


async def _ensure_local_mp3(ep: dict, fallback_notebook_id: str | None, staging_dir: str) -> str:
    """Local path to the episode mp3. If the manifest's mp3_path is gone
    (output_dir cleaned), re-download via artifact_id; else fail-fast.

    重抓的筆記本**優先用該集自己的 `ep["notebook_id"]`**(podcast_episode 的
    manifest stub 會寫入)——滾動 feed 每集獨立筆記本,單一 show 層 notebook_id
    會抓錯本;沒有per-episode 欄位才退到呼叫端傳的 fallback。"""
    path = ep.get("mp3_path")
    if path and os.path.exists(path) and os.path.getsize(path) > 0:
        return path
    artifact_id = ep.get("artifact_id")
    if not artifact_id:
        raise ValueError(
            f"episode {ep.get('episode')}: mp3_path missing and no artifact_id to re-download"
        )
    notebook_id = ep.get("notebook_id") or fallback_notebook_id
    if not notebook_id:
        raise ValueError(
            f"episode {ep.get('episode')}: mp3_path missing and no notebook_id to re-download from "
            "(add per-episode notebook_id to the manifest entry, or pass notebook_id to publish_series)"
        )
    # 既有 mp3_path 就地換檔;沒有時放進呼叫端傳入的 staging_dir(publish_series 用
    # TemporaryDirectory 包住整個 preflight+上傳段,離開自動清除——不留無界累積的目錄)。
    staging = path or os.path.join(staging_dir, f"ep{ep['episode']:02d}.mp3")
    # 取代版的 mp3 一律落在 `attempts/<attempt_id>/`,那層目錄最容易被清掉(把 manifest
    # 搬到另一台發布機也一樣);`download_atomically` 在 dirname(final) 裡 mkstemp,父目錄
    # 不在就直接 FileNotFoundError——而「mp3 不見了就重抓」正是本函式宣稱要處理的情境。
    os.makedirs(os.path.dirname(staging) or ".", exist_ok=True)
    client = runtime.get_client()
    try:
        # 固定檔名的下載一律原子換檔(同 slides/report 的 _finish_slides/_finish_report):
        # temp → 驗非空 → fsync → os.replace。誠實講:實裝 notebooklm-py 0.7.3 的
        # download_url 本來就是 mkstemp → 空檔案就 raise → os.replace,不會把半份檔案
        # 直接寫進我們給的 dest——「partial file 頂替既有完整 mp3」這個情境目前只有
        # 測試用的假 client(故意先寫半份 bytes 再 raise)能重現,不是觀察到的真實
        # SDK 行為。這層外層 guard 防的是「SDK 未來換行為」或「未來換成非 SDK 的寫入
        # 路徑」,belt-and-suspenders,不是在修一個目前存在的真實 bug。
        await download_atomically(
            staging,
            lambda dest: client.artifacts.download_audio(notebook_id, dest, artifact_id),
            lambda _dest: None,   # 內容格式交給後面 _embed_cover 的 ffprobe 驗
        )
    except ValueError as exc:
        # 只攔 _atomic 的「空檔案」語意(download_atomically 唯一會丟的 ValueError 形狀)。
        # 原本 `except ValueError` 連 download_audio 自己的 ValueError(artifact 不存在等)
        # 都改寫成「produced no file」,誤導診斷——那些要原樣往上拋。
        if "downloaded file is empty" not in str(exc):
            raise
        raise ValueError(
            f"episode {ep.get('episode')}: re-download via artifact {artifact_id} produced no file"
        ) from exc
    return staging


def _file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _mp3_provenance_gap(ep: dict, local_path: str) -> str | None:
    """發布前驗「這顆 mp3 真的是 promoted attempt 產出的那顆」。

    回傳 None = 驗過相符;回傳字串 = 驗不動的原因(呼叫端收進
    `sha_unverified_episodes` 標註——「沒驗」必須明講,不能讀成「驗過了」)。
    不符或 canonical 歧義 → raise,擋在任何 PUT 之前。

    存在的理由(podcast-lab retracted-slot-rename 2026-08-19):QA 拒收的音檔坐在
    預設檔名槽,而 `_ensure_local_mp3` 只 resolve「檔案存在」——存在即過,拒收版
    就冒充 canonical 上了公網。「認 canonical 一律比 sha」原本是 host 手動紀律,
    這裡下沉成 fail-closed 閘。legacy manifest(無 attempts)刻意放行:整季
    republish 不能因為驗不動就死(deferred 事故的形狀)。"""
    n = ep.get("episode")
    attempts = ep.get("attempts") or []
    promoted = ep.get("output_attempt_id")
    if not attempts and not promoted:
        return "legacy manifest(無 attempts 記錄,無從比對)"
    if not promoted:
        raise ValueError(
            f"episode {n}: 有 attempts 但沒有 output_attempt_id,分不出哪一顆是 canonical"
            " —— 不靜默挑一顆(那正是本閘要消滅的路徑);用工具把輸出 promote 回"
            " episode 層再發布"
        )
    attempt = next((a for a in attempts if a.get("attempt_id") == promoted), None)
    if attempt is None:
        raise ValueError(
            f"episode {n}: output_attempt_id {promoted} 指向不存在的 attempt"
            " —— manifest 不自洽,不發布"
        )
    expected = ((attempt.get("finalize") or {}).get("download") or {}).get("sha256")
    if not expected:
        return f"promoted attempt {promoted} 沒記 finalize.download.sha256(舊版 finalize 形狀)"
    actual = _file_sha256(local_path)
    if actual != expected:
        raise ValueError(
            f"episode {n}: mp3 檔案的 sha256 與 promoted attempt 不符"
            f"(檔案 {actual[:12]}…,attempt {promoted} 記錄 {expected[:12]}…)"
            " —— 檔案被換過或 mp3_path 指錯位置;重新對帳 manifest,別讓"
            "「檔案存在」冒充 canonical"
        )
    return None


async def _auth_precheck(client, base: str, upload_token: str) -> None:
    """Probe GET /healthz WITH the bearer before sending any big mp3, so a wrong
    token (or a PODCAST_UPLOAD_URL mis-pointed at the read-only Caddy) fails fast
    instead of surfacing as a mid-PUT connection reset. Require BOTH 200 AND the
    uploader's marker header — Caddy's /healthz also returns 200 but lacks it.

    scheme 合法性(http/https)由呼叫端 `_require_url_env` 在讀 env 當下就驗過,這裡
    不重複做;故意不做 host allowlist(過度工程,且合法用途本來就允許任意內網 host)。"""
    r = await client.get(f"{base}/healthz",
                         headers={"Authorization": f"Bearer {upload_token}"})
    if r.status_code != 200 or r.headers.get("X-Podcast-Uploader") != "1":
        raise ValueError(
            f"uploader auth precheck failed (status={r.status_code}, "
            f"marker={r.headers.get('X-Podcast-Uploader')!r}): check PODCAST_UPLOAD_URL "
            "points at the uploader (not the read Caddy) and PODCAST_UPLOAD_TOKEN "
            "matches the NAS .env UPLOAD_TOKEN"
        )


async def _put(client, base: str, token: str, upload_token: str, name: str, data: bytes) -> None:
    url = f"{base}/feeds/{token}/{name}"
    r = await client.put(url, content=data,
                         headers={"Authorization": f"Bearer {upload_token}"})
    if r.status_code != 201:
        raise ValueError(f"upload failed: PUT {name} -> {r.status_code} {r.text[:300]}")


def _embed_cover(mp3_path: str, cover_path: str) -> bytes:
    """把支援的來源正規化成真正 MP3，內嵌 ID3 APIC，回傳決定性 bytes。

    NotebookLM 原始下載常是偽裝成 ``.mp3`` 的 fragmented MP4/AAC；這裡直接轉成
    256 kbps MP3，使實際 container/codec 與 enclosure 的 ``.mp3``/``audio/mpeg``
    契約一致。既有 true MP3 不重編音訊，只重寫封面。其他格式一律 fail-closed。
    這是可被測試 monkeypatch 的 seam(多數 publish 測試用假 audio bytes)。"""
    from mutagen.id3 import APIC, ID3, ID3NoHeaderError

    with open(cover_path, "rb") as f:
        cover = f.read()
    cover_mime = "image/png" if cover[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"

    try:
        probe = json.loads(subprocess.check_output(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "format=format_name:stream=codec_name",
             "-of", "json", mp3_path],
            text=True,
            timeout=30,
        ))
        formats = set(probe["format"]["format_name"].split(","))
        codec = probe["streams"][0]["codec_name"]
    except FileNotFoundError as exc:
        raise ValueError("ffprobe is required to inspect podcast audio") from exc
    except (KeyError, IndexError, json.JSONDecodeError, subprocess.CalledProcessError,
            subprocess.TimeoutExpired) as exc:
        raise ValueError(f"unsupported or unreadable audio: {mp3_path}") from exc

    if formats == {"mp3"} and codec == "mp3":
        source_kind = "mp3"
    elif "mp4" in formats and codec == "aac":
        source_kind = "mp4_aac"
    else:
        raise ValueError(
            f"unsupported audio container/codec for publication: "
            f"format={','.join(sorted(formats)) or 'unknown'} codec={codec or 'unknown'}"
        )

    fd, tmp = tempfile.mkstemp(suffix=".mp3")
    os.close(fd)
    try:
        if source_kind == "mp3":
            shutil.copyfile(mp3_path, tmp)
        else:
            try:
                subprocess.run(
                    ["ffmpeg", "-v", "error", "-y", "-i", mp3_path,
                     "-map", "0:a:0", "-vn", "-map_metadata", "-1",
                     "-c:a", "libmp3lame", "-b:a", "256k", "-ar", "44100", "-ac", "2",
                     tmp],
                    check=True,
                    timeout=600,   # ffprobe(30s)/Chrome(180s)都有 timeout,這是唯一沒有的
                )
            except FileNotFoundError as exc:
                raise ValueError("ffmpeg is required to normalize podcast audio") from exc
            except subprocess.TimeoutExpired as exc:
                raise ValueError(f"ffmpeg normalization timed out after 600s: {mp3_path}") from exc
            except subprocess.CalledProcessError as exc:
                # 與上面 ffprobe 的錯誤契約一致(那邊也是把 subprocess 失敗轉成帶訊息的
                # ValueError):ffmpeg 非 0 退出碼時 raise,別讓原生 CalledProcessError
                # 往外傳。
                raise ValueError(
                    f"ffmpeg normalization failed (returncode={exc.returncode}): {mp3_path}"
                ) from exc

        try:
            tags = ID3(tmp)
        except ID3NoHeaderError:
            tags = ID3()
        # 發布契約是一張 authoritative front cover；移除舊圖避免多 APIC frame 讓
        # client 任選錯張，也讓同音檔＋同封面的輸出 bytes 決定性。
        tags.delall("APIC")
        tags.add(APIC(encoding=3, mime=cover_mime, type=3, desc="Cover", data=cover))
        tags.save(tmp, v2_version=3, padding=lambda _info: 0)
        with open(tmp, "rb") as f:
            return f.read()
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _audio_duration_hms(path: str) -> str | None:
    """Return HH:MM:SS from ffprobe. NotebookLM audio is MP4/DASH with a .mp3
    suffix; mutagen currently reports length=0 for these files."""
    try:
        out = subprocess.check_output(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            text=True,
            timeout=15,
        ).strip()
        seconds = max(0, int(round(float(out))))
    except Exception:
        return None
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


@mcp.tool(annotations=ToolAnnotations(openWorldHint=True))
async def publish_series(
    manifest_path: str,
    show_id: str | None = None,
    show_title: str | None = None,
    show_description: str | None = None,
    author: str | None = None,
    owner_name: str | None = None,
    owner_email: str | None = None,
    artwork_path: str | None = None,
    category: str | None = None,
    explicit: bool | None = None,
    itunes_type: str | None = None,
    notebook_id: str | None = None,
    return_episodes: list[int] | None = None,
    require_slides: bool | None = None,
    require_report: bool | None = None,
) -> dict:
    """把整季 manifest 發布成一條 Apple 合規 RSS feed(一題一 feed)。

    重跑冪等:同 `show_id` → 同 feed URL/token;同 mp3 內容 → 同 enclosure URL。
    ⚠️ **uploader 永不刪檔**:重生只會產生新的不可變 URL,已公開的舊檔收不回來。

    **首發**傳齊 show_id / show_title / show_description / author / owner_name /
    owner_email / artwork_path(缺一 raise),成功後存進 `manifest["show"]`;**之後滾動
    加集只傳 `manifest_path`** 即沿用。顯式參數永遠優先並回寫;`category` / `explicit` /
    `itunes_type` / `require_*` 同樣沿用。

    `return_episodes` **只過濾回傳的 `episodes`,不縮小發布範圍** —— 每次呼叫一律重發
    整季;傳 `[N]` 只是免得回傳隨集數膨脹。

    `itunes_type` 預設 `episodic`(照 pubDate 由新到舊排)。**連載節目不顯式傳 `serial`
    不會報錯,只會在播放器裡靜默倒序。**

    `require_slides` / `require_report` 預設 True:manifest 未回寫該附件路徑就在任何上傳
    前拒發;**只驗路徑已回寫、不等背景生成**(舊路徑 + 新版正在重生 → 照樣放行)。
    使用者明講整季不做某一項時才關掉對應那一個。

    `notebook_id` 只是某集 mp3 不在本機時的重抓 fallback,**每集 manifest 自己的
    `notebook_id` 優先**(每集獨立筆記本時別傳 show 層的,會抓錯本)。

    某集帶 `publication_state: "deferred"` 就**不進 feed**:集號回在 `deferred_episodes`,
    `episode_count` 只算真的發出去的(manifest 50 集、扣下 1 集 → 回 49,不是漏集)。

    完整回傳欄位與 preflight 涵蓋範圍見 skill `references/tool-reference.md`(**參數的正本是這支的 inputSchema**,不必為了湊參數去讀那份)。"""
    base_url = _require_url_env("PODCAST_PUBLIC_BASE_URL")
    # return_episodes 只是回傳過濾器,但舊版拖到所有 PUT + manifest 回寫都完成後才
    # `set(return_episodes)`——傳個 [[1]] 之類的壞型別會在「發布其實已成功」之後才
    # unhashable TypeError,呼叫端誤以為整批失敗。任何遠端副作用前先驗掉。
    if return_episodes is not None and not all(
        isinstance(n, int) and not isinstance(n, bool) for n in return_episodes
    ):
        raise ValueError("return_episodes must be a list of ints")

    salt = _require_env("PODCAST_TOKEN_SALT")
    upload_url = _require_url_env("PODCAST_UPLOAD_URL").rstrip("/")
    upload_token = _require_env("PODCAST_UPLOAD_TOKEN")

    # show 設定解析:顯式參數 > manifest["show"] 既存值;七欄缺一即 fail-fast。
    # (manifest 先讀——show 設定在裡面;episodes preflight 沿用同一份。)
    store = ManifestStore(manifest_path)
    manifest = store.read()
    # 退役快照(graphify `archive/` 那兩份)仍通過 ManifestStore schema、show_id 仍能打到舊
    # feed;位置與 README 擋不住「mp3_path 被修好」的那一天,machine-readable 旗標才擋得住
    # (ADR-0013)。放在讀完 manifest 的第一步:它之後的每一道 preflight 都是在替一份不該
    # 發的東西驗細節。
    state_mod.assert_not_retired(manifest)
    saved_show = manifest.get("show") or {}
    show_cfg = {
        "show_id": show_id or saved_show.get("show_id"),
        "show_title": show_title or saved_show.get("show_title"),
        "show_description": show_description or saved_show.get("show_description"),
        "author": author or saved_show.get("author"),
        "owner_name": owner_name or saved_show.get("owner_name"),
        "owner_email": owner_email or saved_show.get("owner_email"),
        "artwork_path": artwork_path or saved_show.get("artwork_path"),
        # 布林/有預設的兩欄:None 才 fallback,避免 explicit=False 被誤判成「沒傳」。
        "category": category if category is not None else saved_show.get("category", "Technology"),
        "explicit": explicit if explicit is not None else bool(saved_show.get("explicit", False)),
        # 連載 vs 時事,**季級**設定(沿用規則同 show 七欄)。缺這個標籤時 Apple 當
        # episodic:照 pubDate 由新到舊排、`itunes:episode` 基本被忽略,於是連載節目
        # 打開看到的第一集是最後一集。預設沿用 Apple 的隱含值,不替呼叫端改語意。
        "itunes_type": (
            itunes_type if itunes_type is not None
            else saved_show.get("itunes_type", "episodic")
        ),
        # notebook_id(重抓 fallback,選填)也要一起解析:它會進上傳的 show.json,
        # 不解析的話「首發有傳、之後沒傳」會讓 show.json bytes 不穩(null vs 值)。
        "notebook_id": notebook_id or saved_show.get("notebook_id"),
        # 附件政策是**季級**的,所以跟 show 七欄一樣沿用:None = 沿用 manifest、首發預設
        # True。不沿用的話,首發合法地用 require_slides=False 發完之後,照文件只傳
        # manifest_path 做滾動加集會回到 True、掃到缺簡報的舊集直接 raise。
        "require_slides": (
            require_slides if require_slides is not None
            else bool(saved_show.get("require_slides", True))
        ),
        "require_report": (
            require_report if require_report is not None
            else bool(saved_show.get("require_report", True))
        ),
    }
    missing = [k for k in ("show_id", "show_title", "show_description", "author",
                           "owner_name", "owner_email", "artwork_path") if not show_cfg[k]]
    if missing:
        raise ValueError(
            f"missing show fields: {', '.join(missing)} — 首次發布請顯式傳齊"
            "(成功後自動存進 manifest['show'],之後只傳 manifest_path 即沿用)"
        )
    show_id = show_cfg["show_id"]
    show_title = show_cfg["show_title"]
    show_description = show_cfg["show_description"]
    author = show_cfg["author"]
    owner_name = show_cfg["owner_name"]
    owner_email = show_cfg["owner_email"]
    artwork_path = show_cfg["artwork_path"]
    category = show_cfg["category"]
    explicit = show_cfg["explicit"]
    notebook_id = show_cfg["notebook_id"]

    # token = HMAC(salt, show_id) 已經同時內含這兩個分支(identity.make_token 內部會先
    # validate_show_id,故不再另外呼叫)。sha256(token) 存成
    # manifest["show"]["feed_identity_sha256"]——**存雜湊不存 token**,manifest 可能被
    # host 放進 podcast-lab 的版控,token 是 feed URL 的秘密成分。一條比對同時擋「show_id
    # 打錯」與「PODCAST_TOKEN_SALT 漂移」兩個分支:同一份 manifest 換身分沒有警告的話,
    # 整季會無聲落到一條新 feed,既有訂閱者收不到任何更新,而錯誤還會自我延續(下次滾動
    # 加集繼續發去新 feed)。缺席 = legacy manifest,照發並在下面寫回;要刻意換身分,
    # 照 ADR-0013 開新 manifest、舊的標 retired,或用 ManifestStore.update 把這個欄位
    # 移除再發(復活要被看見,不是自動發生)。
    token = identity.make_token(show_id, salt)
    feed_identity_sha256 = hashlib.sha256(token.encode("utf-8")).hexdigest()
    saved_identity = saved_show.get("feed_identity_sha256")
    if saved_identity is not None and saved_identity != feed_identity_sha256:
        raise ValueError(
            f"feed identity changed(manifest {manifest_path}):show_id 或 "
            "PODCAST_TOKEN_SALT 與上一次發布這份 manifest 時不同——照發會讓整季無警告"
            "落到一條新 feed,既有訂閱者收不到任何更新。若是打錯,把 show_id / "
            "PODCAST_TOKEN_SALT 改回原值;若是刻意換身分,照 ADR-0013 開一份新 manifest、"
            "把舊的標 retired,或用 ManifestStore.update 把 "
            "manifest['show']['feed_identity_sha256'] 移除再發(復活要被看見,不是自動"
            "發生)。"
        )
    show_cfg["feed_identity_sha256"] = feed_identity_sha256

    # **值域驗證要在任何遠端副作用之前。** 打錯的 itunes_type 若拖到渲染才擋,前面的
    # artwork/media PUT 已經送出去了(而 feed.xml 是最後一個 PUT),呼叫端會拿到「發布
    # 失敗」但遠端其實留下了一半的檔案。渲染邊界那份是把關,這裡是 fail-fast。
    itunes_type = feed_mod.normalize_itunes_type(show_cfg["itunes_type"])
    show_cfg["itunes_type"] = itunes_type
    # `category=""` 原本能通過 `is not None` 檢查照發:回寫進 manifest 之後,下次
    # `saved_show.get("category", "Technology")` 因為 key 本身存在(值是 ''),不會
    # fallback 回預設值——空字串就此黏住,manifest 修不回去(得手動移除欄位)。只補
    # non-empty,**不做 Apple 分類白名單**(那份清單會漂,維護負債高過它擋得住的錯)。
    if not isinstance(category, str) or not category.strip():
        raise ValueError(
            f"category must be a non-empty string, got: {category!r} —— 空字串/純空白"
            "回寫進 manifest['show']['category'] 之後會黏住(下次沿用時 key 存在,不會"
            "退回預設 'Technology')"
        )
    # Validate artwork up front (fail-fast before any upload). Extension follows
    # the real format so a JPEG is never served as .png. Content-address the filename
    # (artwork-<hash>.jpg) so a CHANGED show cover gets a NEW URL → bypasses the CDN
    # cache: the old fixed "artwork.jpg" name made Cloudflare serve the stale cover up
    # to its TTL (~4h). Same content -> same hash -> byte-stable feed across republish.
    art_info = artwork_mod.validate_artwork(artwork_path)
    with open(artwork_path, "rb") as f:
        art_bytes = f.read()
    art_ext = "jpg" if art_info["format"] == "JPEG" else "png"
    artwork_file = f"artwork-{hashlib.sha256(art_bytes).hexdigest()[:8]}.{art_ext}"

    # token 已在上面算過(feed identity 圍籬與這裡是同一顆值,不重算)。

    # deferred = 稽核上刻意不公開(例如 QA 五次拒收、attempt 全撤回)。必須留在 manifest
    # 當 audit,但不能擋整季重發、也不能被 leftover 本機 mp3 偷偷送上 feed
    # (`_ensure_local_mp3` 缺 mp3_path 時會拿 artifact_id 重抓,那正是被拒收的那顆)。
    # **未知值 fail-loud,不 fall through 成照發**:這個欄位由 host 寫,唯一用途就是
    # 「別公開這一集」,拼錯(`defered`)、空字串、`null` 或新增狀態時靜默發布,正好在它
    # 該生效的時候失效 —— 要加狀態就在 `WITHHELD_PUBLICATION_STATES` 明講。
    # ⚠️ deferred 集**整集跳過 preflight**,不是只跳過 mp3 那一項:它不進 feed,驗它的
    # 檔案沒有意義,而 deferred 也不保證那些檔存在(生產上的 EP46 只缺 mp3、其餘齊全,
    # 但那是巧合 —— 攤在 QA 撤回之後的任何一步都可能是別的形狀)。
    all_eps = manifest.get("episodes", [])
    if not all_eps:
        raise ValueError(f"manifest has no episodes: {manifest_path}")
    manifest_eps: list[dict] = []
    withheld: list[object] = []
    for ep in all_eps:
        # 判準(含未知值 fail-loud)在 `publish/state.is_withheld`:兩支修 `published_at` 的
        # 腳本也要照同一條算「哪些集進 feed」,各寫一份就會漂。
        if state_mod.is_withheld(ep):
            withheld.append(ep.get("episode"))
        else:
            manifest_eps.append(ep)
    if not manifest_eps:
        raise ValueError(
            f"manifest has no publishable episodes (all withheld): {manifest_path}"
        )
    # Preflight the WHOLE manifest before any upload, so bad data fails fast
    # instead of after some media already landed. EP\d{2} on the wire caps a feed
    # at 99 episodes; enforce that + integer + uniqueness + non-empty title here.
    seen_n: set[int] = set()
    for ep in manifest_eps:
        n = ep.get("episode")
        if not isinstance(n, int) or not (1 <= n <= 99):
            raise ValueError(f"episode number must be an int in 1..99, got: {n!r}")
        if n in seen_n:
            raise ValueError(f"duplicate episode number in manifest: {n}")
        seen_n.add(n)
        if not isinstance(ep.get("title"), str) or not ep["title"].strip():
            raise ValueError(f"episode {n}: title is required and must be non-empty")
        # 連載節目的標題必須自己帶集號。`<itunes:episode>` 已經在 feed 裡,但**多數播放器
        # 不顯示它** —— 訂閱者在清單上看到的只有標題,而連載節目的價值就是順序。
        # **擋在這裡而不是靠人記**:同一個 feed 半數有前綴半數沒有,只有訂閱者看得見,
        # host 這邊完全無感。前綴的集號要**等於**該集集號:複製上一集 brief 留下的
        # `EP07. ` 會讓標題與 `<itunes:episode>` 各說各話,那比沒有集號更糟。
        # 射程只到 `serial`:`episodic` 是時事型、由新到舊聽,集號沒有意義(Audicast
        # 五十集都沒有前綴)。
        if itunes_type == "serial" and not re.match(rf"^EP{n:02d}\. ", ep["title"]):
            raise ValueError(
                f"episode {n}: serial 節目的標題必須以 'EP{n:02d}. ' 開頭"
                f"(現在是 {ep['title']!r})—— itunes:episode 多數播放器不顯示,"
                "集號要進標題才是給人看的。title 生成時就鎖死、沒有 episode_set_title,"
                "缺前綴補不回去;整個節目不編號請把 itunes_type 設成 episodic"
            )
        # 單集封面每集必做(程式對齊 skill 政策):缺 cover_path 直接 fail,不再靜默
        # fallback 節目封面——否則漏生封面的集數會「發布成功」卻掛錯圖(EP03 就這樣漏掉)。
        # 這裡就驗(存在 + Apple 規格),讓缺檔/不合規在**任何 PUT 之前**就 fail,不留 orphan media。
        if not ep.get("cover_path"):
            raise ValueError(f"episode {n}: cover_path is required (每集必做,不再 fallback 節目封面)")
        artwork_mod.validate_artwork(ep["cover_path"])
        # 單集 show notes 必做且不可等於標題:缺/等於標題都 fail,不再 fallback 成標題
        # (否則播放器上簡介跟標題一字不差、看起來像壞掉)。
        desc = (ep.get("description") or "").strip()
        if not desc:
            raise ValueError(f"episode {n}: description is required (真 show notes,不可空白)")
        if desc == ep["title"].strip():
            raise ValueError(f"episode {n}: description must not equal title (需真 show notes)")
        # render_episode_notes_html 對 script/外部資源(markdown 圖片、javascript: 連結)
        # fail-closed,那是本機可預判的 deterministic 失敗,不該等到前幾集 PUT 完才爆
        # (與下面 report_md_path 的 render_report_html 預渲染同一理由)。這裡丟棄回傳值
        # ——guard 只掃 notes 本身的 body,attachments 對它無影響,迴圈內仍會帶
        # attachments 重新渲染一次真正要上傳的 HTML。
        # guard 的訊息只描述「壞在哪」,不知道自己是第幾集——45 集的季度光靠內容片段
        # 很難定位(同一支工具的 title/cover/description 檢查都有帶集號)。補上。
        try:
            notes_html.render_episode_notes_html(desc, [])
        except ValueError as exc:
            raise ValueError(f"episode {n}: description {exc}") from exc
        # 附件 requirement + 已填路徑的存在性都在這裡驗完。存在性檢查原本在上傳迴圈
        # 內(舊 :385/:397),後面某集缺檔會讓前面幾集的 mp3/封面已經 PUT 到 NAS,
        # 違反本迴圈上方註解自己宣告的「任何 upload 之前驗完」不變式。
        for key, required, why in (
            ("slides_pdf_path", show_cfg["require_slides"], "簡報"),
            ("report_md_path", show_cfg["require_report"], "研讀講義"),
        ):
            path = ep.get(key)
            if required and not path:
                raise ValueError(
                    f"episode {n}: {key} 未回寫({why}可能還在生成中)。等 generate_slides/"
                    f"generate_report 回寫後再發布;使用者明講整季不做這項才傳 "
                    f"require_{'slides' if key.startswith('slides') else 'report'}=False"
                )
            if path and not (os.path.exists(path) and os.path.getsize(path) > 0):
                raise ValueError(f"episode {n}: {key} missing file: {path}")

    # pubDate 隨集號遞增是整份 feed 的排序前提(`live_episodes` 依集號排、播放器依
    # pubDate 排),而 `published_at` 存的是生成完成時間 —— 亂序生成就會違反它。
    # 擋在這裡,不是渲染時:非單調的 feed 一旦 PUT 上去,訂閱者的 App 就已經拿到亂序了。
    _assert_pub_dates_ascend(manifest_eps)

    # **只驗真的會投影進 feed.xml 的欄位**,而且在第一個 PUT 之前:manifest 內部欄位
    # (brief、錯誤訊息、本機路徑)刻意不管 —— 那些不會進 XML,拿它們擋發布是誤殺。
    # `build_feed_xml` 在它自己的公開邊界會再驗一次(這裡是 preflight,那裡是把關)。
    # show 層欄位一次驗完(不屬於任何一集,沒有集號可報)。
    feed_mod.validate_xml_text((
        base_url, show_title, show_description, author, owner_name, owner_email, category,
    ))
    # 每一集各自驗、訊息包上集號 —— 原本攤平成一個 tuple 的單一呼叫,45 集的季度只知道
    # 「壞在某個地方」,同一支工具其他五處 guard(title/cover/description 等)都帶集號,
    # 這裡漏了。
    for ep in manifest_eps:
        try:
            feed_mod.validate_xml_text((ep["title"], ep["description"], ep.get("published_at")))
        except ValueError as exc:
            raise ValueError(f"episode {ep['episode']}: {exc}") from exc

    # 用單次 run 專屬的 staging 目錄裝重抓的 mp3(見 _ensure_local_mp3):
    # os.replace 換的是目的路徑本身,symlink 攻擊面在最終路徑,不在暫存目錄的檔案
    # mode——固定檔名不是覆寫面。TemporaryDirectory 預設 0700 解的是另一件事:未發布
    # 的音檔以 0644 落在共用 /tmp 會被同機其他本機帳號讀到,0700 只讓這個 process 的
    # owner 進得去。舊版每次缺檔用 mkdtemp() 各留一個目錄,沒人清、無界累積;這裡改成
    # with 包住整段 preflight+上傳,離開(含例外)自動清乾淨。
    with tempfile.TemporaryDirectory(prefix="notebooklm-mp3-") as staging_dir:
        _require_media_binaries()

        # 每集 mp3 在任何 PUT 之前 resolve 成真正存在的本機檔(缺檔就在這裡完成 NotebookLM
        # 重抓)。只驗 artifact_id 不夠:_ensure_local_mp3 還要 notebook_id,而且遠端下載
        # 本身可能失敗——舊版在上傳迴圈內才 resolve,EP05 重抓失敗會讓 EP01–04 的
        # mp3/封面已經落在 NAS 上。dict 只存路徑,不把整季音訊載進 RAM。
        resolved_mp3: dict[int, str] = {}
        for ep in manifest_eps:
            resolved_mp3[int(ep["episode"])] = await _ensure_local_mp3(ep, notebook_id, staging_dir)

        # resolve 完接著驗出身:resolve 只答「有沒有檔」,這裡答「是不是那顆」。
        # 逐集 sha 整季幾 GB 的雜湊屬昂貴同步 I/O,丟 thread(同 _embed_cover 的理由)。
        sha_unverified: list[int] = []
        for ep in manifest_eps:
            n = int(ep["episode"])
            gap = await asyncio.to_thread(_mp3_provenance_gap, ep, resolved_mp3[n])
            if gap:
                sha_unverified.append(n)

        # 講義 HTML 也預先渲染:render_report_html 命中 script/外部資源會 fail-closed,
        # 那是本機可預判的 deterministic 失敗,不該等到前幾集 PUT 完才爆。Markdown 渲染
        # 後通常幾十 KB,整季加總遠小於單一 mp3,不構成 RAM 壓力。
        rendered_reports: dict[int, bytes] = {}
        for ep in manifest_eps:
            rpath = ep.get("report_md_path")
            if rpath:
                with open(rpath, encoding="utf-8") as f:
                    try:
                        rendered = notes_html.render_report_html(f.read(), ep["title"])
                    except ValueError as exc:      # 同上:訊息要指名是哪一集的講義
                        raise ValueError(
                            f"episode {ep['episode']}: report_md_path {rpath} {exc}"
                        ) from exc
                rendered_reports[int(ep["episode"])] = rendered.encode("utf-8")
        # One mp3 in RAM at a time: read -> hash -> PUT -> drop. NEVER accumulate the
        # whole season (8-12 episodes x tens of MB = 300-600MB resident on a possibly
        # small VM). 講義 HTML 是刻意的例外(整季幾百 KB,見上方預渲染)。Commit order
        # still holds: every mp3 + artwork is PUT inside this loop, and
        # show.json/feed.xml are rendered and PUT only AFTER it.
        new_eps: dict[str, dict] = {}
        published = []
        async with _make_client() as client:
            await _auth_precheck(client, upload_url, upload_token)

            for ep in manifest_eps:                                    # 1) media: mp3
                n = int(ep["episode"])
                local = resolved_mp3[n]                                # preflight 已 resolve
                # 正規化成 true MP3 並內嵌 ID3/APIC:Apple/Spotify 常優先吃音檔內嵌圖,
                # 不是 feed 的 <item> itunes:image。正規化/內嵌後 bytes 變 → content-hash/URL
                # 變(預期一次性 churn,uploader 不刪舊 URL)。cover_path 已 preflight。
                # ffmpeg 轉檔屬於昂貴同步 I/O,丟到 thread 別卡住事件迴圈(同 tools_basic.py
                # :159-161 對便宜檔案 I/O 的理由,這裡代價更高:整季逐集轉檔期間其他工具與
                # protocol ping 都會無回應)。用裸名稱查找(不先綁 local 變數),讓測試的
                # autouse monkeypatch seam(`monkeypatch.setattr(tools_publish, "_embed_cover", …)`)
                # 繼續有效——模組全域在呼叫當下才查名字,monkeypatch 換掉的正是這個全域。
                mp3_bytes = await asyncio.to_thread(_embed_cover, local, ep["cover_path"])
                mp3_len = len(mp3_bytes)                                # enclosure length 用內嵌後大小
                hash8 = hashlib.sha256(mp3_bytes).hexdigest()[:8]      # hash 內嵌後 bytes
                mfile = media_filename(n, hash8)
                await _put(client, upload_url, token, upload_token, mfile, mp3_bytes)
                del mp3_bytes

                # 1a) media: 單集封面(每集必做,preflight 已驗存在 + Apple 規格:方形
                #     1400–3000px、RGB、無 alpha)。content-addressed;這裡重讀一次取格式定副檔名。
                cpath = ep["cover_path"]
                c_info = artwork_mod.validate_artwork(cpath)
                with open(cpath, "rb") as f:
                    cover_bytes = f.read()
                c_ext = "jpg" if c_info["format"] == "JPEG" else "png"
                ep_artwork_file = cover_filename(n, hashlib.sha256(cover_bytes).hexdigest()[:8], c_ext)
                await _put(client, upload_url, token, upload_token, ep_artwork_file, cover_bytes)
                del cover_bytes

                # 1b) media: 附件(簡報 PDF / 研讀講義 HTML),content-addressed,公開 URL
                #     append 到單集 description。requirement 與缺檔都已在 preflight 擋掉;
                #     這裡的檢查留作 defensive assertion(不 re-download)。
                base_pub = base_url.rstrip("/")
                desc_base = ep["description"].strip()   # preflight 已保證非空且不等於標題
                attachments: list[tuple[str, str, str]] = []   # (emoji, label, url)
                pdf_url = None      # 回傳給呼叫端,免其事後逆向 content-hash 檔名
                html_url = None

                spath = ep.get("slides_pdf_path")
                if spath:
                    if not (os.path.exists(spath) and os.path.getsize(spath) > 0):
                        raise ValueError(f"episode {n}: slides_pdf_path missing file: {spath}")
                    with open(spath, "rb") as f:
                        pdf_bytes = f.read()
                    pfile = attachment_filename(n, hashlib.sha256(pdf_bytes).hexdigest()[:8], "pdf")
                    pdf_url = f"{base_pub}/feeds/{token}/{pfile}"
                    await _put(client, upload_url, token, upload_token, pfile, pdf_bytes)
                    attachments.append(("📄", "本集簡報 (PDF)", pdf_url))
                    del pdf_bytes

                rpath = ep.get("report_md_path")
                if rpath:
                    if not (os.path.exists(rpath) and os.path.getsize(rpath) > 0):
                        raise ValueError(f"episode {n}: report_md_path missing file: {rpath}")
                    html_bytes = rendered_reports[n]                    # preflight 已渲染 + 驗過
                    hfile = attachment_filename(n, hashlib.sha256(html_bytes).hexdigest()[:8], "html")
                    html_url = f"{base_pub}/feeds/{token}/{hfile}"
                    await _put(client, upload_url, token, upload_token, hfile, html_bytes)
                    del html_bytes                                     # 只放掉區域名稱(正本在 rendered_reports)
                    attachments.append(("📖", "研讀講義", html_url))

                # 純文字 <description>(fallback,含裸 URL)+ 富文字 <content:encoded>
                # (Apple/Overcast/Pocket Casts 優先渲染:條列 + 具名連結,不裸露長 URL)。
                desc = desc_base
                if attachments:
                    desc += "\n\n" + "\n".join(f"{e} {label}:{u}" for e, label, u in attachments)
                desc_html = notes_html.render_episode_notes_html(desc_base, attachments)

                new_eps[str(n)] = {
                    "title": ep["title"],
                    "description": desc,
                    "description_html": desc_html,
                    "guid": identity.episode_guid(show_id, n),
                    "pub_date": _effective_pub_date(ep, n),
                    "media_file": mfile,
                    "length": mp3_len,   # 內嵌封面後的大小(mp3_bytes 已 del)
                }
                # ffprobe 是同步 subprocess,跟上面的 _embed_cover 同理丟到 thread,別讓
                # 整季逐集探測期間卡住事件迴圈。
                duration = await asyncio.to_thread(_audio_duration_hms, local)
                if duration:
                    new_eps[str(n)]["duration"] = duration
                new_eps[str(n)]["artwork_file"] = ep_artwork_file   # 每集必做,一定有單集封面
                published.append({
                    "n": n, "title": ep["title"], "guid": new_eps[str(n)]["guid"],
                    "url": f"{base_pub}/feeds/{token}/{mfile}",
                    "cover_url": f"{base_pub}/feeds/{token}/{ep_artwork_file}",
                    "pdf_url": pdf_url,     # None 若該集無簡報
                    "html_url": html_url,   # None 若該集無講義
                    "duration": duration,   # 對帳用(HH:MM:SS;ffprobe 失敗為 None),免再抓整份 feed
                })

            await _put(client, upload_url, token, upload_token,         # 1) media: artwork
                       artwork_file, art_bytes)                         # bytes 已在 preflight 讀好

            show = {
                "show_id": show_id, "token": token, "notebook_id": notebook_id,
                "title": show_title.strip(), "description": show_description,
                "language": "zh-Hant", "author": author,
                "owner_name": owner_name, "owner_email": owner_email,
                "category": category, "explicit": bool(explicit),
                "itunes_type": itunes_type,
                "artwork_file": artwork_file, "episodes": new_eps,
            }
            show_json = json.dumps(show, ensure_ascii=False, indent=2).encode("utf-8")
            await _put(client, upload_url, token, upload_token, "show.json", show_json)    # 2) state
            await _put(client, upload_url, token, upload_token,                            # 3) derived
                       "feed.xml", feed_mod.build_feed_xml(show, base_url).encode("utf-8"))
            await _put(client, upload_url, token, upload_token,
                       "index.html", feed_mod.build_index_html(show, base_url).encode("utf-8"))

    # 發布成功才在最新 snapshot 上回寫 show；store lock 不跨上方任何 HTTP await。
    persisted_show = {
        **show_cfg,
        "artwork_path": os.path.abspath(show_cfg["artwork_path"]),
    }
    store.update(lambda latest: latest.update({"show": persisted_show}))

    episodes_out = sorted(published, key=lambda e: e["n"])
    if return_episodes is not None:
        # 只縮回傳、不縮發布:feed 仍是整季;episode_count 維持這次發布的全部集數,
        # 別誤讀成「只發了這些」(被扣下的集數另見 `deferred_episodes`)。
        want = set(return_episodes)
        episodes_out = [e for e in episodes_out if e["n"] in want]
    return {
        "feed_url": f"{base_url.rstrip('/')}/feeds/{token}/feed.xml",
        "show_page_url": f"{base_url.rstrip('/')}/feeds/{token}/index.html",
        "token": token,
        "episode_count": len(new_eps),
        # 被 publication_state 扣下的集數。**一定要回報** —— 50 集的 manifest 回
        # episode_count=49 而不說是哪一集不見了,看起來就像發布漏集。
        "deferred_episodes": withheld,
        # provenance 閘驗不動的集數(legacy 無 attempts / 舊 finalize 沒記 sha)。
        # 「沒驗」要明講,不能讀成「驗過了」——check_episode.py 的同一條紀律。
        "sha_unverified_episodes": sorted(sha_unverified),
        "episodes": episodes_out,
    }


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
async def feed_info(show_id: str) -> dict:
    """由 `show_id` 純計算 feed 身分與公開 URL(token = HMAC(salt, show_id)),不打網路。
    **不含各集細節** —— 要看已發布的集就去抓 `feed_url`。"""
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
