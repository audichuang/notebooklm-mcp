"""按需生成單集附加產物(簡報 slide deck / 研讀 report),路徑回寫 series_manifest.json。
不碰音檔迴圈;想幫哪集加就對哪集跑。產物只餵傳入的 source_ids(不傳則 SDK 用全部來源)。"""
from __future__ import annotations

import os

from mcp.types import ToolAnnotations

from . import runtime
from ._atomic import download_atomically
from ._sources import assert_sources_exist, to_source_ids
from ._status import ensure_completed, ensure_started
from .enums import to_report_format, to_slide_format, to_slide_length
from .languages import resolve_language
from .app import mcp
from ._text import _CITATION_RE, strip_inline_emphasis
from .manifest_store import ManifestStore
from .publish import notes_html


def _load_ep_and_write(manifest_path: str, episode_n: int, **fields) -> dict:
    """在 locked fresh snapshot 上合併單集欄位，避免其他 writer 的更新被覆蓋。"""
    def mutate(data):
        ep = next(
            (
                e for e in data.get("episodes", [])
                if isinstance(e, dict) and e.get("episode") == episode_n
            ),
            None,
        )
        if ep is None:
            raise ValueError(f"episode {episode_n} not found in manifest {manifest_path}")
        if "description" in fields:
            title = (ep.get("title") or "").strip()
            if title and fields["description"] == title:
                raise ValueError("description must not equal title(需真 show notes,publish 會擋)")
        ep.update(fields)
        return dict(ep)

    _, episode = ManifestStore(manifest_path).update(mutate)
    return episode


@mcp.tool()
async def episode_set_description(
    manifest_path: str,
    episode_n: int,
    description: str,
    strip_citations: bool = True,
) -> dict:
    """把單集 show notes 寫進 manifest 的 description(預設先清引用標記 [n])。

    取代「host 開 bash 改 JSON」那步:本工具與 generate_slides/generate_report 的
    回寫同在 server process 事件迴圈內同步讀改寫,天然不 interleave——chat_ask 產完
    show notes 即可回寫,不用等三個生成到齊。注意這不是跨 process 檔案鎖,別再用
    外部腳本同時改同一份 manifest。並前置驗 publish 的**全部** preflight 條件(非空、
    不等於標題、渲染後自包含),讓錯誤在寫入當下就爆,不留到發布才 fail。"""
    desc = description.strip()
    if strip_citations:
        # 引用標記 + inline 星號強調一起清:v0.9.14 驗收實測,含 `**粗體**` 的字串
        # 原樣寫進 manifest 再原樣進公開 RSS。清理放這裡(不是只放 chat_ask)是因為
        # 呼叫端也可能自己組 show notes,而這支是進 manifest 的唯一正門。
        desc = strip_inline_emphasis(_CITATION_RE.sub("", desc)).strip()
    if not desc:
        raise ValueError("description is empty(清完引用標記後也不可為空)")
    # 跑 publish 端那顆一模一樣的 guard。少了這道,夾帶 markdown 圖片／javascript: 連結的
    # show notes 會安穩寫進 manifest,直到整季生完、跑 publish_series 才 fail-closed——
    # docstring 自己宣告「錯誤在寫入當下就爆」,那就要真的做到(chat_ask 產的 notes 來自
    # 可被 prompt injection 的外部文章,這不是理論風險)。
    notes_html.render_episode_notes_html(desc, [])
    _load_ep_and_write(manifest_path, episode_n, description=desc)
    return {"episode": episode_n, "description": desc, "stripped": strip_citations}


def _validate_pdf(path: str) -> None:
    """簡報必須真的是 PDF。torn write 的 partial 檔常常前幾 byte 還在、尾巴沒了,
    但「非空」檢查會放行——magic 只是最便宜的一層,擋掉完全不成形的那種。"""
    with open(path, "rb") as handle:
        magic = handle.read(5)
    if magic != b"%PDF-":
        raise ValueError(f"downloaded slides are not a PDF (magic={magic!r}): {path}")


def _validate_utf8_text(path: str) -> None:
    """講義是 Markdown;截在多位元組字元中間的半份檔會在這裡被抓到。"""
    try:
        with open(path, encoding="utf-8") as handle:
            handle.read()
    except UnicodeDecodeError as exc:
        raise ValueError(f"downloaded report is not valid UTF-8 text: {path}") from exc


def _require_artifact_id(artifact_id: str) -> str:
    if not isinstance(artifact_id, str) or not artifact_id.strip():
        raise ValueError("artifact_id must be a non-empty string")
    return artifact_id.strip()


def _require_episode(manifest_path: str, episode_n: int) -> None:
    """在**第一個遠端副作用之前**確認該集存在於 manifest。

    原本這個檢查只在 `_load_ep_and_write` —— 也就是生成/改版跑完、下載完之後才驗:
    `episode_n` 打錯就是燒完一次配額才 raise,而遠端那份 artifact 已經產生了。
    這裡不驗 artifact ↔ episode 的 binding(manifest 目前沒存 `slides_artifact_id`),
    只擋掉打錯集號這種可預判的錯。"""
    data = ManifestStore(manifest_path).read()
    if not any(
        isinstance(e, dict) and e.get("episode") == episode_n
        for e in data.get("episodes", [])
    ):
        raise ValueError(f"episode {episode_n} not found in manifest {manifest_path}")


async def _require_completed_slide_deck(client: object, notebook_id: str, artifact_id: str):
    """遠端 mutation 前驗 artifact:存在、屬於這個 notebook、是簡報、已完成。

    `get_or_none` 是「list 一次再比對 id」,所以一次呼叫同時回答「存不存在」與
    「屬不屬於這個 notebook」——`revise_slide` 的 RPC 只靠 artifact_id 定位,
    notebook_id 只是 routing header,錯配的 ID 不會被伺服器擋下來。
    (`get()` 在 0.8.0 會改成 raise,故用 sanctioned 的 `get_or_none`。)"""
    art = await client.artifacts.get_or_none(notebook_id, artifact_id)  # type: ignore[attr-defined]
    if art is None:
        raise ValueError(
            f"artifact {artifact_id} 不在 notebook {notebook_id}"
            "(用 artifact_list 確認 ID 與筆記本)"
        )
    kind = getattr(art.kind, "value", str(art.kind))
    if kind != "slide_deck":
        raise ValueError(
            f"artifact {artifact_id} 的 kind 是 {kind!r},不是 slide_deck —— "
            "artifact_revise_slide 只能改簡報"
        )
    if not art.is_completed:
        raise ValueError(
            f"artifact {artifact_id} 尚未完成(status={art.status_str!r}),不能改版;"
            "先 artifact_wait 等它完成"
        )
    return art


async def _finish_slides(
    client: object,
    notebook_id: str,
    manifest_path: str,
    episode_n: int,
    artifact_id: str,
    wait_timeout: float,
) -> dict:
    """生成之後的共用尾段:等完成→下載→回寫 manifest。

    抽出來是為了讓「已經生好、但 client 端斷線／timeout 丟掉結果」的 artifact 能只走
    這段救回來(`artifact_download_slides`),而不必重生一次燒配額。"""
    # client 是 public tool 入口固定下來的同一個帳號，不回頭讀全域 active slot。
    final = await client.artifacts.wait_for_completion(notebook_id, artifact_id, timeout=wait_timeout)
    ensure_completed(final)

    out = os.path.join(os.path.dirname(os.path.abspath(manifest_path)), f"ep{episode_n:02d}-slides.pdf")
    # 固定檔名 → 重生就是就地覆寫。原子換檔,失敗時舊那份完整簡報原封不動。
    await download_atomically(
        out,
        lambda dest: client.artifacts.download_slide_deck(
            notebook_id, dest, artifact_id=artifact_id, output_format="pdf"
        ),
        _validate_pdf,
    )
    _load_ep_and_write(manifest_path, episode_n, slides_pdf_path=out)
    return {"episode": episode_n, "slides_pdf_path": out, "artifact_id": artifact_id}


@mcp.tool(annotations=ToolAnnotations(openWorldHint=True))
async def generate_slides(
    notebook_id: str,
    manifest_path: str,
    episode_n: int,
    source_ids: list[str] | None = None,
    language: str | None = None,
    instructions: str | None = None,
    slide_format: str | None = "detailed",
    slide_length: str | None = "default",
    wait_timeout: float = 1800.0,
) -> dict:
    """生成該集簡報並下載 PDF,路徑回寫 manifest 的 slides_pdf_path。"""
    selected = to_source_ids(source_ids)
    _require_episode(manifest_path, episode_n)      # 打錯集號別燒一次生成配額
    client = runtime.get_client()
    if selected is not None:
        # 打錯/已刪的 source_id 伺服器不擋——燒完一次生成配額才發現拿到聚焦錯誤的簡報。
        await assert_sources_exist(client, notebook_id, selected)
    status = await client.artifacts.generate_slide_deck(
        notebook_id,
        source_ids=selected,
        language=resolve_language(language),
        instructions=instructions,
        slide_format=to_slide_format(slide_format),
        slide_length=to_slide_length(slide_length),
    )
    artifact_id = ensure_started(status)
    return await _finish_slides(
        client, notebook_id, manifest_path, episode_n, artifact_id, wait_timeout
    )


@mcp.tool()
async def artifact_download_slides(
    notebook_id: str,
    manifest_path: str,
    episode_n: int,
    artifact_id: str,
    wait_timeout: float = 1800.0,
) -> dict:
    """把**已經生成**的簡報用 artifact_id 下載並回寫 manifest,不重新生成。

    救援用:client timeout 砍掉 `generate_slides` 時雲端那份其實生完了,用
    `artifact_list(kind="slide_deck")` 找回 ID 就能省一次配額。不確定是哪一筆別猜
    ——重生比綁錯便宜。"""
    client = runtime.get_client()
    return await _finish_slides(
        client,
        notebook_id,
        manifest_path,
        episode_n,
        _require_artifact_id(artifact_id),
        wait_timeout,
    )


@mcp.tool()
async def artifact_revise_slide(
    notebook_id: str,
    manifest_path: str,
    episode_n: int,
    artifact_id: str,
    slide_index: int,
    prompt: str,
    wait_timeout: float = 1800.0,
) -> dict:
    """改**已生成簡報中的單一頁**(0-based `slide_index`),再重新下載回寫 manifest。

    省配額用:一頁改一句話不必整份重生(那還會連帶改動其他頁)。結束後走與生成相同的
    尾段(等完成→原子換檔下載→回寫 `slides_pdf_path`)。
    `artifact_id` 用 `artifact_list(kind="slide_deck")` 找。

    ⚠️ **這不是就地修改:遠端會多出一顆新 artifact,舊的留著。** v0.9.0 真實驗收實測
    (本 docstring 原本寫「artifact 不變」,是錯的):revise 之後 `artifact_list` 多一顆
    `<原標題> (2)`,原本那顆**原封不動還在**。回傳的 `artifact_id` 是**新的那顆**
    (manifest 也回寫成它),`superseded_artifact_id` 是被取代的舊那顆。

    所以連續 revise 會讓遠端堆出 `(2)`、`(3)`… 而它們**標題只差一個序號**——正好放大
    「artifact_list 分不出這是哪一集的 deck」那個既有風險。要清乾淨的話,拿
    `superseded_artifact_id` 自己決定要不要刪(本工具刻意不自動刪:那是遠端破壞性動作,
    而且萬一新的那顆有問題,舊的是唯一的退路)。"""
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must be a non-empty string(空 prompt 等於白改一次)")
    artifact_id = _require_artifact_id(artifact_id)
    client = runtime.get_client()
    # 所有可預判的錯都擋在第一個遠端副作用之前。**注意這不是 durable attempt**:
    # 外層 client 在 revise 成功之後、下載回寫之前斷線,重跑仍會再 revise 一次
    # ——那是整個 slides/report 家族共有的架構債(generate_slides 逐字同形),
    # 要修得連同 generate 一起做成 attachment attempt,不在本工具的範圍。
    _require_episode(manifest_path, episode_n)
    await _require_completed_slide_deck(client, notebook_id, artifact_id)
    status = await client.artifacts.revise_slide(
        notebook_id, artifact_id, slide_index, prompt.strip()
    )
    # **以回傳值為準,不假設它是同一顆** —— 這個「不自己假設」的寫法救了這支工具:
    # v0.9.0 真實驗收證明 REVISE_SLIDE **不是**就地改版,伺服器會 fork 出一顆新的
    # `<原標題> (2)`,舊的留著。當初若照 docstring 寫死用輸入的 artifact_id,下載到的
    # 會是**沒改過的舊那份**,而且看起來完全成功。
    revised_id = ensure_started(status)
    out = await _finish_slides(
        client, notebook_id, manifest_path, episode_n, revised_id, wait_timeout
    )
    out["slide_index"] = slide_index
    # 讓呼叫端看得出 id 換了、舊的還在遠端 —— 否則它只會拿到一個「artifact_id 跟我傳的
    # 不一樣」的回傳值,無從判斷是 fork 還是自己記錯。
    if revised_id != artifact_id:
        out["superseded_artifact_id"] = artifact_id
    return out


async def _finish_report(
    client: object,
    notebook_id: str,
    manifest_path: str,
    episode_n: int,
    artifact_id: str,
    report_format: str,
    wait_timeout: float,
) -> dict:
    """生成之後的共用尾段(同 `_finish_slides` 的理由)。"""
    # client 是 public tool 入口固定下來的同一個帳號，不回頭讀全域 active slot。
    final = await client.artifacts.wait_for_completion(notebook_id, artifact_id, timeout=wait_timeout)
    ensure_completed(final)

    out = os.path.join(os.path.dirname(os.path.abspath(manifest_path)), f"ep{episode_n:02d}-report.md")
    # 同 _finish_slides:固定檔名的就地覆寫換成原子換檔。
    await download_atomically(
        out,
        lambda dest: client.artifacts.download_report(notebook_id, dest, artifact_id=artifact_id),
        _validate_utf8_text,
    )
    _load_ep_and_write(manifest_path, episode_n, report_md_path=out, report_format=report_format)
    return {"episode": episode_n, "report_md_path": out, "report_format": report_format, "artifact_id": artifact_id}


def _validate_report_prompt(
    report_format: str, custom_prompt: str | None, extra_instructions: str | None
) -> str | None:
    """`custom` 與 `custom_prompt` 必須成對,且 custom 不吃 extra_instructions。

    三種誤用 SDK 全都**靜默吞掉**,要燒完一次配額、拿到一份不是你要的講義才會發現:
      - `custom` 無 prompt → `_report_config` 套 "Create a report based on the
        provided sources."(payloads.py:538)
      - 靜態格式帶 prompt → `_report_config` 走 `_STATIC_REPORT_CONFIGS`,prompt 丟掉
      - `custom` 帶 extra_instructions → `payloads.py:219` 明確跳過串接
    """
    prompt = custom_prompt.strip() if isinstance(custom_prompt, str) else None
    if report_format == "custom":
        if not prompt:
            raise ValueError(
                "report_format='custom' 需要非空的 custom_prompt"
                "(否則 SDK 靜默套用通用預設句,白燒一次配額)"
            )
        if extra_instructions:
            raise ValueError(
                "report_format='custom' 不吃 extra_instructions(SDK 不串接,會靜默消失);"
                "把要求併進 custom_prompt"
            )
    elif prompt:
        raise ValueError(
            f"custom_prompt 只在 report_format='custom' 有效"
            f"(現在是 {report_format!r},SDK 會套靜態模板並丟掉 prompt)"
        )
    return prompt


@mcp.tool(annotations=ToolAnnotations(openWorldHint=True))
async def generate_report(
    notebook_id: str,
    manifest_path: str,
    episode_n: int,
    report_format: str = "study_guide",
    source_ids: list[str] | None = None,
    language: str | None = None,
    extra_instructions: str | None = None,
    custom_prompt: str | None = None,
    wait_timeout: float = 1800.0,
) -> dict:
    """生成該集研讀文件(預設 study_guide)並下載 Markdown,路徑回寫 report_md_path。

    `report_format="custom"` + `custom_prompt` = 完全自訂講義結構(三種靜態模板
    study_guide / briefing_doc / blog_post 之外的形狀)。兩者必須成對,且 custom
    格式不吃 `extra_instructions`——要求併進 `custom_prompt`。"""
    custom_prompt = _validate_report_prompt(report_format, custom_prompt, extra_instructions)
    selected = to_source_ids(source_ids)
    _require_episode(manifest_path, episode_n)      # 同上
    client = runtime.get_client()
    if selected is not None:
        # 同 generate_slides:打錯/已刪的 source_id 伺服器不擋,先唯讀對帳。
        await assert_sources_exist(client, notebook_id, selected)
    status = await client.artifacts.generate_report(
        notebook_id,
        report_format=to_report_format(report_format),
        source_ids=selected,
        language=resolve_language(language),
        custom_prompt=custom_prompt,
        extra_instructions=extra_instructions,
    )
    artifact_id = ensure_started(status)
    return await _finish_report(
        client,
        notebook_id,
        manifest_path,
        episode_n,
        artifact_id,
        report_format,
        wait_timeout,
    )


@mcp.tool()
async def artifact_download_report(
    notebook_id: str,
    manifest_path: str,
    episode_n: int,
    artifact_id: str,
    report_format: str = "study_guide",
    wait_timeout: float = 1800.0,
) -> dict:
    """把**已經生成**的講義用 artifact_id 下載並回寫 manifest,不重新生成。

    救援用,同 `artifact_download_slides`(client timeout 丟掉結果時省一次配額)。
    `report_format` 只影響回寫 manifest 的標記,傳當初生成用的那個值。"""
    client = runtime.get_client()
    return await _finish_report(
        client,
        notebook_id,
        manifest_path,
        episode_n,
        _require_artifact_id(artifact_id),
        report_format,
        wait_timeout,
    )
