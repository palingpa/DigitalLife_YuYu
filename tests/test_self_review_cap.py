"""自省叙事长度闸门（2026-11-10 新增）的回归测试。

核心逻辑抽成了纯函数 scheduler._review_decision，便于单测，不碰库/网络。
集成部分用 asyncio.run 包住 _self_review（项目未启用 pytest-asyncio 插件）。
"""
import asyncio
import sqlite3

from fish import config, db, memory, scheduler


# ── 纯函数单测 ──────────────────────────────────────────────────────────────

def test_hard_max_rejects_overlong():
    """new 超过硬上限 → 拒绝（安全网，与保留率无关）。"""
    old = "短句一。短句二。"
    new = "长" * (config.SELF_REVIEW_HARD_MAX + 10)
    accepted, detail = scheduler._review_decision(old, new)
    assert accepted is False
    assert "超长" in detail


def test_strict_gate_rejects_deep_cut_when_under_target(monkeypatch):
    """旧版低于软目标 → 走严格闸门 0.6，砍掉 2/3 被拒。"""
    monkeypatch.setattr(config, "SELF_REVIEW_TARGET_MAX", 10_000)
    old = "甲甲甲甲甲甲。乙乙乙乙乙乙。丙丙丙丙丙丙。"
    new = "甲甲甲甲甲甲。"            # 只保留 1/3
    accepted, detail = scheduler._review_decision(old, new)
    assert accepted is False
    assert "保留率" in detail


def test_relaxed_gate_accepts_deep_cut_when_over_target(monkeypatch):
    """旧版超过软目标 → 放宽到 0.3，允许深度压缩（砍到 1/3 也接受）。"""
    monkeypatch.setattr(config, "SELF_REVIEW_TARGET_MAX", 1)
    old = "甲甲甲甲甲甲。乙乙乙乙乙乙。丙丙丙丙丙丙。"
    new = "甲甲甲甲甲甲。"
    accepted, detail = scheduler._review_decision(old, new)
    assert accepted is True


def test_under_target_accepts_light_cut(monkeypatch):
    """旧版低于软目标 → 轻量改写（保留 2/3）正常接受。"""
    monkeypatch.setattr(config, "SELF_REVIEW_TARGET_MAX", 10_000)
    old = "甲甲甲甲甲甲。乙乙乙乙乙乙。丙丙丙丙丙丙。"
    new = "甲甲甲甲甲甲。乙乙乙乙乙乙。"
    accepted, _ = scheduler._review_decision(old, new)
    assert accepted is True


# ── 集成：把闸门接到数据库上 ───────────────────────────────────────────────

def _tmp_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    db._migrate(conn)
    return conn


async def _run_review(conn, new_core, monkeypatch, new_relation=None):
    def fake_review(client, old_core, old_relation="", exchanges="", trace_id=""):
        return {"new_core": new_core, "new_relation": new_relation}
    monkeypatch.setattr(scheduler.brain, "review_narrative", fake_review)
    await scheduler._self_review(conn, client=None, trace_id="t1")


def test_self_review_persists_compressed_when_over_target(monkeypatch):
    """旧版算「超目标」时，深度压缩被接受并落库 + 记成功事件。"""
    monkeypatch.setattr(config, "SELF_REVIEW_TARGET_MAX", 1)
    conn = _tmp_db()
    memory.save_narrative(conn, "core", "甲甲甲甲甲甲。乙乙乙乙乙乙。丙丙丙丙丙丙。")
    asyncio.run(_run_review(conn, "甲甲甲甲甲甲。", monkeypatch))
    assert memory.get_narrative(conn, "core") == "甲甲甲甲甲甲。"
    row = conn.execute(
        "SELECT why FROM events WHERE kind='self_review' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row and "已更新" in row["why"]


def test_self_review_rejects_deep_cut_when_under_target(monkeypatch):
    """旧版算「未超目标」时，深度压缩被拒：core 不变 + 记 error 事件。"""
    monkeypatch.setattr(config, "SELF_REVIEW_TARGET_MAX", 10_000)
    conn = _tmp_db()
    old = "甲甲甲甲甲甲。乙乙乙乙乙乙。丙丙丙丙丙丙。"
    memory.save_narrative(conn, "core", old)
    asyncio.run(_run_review(conn, "甲甲甲甲甲甲。", monkeypatch))
    assert memory.get_narrative(conn, "core") == old
    row = conn.execute(
        "SELECT why FROM events WHERE kind='error' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row and "自省被拒" in row["why"]


# ── 关系叙事（relation）：合并进同一轮自省，v1 无长度闸门 ──────────────────

def test_self_review_saves_relation_when_provided(monkeypatch):
    """模型给了 new_relation → 落库 + 记成功事件。"""
    conn = _tmp_db()
    memory.save_narrative(conn, "core", "甲甲甲甲甲甲。乙乙乙乙乙乙。")
    asyncio.run(_run_review(conn, "甲甲甲甲甲甲。乙乙乙乙乙乙。", monkeypatch,
                            new_relation="小帕是维护这套系统的人。"))
    assert memory.get_narrative(conn, "relation") == "小帕是维护这套系统的人。"
    row = conn.execute(
        "SELECT why FROM events WHERE why LIKE '%关系叙事%' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row and "已更新" in row["why"]


def test_self_review_skips_empty_relation(monkeypatch):
    """new_relation 为空 → 不覆盖已有的 relation。"""
    conn = _tmp_db()
    memory.save_narrative(conn, "core", "甲甲甲甲甲甲。")
    memory.save_narrative(conn, "relation", "旧关系还在")
    asyncio.run(_run_review(conn, "甲甲甲甲甲甲。", monkeypatch, new_relation=None))
    assert memory.get_narrative(conn, "relation") == "旧关系还在"


def test_relation_saved_even_if_core_rejected(monkeypatch):
    """core 因保留率被拒，但 relation 与 core 解耦，仍应落库。"""
    monkeypatch.setattr(config, "SELF_REVIEW_TARGET_MAX", 10_000)
    conn = _tmp_db()
    old = "甲甲甲甲甲甲。乙乙乙乙乙乙。丙丙丙丙丙丙。"
    memory.save_narrative(conn, "core", old)
    asyncio.run(_run_review(conn, "甲甲甲甲甲甲。", monkeypatch,
                            new_relation="小帕是维护者。"))
    # core 被拒，原样保留
    assert memory.get_narrative(conn, "core") == old
    # relation 独立落库
    assert memory.get_narrative(conn, "relation") == "小帕是维护者。"
