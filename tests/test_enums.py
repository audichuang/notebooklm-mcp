import pytest
from notebooklm.rpc.types import AudioFormat, AudioLength

from notebooklm_mcp.enums import to_audio_format, to_audio_length


def test_audio_format_strings():
    assert to_audio_format("deep-dive") == AudioFormat.DEEP_DIVE
    assert to_audio_format("debate") == AudioFormat.DEBATE
    assert to_audio_format(None) is None


def test_audio_length_strings():
    assert to_audio_length("short") == AudioLength.SHORT
    assert to_audio_length("default") == AudioLength.DEFAULT
    assert to_audio_length("long") == AudioLength.LONG
    assert to_audio_length(None) is None


def test_invalid_raises():
    with pytest.raises(ValueError):
        to_audio_format("bogus")
    with pytest.raises(ValueError):
        to_audio_length("medium")
