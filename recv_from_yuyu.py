"""鱼鱼 → 小帕：收鱼鱼发来的消息（终端适配器，v1 极简版）。

v1 行为（用户拍板）：只做「打印 + 追加日志 + 标记已读」，不做桌面通知
（通知以后接聊天软件适配器时再说）。

    - 打印所有「鱼鱼发出、但小帕还没读过」的 from_yuyu 消息
    - 追加进 data/from_yuyu.log（带时间戳）
    - 标记 delivered_to_xiaopa=1，下次运行不再重复打印

用法：
    python recv_from_yuyu.py
"""

from fish import config, db, messages


def main() -> None:
    conn = db.connect()
    rows = messages.fetch_undelivered_from_yuyu(conn)
    if not rows:
        print("鱼鱼暂时没有新消息。")
        return

    log_path = config.DATA_DIR / "from_yuyu.log"
    # db.connect() 已经确保 data/ 目录存在
    with log_path.open("a", encoding="utf-8") as f:
        for m in rows:
            line = f"[{m['ts']}] 鱼鱼：{m['text']}"
            print(line)
            f.write(line + "\n")
            messages.mark_delivered(conn, m["id"])

    print(f"\n共 {len(rows)} 条，已写入 {log_path} 并标记已读。")


if __name__ == "__main__":
    main()
