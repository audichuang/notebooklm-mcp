#!/usr/bin/env python3
"""
episodic_podcast.py — 連續性 Podcast 自動化生成腳本（序列回饋法）

核心流程：生成 → 下載 → 上傳回筆記本 → 生成下一集
每集完成後將音檔上傳回筆記本，NotebookLM 自動轉錄為逐字稿，
使下一集的 AI 擁有前集的完整對話記憶。

用法:
  python scripts/episodic_podcast.py --config series_config.yaml [OPTIONS]

選項:
  --config PATH      配置檔路徑 (必須)
  --episodes N       只生成前 N 集 (預設: 全部)
  --start N          從第 N 集開始 (預設: 1, 用於續製)
  --dry-run          預覽指令，不實際執行
  --output-dir PATH  輸出目錄 (預設: /tmp/notebooklm/series/<show_name>)
  --distill          額外生成知識蒸餾摘要 (預設關閉)
  --skip-wait        不等待生成完成 (僅觸發生成)

前置條件:
  - pip install notebooklm-py pyyaml
  - doppler 已設定 (doppler run -p notebooklm -c dev)
  - notebooklm 已認證
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("❌ 缺少 pyyaml，請執行: pip install pyyaml")
    sys.exit(1)

# ── 常數 ───────────────────────────────────────────────
DOPPLER_PREFIX = ["doppler", "run", "-p", "notebooklm", "-c", "dev", "--"]
CMD_BASE = DOPPLER_PREFIX + ["notebooklm"]
DEFAULT_TIMEOUT = 600
MAX_WAIT_RETRIES = 3


# ── CLI 輔助 ───────────────────────────────────────────
def run_cmd(args: list[str], *, dry_run: bool = False, capture: bool = True) -> str:
    """執行 CLI 命令，回傳 stdout。"""
    cmd_str = " ".join(args)
    if dry_run:
        print(f"  [DRY-RUN] {cmd_str}")
        return ""
    print(f"  $ {cmd_str}")
    result = subprocess.run(args, capture_output=capture, text=True, timeout=900)
    if result.returncode != 0:
        print(f"  ⚠️  stderr: {result.stderr.strip()}")
    output = result.stdout.strip() if capture else ""
    if output:
        lines = output.split("\n")
        for line in lines[:5]:
            print(f"     {line}")
        if len(lines) > 5:
            print(f"     ... ({len(lines) - 5} more lines)")
    return output


def nbm(*args, notebook_id: str = "", json_output: bool = False, dry_run: bool = False) -> str:
    """notebooklm CLI 封裝。"""
    cmd = CMD_BASE + list(args)
    if notebook_id:
        cmd += ["-n", notebook_id]
    if json_output:
        cmd += ["--json"]
    return run_cmd(cmd, dry_run=dry_run)


def parse_id(output: str) -> str:
    """從 JSON 或純文字中解析 ID（支援巢狀結構）。"""
    try:
        data = json.loads(output)
        # 直接欄位
        for key in ("task_id", "artifact_id", "source_id", "id"):
            if data.get(key):
                return data[key]
        # 巢狀結構（如 {"notebook": {"id": "..."}}）
        for value in data.values():
            if isinstance(value, dict) and value.get("id"):
                return value["id"]
        return ""
    except (json.JSONDecodeError, KeyError, TypeError):
        match = re.search(r"[a-f0-9\-]{20,}", output)
        return match.group(0) if match else ""


# ── 配置解析 ──────────────────────────────────────────
def load_config(path: str) -> dict:
    """載入 YAML 配置檔。"""
    with open(path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    if not config.get("episodes"):
        print("❌ 配置檔中沒有 episodes")
        sys.exit(1)
    # 驗證每集必須有 artifact_title
    for i, ep in enumerate(config["episodes"], start=1):
        if not ep.get("artifact_title"):
            print(f"❌ 第 {i} 集缺少 artifact_title 欄位（每集必須指定 artifact_title 供重命名用）")
            sys.exit(1)
    return config


def resolve_source_path(path: str, config_dir: Path) -> str:
    """解析來源路徑（相對於配置檔目錄）。"""
    p = Path(path)
    if p.is_absolute():
        return str(p)
    return str((config_dir / p).resolve())


# ── 提示詞組裝 ─────────────────────────────────────────
def build_identity_prompt(series: dict) -> str:
    """組裝身份綁定提示詞。"""
    return (
        f'This is the podcast called "{series["name"]}".\n'
        f'The male host is "{series["hosts"]["host_a"]}" '
        f'and the female host is "{series["hosts"]["host_b"]}".\n'
        f"At the very beginning, announce the show name and introduce yourselves.\n\n"
        f'Style: {series.get("style", "informative and engaging")}.\n'
        f"Do NOT use filler words (like, um, ah). Do NOT interrupt each other."
    )


def build_sequel_prompt(series: dict, episode_num: int, prev_audio_title: str) -> str:
    """組裝續集銜接提示詞（引用前集音檔逐字稿）。"""
    return (
        f'This is Episode {episode_num} of "{series["name"]}".\n'
        f'IMPORTANT: First read the source titled "{prev_audio_title}" — '
        f"it contains the FULL transcript of your conversation from the previous episode.\n\n"
        f"Opening (30-45 seconds):\n"
        f'1. Welcome listeners back with "歡迎回來" energy.\n'
        f"2. Recap 2-3 specific points you discussed in the previous episode "
        f"(reference actual things you said, not generic summaries).\n"
        f"3. Tease this episode's new topic.\n\n"
        f"Do NOT re-introduce basic background already covered in previous episodes."
    )


STRATEGY_PROMPTS = {
    "contradiction": (
        "Identify the BIGGEST contradiction between the sources discussed. "
        "{host_a} defends one side, {host_b} defends the other. "
        "Both MUST directly quote original text as evidence. "
        "End by noting unresolved points for the next episode."
    ),
    "source_gap": (
        "Act as rigorous data auditors. Identify: "
        "1) Unverified assumptions presented as facts, "
        "2) Information gaps the authors avoided, "
        "3) Logical leaps lacking evidence. "
        "Cite direct quotes as proof. Frame gaps as questions for the next season."
    ),
    "dialectical": (
        'Assume the author is an "unreliable narrator" with hidden biases. '
        "{host_a}: Present the argument faithfully. "
        "{host_b}: Systematically deconstruct it, finding hidden contradictions. "
        "Both cite specific passages. Conclude by asking what the text would look like "
        "if written by someone with the opposite bias."
    ),
    "verbatim": (
        "Before ANY discussion, one host must read the designated section "
        "VERBATIM, word for word. After reading, the other host says: "
        '"Thank you. Now let\'s break this down." Then proceed with analysis.'
    ),
}


def build_episode_prompt(
    series: dict,
    episode: dict,
    episode_num: int,
    prev_audio_title: str | None,
) -> str:
    """組裝完整的單集提示詞。"""
    parts = []

    # 1. 身份綁定（每集都有）
    parts.append(build_identity_prompt(series))

    # 2. 續集銜接（第 2 集起，引用前集音檔逐字稿）
    if prev_audio_title and episode_num > 1:
        parts.append(build_sequel_prompt(series, episode_num, prev_audio_title))

    # 3. 本集自訂指令
    custom = episode.get("custom_prompt", "").strip()
    if custom:
        custom = custom.replace("{host_a}", series["hosts"]["host_a"])
        custom = custom.replace("{host_b}", series["hosts"]["host_b"])
        custom = custom.replace("{episode_number}", str(episode_num))
        parts.append(custom)

    # 4. 策略提示詞
    strategy = episode.get("strategy")
    if strategy and strategy in STRATEGY_PROMPTS:
        s = STRATEGY_PROMPTS[strategy]
        s = s.replace("{host_a}", series["hosts"]["host_a"])
        s = s.replace("{host_b}", series["hosts"]["host_b"])
        parts.append(s)

    return "\n\n".join(parts)


# ── 核心流程 ──────────────────────────────────────────
def create_or_get_notebook(series: dict, dry_run: bool) -> str:
    """建立或取得筆記本 ID。"""
    notebook_id = series.get("notebook_id")
    if notebook_id:
        print(f"📓 使用現有筆記本: {notebook_id}")
        return notebook_id

    print(f'📓 建立新筆記本: {series["name"]}')
    output = nbm("create", series["name"], json_output=True, dry_run=dry_run)
    if dry_run:
        return "DRY_RUN_NOTEBOOK_ID"

    notebook_id = parse_id(output)
    if notebook_id:
        print(f"   ID: {notebook_id}")
        return notebook_id
    print(f"⚠️  無法解析筆記本 ID，原始輸出:\n{output}")
    sys.exit(1)


def upload_sources(
    episode: dict, notebook_id: str, config_dir: Path, dry_run: bool
) -> list[str]:
    """上傳本集來源，回傳 source IDs。"""
    source_ids = []
    for src in episode.get("sources", []):
        path = resolve_source_path(src["path"], config_dir)
        title = src.get("title", "")
        print(f"   📄 上傳來源: {path}")
        args = ["source", "add", path]
        if title:
            args += ["--title", title]
        output = nbm(*args, notebook_id=notebook_id, json_output=True, dry_run=dry_run)
        sid = parse_id(output) if not dry_run else ""
        if sid:
            source_ids.append(sid)
    return source_ids


def generate_audio(
    description: str,
    series: dict,
    notebook_id: str,
    dry_run: bool,
) -> str:
    """生成音頻，回傳 task-id。"""
    args = ["generate", "audio", description]
    fmt = series.get("format", "deep-dive")
    length = series.get("length", "default")
    lang = series.get("language", "en")
    args += ["--format", fmt, "--length", length, "--language", lang]

    output = nbm(*args, notebook_id=notebook_id, json_output=True, dry_run=dry_run)
    if dry_run:
        return "DRY_RUN_TASK_ID"

    task_id = parse_id(output)
    if task_id:
        print(f"   🎙️ Task ID: {task_id}")
    else:
        print(f"⚠️  無法解析 task-id:\n{output}")
    return task_id


def wait_and_download(
    task_id: str,
    notebook_id: str,
    output_path: str,
    dry_run: bool,
    skip_wait: bool = False,
) -> bool:
    """等待完成並下載音頻（含超時自動重試）。"""
    if not skip_wait:
        for attempt in range(1, MAX_WAIT_RETRIES + 1):
            print(f"   ⏳ 等待生成完成 (嘗試 {attempt}/{MAX_WAIT_RETRIES}, timeout={DEFAULT_TIMEOUT}s)...")
            output = nbm(
                "artifact", "wait", task_id,
                "--timeout", str(DEFAULT_TIMEOUT),
                notebook_id=notebook_id,
                dry_run=dry_run,
            )
            if dry_run or "completed" in output.lower() or "Artifact completed" in output:
                break
            # 超時 → poll 檢查狀態
            print(f"   ⏳ 超時，檢查狀態...")
            poll_output = nbm("artifact", "poll", task_id,
                              notebook_id=notebook_id, dry_run=dry_run)
            if "completed" in poll_output.lower():
                print(f"   ✅ 已完成！")
                break
            if "failed" in poll_output.lower():
                print(f"   ❌ 生成失敗")
                return False
            print(f"   ⏳ 仍在處理中，繼續等待...")

    print(f"   ⬇️ 下載至: {output_path}")
    nbm(
        "download", "audio", output_path,
        "-a", task_id, "--force",
        notebook_id=notebook_id,
        dry_run=dry_run,
    )

    if not dry_run and Path(output_path).exists():
        size = Path(output_path).stat().st_size
        print(f"   ✅ 下載成功 ({size:,} bytes)")
        return True
    return dry_run


def rename_artifact(
    task_id: str,
    notebook_id: str,
    new_title: str,
    dry_run: bool,
) -> None:
    """重命名 artifact（task_id 即 artifact_id）。"""
    print(f"   ✏️ 重命名 artifact: {new_title}")
    nbm("artifact", "rename", task_id, new_title,
         notebook_id=notebook_id, dry_run=dry_run)


def feedback_audio(
    audio_path: str,
    episode_num: int,
    series: dict,
    notebook_id: str,
    dry_run: bool,
) -> str:
    """將音檔上傳回筆記本，回傳來源標題（用於下一集的續集銜接）。"""
    filename = Path(audio_path).name
    title = f"EP{episode_num:02d}_第{episode_num}集對話紀錄"
    print(f"   ♻️ 上傳音檔回筆記本: {filename}")
    nbm(
        "source", "add", audio_path,
        "--title", title,
        notebook_id=notebook_id,
        json_output=True,
        dry_run=dry_run,
    )
    # NotebookLM 可能使用原始檔名作為 title，回傳檔名以備用
    print(f"   ✅ 已回傳為來源（檔名: {filename}）")
    return filename


def distill_episode(
    episode_num: int,
    series: dict,
    notebook_id: str,
    dry_run: bool,
) -> str:
    """（可選）生成知識蒸餾摘要，建立為 note。"""
    show_name = series["name"]
    title = f"EP{episode_num:02d} 摘要 — {show_name}"
    description = (
        f'Create a concise study guide for Episode {episode_num} of "{show_name}". '
        f"Structure: 1) KEY CONCLUSIONS (3-5 points), "
        f"2) UNRESOLVED QUESTIONS (2-3 points for future episodes), "
        f"3) CONTINUITY NOTES (recurring themes, callbacks). "
        f"Keep under 500 words."
    )

    print(f"   📝 生成蒸餾摘要: {title}")
    output = nbm(
        "generate", "report", description,
        "--format", "study-guide",
        notebook_id=notebook_id,
        json_output=True,
        dry_run=dry_run,
    )

    if not dry_run:
        task_id = parse_id(output)
        if task_id:
            nbm("artifact", "wait", task_id, "--timeout", "300",
                 notebook_id=notebook_id, dry_run=dry_run)
            report_path = f"/tmp/notebooklm/_distill_ep{episode_num:02d}.md"
            nbm("download", "report", report_path, "-a", task_id, "--force",
                 notebook_id=notebook_id, dry_run=dry_run)
            if Path(report_path).exists():
                content = Path(report_path).read_text(encoding="utf-8")
                nbm("note", "create", content, "-t", title,
                     notebook_id=notebook_id, dry_run=dry_run)
                print(f"   ✅ 蒸餾摘要已建立為筆記: {title}")
    elif dry_run:
        print(f"   [DRY-RUN] 蒸餾摘要筆記: {title}")
    return title


# ── 主流程 ────────────────────────────────────────────
def run_series(config: dict, args: argparse.Namespace):
    """執行連續性 Podcast 序列生成。"""
    series = config["series"]
    episodes = config["episodes"]
    config_dir = Path(args.config).parent.resolve()

    # 輸出目錄
    show_slug = re.sub(r"[^\w\-]", "_", series["name"])
    output_dir = Path(args.output_dir or f"/tmp/notebooklm/series/{show_slug}")
    output_dir.mkdir(parents=True, exist_ok=True)
    print(f"📂 輸出目錄: {output_dir}")

    # 集數範圍
    start = max(1, args.start)
    end = min(len(episodes), args.episodes) if args.episodes else len(episodes)
    selected = episodes[start - 1 : end]

    total_steps = 6
    print(f"🎬 將生成第 {start} 至第 {end} 集 (共 {len(selected)} 集, 模式: 序列回饋)\n")

    # 筆記本
    notebook_id = create_or_get_notebook(series, args.dry_run)
    prev_audio_title: str | None = None

    for i, episode in enumerate(selected, start=start):
        ep_title = episode.get("title", f"Episode {i}")
        print(f"\n{'='*60}")
        print(f"🎙️  第 {i} 集: {ep_title}")
        print(f"{'='*60}")

        # 1. 上傳來源
        print(f"\n[1/{total_steps}] 上傳來源...")
        upload_sources(episode, notebook_id, config_dir, args.dry_run)

        # 2. 組裝提示詞
        print(f"\n[2/{total_steps}] 組裝提示詞...")
        description = build_episode_prompt(series, episode, i, prev_audio_title)
        print(f"   📋 提示詞長度: {len(description)} 字元")
        if args.dry_run:
            print(f"   --- 提示詞預覽 ---")
            for line in description.split("\n")[:10]:
                print(f"   | {line}")
            if description.count("\n") > 10:
                print(f"   | ... ({description.count(chr(10)) - 10} more lines)")

        # 3. 生成音頻
        print(f"\n[3/{total_steps}] 生成音頻...")
        task_id = generate_audio(description, series, notebook_id, args.dry_run)

        # 4. 等待 + 下載
        ep_filename = f"ep{i:02d}_{re.sub(r'[^\\w]', '_', ep_title)}.mp3"
        output_path = str(output_dir / ep_filename)
        print(f"\n[4/{total_steps}] 等待與下載...")
        success = wait_and_download(
            task_id, notebook_id, output_path, args.dry_run, args.skip_wait
        )

        # 4.5 重命名 artifact
        artifact_title = episode.get("artifact_title", ep_title)
        print(f"\n[4.5/{total_steps}] 重命名 artifact...")
        rename_artifact(task_id, notebook_id, artifact_title, args.dry_run)

        # 5. 音檔回傳（序列回饋法核心）
        if not args.skip_wait:
            print(f"\n[5/{total_steps}] 音檔回傳至筆記本...")
            prev_audio_title = feedback_audio(
                output_path, i, series, notebook_id, args.dry_run
            )
        else:
            prev_audio_title = None

        # 6. 知識蒸餾（可選）
        step = total_steps
        if args.distill:
            print(f"\n[{step}/{total_steps}] 知識蒸餾...")
            distill_episode(i, series, notebook_id, args.dry_run)
        else:
            print(f"\n[{step}/{total_steps}] 完成")

        print(f"\n✅ 第 {i} 集完成!")

    print(f"\n{'='*60}")
    print(f"🎉 全部完成! 共 {len(selected)} 集 (序列回饋)")
    print(f"📂 檔案位於: {output_dir}")
    if not args.dry_run:
        run_cmd(["ls", "-la", str(output_dir)])


# ── 入口 ──────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="連續性 Podcast 自動化生成（序列回饋法）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
序列生成 + 音檔回傳（歷史對話回饋法）
每集完成後自動上傳音檔回筆記本，AI 下一集即擁有前集完整記憶。

範例:
  # 預覽模式
  python scripts/episodic_podcast.py --config series.yaml --dry-run

  # 生成全部
  python scripts/episodic_podcast.py --config series.yaml

  # 續製（從第 3 集開始）
  python scripts/episodic_podcast.py --config series.yaml --start 3

  # 加上知識蒸餾
  python scripts/episodic_podcast.py --config series.yaml --distill
        """,
    )
    parser.add_argument("--config", required=True, help="YAML 配置檔路徑")
    parser.add_argument("--episodes", type=int, default=0, help="只生成前 N 集")
    parser.add_argument("--start", type=int, default=1, help="從第 N 集開始")
    parser.add_argument("--dry-run", action="store_true", help="預覽模式")
    parser.add_argument("--output-dir", default="", help="輸出目錄")
    parser.add_argument("--distill", action="store_true",
                        help="額外生成知識蒸餾摘要 (預設關閉)")
    parser.add_argument("--skip-wait", action="store_true",
                        help="不等待生成完成 (僅觸發)")

    args = parser.parse_args()

    print(f"📋 載入配置: {args.config}")
    config = load_config(args.config)
    if args.dry_run:
        print("🔍 DRY-RUN 模式：僅預覽，不實際執行\n")

    run_series(config, args)


if __name__ == "__main__":
    main()
