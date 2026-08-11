import pytest
from PIL import Image
from notebooklm_mcp.publish import artwork


def _make(tmp_path, name, size, mode="RGB", fmt="PNG"):
    p = tmp_path / name
    Image.new(mode, size).save(str(p), format=fmt)
    return str(p)


def test_valid_square_png_passes(tmp_path):
    info = artwork.validate_artwork(_make(tmp_path, "ok.png", (1500, 1500)))
    assert info["width"] == 1500 and info["height"] == 1500
    assert info["format"] == "PNG"


def test_valid_jpeg_passes(tmp_path):
    info = artwork.validate_artwork(_make(tmp_path, "ok.jpg", (1400, 1400), fmt="JPEG"))
    assert info["format"] == "JPEG"


def test_truncated_image_rejected_even_when_header_is_readable(tmp_path):
    path = _make(tmp_path, "truncated.jpg", (1400, 1400), fmt="JPEG")
    with open(path, "rb+") as image_file:
        image_file.truncate(image_file.seek(0, 2) - 100)

    with pytest.raises(ValueError, match="readable image"):
        artwork.validate_artwork(path)


def test_non_square_rejected(tmp_path):
    with pytest.raises(ValueError, match="square"):
        artwork.validate_artwork(_make(tmp_path, "rect.png", (1500, 1400)))


def test_too_small_rejected(tmp_path):
    with pytest.raises(ValueError, match="1400"):
        artwork.validate_artwork(_make(tmp_path, "small.png", (1000, 1000)))


def test_too_large_rejected(tmp_path):
    with pytest.raises(ValueError, match="3000"):
        artwork.validate_artwork(_make(tmp_path, "big.png", (3200, 3200)))


def test_alpha_channel_rejected(tmp_path):
    with pytest.raises(ValueError, match="alpha"):
        artwork.validate_artwork(_make(tmp_path, "rgba.png", (1500, 1500), mode="RGBA"))


def test_grayscale_rejected(tmp_path):
    with pytest.raises(ValueError, match="RGB"):
        artwork.validate_artwork(_make(tmp_path, "gray.png", (1500, 1500), mode="L"))


def test_cmyk_rejected(tmp_path):
    with pytest.raises(ValueError, match="RGB"):
        artwork.validate_artwork(_make(tmp_path, "cmyk.jpg", (1500, 1500), mode="CMYK", fmt="JPEG"))


def test_wrong_format_rejected(tmp_path):
    with pytest.raises(ValueError, match="PNG or JPEG"):
        artwork.validate_artwork(_make(tmp_path, "x.gif", (1500, 1500), fmt="GIF"))


def test_missing_file_rejected(tmp_path):
    with pytest.raises(ValueError, match="not found"):
        artwork.validate_artwork(str(tmp_path / "nope.png"))
