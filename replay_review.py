"""一次性运维脚本：补做 2026-11-12 19:13 那次「缺素材的自省」。

背景
----
19:13 之前的自省代码只喂「旧自我叙事 + 全量往来」，看不到她这段时间
自己写下的东西。新代码上线后，正常自省只会吃「19:13 → 现在」的增量，
那段历史就永远补不回来了。小帕要求：用**库里最新版 core** 当种子，
把**上上次～上次**区间的记忆重新喂一遍，只做这一次。

做法
----
不改任何生产代码。用 unittest.mock.patch 临时把 memory.last_review_ts
的返回值指到「上上次成功自省」，然后直接调 brain.run_self_review ——
种子、增量、时间锚点、往来、保留率闸门、落库、事件、trace 全部复用
既有制度，脚本本身不重新实现任何一条规则，也不注入任何新的自然语言。

为什么这样能精确取到历史区间
----------------------------
her_own_writings 只认下界（since 之后），没有上界。它这次之所以恰好
等于 11:26～19:13，全靠「19:13 之后没有符合判据的记忆」（那之后只有
2 条 tags=小帕 的来信/回复，按既有去重规则被排除）。脚本会用断言把这个
前提钉死：哪天鱼鱼重启后又写了日记，脚本会拒绝执行，而不是悄悄把新素材
混进这次「补做历史」的自省。

用法（在项目根）
----------------
    python replay_review.py            # 只预览：零 API、零写库
    python replay_review.py --run      # 备份 → 真跑 → 落库 → 打印结果

一次性脚本，用完可删；留在仓库里只为可回溯。
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

from fish import brain, config, db, lock, memory, prompt, util

ROOT = Path(__file__).resolve().parent
PREVIEW_FILE = ROOT / "logs" / "replay_review_preview.txt"
BACKUP_DIR = ROOT / "backups"


# ── 找区间、钉前提 ────────────────────────────────────────────────────────

def review_rounds(conn: sqlite3.Connection) -> tuple[sqlite3.Row, sqlite3.Row]:
    """返回 (上上次, 上次) 两轮成功自省，每轮给出起止时间。

    一轮自省最多写两条 kind='self_review' 事件（关系叙事 + 自我叙事），
    它们共用一个 trace_id、时间戳可能差 1 秒。所以必须按 trace_id 分组，
    不能直接取「倒数两条事件」——那样「上上次」会错位成 R7 的关系叙事那条。
    """
    rows = conn.execute(
        """SELECT trace_id, MIN(ts) AS ts_start, MAX(ts) AS ts_end
           FROM events
           WHERE kind = 'self_review'
             AND trace_id IS NOT NULL AND trace_id != ''
           GROUP BY trace_id
           ORDER BY ts_start DESC, trace_id DESC
           LIMIT 2"""
    ).fetchall()
    if len(rows) < 2:
        raise SystemExit("库里不足两轮成功自省，定不出「上上次～上次」区间")
    return rows[1], rows[0]


def assert_prerequisite(conn: sqlite3.Connection, r6_end: str,
                        r7_end: str) -> int:
    """确认「把水位指回 R6」取到的正好是 R6～R7 那批，一条不多一条不少。"""
    after = conn.execute(
        """SELECT count(*) FROM memories
           WHERE layer = 'episodic' AND ts > ?
             AND (tags IS NULL OR tags != ?)""",
        (r7_end, config.XIAOPA_MSG_TAG),
    ).fetchone()[0]
    if after:
        raise SystemExit(
            f"前提不成立：上次自省之后已有 {after} 条会被喂进增量的新记忆。\n"
            f"这次是「补做 19:13」，不该混进新素材。请确认要不要改做正常自省，"
            f"或给 her_own_writings 加区间上界后再跑。"
        )
    expected = conn.execute(
        """SELECT count(*) FROM memories
           WHERE layer = 'episodic' AND ts > ? AND ts <= ?
             AND (tags IS NULL OR tags != ?)""",
        (r6_end, r7_end, config.XIAOPA_MSG_TAG),
    ).fetchone()[0]
    actual = len(memory.her_own_writings(conn, r6_end,
                                         config.REVIEW_EXPERIENCE_MAX))
    if actual != expected:
        raise SystemExit(
            f"区间对不上：R6～R7 有 {expected} 条，实际会喂 {actual} 条"
            f"（上限 {config.REVIEW_EXPERIENCE_MAX}）。停下等人确认。"
        )
    return actual


# ── 一致性快照 ────────────────────────────────────────────────────────────

def backup_db(dest: Path) -> None:
    """用 sqlite3 的 backup() 做一致性快照，而不是 shutil.copy2 主库文件。

    库开了 WAL，最近的写入可能还躺在 -wal 里没合并回主文件；直接复制
    .db 会拿到缺页的半成品。backup() 让 SQLite 自己把当前一致状态整份
    写进新文件，WAL 与否都不用操心。
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    rel = Path(os.path.relpath(db.DB_PATH)).as_posix()
    src = sqlite3.connect(f"file:{rel}?mode=ro", uri=True)
    try:
        out = sqlite3.connect(dest)
        try:
            src.backup(out)
        finally:
            out.close()
    finally:
        src.close()


# ── 预览：截获「实跑会喂的」完整 messages ─────────────────────────────────

def capture_messages(r6_end: str) -> tuple[list[dict], dict]:
    """在库的临时副本上跑一次组装段，原样截获上下文。

    为什么不照抄 run_self_review 里那几行组装代码：这次最怕的就是
    「预览看到的」和「实际喂给模型的」不是同一份（19:13 那次正是栽在
    输入不可见）。于是把 brain.review_narrative 临时换成一个只记参数的
    假函数，让 run_self_review 自己把上下文拼好——它传给假函数的参数，
    就是真跑时会传给模型的那些。

    全程在副本上：假函数返回「没有新叙事」，run_self_review 会直接跳过
    落库分支，所以副本和真库都不会被写，也不会发出任何 API 请求。
    """
    tmpdir = Path(tempfile.mkdtemp(prefix="fish-replay-"))
    tmpdb = tmpdir / "copy.db"
    backup_db(tmpdb)
    saved_path = db.DB_PATH
    db.DB_PATH = tmpdb
    captured: dict = {}

    def fake_review(client, old_core, old_relation="", exchanges="",
                    trace_id="", experiences="", time_anchor=""):
        captured.update(old_core=old_core, old_relation=old_relation,
                        exchanges=exchanges, experiences=experiences,
                        time_anchor=time_anchor)
        # 一眼可辨的占位返回：不是模型产出，只是让流程提前收尾
        return {"new_core": None, "new_relation": None}

    try:
        conn = db.connect()
        try:
            # patch.object 的第三个位置参数就是「拿什么替换」，直接给函数。
            # run_self_review 内部 `from . import memory` 拿到的是同一个模块
            # 对象，所以替换 last_review_ts 对它一样生效。
            with patch.object(memory, "last_review_ts", return_value=r6_end), \
                 patch.object(brain, "review_narrative", fake_review):
                brain.run_self_review(conn, client=None, trace_id="{预览占位}")
        finally:
            conn.close()
    finally:
        db.DB_PATH = saved_path
        shutil.rmtree(tmpdir, ignore_errors=True)

    msgs = prompt.build_review_messages(
        captured["old_core"], captured["old_relation"],
        captured["exchanges"], captured["experiences"],
        captured["time_anchor"],
    )
    return msgs, captured


def print_preview(msgs: list[dict], cap: dict, r6: sqlite3.Row,
                  r7: sqlite3.Row, n_writings: int) -> None:
    PREVIEW_FILE.parent.mkdir(parents=True, exist_ok=True)
    PREVIEW_FILE.write_text(
        "\n\n".join(f"--- [{m['role']}] ---\n{m['content']}" for m in msgs),
        encoding="utf-8",
    )
    user = msgs[1]["content"]
    print(f"区间：上上次 {r6['ts_start']} → 上次 {r7['ts_start']}"
          f"（{r7['ts_end']} 收尾）")
    print(f"会喂的增量：{n_writings} 条（上限 {config.REVIEW_EXPERIENCE_MAX}）")
    for line in cap["experiences"].splitlines():
        print("   ", line[:88])
    print(f"\n时间锚点：{cap['time_anchor'].strip()}")
    print(f"seed core {len(cap['old_core'])} 字 / relation "
          f"{len(cap['old_relation'])} 字 / 往来 {len(cap['exchanges'])} 字")
    print(f"system {len(msgs[0]['content'])} 字，user {len(user)} 字")
    print(f"增量区块位置 {user.find(config.REVIEW_EXPERIENCES_LABEL)} "
          f"/ user 全长 {len(user)}")
    print("完整请求已写入", PREVIEW_FILE.relative_to(ROOT))


# ── 真跑 ─────────────────────────────────────────────────────────────────

def do_run(r6_end: str) -> None:
    stamp = util.now().strftime("%Y%m%d-%H%M%S")

    # 备份必须在 db.connect() 之前：connect 会跑 _migrate，得留一份
    # 「动库之前」的原样，万一要回滚才干净。
    dest = BACKUP_DIR / f"fish.db.before-replay-{stamp}.db"
    backup_db(dest)
    print(f"\n已备份原库 → {dest.relative_to(ROOT)}")

    conn = db.connect()
    before_core = memory.get_narrative(conn, "core")
    before_relation = memory.get_narrative(conn, "relation")
    before_events = conn.execute("SELECT count(*) FROM events").fetchone()[0]
    before_note = memory.get_working_note(conn)
    before_note = (dict(before_note) if before_note else None)

    trace_id = f"replay-{stamp}"
    with patch.object(memory, "last_review_ts", return_value=r6_end):
        res = brain.run_self_review(conn, client=brain.make_client(),
                                    trace_id=trace_id)

    after_core = memory.get_narrative(conn, "core")
    after_relation = memory.get_narrative(conn, "relation")
    after_events = conn.execute("SELECT count(*) FROM events").fetchone()[0]
    after_note = memory.get_working_note(conn)
    after_note = (dict(after_note) if after_note else None)

    print("\n=== 自省结果 ===")
    print(" ", res["summary"])
    print(f"  core     {len(before_core or '')} → {len(after_core or '')} 字"
          f"（{'已更新' if after_core != before_core else '未变'}）")
    print(f"  relation {len(before_relation or '')} → "
          f"{len(after_relation or '')} 字"
          f"（{'已更新' if after_relation != before_relation else '未变'}）")
    if after_core and after_core != before_core:
        print("  新 core 开头：", after_core[:180].replace("\n", " "))
    print(f"  events   {before_events} → {after_events}")
    print(f"  工作记忆提示未变：{before_note == after_note}"
          f"（脚本不跑 wake，不扣 TTL，也不进自省素材）")
    print(f"  trace_id = {trace_id}，轨迹里 kind='review'")
    print("\n  最近 3 条事件：")
    for row in conn.execute(
        "SELECT ts, kind, why FROM events ORDER BY id DESC LIMIT 3"
    ).fetchall():
        print("   ", row["ts"], row["kind"], "|", (row["why"] or "")[:60])
    conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="补做 19:13 那次缺素材的自省")
    ap.add_argument("--run", action="store_true",
                    help="真跑（默认只预览，零 API、零写库）")
    args = ap.parse_args()

    if config.DRY_RUN:
        raise SystemExit("当前 FISH_DRY_RUN=1，自省会被短路。请 unset 后重试。")
    if args.run and not config.DEEPSEEK_API_KEY:
        raise SystemExit("没有 DEEPSEEK_API_KEY，无法调 API。")
    if args.run:
        # 和 fish.main 一样先抢单实例锁：脚本会写库，不能和真跑的鱼鱼并发。
        lock.acquire(mode_label="REPLAY")

    rel = Path(os.path.relpath(db.DB_PATH)).as_posix()
    conn = sqlite3.connect(f"file:{rel}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        r6, r7 = review_rounds(conn)
        n = assert_prerequisite(conn, r6["ts_end"], r7["ts_end"])
    finally:
        conn.close()

    msgs, cap = capture_messages(r6["ts_end"])
    print_preview(msgs, cap, r6, r7, n)

    if not args.run:
        print("\n预览完成：没有调 API、没有写库。确认后加 --run 执行。")
        return
    do_run(r6["ts_end"])


if __name__ == "__main__":
    main()
