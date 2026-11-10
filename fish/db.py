"""数据层：SQLite 连接、建表、少量通用读写。

为什么用同步的 sqlite3 而不是异步驱动（如 aiosqlite）？
本项目所有数据库操作都是毫秒级的小读写，就算偶尔阻塞主循环
几十毫秒也完全无感。异步数据库驱动会显著增加心智负担，
等真的出现慢查询再换不迟——先把精力花在值得复杂化的地方。
"""

import sqlite3

from . import config, util

DB_PATH = config.DATA_DIR / "fish.db"

# DDL 与 Design_v0.2.md §3.1 一字不差，字段含义看那里的注释。
SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS state (
  id            INTEGER PRIMARY KEY CHECK (id = 1),
  energy        REAL NOT NULL,
  valence       REAL NOT NULL,
  boredom       REAL NOT NULL,
  updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  ts             TEXT    NOT NULL,
  kind           TEXT    NOT NULL CHECK (kind IN ('heartbeat','event','error','self_review')),
  trigger        TEXT    NOT NULL,
  tool           TEXT,
  args_json      TEXT,
  result_summary TEXT,
  why            TEXT    NOT NULL,
  energy_before  REAL, energy_after  REAL,
  valence_before REAL, valence_after REAL,
  boredom_before REAL, boredom_after REAL,
  next_wake_at   TEXT,
  tokens_in      INTEGER DEFAULT 0,
  tokens_out     INTEGER DEFAULT 0,
  duration_ms    INTEGER DEFAULT 0,
  parse_ok       INTEGER DEFAULT 1,
  trace_id       TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts   ON events(ts DESC);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind, ts DESC);

CREATE TABLE IF NOT EXISTS memories (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  ts               TEXT    NOT NULL,
  layer            TEXT    NOT NULL CHECK (layer IN ('episodic','core','relation')),
  content          TEXT    NOT NULL,
  salience         REAL    NOT NULL DEFAULT 0.5,
  tags             TEXT,
  source_event_id  INTEGER REFERENCES events(id),
  last_recalled_at TEXT,
  recall_count     INTEGER DEFAULT 0,
  pinned           INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_mem_layer ON memories(layer, ts DESC);

CREATE TABLE IF NOT EXISTS inbox (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  ts       TEXT NOT NULL,
  source   TEXT NOT NULL,
  title    TEXT,
  body     TEXT,
  link     TEXT,
  consumed INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_inbox_unread ON inbox(consumed, ts DESC);

CREATE TABLE IF NOT EXISTS messages (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  ts                  TEXT    NOT NULL,
  direction           TEXT    NOT NULL CHECK (direction IN ('to_yuyu','from_yuyu')),
  text                TEXT    NOT NULL,
  wake_trace_id       TEXT,
  seen_by_yuyu        INTEGER DEFAULT 0,
  delivered_to_xiaopa INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_msg_dir_seen ON messages(direction, seen_by_yuyu);
"""


def connect() -> sqlite3.Connection:
    """打开（必要时创建）数据库并返回连接。

    row_factory 这行让查询结果能按列名取值（row["energy"]），
    而不是按下标（row[1]）。前者可读性远好于后者。
    """
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """给已存在的旧库补新列。

    CREATE TABLE IF NOT EXISTS 只对新建的库生效——老库里不会自动
    多出新字段。所以加列时必须在这里显式补一次，否则跑起来就是
    “no such column: trace_id”。这是 SQLite 项目里最常见的坑之一。
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(events)")}
    if "trace_id" not in cols:
        conn.execute("ALTER TABLE events ADD COLUMN trace_id TEXT")

    # inbox.link（2026-11-10）：read_feed 靠它按需抓全文。老库里没有这列，
    # 旧条目 link 为 NULL——不抓，维持原样，等下轮 RSS 重抓时自愈回填。
    icols = {r["name"] for r in conn.execute("PRAGMA table_info(inbox)")}
    if "link" not in icols:
        conn.execute("ALTER TABLE inbox ADD COLUMN link TEXT")


def init_state(conn: sqlite3.Connection) -> None:
    """保证 state 表恰好有一行（id=1）。

    INSERT OR IGNORE：已存在就什么都不做，不存在才插入初始值。
    这是"只在第一次启动时播种"的标准写法。
    """
    conn.execute(
        """INSERT OR IGNORE INTO state (id, energy, valence, boredom, updated_at)
           VALUES (1, ?, ?, ?, ?)""",
        (
            config.BASELINE["energy"],
            config.BASELINE["valence"],
            30.0,                      # boredom 的初始/常态值，见 Design_v0.2 §4.1
            util.now_iso(),
        ),
    )
    conn.commit()


def latest_event(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM events ORDER BY ts DESC, id DESC LIMIT 1"
    ).fetchone()


def wake_count(conn: sqlite3.Connection) -> int:
    """总共 wake 过多少次（含空转）。用于 self_review 的触发计数。"""
    return conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
