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
    # ⚠️ **這份不等於 `ReportFormat` 的全部,而且不該等於。** `ReportFormat` 是
    # backend-neutral 的 enum,但能不能真的生出來是 **backend-specific** 的:web 的
    # `_web.params.artifacts.build_report_artifact_params` 只編得出三個靜態格式,
    # `CONCEPT_EXPLANATION` **只在 `_android` 有 dispatch config**,而我們釘在 web
    # (見 tests/test_contracts.py::test_default_backend_is_still_web)。
    # v0.9.25-rc 驗收 5.1 實測:傳它會拿到
    # `Unsupported report format …; expected one of: briefing_doc, study_guide, blog_post, custom`。
    # 判準因此是「web 生得出來」,不是「enum 有這個成員」——
    # tests/test_enums.py 的 tripwire 現在對 `_STATIC_REPORT_CONFIGS` 驗,別再改回對 enum 驗。
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
