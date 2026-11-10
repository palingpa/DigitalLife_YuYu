"""energy 电池的批量模拟验收（2026-11-10 夜）。

直接调用 fish.state 的真实函数，跑三类场景 + 一轮蒙特卡洛，验证精力
机制改动是否达到小帕提的三个验收点：
  ① 连续活动 5 次 → 降到「精力很低」
  ② 空转一次（带一次正经休息）→ 至少升一档
  ③ 混合（接近真实）→ 在档位间游走，不钉死

另外做一轮蒙特卡洛，看长程分布是不是真的在动。
用法：python sim_energy.py
"""

import random

from fish import config
from fish.state import State, apply_sleep_recovery, apply_tool_effect, felt_text

TIERS = ["精力很低", "精力偏低", "精力较好", "精力充沛"]


def tier_of(e: float) -> str:
    return felt_text(State(e, 50.0, 30.0)).split("；")[0]


def tier_idx(e: float) -> int:
    return TIERS.index(tier_of(e))


def cycle(energy: float, action: str, gap_min: float) -> float:
    s = State(energy, 50.0, 30.0)
    if gap_min >= config.SLEEP_MIN_MIN:
        apply_sleep_recovery(s, gap_min)
    if action != "none":
        apply_tool_effect(s, action, ok=True, repeat_streak=1)
    return s.energy


def scenario_drain() -> bool:
    print("── 场景①：连续读 5 次（间隔 30 分，不足 SLEEP_MIN_MIN，不回血）──")
    e = config.BASELINE["energy"]
    for i in range(5):
        e = cycle(e, "read_feed", 30)
        print(f"  第{i+1}次读后: energy={e:5.1f}  {tier_of(e)}")
    ok = tier_of(e) == "精力很低"
    print(f"  结论: {'PASS ✅' if ok else 'FAIL ❌'}（应达 精力很低）\n")
    return ok


def scenario_rest() -> bool:
    print("── 场景②：从 精力很低 空转一次 + 一次正经休息（150 分）──")
    start = 20.0
    e = cycle(start, "none", 150)
    print(f"  休息前: {tier_of(start)} ({start})  休息后: {tier_of(e)} ({e:.1f})")
    ok = tier_idx(e) > tier_idx(start)
    print(f"  结论: {'PASS ✅' if ok else 'FAIL ❌'}（应至少升一档）\n")
    return ok


def scenario_mixed() -> bool:
    print("── 场景③：混合模式（接近真实，读多+偶尔休息/搜/写）──")
    pattern = [
        ("read_feed", 30), ("read_feed", 30), ("read_feed", 30), ("read_feed", 30),
        ("none", 150), ("read_feed", 30), ("web_search", 30), ("none", 150),
        ("read_feed", 30), ("write_journal", 30), ("none", 150), ("read_feed", 30),
        ("read_feed", 30), ("none", 180), ("read_feed", 30), ("none", 150),
        ("read_feed", 30), ("read_feed", 30), ("none", 180), ("read_feed", 30),
        ("none", 150), ("read_feed", 30), ("none", 180), ("read_feed", 30),
    ]
    e = config.BASELINE["energy"]
    seen = set()
    for i, (a, g) in enumerate(pattern):
        e = cycle(e, a, g)
        t = tier_of(e)
        seen.add(t)
        print(f"  #{i+1:2d} {a:12s} gap={g:3d} → {e:5.1f} {t}")
    ok = len(seen) >= 3
    print(f"  跨越档位: {[t for t in TIERS if t in seen]}（{len(seen)} 档）")
    print(f"  结论: {'PASS ✅' if ok else 'FAIL ❌'}（应在档位间游走，≥3 档）\n")
    return ok


def monte_carlo(n: int = 400, seed: int = 20261110) -> bool:
    print(f"── 蒙特卡洛：{n} 轮随机作息，看长程分布 ──")
    rnd = random.Random(seed)
    e = config.BASELINE["energy"]
    counts = {t: 0 for t in TIERS}
    for _ in range(n):
        # 70% 概率活动，30% 空转；活动里读/搜/写随机；间隔：
        # 活动后短间隔(20~50)，空转后长休息(120~180)
        if rnd.random() < 0.7:
            action = rnd.choice(["read_feed", "read_feed", "web_search", "write_journal"])
            gap = rnd.randint(20, 50)
        else:
            action = "none"
            gap = rnd.randint(120, 180)
        e = cycle(e, action, gap)
        counts[tier_of(e)] += 1
    total = sum(counts.values())
    print("  档位分布:")
    for t in TIERS:
        bar = "#" * int(40 * counts[t] / total)
        print(f"    {t}: {counts[t]:4d} ({100*counts[t]/total:4.1f}%) {bar}")
    ok = len([t for t in TIERS if counts[t] > 0]) >= 3
    print(f"  结论: {'PASS ✅' if ok else 'FAIL ❌'}（长程应横跨多档，非钉死）\n")
    return ok


if __name__ == "__main__":
    results = [scenario_drain(), scenario_rest(), scenario_mixed(), monte_carlo()]
    print("=" * 52)
    print("总览:", "全部 PASS ✅" if all(results) else "有 FAIL ❌")
