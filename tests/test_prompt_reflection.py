"""回归：反应阶段的「事后想法」不能被读成「写日记」。

2026-11-10 14:42 那轮，她在【最近在做的事】里看到一条被标成
「事后你记下：「…」」的反应反思，于是推断「写日记刚写过两小时内的」，
降低了使用 write_journal 工具的意愿。反思是系统自动存下的「当时心里怎么想」，
和「主动写日记」是两回事。这个测试锁住：标签不再用「记下」，
且 write_journal 的工具说明自己点明了区别。
"""

import pytest

from fish import db, state, prompt, tools, util


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    c = db.connect()
    db.init_state(c)  # schema（含 memories 表）；core/relation 叙事缺失时
                     # build_messages 会自己用占位符兜底
    return c


def test_reflection_label_is_thought_not_journal(conn):
    """reaction 想法渲染成「你事后心里想」，而不是「事后你记下」。"""
    now = util.now()

    # 一条已完成的事件 + 它挂回的反思记忆
    conn.execute(
        """INSERT INTO events
           (ts, kind, trigger, tool, result_summary, why, valence_before, valence_after,
            energy_before, energy_after, boredom_before, boredom_after,
            next_wake_at, tokens_in, tokens_out, duration_ms, parse_ok, trace_id)
           VALUES (?, 'event', 'wake', 'read_feed', '读了《某条》', '想读条真的有内容的',
                   55, 56, 57, 56, 30, 25,
                   ?, 100, 20, 1200, 1, 'test-trace')""",
        (now.isoformat(timespec="seconds"),
         now.isoformat(timespec="seconds")),
    )
    ev_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        """INSERT INTO memories (ts, layer, content, salience, tags, source_event_id)
           VALUES (?, 'episodic', '这套 inbox 给我的多是壳，我不再当成巧合了。',
                   0.6, 'read_feed', ?)""",
        (now.isoformat(timespec="seconds"), ev_id),
    )
    conn.commit()

    msgs, _ = prompt.build_messages(conn, state.load(conn), now)
    section = msgs[-1]["content"].split("【最近在做的事】")[1]

    assert "你事后心里想" in section, "反思应渲染成「你事后心里想」"
    assert "事后你记下" not in section, "不能用「记下」——她会读成写日记"
    # 反思那一行本身不能出现「写日记」，否则区分就白做了
    refl_line = section.split("你事后心里想")[1].split("\n", 1)[0]
    assert "写日记" not in refl_line, "反思标签里不能出现「写日记」"


def test_write_journal_desc_distinguishes_from_auto_reflection(conn):
    """write_journal 说明要让她分清「主动写」和「系统自动记的事后想法」。"""
    desc = next(t.desc for t in tools.ALL_TOOLS if t.name == "write_journal")
    assert "主动" in desc, "要强调这是她主动的行为"
    assert "事后想法" in desc or "自动" in desc, \
        "要点明它和每次行动后自动存下的「事后想法」不是一回事"
