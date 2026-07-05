import pytest

from notebooklm_mcp.publish.notes_html import render_report_html


def test_renders_markdown_structure():
    md = "# 標題\n\n- 一\n- 二\n\n```py\nx = 1\n```"
    html = render_report_html(md, "EP01 測試")
    assert "<h1" in html and "標題" in html
    assert "<ul" in html and "<li>一</li>" in html
    assert "<code>" in html or "<pre>" in html


def test_self_contained_and_titled():
    html = render_report_html("內文", "我的講義")
    assert html.lstrip().lower().startswith("<!doctype html>")
    assert "我的講義" in html                 # title 有帶入
    assert "http://" not in html and "https://" not in html  # 無外部資源
    assert "<style" in html                    # CSS inline


def test_rejects_raw_script():
    with pytest.raises(ValueError, match="自包含"):
        render_report_html("正文\n\n<script>alert(1)</script>", "x")


def test_rejects_external_image():
    # markdown 的 ![](http) 會產生 <img src=http> → 非自包含 → 必須擋
    with pytest.raises(ValueError, match="自包含"):
        render_report_html("![遠端](https://evil.example/x.png)", "x")


def test_code_showing_html_tags_is_safe():
    # 程式碼區塊裡展示 <script> 會被逸出成 &lt;script,是安全的,不該誤擋
    html = render_report_html("```html\n<script>x</script>\n```", "x")
    assert "&lt;script&gt;" in html          # 逸出後當文字顯示
    assert "<script>" not in html            # 沒有真的 script 標籤
