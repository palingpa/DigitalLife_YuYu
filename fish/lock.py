"""进程级单实例锁：保证这台机器上永远只有一条鱼（不分 DRY_RUN / 真实）。

背景（2026-11-11 事故）：scheduler 的 wake_lock 是 asyncio.Lock，它只活在
单个进程的内存里。两个终端各起一份 fish.main 时，两个锁互相看不见，
于是出现两条鱼同时跑——状态库被两份调度交错读写，"她"的连续性被打断。

方案：data/fish.lock + msvcrt.locking（Windows 的 OS 级字节范围锁）。
选它是因为两个关键性质，缺一不可：

1. **OS 保证排他**：第二个进程上锁会立刻失败，不存在"两个进程同时
   检查发现没人"的竞态窗口。（自己扫进程列表、查 pid 文件都有这个竞态）
2. **进程死锁自动消失**：正常退出、崩溃、被杀、断电，OS 都会释放锁。
   所以**永远不存在"残留死锁"**——不需要心跳、过期时间、stale 检测
   这些容易写错反而制造假死的补丁。

锁文件里写的 PID/启动时间只是给人看的诊断信息，判定只认 OS 锁本身。
另有一份启动日志（logs/startup.log）：每次启动尝试（成功/被拒）各记
一行带时间戳的 JSON，方便日后回溯"当时到底起了几个进程"。

注意 msvcrt 是 Windows 专属模块；本项目只跑在 Windows 上，不做兼容层。
"""

import json
import os
import sys

from . import config, util

# 进程活着期间一直持有这个 fd，绝不能主动 close——一 close 锁就没了。
# 放在模块级只为表达"它属于整个进程，不属于某次调用"。
_LOCK_FD: int | None = None

# 锁哪个字节。不能锁第 0 字节：Windows 上被锁的区间对其他进程连"读"
# 都不允许，而锁文件开头恰恰是我们要留给被拒进程读的诊断信息。
# 所以锁一个远在数据之后的字节（锁超过文件末尾是合法的），两者互不干扰。
_LOCK_OFFSET = 1024


def _append_log(log_path, record: dict) -> None:
    """往启动日志追加一行 JSON。

    日志写失败（磁盘满、权限）不该拦住鱼鱼启动，所以吞掉 OSError。
    但也不吞别的异常——那样真有 bug 时还能炸出声音来。
    """
    try:
        line = json.dumps(record, ensure_ascii=False)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def acquire(lock_path=None, log_path=None, mode_label: str = "?") -> int:
    """尝试取得单实例锁。成功返回 fd（进程整个生命周期持有）；
    已有别的鱼在跑则打印诊断信息、记 BLOCKED 日志，然后 SystemExit。

    lock_path / log_path 参数是给测试用的（pytest 的 tmp_path），
    生产代码一律用默认的 data/fish.lock 和 logs/startup.log。
    """
    path = lock_path or (config.DATA_DIR / "fish.lock")
    log = log_path or (config.LOGS_DIR / "startup.log")

    # O_CREAT：文件不存在就建。上锁不依赖文件里有没有内容。
    fd = os.open(path, os.O_CREAT | os.O_RDWR)
    try:
        # LK_NBLCK = 非阻塞锁：拿不到立刻抛 OSError，而不是干等。
        # 需要先 seek 到目标字节——locking 从当前位置开始锁。
        import msvcrt
        os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    except OSError:
        # 拿不到锁 → 已有一条鱼。把持有者信息从锁文件里读出来给人看。
        # 只读前 _LOCK_OFFSET 字节：那是没被锁的数据区，读得动。
        os.lseek(fd, 0, os.SEEK_SET)
        raw = os.read(fd, _LOCK_OFFSET).decode("utf-8", errors="replace").strip()
        try:
            holder = json.loads(raw)
        except ValueError:
            holder = {"raw": raw or "(空)"}
        os.close(fd)
        _append_log(log, {
            "ts": util.now_iso(),
            "kind": "BLOCKED",
            "pid": os.getpid(),
            "mode": mode_label,
            "holder_pid": holder.get("pid"),
        })
        raise SystemExit(
            f"已有一条鱼在跑（PID {holder.get('pid', '?')}，"
            f"启动于 {holder.get('started', '?')}，模式 {holder.get('mode', '?')}）。\n"
            "本机永远只允许一条鱼。先确认旧进程不需要了（Ctrl+C 或关掉那个终端），再启动新的。"
        )

    # 走到这里说明锁是我们的了。写诊断信息（先清掉可能残留的旧内容）。
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, json.dumps({
        "pid": os.getpid(),
        "mode": mode_label,
        "started": util.now_iso(),
    }, ensure_ascii=False).encode("utf-8"))

    # 注意：这里故意不 close。fd 随进程存活，进程一死 OS 自动放锁。
    global _LOCK_FD
    _LOCK_FD = fd

    _append_log(log, {
        "ts": util.now_iso(),
        "kind": "START",
        "pid": os.getpid(),
        "mode": mode_label,
    })
    return fd
