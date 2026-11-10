"""防「两条鱼」回归：进程级单实例锁的排他性与启动日志。

2026-11-11 事故：两个进程各起一份 fish.main，asyncio.Lock 只活在各自
进程里互不可见。现在由 fish/lock.py 的 OS 级锁兜底，这组测试锁死它的
三个关键性质：
1. 已有持有者时，第二个进程必须被拒绝（SystemExit）；
2. 锁随进程消失（close 即释放），不存在残留死锁；
3. 每次启动尝试（成功 / 被拒）都各留一行带时间戳的启动日志。
"""

import json
import os

import pytest

from fish import lock


def test_second_acquire_is_blocked(tmp_path):
    """第一个进程持有锁期间，第二个进程必须拿不到。"""
    p = tmp_path / "fish.lock"
    log = tmp_path / "startup.log"

    fd1 = lock.acquire(lock_path=p, log_path=log, mode_label="REAL")
    try:
        # 同一进程、不同的文件句柄再上锁也必须失败——OS 字节范围锁
        # 是按"文件+区间"排他的，不看进程是谁，正好测出最严的情况。
        with pytest.raises(SystemExit):
            lock.acquire(lock_path=p, log_path=log, mode_label="DRY_RUN")
    finally:
        os.close(fd1)


def test_lock_releases_on_close(tmp_path):
    """模拟进程退出（close fd）后，锁必须自动消失，新进程能正常拿到。

    这是"永远不会有残留死锁"的根基：不需要任何清理逻辑。
    """
    p = tmp_path / "fish.lock"
    fd1 = lock.acquire(lock_path=p, log_path=tmp_path / "s.log", mode_label="REAL")
    os.close(fd1)   # 进程死亡的等价物
    fd2 = lock.acquire(lock_path=p, log_path=tmp_path / "s.log", mode_label="REAL")
    os.close(fd2)   # 能走到这里就说明第二次 acquire 成功了


def test_blocked_message_contains_holder_pid(tmp_path):
    """被拒时 SystemExit 的文案要带上持有者的 PID，方便定位旧进程。"""
    p = tmp_path / "fish.lock"
    fd1 = lock.acquire(lock_path=p, log_path=tmp_path / "s.log", mode_label="REAL")
    try:
        with pytest.raises(SystemExit) as e:
            lock.acquire(lock_path=p, log_path=tmp_path / "s.log", mode_label="REAL")
        assert f"PID {os.getpid()}" in str(e.value)
    finally:
        os.close(fd1)


def test_startup_log_records_start_and_blocked(tmp_path):
    """每次启动尝试各留一行 JSON：成功的 START、被拒的 BLOCKED。"""
    p = tmp_path / "fish.lock"
    log = tmp_path / "startup.log"

    fd1 = lock.acquire(lock_path=p, log_path=log, mode_label="DRY_RUN")
    try:
        with pytest.raises(SystemExit):
            lock.acquire(lock_path=p, log_path=log, mode_label="REAL")
    finally:
        os.close(fd1)

    lines = [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines()]
    assert [l["kind"] for l in lines] == ["START", "BLOCKED"]
    assert lines[0]["mode"] == "DRY_RUN"
    assert lines[1]["mode"] == "REAL"
    assert lines[1]["holder_pid"] == os.getpid()   # 拒我的是我自己起的第一个进程
    # 每行都要有能排序定位的时间戳
    assert all(l["ts"] for l in lines)
