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


def test_attachment_filename():
    from notebooklm_mcp.publish.layout import attachment_filename

    assert attachment_filename(1, "deadbeef", "pdf") == "EP01-deadbeef.pdf"
    assert attachment_filename(12, "0a1b2c3d", "html") == "EP12-0a1b2c3d.html"
