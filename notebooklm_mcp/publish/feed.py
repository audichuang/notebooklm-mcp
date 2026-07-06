"""Render the authoritative show state into an RSS 2.0 feed (with the iTunes +
atom namespaces Apple Podcasts needs) and a minimal index.html for <link>.
Pure function of (show, base_url): no I/O, no clock — fully deterministic."""
from __future__ import annotations

from xml.sax.saxutils import escape, quoteattr

from .rss_models import live_episodes

_ITUNES = "http://www.itunes.com/dtds/podcast-1.0.dtd"
_CONTENT = "http://purl.org/rss/1.0/modules/content/"
_ATOM = "http://www.w3.org/2005/Atom"


def _feed_dir_url(base_url: str, token: str) -> str:
    return f"{base_url.rstrip('/')}/feeds/{token}"


def build_feed_xml(show: dict, base_url: str) -> str:
    token = show["token"]
    base = _feed_dir_url(base_url, token)
    feed_url = f"{base}/feed.xml"
    explicit = "true" if show.get("explicit") else "false"
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
    rows = "\n".join(
        f"    <li>EP{n:02d} — {escape(ep['title'])} "
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
