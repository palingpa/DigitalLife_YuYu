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


def test_energy_valence_regress_to_baseline():
    """energy/valence 会慢慢回到各自的 baseline，而不是漂走。"""
    s = make_state(energy=95.0, valence=10.0)
    for _ in range(500):
        tick_regress(s, minutes=60.0)   # 连续 500 小时
    assert s.energy > 54.0              # 已被拉回 55 附近
    assert s.valence < 51.0
    # 但不会越过 baseline 反向漂移（0.001 容差，防浮点误差）
    assert s.energy <= 55.0 + 0.001
    assert s.valence >= 50.0 - 0.001


def test_all_values_stay_in_clamp():
    """极端初值 + 极端 delta 之后，一切仍在 [5, 95]。"""
    for v in (0.0, 5.0, 95.0, 100.0, -10.0):
        s = make_state(energy=v, valence=v, boredom=v)
        tick_regress(s, minutes=500)
        apply_felt_delta(s, {"energy": 10, "valence": -10})
        for x in (s.energy, s.valence, s.boredom):
            assert config.CLAMP[0] <= x <= config.CLAMP[1]


def test_felt_delta_is_divided_by_ten():
    """模型给的是 -10..+10 的刻度，实际影响只有 ±1。"""
    s = make_state(valence=50.0)
    apply_felt_delta(s, {"valence": 10})    # 模型喊出最大值
    assert abs(s.valence - 51.0) < 0.001    # 实际只涨了 1


def test_felt_delta_cannot_touch_boredom():
    """关键漏洞的回归测试：模型不能通过自评 delta 操纵 boredom。

    boredom 高会触发紧急唤醒，这个口子一旦打开，
    模型就等于掌握了「让自己早点醒」的开关。
    """
    s = make_state(boredom=50.0)
    apply_felt_delta(s, {"boredom": 10, "energy": 5})
    assert s.boredom == 50.0
    assert s.energy == 55.5             # delta=5 → 实际 +0.5


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
    """同样的 60 分钟，睡着的恢复必须显著大于醒着——否则睡没有物理意义。"""
    awake = make_state(energy=44.0)
    tick_regress(awake, minutes=60.0)

    asleep = make_state(energy=44.0)
    bonus = apply_sleep_recovery(asleep, minutes=60.0)

    assert bonus >= config.SLEEP_RECOVERY_PER_HOUR - 0.01   # ≈ +2.0
    assert asleep.energy > awake.energy + 1.0               # 差距要可感知


def test_sleep_is_capped_and_never_exceeds_baseline():
    """三道闸：超时不加算、单次封顶、不越过 baseline。"""
    # 睡 500 分钟 = 睡 180 分钟（超过 WAKE_MAX_GAP 不计）
    long1 = make_state(energy=40.0)
    apply_sleep_recovery(long1, minutes=500.0)
    long2 = make_state(energy=40.0)
    apply_sleep_recovery(long2, minutes=180.0)
    assert long1.energy == long2.energy

    # 单次封顶 SLEEP_MAX_BONUS
    assert long2.energy == 40.0 + config.SLEEP_MAX_BONUS

    # energy 已经在 baseline 附近 → 只补差额（55 - 54.5 = 0.5），不越过 baseline
    rested = make_state(energy=54.5)
    bonus = apply_sleep_recovery(rested, minutes=180.0)
    assert bonus <= 0.5 + 0.001
    assert rested.energy <= config.BASELINE["energy"]


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
