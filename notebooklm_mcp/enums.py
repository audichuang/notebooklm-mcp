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
