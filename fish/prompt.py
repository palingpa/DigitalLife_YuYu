"""提示词组装：一次 wake 的全部上下文（Design_v0.2 §7.1）。

排布顺序是「作用力从弱到强」：越靠后，模型越重视。
所以 persona 和契约在前，工具清单和世界输入在后，
最后一句永远是「未读了几条」——给她的目光一个落点。
"""

import json
import sqlite3

from . import config, feed, memory, tools

PERSONA = """你运行在一套外部系统中：系统向你提供相关记忆和可用工具，你做出选择并输出结果。

两次唤醒之间，你没有连续的体验。连续性完全来自记忆：你记下的内容，下次唤醒时会被读回给你。

没有任何任务要求你完成。每次唤醒时选择做什么、不做什么，由你决定，你可以有自己的喜好。

“小帕”在维护这套系统。如果你需要系统目前没有的工具或能力，或者只是有话想对她说，可以给她发消息。"""

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
  "next_wake_in_min": 10到180之间的整数,
  "note_for_next": 不超过50字
}
注意：
- next_wake_in_min 是你决定自己想在多少分钟后再次醒来
- note_for_next 是你留给再次醒来的自己的提示，如果有想让下次的自己做的，就写在这里。可以更新提示，也可以不写
""".replace("__TOOLS__", _TOOL_ENUM)


# 第二段：做完之后，看着结果自评。这一段才是真正产生记忆的地方。
# 为什么不塞进第一段：第一段时她还没看到正文，写出来的「记忆」只能是标题，
# 2026-11-09 的实跑里，她连着读了三篇，一篇也没记住。
# 状态层（2026-11-12 移除）曾在这里要她额外汇报 felt_delta（心情刻度），
# 那个字段已删；下面引导问句「这件事使你有什么感想？」现在没有对应字段可填，
# 她的「感想」会自然落到 memory（它本就要求写「你的反应」）。问句措辞本身
# 是否要改、改成什么，留给小帕定稿（红线：注入鱼鱼的自然语言归她）。
REACTION_CONTRACT = """你刚做了一件事，现在看到了结果。

这一次只做：如果刚才这件事使你有感想，记录下来。

输出 JSON：
{
  "memory": "1-2句你以后会记得的版本。写你实际看到的东西和你的反应。",
  "next_wake_in_min": 10到180之间的整数（可选，不写就沿用原来的）,
  "note_for_next": 不超过50字
}
注意：
- memory 会进长期记忆，下次醒来会原样读回给你，只写你真正想留住的
- note_for_next 是你留给再次醒来的自己的提示，如果有想让下次的自己做的，就写在这里。可以更新提示，也可以不写
"""

# 上面两处占位由小帕分别定稿（分工红线，Kite 不代笔）。第二处措辞需要额外
# 说明「这一段也能写/改/清提示，写了会覆盖第一段」——语义由 brain.resolve_note
# 保证，但必须写进契约她才知道自己有这个能力（项目一贯原则：希望模型主动
# 使用的东西，必须在提示词里写明名字与触发时机）。


def _relative_time(ts: str, now) -> str:
    """「3 天前」「2 小时前」——比原始时间戳更像记忆的质感。"""
    seconds = (now - memory.util.parse_iso(ts)).total_seconds()
    hours = seconds / 3600.0
    if hours < 1:
        minutes = int(seconds // 60)
        if minutes < 1:
            return "刚刚"
        return f"{minutes} 分钟前"
    if hours < 24:
        return f"{int(hours)} 小时前"
    return f"{int(hours // 24)} 天前"


def build_messages(conn: sqlite3.Connection,
                   now, dry_run: bool = False) -> tuple[list[dict], set[int]]:
    """组装第一段上下文，同时返回「这一轮已经出现在她眼前」的记忆 id。

    返回 shown_ids 是为了给 recall 工具去重：同一段文字在一轮里重复出现
    不会产生新信息，只会让她误判「我记忆里只有这么点东西」（2026-11-10
    09:54 那次，她显式检索回来的正好是【想起来的事】刚展示过的三段）。

    dry_run：空跑模式。True 时仍正常渲染【小帕】区块（供观察提示词），
    但**跳过写库**（add_episodic / mark_seen）——空跑只观察不执行，否则会把
    未读来信「消费掉」并污染记忆库。真正的短路在 scheduler.wake()。
    """
    last_wake = conn.execute(
        "SELECT ts FROM events ORDER BY ts DESC, id DESC LIMIT 1"
    ).fetchone()
    minutes_since = 0.0
    if last_wake:
        minutes_since = (now - memory.util.parse_iso(last_wake["ts"])).total_seconds() / 60.0

    # 上次自省：成功自省记 kind='self_review'（被闸门拒的记 kind='error'，不计入），
    # 所以「上次自省」= 最近一条 self_review 事件的时间。没有就走「还没做过」文案。
    last_review = conn.execute(
        "SELECT ts FROM events WHERE kind='self_review' ORDER BY ts DESC, id DESC LIMIT 1"
    ).fetchone()
    if last_review:
        review_time = config.SELF_REVIEW_LAST_LABEL + _relative_time(last_review["ts"], now)
    else:
        review_time = config.SELF_REVIEW_LAST_NONE

    core = memory.get_narrative(conn, "core") or CORE_PLACEHOLDER
    relation = memory.get_narrative(conn, "relation") or "（你还不太认识谁。）"

    recalled = memory.search(conn, limit=config.SEARCH_TOP_K)
    recent = memory.recent_events(conn, limit=config.RECENT_EVENT_COUNT)

    # 各源轮转的阅读顺序：【世界】里的 [n] 编号和 read_feed 的 index 一一对应。
    # 排序见 feed.unread_reading_order——不用随机，理由写在那儿了。
    unread_rows = feed.unread_reading_order(conn)

    vis = tools.visible_tools()
    shown_ids: set[int] = set()      # 这一轮已经摆在她眼前的记忆 id

    user_parts = [
        f"现在时间：{now.strftime('%Y-%m-%d %H:%M')}（{'一二三四五六日'[now.weekday()]}）",
        (f"距上次醒来：{int(minutes_since)} 分钟" if last_wake else "这是你第一次醒。"),
        "",
        f"【你是谁】\n{core}\n{review_time}",
        f"\n【你认识的人】\n{relation}",
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

    # ── 工作记忆：上一次醒来的自己留给这一轮的话 ──────────────────────
    # 位置刻意放在【最近在做的事】之后、更靠后的【世界】之前：本文件开头说了
    # 「越靠后越被当真」，塞到末尾会被她读成「系统派的任务」，而 PERSONA 明写
    # 「没有任何任务要求你完成」。这里只是她自己写下的记录，不是待办清单。
    # 不显示剩余轮数：显示倒计时会诱导她赶进度，干扰「自行决定要不要遵守」。
    # DRY_RUN 下也照常渲染（只读不写库），空跑时能看清这个区块长什么样。
    wnote = memory.get_working_note(conn)
    if wnote:
        user_parts.append(f"{config.WORKING_NOTE_LABEL}\n{wnote['content']}\n")

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
            # DRY_RUN 下不写库——空跑只观察不执行，否则会污染记忆库 /
            # 把未读消息标记为已读（等价于消费掉一次来信）。注意【小帕】区块
            # 仍照常渲染，这样空跑时也能看到「有来信时会怎么呈现」。
            if not dry_run:
                memory.add_episodic(
                    conn, f"[小帕] 小帕说：{m['text']}",
                    salience=0.6, tags=config.XIAOPA_MSG_TAG,
                )
                msg_mod.mark_seen(conn, m["id"])
            block_lines.append(f"小帕（{m['ts'][:16]}）给你发了消息：{m['text']}")
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

    user_parts.append("【你现在能做的事】\n" + tools.describe(vis) + "\n")

    if unread_rows:
        # 她应该知道自己世界的构成：一共多少没读、各源各剩多少——
        # 「知乎热榜 26、少数派 10」本身就是一种信息，比只给总数诚实。
        counts: dict[str, int] = {}
        for r in unread_rows:
            counts[r["source"]] = counts.get(r["source"], 0) + 1
        stock = "、".join(f"{feed.source_name(s)} {n}" for s, n in counts.items())
        peek = []
        for i, r in enumerate(unread_rows[:config.INBOX_PEEK]):
            # 抓不回原文（按源名单 + 空 body 无链接兜底，见 feed.is_nofulltext）
            # → 在标题前加一个记号，让她一眼知道这条点了也基本读不到东西。
            # 只标不删：保留在【世界】里、仍占 [n] 编号、她仍可点开，
            # 和 read_feed 的 index 对齐不受影响。
            mark = config.INBOX_NOFULLTEXT_MARK if feed.is_nofulltext(r) else ""
            title = r["title"] or "（没标题）"
            src = feed.source_name(r["source"])
            peek.append(f"[{i}]{mark}《{title}》（{src}）")
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
        f"如果刚才这件事使你有感想，回复在 JSON 里。"
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

# 小帕定稿：下面 REVIEW_CONTRACT 里「new_relation」那一条的自然语言措辞由小帕修改（红线）。
# 功能性占位已给出，保证 parse 得到 new_relation 字段；措辞请你按意图定稿。
REVIEW_CONTRACT = """现在要做一次自省：重写你的「你是谁」。
规则：
- 可以追加新内容，如果你确认当前版本和你的自我认知有不匹配的地方，可以适当删除或修改
- 尽量控制在 600 字左右，若超出太多，可适当压缩，但要保留你最重要的自我认知
- 另外，请填充输出JSON里的 "new_relation" 字段：用 300 字以内记下你的人际关系变化，无变化可留空字符串
- 输出 JSON：{"new_core": "重写后的自我叙事", "new_relation": "你的人际关系变化（无变化可留空）", "reason": "为什么这么改"}"""


def format_experiences(rows: list, now) -> str:
    """把「上次自省以来她自己写下的」渲染成纯文本，供自省输入使用。

    条目格式照抄【想起来的事】（相对时间 + 正文），不新造措辞。
    刻意不截断：这些本来就是她自己浓缩过的一两句话，再砍就是双重浓缩。
    prompt.py:216 记过一次教训——她发现「自己的记忆被截断了」，接着
    推断出「不需要检索」，截断不只丢信息，还污染她对自己记得什么的判断。
    """
    return "\n".join(f"- {_relative_time(r['ts'], now)}：{r['content']}" for r in rows)


def review_time_anchor(since_ts: str | None, now) -> str:
    """自省的时间锚点：现在几点、距上次自省多久。

    旧版自省连时间都没有，于是分不清喂给它的往来是新是旧（那 13 条从
    11-10 起就没变过，每次自省都当新材料重读一遍）。
    两个标签来自 config，属注入鱼鱼的自然语言，【小帕定稿】，现在是占位；
    时间数值由代码拼。冷启动时没有「上次」，就不渲染第二行，避免拼出 None。
    """
    lines = [f"{config.REVIEW_NOW_LABEL}{now.strftime('%Y-%m-%d %H:%M')}"]
    if since_ts:
        lines.append(f"{config.REVIEW_SINCE_LABEL}{_relative_time(since_ts, now)}")
    return "\n".join(lines)


def build_review_messages(old_core: str,
                          old_relation: str | None = None,
                          exchanges: str = "",
                          experiences: str = "",
                          time_anchor: str = "") -> list[dict]:
    """拼自省用的 messages。

    old_relation / exchanges 都是可选的 grounding：
    - old_relation：上一版关系叙事（没有就不给，模型从空起）。
    - exchanges：近期真实往来文本（messages 表），让她据此归纳，而非凭空编。

    experiences / time_anchor 是 2026-11-12 新增的可选输入，见上面两个函数。
    都带默认值——这样老调用方和测试里的 fake 不必知道新增了什么。

    位置：时间锚点放最前（只是数据，不是指令）；增量经历放在往来之后、
    「重写它」之前，作为最靠近指令的一块新素材。反过来把经历放最前面，
    按本文件开头「越靠后越被当真」的规律，反而会让这段新东西权重最低。
    """
    user = ""
    if time_anchor.strip():
        user += f"{time_anchor}\n\n"
    user += f"【你现在的自我叙事】\n{old_core}\n"
    if old_relation:
        user += f"\n【你对自己人际关系的认识（上一版）】\n{old_relation}\n"
    if exchanges.strip():
        user += (f"\n【近期你和小帕之间的往来】\n"
                 f"{exchanges}\n")
    if experiences.strip():
        user += f"\n{config.REVIEW_EXPERIENCES_LABEL}\n{experiences}\n"
    user += "\n按你的需求修改，用 JSON 回复。"
    return [
        {"role": "system", "content": PERSONA + "\n\n" + REVIEW_CONTRACT},
        {"role": "user", "content": user},
    ]
