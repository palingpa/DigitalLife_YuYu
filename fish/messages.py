"""小帕 ↔ 鱼鱼 之间的薄消息层（通信模块，2026-11-10 新增）。

设计红线（用户拍板）：
- 【小帕】区块只在「有未读 to_yuyu 消息」的那一轮渲染，绝不常驻——
  常驻会过度强化「小帕」的存在感，抬高鱼鱼主动给她发消息的概率。
- 渲染即 seen：消息在 prompt.build_messages 里被读出来展示的那一刻就标记
  seen（见 prompt.py）。本文件只负责落库与读写，不决定何时展示。
- 收到消息不改鱼鱼状态、不加 boredom 缓解；回信（send_message 工具）走
  标准单段流程，valence+2 / energy-1（见 state.apply_tool_effect）。
- 以后接聊天软件（微信/邮件…）只是新增一个「适配器」：适配器负责把外部
  消息转成 post("to_yuyu", text)、把 from_yuyu 转发出去。本文件是唯一的
  消息真相源，适配器不动这里。

消息只有两张流向：
  to_yuyu   ：小帕 → 鱼鱼（小帕用脚本 post，下一 tick 自动被发现唤醒）
  from_yuyu ：鱼鱼 → 小帕（send_message 工具 post，小帕用脚本收）
"""

import sqlite3

from . import config, util


def post(conn: sqlite3.Connection, direction: str, text: str,
         trace_id: str = "") -> int | None:
    """写一条消息。direction ∈ {"to_yuyu", "from_yuyu"}。

    返回新插入行的 id；text 为空（全是空白）时不写，返回 None。
    """
    text = (text or "").strip()
    if not text:
        return None
    cur = conn.execute(
        """INSERT INTO messages (ts, direction, text, wake_trace_id)
           VALUES (?, ?, ?, ?)""",
        (util.now_iso(), direction, text, trace_id or None),
    )
    conn.commit()
    return cur.lastrowid


def has_unseen_to_yuyu(conn: sqlite3.Connection) -> bool:
    """是否还有小帕发来、鱼鱼没看过的消息。

    _should_wake 用它决定是否因外部消息唤醒；prompt.build_messages 用它
    决定是否渲染【小帕】区块。两边共用同一个判据，语义不会漂移。
    """
    row = conn.execute(
        "SELECT 1 FROM messages WHERE direction='to_yuyu' AND seen_by_yuyu=0 LIMIT 1"
    ).fetchone()
    return row is not None


def fetch_unseen_to_yuyu(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """取出所有未读 to_yuyu 消息，按时间升序（老信在前）。"""
    return conn.execute(
        "SELECT * FROM messages WHERE direction='to_yuyu' AND seen_by_yuyu=0 "
        "ORDER BY ts ASC, id ASC"
    ).fetchall()


def mark_seen(conn: sqlite3.Connection, msg_id: int) -> None:
    """标记某条 to_yuyu 消息已被鱼鱼看到（渲染即 seen 的执行动作）。"""
    conn.execute("UPDATE messages SET seen_by_yuyu=1 WHERE id=?", (msg_id,))
    conn.commit()


def fetch_undelivered_from_yuyu(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """小帕收件：取出所有鱼鱼发出、但还没标记送达的消息，按时间升序。"""
    return conn.execute(
        "SELECT * FROM messages WHERE direction='from_yuyu' AND delivered_to_xiaopa=0 "
        "ORDER BY ts ASC, id ASC"
    ).fetchall()


def mark_delivered(conn: sqlite3.Connection, msg_id: int) -> None:
    """标记某条 from_yuyu 消息已送达小帕（收件脚本执行）。"""
    conn.execute("UPDATE messages SET delivered_to_xiaopa=1 WHERE id=?", (msg_id,))
    conn.commit()


def recent_exchanges(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    """取最近若干条双向往来（to_yuyu + from_yuyu），按时间升序（老信在前）。

    用途：自省时作为「你和小帕之间发生过什么」的素材（grounding），
    让她据此归纳关系，而不是凭空编。只取、不判，是否采用由自省逻辑决定。
    """
    rows = conn.execute(
        "SELECT * FROM messages ORDER BY ts DESC, id DESC LIMIT ?", (limit,)
    ).fetchall()
    return list(reversed(rows))   # 转回时间升序，读起来自然


def format_exchanges(rows: list[sqlite3.Row]) -> str:
    """把往来行拼成给模型看的纯文本。

    to_yuyu（小帕→鱼鱼）标注为「小帕」，from_yuyu（鱼鱼→小帕）标注为「鱼鱼」。
    无内容时返回空串，调用方据此决定是否注入。
    """
    if not rows:
        return ""
    out = []
    for r in rows:
        who = "小帕" if r["direction"] == "to_yuyu" else "鱼鱼"
        out.append(f"[{who}] {r['text']}")
    return "\n".join(out)
