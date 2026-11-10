"""energy 电池模型的验收（对应小帕 2026-11-10 夜提出的三个验收点）。

直接调用 fish.state 的真实函数，不复制逻辑——测的是「真机制」而不是
一份会悄悄和代码漂移的副本。

档位（felt_text 的精力轴，CUTS=(25,50,75)）：
    精力很低(≤25) < 精力偏低(25~50) < 精力较好(50~75) < 精力充沛(>75)

跑法：python -m pytest tests/test_energy_battery.py -q
"""

from fish import config
from fish.state import State, apply_sleep_recovery, apply_tool_effect, felt_text

TIERS = ["精力很低", "精力偏低", "精力较好", "精力充沛"]


def _energy_after(energy: float, action: str, gap_min: float) -> float:
    """模拟「睡 gap 分钟 → 做一次动作」一个完整唤醒周期。"""
    s = State(energy, 50.0, 30.0)
    if gap_min >= config.SLEEP_MIN_MIN:
        apply_sleep_recovery(s, gap_min)
    if action != "none":   # "none" 表示空转，没有动作扣血
        apply_tool_effect(s, action, ok=True, repeat_streak=1)
    return s.energy


def _tier(e: float) -> str:
    return felt_text(State(e, 50.0, 30.0)).split("；")[0]


def _tier_idx(e: float) -> int:
    return TIERS.index(_tier(e))


def test_consecutive_activity_reaches_very_low():
    """验收①：连续读 5 次（快速续读，间隔不足 SLEEP_MIN_MIN，不回血）
    应当已经降到「精力很低」。

    这对应小帕的观察：她一直在读，但旧机制把 energy 钉在 baseline 附近，
    永远走不到「精力很低」、也就不会主动去休息。
    """
    e = config.BASELINE["energy"]     # 55 起步
    gap = 30                          # 快速续读：间隔 < 60 分钟，无恢复
    for _ in range(5):
        e = _energy_after(e, "read_feed", gap)
    assert _tier(e) == "精力很低"


def test_idle_with_rest_rises_at_least_one_tier():
    """验收②：从「精力很低」空转一次 + 一次正经休息（150 分钟），
    应当至少上升一个档位。

    空转本身不扣血，恢复全靠那一觉——这正是「累到很低 → 歇一觉回血」
    的闭环。
    """
    start = 20.0                       # 精力很低
    e = _energy_after(start, "none", 150)   # 空转 + 150 分钟休息
    assert _tier_idx(e) > _tier_idx(start)


def test_mixed_wanders_across_tiers():
    """验收③：混合作息（接近真实，读多 + 偶尔休息/搜/写）应当在档位间
    游走，而不是钉死在某一档。

    模式里短间隔(30)的活动不回血、长间隔(150~180)的休息回血，于是
    energy 随「读→累→歇→回→再读」上下起伏。
    """
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
    for action, gap in pattern:
        e = _energy_after(e, action, gap)
        seen.add(_tier(e))
    # 至少横跨 3 个档位，证明它不是固定在某一档
    assert len(seen) >= 3
