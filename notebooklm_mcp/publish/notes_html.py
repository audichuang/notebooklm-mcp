"""把 NotebookLM report/study-guide 的 Markdown 渲染成自包含、手機好讀的 HTML。
純函式、可離線測;inline CSS,無外部資源(才能被 uploader 白名單當單一 .html 檔服務)。"""
from __future__ import annotations

from html import escape

import markdown as _md

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


def render_report_html(markdown_text: str, title: str) -> str:
    body = _md.markdown(markdown_text, extensions=["extra", "sane_lists"])
    return (
        "<!doctype html>\n"
        '<html lang="zh-Hant"><head><meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{escape(title)}</title>\n"
        f"<style>{_CSS}</style></head>\n"
        f"<body><main>\n<h1>{escape(title)}</h1>\n{body}\n</main></body></html>\n"
    )
