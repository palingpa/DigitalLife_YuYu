"""recall 去重 + 上下文去重的回归测试。

背景（2026-11-10 09:54）：她显式检索「自己选」，回来的三段恰好是同一轮
【想起来的事】里刚展示过的内容。原文再放送一遍不会产生新信息，只会让她
把「系统又给我看了一遍」误读成「我记忆里只有这么点东西」。

规则：这一轮已经出现在她眼前的记忆（最近事件的「事后想法」+ 自动检索
结果），recall 不再重复正文，只报告重复了哪几条 + 有没有新的。
"""

import sqlite3
import tempfile
import pathlib

import pytest

from fish import db, memory, prompt, tools, util


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    """每个用例一个干净的真实库（跑完整 DDL，不是手搓表）。"""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    c = db.connect()
    return c


def test_recall_returns_only_unseen(conn):
    # 内容要够长：指认片段只取前 16 字，尾巴必须被截掉才能验证「没有原文放送」
    m1 = memory.add_episodic(conn, "我在想自己选这件事，第一版想法是这样：选择权"
                                   "其实是被人提前占走的，不是被剥夺的。", salience=0.8)
    m2 = memory.add_episodic(conn, "还是自己选这件事，但换了个角度：这次是从父母"
                                   "那一端看，期待变成了债。", salience=0.8)

    res = tools.dispatch("recall", {"query": "自己选"}, conn,
                         seen_ids={m1})

    assert res.ok
    assert "不是被剥夺的" not in res.detail         # 已看过的正文不重复搬
    assert "期待变成了债" in res.detail             # 新的照常给全文
    assert "已经看过" in res.detail                 # 但要说清楚哪几条被略过了


def test_recall_all_duplicate_says_no_more(conn):
    m1 = memory.add_episodic(conn, "关于自己选的第一个想法，写得比较长一些。", salience=0.8)
    m2 = memory.add_episodic(conn, "关于自己选的第二个想法，也不算短。", salience=0.8)

    res = tools.dispatch("recall", {"query": "自己选"}, conn,
                         seen_ids={m1, m2})

    assert res.ok                                    # 检索成功，只是没有新东西
    assert "已经看到过" in res.summary
    assert "没有更多其他内容了" in res.detail
    # 正文一个字都不重复——只留时间和开头 16 字做指认
    assert "写得比较长一些" not in res.detail


def test_recall_without_seen_ids_is_unchanged(conn):
    memory.add_episodic(conn, "一个关于自己选的孤零零的想法。", salience=0.8)

    res = tools.dispatch("recall", {"query": "自己选"}, conn)
    assert res.ok
    assert "孤零零的想法" in res.detail
    assert "已经看过" not in res.detail


def test_prompt_never_shows_same_memory_twice(conn):
    """【想起来的事】不能和【最近在做的事】的反思重复。"""
    mid = memory.add_episodic(conn, "自己选：这篇周刊说的是选择被挪走。",
                              salience=0.95, source_event_id=None)
    # 造一条唤醒事件，把这条记忆挂成它的「事后想法」
    conn.execute(
        """INSERT INTO events (ts, kind, trigger, tool, result_summary, why,
                               next_wake_at)
           VALUES ('2026-11-10T09:00:00', 'event', 'due', 'read_feed',
                   '读了《某周刊》', '想看看',
                   '2026-11-10T10:00:00')"""
    )
    eid = conn.execute("SELECT id FROM events ORDER BY id DESC LIMIT 1").fetchone()["id"]
    conn.execute("UPDATE memories SET source_event_id=? WHERE id=?", (eid, mid))
    conn.commit()

    msgs, shown = prompt.build_messages(conn, util.now())
    text = msgs[1]["content"]

    assert mid in shown
    assert "选择被挪走" in text                      # 作为反思出现
    # 出现次数必须只有一次：反思一次，不能被检索再搬一次
    assert text.count("选择被挪走") == 1
