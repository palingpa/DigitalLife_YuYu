"""写日记是单段工具：写完即止，不再把她刚写的日记原样回念一遍让她「回应」。

回归点（2026-11-10）：用户发现 write_journal 也走两段式，等于让她对自己的反思
再做一次反思；而且情绪会被算两次（工具效果 +3 与第二段自报的 felt_delta 叠加，
对别的工具是「内容感受 vs 费力程度」两个维度，对写日记却是同一件事）。
单段化后：不触发第二段、只落一条日记记忆、valence 仅由工具效果 +3 调整。
"""

import asyncio
import sqlite3

from fish import db, scheduler
from fish.brain import Decision, Reaction


def test_write_journal_skips_second_pass(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    c = db.connect()
    db.init_state(c)

    calls = {"react": 0}

    def fake_decide(client, messages, tid):
        # 第一段：她决定主动写一段日记
        return Decision(
            action="tool", tool="write_journal",
            args={"text": "今天有点累，但写下来心里松快了。"},
            felt_delta={"energy": 0, "valence": 0},
            why="想留下这段话", next_wake_in_min=30,
        )

    def fake_react(client, messages, d, tool, summary, detail, tid):
        calls["react"] += 1
        return Reaction(felt_delta={"valence": -2}, memory="事后元评论", parse_ok=True)

    monkeypatch.setattr("fish.brain.decide", fake_decide)
    monkeypatch.setattr("fish.brain.react", fake_react)

    before = c.execute("SELECT valence FROM state").fetchone()[0]
    asyncio.run(scheduler.wake(c, None, trigger="test"))
    after = c.execute("SELECT valence FROM state").fetchone()[0]

    # 1) 没有触发第二段（没有「回应自己的日记」那次调用）
    assert calls["react"] == 0

    # 2) 只多一条记忆，且是她主动写的、pinned 的日记
    rows = c.execute("SELECT content, pinned, tags FROM memories").fetchall()
    assert len(rows) == 1
    assert rows[0]["content"].startswith("[日记]")
    assert rows[0]["pinned"] == 1

    # 3) 只多了一条记忆，且它是日记本身（source_event_id 为 NULL），
    #    没有产生「回应自己日记」的反应记忆（反应记忆才带 source_event_id 挂接）
    journal = c.execute(
        "SELECT content, pinned, tags, source_event_id FROM memories"
    ).fetchone()
    assert journal["source_event_id"] is None

    # 4) valence 只受工具效果 +3 影响（第一段 felt_delta 为 0，无第二段自报）
    assert after == before + 3.0
