"""调用轨迹：每一次 API 调用的完整输入、输出、思考过程。

为什么单独一层，而不是塞进 events 表？
events 是「观察层」——一次唤醒一行，用来算指标、看趋势。
轨迹是「原料层」——一次唤醒可能有两次调用（重试、自省），每次
几千 token 的原文。混在一起，events 就没法看了。

为什么用 JSONL 而不是数据库表？
- 追加写，一行一条，进程崩了也最多丢最后一行
- 不用建表、不用迁移，字段随时可以加
- 原生是文本，grep/less/看原文都方便
代价是不能直接 SQL 查询——但真要做统计，读进 Python 也很简单，
而且这一层的量（每天几十条）根本不需要索引。

文件位置：logs/traces/YYYY-MM-DD.jsonl
（pytest 运行时由 tests/conftest.py 切到 logs/traces_test/，与真实日志隔离）
"""

import json
import uuid
from pathlib import Path

from . import config


def new_id() -> str:
    """给一次唤醒编个号，让 events 行和轨迹行对得上。"""
    return uuid.uuid4().hex[:12]


# 测试模式开关：pytest 启动时由 tests/conftest.py 的 autouse fixture 打开。
# 打开后，所有 record / 读取都走 config.TRACE_TEST_DIR，与真实日志彻底隔离——
# 因为测试记录用的也是 12 位 hex trace_id，和真机一模一样，只能靠目录区分。
_test_mode = False


def set_test_mode(on: bool) -> None:
    """测试时切到单独的轨迹目录，避免污染 logs/traces/。"""
    global _test_mode
    _test_mode = bool(on)


def _base_dir() -> Path:
    """当前轨迹根目录：测试模式走 traces_test/，否则走 traces/。"""
    return config.TRACE_TEST_DIR if _test_mode else config.TRACE_DIR


def _path_for_today() -> Path:
    from . import util
    # 测试模式下写到 traces_test/，与真实日志分开
    d = _base_dir() / f"{util.now():%Y-%m-%d}.jsonl"
    d.parent.mkdir(parents=True, exist_ok=True)
    return d


def record(kind: str, trace_id: str, **fields) -> None:
    """追加一条轨迹。kind 有：decision / reaction / review / tool_result / error。

    所有字段原样存，不裁剪——这一层的价值就在于「完整」。
    将来分析鱼鱼的 reasoning 怎么演化，靠的就是这里的原文。
    """
    if not config.TRACE_ENABLED:
        return
    from . import util
    line = {"ts": util.now_iso(), "kind": kind, "trace_id": trace_id, **fields}
    with _path_for_today().open("a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")


def iter_records(day: str | None = None):
    """按行读某天的轨迹（day 形如 '2026-11-09'，默认今天）。"""
    from . import util
    path = _base_dir() / f"{day or f'{util.now():%Y-%m-%d}'}.jsonl"
    if not path.exists():
        return
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def tail(n: int = 5) -> list[dict]:
    """最近 n 条（跨天时按文件名排序拼接）。"""
    files = sorted(_base_dir().glob("*.jsonl"))
    out: list[dict] = []
    for p in files:
        with p.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
    return out[-n:]


# ── 命令行查看器 ────────────────────────────────────────────────────────
# 用法：python -m fish.trace 3     → 打印最近 3 条，带格式

def _main() -> None:
    import sys
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    for rec in tail(n):
        print("=" * 70)
        print(f"[{rec['ts']}] {rec['kind']}  trace={rec.get('trace_id')}")
        if rec["kind"] in ("decision", "reaction"):
            label = "第一段 决策" if rec["kind"] == "decision" else "第二段 反应"
            print(f"  {label}")
            print(f"  模型 {rec.get('model')}  温度 {rec.get('temperature')}  第 {rec.get('attempt')} 次")
            print(f"  token: 入 {rec.get('tokens_in')} / 出 {rec.get('tokens_out')}  耗时 {rec.get('duration_ms')}ms")
            for m in rec.get("messages", []):
                print(f"\n  ── {m['role']} ──")
                print("  " + m["content"].replace("\n", "\n  "))
            print("\n  ── 回复 ──")
            print("  " + (rec.get("content") or "").replace("\n", "\n  "))
            if rec.get("reasoning"):
                print("\n  ── 思考过程 ──")
                print("  " + rec["reasoning"].replace("\n", "\n  "))
        elif rec["kind"] == "tool_result":
            print(f"  {rec.get('tool')}  ok={rec.get('ok')}")
            print(f"  摘要: {rec.get('summary')}")
            if rec.get("detail"):
                print(f"  全文({len(rec['detail'])}字): {rec['detail'][:200]}…")
        else:
            print(f"  {json.dumps({k: v for k, v in rec.items() if k not in ('ts','kind','trace_id')}, ensure_ascii=False)[:400]}")
    print("=" * 70)


if __name__ == "__main__":
    _main()
