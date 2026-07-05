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


def test_episode_notes_html_bullets_and_named_links():
    from notebooklm_mcp.publish.notes_html import render_episode_notes_html

    notes = "鉤子一段。\n\n• 穩固地基:harness 七檔\n• 最小迴圈:三步驟\n\n改編自《X》。"
    atts = [
        ("📄", "本集簡報 (PDF)", "https://h.example/EP01-a.pdf"),
        ("📖", "研讀講義", "https://h.example/EP01-b.html"),
    ]
    html = render_episode_notes_html(notes, atts)
    # bullets 變真正清單
    assert "<ul>" in html and "<li>穩固地基:harness 七檔</li>" in html
    # 具名連結:顯示 label,href 藏 URL(不再裸露長 URL 當顯示文字)
    assert '<a href="https://h.example/EP01-a.pdf">本集簡報 (PDF)</a>' in html
    assert '<a href="https://h.example/EP01-b.html">研讀講義</a>' in html
    # URL 只在 href,不裸露當顯示文字
    assert ">https://h.example/EP01-a.pdf</a>" not in html
    # 段落
    assert "<p>鉤子一段。</p>" in html


def test_episode_notes_neutralizes_raw_html():
    # notes 夾帶原始 <script> 先被逸出成文字,公開 feed 不會有可執行標籤
    from notebooklm_mcp.publish.notes_html import render_episode_notes_html

    html = render_episode_notes_html("正文\n\n<script>alert(1)</script>", [])
    assert "<script>" not in html            # 沒有真的 script 標籤
    assert "&lt;script&gt;" in html          # 逸出後當文字顯示


def test_episode_notes_html_no_attachments():
    from notebooklm_mcp.publish.notes_html import render_episode_notes_html

    html = render_episode_notes_html("只有一段話。", [])
    assert "<p>只有一段話。</p>" in html
    assert "<a " not in html
