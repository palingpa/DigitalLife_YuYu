"""回归：反应阶段的「事后想法」不能被读成「写日记」。

2026-11-10 14:42 那轮，她在【最近在做的事】里看到一条被标成
「事后你记下：「…」」的反应反思，于是推断「写日记刚写过两小时内的」，
降低了使用 write_journal 工具的意愿。反思是系统自动存下的「当时心里怎么想」，
和「主动写日记」是两回事。这个测试锁住：标签不再用「记下」，
且写日记工具描述指向「主动写日记」行为（具体措辞归小帕，红线不代笔）。
"""

import pytest
from datetime import timedelta

from fish import db, state, prompt, tools, util, config


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


def test_write_journal_desc_refers_to_diary(conn):
    """write_journal 的工具描述必须指向「写日记」这个她主动的行为。

    注意：具体措辞（是否强调「主动」、是否点明「区别于系统自动记的事后想法」）
    属于会注入给鱼鱼的提示词，按分工红线由小帕本人定稿，这里只锁契约性质——
    描述必须存在、且明确指向「写日记」这件事，不锁定具体字词（否则 Kite 越界代笔）。
    """
    desc = next(t.desc for t in tools.ALL_TOOLS if t.name == "write_journal")
    assert desc and "日记" in desc, "工具说明必须指向「写日记」这一行为"


def test_self_review_last_time_shows_in_whoami(conn):
    """【你是谁】板块应渲染「上次自省」时间，复用 _relative_time 的「x小时前」质感。

    鱼鱼实际观察到：她一直觉得自己欠着一条自省没做，其实已经做过了——
    只是没有「上次自省是什么时候」的反馈。这条渲染就是给她一个锚点。
    标签/无记录文案走 config 常量（【小帕定稿】），测试只锁契约性质，
    不锁定具体字词，否则 Kite 越界代笔。
    """
    now = util.now()

    # 没有任何 self_review 事件 → 显示「还没做过」占位（文案归小帕）
    msgs, _ = prompt.build_messages(conn, state.load(conn), now)
    content = msgs[-1]["content"]
    assert "【你是谁】" in content
    assert config.SELF_REVIEW_LAST_NONE in content, "无自省记录时应显示占位提示"

    # 插一条 3 小时前的自省事件 → 渲染应出现「3 小时前」式相对时间
    ts_3h_ago = (now - timedelta(hours=3)).isoformat(timespec="seconds")
    conn.execute(
        """INSERT INTO events (ts, kind, trigger, why, trace_id)
           VALUES (?, 'self_review', 'self_review', '自我叙事已更新', 'test-trace')""",
        (ts_3h_ago,),
    )
    conn.commit()

    msgs2, _ = prompt.build_messages(conn, state.load(conn), now)
    content2 = msgs2[-1]["content"]
    # 标签常量 + 相对时间拼在一起：小帕改文案后这行仍成立
    assert config.SELF_REVIEW_LAST_LABEL + "3 小时前" in content2, \
        "有自省记录时应渲染「上次自省标签 + x小时前」"
