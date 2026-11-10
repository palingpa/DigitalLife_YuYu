"""状态动力学的单测。

对应 Design_v0.2.md §11 第 2 步：
「状态动力学错了，后面所有观察数据都是废的，而且你不会发现。」
所以这组测试是整个项目最重要的安全网。

跑法：在项目根目录执行  python -m pytest tests/ -q
"""

from fish import config
from fish.state import (State, tick_regress, apply_felt_delta, apply_tool_effect,
                        felt_text, apply_sleep_recovery)


def make_state(**kw) -> State:
    """造一个已知初值的状态，方便断言。"""
    base = {"energy": 55.0, "valence": 50.0, "boredom": 30.0}
    base.update(kw)
    return State(**base)


def test_boredom_only_goes_up():
    """boredom 随时间单调上涨，且永远不超上限。"""
    s = make_state()
    prev = s.boredom
    for _ in range(100):
        tick_regress(s, minutes=1.0)
        assert s.boredom >= prev
        prev = s.boredom
    # 100 分钟 × 0.03 = +3
    assert abs(s.boredom - 33.0) < 0.01


def test_boredom_capped_at_95():
    """无聊攒了一整天也封顶在 95。"""
    s = make_state(boredom=94.0)
    tick_regress(s, minutes=1440)   # 一整天
    assert s.boredom <= config.CLAMP[1]


def test_valence_regress_to_baseline():
    """valence 会慢慢回到 baseline，而不是漂走。

    2026-11-10 夜：energy 不再是「向 baseline 回归」的量——它是纯电池
    模型（动作扣、睡觉回），所以这里只断言 valence，并额外确认 energy
    在这样的长时间流逝里纹丝不动（不被回归拉走）。
    """
    s = make_state(energy=95.0, valence=10.0)
    for _ in range(500):
        tick_regress(s, minutes=60.0)   # 连续 500 小时
    assert s.energy == 95.0             # energy 不回归，保持初始
    assert s.valence < 51.0             # valence 被拉回 50 附近
    assert s.valence >= 50.0 - 0.001    # 但不会越过 baseline 反向漂移


def test_all_values_stay_in_clamp():
    """极端初值 + 极端 delta 之后，一切仍在 [5, 95]。

    注意：energy 现在是电池模型，tick_regress / felt_delta 都不碰它，
    只有 apply_tool_effect / apply_sleep_recovery（写它的两个地方）会
    clamp。所以这里 valence/boredom 走极端、energy 用正常初值，再额外
    验证「写 energy 的地方确实 clamp」。
    """
    for v in (0.0, 5.0, 95.0, 100.0):
        s = make_state(energy=55.0, valence=v, boredom=v)
        tick_regress(s, minutes=500)
        apply_felt_delta(s, {"valence": -10})
        for x in (s.valence, s.boredom):
            assert config.CLAMP[0] <= x <= config.CLAMP[1]

    # energy 的 clamp 由真正写它的地方保证
    low = make_state(energy=5.0)
    apply_tool_effect(low, "read_feed", ok=True, repeat_streak=1)   # -10 → 夹在 5
    assert low.energy >= config.CLAMP[0]
    high = make_state(energy=95.0)
    apply_sleep_recovery(high, minutes=1000.0)                      # 超量恢复 → 夹在 95
    assert high.energy <= config.CLAMP[1]


def test_felt_delta_is_divided_by_ten():
    """模型给的是 -10..+10 的刻度，实际影响只有 ±1。"""
    s = make_state(valence=50.0)
    apply_felt_delta(s, {"valence": 10})    # 模型喊出最大值
    assert abs(s.valence - 51.0) < 0.001    # 实际只涨了 1


def test_felt_delta_only_touches_valence():
    """felt_delta 只动 valence；boredom 和 energy 都不许被它碰。

    - boredom：高会触发紧急唤醒，若模型能给自己加 boredom，就等于掌握了
      「让自己早点醒」的开关（结构性假自主）。
    - energy：自 2026-11-10 夜起是纯系统量（动作扣、睡觉回），模型自评
      不再生效——否则她能把自己「想」累或「想」精神。
    """
    s = make_state(boredom=50.0, energy=55.0)
    apply_felt_delta(s, {"boredom": 10, "energy": 5})
    assert s.boredom == 50.0            # boredom 原样不动
    assert s.energy == 55.0             # energy 现在不吃自评


def test_felt_delta_tolerates_garbage():
    """模型输出字段缺失/非数字时，安静地当 0 处理，绝不崩溃。"""
    s = make_state()
    before = (s.energy, s.valence)
    apply_felt_delta(s, {"energy": "abc", "valence": None})
    assert (s.energy, s.valence) == before


def test_tool_relief_diminishes_with_repetition():
    """边际递减：连续做同一件事，解闷效果 15 → 10 → 5 → 5。"""
    s = make_state(boredom=80.0)
    apply_tool_effect(s, "read_feed", ok=True, repeat_streak=1)
    assert abs(s.boredom - 65.0) < 0.001    # -15
    apply_tool_effect(s, "read_feed", ok=True, repeat_streak=2)
    assert abs(s.boredom - 55.0) < 0.001    # -10
    apply_tool_effect(s, "read_feed", ok=True, repeat_streak=3)
    assert abs(s.boredom - 50.0) < 0.001    # -5
    apply_tool_effect(s, "read_feed", ok=True, repeat_streak=9)
    assert abs(s.boredom - 45.0) < 0.001    # 依然 -5


def test_failed_tool_bumps_valence_down():
    s = make_state(valence=50.0)
    apply_tool_effect(s, "read_feed", ok=False, repeat_streak=1)
    assert abs(s.valence - 47.0) < 0.001


def test_felt_text_has_no_advice():
    """感受文本只描述状态，绝不能夹带「所以你应该…」式的建议。

    2026-11-10 文案改为中性的「状态读数」风格（连「你现在」这种
    第二人称称呼都去掉了），断言跟着改，但测试目的不变：只报告，
    不指挥。
    """
    s = make_state(boredom=90.0, energy=80.0, valence=80.0)
    text = felt_text(s)
    assert "无聊感很强" in text          # 高 boredom 要如实报告
    for banned in ("应该", "建议", "不妨", "可以试试", "想去"):
        assert banned not in text


# ── 睡眠恢复（2026-11-10 新增）──────────────────────────────────────────

def test_sleep_recovers_more_than_being_awake():
    """同样的 60 分钟，睡着的恢复必须显著大于醒着——否则睡没有物理意义。

    2026-11-10 夜：醒着（tick_regress）完全不碰 energy 了，所以「醒着」
    那条 energy 原样不动；睡着按分钟回血 0.15*60=9。
    """
    awake = make_state(energy=44.0)
    tick_regress(awake, minutes=60.0)        # 现在不碰 energy

    asleep = make_state(energy=44.0)
    bonus = apply_sleep_recovery(asleep, minutes=60.0)

    assert abs(bonus - config.SLEEP_RECOVERY_PER_MIN * 60.0) < 0.001   # ≈ +9.0
    assert asleep.energy > awake.energy + 1.0               # 差距要可感知


def test_sleep_capped_and_can_exceed_baseline():
    """睡眠恢复的两道闸（2026-11-10 夜重定）：单次封顶、且能越过 baseline。

    旧版「不超 baseline」是 energy 被钉死在 55 的根因之一，已去掉：
    睡饱了就该比 baseline 更有精神，否则「下次几点醒」没有身体后果。
    """
    # 单次封顶 SLEEP_MAX_BONUS：睡再久也不超过这一口气
    huge = make_state(energy=40.0)
    bonus = apply_sleep_recovery(huge, minutes=1000.0)
    assert bonus == config.SLEEP_MAX_BONUS
    assert huge.energy == 40.0 + config.SLEEP_MAX_BONUS

    # 普通长睡（180 分钟）不受封顶，给满 0.15*180=27
    normal = make_state(energy=40.0)
    apply_sleep_recovery(normal, minutes=180.0)
    assert abs(normal.energy - 67.0) < 0.001

    # 关键变化：睡眠恢复可以越过 baseline（54.5 睡 180 分 → 81.5）
    rested = make_state(energy=54.5)
    apply_sleep_recovery(rested, minutes=180.0)
    assert rested.energy > config.BASELINE["energy"]


def test_short_nap_gets_nothing():
    """间隔小于 SLEEP_MIN_MIN 算打盹，一分血都不回。"""
    s = make_state(energy=44.0)
    assert apply_sleep_recovery(s, minutes=config.SLEEP_MIN_MIN - 1) == 0.0
    assert s.energy == 44.0


def test_valence_tiers_match_her_actual_range():
    """valence 分档要落在她实际活动的区间里（2026-11-10 改定）。

    实测三天 valence 只走出过 53~60：delta 一次最多 ±1、失败 -3、
    又不断向 baseline 50 回归。老分档 (25,50,75) 把这整个区间都读成
    「情绪较好」，她明明扫兴、身体却说不错——读数和自我叙事对不上。
    """
    # 12:56 那次的真实读数：55.45，她的自述是「扫兴但并不愤怒」
    assert "情绪平平" in felt_text(make_state(valence=55.45))
    # 她的历史峰值 59.3（把两期周刊串起来的那次，是真好）要读得出好
    assert "情绪不错" in felt_text(make_state(valence=59.3))
    # baseline 之下一点：读「偏低」而不是「很低」
    assert "情绪偏低" in felt_text(make_state(valence=45.0))
    assert "情绪很低" in felt_text(make_state(valence=30.0))
    # 极端好也还有一档
    assert "情绪很好" in felt_text(make_state(valence=80.0))


# ── energy 电池模型（2026-11-10 夜新增）────────────────────────────────

def test_energy_drains_on_activity():
    """活动按动作系统结算，不看模型自评；费神程度分档合理。

    设计意图：连续读/搜 5 次就该见底（见 test_energy_battery.py 的验收）。
    """
    for tool, expect in (("read_feed", -10.0), ("web_search", -10.0),
                         ("write_journal", -8.0), ("send_message", -2.0),
                         ("recall", 0.0)):
        s = make_state(energy=50.0)
        apply_tool_effect(s, tool, ok=True, repeat_streak=1)
        assert abs(s.energy - (50.0 + expect)) < 0.001, tool


def test_energy_recovers_on_sleep_only():
    """energy 只靠睡觉回血，且不超单次封顶；短打盹一分不回。"""
    s = make_state(energy=40.0)
    assert apply_sleep_recovery(s, minutes=config.SLEEP_MIN_MIN - 1) == 0.0
    s2 = make_state(energy=40.0)
    apply_sleep_recovery(s2, minutes=120.0)
    assert abs(s2.energy - (40.0 + config.SLEEP_RECOVERY_PER_MIN * 120.0)) < 0.001
