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


def test_to_report_format_supports_custom():
    """ReportFormat 有第四個成員 CUSTOM(配 custom_prompt 完全自訂講義結構),
    白名單漏了它等於整個能力對呼叫端不存在。"""
    from notebooklm.types import ReportFormat

    from notebooklm_mcp.enums import to_report_format

    assert to_report_format("custom") == ReportFormat.CUSTOM


def test_report_format_whitelist_matches_what_the_web_backend_can_dispatch():
    """`ReportFormat` 有幾個成員 ≠ 我們生得出幾個。**判準是 backend 的 dispatch table。**

    v0.9.25-rc 驗收 5.1 的事故:上一版把 `CONCEPT_EXPLANATION` 加進白名單,理由是
    「SDK 有這個成員,白名單漏了就等於能力對呼叫端不存在」—— 那個推理對 `CUSTOM` 成立,
    對這一個**不成立**。0.8.2 把 client 拆成 web / android 之後,`ReportFormat` 仍是
    backend-neutral,但 dispatch config 是 backend-specific:`CONCEPT_EXPLANATION`
    只在 `_android` 有,而我們釘在 web。實跑拿到的是
    `Unsupported report format …; expected one of: briefing_doc, study_guide, blog_post, custom`。

    **上一版的 tripwire 指錯方向,而且是它把人推向錯誤答案的**:它斷言「每個 enum 成員
    都要進白名單或進 `_DECLINED`」,於是「加進白名單」看起來就是讓它變綠的正解。
    真正的不變式是**我們開放的每一個靜態格式,web 都生得出來** —— 這一條會攔下那次改動,
    而且反向也有用:上游哪天把某個格式加進 web 的表,這裡會告訴我們現在可以開放了。

    `CUSTOM` 不在靜態格式裡是正常的 —— 它走 `custom_prompt`,
    不是靜態模板,所以單獨排除。
    """
    # 0.8.3 拿掉了 `_STATIC_REPORT_CONFIGS`(改走 creation policy),所以不再讀上游的表,
    # 直接問 web 的 params builder 每個格式「編不編得出來」—— 那一支就是會丟
    # `Unsupported report format` 的地方,驗的是我們真的做得到什麼。
    from notebooklm._web.params.artifacts import build_report_artifact_params
    from notebooklm.types import ReportFormat

    from notebooklm_mcp.enums import _REPORT_FORMAT

    def _web_can_build(fmt):
        try:
            build_report_artifact_params(
                "nb",
                ["src"],
                report_format=fmt,
                language="en",
                custom_prompt=None,
                extra_instructions=None,
            )
        except ValueError:
            return False
        return True

    exposed_static = {v for v in _REPORT_FORMAT.values() if v is not ReportFormat.CUSTOM}
    dispatchable = {f for f in ReportFormat if f is not ReportFormat.CUSTOM and _web_can_build(f)}
    assert dispatchable, "builder 對每個格式都失敗 —— 探測本身壞了,不是白名單的問題"

    dead = exposed_static - dispatchable
    assert not dead, (
        f"白名單開放了 web 生不出來的格式 {sorted(f.name for f in dead)} —— 呼叫端會拿到 "
        f"`Unsupported report format`,而那是燒完一次呼叫才發現的死選項。"
        f"web 現在支援:{sorted(f.name for f in dispatchable)}"
    )

    # 反向只是通報,不是失敗:上游把新格式加進 web 的表時,我們可以開放它。
    unexposed = dispatchable - exposed_static
    assert not unexposed, (
        f"web 支援但我們沒開放:{sorted(f.name for f in unexposed)} —— 要嘛加進 "
        f"enums._REPORT_FORMAT(順便同步 generate_report docstring 與 skill),"
        f"要嘛在這裡寫明為什麼不開放。"
    )


def test_every_sdk_enum_member_is_mapped_or_explicitly_declined():
    """SDK enum 長出新成員時要爆,而不是靜默對呼叫端不存在。

    `ReportFormat.CUSTOM` 被漏掉過一次(上游一直有,白名單沒有,整個「自訂講義結構」
    的能力對呼叫端等於不存在,直到有人手動比對才發現)。這條測試把「漏了」變成「必須明講」。

    **但「明講」不等於「開放」** —— v0.9.25-rc 驗收 5.1:上一版把
    `CONCEPT_EXPLANATION` 加進白名單只為了讓這條綠,結果開出一個 web 生不出來的死選項。
    能不能開放由上面那條(對 backend dispatch table 驗)決定;這一條只保證**有人做過決定**。
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
    _DECLINED = {
        "ReportFormat": {
            # web backend 的 params builder 編不出它(只在 _android),
            # 而我們釘在 web。開放 = 死選項。實測訊息見上一條測試的 docstring。
            "CONCEPT_EXPLANATION": "web backend 沒有 dispatch config,只有 android 有",
        },
    }

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
            f"沒跟上。**先確認 backend 生不生得出來**,再決定加進 enums.py 的 map、"
            f"還是加進本測試的 _DECLINED 並寫下理由。"
        )
