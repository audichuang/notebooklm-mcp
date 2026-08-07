"""Caller-supplied source selection: shape check + cheap existence preflight.

不傳 ``source_ids`` 時 SDK 自己抓筆記本全部來源(``_artifacts.py`` 的 fallback);
要「只聽這幾筆」就得指名。**選哪幾筆是 host 的政策**(ADR-0007)——例如重生某集時
排除集號更大的來源——這裡只負責別讓壞的選取燒掉一次生成。
"""
from __future__ import annotations


def to_source_ids(value: object) -> list[str] | None:
    """Normalize a caller's selection; ``None`` keeps the SDK's use-everything fallback."""
    if value is None:
        return None
    # 字串是最容易誤傳的形狀(單一 id 忘了包 list),而它剛好是 iterable of str,
    # 放行會靜默變成「每個字元一筆來源」。
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise ValueError(f"source_ids must be a list of source id strings, got {value!r}")
    ids = list(value)
    if not ids:
        raise ValueError("source_ids must not be empty; omit it to use every source")
    if any(not isinstance(sid, str) or not sid.strip() for sid in ids):
        raise ValueError(f"source_ids entries must be non-empty strings, got {ids!r}")
    if len(set(ids)) != len(ids):
        raise ValueError(f"source_ids must not repeat a source id, got {ids!r}")
    return ids


async def assert_sources_exist(client, notebook_id: str, source_ids: list[str]) -> None:
    """打錯或已被刪掉的 source_id 伺服器不會擋——它只會靜默生出一集「看不到那幾筆
    來源」的音檔,燒完配額與二十分鐘才發現(同 ``_validate_report_prompt`` 的情境)。
    一次唯讀 list RPC 換掉那個代價。"""
    live = {getattr(source, "id", None) for source in await client.sources.list(notebook_id)}
    missing = [sid for sid in source_ids if sid not in live]
    if missing:
        raise ValueError(
            f"source_ids not in notebook {notebook_id}: {', '.join(missing)}; "
            "call source_list and select from its real ids"
        )
