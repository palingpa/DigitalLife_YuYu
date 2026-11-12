"""自省输入上下文（2026-11-12 新增）的回归测试。

修的是这条断层：旧版自省只看得见「旧 core + 旧 relation + 全量往来」，
两次自省之间新增的记忆和唤醒全都不进输入。小帕拍板的四点：
  ① 增量式（以上次成功自省为界）
  ② 只要「她自己写下的」
  ③ 工作记忆提示不进自省素材
  ④ 自省调用的 trace kind 修成 review

所有会落库 / 拼进 messages 的假文本都用花括号占位，一眼可辨，
绝不混进 logs/traces 里冒充鱼鱼的真实产出。
"""
import json
import sqlite3
import sys

from fish import brain, config, db, memory, messages, prompt, trace, util


# ── 夹具 ─────────────────────────────────────────────────────────────────

def _tmp_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    db._migrate(conn)
    return conn


def _t(hhmm: str) -> str:
    return f"2026-11-12T{hhmm}:00"


def _now():
    return util.parse_iso(_t("20:00"))


def _mem(conn, ts: str, content: str, tags: str = "") -> None:
    conn.execute(
        "INSERT INTO memories (ts, layer, content, salience, tags) "
        "VALUES (?, 'episodic', ?, 0.7, ?)",
        (ts, content, tags),
    )
    conn.commit()


def _review_event(conn, ts: str, kind: str = "self_review") -> None:
    conn.execute(
        "INSERT INTO events (ts, kind, trigger, why) VALUES (?, ?, 'self_review', ?)",
        (ts, kind, "{这是一条测试自省事件}"),
    )
    conn.commit()


def _capture_api(monkeypatch, captured: dict) -> None:
    """替掉 _call_api，抓住真正发给模型的 messages 和 kind（不碰网络）。"""
    def fake_call(client, msgs, temperature, trace_id="", attempt=1,
                  kind="decision"):
        captured["messages"] = msgs
        captured["kind"] = kind
        return brain.ApiReply(
            json.dumps({
                "new_core": "{这是旧的测试自我叙事。}{这是新增的测试句子。}",
                "new_relation": "",
            }, ensure_ascii=False),
            "", (0, 0), 0,
        )
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(brain, "_call_api", fake_call)


# ── ① 增量边界 ───────────────────────────────────────────────────────────

def test_last_review_ts_prefers_latest_success_and_ignores_rejected():
    """被闸门拒的自省记的是 kind='error'，不能当成「上次自省」。"""
    conn = _tmp_db()
    _review_event(conn, _t("09:00"))
    _review_event(conn, _t("11:00"))
    _review_event(conn, _t("11:30"), kind="error")
    assert memory.last_review_ts(conn) == _t("11:00")


def test_last_review_ts_none_on_cold_start():
    conn = _tmp_db()
    assert memory.last_review_ts(conn) is None


def test_incremental_boundary_only_returns_writings_after_last_review():
    conn = _tmp_db()
    _review_event(conn, _t("10:00"))
    _mem(conn, _t("09:00"), "{这是自省前写下的测试日记}")
    _mem(conn, _t("11:00"), "{这是自省后写下的测试日记}")
    rows = memory.her_own_writings(conn, _t("10:00"), 10)
    assert [r["content"] for r in rows] == ["{这是自省后写下的测试日记}"]


def test_cold_start_review_has_no_experiences_block(monkeypatch):
    """没有「上次」就没有「这一段」：第一次自省不灌历史，时间锚点仍给。"""
    conn = _tmp_db()
    _mem(conn, _t("11:00"), "[日记] {这是第一次自省前的测试日记}")
    captured = {}
    _capture_api(monkeypatch, captured)
    brain.run_self_review(conn, client=None)
    user = captured["messages"][1]["content"]
    assert config.REVIEW_EXPERIENCES_LABEL not in user
    assert config.REVIEW_SINCE_LABEL not in user
    assert config.REVIEW_NOW_LABEL in user


# ── ② 只要「她自己写下的」────────────────────────────────────────────────

def test_excludes_xiaopa_tagged_records():
    """tags='小帕' 同时标了系统代记的来信、以及已在往来区块出现的去信。"""
    conn = _tmp_db()
    _mem(conn, _t("11:00"), "{这是一封测试来信}", tags=config.XIAOPA_MSG_TAG)
    _mem(conn, _t("11:05"), "{这是一条发给小帕的测试消息}",
         tags=config.XIAOPA_MSG_TAG)
    _mem(conn, _t("11:10"), "[日记] {这是一条测试日记}")
    rows = memory.her_own_writings(conn, _t("10:00"), 10)
    assert [r["content"] for r in rows] == ["[日记] {这是一条测试日记}"]


def test_includes_journal_and_second_pass_reflection():
    """日记 + 第二段「事后心里想」都是她自己的措辞，按时间升序进来。"""
    conn = _tmp_db()
    _mem(conn, _t("11:10"), "{这是一段测试反思}", tags="read_feed")
    _mem(conn, _t("11:00"), "[日记] {这是一条测试日记}")
    rows = memory.her_own_writings(conn, _t("10:00"), 10)
    assert [r["content"] for r in rows] == [
        "[日记] {这是一条测试日记}", "{这是一段测试反思}",
    ]


def test_max_items_keeps_newest_in_chronological_order():
    """按条数限，取最近 N 条并翻回时间升序（不是最早的 N 条）。"""
    conn = _tmp_db()
    for i in range(15):
        _mem(conn, f"2026-11-12T11:{i:02d}:00", f"{{这是第{i}条测试日记}}")
    rows = memory.her_own_writings(conn, _t("10:00"), 3)
    assert [r["content"] for r in rows] == [
        f"{{这是第{i}条测试日记}}" for i in (12, 13, 14)
    ]


def test_experiences_are_never_character_truncated():
    """不硬截字符：截断会留下半句话，这是 prompt.py:216 记过的教训。"""
    conn = _tmp_db()
    long_text = "{这是一条很长的测试日记}" + "甲" * 500
    _mem(conn, _t("11:00"), long_text)
    rows = memory.her_own_writings(conn, _t("10:00"), 10)
    assert long_text in prompt.format_experiences(rows, _now())


# ── 上下文组装 ───────────────────────────────────────────────────────────

def test_build_review_messages_renders_new_block_and_anchor():
    msgs = prompt.build_review_messages(
        "{这是旧的测试自我叙事。}", "{这是旧的测试关系叙事。}",
        "{这是一条测试往来}",
        experiences="- 2 小时前：{这是一条测试日记}",
        time_anchor=f"{config.REVIEW_NOW_LABEL}2026-11-12 20:00",
    )
    user = msgs[1]["content"]
    assert config.REVIEW_NOW_LABEL in user
    assert config.REVIEW_EXPERIENCES_LABEL in user
    assert "{这是一条测试日记}" in user
    assert "重写它。用 JSON 回复。" in user          # 既有收尾措辞一字未动
    assert config.WORKING_NOTE_LABEL not in user


def test_empty_experiences_omit_the_block():
    msgs = prompt.build_review_messages("{占位core}", "", "",
                                        experiences="", time_anchor="")
    assert config.REVIEW_EXPERIENCES_LABEL not in msgs[1]["content"]


def test_time_anchor_cold_start_has_no_since_line():
    out = prompt.review_time_anchor(None, _now())
    assert config.REVIEW_NOW_LABEL in out
    assert config.REVIEW_SINCE_LABEL not in out


def test_time_anchor_uses_relative_time():
    out = prompt.review_time_anchor(_t("18:00"), _now())
    assert config.REVIEW_SINCE_LABEL in out
    assert "2 小时前" in out


def test_labels_come_from_config_so_wording_can_change(monkeypatch):
    """小帕改措辞不该让测试变红：断言一律走 config 常量，不硬编码文案。"""
    monkeypatch.setattr(config, "REVIEW_EXPERIENCES_LABEL", "{替换后的测试标签}")
    msgs = prompt.build_review_messages("{占位core}", "", "",
                                        experiences="{测试经历}")
    assert "{替换后的测试标签}" in msgs[1]["content"]


# ── ③ 工作记忆 + ④ trace kind：走完整 run_self_review ─────────────────────

def test_run_self_review_feeds_incremental_writings_but_never_working_note(monkeypatch):
    """四条拍板一次性验完：增量、只要她写的、提示不进、kind=review。"""
    conn = _tmp_db()
    memory.save_narrative(conn, "core", "{这是旧的测试自我叙事。}")
    _review_event(conn, _t("10:00"))
    _mem(conn, _t("09:00"), "{这是自省前的测试日记，不该出现}")
    _mem(conn, _t("11:00"), "[日记] {这是自省后的测试日记，该出现}")
    _mem(conn, _t("11:10"), "{这是自省后的测试反思，该出现}", tags="read_feed")
    _mem(conn, _t("11:20"), "{这是一封测试来信，不该出现}",
         tags=config.XIAOPA_MSG_TAG)
    memory.set_working_note(conn, "{这是一条绝不能进自省的测试提示}")
    messages.post(conn, "to_yuyu", "{这是一条测试往来}")

    captured = {}
    _capture_api(monkeypatch, captured)
    brain.run_self_review(conn, client=None, trace_id="t1")

    user = captured["messages"][1]["content"]
    assert captured["kind"] == "review"                              # ④
    assert "{这是自省后的测试日记，该出现}" in user                  # ①
    assert "{这是自省后的测试反思，该出现}" in user                  # ②
    assert "{这是自省前的测试日记，不该出现}" not in user             # ①
    assert "{这是一封测试来信，不该出现}" not in user                 # ②
    assert "{这是一条绝不能进自省的测试提示}" not in user             # ③
    assert config.REVIEW_NOW_LABEL in user
    assert config.REVIEW_EXPERIENCES_LABEL in user


def test_dry_run_review_never_calls_api(monkeypatch):
    """空跑时自省是制度动作，不该真的改自我认知，也不该发请求。"""
    monkeypatch.setattr(config, "DRY_RUN", True)
    called = []
    monkeypatch.setattr(brain, "_call_api", lambda *a, **k: called.append(1))
    res = brain.run_self_review(_tmp_db(), client=None)
    assert called == []
    assert "空跑" in res["summary"]


# ── ④ 命令行查看器也要能看懂 review ───────────────────────────────────────

def test_trace_viewer_prints_full_review_messages(monkeypatch, capsys):
    """review 走完整 messages 分支，不能被当成杂项只打印 400 字截断 JSON。"""
    monkeypatch.setattr(sys, "argv", ["trace", "1"])
    monkeypatch.setattr(trace, "tail", lambda n: [{
        "ts": _t("20:00"), "kind": "review", "trace_id": "t1",
        "model": "m", "temperature": 0.7, "attempt": 1,
        "tokens_in": 1, "tokens_out": 1, "duration_ms": 1,
        "messages": [{"role": "user", "content": "{这是自省的完整测试输入}"}],
        "content": "{}", "reasoning": "",
    }])
    trace._main()          # 参数从 sys.argv 读，不接位置参数
    out = capsys.readouterr().out
    assert "自省" in out
    assert "{这是自省的完整测试输入}" in out
