# 驗證 C:發布五條(R4)—— F1 P1 成立且更嚴重;F3/F4 P3 成立;F2 降 P3;F5 降 P4

R4-F1 feed 身分無圍籬 → **CONFIRMED,show_id 分支 P1 維持;salt 分支 P2(ADR-0003 已決策未實作範圐)**。
  判斷成分已解:docstring :428 講的是首發七欄沿用機制,沒把 show_id 提升成可換身分;design-notes:116「永不改」、identity.py「NO global registry」;CHANGELOG/gotchas-publish 零句「換 show_id 是 host 決定」;ADR-0013 graphify 換 show_id 的做法是新 manifest + 舊標 retired,不是同一份改指向。
  實跑(MockTransport):第一次 token 5tash…,第二次 show_id=audicast-v2 → token nhlch… 無例外無警告、manifest show_id 被覆寫、8 個檔全 PUT 到新 token、回傳零提及舊身分;salt s3cret vs s3cret-dev 同 show_id 不同 token;全 tools_publish 唯一 GET 是 /healthz(零 read-back)。
  加重兩點:錯誤自我延續(:823 覆寫後之後滾動加集永遠落新 feed,manifest 不記舊身分);salt 分支零痕跡(persisted_show 不含 token)。
  觸發可信度:show_id 打錯 中;salt 漂移 中偏高且無任何可見訊號。
  修法落點:tools_publish.py:471-480 show_cfg 解析後加一條比對,:819 persisted_show 存 token(token=f(show_id,salt) 一條比對蓋兩分支)。缺席=照發;逃生口=ManifestStore.update 移除欄位;⚠ token 是 feed URL 秘密成分會落到 podcast-lab 磁碟 —— 要確認那份 manifest 是否進版控/公開。show.json 在 :802-810 顯式 key 另組,加欄位不動遠端 bytes。測試:tests/test_publish_tools.py。

R4-F2 cover 品牌圍籬 → CONFIRMED(機制)**降 P3**。gotchas-publish.md:162 正規批次指令帶 --show-name Audicast;需三條件疊加;wordmark 一眼可見。manifest.get("title") fallback 實測死碼。修法:cover_cli.py:164 default=None、:201 兩者皆無 ap.error;⚠ 會打到 test_cover_cli.py:198/229/261/161。測試:test_cover_cli.py :300 旁加姊妹案例。

R4-F3 category 無驗證 → **CONFIRMED P3**。'' / 'Bogus' / '   ' 照發;加重:'' 回寫進 show.category 且 saved_show.get(...,"Technology") 因 key 存在回 '' → 黏性。建議只補 non-empty,不做 Apple 白名單。修法:tools_publish.py:520 附近 fail-fast 區一行。測試:test_publish_tools.py。

R4-F4 validate_xml_text → **CONFIRMED P3(措辭收斂)**。呼叫端只有 tools_publish.py:642 + feed.py 內部;U+000C 中段 set_description 放行、publish 才爆無集號。docstring 只列三項具名檢查非全稱(審查者過度解讀),但 :185-189 行內註解「錯誤在寫入當下就爆」意圖為真。修法:tools_artifacts.py set_description 旁加 feed.validate_xml_text(desc);tools_publish.py:642 拆 per-episode 迴圈包集號。測試:test_tools_artifacts.py / test_publish_tools.py。

R4-F5 naming 雙前綴 → 邊界表 **REFUTED**(naming.py docstring「leave anything else」;test_bare_leaves_another_episode_prefix_alone「publish-gate problem, not naming」);雙前綴 CONFIRMED 但無 code path 產得出來(host 手打)。**P4 won't-fix**。附帶:feed.py:152 build_index_html 自己寫第二份剝除 regex,牴觸 naming.py「only place」。
