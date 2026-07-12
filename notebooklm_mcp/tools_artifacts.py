"""按需生成單集附加產物(簡報 slide deck / 研讀 report),路徑回寫 series_manifest.json。
不碰音檔迴圈;想幫哪集加就對哪集跑。產物只餵傳入的 source_ids(不傳則 SDK 用全部來源)。"""
from __future__ import annotations

import json
import os

from . import runtime
from ._status import ensure_completed, ensure_started
from .enums import to_report_format, to_slide_format, to_slide_length
from .languages import resolve_language
from .app import mcp
from ._text import _CITATION_RE


def _load_ep_and_write(manifest_path: str, episode_n: int, **fields) -> dict:
    """讀 manifest、找 episode==episode_n 的集、merge 欄位、寫回(沿用直接 json.dump)。"""
    with open(manifest_path, encoding="utf-8") as f:
        data = json.load(f)
    ep = next(
        (e for e in data.get("episodes", []) if isinstance(e, dict) and e.get("episode") == episode_n),
        None,
    )
    if ep is None:
        raise ValueError(f"episode {episode_n} not found in manifest {manifest_path}")
    ep.update(fields)
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return ep


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
    外部腳本同時改同一份 manifest。並前置驗 publish 的 preflight 條件(非空、
    不等於標題),讓錯誤在寫入當下就爆,不留到發布才 fail。"""
    desc = description.strip()
    if strip_citations:
        desc = _CITATION_RE.sub("", desc).strip()
    if not desc:
        raise ValueError("description is empty(清完引用標記後也不可為空)")
    # 先讀該集驗 title,再走同一個讀改寫 helper;全程同步、中間無 await,不互蓋。
    with open(manifest_path, encoding="utf-8") as f:
        data = json.load(f)
    ep = next(
        (e for e in data.get("episodes", []) if isinstance(e, dict) and e.get("episode") == episode_n),
        None,
    )
    if ep is None:
        raise ValueError(f"episode {episode_n} not found in manifest {manifest_path}")
    title = (ep.get("title") or "").strip()
    if title and desc == title:
        raise ValueError("description must not equal title(需真 show notes,publish 會擋)")
    _load_ep_and_write(manifest_path, episode_n, description=desc)
    return {"episode": episode_n, "description": desc, "stripped": strip_citations}


@mcp.tool()
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
    client = runtime.get_client()
    status = await client.artifacts.generate_slide_deck(
        notebook_id,
        source_ids=source_ids,
        language=resolve_language(language),
        instructions=instructions,
        slide_format=to_slide_format(slide_format),
        slide_length=to_slide_length(slide_length),
    )
    artifact_id = ensure_started(status)
    final = await client.artifacts.wait_for_completion(notebook_id, artifact_id, timeout=wait_timeout)
    ensure_completed(final)

    out = os.path.join(os.path.dirname(os.path.abspath(manifest_path)), f"ep{episode_n:02d}-slides.pdf")
    await client.artifacts.download_slide_deck(notebook_id, out, artifact_id=artifact_id, output_format="pdf")
    _load_ep_and_write(manifest_path, episode_n, slides_pdf_path=out)
    return {"episode": episode_n, "slides_pdf_path": out, "artifact_id": artifact_id}


@mcp.tool()
async def generate_report(
    notebook_id: str,
    manifest_path: str,
    episode_n: int,
    report_format: str = "study_guide",
    source_ids: list[str] | None = None,
    language: str | None = None,
    extra_instructions: str | None = None,
    wait_timeout: float = 1800.0,
) -> dict:
    """生成該集研讀文件(預設 study_guide)並下載 Markdown,路徑回寫 report_md_path。"""
    client = runtime.get_client()
    status = await client.artifacts.generate_report(
        notebook_id,
        report_format=to_report_format(report_format),
        source_ids=source_ids,
        language=resolve_language(language),
        extra_instructions=extra_instructions,
    )
    artifact_id = ensure_started(status)
    final = await client.artifacts.wait_for_completion(notebook_id, artifact_id, timeout=wait_timeout)
    ensure_completed(final)

    out = os.path.join(os.path.dirname(os.path.abspath(manifest_path)), f"ep{episode_n:02d}-report.md")
    await client.artifacts.download_report(notebook_id, out, artifact_id=artifact_id)
    _load_ep_and_write(manifest_path, episode_n, report_md_path=out, report_format=report_format)
    return {"episode": episode_n, "report_md_path": out, "report_format": report_format, "artifact_id": artifact_id}
