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
