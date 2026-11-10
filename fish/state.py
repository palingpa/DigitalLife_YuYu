"""状态层：鱼鱼的「身体」。

三条铁律（Design_v0.2.md §1 的不变量）在本文件执行：

I2  模型的输出永远不直接成为状态值。valence 仍可由模型自评一个
    -10..+10 的 delta「刻度」，这里除以 10 再加到 0..100 上并 clamp；
    boredom 则完全由服务端规则决定（见 apply_tool_effect）——它高会
    触发紧急唤醒，若模型能给自己加 boredom，就等于掌握了「让自己早点醒」
    的开关，自主性在结构上变假。

    energy 自 2026-11-10 夜起是纯「系统量」：只在「做动作」时按动作
    扣（见 apply_tool_effect）、只在「睡觉」时按分钟回（见
    apply_sleep_recovery），既不向 baseline 回归、也不接受模型自评。
    这是因为早期版本让 energy 自评 + 回归 baseline，结果被钉死在 55
    附近，她永远走不到「精力很低」、也就不会主动去休息——自主性同样变假。
"""

from dataclasses import dataclass
import sqlite3

from . import config


@dataclass
class State:
    """三个数值。范围都是 config.CLAMP = (5, 95)。"""

    energy: float    # 体能/唤醒度：疲惫 ←→ 清醒
    valence: float   # 效价：难受 ←→ 舒服
    boredom: float   # 无聊度：驱动量，只随时间涨，被新体验消耗


# ── 存取 ─────────────────────────────────────────────────────────────────

def load(conn: sqlite3.Connection) -> State:
    row = conn.execute("SELECT * FROM state WHERE id = 1").fetchone()
    return State(row["energy"], row["valence"], row["boredom"])


def save(conn: sqlite3.Connection, s: State) -> None:
    from . import util  # 延迟导入避免循环依赖
    conn.execute(
        "UPDATE state SET energy=?, valence=?, boredom=?, updated_at=? WHERE id=1",
        (s.energy, s.valence, s.boredom, util.now_iso()),
    )
    conn.commit()


# ── 动力学 ───────────────────────────────────────────────────────────────

def _clamp(v: float) -> float:
    lo, hi = config.CLAMP
    return max(lo, min(hi, v))


def tick_regress(s: State, minutes: float) -> None:
    """每 tick 调用：时间的自然流逝对状态的影响。

    只有 valence 向 baseline 回归——心情有「恢复力」，难过会淡，
    兴奋会平。回归率按实际经过的小时数缩放，所以 tick 晚点跑也准确。

    energy 不在这里回归（见文件头）：它是纯电池模型，只在「做动作」
    时按动作扣、只在「睡觉」时按分钟回血。若也向 baseline 回归，它会被
    钉死在 55 附近、永远走不到「精力很低」，自主性就成了假的。

    boredom 按分钟匀速上涨——这是唯一的「只增不减」量，是鱼鱼
    最终一定会去找事做的结构性原因（Design_v0.2 §4.1）。
    """
    hours = minutes / 60.0
    # 只有 valence 回归 baseline；energy 是电池，不回归
    cur = s.valence
    rate = min(config.REGRESS_PER_HOUR["valence"] * hours, 0.5)  # 0.5 上限防 tick 异常大
    s.valence = _clamp(cur + (config.BASELINE["valence"] - cur) * rate)
    s.boredom = _clamp(s.boredom + config.BOREDOM_PER_MIN * minutes)


def apply_sleep_recovery(s: State, minutes: float) -> float:
    """结算睡眠恢复：间隔够长的一次「睡觉」让 energy 额外回血。

    为什么需要它：电池模型下，tick_regress 不再管 energy，睡 75 分钟
    就什么都不回——「下次几点醒」会变成纯叙事选择，没有身体后果。
    这个函数让睡眠真的有区别，next_wake_in_min 才算一个带物理含义的决策。

    按分钟精确结算（2026-11-10 夜）：她一般睡不到一小时或 1~2 小时，
    用「每分钟 0.15」比「每小时 2」更贴合真实间隔。

    两道闸（config 注释里还有第三道 I2 不在服务端）：
    1. 间隔 < SLEEP_MIN_MIN（60 分）不算睡，一分血不回（打盹）。
    2. 单次封顶 SLEEP_MAX_BONUS（30）——睡再久也不一口气回满，
       防止「睡一觉从很低直接弹到充沛」。
    不再有「不超 baseline」的截顶：睡饱了就该比 baseline 更有精神。
    返回实际生效的 bonus（0 表示没睡够时长），调用方记轨迹用。
    """
    if minutes < config.SLEEP_MIN_MIN:
        return 0.0
    bonus = config.SLEEP_RECOVERY_PER_MIN * minutes
    bonus = min(bonus, config.SLEEP_MAX_BONUS)
    s.energy = _clamp(s.energy + bonus)
    return bonus


def apply_felt_delta(s: State, delta: dict) -> None:
    """应用模型的自评感受变化（仅 valence）。

    delta 是模型输出的 dict，如 {"valence": 3}。
    - 刻度 -10..+10 → 实际 -1..+1（除以 10）
    - 缺键 / 写错键直接忽略
    - 不处理 boredom（见文件头）；也不处理 energy——energy 自
      2026-11-10 夜起是纯系统量（动作扣、睡觉回），模型自评不再生效，
      否则她会把自己「想」累或「想」精神，自主性变假。
    """
    raw = delta.get("valence", 0)
    try:
        d = float(raw)
    except (TypeError, ValueError):
        return  # 模型给了非数字，忽略而不是崩溃
    d = max(-config.FELT_DELTA_RANGE, min(config.FELT_DELTA_RANGE, d))
    s.valence = _clamp(s.valence + d / 10.0)


def apply_tool_effect(s: State, tool_name: str, ok: bool, repeat_streak: int) -> None:
    """工具副作用：服务端说了算的规则，跟模型自评无关。

    注意刻度：这里用的是 0-100 的绝对刻度（跟 Design_v0.2 §4.1 一致），
    和 apply_felt_delta 的「除以 10」是两码事——除 10 只针对模型输出，
    服务端自己的规则想改多少就写多少。

    repeat_streak 是「加上这一次，连续第几次做同一件事」。规则模仿
    Design_v0.2 §4.1 的边际递减：第一次解闷最多，越重复越不管用。
    """
    if not ok:
        s.valence = _clamp(s.valence - 3.0)          # 事情办砸了，小受挫
        return

    # 解闷效果：15 → 10 → 5 → 5 ...（第 3 次起不再递减）
    relief = {0: 15.0, 1: 10.0}.get(max(repeat_streak - 1, 0), 5.0)
    s.boredom = _clamp(s.boredom - relief)

    effects = {
        # 读 / 搜都是「费神」的对外活动，扣得狠一点：5 次连读就该见底
        "read_feed":     {"energy": -10.0},
        "web_search":    {"energy": -10.0},
        # 写日记：费神但比读轻，且顺带 valence +3（写完舒坦）
        "write_journal": {"valence": 3.0, "energy": -8.0},
        "recall":        {},                             # 翻自己记忆不费神
        # 给小帕发一句话：把心思说出去微微舒坦；组织语言略费神。
        # 这里的 -2 只是「费神」维度，解闷仍走上面的标准边际递减。
        "send_message":  {"valence": 2.0, "energy": -2.0},
        # 主动自省：内省是轻度费神、但想清楚会微微通透（valence 微升）。
        # 比写日记(-8/+3) 轻，符合「只是自己想了想」。
        "self_review":   {"energy": -3.0, "valence": 1.0},
    }
    for attr, d in effects.get(tool_name, {}).items():
        setattr(s, attr, _clamp(getattr(s, attr) + d))


# def felt_text(s: State) -> str:
#     """数值 → 第一人称感受文本。

#     只描述状态，绝不给建议（「所以你应该去…」)
#     """
#     e = "很精神" if s.energy > 70 else "不累" if s.energy > 40 else "很累"
#     v = "心情不错" if s.valence > 65 else "没什么感觉" if s.valence > 35 else "有点闷"
#     if s.boredom > 75:
#         b = "闲得发慌，总想找点事"
#     elif s.boredom > 55:
#         b = "有点坐不住"
#     elif s.boredom > 30:
#         b = "还算安稳"
#     else:
#         b = "很平静"
#     return f"你现在{e}，{v}。{b}。"

def tier(x, words, cuts=None):
    if cuts is None:
        cuts = config.CUTS
    return words[sum(x > c for c in cuts)]

def felt_text(s):
    e = tier(s.energy,  ["精力很低", "精力偏低", "精力较好", "精力充沛"])
    # valence 的分档比另外两轴细，因为它实际能走动的范围窄：模型 delta
    # 一次最多 ±1（I2 的除以 10），工具失败 -3，还不断向 baseline 50
    # 回归——实测三天只走出过 53~60 这 7 分的区间。老分档 (25,50,75)
    # 把整个 50~75 都读成「情绪较好」，于是 2026-11-10 12:56 她明明
    # 扫兴（连读三条只有壳的内容，开始回避 read_feed），读数却说
    # 「情绪较好」——身体和自我叙事对不上。收窄后：baseline 附近
    # （50~58）读作「平平」，真的偏离才读作偏低/不错。
    v = tier(s.valence, ["情绪很低", "情绪偏低", "情绪平平", "情绪不错", "情绪很好"],
             cuts=(40, 50, 58, 70))
    b = tier(s.boredom, ["无聊感很弱", "无聊感较弱", "无聊感较强", "无聊感很强"])
    return f"{e}；{v}；{b}。"
