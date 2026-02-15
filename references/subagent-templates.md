# 子代理模板

生成任務需 **5-15 分鐘**，必須用 `sessions_spawn` 委派子代理等待，避免主代理阻塞超時。

## 單任務模板

```
sessions_spawn task:"使用 exec 工具依序執行以下命令：
1. exec doppler run -p notebooklm -c dev -- notebooklm artifact wait <task-id> --timeout 600
2. exec mkdir -p /tmp/notebooklm
3. exec doppler run -p notebooklm -c dev -- notebooklm download audio -a <task-id> /tmp/notebooklm/podcast.mp3
4. exec ls -la /tmp/notebooklm/podcast.mp3
如果 step 1 超時，執行 exec doppler run -p notebooklm -c dev -- notebooklm artifact poll <task-id> 檢查狀態：
- in_progress → 再次 artifact wait
- completed → 繼續 step 2
- failed → 回報錯誤
最後回報下載的檔案路徑。"
label:"NotebookLM 生成"
runTimeoutSeconds: 900
```

## 多任務模板

當需要同時生成多個內容（如中英文音頻 + 簡報）時：

1. 在主代理中**依序**啟動所有 generate 任務（必須加 `--json`），記錄每個 task-id
2. 用**一個**子代理依序 wait + download 所有任務
3. **下載時必須用 `-a <task-id>` 指定 artifact**，否則會重複下載同一個
4. 下載路徑必須包含語言/類型標識，避免覆蓋

⚠️ **超時計算**：`runTimeoutSeconds = 任務數 × 單次 wait timeout + 300`

```
sessions_spawn task:"使用 exec 工具依序執行以下命令：
1. exec doppler run -p notebooklm -c dev -- notebooklm artifact wait <task-id-1> --timeout 600
2. exec doppler run -p notebooklm -c dev -- notebooklm download audio -a <task-id-1> /tmp/notebooklm/<描述>_zh.mp3
3. exec doppler run -p notebooklm -c dev -- notebooklm artifact wait <task-id-2> --timeout 600
4. exec doppler run -p notebooklm -c dev -- notebooklm download audio -a <task-id-2> /tmp/notebooklm/<描述>_en.mp3
5. exec doppler run -p notebooklm -c dev -- notebooklm artifact wait <task-id-3> --timeout 600
6. exec doppler run -p notebooklm -c dev -- notebooklm download slide-deck -a <task-id-3> /tmp/notebooklm/<描述>_zh.pdf
7. exec doppler run -p notebooklm -c dev -- notebooklm artifact wait <task-id-4> --timeout 600
8. exec doppler run -p notebooklm -c dev -- notebooklm download slide-deck -a <task-id-4> /tmp/notebooklm/<描述>_en.pdf
9. exec ls -la /tmp/notebooklm/
如果某個 wait 超時，用 artifact poll 檢查狀態後繼續。
最後回報所有下載的檔案路徑。"
label:"NotebookLM 多任務生成"
runTimeoutSeconds: 2700
```
