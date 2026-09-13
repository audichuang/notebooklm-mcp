# R8 審查者:打包/CI/文件漂移/實裝對帳(10 條)
註:session 中 HEAD 從 983370d 推到 751e04b(兩個 commit 只動 .claude/skills/acceptance-workspace/,非本 session 的 agent 所為,是使用者另一個 session);v0.9.27..HEAD 對 notebooklm_mcp/ tests/ pyproject uv.lock 零差異。

## P1-1 tag → latest 之間沒有 CI 閘,只靠 27 秒手速
ci.yml:3-7 只對 branches master 觸發;retag-latest.yml:9-12 收到 v*.*.* 就 force push latest。實測 v0.9.27:ci run 34717378632 20:32:05 起 1m20s;retag run 34717467740 20:33:52 起。branch protection API 403(private+free)。retag 單調不可退。信心高。

## P2-2 CI 不對帳 uv.lock 與 pyproject,已漏過一次
v0.9.23 tag 樹:pyproject=0.9.23 / uv.lock=0.9.21,該 commit CI 綠(uv sync 自己補 lock,不查 tree 髒)。現況一致。信心高。

## P2-3 實裝 vs lock 漂移約 20 個 transitive 套件,對帳 recipe 只查 mcp
anyio 4.13.0→4.15.1、pydantic 2.13.4→2.13.5、pydantic_settings 2.14.1→2.15.0、cryptography 48→50、starlette 1.2.1→1.6.0、uvicorn 0.49→0.52.4 等。anyio 與 pydantic_settings 是 stdio 也踩的。信心高。

## P2-4 markdown>=3.5 無上界,與 pyproject 自述「上界是硬需求」矛盾
pyproject.toml:32。唯一實際漂開的直接依賴(3.10.2→3.10.3)。用在講義 Markdown→HTML 發布路徑。信心高。

## P2-5 README 教 `uv pip install -e ".[dev]"`,AGENTS.md 明文禁止
README §Development install / §Auth refresh vs AGENTS.md §Commands。dev extras 無上界(本機 pytest 9.0.3 / pytest_asyncio 1.4.0)。信心高。

## P2-6 AGENTS.md 模組表漏三個承重模組
manifest_store.py(ManifestStore 唯一寫入入口)、_cookies.py(ADR-0010 load-bearing claim)、audio_finalize.py(Canonical Enclosure 落地處)。信心高。

## P2-7 跨 repo sync check 觸發面單邊,且無 workflow_dispatch / schedule
audi-skill 無 .github/workflows;本 repo ci.yml 也沒手動觸發口;抓 audi-skill 當下 HEAD 非釘版。本機現在綠(37 tools + 33 terms)。信心高。

## P3-8 CI action 用浮動 major tag,而 job 持有跨 repo deploy key / contents:write
ci.yml:12 checkout@v4、:19 setup-uv@v6、retag-latest.yml:29。信心中。

## P3-9 AGENTS.md scripts 列漏 compare_sdk_surface.py 與 retag-latest.sh
AGENTS.md:66。前者是 release-checklist 規定升 SDK 前必跑且帶 _CALLS 維護義務。信心高。

## P3-10 dist/ 留兩版舊 wheel,本機照 CI recipe `dist/*.whl` 會炸
untracked、已 gitignored。吵著失敗。信心高。

## 乾淨的(信任錨)
版本單一來源(importlib.metadata);出貨無缺口;latest 指針正確 = v0.9.27 = 593727f;retag sort -V 正確、pre-release 被 regex 濾掉;payload 37 支 / 37,255 字元(上限 40,000)、publish_series 2,950(上限 3,200)、instructions 1,271(上限 1,400);18 個文件連結零 404;release-checklist 可執行面全對;命名一致 nblm-mcp;login extra pin 一致;版控衛生乾淨(160 tracked,無 dist/.venv/.remember/.superpowers);CONTEXT.md 無敏感;secrets grep 全偽陽性;.gitignore 涵蓋(.dev.vars 未涵蓋但不用 CF)。

## 對帳表
mcp 1.30.0 三處一致;notebooklm-py 0.8.2 一致;httpx 0.28.1;Pillow 11.3.0;mutagen 1.48.1;markdown lock 3.10.2 / tool 3.10.3 ⚠;pydantic lock 2.13.4 / tool 2.13.5 ⚠。
中性目錄實裝驗證:tool venv __file__ = ~/.local/share/uv/tools/notebooklm-mcp/.../notebooklm_mcp/__init__.py,__version__ 0.9.27,python 3.12.3。
