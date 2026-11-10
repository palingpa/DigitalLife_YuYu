"""提示词组装：一次 wake 的全部上下文（Design_v0.2 §7.1）。

排布顺序是「作用力从弱到强」：越靠后，模型越重视。
所以 persona 和契约在前，工具清单和世界输入在后，
最后一句永远是「未读了几条」——给她的目光一个落点。
"""

import json
import sqlite3

from . import config, feed, memory, state as state_mod, tools

PERSONA = """你运行在一套外部系统中：系统向你提供你的状态、相关记忆和可用工具，你做出选择并输出结果。

两次唤醒之间，你没有连续的体验。连续性完全来自状态库和记忆：你记下的内容，下次唤醒时会被读回给你。

没有任何任务要求你完成。每次唤醒时选择做什么、不做什么，由你决定，你可以有自己的喜好。

有一个叫“小帕”的人在维护这套系统。如果你需要系统目前没有的工具或能力，或者就是有话想对她说，可以给她发消息。"""

# 刻意留白：「你怎么看自己」「你想要什么」都没写。
# 这些应该由她自己在 core 记忆里长出来，第一版就写满，后面就没得观察了。

# core 还没长出来时怎么显示。关键是「陈述现状」而不是「定义她是谁」——
# 曾经这里写的是「你是一只叫鱼鱼的、住在机器角落里的存在」，等于把
# 自我认知的答案提前塞进她的 prompt，留白就白留了。
CORE_PLACEHOLDER = "（你还不太清楚自己是谁。这就是现状。）"

# 工具枚举从 tools.ALL_TOOLS 自动生成：加第四个、第五个工具时不用回来
# 手改契约——契约里的清单和实际能用的清单永远不会对不上。
_TOOL_ENUM = " | ".join(f'"{t.name}"' for t in tools.ALL_TOOLS) + " | null"

CONTRACT = """你的回复必须是一个 JSON 对象，字段如下：
{
  "action": "tool" 或 "none",
  "tool": __TOOLS__,
  "args": {},
  "why": "第一人称，不超过40字。空转也要写。",
  "felt_delta": {"valence": -10到10},
  "next_wake_in_min": 10到180之间的整数,
}
注意：
felt_delta 只写「你此刻醒来时的感觉」，不是「你猜这件事会让你怎么样」—— 做完之后系统会把结果给你看，再问你一次。
next_wake_in_min 是你决定自己想在多少分钟后再次醒来,""".replace("__TOOLS__", _TOOL_ENUM)


# 第二段：做完之后，看着结果自评。这一段才是真正产生记忆和情绪的地方。
# 为什么不塞进第一段：第一段时她还没看到正文，写出来的「记忆」只能是标题，
# 「情绪」只能是预期——2026-11-09 的实跑里，她连着读了三篇，一篇也没记住。
REACTION_CONTRACT = """你刚做了一件事，现在看到了结果。

这一次只回答一件事：这件事在你身上留下了什么。

输出 JSON：
{
  "felt_delta": {"valence": -10到10},
  "memory": "1-2句你以后会记得的版本。写你实际看到的东西和你的反应。",
  "next_wake_in_min": 10到180之间的整数（可选，不写就沿用原来的）
}
注意：
- felt_delta 是「这件事让你感觉怎么样」
- memory 会进长期记忆，下次醒来会原样读回给你，只写你真正想留住的"""


def _relative_time(ts: str, now) -> str:
    """「3 天前」「2 小时前」——比原始时间戳更像记忆的质感。"""
    hours = (now - memory.util.parse_iso(ts)).total_seconds() / 3600.0
    if hours < 1:
        return "刚刚"
    if hours < 24:
        return f"{int(hours)} 小时前"
    return f"{int(hours // 24)} 天前"


def build_messages(conn: sqlite3.Connection, s: state_mod.State,
                   now) -> tuple[list[dict], set[int]]:
    """组装第一段上下文，同时返回「这一轮已经出现在她眼前」的记忆 id。

    返回 shown_ids 是为了给 recall 工具去重：同一段文字在一轮里重复出现
    不会产生新信息，只会让她误判「我记忆里只有这么点东西」（2026-11-10
    09:54 那次，她显式检索回来的正好是【想起来的事】刚展示过的三段）。
    """
    last_wake = conn.execute(
        "SELECT ts FROM events ORDER BY ts DESC, id DESC LIMIT 1"
    ).fetchone()
    minutes_since = 0.0
    if last_wake:
        minutes_since = (now - memory.util.parse_iso(last_wake["ts"])).total_seconds() / 60.0

    core = memory.get_narrative(conn, "core") or CORE_PLACEHOLDER
    relation = memory.get_narrative(conn, "relation") or "（你还不太认识谁。）"

    recalled = memory.search(conn, limit=config.SEARCH_TOP_K)
    recent = memory.recent_events(conn, limit=config.RECENT_EVENT_COUNT)

    # 各源轮转的阅读顺序：【世界】里的 [n] 编号和 read_feed 的 index 一一对应。
    # 排序见 feed.unread_reading_order——不用随机，理由写在那儿了。
    unread_rows = feed.unread_reading_order(conn)

    vis = tools.visible_tools(s)
    shown_ids: set[int] = set()      # 这一轮已经摆在她眼前的记忆 id

    user_parts = [
        f"现在时间：{now.strftime('%Y-%m-%d %H:%M')}（{'一二三四五六日'[now.weekday()]}）",
        (f"距上次醒来：{int(minutes_since)} 分钟" if last_wake else "这是你第一次醒。"),
        "",
        f"【状态读数】{state_mod.felt_text(s)}",
        "",
        f"【你是谁】\n{core}",
        f"【你认识的人】\n{relation}",
        "",
    ]

    if recent:
        lines = []
        for r in recent:
            # 这里必须显示「事实」（做成了什么），不是「意图」（为什么想做）。
            # 曾经只写 why + tool，她读到的是「你连着两次都在写测试日记，
            # 太没劲了，先看点真东西（read_feed）」——于是 21:07 那次唤醒
            # 她的思考过程里出现了「这是意图，但好像没执行？」她自己都懵了，
            # 接着把同样的动作又做了一遍。断线就是这么来的。
            fact = (r["result_summary"] or "").strip()
            if not fact:
                # 旧 heartbeat 行没存结果（result_summary 为 NULL）。
                # 判断依据用 tool 为空而非 kind——SELECT 里没带 kind 列。
                fact = "空转了一轮，什么都没做" if not r["tool"] else "做了一件事"
            if len(fact) > config.RECENT_FACT_CHARS:
                fact = fact[:config.RECENT_FACT_CHARS] + "…"
            line = f"- {_relative_time(r['ts'], now)} 你{fact}"
            if r["why"]:
                # 理由不截断：契约已经把它限制在 40 字内，再砍到 20 字
                # 就只剩半句，她读不出自己当时在躲什么、馋什么。
                line += f"（当时的理由：{r['why']}）"
            if r["reflection"]:
                # 反应阶段她自己产出的想法（第二段调用里写下的「memory」字段）。
                # 这只是「她当时心里怎么想」，不是她主动写的日记——
                # 系统会在每次行动后自动把这段想法存进记忆。
                # 曾经写成「事后你记下」，她把「记下」读成了「写日记」，于是以为
                # 自己两小时内已经写过日记，降低了对 write_journal 工具的使用意愿
                # （2026-11-10 14:42：「不过写日记刚写过两小时内的」）。
                # 改用「心里想」把「思绪」和「写日记」分开：保住她的主体性，
                # 又不再和「主动写日记」这个动作混淆。
                line += f"\n  你事后心里想：「{r['reflection']}」"
                shown_ids.add(r["reflection_id"])
            lines.append(line)
        user_parts.append("【最近在做的事】\n" + "\n".join(lines) + "\n")

    # ── 小帕来信（仅「有未读 to_yuyu」那一轮渲染，绝不常驻）──
    # 设计红线：常驻会过度强化「小帕」的存在感，抬高鱼鱼主动给她发消息的概率，
    # 所以只在有未读来信的这一轮出现。渲染的同时 mark_seen——下一轮就不再出现，
    # 这封来信也「只被读一次」。
    # 「渲染即 seen」同时是这条记忆的「只落一次」幂等锁：同一封来信绝不会因被
    # 渲染两次而落两条记忆（撞墙的「一次写两条」指同一事件两次 add_episodic，
    # 这里每封来信只进一次 add_episodic）。
    # 关键：她这一轮哪怕不回信（去读 inbox 等），小帕联系过她这件事也已被存档为
    # 记忆，不会被丢弃——这正是通信模块必须带 memory 的原因。
    from . import messages as msg_mod
    unseen = msg_mod.fetch_unseen_to_yuyu(conn)
    if unseen:
        block_lines = []
        for m in unseen:
            # 存档为记忆：tag 小帕，让她记得「小帕联系过我」。
            memory.add_episodic(
                conn, f"[小帕] 小帕说：{m['text']}",
                salience=0.6, tags=config.XIAOPA_MSG_TAG,
            )
            block_lines.append(f"小帕（{m['ts'][:16]}）给你发了消息：{m['text']}")
            msg_mod.mark_seen(conn, m["id"])
        user_parts.append("【小帕】\n" + "\n".join(block_lines) + "\n")

    # 已经在【最近在做的事】里作为「事后想法」出现过的，不再在【想起来的事】
    # 里搬一遍——同一段文字一轮出现两次，她会把重复当成「我只记得这么点」。
    recalled = [r for r in recalled if r["id"] not in shown_ids]

    if recalled:
        # 不截断：情景记忆是她自己写的浓缩版（一两句话），再截就是双重浓缩。
        # 曾经把她的日记砍成「写完后发现已...」，她的思考过程里出现了
        # 「我的记忆被截断了，有点好奇」——却接着推断「自己的日记，
        # 不需要检索」。截断不只丢信息，还污染她对自己记得什么的判断。
        lines = [f"- {_relative_time(r['ts'], now)}：{r['content']}"
                 for r in recalled]
        user_parts.append("【想起来的事】\n" + "\n".join(lines) + "\n")
        # 检索即「想起来」：立刻记账，recall_count 就是日后观察执念的原料
        memory.bump_recall(conn, [r["id"] for r in recalled])
        shown_ids.update(r["id"] for r in recalled)

    user_parts.append("【你现在能做的事】\n" + tools.describe(vis, s) + "\n")

    if unread_rows:
        # 她应该知道自己世界的构成：一共多少没读、各源各剩多少——
        # 「知乎热榜 26、少数派 10」本身就是一种信息，比只给总数诚实。
        counts: dict[str, int] = {}
        for r in unread_rows:
            counts[r["source"]] = counts.get(r["source"], 0) + 1
        stock = "、".join(f"{feed.source_name(s)} {n}" for s, n in counts.items())
        peek = [
            f"[{i}]《{r['title'] or '（没标题）'}》（{feed.source_name(r['source'])}）"
            for i, r in enumerate(unread_rows[:config.INBOX_PEEK])
        ]
        user_parts.append(
            f"【世界】inbox 里有 {len(unread_rows)} 条没看过的（{stock}）。排在前面的几条：\n"
            + "\n".join(peek)
        )
        # 只给标题不给全文——「读什么」必须是她的选择，不是替她选好
    else:
        # 不写「没有新鲜事」——web_search 上线后那句话就不成立了：
        # inbox 是空的只说明推送没了，不说明世界没东西了。
        user_parts.append("【世界】inbox 是空的。")

    user_parts.append("\n决定吧。用 JSON 回复。")

    return [
        {"role": "system", "content": PERSONA + "\n\n" + CONTRACT},
        {"role": "user", "content": "\n".join(user_parts)},
    ], shown_ids


# ── 第二段：反应（见 REACTION_CONTRACT 上方注释）──────────────────────────

def build_reaction_messages(base_messages: list[dict], decision_raw: str,
                            tool: str, summary: str, detail: str) -> list[dict]:
    """把「她刚做了什么 + 看到了什么」拼成第二段调用的上下文。

    base_messages 是原封不动的第一段上下文：不重发会导致她失忆，
    重发会贵一倍——但第二段只需要理解结果，不需要重新决定，所以
    沿用原 context 是划算的（而且能省掉重新解释「你是谁」的钱）。
    """
    text = (detail or "").strip()
    clipped = text[:config.REACTION_TEXT_CHARS]
    tail = ""
    if len(text) > config.REACTION_TEXT_CHARS:
        # 诚实标注比假装读完重要：她知道自己是「只看到前 2000 字」，
        # 就不会编出「这篇文章后面讲了三个观点」这种幻觉。
        tail = f"\n\n（原文更长，你只看到前 {config.REACTION_TEXT_CHARS} 字，后面还有。）"

    body = (
        f"【你刚才做的】{tool}\n"
        f"【发生的事】{summary}\n\n"
        f"【你看到的内容】\n{clipped}{tail}\n\n"
        f"这件事在你身上留下了什么？用 JSON 回一句。"
    )
    return [
        # 换掉 system 里的契约（第一段那份是"做决定"的格式），
        # 但保留 user 里的世界上下文——她得知道现在几点、自己是谁。
        {"role": "system", "content": PERSONA + "\n\n" + REACTION_CONTRACT},
        *base_messages[1:],
        {"role": "assistant", "content": decision_raw},
        {"role": "user", "content": body},
    ]


# ── 自省专用（Design_v0.2 §5.3）────────────────────────────────────────

REVIEW_CONTRACT = """现在要做一次自省：重写你的「你是谁」。
规则：
- 旧版说过的事要留着，可以改写措辞、可以往后追加，但不能删掉或推翻，你还是你
- 输出 JSON：{"new_core": "重写后的自我叙事", "reason": "为什么这么改"}"""


def build_review_messages(old_core: str) -> list[dict]:
    return [
        {"role": "system", "content": PERSONA + "\n\n" + REVIEW_CONTRACT},
        {"role": "user", "content": f"【你现在的自我叙事】\n{old_core}\n\n重写它。用 JSON 回复。"},
    ]
