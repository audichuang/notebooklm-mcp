"""Composite podcast tools implementing sequential feedback.

Continuity comes from NotebookLM transcribing the prior episode mp3 that we
re-upload as a source. The generation loop is deterministic Python code.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

from . import runtime
from ._status import TerminalGenerationError, ensure_completed, ensure_started
from .auth_probe import probe_auth
from .enums import to_audio_format, to_audio_length
from .languages import resolve_language
from .app import mcp

_TZ = timezone(timedelta(hours=8))


def _episode_label(episode_n: int, title: str) -> str:
    """Unified name for BOTH the Studio artifact and the self-uploaded source:
    ``EP{n:02d} {title}`` (e.g. ``EP01 心法篇``). The EP prefix keeps ordering /
    resume / reject-then-delete addressable; the title makes it human-legible.
    Both sides use this identical string (the unified-naming iron rule)."""
    return f"EP{episode_n:02d} {title.strip()}"


def _load_prior_manifest_episodes(manifest_path: str, notebook_id: str, start: int) -> list[dict]:
    """On resume (start>1), load episodes < start from the existing season manifest
    so resuming does not wipe earlier episodes from the record."""
    if start == 1 or not os.path.exists(manifest_path):
        return []
    # Validate before trusting it: a corrupt/truncated manifest must surface a
    # clear error (not a raw JSONDecodeError leaking the parse position) so the
    # caller knows resume can't safely preserve the earlier episodes.
    try:
        with open(manifest_path, encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Existing series_manifest.json is corrupt and cannot be read for resume: {exc}"
        ) from None
    if not isinstance(data, dict):
        raise ValueError("Existing series_manifest.json is corrupt: top level is not an object")
    if data.get("notebook_id") not in (None, notebook_id):
        raise ValueError("Existing series_manifest.json belongs to a different notebook_id")
    return [
        ep for ep in data.get("episodes", [])
        if isinstance(ep, dict) and isinstance(ep.get("episode"), int) and ep["episode"] < start
    ]


def _write_manifest(manifest_path: str, notebook_id: str, episodes: list[dict]) -> None:
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump({"notebook_id": notebook_id, "episodes": episodes}, f, ensure_ascii=False, indent=2)


def _upsert_manifest_stub(
    manifest_path: str,
    notebook_id: str,
    episode_n: int,
    title: str,
    output_dir: str,
    artifact_id: str,
) -> None:
    """生成一送出就把該集 stub upsert 進 manifest(fill-if-missing,不覆蓋既有欄位)。

    消滅滾動 feed 流程裡「發射前忘了手動補 stub → generate_slides/report 完成回寫時
    raise」的固定漏步:mp3_path 決定性、published_at 取受理當下,寫入點在 generate_audio
    受理之後、生成完成(數分鐘)之前,slides/report 完成回寫時這筆必已在。
    既有欄位一律保留(手動 stub 先寫的 title/published_at 優先);頂層 notebook_id
    不驗不動——滾動 feed 每集獨立筆記本,頂層那欄只是歷史遺留。stub 額外帶
    每集自己的 notebook_id + artifact_id,掉檔重抓/發布 fallback 都認得到對的筆記本。
    同 process 內與 slides/report 的回寫同為事件迴圈內同步讀改寫,無互蓋。
    """
    if os.path.exists(manifest_path):
        with open(manifest_path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or not isinstance(data.get("episodes"), list):
            raise ValueError(
                f"series manifest is corrupt (expect {{notebook_id, episodes:[…]}}): {manifest_path}"
            )
    else:
        data = {"notebook_id": notebook_id, "episodes": []}
    stub = {
        "episode": episode_n,
        "title": title.strip(),
        "label": _episode_label(episode_n, title),
        "mp3_path": os.path.abspath(os.path.join(output_dir, f"ep{episode_n:02d}.mp3")),
        "published_at": format_datetime(datetime.now(_TZ)),
        "notebook_id": notebook_id,
        "artifact_id": artifact_id,
    }
    ep = next(
        (e for e in data["episodes"] if isinstance(e, dict) and e.get("episode") == episode_n),
        None,
    )
    if ep is None:
        data["episodes"].append(stub)
    else:
        for k, v in stub.items():
            ep.setdefault(k, v)
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _validate_episode_args(episode_n: int, title: str, prior_mp3_path: str | None) -> None:
    """單集參數的純本地驗證(不打網路)。壞參數 ValueError 秒退——必須在
    auth 預檢之前跑,認證錯誤不得蓋掉參數錯誤。"""
    # episode_n 必須是 >= 1 的整數(bool 是 int 子類,明確擋掉):否則會生出 EP00/
    # 負集號、還燒掉一次生成 quota。podcast_series 有 start>=1 守衛,單集入口也要有。
    if not isinstance(episode_n, int) or isinstance(episode_n, bool) or episode_n < 1:
        raise ValueError(f"episode_n must be an int >= 1, got: {episode_n!r}")
    if not isinstance(title, str) or not title.strip():
        raise ValueError(f"episode {episode_n} requires a non-empty 'title'")
    if prior_mp3_path and episode_n <= 1:
        raise ValueError("prior_mp3_path requires episode_n >= 2 (there is no prior to episode 1)")


async def _finalize_episode(
    notebook_id: str,
    episode_n: int,
    title: str,
    artifact_id: str,
    output_dir: str,
    wait_timeout: float,
) -> dict:
    """單集後半段(生成之後):等完成 → 命名 → 下載 → 自上傳回錄 → 回傳 manifest 列。

    抽成獨立函式,讓 `podcast_episode_resume` 能拿一個「已在雲端啟動」的 artifact_id
    直接續完,不重生、不燒 quota。artifact_id 就是 generate_audio 的 task_id
    (task_id ≡ artifact id;GenerationStatus 無 artifact_id 欄位)。"""
    client = runtime.get_client()
    os.makedirs(output_dir, exist_ok=True)
    label = _episode_label(episode_n, title)

    final = await client.artifacts.wait_for_completion(notebook_id, artifact_id, timeout=wait_timeout)
    ensure_completed(final)

    # Rename the Studio artifact BEFORE downloading: name it in NotebookLM first so
    # the notebook stays legible regardless of the download outcome, then pull the mp3.
    # fire-and-forget:0.7.3 預設 return_object=True 會再抓全量清單驗證且可能
    # raise not-found;顯式 False 保留 0.4.1 語意(RPC 層錯誤仍會 raise)。
    await client.artifacts.rename(notebook_id, artifact_id, label, return_object=False)

    mp3_path = os.path.join(output_dir, f"ep{episode_n:02d}.mp3")
    await client.artifacts.download_audio(notebook_id, mp3_path, artifact_id)

    # Re-upload THIS episode's own mp3 as a source named IDENTICALLY to its Studio
    # artifact ("EP02" artifact <-> "EP02" source — same string, no suffix). This is
    # the heart of the sequential-feedback method AND keeps a complete record:
    #  - EVERY episode (including the last) ends up in Sources, name-matched to Studio.
    #  - the NEXT episode's generation automatically sees this source for continuity,
    #    so podcast_series needs no separate prior-upload step.
    own_src = await client.sources.add_file(
        notebook_id, mp3_path, mime_type="audio/mpeg", wait=True, wait_timeout=600.0
    )
    await client.sources.rename(notebook_id, own_src.id, label, return_object=False)

    return {
        "episode": episode_n,
        "title": title.strip(),
        "label": label,
        "task_id": artifact_id,
        "artifact_id": artifact_id,
        "mp3_path": mp3_path,
        "published_at": format_datetime(datetime.now(_TZ)),  # 產出時間 → 進 manifest
    }


async def _run_episode(
    notebook_id: str,
    episode_n: int,
    title: str,
    brief: str,
    output_dir: str,
    prior_mp3_path: str | None,
    language: str | None,
    audio_format: str | None,
    audio_length: str | None,
    wait_timeout: float,
    manifest_path: str | None = None,
) -> dict:
    client = runtime.get_client()
    os.makedirs(output_dir, exist_ok=True)

    _validate_episode_args(episode_n, title, prior_mp3_path)

    # Standalone continuity: if the caller hands us a prior episode's mp3 that is
    # NOT yet in the notebook (one-off podcast_episode use), upload + name it so
    # this episode can recap it. In a full podcast_series this is unnecessary —
    # each episode self-uploads at the end (below), so the prior is already there.
    if prior_mp3_path:
        prior_src = await client.sources.add_file(
            notebook_id,
            prior_mp3_path,
            mime_type="audio/mpeg",
            wait=True,
            wait_timeout=600.0,
        )
        await client.sources.rename(
            notebook_id, prior_src.id, f"EP{episode_n - 1:02d}", return_object=False
        )

    status = await client.artifacts.generate_audio(
        notebook_id,
        language=resolve_language(language),
        instructions=brief,
        audio_format=to_audio_format(audio_format),
        audio_length=to_audio_length(audio_length),
    )

    # task_id IS the artifact_id — notebooklm-py _types/artifacts.py:421 states
    # "task_id and artifact_id are the same identifier"; GenerationStatus has NO
    # artifact_id field, so we must use task_id for the download/rename targeting
    # (otherwise download falls back to "latest" and rename targets None).
    # ensure_started guards the failed/empty-task_id case (rate limit / quota / refusal).
    artifact_id = ensure_started(status)

    # 生成一旦送出,artifact 就在 NotebookLM 雲端建立並跑到完成,不靠本地連線活著。
    try:
        # stub 寫在生成受理之後(拿到 artifact_id)、完成之前:放 try 內,壞 manifest 的
        # ValueError 也會被下方 except 附上 resume hint(生成已在雲端跑,不該無聲丟失)。
        if manifest_path:
            _upsert_manifest_stub(
                manifest_path, notebook_id, episode_n, title, output_dir, artifact_id
            )
        return await _finalize_episode(
            notebook_id, episode_n, title, artifact_id, output_dir, wait_timeout
        )
    except TerminalGenerationError:
        # 伺服器端終態(failed / removed,如每日配額耗盡):artifact 已被下架,resume
        # 也救不回——原樣往上拋,不誤導成「可續跑」。用專屬型別而非 except RuntimeError,
        # 才不會把下載/命名步驟意外的 RuntimeError 也當成不可續跑。
        raise
    except Exception as exc:
        # 其餘失敗(本地 wait 超時、下載中斷、網路斷)發生在生成之後,artifact 仍在雲端
        # 完好。就地改寫 exc.args 附上 artifact_id + 現成的 podcast_episode_resume 呼叫,
        # 再原樣 re-raise —— 保留原例外「型別與結構化欄位」(SDK 的 ArtifactTimeoutError
        # 等 constructor 需 notebook_id/task_id/timeout 多個必填參數,type(exc)(str) 會
        # 反而 TypeError 吞掉真錯;改寫 args 對內建與 SDK 例外都能把 hint 帶進 str(exc))。
        # 呼叫端據此續完(不重生、不燒 quota),不必再 artifact_list 撈 id。
        exc.args = (
            f"{exc}\n音檔已在雲端生成(artifact_id={artifact_id!r})但後續步驟失敗。"
            f"用 podcast_episode_resume 續完(不會重新生成):"
            f"podcast_episode_resume(notebook_id={notebook_id!r}, episode_n={episode_n}, "
            f"title={title.strip()!r}, artifact_id={artifact_id!r}, output_dir={output_dir!r})",
        )
        raise


@mcp.tool()
async def podcast_episode(
    notebook_id: str,
    episode_n: int,
    title: str,
    brief: str,
    output_dir: str,
    prior_mp3_path: str | None = None,
    language: str | None = None,
    audio_format: str | None = "deep-dive",
    audio_length: str | None = "long",
    wait_timeout: float = 1200.0,
    manifest_path: str | None = None,
) -> dict:
    """Generate one podcast episode end-to-end.

    The Studio artifact and self-uploaded source are both named ``EP{n:02d} {title}``
    (e.g. ``EP02 實戰篇``), so pass the outline's episode title.

    傳 ``manifest_path``(如 ``output/series_manifest.json``)= 生成一受理就把該集
    stub(episode/title/label/mp3_path/published_at/notebook_id/artifact_id)upsert
    進 manifest——滾動 feed 免手動補 stub,generate_slides/report 的回寫也保證找得到
    這筆。既有欄位不覆蓋;不傳維持舊行為(只 return 不寫)。
    """
    # 本地驗證先行(壞參數 ValueError 秒退,不浪費 RPC),再做認證預檢:
    # 單集也要等最多 20 分鐘,cookie 死了先秒退(見 auth_probe docstring)。
    _validate_episode_args(episode_n, title, prior_mp3_path)
    await probe_auth(runtime.get_client())
    return await _run_episode(
        notebook_id,
        episode_n,
        title,
        brief,
        output_dir,
        prior_mp3_path,
        language,
        audio_format,
        audio_length,
        wait_timeout,
        manifest_path=manifest_path,
    )


@mcp.tool()
async def podcast_episode_resume(
    notebook_id: str,
    episode_n: int,
    title: str,
    artifact_id: str,
    output_dir: str,
    wait_timeout: float = 1200.0,
    manifest_path: str | None = None,
) -> dict:
    """接續一個「已在 NotebookLM 雲端啟動」的音檔生成,續完後半段而**不重新生成**。

    用途:``podcast_episode`` 在「生成送出後、下載/命名/回錄前」因 MCP 呼叫超時或
    連線中斷而斷掉時,音檔仍在雲端跑完 —— 拿那次的 ``artifact_id`` 呼叫這個工具,
    等它完成→命名→下載→自上傳回錄,不燒 quota、不多生一個 artifact。

    ``artifact_id`` 從哪來:斷掉那次若有回錯誤訊息,裡面已附上;否則用
    ``artifact_list(notebook_id, kind="audio")`` 依 ``created_at`` 找出最新那個。
    ``title`` 傳大綱裡該集的標題(決定命名鐵律 ``EP{n:02d} {title}``)。

    注意:同一 ``artifact_id`` **只續一次**。重複續(或斷線後原呼叫其實已完成、又再續
    一次)會重下載、並多上傳一筆同名 ``EP{n:02d} 標題`` 來源。發現重覆時用 ``source_list``
    找出多餘那筆、``source_delete`` 刪掉即可(reject-then-delete 同一套)。
    """
    # ponytail: 去重靠「一集只續一次 + 事後 source_delete」,不加行內鎖——真正的重覆風險
    # 是斷線後換 stdio process 再續(跨 process),行內 asyncio.Lock 擋不到、只給假安全感。
    # 本地驗證先行(壞參數 ValueError 秒退),再認證預檢——與 podcast_episode 同一
    # fail-fast 順序:認證錯誤不得蓋掉參數錯誤。
    _validate_episode_args(episode_n, title, None)
    if not isinstance(artifact_id, str) or not artifact_id.strip():
        raise ValueError("artifact_id 必填(從斷掉那次的錯誤訊息或 artifact_list 取得)")
    await probe_auth(runtime.get_client())
    # 原呼叫若死在 stub 寫入前,manifest 會缺這集——resume 傳 manifest_path 一併補上
    # (upsert fill-if-missing,已存在則不動)。
    if manifest_path:
        _upsert_manifest_stub(
            manifest_path, notebook_id, episode_n, title, output_dir, artifact_id.strip()
        )
    return await _finalize_episode(
        notebook_id, episode_n, title, artifact_id.strip(), output_dir, wait_timeout
    )


@mcp.tool()
async def podcast_series(
    notebook_id: str,
    episodes: list[dict],
    output_dir: str,
    start: int = 1,
    language: str | None = None,
    audio_format: str | None = "deep-dive",
    audio_length: str | None = "long",
    wait_timeout: float = 1200.0,
) -> dict:
    """Generate a full podcast series deterministically."""
    # Validate shape up front so a malformed episodes list fails with a clear
    # message instead of a raw KeyError/TypeError mid-loop (after burning a
    # generation). Each episode must be a dict carrying a 'brief' AND a non-empty
    # 'title' (the title names the Studio artifact + source as "EP{n:02d} {title}").
    for i, ep in enumerate(episodes, start=1):
        if not isinstance(ep, dict) or "brief" not in ep:
            raise ValueError(
                f"episode {i} must be a dict with a 'brief' key, got: {ep!r}"
            )
        title = ep.get("title")
        if not isinstance(title, str) or not title.strip():
            raise ValueError(
                f"episode {i} must have a non-empty 'title' (got: {title!r})"
            )

    # Validate the resume cursor so start=0 (would index episodes[-1]) or an
    # out-of-range start fail clearly instead of silently producing wrong output.
    if start < 1:
        raise ValueError("start must be >= 1")
    if start > len(episodes):
        raise ValueError(f"start must be <= len(episodes) ({len(episodes)})")

    os.makedirs(output_dir, exist_ok=True)
    manifest_path = os.path.join(output_dir, "series_manifest.json")

    run_results: list[dict] = []  # episodes generated THIS call (the return value)
    # Season manifest preserves earlier episodes across a resume.
    manifest_results = _load_prior_manifest_episodes(manifest_path, notebook_id, start)

    # 所有本地驗證(episodes 形狀、start 邊界、manifest 載入)通過後才做認證
    # 預檢:整季動輒數小時,cookie 死了要在燒任何生成之前秒退。只在季開頭驗
    # 一次;Doppler inline auth 不做 RotateCookies,避免各 stdio process 輪替出
    # Doppler 寫不回的新 cookie。
    await probe_auth(runtime.get_client())

    # No prior-mp3 threading: each episode self-uploads its mp3 as a named source
    # at the end of _run_episode, so the next episode (and a `start`-based resume
    # on the same notebook) automatically sees prior episodes already present.
    for episode_n in range(start, len(episodes) + 1):
        ep = episodes[episode_n - 1]
        res = await _run_episode(
            notebook_id,
            episode_n,
            ep["title"],
            ep["brief"],
            output_dir,
            None,
            language,
            audio_format,
            audio_length,
            wait_timeout,
        )
        run_results.append(res)
        manifest_results.append(res)
        _write_manifest(manifest_path, notebook_id, manifest_results)

    return {"notebook_id": notebook_id, "episodes": run_results, "manifest": manifest_path}
