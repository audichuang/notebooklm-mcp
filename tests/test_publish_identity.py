import re
import pytest
from notebooklm_mcp.publish import identity


def test_make_token_is_deterministic():
    a = identity.make_token("ai-news", salt="s3cret")
    b = identity.make_token("ai-news", salt="s3cret")
    assert a == b


def test_make_token_differs_by_show_id():
    assert identity.make_token("ai-news", salt="s3cret") != identity.make_token(
        "rust-deep", salt="s3cret"
    )


def test_make_token_differs_by_salt():
    assert identity.make_token("ai-news", salt="s1") != identity.make_token(
        "ai-news", salt="s2"
    )


def test_token_charset_is_base32_lower():
    token = identity.make_token("ai-news", salt="s3cret")
    assert re.fullmatch(r"[a-z2-7]+", token)
    assert len(token) == 24  # 15 bytes -> 24 base32 chars, no padding


def test_make_token_requires_salt():
    with pytest.raises(ValueError):
        identity.make_token("ai-news", salt="")


@pytest.mark.parametrize("bad", ["", "A", "UPPER", "has space", "trailing-", "-lead", "x" * 100])
def test_validate_show_id_rejects_bad(bad):
    with pytest.raises(ValueError):
        identity.validate_show_id(bad)


@pytest.mark.parametrize("ok", ["ai-news", "rust-deep", "s2", "a1b2-c3"])
def test_validate_show_id_accepts_good(ok):
    assert identity.validate_show_id(ok) == ok


def test_episode_guid_stable_and_source_decoupled():
    from notebooklm_mcp.publish import identity
    g1 = identity.episode_guid("ai-news", 1)
    assert g1 == identity.episode_guid("ai-news", 1)
    assert g1 != identity.episode_guid("ai-news", 2)
    assert g1 != identity.episode_guid("other", 1)
