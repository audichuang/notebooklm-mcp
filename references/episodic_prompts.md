# 連續性 Podcast 超級提示詞模板庫

所有模板中 `{變數}` 由腳本或代理自動填入。

> 命名銜接:大綱每集都要有 `title`(必填非空),工作室 artifact 與來源都會命名為
> `EP{n:02d} 標題`(例 `EP01 心法篇`)。下方模板 2 的 `{prev_episode_summary_title}`
> 對應前集那個 `EP{N-1:02d} 標題` 來源。

***

## 1. 身份綁定預設提示詞（每集必須貼入）

```
This is the podcast called "{show_name}".
The male host is "{host_a}" and the female host is "{host_b}".
At the very beginning, announce the show name and introduce yourselves by name.

Style guidelines:
- {style_description}
- Target audience: {audience}
- Do NOT use filler words (like, um, ah, you know).
- Do NOT interrupt each other. Take turns speaking clearly.
- Keep the tone consistent across all episodes.
```

***

## 2. 續集銜接提示詞（第 2 集起使用）

```
This is Episode {episode_number} of "{show_name}".
You MUST first read the source titled "{prev_episode_summary_title}" — it contains the transcript and key conclusions from Episode {prev_episode_number}.

Opening structure (30 seconds max):
1. Announce the show name and your names.
2. Briefly recap 2-3 key points from the previous episode.
3. Tease what this episode will explore.

Then dive into the new material. Do NOT re-introduce basic background that was already covered. Assume listeners heard the previous episode.
```

***

## 3. 矛盾放大法（The Contradictions Prompt）

適用時機：系列中期，引入新觀點時。

```
Compare the source titled "{source_a}" with "{source_b}".
Identify the BIGGEST contradiction or conflict between their findings.
{host_a} must defend Source A's position, and {host_b} must defend Source B's position.
Both hosts MUST directly quote original text as evidence to support their arguments.
This should feel like an intense but respectful academic debate.
End by acknowledging which points remain unresolved — these will be revisited in the next episode.
```

***

## 4. 來源缺口探測法（The Source Gap Prompt）

適用時機：季末回顧、深度研究。

```
Act as rigorous data auditors reviewing all sources in this notebook.
Identify:
1. Unverified assumptions that the authors presented as facts.
2. Information gaps — topics the authors deliberately avoided.
3. Logical leaps where evidence does not support the conclusion.
For each finding, provide a direct quote from the source as proof.
Frame these gaps as open questions for the next season of the show.
```

***

## 5. 動態辯證法（The Dialectical Lens）

適用時機：單一視角文本的批判性解析。

```
Assume the author of this text is an "unreliable narrator" who has hidden biases or conflicts of interest.
{host_a}: Present the author's argument faithfully and explain why it seems convincing.
{host_b}: Systematically deconstruct it. Find hidden contradictions, power dynamics, and self-serving logic.
Both hosts must cite specific passages from the source.
Conclude by asking: "What would this text look like if written by someone with the opposite bias?"
```

***

## 6. 逐字朗讀駭客（Read Verbatim Hack）

適用時機：經典文本導讀、每日連載。

```
Before ANY discussion, one host must read the following section VERBATIM, word for word, without paraphrasing:

####INTRODUCTION
{verbatim_text}
####END

After the verbatim reading, the other host should say: "Thank you. Now let's break this down."
Then proceed with analysis and discussion of the text just read.
```

***

## 7. 知識蒸餾指令（生成本集研讀講義 / 續集摘要）

把下方結構要求放進 `generate_report` 的 `extra_instructions`（`report_format="study_guide"`）。
產出的 study guide 有兩種用途:(a) 當**單集研讀講義**發布 —— 路徑自動回寫 manifest,
`publish_series` 會渲染成 HTML 附進單集簡介(見 SKILL §Publish 步驟 3);(b) 或用
`source_add_file` 加回筆記本當來源,供下一集 recap(續集連續性)。

```
Create a concise study guide for Episode {episode_number} of "{show_name}".
Structure:
1. KEY CONCLUSIONS (3-5 bullet points of what was established)
2. UNRESOLVED QUESTIONS (2-3 points left for future episodes)
3. NOTABLE QUOTES (direct quotes from sources that were discussed)
4. CONTINUITY NOTES (character arcs, recurring themes, callbacks to previous episodes)
Keep it under 500 words. This will be used as context for the next episode.
```

***

## 8. 大綱規劃提示詞（當作 `chat_ask` 的提問）

沒有 persona / configure 工具;規劃整季大綱時,把下方文字當作 `chat_ask(notebook_id, question)`
的問題送進筆記本,讓 NotebookLM 依來源產出結構化大綱(標題 / 主題 / 來源 / 連續性鉤子 / 預告)。
產出的大綱經你核可後,整理成 `podcast_series` 的 `episodes` 陣列。

```
You are the executive producer of a podcast series called "{show_name}".
Your hosts are {host_a} and {host_b}. The show's style is: {style_description}.
When asked about episode planning, you provide structured outlines with:
- Episode title
- Key themes to cover
- Suggested source materials to highlight
- Continuity hooks from previous episodes
- Cliffhanger or teaser for the next episode
```
