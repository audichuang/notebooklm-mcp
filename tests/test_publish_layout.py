import os
from notebooklm_mcp.publish import layout


def test_content_hash8_is_stable_and_content_addressed(tmp_path):
    p = tmp_path / "a.mp3"
    p.write_bytes(b"hello world")
    h1 = layout.content_hash8(str(p))
    h2 = layout.content_hash8(str(p))
    assert h1 == h2
    assert len(h1) == 8 and all(c in "0123456789abcdef" for c in h1)

    p.write_bytes(b"different")
    assert layout.content_hash8(str(p)) != h1


def test_media_filename():
    assert layout.media_filename(1, "ab12cd34") == "EP01-ab12cd34.mp3"
    assert layout.media_filename(12, "deadbeef") == "EP12-deadbeef.mp3"


def test_atomic_write_text_creates_dirs_and_no_tmp_left(tmp_path):
    target = tmp_path / "feeds" / "tok" / "feed.xml"
    layout.atomic_write_text(str(target), "<rss/>")
    assert target.read_text() == "<rss/>"
    # No leftover temp files in the directory.
    assert [n for n in os.listdir(target.parent) if n.startswith(".tmp-")] == []


def test_atomic_copy_matches_bytes(tmp_path):
    src = tmp_path / "src.mp3"
    src.write_bytes(b"\x00\x01\x02audio")
    dst = tmp_path / "out" / "EP01-aaaaaaaa.mp3"
    layout.atomic_copy(str(src), str(dst))
    assert dst.read_bytes() == b"\x00\x01\x02audio"
