# R4 審查者:發布/附件/RSS/封面/命名(5 條)

## F1 〔P1〕 feed 身分沒有 machine-readable 圍籬:同一份 manifest 可無警告換到另一條 feed
位置:tools_publish.py:472(`show_id or saved_show.get("show_id")` 不比對既存值)、:457(salt)、:539(token)、:819-823(回寫 show)、:831-843(回傳)
不變式:design-notes.md:116「show_id 永不改」;ADR-0003;ADR-0013 §背景(graphify 換 show_id 事故);AGENTS.md salt 紅線
序列 A:已發 50 集的 manifest 再呼叫 publish_series(show_id="audicast-v2" 或打錯字)→ 新 token → 整季 PUT 到新 feed → :823 覆寫 manifest show_id → 舊 feed 永久凍住,訂閱者收不到
序列 B:同一 manifest 在 -c dev 或 Doppler 漂掉 → 不同 salt → 同樣落到新 feed
為何偵測不到:persisted_show 不含 token;publish_series 從不 GET 既有 show.json 做 read-back
對比:itunes_type / published_at / retired / publication_state 都有機器檢查,決定 URL 的 show_id × salt 沒有
測試:無(test_publish_tools.py:146/306 只有單一 show_id)
信心高(唯一判斷成分:docstring :428「顯式參數永遠優先並回寫」是否刻意涵蓋身分欄位)

## F2 〔P2〕 cover_cli 批次模式 Audicast 品牌圍籬在「首次發布前」失效
位置:cover_cli.py:200-203。--show-name argparse default "Audicast" 永遠 truthy → manifest.get("title") fallback 死碼 → 唯一有效判準是 manifest["show"]["show_title"],而它只在第一次 publish_series 成功後(tools_publish.py:823)才存在
序列:新節目未發布、批次 `notebooklm-cover --manifest … --byline …` 不帶 --show-name → 12 張封面印上模板寫死副標「AI AGENTIC ENGINEERING」/ audicast@dev-env 裝飾(assets/cover_episode.html:455-470)→ 上架並內嵌 ID3
緩解:wordmark 印成 Audicast,肉眼可見(故 P2)
測試:test_cover_cli.py:300 fixture 預塞 show.show_title,避開此狀態
信心高

## F3 〔P3〕 itunes:category 無驗證,空字串繞過 required 檢查
位置:tools_publish.py:480(`category is not None`)、:504-505(missing 七欄不含 category);feed.py:94
實跑:category="" → `<itunes:category text=""/>`;"Bogus Category" 照發
不變式:feed.normalize_itunes_type docstring 的論證逐字適用
測試:test_publish_feed.py 無 category 值域測試(itunes_type 有四支)
信心高

## F4 〔P3〕 validate_xml_text preflight 沒集號定位;寫入端 episode_set_description 不跑同一檢查
位置:tools_publish.py:642-654 → feed.py:36;tools_artifacts.py:187-192
不變式:tools_publish.py:611-612 自己寫「guard 要帶集號」,五處補了、這處沒;episode_set_description docstring :174-178 宣稱「寫入當下就驗 publish preflight」但只跑 render_episode_notes_html
實跑:U+000C(PDF 分頁符)→ set_description 放行 → publish_series raise「XML 1.0 forbids character U+000C」無集號無欄位
測試:無
信心高

## F5 〔P3〕 naming.bare_episode_title 只剝一層前綴,重複前綴原樣上 RSS
位置:naming.py:11-24;cover_cli.py:228/:281;tools_publish.py:586(serial preflight 只驗開頭一個對的前綴)
實跑:"EP01. EP01. 深入淺出" → RSS 原樣;封面 __TITLE__ = "EP01. 深入淺出"(EP 徽章旁再印一次)
邊界表(不上 serial feed,影響 episodic 封面 / Studio 名):ep01.(小寫)、EP01．(全形)、EP01.標題(無空格)、NBSP 都不剝
測試:test_naming.py 九支無重複前綴/大小寫/全形
信心高

## 已查證乾淨
XML escaping/CDATA(escape/quoteattr/]]> 拆接)、notes_html 允許清單(javascript:/data:/charref/svg/img/style;markdown footnotes/abbr 不誤殺)、publication_state/retired 共用 state.py(四入口)、assert_not_retired 在 :469 讀完就擋
**retracted attempt 不可能上 feed**:manifest_store._validate:244 的檢查沒被 on_write 圍住 → publish_series 的 store.read() 就 raise;_RETRACTED_EPISODE_KEYS 把 output_attempt_id / artifact_id / mp3_path 一起 pop(⚠ 與 R1-F3 是同一個事實的兩面:R1 說讀端過嚴鎖死修復門,R4 說它正好守住發布)
附件三支稽核面全接線(ADR-0011);published_at 單調性同一入口;提交順序媒體→artwork→show.json→feed.xml→index.html
刻意排除:_audio_duration_hms、feed_info _require_env、ADR-0011/0013 明列刻意不做的
