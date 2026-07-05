#!/usr/bin/env python3
"""生成 Apple-Podcast 合規的節目封面(2000x2000 RGB JPEG,無 alpha)。

為什麼有這支:`publish_series` 的封面是使用者自備 or 這裡生。agy 之類的
coding agent **不能直接出點陣圖**(它只會幫你寫這種 PIL code),所以把已驗過的
排版固化成一支 CLI,agent 不必每次重寫、產出也一致。

深色終端機風:左上 mono tag(如 ~/.claude/)+ 大字標題(逐行)+ 副標 + 署名。
標題字級會自動縮到塞得下版面寬度,長短標題都不爆框。

用法:
    notebooklm-cover --output cover.jpg \\
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


def make_cover(output: str, lines: list[str], tag: str, subtitle: str, byline: str,
               episode: str | None = None, font_cjk: str | None = None,
               font_mono: str | None = None) -> None:
    import colorsys
    import hashlib
    import re

    # font_cjk/font_mono 可 pin 明確字型路徑:輸出 bytes 是 feed 的 content-hash 依據,
    # 各 VM 的 _first_existing 若挑到不同字型會產生不同 bytes → feed churn。要跨機器
    # 決定性,發布前在各機傳同一個明確字型路徑(或確保候選清單首選在各機一致)。
    cjk = font_cjk or _first_existing(_CJK_CANDIDATES, "CJK")
    mono = font_mono or _first_existing(_MONO_CANDIDATES, "mono")
    os.makedirs(os.path.dirname(os.path.abspath(output)), exist_ok=True)

    # 決定配色
    if episode is None:
        # 完全維持原版常數，100% 向後相容
        top_color = (11, 18, 32)
        bot_color = (23, 30, 54)
        glow_fill = "#1e3a8a"
        accent_fill = "#38bdf8"
        tag_fill = "#7dd3fc"
        sub_fill = "#94a3b8"
        by_fill = "#64748b"
        border_outline = "#1e293b"
    else:
        # 決定 base_hue (基底色相)
        match = re.search(r'\d+', episode)
        if match:
            n = int(match.group(0))
            base_hue = (n * 77) % 360
        else:
            h = hashlib.sha256(episode.encode("utf-8")).hexdigest()
            base_hue = int(h, 16) % 360

        def hls_to_hex(h_deg: float, l: float, s: float) -> str:
            r, g, b = colorsys.hls_to_rgb((h_deg % 360) / 360.0, l, s)
            return f"#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}"
        
        def hls_to_rgb255(h_deg: float, l: float, s: float) -> tuple[int, int, int]:
            r, g, b = colorsys.hls_to_rgb((h_deg % 360) / 360.0, l, s)
            return (int(r * 255), int(g * 255), int(b * 255))
            
        top_color = hls_to_rgb255(base_hue, 0.08, 0.49)
        bot_color = hls_to_rgb255(base_hue + 6, 0.15, 0.40)
        glow_fill = hls_to_hex(base_hue + 4, 0.33, 0.64)
        accent_fill = hls_to_hex(base_hue - 21, 0.60, 0.94)
        tag_fill = hls_to_hex(base_hue - 20, 0.74, 0.95)
        sub_fill = hls_to_hex(base_hue - 7, 0.65, 0.20)
        by_fill = hls_to_hex(base_hue - 5, 0.47, 0.16)
        border_outline = hls_to_hex(base_hue - 3, 0.17, 0.33)

    # 垂直漸層底 + 右上柔光球:低調的科技感,縮圖也吃得住。
    img = Image.new("RGB", (S, S), top_color)
    d = ImageDraw.Draw(img)
    for y in range(S):
        t = y / S
        d.line([(0, y), (S, y)], fill=tuple(int(top_color[i] + (bot_color[i] - top_color[i]) * t) for i in range(3)))
    glow = Image.new("RGB", (S, S), top_color)
    ImageDraw.Draw(glow).ellipse([S * 0.55, -S * 0.15, S * 1.25, S * 0.55], fill=glow_fill)
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
        d.text((M, 150), tag, font=f_tag, fill=tag_fill)
        badge_x = M + int(f_tag.getlength(tag)) + 30
    else:
        badge_x = M

    if episode:
        match = re.search(r'\d+', episode)
        n = int(match.group(0)) if match else 1
        badge_text = f"EP{n:02d}"
        f_badge = ImageFont.truetype(mono, 50)
        badge_w = int(f_badge.getlength(badge_text))
        px, py = 18, 6
        bx1 = badge_x
        by1 = 150 + 10 - py
        by2 = by1 + 50 + 2 * py
        bx2 = bx1 + badge_w + 2 * px
        try:
            d.rounded_rectangle([bx1, by1, bx2, by2], radius=8, fill=accent_fill)
        except AttributeError:
            d.rectangle([bx1, by1, bx2, by2], fill=accent_fill)
        d.text((bx1 + px, by1 + py - 2), badge_text, font=f_badge, fill=top_color)

    d.rectangle([M, 300, M + 180, 322], fill=accent_fill)     # accent bar
    y = 470
    for ln in lines:
        d.text((M, y), ln, font=f_title, fill="#f8fafc")
        y += line_h
    if subtitle:
        d.text((M, y + 20), subtitle, font=f_sub, fill=sub_fill)
    if byline:
        d.text((M, S - 200), byline, font=f_by, fill=by_fill)
    d.rectangle([40, 40, S - 40, S - 40], outline=border_outline, width=6)

    img.convert("RGB").resize((S, S), Image.LANCZOS).save(output, "JPEG", quality=92)


def _preflight_episodes(episodes: list) -> None:
    """批次模式前先驗整份 episodes,壞資料 fail-fast(而非靜默 continue 漏集、
    或 string episode 撞上 `:02d` 崩潰)。契約對齊 publish_series:episode 為 int 1..99、
    title 非空、集號不重複。"""
    if not episodes:
        raise ValueError("manifest 沒有 episodes")
    seen: set[int] = set()
    for ep in episodes:
        n = ep.get("episode")
        if not isinstance(n, int) or isinstance(n, bool) or not (1 <= n <= 99):
            raise ValueError(f"episode 必須是 1..99 的整數,got: {n!r}")
        if n in seen:
            raise ValueError(f"重複的 episode 集號: {n}")
        seen.add(n)
        t = ep.get("title")
        if not isinstance(t, str) or not t.strip():
            raise ValueError(f"episode {n}: title 必填且非空")


def _atomic_write_json(path: str, data: object) -> None:
    """同目錄 temp + fsync + os.replace 原子覆寫,避免中斷把 manifest 截斷。"""
    import json
    import tempfile

    d = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main() -> None:
    ap = argparse.ArgumentParser(description="生成 Apple-Podcast 合規節目封面")
    ap.add_argument("--output", help="輸出路徑(.jpg)")
    ap.add_argument("--line", action="append", default=[], help="標題的一行,可重複")
    ap.add_argument("--tag", default="", help="左上 mono 標籤,如 ~/.claude/")
    ap.add_argument("--subtitle", default="", help="標題下方副標")
    ap.add_argument("--byline", default="", help="左下署名")
    ap.add_argument("--episode", default=None, help="集數識別(如 EP01),啟用每集視覺差異化配色")
    ap.add_argument("--manifest", default=None, help="JSON manifest 路徑;批次為每集生單集封面並寫回 cover_path")
    ap.add_argument("--show-name", default=None, help="批次模式的節目名(單集封面副標);未給則用 manifest 的 title")
    ap.add_argument("--output-dir", default=None, help="批次生成封面時的輸出目錄")
    ap.add_argument("--font-cjk", default=None, help="pin CJK 字型路徑(跨機器決定性用)")
    ap.add_argument("--font-mono", default=None, help="pin mono 字型路徑(跨機器決定性用)")
    args = ap.parse_args()

    # 檢查必填參數，維持向後相容
    if not args.manifest:
        if not args.output:
            ap.error("在未指定 --manifest 時，必須指定 --output")
        if not args.line:
            ap.error("在未指定 --manifest 時，至少要一個 --line 當標題")

    from notebooklm_mcp.publish.artwork import validate_artwork

    if args.manifest:
        import json

        with open(args.manifest, encoding="utf-8") as f:
            manifest = json.load(f)
        episodes = manifest.get("episodes", [])

        # 先驗整份(壞資料 fail-fast,絕不動 manifest / 不半途覆寫)
        try:
            _preflight_episodes(episodes)
        except ValueError as e:
            ap.error(str(e))

        program_title = args.show_name or manifest.get("title") or " ".join(args.line) or "Agentic 工程 筆記"
        out_dir = args.output_dir or os.path.dirname(os.path.abspath(args.manifest))
        os.makedirs(out_dir, exist_ok=True)

        for ep_item in episodes:
            ep_num = int(ep_item["episode"])            # 已過 preflight,保證 int
            cover_path = os.path.abspath(os.path.join(out_dir, f"EP{ep_num:02d}.jpg"))
            make_cover(
                output=cover_path,
                lines=[ep_item["title"]],
                tag=args.tag,
                subtitle=program_title,
                byline=args.byline,
                episode=str(ep_num),
                font_cjk=args.font_cjk,
                font_mono=args.font_mono,
            )
            info = validate_artwork(cover_path)
            print(f"OK {cover_path} -> {info}")
            ep_item["cover_path"] = cover_path          # 寫回絕對路徑

        _atomic_write_json(args.manifest, manifest)     # 原子覆寫,避免截斷
        print(f"Manifest updated: {args.manifest}")
    else:
        make_cover(args.output, args.line, args.tag, args.subtitle, args.byline,
                   args.episode, font_cjk=args.font_cjk, font_mono=args.font_mono)
        info = validate_artwork(args.output)
        print(f"OK {args.output} -> {info}")


if __name__ == "__main__":
    sys.exit(main())
