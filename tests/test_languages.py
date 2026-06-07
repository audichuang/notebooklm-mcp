import pytest

from notebooklm_mcp.languages import DEFAULT_LANGUAGE, resolve_language


def test_default_is_zh_hant():
    assert DEFAULT_LANGUAGE == "zh_Hant"
    assert resolve_language(None) == "zh_Hant"


def test_accepts_known_code():
    assert resolve_language("en") == "en"
    assert resolve_language("zh_Hant") == "zh_Hant"


def test_rejects_hyphen_form():
    with pytest.raises(ValueError) as e:
        resolve_language("zh-TW")
    assert "zh_Hant" in str(e.value)


def test_rejects_unknown():
    with pytest.raises(ValueError):
        resolve_language("xx")
