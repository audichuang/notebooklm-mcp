"""`_text` 的兩支 regex 在 CJK + 程式碼文字上的誤傷(v0.9.26 三視角複核確認)。

這兩支的產物直通公開 RSS:`chat_ask` → `episode_set_description`
(`strip_citations` 預設 True)→ manifest → Apple Podcast `<description>`。
被改壞的句子仍然通順,所以**沒有任何東西會告訴呼叫端內容被動過** —— 這正是
需要 false-positive 測試的理由:原本的測試只驗「該清的有清掉」。
"""
from notebooklm_mcp._text import strip_citations as _strip_citations_impl, strip_inline_emphasis


def _strip_citations(text: str) -> str:
    return _strip_citations_impl(text)


# --- _EMPHASIS_RE:單星號不是強調標記 ---

def test_emphasis_leaves_cjk_adjacent_single_stars_alone():
    """`A*搜尋` / `B*樹`:演算法名稱裡的星號,兩個湊成一對被整段吃掉。

    界線只認 `\\s`,而 CJK 不用空格分詞,所以西文那套邊界判斷在這裡完全失效。
    """
    assert strip_inline_emphasis("他說「A*搜尋」與「B*樹」的差別") == "他說「A*搜尋」與「B*樹」的差別"
    assert strip_inline_emphasis("A*搜尋比較快,但 A*需要 heuristic") == "A*搜尋比較快,但 A*需要 heuristic"


def test_emphasis_leaves_c_pointer_stars_alone():
    assert strip_inline_emphasis("指標 *p 與 *q 的差別") == "指標 *p 與 *q 的差別"


def test_emphasis_still_strips_the_shape_the_incident_was_about():
    """v0.9.14 FINDING-D 的形狀是 `**粗體**`,CJK 貼著也要清得掉。"""
    assert strip_inline_emphasis("**粗體**要被清掉") == "粗體要被清掉"
    assert strip_inline_emphasis("前面**粗體**後面") == "前面粗體後面"
    assert strip_inline_emphasis("***三顆星***也是") == "三顆星也是"


def test_emphasis_leaves_underscores_alone():
    """既有界線:`_斜體_` 與 snake_case 識別碼同形,不動。"""
    assert strip_inline_emphasis("欄位 _source_id_ 不要動") == "欄位 _source_id_ 不要動"


# --- _CITATION_RE:緊貼識別碼的方括號是索引,不是引用 ---

def test_citation_leaves_array_indexing_alone():
    assert _strip_citations("陣列索引 arr[0] 與 arr[1] 的差別") == "陣列索引 arr[0] 與 arr[1] 的差別"
    assert _strip_citations("數學式 a[1][2] 表示矩陣元素") == "數學式 a[1][2] 表示矩陣元素"


def test_citation_still_strips_real_citation_markers():
    """原本就在守的行為:真的引用標記照清,而且連前面的水平空白一起吃。"""
    assert _strip_citations("他說的是這個 [1] 引用") == "他說的是這個 引用"
    assert _strip_citations("見 [3, 4] 兩篇") == "見 兩篇"
    assert _strip_citations("範圍 [2-5] 都有") == "範圍 都有"


# --- 反向:別為了擋誤傷而漏清真引用(v0.9.26 獨立複審抓到的回歸) ---

def test_citation_strips_markers_glued_to_cjk():
    """`重點[1]。` —— 中文不用空格,標記緊貼正文是**最常見**的形狀,不是邊角案例。

    第一版的界線寫成 `(?<![\w\]])`,而 Python 的 `\w` 涵蓋 CJK,於是整類中文引用
    都漏清 —— 比它原本要修的 `arr[0]` 誤傷更糟,因為這支的用途就是產繁中公開文案。
    """
    assert _strip_citations("重點[1]。") == "重點。"
    assert _strip_citations("這個說法[3, 4]很可疑") == "這個說法很可疑"


def test_citation_strips_consecutive_markers():
    """`[1][2]` 連續引用:第二個標記前面是 `]`,單趟會留下 `見[2]` 這種半清理殘骸。"""
    assert _strip_citations("見 [1][2] 兩篇") == "見 兩篇"
    assert _strip_citations("他說的是這個 [1][3, 4] 引用") == "他說的是這個 引用"


def test_identifier_indexing_survives_the_fixpoint_loop():
    """不動點迴圈不可以把識別碼索引磨掉 —— `a[1][2]` 第一趟就該是不動點。"""
    assert _strip_citations("數學式 a[1][2] 表示矩陣元素") == "數學式 a[1][2] 表示矩陣元素"
    assert _strip_citations("Transformer[1] is good.") == "Transformer[1] is good."
