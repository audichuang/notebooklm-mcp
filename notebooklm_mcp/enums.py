"""String -> int-enum maps for NotebookLM audio generation."""
from __future__ import annotations

from notebooklm.rpc.types import AudioFormat, AudioLength

_FORMAT = {
    "deep-dive": AudioFormat.DEEP_DIVE,
    "brief": AudioFormat.BRIEF,
    "critique": AudioFormat.CRITIQUE,
    "debate": AudioFormat.DEBATE,
}
_LENGTH = {
    "short": AudioLength.SHORT,
    "default": AudioLength.DEFAULT,
    "long": AudioLength.LONG,
}


def to_audio_format(value: str | None) -> AudioFormat | None:
    if value is None:
        return None
    try:
        return _FORMAT[value]
    except KeyError:
        raise ValueError(f"Invalid audio format {value!r}. Choose from: {', '.join(_FORMAT)}") from None


def to_audio_length(value: str | None) -> AudioLength | None:
    if value is None:
        return None
    try:
        return _LENGTH[value]
    except KeyError:
        raise ValueError(f"Invalid audio length {value!r}. Choose from: {', '.join(_LENGTH)}") from None


from notebooklm.types import SlideDeckFormat, SlideDeckLength, ReportFormat

_SLIDE_FORMAT = {
    "detailed": SlideDeckFormat.DETAILED_DECK,
    "presenter": SlideDeckFormat.PRESENTER_SLIDES,
}
_SLIDE_LENGTH = {
    "default": SlideDeckLength.DEFAULT,
    "short": SlideDeckLength.SHORT,
}
_REPORT_FORMAT = {
    "study_guide": ReportFormat.STUDY_GUIDE,
    "briefing_doc": ReportFormat.BRIEFING_DOC,
    "blog_post": ReportFormat.BLOG_POST,
    # CUSTOM 走 generate_report(custom_prompt=…) 完全自訂結構;沒有它,三種靜態模板
    # 以外的講義形狀對呼叫端等於不存在(SDK 一直有這個成員,只是白名單漏了)。
    "custom": ReportFormat.CUSTOM,
    # CONCEPT_EXPLANATION 跟 CUSTOM 同一個根因:上游一直有,白名單漏了,整個形狀對
    # 呼叫端等於不存在。這一筆是 0.8.2 升級時逐項比對 enum 值才發現的(0.8.1 就漏著)。
    "concept_explanation": ReportFormat.CONCEPT_EXPLANATION,
    # ⚠️ 這份**現在**等於 ReportFormat 的全部,但別預設它永遠是。上游新增成員時由
    # tests/test_enums.py 的 test_every_sdk_enum_member_is_mapped_or_explicitly_declined
    # 逼著做決定(進白名單、或進 _DECLINED 寫下理由),而不是靠有人剛好去比對。
}


def to_slide_format(value: str | None) -> SlideDeckFormat | None:
    if value is None:
        return None
    try:
        return _SLIDE_FORMAT[value]
    except KeyError:
        raise ValueError(f"Invalid slide format {value!r}. Choose from: {', '.join(_SLIDE_FORMAT)}") from None


def to_slide_length(value: str | None) -> SlideDeckLength | None:
    if value is None:
        return None
    try:
        return _SLIDE_LENGTH[value]
    except KeyError:
        raise ValueError(f"Invalid slide length {value!r}. Choose from: {', '.join(_SLIDE_LENGTH)}") from None


def to_report_format(value: str | None) -> ReportFormat | None:
    if value is None:
        return None
    try:
        return _REPORT_FORMAT[value]
    except KeyError:
        raise ValueError(f"Invalid report format {value!r}. Choose from: {', '.join(_REPORT_FORMAT)}") from None
