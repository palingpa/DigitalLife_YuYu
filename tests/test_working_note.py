"""工作记忆（给之后醒来的自己的提示）：三态、两段优先、TTL 硬兜底。

回归点（2026-11-12）：真机轨迹里 58 次 decision 有 16 次 reasoning 提到「下次」、
13 次是明确的分轮打算，但 reasoning 只进 logs/traces、不进 prompt，
`why` 又只写本轮理由——她下一轮根本看不到自己上轮定过什么。

这一组测试守的是设计里最容易写反的四件事：
  1. None / "" / 文本 三态不能混（不写=沿用，写空=清除，文本=覆盖）
  2. 第二段（看完结果后）优先，但解析失败那一段必须被丢弃（I2 等价纪律）
  3. 存活计数按「一次 wake」结算，不按 events 行数（自省旁支会污染计数）
  4. 归零自动删——软约束「最多安排 3 轮」模型不会可靠遵守，硬兜底必须在

所有测试占位文本一律用花括号包住，一眼可辨为占位：将来 debug 真机 jsonl
时不会把它误认成鱼鱼的真实产出。
"""

import asyncio
import json
import sqlite3

import pytest

from fish import brain, config, db, memory, prompt, scheduler, util
from fish.brain import ApiReply, Decision, Reaction
from fish.tools import ToolResult


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    """每人一个干净的假库，永远不碰 data/fish.db 里鱼鱼的真实记忆。"""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    return db.connect()


def _reply(obj) -> ApiReply:
    """把 dict 伪装成一次 API 回复，喂给 _parse_* 纯函数。"""
    return ApiReply(content=json.dumps(obj, ensure_ascii=False),
                    reasoning="", tokens=(0, 0), duration_ms=0)


# ── 1. 三态校验（brain.normalize_note）───────────────────────────────────

def test_normalize_note_three_states():
    assert brain.normalize_note(None) is None          # 字段没写
    assert brain.normalize_note(123) is None           # 类型不对，当没写
    assert brain.normalize_note(["{测试}"]) is None     # 数组也不算
    assert brain.normalize_note("") == ""              # 明确不留，不能和 None 合并
    assert brain.normalize_note("   ") == ""           # 纯空白也当「不留」
    assert brain.normalize_note("  {这是一条测试提示}  ") == "{这是一条测试提示}"


def test_normalize_note_rejects_too_long_without_truncating():
    """超长要拒绝，不能截断：截断会留半句话，她下次会以为自己当初就写了半句。"""
    at_limit = "字" * config.WORKING_NOTE_MAX_CHARS
    assert brain.normalize_note(at_limit) == at_limit
    assert brain.normalize_note(at_limit + "字") is None


def test_resolve_note_second_pass_wins():
    """第二段优先；但第二段「没写」（None）才回落，空字符串是明确的清除。"""
    assert brain.resolve_note("{第一段}", None) == "{第一段}"
    assert brain.resolve_note("{第一段}", "{第二段}") == "{第二段}"
    assert brain.resolve_note("{第一段}", "") == ""
    assert brain.resolve_note(None, "{第二段}") == "{第二段}"
    assert brain.resolve_note(None, None) is None


# ── 2. 单条槽存取与 TTL ──────────────────────────────────────────────────

def test_set_get_clear_is_single_row(conn):
    assert memory.get_working_note(conn) is None
    memory.set_working_note(conn, "{第一条测试提示}", born_event_id=7)
    memory.set_working_note(conn, "{第二条测试提示}")   # 单条覆盖语义
    assert conn.execute("SELECT COUNT(*) AS n FROM working_note").fetchone()["n"] == 1
    row = memory.get_working_note(conn)
    assert row["content"] == "{第二条测试提示}"
    assert row["remaining_wakes"] == config.WORKING_NOTE_TTL
    assert row["born_event_id"] is None
    memory.clear_working_note(conn)
    assert memory.get_working_note(conn) is None


def test_note_dies_after_three_wakes(conn):
    """写下后最多被之后 3 次 wake 读到，第 3 次结算时自动删除（硬兜底）。"""
    memory.set_working_note(conn, "{这是一条测试提示}")
    seen = []
    for _ in range(config.WORKING_NOTE_TTL):
        row = memory.get_working_note(conn)
        seen.append(row["remaining_wakes"] if row else None)
        memory.settle_working_note(conn, None)       # 这轮没写新提示
    assert seen == [3, 2, 1]
    assert memory.get_working_note(conn) is None
    # 已经删掉后再结算也不该复活 / 报错
    memory.settle_working_note(conn, None)
    assert memory.get_working_note(conn) is None


def test_rewrite_resets_the_clock(conn):
    memory.set_working_note(conn, "{旧测试提示}")
    memory.settle_working_note(conn, None)
    assert memory.get_working_note(conn)["remaining_wakes"] == 2
    memory.settle_working_note(conn, "{新测试提示}")
    row = memory.get_working_note(conn)
    assert row["content"] == "{新测试提示}"
    assert row["remaining_wakes"] == config.WORKING_NOTE_TTL


def test_explicit_empty_clears_immediately(conn):
    memory.set_working_note(conn, "{这是一条测试提示}")
    memory.settle_working_note(conn, "")             # 「还是不留提示」
    assert memory.get_working_note(conn) is None


def test_side_events_do_not_consume_a_wake(conn):
    """结算只认一次 wake，不认 events 行数。

    真库 64 条 events 里 9 条是 self_review 旁支；若用 db.wake_count
    （COUNT(*) FROM events）推算存活，提示会被凭空提前杀掉。
    """
    memory.set_working_note(conn, "{这是一条测试提示}")
    for _ in range(5):
        conn.execute(
            "INSERT INTO events (ts, kind, trigger, why) VALUES (?, 'self_review', 'test', '{旁支}')",
            (util.now_iso(),),
        )
    conn.commit()
    memory.settle_working_note(conn, None)
    assert memory.get_working_note(conn)["remaining_wakes"] == config.WORKING_NOTE_TTL - 1


# ── 3. 解析：两段都能读字段，坏输出绝不往下传 ────────────────────────────

def test_parse_decision_reads_and_clamps_note():
    d = brain._parse_decision(_reply({
        "action": "none", "why": "{测试理由}", "next_wake_in_min": 30,
        "note_for_next": "{这是一条测试提示}",
    }))
    assert d.note_for_next == "{这是一条测试提示}"

    too_long = brain._parse_decision(_reply({
        "action": "none", "next_wake_in_min": 30,
        "note_for_next": "字" * (config.WORKING_NOTE_MAX_CHARS + 1),
    }))
    assert too_long.note_for_next is None

    missing = brain._parse_decision(_reply({"action": "none", "next_wake_in_min": 30}))
    assert missing.note_for_next is None


def test_parse_reaction_reads_note():
    r = brain._parse_reaction(_reply({"memory": "{测试记忆}", "note_for_next": "{第二段测试提示}"}))
    assert r.note_for_next == "{第二段测试提示}"
    empty = brain._parse_reaction(_reply({"memory": "{测试记忆}", "note_for_next": ""}))
    assert empty.note_for_next == ""
    assert brain._parse_reaction(_reply({"note_for_next": {}})).note_for_next is None


def test_parse_failure_never_leaks_a_note():
    """JSON 坏掉时强制空转，绝不能把坏文本当成留给下一轮的提示（I2）。"""
    bad = ApiReply(content="{这不是合法 JSON", reasoning="", tokens=(0, 0), duration_ms=0)
    d = brain._parse_decision(bad)
    assert d.parse_ok is False
    assert d.note_for_next is None
    r = brain._parse_reaction(bad)
    assert r.parse_ok is False
    assert r.note_for_next is None


def test_two_bad_replies_degrade_to_empty_wake(monkeypatch):
    """两次解析都失败 → 强制空转，不抛异常、不带任何提示（I2 等价纪律）。

    顺带守住 2026-11-12 照出来的既有 bug：降级分支构造 Decision 时漏了
    必填的 tool，导致「坏 JSON → 降温重试」这条路其实是 TypeError。
    """
    bad = ApiReply(content="{这不是合法 JSON", reasoning="", tokens=(1, 1), duration_ms=0)
    monkeypatch.setattr(brain, "_call_api", lambda *a, **kw: bad)
    d = brain.decide(None, [{"role": "user", "content": "{测试上下文}"}], "test-tid")
    assert d.parse_ok is False
    assert d.action == "none"
    assert d.tool is None
    assert d.note_for_next is None


# ── 4. 集成：真的跑一次 scheduler.wake ───────────────────────────────────

def _run_wake(conn, monkeypatch, *, note_first=None, note_second=None,
              second_pass=False, reaction_parse_ok=True):
    """跑一次完整 wake，返回最新一条 events 行。

    second_pass=True 时用假的 tools.dispatch 造出带 detail 的结果，
    这样调度器会真的走第二段（判断依据是「有没有可消化内容」，
    不是选了哪个工具，见 scheduler.wake 的两段式分叉点）。
    """
    def fake_decide(client, messages, tid):
        return Decision(
            action="tool" if second_pass else "none",
            tool="read_feed" if second_pass else None,
            args={}, why="{测试理由}", note_for_next=note_first,
            next_wake_in_min=30, raw="{}", parse_ok=True,
        )

    monkeypatch.setattr("fish.brain.decide", fake_decide)

    if second_pass:
        monkeypatch.setattr(
            "fish.tools.dispatch",
            lambda *a, **kw: ToolResult(ok=True, summary="{测试摘要}", detail="{测试正文}"),
        )

        def fake_react(client, messages, d, tool, summary, detail, tid):
            return Reaction(memory="{测试记忆}", note_for_next=note_second,
                            parse_ok=reaction_parse_ok, raw="{}")

        monkeypatch.setattr("fish.brain.react", fake_react)

    asyncio.run(scheduler.wake(conn, None, trigger="test"))
    return conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT 1").fetchone()


def test_wake_writes_note_and_next_round_reads_it(conn, monkeypatch):
    row = _run_wake(conn, monkeypatch, note_first="{这是一条测试提示}")
    assert row["note_for_next"] == "{这是一条测试提示}"
    assert row["kind"] == "heartbeat", "空转轮也允许留提示：工作记忆不受 I1 管辖"
    stored = memory.get_working_note(conn)
    assert stored["content"] == "{这是一条测试提示}"
    assert stored["remaining_wakes"] == config.WORKING_NOTE_TTL
    assert stored["born_event_id"] == row["id"]

    # 下一轮：这个提示会被读回给她；用 config 常量断言，小帕改文案测试不会红
    msgs, _ = prompt.build_messages(conn, util.now(), dry_run=True)
    user_text = msgs[1]["content"]
    assert config.WORKING_NOTE_LABEL in user_text
    assert "{这是一条测试提示}" in user_text


def test_second_pass_overrides_first(conn, monkeypatch):
    """两段都可写，且看完结果后的第二段优先。"""
    row = _run_wake(conn, monkeypatch, note_first="{第一段测试提示}",
                    note_second="{第二段测试提示}", second_pass=True)
    assert row["note_for_next"] == "{第二段测试提示}"
    assert memory.get_working_note(conn)["content"] == "{第二段测试提示}"


def test_second_pass_parse_failure_falls_back_to_first(conn, monkeypatch):
    """第二段解析失败 → 丢弃它，回落到第一段；绝不沿用坏输出。"""
    row = _run_wake(conn, monkeypatch, note_first="{第一段测试提示}",
                    note_second="{不该被采用的第二段}", second_pass=True,
                    reaction_parse_ok=False)
    assert row["note_for_next"] == "{第一段测试提示}"
    assert memory.get_working_note(conn)["content"] == "{第一段测试提示}"


def test_wake_without_note_ticks_existing(conn, monkeypatch):
    memory.set_working_note(conn, "{这是一条测试提示}")
    row = _run_wake(conn, monkeypatch)               # 这轮什么都没写
    assert row["note_for_next"] is None
    assert memory.get_working_note(conn)["remaining_wakes"] == config.WORKING_NOTE_TTL - 1


def test_dry_run_never_touches_working_note(tmp_path, monkeypatch):
    """空跑只观察不执行：提示不被消费、不被覆盖，events 也不增长。"""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "dry.db")
    c = db.connect()
    memory.set_working_note(c, "{这是一条测试提示}")
    memory.settle_working_note(c, None)              # 先扣到 2
    before = memory.get_working_note(c)["remaining_wakes"]
    monkeypatch.setattr(config, "DRY_RUN", True)

    asyncio.run(scheduler.wake(c, None, trigger="test"))

    assert memory.get_working_note(c)["remaining_wakes"] == before
    assert c.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"] == 0


def test_no_note_means_no_block_in_prompt(conn):
    msgs, _ = prompt.build_messages(conn, util.now(), dry_run=True)
    assert config.WORKING_NOTE_LABEL not in msgs[1]["content"]


# ── 5. 老库迁移 & 契约字段名 ─────────────────────────────────────────────

def test_old_db_gets_new_column_and_table(tmp_path, monkeypatch):
    """老库没有 events.note_for_next，也没有 working_note 表，connect 要补齐。"""
    path = tmp_path / "old.db"
    raw = sqlite3.connect(path)
    raw.executescript(
        """CREATE TABLE events (
             id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
             kind TEXT NOT NULL, trigger TEXT NOT NULL, tool TEXT,
             args_json TEXT, result_summary TEXT, why TEXT NOT NULL,
             next_wake_at TEXT, tokens_in INTEGER DEFAULT 0,
             tokens_out INTEGER DEFAULT 0, duration_ms INTEGER DEFAULT 0,
             parse_ok INTEGER DEFAULT 1, trace_id TEXT);"""
    )
    raw.commit()
    raw.close()

    monkeypatch.setattr(db, "DB_PATH", path)
    c = db.connect()
    cols = {r["name"] for r in c.execute("PRAGMA table_info(events)")}
    assert "note_for_next" in cols
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "working_note" in tables
    assert memory.get_working_note(c) is None


def test_contracts_expose_the_field_name():
    """字段名进契约她才知道自己有这个能力（项目一贯原则）。

    只锁字段名、不锁任何自然语言措辞——文案归小帕定稿（分工红线）。
    """
    assert "note_for_next" in prompt.CONTRACT
    assert "note_for_next" in prompt.REACTION_CONTRACT
