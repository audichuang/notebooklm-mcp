"""Render the authoritative show state into an RSS 2.0 feed (with the iTunes +
atom namespaces Apple Podcasts needs) and a minimal index.html for <link>.
Pure function of (show, base_url): no I/O, no clock — fully deterministic."""
from __future__ import annotations

import re
from xml.sax.saxutils import escape, quoteattr

from .rss_models import live_episodes

_ITUNES = "http://www.itunes.com/dtds/podcast-1.0.dtd"
_CONTENT = "http://purl.org/rss/1.0/modules/content/"
_ATOM = "http://www.w3.org/2005/Atom"


def _feed_dir_url(base_url: str, token: str) -> str:
    return f"{base_url.rstrip('/')}/feeds/{token}"


def validate_xml_text(value: object) -> None:
    """擋掉 XML 1.0 表達不了的字元。

    這些字元進得了 Python 字串、也進得了 manifest,但寫進 feed.xml 之後整份 feed 對
    Apple 的 parser 就是壞的 —— 而壞掉的位置在**已發布的** RSS,不是在這裡。所以在
    組 XML 之前先擋。dict/list/tuple 遞迴檢查(dict 只看 value:key 是我們自己寫死的)。
    """
    if isinstance(value, str):
        for char in value:
            code = ord(char)
            if not (
                code in (0x9, 0xA, 0xD)
                or 0x20 <= code <= 0xD7FF
                or 0xE000 <= code <= 0xFFFD
                or 0x10000 <= code <= 0x10FFFF
            ):
                raise ValueError(f"XML 1.0 forbids character U+{code:04X}")
    elif isinstance(value, dict):
        for item in value.values():
            validate_xml_text(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            validate_xml_text(item)


#: Apple 的 `<itunes:type>` 值域。`episodic` 是 Apple 在缺這個標籤時的隱含預設,所以
#: 也是我們的預設 —— 不能靠一個布林旗標把別人的節目語意改掉。
ITUNES_TYPES = ("episodic", "serial")


def normalize_itunes_type(value: object) -> str:
    """驗 `itunes_type` 並補上預設。

    **打錯的值只有 Apple 端看得到**(feed 是渲染後直接 PUT 的,本機沒有檔案可對),所以
    在渲染邊界就擋掉,而不是讓 `<itunes:type>series</itunes:type>` 靜默發出去。
    """
    if value is None:
        return "episodic"
    if value not in ITUNES_TYPES:
        raise ValueError(
            f"itunes_type must be one of {ITUNES_TYPES} (got: {value!r}) —— "
            "serial=連載(播放器照 itunes:episode 由第一集排)、episodic=時事(由新到舊)"
        )
    return value


def build_feed_xml(show: dict, base_url: str) -> str:
    validate_xml_text((show, base_url))
    token = show["token"]
    base = _feed_dir_url(base_url, token)
    feed_url = f"{base}/feed.xml"
    explicit = "true" if show.get("explicit") else "false"
    itunes_type = normalize_itunes_type(show.get("itunes_type"))
    eps = live_episodes(show)

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<rss version="2.0" xmlns:itunes="{_ITUNES}" '
        f'xmlns:content="{_CONTENT}" xmlns:atom="{_ATOM}">',
        "  <channel>",
        f"    <title>{escape(show['title'])}</title>",
        f"    <link>{escape(base + '/index.html')}</link>",
        f"    <description>{escape(show['description'])}</description>",
        f"    <language>{escape(show.get('language', 'zh-Hant'))}</language>",
        f"    <itunes:author>{escape(show['author'])}</itunes:author>",
        f"    <itunes:summary>{escape(show['description'])}</itunes:summary>",
        f"    <itunes:explicit>{explicit}</itunes:explicit>",
        # **一律輸出,即使是預設值** —— 跟 `<itunes:explicit>` 同一條紀律。缺這一行時
        # Apple 當 episodic:照 pubDate 由新到舊排,`itunes:episode` 基本被忽略,連載
        # 節目打開看到的第一集是最後一集(實測 SAA/SAP 兩個 feed 都撞到)。
        # **item 的文件順序刻意不跟著這個值變**:Apple 對 serial 是用 `itunes:episode`
        # 排、不看文件順序;而真的照文件順序顯示的播放器,現行的遞增正好是連載要的順序
        # —— 反轉只會把問題從 Apple 搬到它們身上,還讓每一個既有 feed 的 bytes 全變。
        f"    <itunes:type>{itunes_type}</itunes:type>",
        f'    <itunes:category text={quoteattr(show.get("category", "Technology"))}/>',
        f'    <itunes:image href={quoteattr(base + "/" + show["artwork_file"])}/>',
        "    <itunes:owner>",
        f"      <itunes:name>{escape(show['owner_name'])}</itunes:name>",
        f"      <itunes:email>{escape(show['owner_email'])}</itunes:email>",
        "    </itunes:owner>",
        f'    <atom:link href={quoteattr(feed_url)} rel="self" type="application/rss+xml"/>',
    ]

    # lastBuildDate = the most recent live episode's (stable) pubDate, so the feed
    # stays byte-identical across rebuilds when nothing changed (no wall clock).
    if eps:
        lines.append(f"    <lastBuildDate>{escape(eps[-1][1]['pub_date'])}</lastBuildDate>")

    for n, ep in eps:
        media_url = f"{base}/{ep['media_file']}"
        item = [
            "    <item>",
            f"      <title>{escape(ep['title'])}</title>",
            # 使用跨季續接的全域集號，讓 Pocket Casts / Apple 等客戶端能顯示
            # 「第幾集」。刻意不輸出 itunes:season，避免 EP12 被重解讀成 S2E6。
            f"      <itunes:episode>{n}</itunes:episode>",
            "      <itunes:episodeType>full</itunes:episodeType>",
            # <description> = 純文字(fallback,含裸 URL);<content:encoded> = 富文字 HTML
            # (Apple/Overcast/Pocket Casts 優先渲染:條列 + 具名連結,不裸露長 URL)。
            f"      <description>{escape(ep.get('description', ep['title']))}</description>",
        ]
        html = ep.get("description_html")
        if html:
            # CDATA 安全:內容若含 "]]>" 會提前關閉,拆開再續。
            safe = html.replace("]]>", "]]]]><![CDATA[>")
            item.append(f"      <content:encoded><![CDATA[{safe}]]></content:encoded>")
        # 單集封面(選填):有才放 <itunes:image>,沒有就省略 → 播放器 fallback 到 channel
        # 層的節目封面(向後相容:舊 show 無此欄位即無單集圖)。
        ep_art = ep.get("artwork_file")
        if ep_art:
            item.append(f'      <itunes:image href={quoteattr(base + "/" + ep_art)}/>')
        duration = ep.get("duration")
        if duration:
            item.append(f"      <itunes:duration>{escape(str(duration))}</itunes:duration>")
        item += [
            f"      <pubDate>{escape(ep['pub_date'])}</pubDate>",
            f'      <guid isPermaLink="false">{escape(ep["guid"])}</guid>',
            f'      <enclosure url={quoteattr(media_url)} length="{int(ep["length"])}" type="audio/mpeg"/>',
            "    </item>",
        ]
        lines += item

    lines += ["  </channel>", "</rss>", ""]
    return "\n".join(lines)


def build_index_html(show: dict, base_url: str) -> str:
    base = _feed_dir_url(base_url, show["token"])
    # 顯示層去重:serial 節目的 title 本來就帶「EP{NN}. 」前綴(命名鐵律,不在這裡碰),
    # 這行自己又加一次 EP{NN} — 會顯示成「EP01 — EP01. 標題」。只剝掉等於本集集號的前綴;
    # RSS item title / GUID / 檔名都不經過這個函式。
    rows = "\n".join(
        f"    <li>EP{n:02d} — {escape(re.sub(rf'^EP{n:02d}\.? +', '', ep['title']))} "
        f'(<a href={quoteattr(base + "/" + ep["media_file"])}>mp3</a>)</li>'
        for n, ep in live_episodes(show)
    )
    return (
        "<!doctype html>\n"
        '<html lang="zh-Hant"><head><meta charset="utf-8">\n'
        f"<title>{escape(show['title'])}</title></head>\n"
        f"<body>\n  <h1>{escape(show['title'])}</h1>\n"
        f"  <p>{escape(show['description'])}</p>\n"
        f'  <p>RSS: <a href={quoteattr(base + "/feed.xml")}>{escape(base)}/feed.xml</a></p>\n'
        f"  <ul>\n{rows}\n  </ul>\n</body></html>\n"
    )
