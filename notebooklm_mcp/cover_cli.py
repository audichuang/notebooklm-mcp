#!/usr/bin/env python3
"""生成 Apple-Podcast 合規封面(3000x3000 RGB JPEG,無 alpha)。

設計固化成 HTML template(`assets/cover_episode.html` / `assets/cover_show.html`,
由 agy/Gemini 設計),本工具只做「填佔位符 → headless Chrome 光柵化 → JPEG →
過 artwork 驗證器」。要換設計就重生 template(見 docs/superpowers 的封面流程),程式不用動。

為什麼不再用 PIL 手繪:coding agent 出不了漂亮的點陣排版,只能刻死座標;改成讓 agy
產出自包含 HTML(它擅長 markup/CSS),我方光柵化,設計質感高一個檔次且改版只改 template。

決定性:同 template + 同輸入 + 同 Chrome/CJK 字型 → 同 bytes(發布端拿封面 bytes 做
content-hash)。換 Chrome 或字型版本可能改 bytes → 重發時該集封面 URL 變動(等同舊 PIL
的字型 caveat);所以「產封面」固定在同一台機器跑,產出的 JPEG 即事實來源(可版控)。

需求:系統要有 headless Chrome(google-chrome / chromium)。只有「產封面的那台」需要,
3 個靠 Doppler 認證的 VM 不需要。可用 NOTEBOOKLM_COVER_CHROME 或 --chrome 指定 binary。

用法:
    # 整季單集封面(讀 manifest,逐集填集號決定色 + 寫回 cover_path)
    notebooklm-cover --manifest series_manifest.json --show-name Audicast --byline audichuang

    # 節目封面(show 層,品牌)
    notebooklm-cover --show --output assets/cover.jpg --show-name Audicast \\
        --tagline "AI 協作工程・每集拆解" --byline audichuang

    # 單集一次性
    notebooklm-cover --output ep05.jpg --show-name Audicast --episode EP05 \\
        --title "本集標題" --byline audichuang
"""
from __future__ import annotations

import argparse
import html
import os
import shutil
import subprocess
import sys
import tempfile

from PIL import Image

from notebooklm_mcp._atomic import prepared_replacement
from notebooklm_mcp.publish.artwork import validate_artwork
from notebooklm_mcp.manifest_store import ManifestStore
from notebooklm_mcp.naming import bare_episode_title

_ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
_CHROME_CANDIDATES = ["google-chrome", "google-chrome-stable", "chromium",
                      "chromium-browser", "chrome"]
S = 3000        # 邊長:Apple 上限,縮圖與大圖都最清晰
_SHOW_HUE = 265  # 節目封面品牌簽名色(色相),可 --hue 覆寫


def _episode_hue(n: int) -> int:
    """集號決定色相(決定性鐵律:同一集永遠同色,發布端 content-hash 才穩)。"""
    return (n * 77) % 360


def _find_chrome(explicit: str | None = None) -> str:
    """找 headless Chrome。**呼叫點刻意在每個 render 之前,不在 main 開頭**——
    `--skip-existing` 而且每張封面都驗得過時,這一輪根本不需要 Chrome,不該因為那台機器
    沒裝而失敗。`shutil.which` 很便宜,不值得為它做 memo。"""
    cand = explicit or os.environ.get("NOTEBOOKLM_COVER_CHROME")
    if cand:
        if os.path.exists(cand) or shutil.which(cand):
            return cand
        raise SystemExit(f"指定的 Chrome 找不到:{cand!r}")
    for c in _CHROME_CANDIDATES:
        p = shutil.which(c)
        if p:
            return p
    raise SystemExit(
        "找不到 headless Chrome。裝 google-chrome / chromium,或設 "
        "NOTEBOOKLM_COVER_CHROME / --chrome 指向 binary。"
    )


def _load_template(name: str) -> str:
    path = os.path.join(_ASSETS, name)
    if not os.path.exists(path):
        raise SystemExit(f"找不到封面 template:{path}")
    with open(path, encoding="utf-8") as f:
        return f.read()


def _render(template: str, subs: dict, output: str, chrome: str) -> dict:
    """填佔位符 → Chrome 光柵化 3000² PNG → RGB JPEG → 過 validate_artwork。
    __HUE__ 是數值不轉義;其餘值 HTML-escape(標題可能含 & < >)。"""
    doc = template
    for key, val in subs.items():
        doc = doc.replace(key, str(val) if key == "__HUE__" else html.escape(str(val)))
    os.makedirs(os.path.dirname(os.path.abspath(output)), exist_ok=True)
    # 原子換檔走 `_atomic` 那一份(gotchas-files 的紅線:不准再自己寫一份)。mode 不指定
    # → 沿用既有封面的 mode,首次生成用 `_NEW_FILE_MODE`(0644;封面要讓發布端讀得到)。
    with prepared_replacement(output) as temporary_output:
        with tempfile.TemporaryDirectory() as td:
            hpath = os.path.join(td, "cover.html")
            png = os.path.join(td, "cover.png")
            with open(hpath, "w", encoding="utf-8") as f:
                f.write(doc)
            r = subprocess.run(
                [chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
                 "--hide-scrollbars", "--force-device-scale-factor=1",
                 f"--window-size={S},{S}", f"--screenshot={png}",
                 f"file://{os.path.abspath(hpath)}"],   # 必須絕對路徑,否則 Chrome 當 host → ERR_INVALID_URL
                capture_output=True, text=True, timeout=180)
            if not os.path.exists(png):
                raise SystemExit(f"Chrome 光柵化失敗 (exit={r.returncode}):{r.stderr[-400:]}")
            with Image.open(png) as image:
                image.convert("RGB").save(temporary_output, "JPEG", quality=92)
        info = validate_artwork(temporary_output)   # 驗過才 commit(replace 在離開時)
    return info


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




def main() -> None:
    ap = argparse.ArgumentParser(
        description="生成 Apple-Podcast 合規封面(HTML template + headless Chrome)")
    ap.add_argument("--manifest", default=None,
                    help="JSON manifest:批次為每集生單集封面並寫回 cover_path")
    ap.add_argument("--show", action="store_true", help="產節目(show 層)品牌封面")
    ap.add_argument("--output", default=None, help="輸出路徑(.jpg);單集/節目模式必填")
    ap.add_argument("--output-dir", default=None, help="批次模式輸出目錄")
    ap.add_argument("--show-name", default="Audicast", help="節目名(封面 wordmark)")
    ap.add_argument("--title", default="", help="單集標題(單集一次性模式)")
    ap.add_argument("--tagline", default="", help="節目封面副標(--show 模式)")
    ap.add_argument("--byline", default="", help="署名")
    ap.add_argument("--episode", default=None, help="集號如 EP05(單集一次性模式)")
    ap.add_argument("--skip-existing", action="store_true",
                    help="批次模式:目標封面檔已存在就跳過 render(只生新集,仍寫回 cover_path)")
    ap.add_argument("--hue", type=int, default=None,
                    help="覆寫色相 0-360(預設:單集用集號決定、節目用品牌色)")
    ap.add_argument("--chrome", default=None,
                    help="Chrome binary(否則自動找 / 用 NOTEBOOKLM_COVER_CHROME)")
    args = ap.parse_args()

    # 1) 批次:整季單集封面
    if args.manifest:
        store = ManifestStore(args.manifest)
        try:
            manifest = store.read()
        except ValueError as error:
            # ManifestStore 會比 cover 專用 preflight 更早擋 schema；CLI 仍維持
            # argparse exit 2 + 既有中文提示，不把資料錯誤洩成 traceback。
            message = str(error)
            if "episode must be an integer" in message:
                message = "episode 必須是 1..99 的整數"
            elif "duplicate episode" in message:
                message = "重複的 episode 集號"
            ap.error(message)
        episodes = manifest.get("episodes", [])
        try:
            _preflight_episodes(episodes)   # 壞資料 fail-fast,絕不半途覆寫 manifest
        except ValueError as e:
            ap.error(str(e))

        show_name = args.show_name or manifest.get("title") or "Audicast"
        out_dir = args.output_dir or os.path.dirname(os.path.abspath(args.manifest))
        os.makedirs(out_dir, exist_ok=True)
        tpl = _load_template("cover_episode.html")
        cover_updates: dict[int, tuple[str, str]] = {}
        for ep in episodes:
            n = int(ep["episode"])                      # 已過 preflight,保證 int
            cover_path = os.path.abspath(os.path.join(out_dir, f"EP{n:02d}.jpg"))
            if args.skip_existing and os.path.exists(cover_path):
                # `--skip-existing` 的語意是「已經做完的跳過」,而**壞檔就是沒做完**
                # ——截斷的 JPEG 會一路帶到 publish 才爆(Apple 端拒收)。所以只把
                # 「驗得過」當成做完;壞檔 fall through 重畫,不預先刪它:_render 是
                # 驗證後才 atomic replace,render 失敗時舊檔仍在(手上至少還有東西)。
                try:
                    validate_artwork(cover_path)
                except ValueError as invalid:
                    print(f"REBUILD {cover_path}({invalid})")
                else:
                    cover_updates[n] = (ep["title"], cover_path)
                    print(f"SKIP {cover_path} (exists)")
                    continue
            hue = args.hue if args.hue is not None else _episode_hue(n)
            info = _render(tpl, {
                "__SHOW__": show_name,
                "__EPNUM__": f"{n:02d}",
                "__TITLE__": bare_episode_title(n, ep["title"]),
                "__BYLINE__": args.byline,
                "__HUE__": hue,
            }, cover_path, _find_chrome(args.chrome))
            print(f"OK {cover_path} -> {info}")
            cover_updates[n] = (ep["title"], cover_path)

        def attach_cover_paths(latest):
            for episode_n, (expected_title, cover_path) in cover_updates.items():
                current = next(
                    (
                        item for item in latest["episodes"]
                        if item.get("episode") == episode_n
                    ),
                    None,
                )
                if current is None or current.get("title") != expected_title:
                    raise ValueError(
                        f"episode {episode_n} changed while rendering covers; retry batch"
                    )
                current["cover_path"] = cover_path

        store.update(attach_cover_paths)
        print(f"Manifest updated: {args.manifest}")
        return

    # 2) 節目(show 層)品牌封面
    if args.show:
        if not args.output:
            ap.error("--show 模式需要 --output")
        hue = args.hue if args.hue is not None else _SHOW_HUE
        info = _render(_load_template("cover_show.html"), {
            "__SHOW__": args.show_name,
            "__TAGLINE__": args.tagline,
            "__BYLINE__": args.byline,
            "__HUE__": hue,
        }, args.output, _find_chrome(args.chrome))
        print(f"OK {args.output} -> {info}")
        return

    # 3) 單集一次性
    if not (args.output and args.episode and args.title):
        ap.error("單集模式需要 --output --episode --title(或改用 --manifest / --show)")
    import re

    m = re.search(r"\d+", args.episode)
    n = int(m.group(0)) if m else 1
    hue = args.hue if args.hue is not None else _episode_hue(n)
    info = _render(_load_template("cover_episode.html"), {
        "__SHOW__": args.show_name,
        "__EPNUM__": f"{n:02d}",
        "__TITLE__": bare_episode_title(n, args.title),
        "__BYLINE__": args.byline,
        "__HUE__": hue,
    }, args.output, _find_chrome(args.chrome))
    print(f"OK {args.output} -> {info}")


if __name__ == "__main__":
    sys.exit(main())
