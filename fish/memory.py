"""记忆层：三层存储 + 打分检索。

层职责（Design_v0.2 §5.1）：
    episodic  每次真实做事后的 1-2 句摘要，量大、按需检索
    core      自我叙事，常驻上下文，只能慢改（重写有相似度门槛）
    relation  对具体人的认知，常驻上下文

为什么第一版不上向量库：单条记忆一两百字，三个月大约几千条，
统计打分在这个量级够用，而且接口不变——等检索质量真成瓶颈，
换掉 search() 的实现就行，调用方一行不用改（Design_v0.2 §3.2）。
"""

import math
import re
import sqlite3

from . import config, util

# ── 写入 ─────────────────────────────────────────────────────────────────

def add_episodic(conn: sqlite3.Connection, content: str, salience: float,
                 tags: str = "", source_event_id: int | None = None,
                 pinned: int = 0) -> int:
    cur = conn.execute(
        """INSERT INTO memories (ts, layer, content, salience, tags, source_event_id, pinned)
           VALUES (?, 'episodic', ?, ?, ?, ?, ?)""",
        (util.now_iso(), content, salience, tags, source_event_id, pinned),
    )
    conn.commit()
    return cur.lastrowid


def save_narrative(conn: sqlite3.Connection, layer: str, content: str) -> None:
    """写 core / relation 层。这两层是"单条覆盖"语义：永远只有最新版。"""
    assert layer in ("core", "relation")
    conn.execute("DELETE FROM memories WHERE layer = ?", (layer,))
    conn.execute(
        "INSERT INTO memories (ts, layer, content, salience, pinned) VALUES (?, ?, ?, 1.0, 1)",
        (util.now_iso(), layer, content),
    )
    conn.commit()


def get_narrative(conn: sqlite3.Connection, layer: str) -> str | None:
    row = conn.execute(
        "SELECT content FROM memories WHERE layer = ? ORDER BY ts DESC LIMIT 1",
        (layer,),
    ).fetchone()
    return row["content"] if row else None


def bump_recall(conn: sqlite3.Connection, ids: list[int]) -> None:
    """记录「这条记忆被想起来了」。

    recall_count 的分布是 §10.2 之外最有意思的观察点：
    哪些记忆被反复调取，就是鱼鱼的执念。
    """
    for mid in ids:
        conn.execute(
            "UPDATE memories SET recall_count = recall_count + 1, last_recalled_at = ? WHERE id = ?",
            (util.now_iso(), mid),
        )
    conn.commit()


# ── 工作记忆：单条槽（Design_v0.3 §5.5 M0）────────────────────────────────
# 与上面三层刻意分开：memories 存「发生过的事」，这里存「接下来打算做什么」。
# 所以它不进检索、不进打分、不进自省素材，也不受 I1（heartbeat 不写 memories
# 表）约束——空转轮恰恰是最需要给下一轮留话的时候（「这轮先歇，下次再做 X」）。
#
# 初学者注意「单条覆盖」这个套路：表里永远只有 id=1 一行，写入时先 DELETE
# 再 INSERT。和 save_narrative 是同一个写法，比 UPDATE ... ON CONFLICT 更好读。

def get_working_note(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """当前留给下一次醒来的自己的提示；没有（或已被 TTL 杀掉）就返回 None。"""
    return conn.execute("SELECT * FROM working_note WHERE id = 1").fetchone()


def set_working_note(conn: sqlite3.Connection, content: str,
                     born_event_id: int | None = None) -> None:
    """写一条新提示，覆盖旧的，并把存活计数重置为 TTL。"""
    ts = util.now_iso()
    conn.execute("DELETE FROM working_note WHERE id = 1")
    conn.execute(
        """INSERT INTO working_note
           (id, content, remaining_wakes, created_at, updated_at, born_event_id)
           VALUES (1, ?, ?, ?, ?, ?)""",
        (content, config.WORKING_NOTE_TTL, ts, ts, born_event_id),
    )
    conn.commit()


def _tick_working_note(conn: sqlite3.Connection) -> None:
    """这轮没写新提示：沿用旧的，存活计数 -1；归零就自动删掉。"""
    conn.execute(
        "UPDATE working_note SET remaining_wakes = remaining_wakes - 1, "
        "updated_at = ? WHERE id = 1",
        (util.now_iso(),),
    )
    conn.execute("DELETE FROM working_note WHERE id = 1 AND remaining_wakes <= 0")
    conn.commit()


def clear_working_note(conn: sqlite3.Connection) -> None:
    """她明确说「不留了」：立刻清掉，不等 TTL。"""
    conn.execute("DELETE FROM working_note WHERE id = 1")
    conn.commit()


def settle_working_note(conn: sqlite3.Connection, note: str | None,
                        born_event_id: int | None = None) -> None:
    """一次 wake 结束时结算工作记忆（由 scheduler 调用，每轮恰好一次）。

    note 是**已经过 brain 校验**的三态值，不是模型原始输出（I2 等价纪律）：
      None  = 这轮没写  → 沿用旧提示并扣一轮存活
      ""    = 显式不留  → 立刻清掉
      文本  = 新提示    → 覆盖并把存活重置回 TTL
    """
    if note is None:
        _tick_working_note(conn)
    elif note == "":
        clear_working_note(conn)
    else:
        set_working_note(conn, note, born_event_id)


# ── 检索 ─────────────────────────────────────────────────────────────────

def _recency(ts: str, now) -> float:
    """越新越接近 1。尺度 72 小时：三天前的事权重掉到 ~37%。"""
    hours = util.hours_between(now, util.parse_iso(ts))
    return math.exp(-hours / 72.0)


def _bigrams(text: str) -> set[str]:
    """把文本切成相邻两字一组。

    中文没有空格分词，上 jieba 之类的分词库对初学者是额外的坑。
    相邻两字（bigram）是土但够用的近似：「野猫」「猫的」这种
    片段足以让关键词匹配上。
    """
    return {text[i:i + 2] for i in range(len(text) - 1)}


def _text_grams(text: str) -> set[str]:
    """记忆正文这一侧能拿来匹配的片段：所有两字组 + 所有单字。

    单字也要收进来，因为她的查询可能只有一个字（「猫」）——一个字
    切不出 bigram，只剩「整字命中」这一条路。
    """
    return _bigrams(text) | set(text)


def _query_grams(query: str) -> set[str]:
    """查询这一侧切成片段，按空格和标点分段。

    为什么要分段：`_bigrams` 是滑窗，直接切「胖东来 企业」会造出
    「来 企」这种跨词的假片段——既拉低了真实记忆的匹配率（命中数被
    假片段稀释），也可能撞上别处的巧合。按空格断开，每个词内部才
    产生 bigram，正好对应「她用空格分隔关键词」的写法。
    """
    grams: set[str] = set()
    for token in re.split(r"[\s\W_]+", query):
        if not token:
            continue
        if len(token) == 1:
            grams.add(token)          # 一个字切不出 bigram，就用这个字本身
        else:
            grams |= _bigrams(token)
    return grams


def _hits(content: str, tags: str, q_grams: set[str]) -> int:
    """查询片段与这条记忆命中了几个。"""
    if not q_grams:
        return 0
    return len(q_grams & _text_grams(content + tags))


def _min_hits(n_query_grams: int) -> int:
    """「沾边」的最低命中数：命中的片段太少就当没搜到。

    两个条件都要满足，因为单看一个都不可靠：
      - 比例（RECALL_MIN_MATCH）：防止她把一整句话当查询时，靠
        「什么」「自己」这类高频两字组蒙到一堆不相干的东西；
      - 绝对数（RECALL_MIN_HITS）：查询很短时（「自己选」只有 2 个
        bigram），比例再高也说明不了什么，至少得两侧片段都撞上。
    """
    need = max(config.RECALL_MIN_HITS, math.ceil(config.RECALL_MIN_MATCH * n_query_grams))
    return min(need, n_query_grams)   # 查询本身就短时不能要求超过它的片段数


def search(conn: sqlite3.Connection, limit: int = 5,
           query: str = "", now=None) -> list[sqlite3.Row]:
    """情景记忆检索。

    score = 0.45*recency + 0.35*salience + 0.20*tag_match
    权重的意思：时间新鲜最重要（上下文要接得上），其次是当时情绪冲击
    有多大，最后是跟当前话题像不像。顺序换过好几版，这是当前定稿。

    带 query 时先过一道相关性闸门（见 _min_hits）：一条都没沾上的记忆
    直接出局，不让 pinned / salience / recency 把它顶到第一位。
    2026-11-10 的教训——搜「胖东来 企业 原则」，排第一的是两条空跑
    测试日记：它们一句都不相关，但置顶 + 当天时间足够刷到榜首。
    """
    now = now or util.now()
    q_grams = _query_grams(query)
    need = _min_hits(len(q_grams))
    if query.strip() and not q_grams:
        # 查询里切不出任何片段（只有标点/符号）——它匹配不上任何东西，
        # 这时放回「最新几条」等于又犯了一次「不相关也端上来」的错。
        return []

    rows = conn.execute(
        "SELECT * FROM memories WHERE layer = 'episodic' ORDER BY ts DESC LIMIT 500"
    ).fetchall()

    scored = []
    for r in rows:
        h = _hits(r["content"], r["tags"] or "", q_grams)
        if q_grams and h < need:
            continue
        match = min(h / len(q_grams), 1.0) if q_grams else 0.0
        pinned_bonus = 0.05 if r["pinned"] else 0.0   # 日记是她主动留下的，加权
        score = (
            0.45 * _recency(r["ts"], now)
            + 0.35 * r["salience"]
            + 0.20 * match
            + pinned_bonus
        )
        scored.append((score, r))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [r for _, r in scored[:limit]]


def recent_events(conn: sqlite3.Connection, limit: int = 2) -> list[sqlite3.Row]:
    """最近几次唤醒的摘要（做事和空转都算），带上当时的「事后想法」。

    heartbeat 也要进来：她「选择不动」同样是一种选择，天亮后的自己
    应该知道凌晨的自己发过呆。kind='event' 的旧过滤是错的。
    reflection 是反应阶段她自己写下的记忆，靠 source_event_id 挂回
    对应的唤醒——她如何解读自己的行为，不该依赖检索碰运气。
    """
    return conn.execute(
        """SELECT e.ts, e.tool, e.result_summary, e.why,
                  m.id AS reflection_id, m.content AS reflection
           FROM events e
           LEFT JOIN memories m
                  ON m.source_event_id = e.id AND m.layer = 'episodic'
           WHERE e.kind IN ('event', 'heartbeat')
           ORDER BY e.ts DESC, e.id DESC LIMIT ?""",
        (limit,),
    ).fetchall()
