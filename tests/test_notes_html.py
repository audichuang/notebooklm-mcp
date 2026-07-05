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
