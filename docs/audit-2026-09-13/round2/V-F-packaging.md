# 驗證 F:打包/CI 十條(R8)—— 4 成立、3 已載明、2 撤銷、1 已知未修

前置:.venv 逐筆吻合 uv.lock(所以 venv vs tool diff = lock vs 實裝);branch protection 403(免費方案設不了,不是沒設);a690e72 是刻意瘦身 commit(內容一字未刪,外移到 gotchas)。

R8-P1-1 tag→latest 無 CI 閘 → **CONFIRMED,P1 維持,證據比原報告更硬**。
  五次發版 retag_start - ci_end:v0.9.23 **-77s**(retag 06:06:05 跑完 force-push latest,CI 06:07:11 才綠 → 指針比綠燈早 66 秒)、v0.9.24 +33s、v0.9.25 +349s、v0.9.26 +17s、v0.9.27 +27s。原報告拿 27 秒當破口是誤讀(那是安全餘裕);真破口在 v0.9.23,五次 4 守 1 破,破的那次無人察覺。retag-latest.yml 無 needs / workflow_run。release-checklist.md:54/:20 明文「tag 之前 CI 綠」但無強制。
  修法落點:retag-latest.yml:37 那個 step 之前插 gate step(gh api 查 github.sha 的 ci.yml conclusion,非 success exit 1)。不放進 retag-latest.sh(:61 是本機救援路徑)。

R8-P2-2 CI 不對帳 lock/pyproject → CONFIRMED(機制)/ ALREADY-DOCUMENTED(release-checklist.md:31-32 記了 v0.9.23/24)。**P3**:uv tool install 不讀 lock,對消費端零影響。修法:ci.yml:31 `uv sync --locked --extra dev`。

R8-P2-3 transitive 漂移 → ALREADY-DOCUMENTED(release-checklist.md:72-77 就是這條論述)。實測 **23** 筆不同(42 共同套件),審查者低估。殘留:recipe(:109-112)只 grep mcp、notebooklm-py 由 compare_sdk_surface 覆蓋,transitive 零覆蓋。**P3**。修法:checklist:109-112 改全量 diff。

R8-P2-4 markdown 無上界 → **CONFIRMED**。commit c3d5e94 與 CHANGELOG 查無取捨紀錄;落在 notes_html 半信任渲染路徵,4.0 改 extension 語意會讓允許清單沉默拒收整季(失敗形狀「發不出去」)。**P3**。修法:pyproject.toml:28 `"markdown>=3.5,<4"`。

R8-P2-5 README 教 uv pip install -e → **CONFIRMED,範圍擴大到 4 處**:README.md:16、:60、docs/auth-and-config.md:17、docs/test-account.md:42,無免責。**P2 低**。修法:→ `uv sync --extra dev`(--extra login)。

R8-P2-6 模組表漏三模組 → ALREADY-DOCUMENTED。a690e72 刻意外移;三條紅線各在被路由的 gotchas(attempt:3 manifest_store、pool:33 _cookies、files:44 audio_finalize)。**P3/nit**(純導覽:只知檔名找不到該讀哪份)。若修:§按需載入表「動到什麼」欄補檔名,不加 §Architecture 列。

R8-P2-7 sync check 觸發面 → ALREADY-DOCUMENTED(CHANGELOG:113-136 指名修法留下一輪;checklist:16-24)。兩項補充 REFUTED:`gh run rerun` 是文件化的手動口(:24);抓 HEAD 非釘版是檢查的目的。**已知未修,不計入**。

R8-P3-8 浮動 action tag → CONFIRMED,**P3 維持**。緩解:persist-credentials: false(ci.yml:21-22)、retag permissions 最小。修法:@vN → @<sha> # vN + Dependabot github-actions。

R8-P3-9 scripts 列漏兩支 → **REFUTED/撤銷**。retag-latest.sh 就在 AGENTS.md §Conventions;compare_sdk_surface 在 release-checklist.md:118-122 且 AGENTS.md 路由表指過去;git log -S 零 commit 它從未靠 AGENTS.md 承載。

R8-P3-10 dist/ 舊 wheel → **REFUTED/撤銷**。gitignored(.gitignore:11);CI 乾淨 checkout;uv tool install 只收單一 positional → 當場大聲紅。本機 rm -rf dist/ 即可。
