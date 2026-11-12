"""self_review 工具（鱼鱼主动自省）的回归测试。

复用 test_self_review_cap 的 monkeypatch 套路：替掉 brain.review_narrative，
断言「工具路径」和「定时路径」走的是同一份执行体（brain.run_self_review）。
"""
import sqlite3

from fish import brain, config, db, memory, tools


def _tmp_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    db._migrate(conn)
    return conn


def _fake_review(client, old_core, old_relation="", exchanges="", trace_id="",
                 experiences="", time_anchor=""):
    return {"new_core": "新版自我叙事。", "new_relation": "小帕是维护者。"}


def test_self_review_tool_persists_narrative(monkeypatch):
    """调用 self_review 工具 → core + relation 落库 + 记成功事件。"""
    monkeypatch.setattr(brain, "review_narrative", _fake_review)
    conn = _tmp_db()
    memory.save_narrative(conn, "core", "旧版自我叙事。")
    res = tools.dispatch("self_review", {}, conn,
                         client=None, trace_id="t1")
    assert res.ok is True
    assert memory.get_narrative(conn, "core") == "新版自我叙事。"
    assert memory.get_narrative(conn, "relation") == "小帕是维护者。"
    row = conn.execute(
        "SELECT why FROM events WHERE kind='self_review' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row and "已更新" in row["why"]
    # detail 带新版自我叙事，落 jsonl 备查（不进 prompt，第二段已排除）
    assert res.detail == "新版自我叙事。"


def test_self_review_tool_dry_run_writes_nothing(monkeypatch):
    """DRY_RUN 下：工具返回 ok，但叙事不被改写（零副作用，与 wake 短路一致）。"""
    monkeypatch.setattr(brain, "review_narrative", _fake_review)
    monkeypatch.setattr(config, "DRY_RUN", True)
    conn = _tmp_db()
    memory.save_narrative(conn, "core", "旧版自我叙事。")
    res = tools.dispatch("self_review", {}, conn,
                         client=None, trace_id="t1")
    assert res.ok is True
    assert memory.get_narrative(conn, "core") == "旧版自我叙事。"   # 未改写
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM events WHERE kind='self_review'"
    ).fetchone()["n"] == 0


def test_self_review_tool_rejected_still_ok(monkeypatch):
    """闸门拒绝（保留率过低）→ 工具仍 ok、core 不变、记 error 事件。

    自省「尝试」本身就是这个工具的动作，闸门拒绝只是制度结果，不是工具崩了，
    所以 ok 恒为 True；她确实花了心思内省，但状态层已在 2026-11-12 移除，
    不再有系统定义的「状态代价」。
    """
    monkeypatch.setattr(config, "SELF_REVIEW_TARGET_MAX", 10_000)  # 旧版算「未超目标」→ 严格闸门
    monkeypatch.setattr(
        brain, "review_narrative",
        lambda *a, **k: {"new_core": "甲甲甲甲甲甲。", "new_relation": None},
    )
    conn = _tmp_db()
    old = "甲甲甲甲甲甲。乙乙乙乙乙乙。丙丙丙丙丙丙。"
    memory.save_narrative(conn, "core", old)
    res = tools.dispatch("self_review", {}, conn,
                         client=None, trace_id="t1")
    assert res.ok is True
    assert memory.get_narrative(conn, "core") == old          # 未改写
    row = conn.execute(
        "SELECT why FROM events WHERE kind='error' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row and "自省被拒" in row["why"]


def test_self_review_is_exposed_to_fish():
    """self_review 进入了工具清单，且契约（describe）会把它枚举给鱼鱼。"""
    assert any(t.name == "self_review" for t in tools.ALL_TOOLS)
    text = tools.describe(tools.ALL_TOOLS)
    assert "self_review" in text
