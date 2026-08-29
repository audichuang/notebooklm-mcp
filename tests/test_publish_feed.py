import xml.etree.ElementTree as ET
import pytest
from notebooklm_mcp.publish import feed

NS = {
    "itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd",
    "atom": "http://www.w3.org/2005/Atom",
    "content": "http://purl.org/rss/1.0/modules/content/",
}

SHOW = {
    "show_id": "ai-news",
    "token": "tok123",
    "title": "AI 新聞 & 觀察",  # 含 & 測 XML escape
    "description": "每日 AI 摘要",
    "language": "zh-Hant",
    "author": "Audi",
    "owner_name": "Audi",
    "owner_email": "audi@example.com",
    "category": "Technology",
    "explicit": False,
    "artwork_file": "artwork.png",
    "episodes": {
        "1": {
            "title": "心法篇",
            "description": "心法篇",
            "guid": "g1",
            "pub_date": "Sun, 21 Jun 2026 09:00:00 +0800",
            "media_file": "EP01-ab12cd34.mp3",
            "length": 12345,
        },
        "2": {
            "title": "實戰篇 <重點>",  # 含 < 測 escape
            "description": "實戰篇",
            "guid": "g2",
            "pub_date": "Mon, 22 Jun 2026 09:00:00 +0800",
            "media_file": "EP02-deadbeef.mp3",
            "length": 22222,
        },
    },
}
BASE = "https://podcast.example"


def _feed():
    return ET.fromstring(feed.build_feed_xml(SHOW, BASE))


def test_channel_required_fields():
    ch = _feed().find("channel")
    assert ch.findtext("title") == "AI 新聞 & 觀察"
    assert ch.findtext("description") == "每日 AI 摘要"
    assert ch.findtext("language") == "zh-Hant"
    assert ch.findtext("itunes:author", namespaces=NS) == "Audi"
    owner = ch.find("itunes:owner", NS)
    assert owner.findtext("itunes:email", namespaces=NS) == "audi@example.com"
    assert ch.find("itunes:image", NS).get("href") == f"{BASE}/feeds/tok123/artwork.png"
    assert ch.findtext("itunes:explicit", namespaces=NS) == "false"  # 小寫
    assert ch.find("atom:link", NS).get("href") == f"{BASE}/feeds/tok123/feed.xml"
    assert ch.findtext("link") == f"{BASE}/feeds/tok123/index.html"
    # lastBuildDate = 最後一集(EP02)的 pubDate,穩定
    assert ch.findtext("lastBuildDate") == "Mon, 22 Jun 2026 09:00:00 +0800"


def test_items_escape_xml():
    items = _feed().find("channel").findall("item")
    assert len(items) == 2
    ep1 = items[0]
    assert ep1.findtext("title") == "心法篇"
    assert ep1.findtext("guid") == "g1"
    assert ep1.find("guid").get("isPermaLink") == "false"
    enc = ep1.find("enclosure")
    assert enc.get("url") == f"{BASE}/feeds/tok123/EP01-ab12cd34.mp3"
    assert enc.get("length") == "12345"
    assert enc.get("type") == "audio/mpeg"
    assert ep1.findtext("pubDate") == "Sun, 21 Jun 2026 09:00:00 +0800"
    # XML escaping round-trips: parser yields the raw chars.
    assert items[1].findtext("title") == "實戰篇 <重點>"


def test_items_include_global_episode_number_without_season():
    """節目跨季沿用全域 EP 編號；播放器應顯示 EP01、EP02，而不是季內編號。"""
    items = _feed().find("channel").findall("item")
    for n, item in enumerate(items, 1):
        assert item.findtext("itunes:episode", namespaces=NS) == str(n)
        assert item.findtext("itunes:episodeType", namespaces=NS) == "full"
        assert item.find("itunes:season", NS) is None


def test_namespaces_declared():
    raw = feed.build_feed_xml(SHOW, BASE)
    assert 'xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"' in raw
    assert 'xmlns:content="http://purl.org/rss/1.0/modules/content/"' in raw
    assert 'xmlns:atom="http://www.w3.org/2005/Atom"' in raw
    assert raw.startswith("<?xml")


def test_feed_rejects_xml_10_forbidden_control_characters():
    show = {**SHOW, "title": "AI\x01新聞"}
    with pytest.raises(ValueError, match=r"XML 1\.0.*U\+0001"):
        feed.build_feed_xml(show, BASE)


def test_index_html_lists_live_episodes_only():
    html = feed.build_index_html(SHOW, BASE)
    assert "心法篇" in html and "實戰篇" in html


def test_content_encoded_emitted_when_description_html_present():
    show = {**SHOW, "episodes": {
        "1": {**SHOW["episodes"]["1"],
              "description_html": '<p>鉤子</p><ul><li>一</li></ul><p>📄 <a href="https://h/x.pdf">本集簡報 (PDF)</a></p>'},
    }}
    item = feed.build_feed_xml(show, BASE)
    # content:encoded 有出現且含 CDATA
    assert "<content:encoded><![CDATA[" in item
    # 解析後拿得到 HTML(具名連結,不是裸 URL 當文字)
    it = ET.fromstring(item).find("channel/item")
    ce = it.findtext("content:encoded", namespaces=NS)
    assert '<a href="https://h/x.pdf">本集簡報 (PDF)</a>' in ce
    assert "<ul><li>一</li></ul>" in ce


def test_no_content_encoded_when_absent():
    # SHOW 的兩集都沒 description_html → 不應出現 content:encoded(向後相容)
    assert "content:encoded" not in feed.build_feed_xml(SHOW, BASE)


def test_item_itunes_image_only_when_episode_has_artwork():
    # EP01 有自己的封面 → <item> 內帶 itunes:image;EP02 沒有 → 該集省略,
    # 由播放器 fallback 到 channel 層的節目封面(向後相容:舊 show 無此欄位即無 item image)。
    show = {**SHOW, "episodes": {
        "1": {**SHOW["episodes"]["1"], "artwork_file": "EP01-cover-abcd1234.jpg"},
        "2": SHOW["episodes"]["2"],
    }}
    items = ET.fromstring(feed.build_feed_xml(show, BASE)).find("channel").findall("item")
    assert items[0].find("itunes:image", NS).get("href") == f"{BASE}/feeds/tok123/EP01-cover-abcd1234.jpg"
    assert items[1].find("itunes:image", NS) is None


def test_content_encoded_escapes_cdata_end_marker():
    # description_html 內含 "]]>" 不能提前關閉 CDATA;拆分後解析仍還原原字串
    html = "<p>a]]>b</p>"
    show = {**SHOW, "episodes": {"1": {**SHOW["episodes"]["1"], "description_html": html}}}
    raw = feed.build_feed_xml(show, BASE)
    assert "]]]]><![CDATA[>" in raw
    ce = ET.fromstring(raw).findtext("channel/item/content:encoded", namespaces=NS)
    assert ce == html          # round-trip 還原


# ---- itunes:type(連載 vs 時事):channel 層一律輸出 --------------------------------


def test_channel_declares_itunes_type_explicitly():
    """**不能靠 Apple 的隱含預設。** 沒有 `<itunes:type>` 時 Apple 當 episodic,於是
    照 pubDate 由新到舊排,`itunes:episode` 基本被忽略 —— 連載節目打開看到的第一集是
    最後一集。跟 `<itunes:explicit>` 同一條紀律:即使是預設值也明講。"""
    ch = _feed().find("channel")
    assert ch.find("itunes:type", NS).text == "episodic"

    serial = {**SHOW, "itunes_type": "serial"}
    ch = ET.fromstring(feed.build_feed_xml(serial, BASE)).find("channel")
    assert ch.find("itunes:type", NS).text == "serial"


def test_itunes_type_does_not_reorder_items():
    """**item 順序不跟著 type 動。**

    Apple 對 serial 是用 `itunes:episode` 排,不看 item 的文件順序;而真的照文件順序
    顯示的播放器,遞增正好是連載要的順序 —— 反轉會把問題從 Apple 搬到它們身上。
    """
    for itunes_type in ("episodic", "serial"):
        show = {**SHOW, "itunes_type": itunes_type}
        items = ET.fromstring(feed.build_feed_xml(show, BASE)).find("channel").findall("item")
        assert [i.find("itunes:episode", NS).text for i in items] == ["1", "2"]


def test_unknown_itunes_type_is_refused_before_it_reaches_the_feed():
    """打錯的值進了 feed 只有 Apple 端看得到,所以在渲染邊界就擋。"""
    with pytest.raises(ValueError, match="itunes_type"):
        feed.build_feed_xml({**SHOW, "itunes_type": "series"}, BASE)


def test_itunes_type_allowlist_is_exactly_two_values():
    """精確鎖死值域:放寬成「非空字串就收」的話,單獨拒絕 "series" 那條測試仍會綠。"""
    assert feed.ITUNES_TYPES == ("episodic", "serial")
    for accepted in feed.ITUNES_TYPES:
        assert feed.normalize_itunes_type(accepted) == accepted
    assert feed.normalize_itunes_type(None) == "episodic"
    for rejected in ("", "Serial", "SERIAL", "trailer", "bonus", 1, True, ["serial"]):
        with pytest.raises(ValueError, match="itunes_type"):
            feed.normalize_itunes_type(rejected)


def test_index_html_strips_serial_ep_prefix_from_title_display_only():
    """serial 節目 title 帶「EP{NN}. 」命名前綴;index.html 自己又加一次 EP{NN} —,
    修法只在顯示層剝前綴。RSS item title 不經過這裡,必須原封不動。"""
    show = {**SHOW, "episodes": {
        "1": {**SHOW["episodes"]["1"], "title": "EP01. 心法篇"},
        "2": {**SHOW["episodes"]["2"], "title": "EP02 實戰篇"},
    }}
    html = feed.build_index_html(show, BASE)
    assert "EP01 — 心法篇" in html
    assert "EP02 — 實戰篇" in html
    assert "EP01 — EP01" not in html
    # 別集的集號、或不帶前綴的 title 都不剝
    plain = feed.build_index_html(SHOW, BASE)
    assert "EP01 — 心法篇" in plain
    # RSS 不受影響:item title 仍是完整命名
    xml = feed.build_feed_xml(show, BASE)
    assert "EP01. 心法篇" in xml
