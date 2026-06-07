import pytest
from notebooklm.rpc.types import AudioFormat, AudioLength

from notebooklm_mcp import tools_basic as t


async def test_generate_audio_defaults_zh_hant(fake_client):
    out = await t.generate_audio("nb-1", instructions="講解重點")
    assert out["task_id"] == "task-123"
    call = fake_client.artifacts.calls[0][1]
    assert call["language"] == "zh_Hant"
    assert call["instructions"] == "講解重點"


async def test_generate_audio_maps_enums(fake_client):
    await t.generate_audio("nb-1", audio_format="debate", audio_length="long")
    call = fake_client.artifacts.calls[0][1]
    assert call["audio_format"] == AudioFormat.DEBATE
    assert call["audio_length"] == AudioLength.LONG


async def test_generate_audio_rejects_bad_language(fake_client):
    with pytest.raises(ValueError):
        await t.generate_audio("nb-1", language="zh-TW")


async def test_source_add_file_passes_mime(fake_client):
    out = await t.source_add_file("nb-1", "/tmp/x.mp3", mime_type="audio/mpeg")
    assert out["source_id"] == "src-123"
    assert fake_client.sources.calls[0][1]["mime_type"] == "audio/mpeg"


async def test_artifact_download_arg_order(fake_client):
    out = await t.artifact_download_audio("nb-1", "/tmp/out.mp3", artifact_id="art-9")
    assert out["path"] == "/tmp/out.mp3"
    c = fake_client.artifacts.calls[0][1]
    assert c["output_path"] == "/tmp/out.mp3" and c["artifact_id"] == "art-9"


async def test_ask(fake_client):
    out = await t.chat_ask("nb-1", "重點?")
    assert "重點" in out["answer"]
