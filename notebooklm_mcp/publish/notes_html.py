"""把 NotebookLM report/study-guide 的 Markdown 渲染成自包含、手機好讀的 HTML。
純函式、可離線測;inline CSS,無外部資源(才能被 uploader 白名單當單一 .html 檔服務)。"""

from __future__ import annotations

import re
from html import escape
from html.parser import HTMLParser
from urllib.parse import urlsplit
from xml.sax.saxutils import quoteattr

import markdown as _md

# extensions=["extra"] 換成顯式清單:拿掉 attr_list(讓「已逸出的文字」也能被注入屬性,
# 例如 escape 後的 `{: onclick="..."}` 仍會被 attr_list 解析成真屬性)與 md_in_html
# (讓原始 HTML 區塊繞過 markdown 的逸出/區塊處理)。footnotes/def_list/abbr/tables/
# fenced_code 是實測會用到、且輸出標籤都在下方允許清單內的功能,保留。
_EXTENSIONS = ["fenced_code", "footnotes", "def_list", "tables", "abbr", "sane_lists"]

# 這頁對外公開靜態服務,必須「自包含」:不執行 script、不載外部資源。report/notes
# 半信任(來自 NotebookLM chat_ask,可能夾帶 prompt-injection 的 markdown),故渲染後
# 對產出的 HTML 做**標籤層級的允許清單**檢查,而不是逐字串掃全文——舊版
# `\son\w+\s*=`/`javascript:` 這類全文 regex 會把「設定 online=1」「JavaScript:動態
# 語言的起點」這種普通中文技術散文一起誤殺(false positive 會讓整季發不出去),而
# `<script>`/`<img>` 這類標籤全集掃描又漏掉 <svg><image href>、<input type=image src>、
# <table background=…> 等一樣危險的組合。改成:只看「標籤名」與「屬性名」是否在允許
# 清單,href 的 scheme 再逐一驗證。
#
# 用 stdlib `html.parser.HTMLParser`(而非自寫 regex 抓 `<tag attrs>`)是因為它能正確
# 處理兩個 regex 容易漏的邊界:屬性值裡含 `>`(regex 的 `[^>]*` 會在那裡就把標籤切斷,
# 讓後面真正的危險屬性逃出掃描範圍)、以及 entity 編碼的 scheme
# (`jav&#x09;ascript:`)——`convert_charrefs=True` 會先把字元參照解碼回真正字元,
# scheme 檢查才照見它本來的意思,不會被編碼繞過。
_ALLOWED_TAGS = frozenset(
    {
        "p",
        "ul",
        "ol",
        "li",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "a",
        "code",
        "pre",
        "em",
        "strong",
        "blockquote",
        "table",
        "thead",
        "tbody",
        "tr",
        "th",
        "td",
        "hr",
        "br",
        "sup",
        "dl",
        "dt",
        "dd",
        "abbr",
        "del",
        "ins",
        "div",  # footnotes extension 的 <div class="footnote"> 包裹區塊
        # 惰性行內標籤:不執行、不載外部資源,屬性另有逐一過濾,放進來不擴大攻擊面。
        # report 來自 NotebookLM,偶爾夾帶這類行內 HTML 並不罕見,而拒收的形狀是「整季
        # publish_series raise」,錯誤訊息只能叫使用者改寫措辭——誤判成本遠高於收益。
        "b",
        "i",
        "u",
        "s",
        "small",
        "span",
        "sub",
        "details",
        "summary",
    }
)
_ALLOWED_ATTRS = frozenset({"href", "title", "id", "class", "colspan", "rowspan", "start"})
_ALLOWED_HREF_SCHEMES = frozenset({"http", "https", "mailto", ""})  # "" = 相對連結/純 fragment
# tables extension 的對齊語法(`| :--- |`)在 th/td 上輸出這個精確 pattern(實測鎖定;
# 見 tests/test_notes_html.py),放行它、其餘 style 一律拒。
_TABLE_ALIGN_STYLE_RE = re.compile(r"^text-align:\s*(?:left|right|center);?$")


class _TagAllowlistChecker(HTMLParser):
    """走一遍 HTML,標籤名 / 屬性名不在允許清單就記錄第一個違規,不 raise(呼叫端統一
    包成帶「自包含」關鍵詞的 ValueError,契約與既有測試/呼叫端一致)。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.violation: str | None = None

    def _reject(self, detail: str) -> None:
        if self.violation is None:  # 只留第一個違規訊息就夠診斷
            self.violation = detail

    def _check(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag not in _ALLOWED_TAGS:
            self._reject(f"標籤 <{tag}> 不在允許清單")
            return
        for name, value in attrs:
            name = (name or "").lower()
            if name == "style":
                if value is None or not _TABLE_ALIGN_STYLE_RE.match(value.strip()):
                    self._reject(f"<{tag}> 的 style={value!r} 不在允許清單")
                    return
                continue
            if name not in _ALLOWED_ATTRS:
                self._reject(f"<{tag}> 的屬性 {name}= 不在允許清單")
                return
            if name == "href":
                try:
                    scheme = urlsplit(value or "").scheme.lower()
                except ValueError:
                    # 畸形 URL(例如 `http://[abc`)會讓 urlsplit 丟原生 ValueError,訊息不含
                    # 「自包含」——呼叫端與測試都靠那個字判斷「這是內容安全拒收」。解析不了
                    # 本來就該當拒收,統一成同一種錯誤形狀。
                    self._reject(f"<{tag} href> 的 URL 無法解析")
                    return
                if scheme not in _ALLOWED_HREF_SCHEMES:
                    self._reject(f"<{tag} href> 的 scheme {scheme!r} 不在允許清單")
                    return

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._check(tag, attrs)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._check(tag, attrs)

    def _hidden(self, data: str) -> None:
        # HTMLParser 把註解 / 宣告 / CDATA / PI 整段吞成一個回呼,裡面的 `<img>` 永遠不會
        # 觸發 handle_starttag——但瀏覽器對 `<![CDATA[ … ]>` 是「bogus comment 在第一個
        # `>` 就結束」,後面的 `<img>` 會變成真元素(實測 chrome --dump-dom 確認)。所以這些
        # 被 parser 藏起來的區段裡只要含 `<`/`>`,就是想繞過標籤掃描,一律拒。
        if "<" in data or ">" in data:
            self._reject("被解析器隱藏的區段(註解/宣告/CDATA)內含 markup")

    handle_comment = _hidden
    handle_decl = _hidden
    unknown_decl = _hidden
    handle_pi = _hidden


def _assert_self_contained(body: str, kind: str) -> None:
    """report/notes 兩條渲染路徑共用同一顆 guard。錯誤訊息保留「自包含」關鍵詞
    (既有測試 `pytest.raises(ValueError, match="自包含")` 與呼叫端都靠這個字比對)。"""
    checker = _TagAllowlistChecker()
    checker.feed(body)
    checker.close()
    if checker.violation:
        raise ValueError(
            f"{kind} HTML 含不在允許清單內的標籤/屬性,拒絕產出非自包含內容:"
            f"{checker.violation}。若為正常內容誤判,請改寫該行措辭。"
        )


_CSS = """
:root { color-scheme: light dark; }
body { margin: 0; background: #ffffff; color: #1a1a1a;
       font: 17px/1.7 -apple-system, "Noto Sans TC", "PingFang TC", sans-serif; }
main { max-width: 44rem; margin: 0 auto; padding: 2rem 1.2rem 4rem; }
h1 { font-size: 1.8rem; line-height: 1.3; }
h2 { margin-top: 2rem; border-bottom: 1px solid #8883; padding-bottom: .3rem; }
code { background: #8882; padding: .1em .35em; border-radius: 4px; }
pre { background: #8882; padding: 1rem; border-radius: 8px; overflow-x: auto; }
pre code { background: none; padding: 0; }
a { color: #2563eb; }
@media (prefers-color-scheme: dark) {
  body { background: #0f1115; color: #e6e6e6; }
  a { color: #7dd3fc; }
}
"""


def render_episode_notes_html(notes_text: str, attachments: list[tuple[str, str, str]]) -> str:
    """把純文字單集 show notes(`•` 條列 + 空行分段)+ 附件連結,轉成 RSS
    `<content:encoded>` 用的 HTML fragment(`<p>`/`<ul>`/具名 `<a>`)。

    這頁走公開 `<content:encoded>`,notes 半信任(chat_ask 產,可能夾帶 prompt-injection
    的 markdown),故**先逐行逸出**再交 markdown:夾帶的原始 `<script>`/`<img>` 會變
    `&lt;…` 純文字(neutralize,不執行也不載資源)。但逸出只防得住「原始 HTML 標籤」,
    防不住**合法 markdown 語法本身**產出的危險標籤——`![t](https://evil/x.png)`
    全是安全字元(無 `&<>`),逸出後原樣通過,markdown 仍會把它轉成會外連的
    `<img src=…>`;`[t](javascript:...)` 同理會產出 `javascript:` 連結。故渲染後仍要跑
    與 `render_report_html` 同一顆允許清單 guard——guard 只掃 notes 本身
    渲染出的 body,不含後面附加的 attachments(那段是我們自己拼的 `<a href=https://…>`,
    URL 皆為自家 podcast 網域,非使用者可控輸入)。附件以 `quoteattr` 手動組具名
    anchor(URL 全為自家 podcast https、藏 href 不裸露)。`•` 開頭行改 markdown `- ` 起
    `<ul>`;prose→bullet 轉換點補空行否則 markdown 不起清單。"""
    md_lines: list[str] = []
    for line in notes_text.split("\n"):
        stripped = line.lstrip()
        if stripped.startswith("•"):
            # prose 直接接 bullet(前一行非空且非清單)時,markdown 需要一個空行才起 <ul>
            if md_lines and md_lines[-1].strip() and not md_lines[-1].startswith("- "):
                md_lines.append("")
            item = escape(stripped[1:].strip())
            if item:
                md_lines.append("- " + item)
        else:
            md_lines.append(escape(line))
    body = _md.markdown("\n".join(md_lines), extensions=_EXTENSIONS)
    _assert_self_contained(body, "episode notes")
    if attachments:
        body += "\n" + "\n".join(
            f"<p>{escape(emoji)} <a href={quoteattr(url)}>{escape(label)}</a></p>"
            for emoji, label, url in attachments
        )
    return body


def render_report_html(markdown_text: str, title: str) -> str:
    body = _md.markdown(markdown_text, extensions=_EXTENSIONS)
    _assert_self_contained(body, "report")
    return (
        "<!doctype html>\n"
        '<html lang="zh-Hant"><head><meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{escape(title)}</title>\n"
        f"<style>{_CSS}</style></head>\n"
        f"<body><main>\n<h1>{escape(title)}</h1>\n{body}\n</main></body></html>\n"
    )
