"""状态层：鱼鱼的「身体」。

三条铁律（Design_v0.2.md §1 的不变量）在本文件执行：

I2  模型的输出永远不直接成为状态值——它只能给 -10..+10 的 delta「刻度」，
    这里统一除以 10 再加到 0..100 的实际数值上，并且 clamp。
    换句话说：模型能表达「更累一点」，但不能说「我现在的能量是 5」。

    另外，实现时堵了文档里没写的一个漏洞：felt_delta 不允许碰 boredom。
    原因：boredom 高会触发紧急唤醒（§6.1 路径 B），如果模型能给自己加
    boredom，它就等于掌握了「让自己早点醒」的开关——自主性会在结构上
    变成假的。boredom 的所有变化只来自服务端规则（见 apply_tool_effect）。
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

    两件事：
    1. energy/valence 向 baseline 回归——心情有「恢复力」，难过会淡，
       兴奋会平。回归率按实际经过的小时数缩放，所以 tick 晚点跑也准确。
    2. boredom 按分钟匀速上涨——这是唯一的「只增不减」量，是鱼鱼
       最终一定会去找事做的结构性原因（Design_v0.2 §4.1）。
    """
    hours = minutes / 60.0
    for attr, base in config.BASELINE.items():
        cur = getattr(s, attr)
        rate = min(config.REGRESS_PER_HOUR[attr] * hours, 0.5)  # 0.5 上限防 tick 异常大
        setattr(s, attr, _clamp(cur + (base - cur) * rate))
    s.boredom = _clamp(s.boredom + config.BOREDOM_PER_MIN * minutes)


def apply_sleep_recovery(s: State, minutes: float) -> float:
    """结算睡眠恢复：间隔够长的一次「睡觉」让 energy 额外回血。

    为什么需要它：tick_regress 对醒着和睡着一视同仁，睡 75 分钟只从
    回归里拿回零点几——「下次几点醒」就变成了纯叙事选择，没有任何
    身体后果。这个函数让睡眠真的有区别，next_wake_in_min 才算一个
    带物理含义的决策。

    防滥用的闸门都在 config 注释里；这里只补一条：恢复量同时受
    baseline 限制——energy 55 以上睡觉不再回血，「睡饱即止」。
    返回实际生效的 bonus（0 表示没睡够时长），调用方记轨迹用。
    """
    if minutes < config.SLEEP_MIN_MIN:
        return 0.0
    bonus = config.SLEEP_RECOVERY_PER_HOUR * min(minutes, config.WAKE_MAX_GAP) / 60.0
    bonus = max(0.0, min(bonus, config.SLEEP_MAX_BONUS,
                         config.BASELINE["energy"] - s.energy))
    s.energy = _clamp(s.energy + bonus)
    return bonus


def apply_felt_delta(s: State, delta: dict) -> None:
    """应用模型的自评感受变化。

    delta 是模型输出的 dict，如 {"energy": -2, "valence": 3}。
    - 刻度 -10..+10 → 实际 -1..+1（除以 10）
    - 缺哪个键就当 0，写错的键直接忽略
    - 特意不处理 boredom，理由见文件头注释
    """
    for attr in ("energy", "valence"):
        raw = delta.get(attr, 0)
        try:
            d = float(raw)
        except (TypeError, ValueError):
            continue  # 模型给了非数字，忽略而不是崩溃
        d = max(-config.FELT_DELTA_RANGE, min(config.FELT_DELTA_RANGE, d))
        setattr(s, attr, _clamp(getattr(s, attr) + d / 10.0))


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
        "read_feed":     {"energy": -3.0},               # 读东西费神
        "write_journal": {"valence": 3.0},               # 写完日记舒坦一点
        "recall":        {},                             # 回忆基本不解闷，这正是看点
        "web_search":    {"energy": -3.0},               # 出门找东西同样费神
        # 给小帕发一句话：把心思说出去，微微舒坦；组织语言、等回应略费神。
        # 先不加 boredom 缓解的额外逻辑——这里的 -1 只是「费神」维度，
        # 解闷仍走上面的标准边际递减（她做了件事，无聊度自然降）。
        "send_message":  {"valence": 2.0, "energy": -1.0},
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
