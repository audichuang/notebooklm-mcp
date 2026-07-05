"""把 NotebookLM report/study-guide 的 Markdown 渲染成自包含、手機好讀的 HTML。
純函式、可離線測;inline CSS,無外部資源(才能被 uploader 白名單當單一 .html 檔服務)。"""
from __future__ import annotations

import re
from html import escape

import markdown as _md

# 這頁對外公開靜態服務,必須「自包含」:不執行 script、不載外部資源。report 來自
# NotebookLM(半信任),markdown 會讓原始 HTML 標籤與 ![](http) 圖片穿透,故渲染後
# 掃描危險/外部資源標記,命中就 fail-closed(raise)——寧可發布中止也不上架不安全頁面。
# 只比對「未逸出」的原始標籤;code 區塊裡的 <script> 會被 markdown 逸出成 &lt;script,
# 不會誤觸(那是程式碼展示,安全)。連結 <a href=http> 是導覽不是載入資源,允許。
_UNSAFE_RE = re.compile(
    r"(?i)<\s*(?:script|iframe|object|embed|img|link|audio|video|source|style|meta|base)\b"
    r"|\son\w+\s*=|javascript:|data:text/html"
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

    複用 markdown 渲染:`•` 開頭行改成 `- ` 讓它出 `<ul>`;附件以 markdown 連結
    `emoji [label](url)` 附在末尾 → 具名可點連結,不再裸露長 URL。內容全為本站
    自產(notes + 自家 podcast URL),不含外部資源。"""
    md_lines = []
    for line in notes_text.split("\n"):
        stripped = line.lstrip()
        if stripped.startswith("•"):
            md_lines.append("- " + stripped[1:].strip())
        else:
            md_lines.append(line)
    md = "\n".join(md_lines)
    if attachments:
        md += "\n\n" + "\n\n".join(f"{emoji} [{label}]({url})" for emoji, label, url in attachments)
    return _md.markdown(md, extensions=["extra", "sane_lists"])


def render_report_html(markdown_text: str, title: str) -> str:
    body = _md.markdown(markdown_text, extensions=["extra", "sane_lists"])
    if _UNSAFE_RE.search(body):
        raise ValueError(
            "report HTML 含 script / 外部資源標記,拒絕產出非自包含頁面"
            "(來源 report 疑似夾帶原始 HTML 或 ![](http) 圖片)"
        )
    return (
        "<!doctype html>\n"
        '<html lang="zh-Hant"><head><meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{escape(title)}</title>\n"
        f"<style>{_CSS}</style></head>\n"
        f"<body><main>\n<h1>{escape(title)}</h1>\n{body}\n</main></body></html>\n"
    )
