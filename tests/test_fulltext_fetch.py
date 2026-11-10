"""read_feed 按需抓全文 + inbox.link 的测试。

背景（2026-11-10 12:56）：少数派的 RSS 只给开头一段 +「查看全文」，
inbox 里存的是壳。她连读三条壳（空正文、纯标题、错配正文），得出
「这套 inbox 给我的多是壳，我不再当成巧合了」，开始回避 read_feed。

修法：入库时带上 <link>；她真去读、且正文短于阈值时，用 DeepSeek
Fetch 接口补全文，抓回来就地入库。抓不到就说清楚是渠道的问题。
"""

import sqlite3

import pytest

from fish import config, db, feed, state, tools, util, websearch


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    c = db.connect()
    db.init_state(c)
    return c


def _put(conn, title, body, link=None):
    conn.execute(
        "INSERT INTO inbox (ts, source, title, body, link) VALUES (?, ?, ?, ?, ?)",
        (util.now_iso(), "https://test.example/feed", title, body, link),
    )
    conn.commit()


# ── 解析：RSS / Atom 都要带出 link ───────────────────────────────────────

RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item><title>甲</title><link>https://test.example/1</link>
        <description>开头一段</description></item>
  <item><title>乙</title><description>没有链接的条目</description></item>
</channel></rss>"""

ATOM = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry><title>丙</title><link href="https://test.example/3"/>
         <summary>一段摘要</summary></entry>
</feed>"""


def test_parse_rss_link(conn):
    items = feed._parse(RSS.encode("utf-8"))
    assert items[0]["link"] == "https://test.example/1"
    assert items[1]["link"] == ""            # 没有链接就是空串，不是 None


def test_parse_atom_link_in_attribute(conn):
    items = feed._parse(ATOM.encode("utf-8"))
    assert items[0]["link"] == "https://test.example/3"   # Atom 的链接在属性里


# ── 迁移：老库的 inbox 没有 link 列 ──────────────────────────────────────

def test_migration_adds_link_column(tmp_path, monkeypatch):
    """link 列是 2026-11-10 加的：建一个老结构的库，connect 要能补上列。"""
    path = tmp_path / "old.db"
    raw = sqlite3.connect(path)
    raw.executescript(
        """CREATE TABLE inbox (
             id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
             source TEXT NOT NULL, title TEXT, body TEXT,
             consumed INTEGER DEFAULT 0);"""
    )
    raw.commit()
    raw.close()

    monkeypatch.setattr(db, "DB_PATH", path)
    c = db.connect()
    cols = {r["name"] for r in c.execute("PRAGMA table_info(inbox)")}
    assert "link" in cols


# ── 读时补全文 ────────────────────────────────────────────────────────────

def _fake_fetch(monkeypatch, content="这是抓回来的全文。" + "字" * 2000):
    calls = []

    def fake(url):
        calls.append(url)
        return {"title": "页标题", "content": content}

    monkeypatch.setattr(websearch, "fetch_page", fake)
    return calls


def test_short_body_with_link_gets_fulltext(conn, monkeypatch):
    calls = _fake_fetch(monkeypatch)
    _put(conn, "壳", "只有开头一段", link="https://test.example/post/1")

    res = tools.dispatch("read_feed", {"index": 0}, conn, state.load(conn))
    assert res.ok
    assert calls == ["https://test.example/post/1"]
    assert res.detail.startswith("这是抓回来的全文")       # 她读到的是全文
    # 全文就地入库：同一行下次不必再抓（等于 feed 自愈的读时版本）
    stored = conn.execute("SELECT body FROM inbox WHERE title = '壳'").fetchone()
    assert stored["body"] == "这是抓回来的全文。" + "字" * 2000


def test_fetch_failure_is_explained_not_blamed_on_content(conn, monkeypatch):
    def boom(url):
        raise websearch.WebSearchError("这页抓不到正文")

    monkeypatch.setattr(websearch, "fetch_page", boom)
    _put(conn, "壳", "只有开头一段", link="https://test.example/post/2")

    res = tools.dispatch("read_feed", {"index": 0}, conn, state.load(conn))
    assert res.ok
    assert "只有开头一段" in res.detail
    assert "RSS 只给了" in res.detail          # 说清楚是渠道的问题
    assert "不是你漏看了" in res.detail        # 别让她归因成「内容本来就是壳」


def test_long_body_or_no_link_never_fetches(conn, monkeypatch):
    calls = _fake_fetch(monkeypatch)
    _put(conn, "长正文", "字" * config.INBOX_FETCH_BELOW, link="https://t.example/1")
    _put(conn, "没链接", "短正文")

    for i in (0, 1):
        tools.dispatch("read_feed", {"index": i}, conn, state.load(conn))
    assert calls == []                         # 两种情况都不该花钱去抓


def test_dry_run_never_fetches(conn, monkeypatch):
    calls = _fake_fetch(monkeypatch)
    monkeypatch.setattr(config, "DRY_RUN", True)
    _put(conn, "壳", "只有开头一段", link="https://test.example/post/3")

    res = tools.dispatch("read_feed", {"index": 0}, conn, state.load(conn))
    assert res.ok and calls == []
    assert "只有开头一段" in res.detail
