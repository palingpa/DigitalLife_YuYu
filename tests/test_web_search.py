"""web_search 工具的测试。

不出网：真 API 调用（fish.websearch.search_web）全部用 monkeypatch 换成
假函数，这里只测「工具层怎么组装参数、怎么排版结果、怎么报错」。
真实的 API 连通性用一次手动实搜验证（见 2026-11-10 的工作日志）。
"""

import pytest

from fish import config, prompt, state, tools, websearch


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr("fish.db.DB_PATH", tmp_path / "test.db")
    from fish import db
    c = db.connect()
    db.init_state(c)
    return c


def _fake_results(monkeypatch, n=3, summary_len=100):
    """装一个假的 search_web，顺便把调用记录下来防止「根本没调」的假绿。"""
    calls = []

    def fake(query, count=3):
        calls.append((query, count))
        return [
            {"title": f"结果标题{i}", "url": f"https://example.com/{i}",
             "site": "示例站", "date": "2026-11-08",
             "summary": "这是第" + str(i) + "条的摘要。" + "字" * summary_len}
            for i in range(1, n + 1)
        ]

    monkeypatch.setattr(websearch, "search_web", fake)
    return calls


def test_happy_path_formats_three_results(conn, monkeypatch):
    calls = _fake_results(monkeypatch)
    res = tools.dispatch("web_search", {"query": "胖东来 原则"}, conn, state.load(conn))

    assert res.ok
    assert calls == [("胖东来 原则", config.WEB_SEARCH_COUNT)]
    for i in (1, 2, 3):
        assert f"《结果标题{i}》" in res.detail      # 每条标题都在
        assert f"https://example.com/{i}" in res.detail  # 出处可追溯
    assert res.summary.startswith("上网搜了「胖东来 原则」，拿到 3 条")
    assert len(res.summary) <= 120                    # summary 的百字纪律


def test_empty_query_does_not_call_api(conn, monkeypatch):
    calls = _fake_results(monkeypatch)
    res = tools.dispatch("web_search", {"query": "  "}, conn, state.load(conn))
    assert not res.ok and calls == []                 # 参数不对，一次请求都不该发


def test_too_long_query_rejected(conn, monkeypatch):
    calls = _fake_results(monkeypatch)
    res = tools.dispatch("web_search", {"query": "长" * (config.WEB_SEARCH_MAX_QUERY + 1)},
                         conn, state.load(conn))
    assert not res.ok and calls == []


def test_api_error_is_honest(conn, monkeypatch):
    def boom(query, count=3):
        raise websearch.WebSearchError("账号余额不足")

    monkeypatch.setattr(websearch, "search_web", boom)
    res = tools.dispatch("web_search", {"query": "什么"}, conn, state.load(conn))
    assert not res.ok and "账号余额不足" in res.summary


def test_no_results_is_a_failure_not_a_lie(conn, monkeypatch):
    monkeypatch.setattr(websearch, "search_web", lambda q, count=3: [])
    res = tools.dispatch("web_search", {"query": "不存在的词"}, conn, state.load(conn))
    assert not res.ok and "一条相关的都没有" in res.summary


def test_long_summaries_share_the_budget(conn, monkeypatch):
    """3 条超长摘要平分预算：每条被截断且标注，总长不超阅读预算。"""
    _fake_results(monkeypatch, summary_len=900)
    res = tools.dispatch("web_search", {"query": "长摘要"}, conn, state.load(conn))

    assert res.ok
    assert res.detail.count("你只看到前") == 3        # 每条都如实标注
    assert len(res.detail) <= config.REACTION_TEXT_CHARS + 40  # 头部字段的一点余量


def test_dry_run_never_touches_network(conn, monkeypatch):
    calls = _fake_results(monkeypatch)
    monkeypatch.setattr(config, "DRY_RUN", True)
    res = tools.dispatch("web_search", {"query": "空跑"}, conn, state.load(conn))
    assert res.ok and calls == [] and "空跑" in res.summary


def test_contract_lists_every_tool():
    """契约里的工具枚举必须覆盖 ALL_TOOLS——曾经手写清单，加工具忘了改就漂移。"""
    for t in tools.ALL_TOOLS:
        assert f'"{t.name}"' in prompt.CONTRACT


def test_web_search_costs_energy_and_relieves_boredom():
    s = state.State(energy=60.0, valence=50.0, boredom=40.0)
    state.apply_tool_effect(s, "web_search", ok=True, repeat_streak=1)
    assert s.energy == 57.0        # -3：出门找东西费神
    assert s.boredom == 25.0       # 首次解闷 -15，和别的工具同一套边际递减
