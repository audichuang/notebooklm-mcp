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


def test_to_slide_format_and_length():
    from notebooklm.types import SlideDeckFormat, SlideDeckLength
    from notebooklm_mcp.enums import to_slide_format, to_slide_length

    assert to_slide_format("detailed") == SlideDeckFormat.DETAILED_DECK
    assert to_slide_format("presenter") == SlideDeckFormat.PRESENTER_SLIDES
    assert to_slide_format(None) is None
    assert to_slide_length("short") == SlideDeckLength.SHORT
    with pytest.raises(ValueError, match="slide format"):
        to_slide_format("bogus")


def test_to_report_format():
    from notebooklm.types import ReportFormat
    from notebooklm_mcp.enums import to_report_format

    assert to_report_format("study_guide") == ReportFormat.STUDY_GUIDE
    assert to_report_format("briefing_doc") == ReportFormat.BRIEFING_DOC
    assert to_report_format("blog_post") == ReportFormat.BLOG_POST
    assert to_report_format(None) is None
    with pytest.raises(ValueError, match="report format"):
        to_report_format("bogus")
