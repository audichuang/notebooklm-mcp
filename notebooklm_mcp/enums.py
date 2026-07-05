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
