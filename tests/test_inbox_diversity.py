"""inbox 轮转排序的测试。

背景（2026-11-10）：小帕把少数派加进 RSS 后，inbox 的「最新 3 条」仍然
全是知乎热榜——RSS 是整批入库的，一次抓取的 10 条同源条目按 id 排会
连着霸占前 10 位。世界在鱼鱼眼里就只剩一个源。

方案：阅读顺序改为「各源轮转」（feed.unread_reading_order），【世界】的
[n] 编号和 read_feed 的 index 用同一个顺序，所以天然一致。
"""

import pytest

from fish import config, db, feed, prompt, tools, util


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    c = db.connect()
    return c


ZHIHU = "https://zhihu-hot.ayanamist.com/feed.xml"
SSPAI = "https://sspai.com/feed"


def _put(conn, source, title, body="正文。"):
    """按 id 递增插入（= 按时间递增，模拟 feed_loop 的反向插入）。"""
    conn.execute(
        "INSERT INTO inbox (ts, source, title, body) VALUES (?, ?, ?, ?)",
        (util.now_iso(), source, title, body),
    )
    conn.commit()


def test_round_robin_alternates_sources(conn):
    """知乎 5 条、少数派 3 条（少数派后入库 = 更新）→ 阅读顺序 S Z S Z S Z Z Z。"""
    for i in range(1, 6):
        _put(conn, ZHIHU, f"知乎第{i}条")
    for i in range(1, 4):
        _put(conn, SSPAI, f"少数派第{i}条")   # 后插入 → id 更大 → 更新

    order = feed.unread_reading_order(conn)
    titles = [r["title"] for r in order]

    assert titles[0] == "少数派第3条"          # index 0 = 全场最新
    assert titles == ["少数派第3条", "知乎第5条", "少数派第2条",
                      "知乎第4条", "少数派第1条", "知乎第3条",
                      "知乎第2条", "知乎第1条"]   # 谁先没货谁消失


def test_single_source_keeps_newest_first(conn):
    """只有一个源时退化为「新→旧」，行为和旧版一致。"""
    for i in range(1, 4):
        _put(conn, ZHIHU, f"知乎第{i}条")
    titles = [r["title"] for r in feed.unread_reading_order(conn)]
    assert titles == ["知乎第3条", "知乎第2条", "知乎第1条"]


def test_read_feed_index_matches_world_numbering(conn):
    """【世界】的 [n] 编号读到的就是 read_feed 的第 n 条——两处必须同序。

    注意队列语义：读完一条后编号整体前移（像排队叫号）。单次唤醒只会
    读一条，所以她永远面对的是「当轮【世界】里的当轮编号」，不会错位。
    """
    _put(conn, ZHIHU, "知乎旧")
    _put(conn, ZHIHU, "知乎新")
    _put(conn, SSPAI, "少数派最新")           # 后插入 = 全场最新

    res0 = tools.dispatch("read_feed", {"index": 0}, conn)
    assert "少数派最新" in res0.summary       # [0] = 全场最新

    # 读完一条，两边都重新编号：新一轮【世界】的 [0] 和 read_feed 的
    # [0] 必须还是同一条——同序是「编号」成立的前提，不是巧合
    msgs, _ = prompt.build_messages(conn, util.now())
    # 知乎在「抓不回原文」名单里 → [0] 这条知乎会带无正文记号（用 config 常量，
    # 不硬编码最终措辞，小帕改 INBOX_NOFULLTEXT_MARK 时测试不会跟着红）。
    assert f"[0]{config.INBOX_NOFULLTEXT_MARK}《知乎新》（知乎热榜）" in msgs[-1]["content"]
    res1 = tools.dispatch("read_feed", {"index": 0}, conn)
    assert "知乎新" in res1.summary


def test_world_shows_per_source_stock_and_labels(conn):
    for i in range(3):
        _put(conn, ZHIHU, f"知乎第{i}条")
    for i in range(2):
        _put(conn, SSPAI, f"少数派第{i}条")

    msgs, _ = prompt.build_messages(conn, util.now())
    # 工具清单的提示词里也提到「【世界】」，所以要按带 inbox 的那段切
    world = msgs[-1]["content"].split("【世界】inbox")[1]

    assert "5 条没看过的" in world            # 总数
    assert "知乎热榜 3" in world and "少数派 2" in world   # 各源库存（显示名来自 config）
    assert "[0]《少数派第1条》（少数派）" in world         # 编号 + 源名（少数派不标无正文）
    # 知乎在名单里 → [1] 带无正文记号（用 config 常量，不硬编码最终措辞）
    assert f"[1]{config.INBOX_NOFULLTEXT_MARK}《知乎第2条》（知乎热榜）" in world   # 相邻编号不同源


def test_source_name_falls_back_to_domain(conn):
    """没在 config 里起名字（或源已下线的旧条目）→ 用去掉 www. 的域名。"""
    assert feed.source_name("https://www.example.com/feed") == "example.com"
    # config 里有名字的源用名字
    assert feed.source_name(SSPAI) == "少数派"


def test_is_nofulltext_flags_named_source(conn):
    """源在 FEED_NOFULLTEXT_SOURCES（知乎热榜）→ 哪怕有正文也标无正文。"""
    _put(conn, ZHIHU, "某知乎问题", body="一段还行的开头")
    row = conn.execute("SELECT * FROM inbox WHERE title='某知乎问题'").fetchone()
    assert feed.is_nofulltext(row)


def test_is_nofulltext_whitelist_excludes_others(conn):
    """少数派不在名单里 → 不标（除非它空 body 且无链接，走兜底）。"""
    _put(conn, SSPAI, "一篇少数派文章", body="正文。")
    row = conn.execute("SELECT * FROM inbox WHERE title='一篇少数派文章'").fetchone()
    assert not feed.is_nofulltext(row)


def test_is_nofulltext_fallback_empty_no_link(conn):
    """兜底：正文为空且没有链接 → 标无正文；有正文 → 不标。"""
    conn.execute(
        "INSERT INTO inbox (ts, source, title, body, link) VALUES (?,?,?,?,?)",
        (util.now_iso(), SSPAI, "空内容无链接", "", None),
    )
    row = conn.execute("SELECT * FROM inbox WHERE title='空内容无链接'").fetchone()
    assert feed.is_nofulltext(row)
    conn.execute("UPDATE inbox SET body='有字了' WHERE title='空内容无链接'")
    row2 = conn.execute("SELECT * FROM inbox WHERE title='空内容无链接'").fetchone()
    assert not feed.is_nofulltext(row2)


def test_world_preview_marks_nofulltext_source(conn):
    """【世界】预览里，知乎条目带无正文记号，少数派不带。"""
    _put(conn, ZHIHU, "知乎问题A", body="短开头")
    _put(conn, SSPAI, "少数派文章B", body="正文。")
    msgs, _ = prompt.build_messages(conn, util.now())
    world = msgs[-1]["content"].split("【世界】inbox")[1]
    zhihu_line = next(l for l in world.splitlines() if "知乎问题A" in l)
    sspai_line = next(l for l in world.splitlines() if "少数派文章B" in l)
    assert config.INBOX_NOFULLTEXT_MARK in zhihu_line
    assert config.INBOX_NOFULLTEXT_MARK not in sspai_line
