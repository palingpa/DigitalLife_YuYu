"""通信模块（小帕 ↔ 鱼鱼）的回归测试，2026-11-10 新增。

覆盖四件事：
1. fish/messages.py 薄消息层的增删查标记；
2. 【小帕】区块只在「有未读」那轮渲染，且「渲染即 seen」= 同一封来信只落一条记忆；
3. send_message 是单段工具：不触发第二段、只落一条「给小帕」记忆、并投递 from_yuyu 消息；
4. _should_wake 因外部消息唤醒（受最小间隔约束），且收到消息本身不改鱼鱼状态。

设计红线回顾（详见 DEVLOG）：
- send_message 的 desc / args_hint 措辞、PERSONA 第 19 行、【小帕】区块正文，
  都属于注入鱼鱼的提示词，按分工红线由小帕本人定稿；本测试只锁「契约性质」
  （必须有、必须单段、必须落记忆），不锁定具体字词，避免 Kite 越界代笔。
"""

import asyncio
from datetime import timedelta

from fish import config, db, scheduler, state, prompt, tools, util
from fish.brain import Decision, Reaction


def _conn(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "comm.db")
    c = db.connect()
    db.init_state(c)
    return c


# ── 1. 消息层 ────────────────────────────────────────────────────────────

def test_messages_post_and_seen(tmp_path, monkeypatch):
    from fish import messages as msg_mod
    c = _conn(tmp_path, monkeypatch)
    mid = msg_mod.post(c, "to_yuyu", "在吗？")
    assert mid is not None
    assert msg_mod.has_unseen_to_yuyu(c) is True

    unseen = msg_mod.fetch_unseen_to_yuyu(c)
    assert len(unseen) == 1 and unseen[0]["text"] == "在吗？"

    msg_mod.mark_seen(c, mid)
    assert msg_mod.has_unseen_to_yuyu(c) is False


def test_messages_empty_text_not_written(tmp_path, monkeypatch):
    from fish import messages as msg_mod
    c = _conn(tmp_path, monkeypatch)
    assert msg_mod.post(c, "to_yuyu", "   ") is None
    assert msg_mod.has_unseen_to_yuyu(c) is False


def test_from_yuyu_delivery_flag(tmp_path, monkeypatch):
    from fish import messages as msg_mod
    c = _conn(tmp_path, monkeypatch)
    msg_mod.post(c, "from_yuyu", "这是一条测试消息")
    undelivered = msg_mod.fetch_undelivered_from_yuyu(c)
    assert len(undelivered) == 1
    msg_mod.mark_delivered(c, undelivered[0]["id"])
    assert msg_mod.fetch_undelivered_from_yuyu(c) == []


# ── 2. 【小帕】区块：仅未读那轮渲染 + 渲染即 seen ───────────────────────

def test_xiaopa_block_renders_once_and_persists_memory(tmp_path, monkeypatch):
    from fish import messages as msg_mod
    c = _conn(tmp_path, monkeypatch)
    msg_mod.post(c, "to_yuyu", "这是一条测试消息")

    now = util.now()
    msgs, _ = prompt.build_messages(c, state.load(c), now)
    content = msgs[-1]["content"]
    assert "【小帕】" in content
    assert "这是一条测试消息" in content

    # 落了一条「小帕来信」记忆，tag 正确
    rows = c.execute(
        "SELECT content, tags FROM memories WHERE tags=?",
        (config.XIAOPA_MSG_TAG,),
    ).fetchall()
    assert len(rows) == 1
    assert "这是一条测试消息" in rows[0]["content"]

    # 渲染即 seen：下一轮不再渲染、不再加记忆（同封来信不落两条）
    assert not msg_mod.has_unseen_to_yuyu(c)
    msgs2, _ = prompt.build_messages(c, state.load(c), now)
    assert "【小帕】" not in msgs2[-1]["content"]
    n = c.execute(
        "SELECT COUNT(*) AS n FROM memories WHERE tags=?",
        (config.XIAOPA_MSG_TAG,),
    ).fetchone()["n"]
    assert n == 1, "同一封来信不能因被渲染两次而落两条记忆（撞墙回归）"


# ── 3. send_message 单段 + 落记忆 + 投递 ─────────────────────────────────

def test_send_message_single_pass_and_delivers(tmp_path, monkeypatch):
    c = _conn(tmp_path, monkeypatch)
    calls = {"react": 0}

    def fake_decide(client, messages, tid):
        return Decision(
            action="tool", tool="send_message",
            args={"text": "这是一条测试消息"},
            felt_delta={"energy": 0, "valence": 0},
            why="想告诉小帕", next_wake_in_min=30,
        )

    def fake_react(client, messages, d, tool, summary, detail, tid):
        calls["react"] += 1
        return Reaction(felt_delta={"valence": -2}, memory="回应自己的话", parse_ok=True)

    monkeypatch.setattr("fish.brain.decide", fake_decide)
    monkeypatch.setattr("fish.brain.react", fake_react)

    before_v = c.execute("SELECT valence FROM state").fetchone()[0]
    asyncio.run(scheduler.wake(c, None, trigger="test"))
    after_v = c.execute("SELECT valence FROM state").fetchone()[0]

    # 1) 单段：没有第二段「回应自己」的调用
    assert calls["react"] == 0

    # 2) 只多一条记忆，是她主动给小帕的留言（不是第二段反应记忆）
    mems = c.execute("SELECT content, tags FROM memories").fetchall()
    assert len(mems) == 1
    assert mems[0]["content"].startswith("[给小帕]")
    assert mems[0]["tags"] == config.XIAOPA_MSG_TAG

    # 3) from_yuyu 消息已投递，供小帕收件脚本取
    froms = c.execute("SELECT text, direction FROM messages").fetchall()
    assert len(froms) == 1
    assert froms[0]["direction"] == "from_yuyu"
    assert froms[0]["text"] == "这是一条测试消息"

    # 4) valence 只受工具效果 +2（第一段 felt_delta 为 0，无第二段自报）
    assert after_v == before_v + 2.0


# ── 4. _should_wake：外部消息唤醒 + 最小间隔 ─────────────────────────────

def test_should_wake_xiaopa_when_pending_and_gap_ok(tmp_path, monkeypatch):
    from fish import messages as msg_mod
    c = _conn(tmp_path, monkeypatch)
    msg_mod.post(c, "to_yuyu", "这是一条测试消息")
    go, trig = scheduler._should_wake(c, state.load(c), util.now())
    assert go and trig == "xiaopa"


def test_should_wake_xiaopa_suppressed_within_min_gap(tmp_path, monkeypatch):
    from fish import messages as msg_mod
    c = _conn(tmp_path, monkeypatch)
    msg_mod.post(c, "to_yuyu", "这是一条测试消息")
    # 一条 1 分钟前、设定 60 分钟后才醒的事件 → 未满 2 分钟最小间隔
    recent = (util.now() - timedelta(minutes=1)).isoformat(timespec="seconds")
    c.execute(
        "INSERT INTO events (ts, kind, trigger, tool, why, next_wake_at) "
        "VALUES (?, 'event', 'wake', 'read_feed', '测试', ?)",
        (recent, (util.now() + timedelta(minutes=60)).isoformat(timespec="seconds")),
    )
    c.commit()
    go, trig = scheduler._should_wake(c, state.load(c), util.now())
    assert not go, "未满最小间隔时，外部消息不应立刻触发唤醒"


def test_receiving_message_does_not_change_state(tmp_path, monkeypatch):
    """收到消息这件事本身不改状态（无 boredom 缓解、无强制情绪）。"""
    from fish import messages as msg_mod
    c = _conn(tmp_path, monkeypatch)
    msg_mod.post(c, "to_yuyu", "这是一条测试消息")

    def fake_decide(client, messages, tid):
        # 空转：不做事，felt_delta 为 0
        return Decision(action="none", tool=None, args={},
                        felt_delta={"energy": 0, "valence": 0},
                        why="这是一条测试原因", next_wake_in_min=30)

    monkeypatch.setattr("fish.brain.decide", fake_decide)

    asyncio.run(scheduler.wake(c, None, trigger="test"))

    # 状态应停在 baseline（init_state：energy55/valence50/boredom30）
    row = c.execute("SELECT energy, valence, boredom FROM state").fetchone()
    assert row["energy"] == 55.0 and row["valence"] == 50.0 and row["boredom"] == 30.0

    # 但「小帕来信」这件事确实被存档为记忆、且已标记 seen
    assert c.execute(
        "SELECT COUNT(*) AS n FROM memories WHERE tags=?",
        (config.XIAOPA_MSG_TAG,),
    ).fetchone()["n"] == 1
    assert not msg_mod.has_unseen_to_yuyu(c)


# ── 5. DRY_RUN 短路：只观察、不执行 ─────────────────────────────────────

def test_dry_run_wake_has_no_side_effects(tmp_path, monkeypatch):
    """空跑模式必须真的「零副作用」。

    2026-11-11 的 bug：一个开 DRY_RUN 的进程仍走 dispatch + commit，
    导致「假消息真发出去」、并产生跨进程的孤儿轨迹（jsonl 有 tool_result
    却无对应 decision、且数据库里什么都没落）。

    本测试验证 DRY_RUN 下 wake() 既不 dispatch 工具、也不写事件、不消费来信、
    不新增记忆、不动状态库——只把提示词写到 logs/last_prompt.txt 供观察。
    """
    from fish import messages as msg_mod

    monkeypatch.setattr(config, "DRY_RUN", True)
    c = _conn(tmp_path, monkeypatch)

    # 塞一封未读来信：若 build_messages 不守 dry_run，它会被 mark_seen + 落记忆
    mid = msg_mod.post(c, "to_yuyu", "这是一条测试消息")

    before = {
        "wake_count": db.wake_count(c),
        "memories": c.execute("SELECT COUNT(*) AS n FROM memories").fetchone()["n"],
        "from_yuyu": c.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE direction='from_yuyu'"
        ).fetchone()["n"],
        "state": c.execute("SELECT energy, valence, boredom FROM state").fetchone(),
    }

    asyncio.run(scheduler.wake(c, None, trigger="test"))

    after = {
        "wake_count": db.wake_count(c),
        "memories": c.execute("SELECT COUNT(*) AS n FROM memories").fetchone()["n"],
        "from_yuyu": c.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE direction='from_yuyu'"
        ).fetchone()["n"],
        "state": c.execute("SELECT energy, valence, boredom FROM state").fetchone(),
    }

    # 事件计数不变：DRY_RUN 不插入 event
    assert after["wake_count"] == before["wake_count"] == 0
    # 没新增任何记忆（来信未被「渲染即 seen」落库）
    assert after["memories"] == before["memories"]
    # send_message 未被 dispatch（没有 from_yuyu 投递）
    assert after["from_yuyu"] == before["from_yuyu"] == 0
    # 状态库完全没动
    assert after["state"] == before["state"]
    # 关键：来信仍未被消费——保持未读，下一轮真实唤醒还能看到
    assert msg_mod.has_unseen_to_yuyu(c) is True
    assert msg_mod.fetch_unseen_to_yuyu(c)[0]["id"] == mid

    # 观察产物：提示词里确实渲染了【小帕】区块（空跑时也能看到会怎么呈现）
    log_path = config.LOGS_DIR / "last_prompt.txt"
    assert log_path.exists()
    assert "这是一条测试消息" in log_path.read_text(encoding="utf-8")
