"""自省 grounding 用的往来检索/格式化（2026-11-11 新增）。

messages.recent_exchanges 从 messages 表取近期双向往来，
messages.format_exchanges 把它拼成「[小帕]/[鱼鱼]」文本喂给自省。
这个数据是关系叙事的「素材」，保证她归纳关系时有据可依、不凭空编。
"""
import sqlite3

from fish import db, messages


def _tmp_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    db._migrate(conn)
    return conn


def test_recent_exchanges_chrono_order_and_directions():
    conn = _tmp_db()
    messages.post(conn, "to_yuyu", "小帕的第一句")
    messages.post(conn, "from_yuyu", "鱼鱼的回复")
    messages.post(conn, "to_yuyu", "小帕的第二句")
    rows = messages.recent_exchanges(conn, limit=20)
    assert [r["text"] for r in rows] == [
        "小帕的第一句", "鱼鱼的回复", "小帕的第二句",
    ]
    # 方向正确：to_yuyu=小帕发出，from_yuyu=鱼鱼发出
    assert rows[0]["direction"] == "to_yuyu"
    assert rows[1]["direction"] == "from_yuyu"


def test_recent_exchanges_respects_limit():
    conn = _tmp_db()
    for i in range(5):
        messages.post(conn, "to_yuyu", f"msg{i}")
    rows = messages.recent_exchanges(conn, limit=3)
    assert len(rows) == 3
    # 取的是最近 3 条，按升序即 msg2,msg3,msg4
    assert [r["text"] for r in rows] == ["msg2", "msg3", "msg4"]


def test_format_exchanges_labels_and_empty():
    conn = _tmp_db()
    assert messages.format_exchanges([]) == ""
    messages.post(conn, "to_yuyu", "你好")
    messages.post(conn, "from_yuyu", "在的")
    text = messages.format_exchanges(messages.recent_exchanges(conn, limit=20))
    assert "[小帕] 你好" in text
    assert "[鱼鱼] 在的" in text
