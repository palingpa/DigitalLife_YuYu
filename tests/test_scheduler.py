"""调度器层的关键回归测试。

目前只覆盖 _next_due 的「闹钟行」筛选——这是 2026-11-10 夜里那个
240 分钟 bug 的直接回归：自省/错误事件没有 next_wake_at，
如果它们被当成最新事件，会把她自己定的间隔覆盖成兜底的 240 分钟。
"""

import sqlite3
from datetime import datetime, timedelta

from fish import config, scheduler


def _conn_with_events(rows: list[tuple[str, str | None, str]]) -> sqlite3.Connection:
    """造一个内存库，rows = [(ts, next_wake_at, kind)]，按顺序插入。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """CREATE TABLE events (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               ts TEXT, kind TEXT, trigger TEXT, tool TEXT, why TEXT,
               next_wake_at TEXT);"""
    )
    for ts, nxt, kind in rows:
        conn.execute(
            "INSERT INTO events (ts, kind, next_wake_at) VALUES (?, ?, ?)",
            (ts, kind, nxt),
        )
    conn.commit()
    return conn


def test_side_events_do_not_become_alarms():
    """自省行夹在中间时，下次醒点必须仍取她自己的决定。"""
    wake_ts = "2026-11-10T02:54:06"
    decided = "2026-11-10T04:09:06"          # +75min，她定的
    review_ts = "2026-11-10T02:54:11"        # 5 秒后的自省行（无闹钟）

    conn = _conn_with_events([
        (wake_ts, decided, "heartbeat"),
        (review_ts, None, "self_review"),
    ])
    now = datetime.fromisoformat(review_ts)
    due = scheduler._next_due(conn, now)
    assert due == datetime.fromisoformat(decided)


def test_corrupt_alarm_falls_back_to_hard_max():
    """闹钟字段损坏 → 从该行的 ts 起算兜底间隔，而不是立即醒。"""
    ts = "2026-11-10T02:54:06"
    conn = _conn_with_events([(ts, "not-a-date", "event")])
    due = scheduler._next_due(conn, datetime.fromisoformat(ts))
    assert due == datetime.fromisoformat(ts) + timedelta(minutes=config.HARD_MAX_GAP)


def test_no_wake_ever_means_wake_now():
    """从未唤醒过（或库里只有旁支事件）→ 立即醒。"""
    conn = _conn_with_events([("2026-11-10T02:54:11", None, "self_review")])
    now = datetime.fromisoformat("2026-11-10T09:00:00")
    assert scheduler._next_due(conn, now) == now
