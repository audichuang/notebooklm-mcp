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
    assert "我的講義" in html  # title 有帶入
    assert "http://" not in html and "https://" not in html  # 無外部資源
    assert "<style" in html  # CSS inline


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
    assert "&lt;script&gt;" in html  # 逸出後當文字顯示
    assert "<script>" not in html  # 沒有真的 script 標籤


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
    assert "<script>" not in html  # 沒有真的 script 標籤
    assert "&lt;script&gt;" in html  # 逸出後當文字顯示


def test_episode_notes_html_no_attachments():
    from notebooklm_mcp.publish.notes_html import render_episode_notes_html

    html = render_episode_notes_html("只有一段話。", [])
    assert "<p>只有一段話。</p>" in html
    assert "<a " not in html


def test_episode_notes_rejects_markdown_image():
    # ![](http) 是合法 markdown 語法(逐行 escape 逃不掉),渲染後會變 <img src=...>
    # 外連 tracking pixel——必須跟 report 一樣 fail-closed 拒絕。
    from notebooklm_mcp.publish.notes_html import render_episode_notes_html

    with pytest.raises(ValueError, match="自包含"):
        render_episode_notes_html("![t](https://evil.example/pixel.png)", [])


def test_episode_notes_rejects_javascript_link():
    # [點我](javascript:...) 同理是合法 markdown 語法,渲染後會產出 javascript: 連結。
    from notebooklm_mcp.publish.notes_html import render_episode_notes_html

    with pytest.raises(ValueError, match="自包含"):
        render_episode_notes_html("[點我](javascript:alert(1))", [])


def test_episode_notes_with_attachments_still_renders():
    # guard 不可誤殺我們自己拼的 attachments 連結(自家 podcast https URL)。
    from notebooklm_mcp.publish.notes_html import render_episode_notes_html

    atts = [("📄", "本集簡報 (PDF)", "https://h.example/EP01-a.pdf")]
    html = render_episode_notes_html("正常內容一段。", atts)
    assert '<a href="https://h.example/EP01-a.pdf">本集簡報 (PDF)</a>' in html
    assert "<p>正常內容一段。</p>" in html


# ---- P2 review:改成允許清單、只掃標籤 ——不可再誤殺普通中文散文 -------------------


def test_prose_mentioning_javascript_scheme_word_is_not_misfired():
    # 「JavaScript:」只是散文裡的技術詞,不是任何標籤的 href scheme——舊版全文 regex
    # 掃到「javascript:」字串就 raise,新版只看標籤/屬性,這句應該正常通過。
    from notebooklm_mcp.publish.notes_html import render_episode_notes_html

    html = render_episode_notes_html("• JavaScript:動態語言的起點", [])
    assert "JavaScript" in html


def test_prose_mentioning_on_param_assignment_is_not_misfired():
    # 「online=1」只是散文裡提到的參數賦值,不是任何標籤的 on* 事件屬性——舊版
    # `\son\w+\s*=` regex 會誤殺,新版只看真正的標籤屬性。
    from notebooklm_mcp.publish.notes_html import render_report_html

    html = render_report_html("設定 online=1 才會生效。", "x")
    assert "online=1" in html


def test_rejects_svg_image_href():
    # <svg><image href=javascript:...> 是黑名單掃 <script|iframe|...> 標籤全集會漏掉的
    # 攻擊面;允許清單制下 <svg>/<image> 本身就不在清單,直接擋。
    with pytest.raises(ValueError, match="自包含"):
        render_report_html('<svg><image href="javascript:alert(1)"></image></svg>', "x")


def test_rejects_input_type_image_src():
    with pytest.raises(ValueError, match="自包含"):
        render_report_html('<input type="image" src="x" onerror="alert(1)">', "x")


def test_rejects_table_background_attribute():
    # <table> 本身在允許清單內,但 background= 這個屬性不在允許清單——必須逐屬性擋,
    # 不能只驗標籤名。
    with pytest.raises(ValueError, match="自包含"):
        render_report_html(
            '<table background="javascript:alert(1)"><tr><td>a</td></tr></table>', "x"
        )


def test_rejects_entity_encoded_javascript_scheme_in_link():
    # jav&#x09;ascript: 用 entity 塞一個 tab 把 "javascript:" 拆開,繞過字串比對;
    # HTMLParser(convert_charrefs=True)會先解碼再驗 scheme,不會被繞過。
    with pytest.raises(ValueError, match="自包含"):
        render_report_html("[點我](jav&#x09;ascript:alert(1))", "x")


def test_table_align_syntax_style_attribute_is_allowed():
    # tables extension 的對齊語法會在 th/td 產出 style="text-align: …",必須放行
    # (不是全面允許 style,只放行這個精確 pattern)。
    md = "| A | B |\n|:---|---:|\n| 1 | 2 |\n"
    html = render_report_html(md, "x")
    assert 'style="text-align: left;"' in html
    assert 'style="text-align: right;"' in html


def test_rejects_markup_hidden_in_cdata():
    # HTMLParser 把 <![CDATA[ … ]]> 整段吞成 unknown_decl,裡面的 <img> 不會觸發
    # handle_starttag;但瀏覽器把 <![CDATA[ 當 bogus comment 在第一個 > 結束,<img>
    # 變真元素(chrome --dump-dom 驗過)。舊的字串 regex 反而擋得住——這條防回歸。
    with pytest.raises(ValueError, match="自包含"):
        render_report_html("<![CDATA[ > <img src=https://evil.example/x.png> ]]>", "x")


def test_rejects_markup_hidden_in_comment():
    with pytest.raises(ValueError, match="自包含"):
        render_report_html("<!-- <img src=https://evil.example/x.png> -->", "x")


def test_plain_comment_without_markup_is_allowed():
    # 只擋「被隱藏區段裡藏 markup」,純文字備註不誤殺。
    html = render_report_html("正文\n\n<!-- 純文字備註,無標籤 -->", "x")
    assert "evil" not in html
