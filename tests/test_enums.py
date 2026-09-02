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


def test_to_report_format_supports_concept_explanation():
    """ReportFormat 的第五個成員。跟 CUSTOM 同一個根因:上游一直有,白名單漏了,
    所以整個「概念解釋」形狀的講義對呼叫端等於不存在。

    這一筆是 0.8.2 升級時**逐項比對 enum 值**才浮出來的(0.8.1 就漏著,不是新的漂移),
    現在由 test_every_sdk_enum_member_is_mapped_or_explicitly_declined 擋住第三次。
    """
    from notebooklm.types import ReportFormat
    from notebooklm_mcp.enums import to_report_format

    assert to_report_format("concept_explanation") == ReportFormat.CONCEPT_EXPLANATION


def test_to_report_format_supports_custom():
    """ReportFormat 有第四個成員 CUSTOM(配 custom_prompt 完全自訂講義結構),
    白名單漏了它等於整個能力對呼叫端不存在。"""
    from notebooklm.types import ReportFormat
    from notebooklm_mcp.enums import to_report_format

    assert to_report_format("custom") == ReportFormat.CUSTOM


def test_every_sdk_enum_member_is_mapped_or_explicitly_declined():
    """SDK enum 長出新成員時要爆,而不是靜默對呼叫端不存在。

    `ReportFormat.CUSTOM` 被漏掉過一次(上游一直有,白名單沒有,整個「自訂講義結構」
    的能力對呼叫端等於不存在,直到有人手動比對才發現)。**同一個根因現在還躺著第二筆**:
    `CONCEPT_EXPLANATION`(0.8.1 / 0.8.2 都有)。這條測試把「漏了」變成「必須明講」——
    要嘛進白名單,要嘛進 `_DECLINED` 並寫下理由。

    這不是 0.8.2 帶來的問題(兩版一模一樣),是升級時逐項比對 enum 值才浮出來的。
    """
    from notebooklm.rpc.types import AudioFormat, AudioLength
    from notebooklm.types import ReportFormat, SlideDeckFormat, SlideDeckLength
    from notebooklm_mcp.enums import (
        _FORMAT,
        _LENGTH,
        _REPORT_FORMAT,
        _SLIDE_FORMAT,
        _SLIDE_LENGTH,
    )

    # 已知、刻意不開放的成員 → 值是「為什麼」。空 dict = 該 enum 全部開放。
    # **現在是空的**:v0.9.24 把 CONCEPT_EXPLANATION 補進白名單之後,五個 enum 全開放。
    # 下次上游長出新成員時這裡才會再有東西 —— 而那必須是一個寫得出理由的決定。
    _DECLINED: dict[str, dict[str, str]] = {}

    for name, sdk_enum, ours in [
        ("AudioFormat", AudioFormat, _FORMAT),
        ("AudioLength", AudioLength, _LENGTH),
        ("SlideDeckFormat", SlideDeckFormat, _SLIDE_FORMAT),
        ("SlideDeckLength", SlideDeckLength, _SLIDE_LENGTH),
        ("ReportFormat", ReportFormat, _REPORT_FORMAT),
    ]:
        mapped = {member.name for member in ours.values()}
        declined = set(_DECLINED.get(name, {}))
        unaccounted = {member.name for member in sdk_enum} - mapped - declined
        assert not unaccounted, (
            f"{name} 有沒被交代的成員 {sorted(unaccounted)}:上游新增了能力而我們的白名單"
            f"沒跟上,呼叫端無從使用。要嘛加進 enums.py 的 map,要嘛加進本測試的 _DECLINED "
            f"並寫下理由。"
        )
        # 反向也要顧:map 指到已被上游移除的成員,import 就會先爆,不必另測。
