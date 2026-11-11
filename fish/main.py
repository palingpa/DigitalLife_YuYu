"""入口：把所有循环跑起来。

启动：  python -m fish.main
空跑：  FISH_DRY_RUN=1 python -m fish.main     （不调 API，验证链路）
测试：  python -m pytest tests/ -q
"""

import asyncio

from . import brain, config, db, feed, lock, scheduler


async def main() -> None:
    # 单实例锁必须是第一件事：在任何数据库/日志写入之前就把第二条鱼拦住。
    # 不区分 DRY_RUN / 真实模式——空跑撞上真跑同样拒绝（2026-11-11 事故）。
    mode = "DRY_RUN" if config.DRY_RUN else "REAL"
    lock.acquire(mode_label=mode)

    conn = db.connect()

    client = brain.make_client()
    if not config.DRY_RUN and not config.DEEPSEEK_API_KEY:
        raise SystemExit(
            "没有找到 DEEPSEEK_API_KEY。\n"
            "  - 真跑：先设置环境变量（见 README）\n"
            "  - 只想空跑验证链路：FISH_DRY_RUN=1 python -m fish.main"
        )

    # gather = 同时跑多个协程，任何一个崩了都会把异常抛到这里
    await asyncio.gather(scheduler.run(conn, client), feed.feed_loop(conn))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # Ctrl+C 是正常退出方式，不是错误。鱼鱼下次醒来会接着上次的状态。
        print("\n鱼鱼睡了。（状态和日志都已落库，下次启动接着跑）")
