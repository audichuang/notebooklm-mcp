"""attempt 要內嵌送出的 brief 全文,不只雜湊。

實測(podcast-lab agent-memory 一季五集):送出的 inline brief 與磁碟上的 brief.md
sha 全數不符——貼上剝檔尾換行是傳輸層差異,更糟的是貼上瞬間的手改;其中一集手改
未記錄,事後三輪重建都對不回「attempt 只存的那個 brief_sha256」,實際生成輸入從此
沒有任何正本。修法:`_create_audio_attempt` 在 `brief_sha256` 旁存 `brief` 原文,
manifest 進版控 = 送出 bytes 有正本,雜湊有東西可對。
"""
import hashlib
import json

from notebooklm_mcp import tools_podcast as p


async def test_episode_attempt_persists_the_exact_submitted_brief(fake_client, tmp_path):
    """逐位元組:檔尾換行也要進 manifest——五集 divergence 的傳輸層差異就是它。"""
    manifest_path = tmp_path / "series_manifest.json"
    brief = "第五集主線:M-way Tree 與磁碟 I/O。\n最後一段收在 leaf chain 七筆。\n"

    await p.podcast_episode(
        "nb-1",
        episode_n=5,
        title="心法篇",
        brief=brief,
        output_dir=str(tmp_path),
        manifest_path=str(manifest_path),
    )

    attempt = json.loads(manifest_path.read_text(encoding="utf-8"))["episodes"][0][
        "attempts"
    ][0]
    assert attempt["brief"] == brief
    # 自我對帳:存進去的全文必須就是雜湊的那份,不然欄位只是第二個謊言。
    assert (
        hashlib.sha256(attempt["brief"].encode("utf-8")).hexdigest()
        == attempt["brief_sha256"]
    )


async def test_series_attempt_persists_the_brief_too(fake_client, tmp_path):
    """series 與 episode 共用 `_create_audio_attempt`——兩個入口同一個洞、同一次修。"""
    eps = [{"title": "心法篇", "brief": "第一集開場。\n"}]

    await p.podcast_series("nb-1", episodes=eps, output_dir=str(tmp_path))

    manifest = json.loads(
        (tmp_path / "series_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["episodes"][0]["attempts"][0]["brief"] == "第一集開場。\n"
