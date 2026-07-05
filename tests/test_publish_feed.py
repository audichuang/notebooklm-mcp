import xml.etree.ElementTree as ET
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


def test_namespaces_declared():
    raw = feed.build_feed_xml(SHOW, BASE)
    assert 'xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"' in raw
    assert 'xmlns:content="http://purl.org/rss/1.0/modules/content/"' in raw
    assert 'xmlns:atom="http://www.w3.org/2005/Atom"' in raw
    assert raw.startswith("<?xml")


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
