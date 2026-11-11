"""检索相关性闸门的回归测试。

背景（2026-11-10）：recall 去重上线后，两条 2026-11-09 的空跑测试日记
成了「唯一的新内容」——搜「胖东来 企业 原则」，它们一句都不沾边，却因为
pinned（日记 +0.05）、salience 0.8、以及当天的时间新鲜度排到了第一位。
根因：`search` 只算分不设下限，匹配度 0 的记忆照样参与排名。

规则：带 query 时，命中片段不到门槛的记忆直接出局。
"""

import sqlite3

import pytest

from fish import config, db, memory


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    """每个用例一个真实 DDL 的干净库。"""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    c = db.connect()
    return c


def _ids(rows):
    return [r["id"] for r in rows]


def test_no_query_keeps_old_behaviour(conn):
    """不带 query（wake 时自动检索）不看相关性，照旧按新鲜度+显著性排序。"""
    memory.add_episodic(conn, "读了胖东来那篇，赔到破产也要守原则。", salience=0.5)
    memory.add_episodic(conn, "邻居家的猫又来蹭饭了。", salience=0.3)

    rows = memory.search(conn, limit=5)
    assert len(rows) == 2


def test_irrelevant_query_returns_nothing(conn):
    """一条都不沾边时返回空——而不是把最置顶的旧账端上来。"""
    memory.add_episodic(conn, "[日记] （空跑模式：这是测试日记）", salience=0.8, pinned=1)
    memory.add_episodic(conn, "邻居家的猫又来蹭饭了。", salience=0.5)

    assert memory.search(conn, limit=5, query="区块链 量子 咖啡") == []
    assert memory.search(conn, limit=5, query="……") == []   # 切不出片段 = 也翻不到


def test_relevant_memory_survives_the_gate(conn):
    mid = memory.add_episodic(conn, "读了胖东来那篇，赔到破产也要守原则。", salience=0.5)
    memory.add_episodic(conn, "邻居家的猫又来蹭饭了。", salience=0.5)

    rows = memory.search(conn, limit=5, query="胖东来 原则")
    assert _ids(rows) == [mid]


def test_query_words_do_not_create_cross_boundary_grams(conn):
    """空格是词边界：查询里不该冒出「来 企」这种跨词的假片段。

    「来企」如果被造出来，下面的记忆（只有「来客」）会白捡一个命中。
    """
    hit = memory.add_episodic(conn, "胖东来那篇讲的是原则，不是来客人情。", salience=0.5)
    miss = memory.add_episodic(conn, "来了客人，泡了茶，聊到企业裁员。", salience=0.9)

    rows = memory.search(conn, limit=5, query="胖东来 企业")
    assert _ids(rows) == [hit]
    assert miss not in _ids(rows)


def test_single_hit_is_not_enough_for_multi_word_query(conn):
    """两个词的查询只蒙对一个片段（量子✓ / 咖啡✗）不算沾边。"""
    memory.add_episodic(conn, "看了量子计算的科普，还是不懂退相干。", salience=0.9)
    assert memory.search(conn, limit=5, query="量子 咖啡") == []


def test_single_char_query_works(conn):
    """单字查询切不出 bigram，仍要能命中——一个字就是它自己的片段。"""
    mid = memory.add_episodic(conn, "邻居家的猫又来蹭饭了。", salience=0.3)
    assert _ids(memory.search(conn, limit=5, query="猫")) == [mid]


def test_min_hits_scales_with_query_length(conn):
    """短查询钉在绝对数 2 上；长查询由比例条件把门槛抬高。"""
    assert memory._min_hits(1) == 1                        # 一个字不能要求 2 个片段
    assert memory._min_hits(3) == config.RECALL_MIN_HITS   # 短查询：绝对数说了算
    assert memory._min_hits(60) > config.RECALL_MIN_HITS   # 长查询：比例把门槛顶上去
