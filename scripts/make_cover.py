#!/usr/bin/env python3
"""生成 Apple-Podcast 合規的節目封面(2000x2000 RGB JPEG,無 alpha)。

為什麼有這支:`publish_series` 的封面是使用者自備 or 這裡生。agy 之類的
coding agent **不能直接出點陣圖**(它只會幫你寫這種 PIL code),所以把已驗過的
排版固化成一支 CLI,agent 不必每次重寫、產出也一致。

深色終端機風:左上 mono tag(如 ~/.claude/)+ 大字標題(逐行)+ 副標 + 署名。
標題字級會自動縮到塞得下版面寬度,長短標題都不爆框。

用法:
    uv run python scripts/make_cover.py --output cover.jpg \\
        --line Agentic --line 工程 --line 筆記 \\
        --tag "~/.claude/" \\
        --subtitle "harness × loop · Claude Code 拆解" \\
        --byline audichuang

--line 可重複,每個 --line 是標題的一行。跑完會自己過 artwork 驗證器,不合規回傳非零。
"""
from __future__ import annotations

import argparse
import os
import sys

from PIL import Image, ImageDraw, ImageFont

# 依偏好順序找 CJK / mono 字型;找不到 CJK 就直接報錯(PIL 內建字型畫不出中文)。
_CJK_CANDIDATES = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Black.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/System/Library/Fonts/PingFang.ttc",                      # macOS
    "/usr/share/fonts/truetype/arphic/uming.ttc",
]
_MONO_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
    "/System/Library/Fonts/Menlo.ttc",
]

S = 2000        # 邊長:落在 Apple 的 1400–3000 中段
M = 150         # 版面邊距


def _first_existing(paths: list[str], what: str) -> str:
    for p in paths:
        if os.path.exists(p):
            return p
    raise SystemExit(
        f"找不到 {what} 字型(試過:{paths})。裝一個(Linux: fonts-noto-cjk)"
        " 或改 _CJK_CANDIDATES / _MONO_CANDIDATES。"
    )


def _fit_font(path: str, text: str, max_w: int, start: int) -> ImageFont.FreeTypeFont:
    """把字級從 start 往下縮,直到 text 的寬度塞進 max_w。"""
    size = start
    while size > 24:
        font = ImageFont.truetype(path, size, index=0)
        if font.getlength(text) <= max_w:
            return font
        size -= 8
    return ImageFont.truetype(path, 24, index=0)


def make_cover(output: str, lines: list[str], tag: str, subtitle: str, byline: str) -> None:
    cjk = _first_existing(_CJK_CANDIDATES, "CJK")
    mono = _first_existing(_MONO_CANDIDATES, "mono")
    os.makedirs(os.path.dirname(os.path.abspath(output)), exist_ok=True)

    # 垂直漸層底 + 右上柔光球:低調的科技感,縮圖也吃得住。
    img = Image.new("RGB", (S, S), "#0b1220")
    d = ImageDraw.Draw(img)
    top, bot = (11, 18, 32), (23, 30, 54)
    for y in range(S):
        t = y / S
        d.line([(0, y), (S, y)], fill=tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(3)))
    glow = Image.new("RGB", (S, S), "#0b1220")
    ImageDraw.Draw(glow).ellipse([S * 0.55, -S * 0.15, S * 1.25, S * 0.55], fill="#1e3a8a")
    img = Image.blend(img, glow, 0.28)
    d = ImageDraw.Draw(img)

    max_w = S - 2 * M
    f_tag = ImageFont.truetype(mono, 70)
    f_sub = ImageFont.truetype(cjk, 88, index=0)
    f_by = ImageFont.truetype(cjk, 72, index=0)
    # 標題字級以「最長那行」為準自動縮放,確保每行都不超寬。
    longest = max(lines, key=lambda s: len(s)) if lines else ""
    f_title = _fit_font(cjk, longest, max_w, start=300)
    line_h = int(f_title.size * 1.1)

    if tag:
        d.text((M, 150), tag, font=f_tag, fill="#7dd3fc")
    d.rectangle([M, 300, M + 180, 322], fill="#38bdf8")     # accent bar
    y = 470
    for ln in lines:
        d.text((M, y), ln, font=f_title, fill="#f8fafc")
        y += line_h
    if subtitle:
        d.text((M, y + 20), subtitle, font=f_sub, fill="#94a3b8")
    if byline:
        d.text((M, S - 200), byline, font=f_by, fill="#64748b")
    d.rectangle([40, 40, S - 40, S - 40], outline="#1e293b", width=6)

    img.convert("RGB").resize((S, S), Image.LANCZOS).save(output, "JPEG", quality=92)


def main() -> None:
    ap = argparse.ArgumentParser(description="生成 Apple-Podcast 合規節目封面")
    ap.add_argument("--output", required=True, help="輸出路徑(.jpg)")
    ap.add_argument("--line", action="append", default=[], help="標題的一行,可重複")
    ap.add_argument("--tag", default="", help="左上 mono 標籤,如 ~/.claude/")
    ap.add_argument("--subtitle", default="", help="標題下方副標")
    ap.add_argument("--byline", default="", help="左下署名")
    args = ap.parse_args()
    if not args.line:
        ap.error("至少要一個 --line 當標題")

    make_cover(args.output, args.line, args.tag, args.subtitle, args.byline)

    # 自我驗證:直接用 publish 端同一個驗證器,產出即保證能過 publish_series。
    from notebooklm_mcp.publish.artwork import validate_artwork

    info = validate_artwork(args.output)
    print(f"OK {args.output} -> {info}")


if __name__ == "__main__":
    sys.exit(main())
