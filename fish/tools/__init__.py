"""工具层：鱼鱼能「做」的事。

ToolResult.summary 与 detail 必须分开（Design_v0.2 §8.1）：
detail 可能是三千字的文章全文，summary 是她「读完之后记住的那点东西」，
控制在百字内。这条纪律是成本的真正护栏——比任何调度优化都重要。
"""

import json
import sqlite3
from dataclasses import dataclass, field

from .. import config, state as state_mod, util


@dataclass
class ToolResult:
    ok: bool
    summary: str                 # 给模型看的 1-3 句，永远不超过百字
    detail: str = ""             # 完整内容，只落库，绝不进 prompt
    state_hint: dict = field(default_factory=dict)  # 备用；状态规则统一走 state.apply_tool_effect


@dataclass
class Tool:
    name: str
    desc: str                    # 给模型看的一句话：能干什么、值不值得干
    cost: int                    # 耗能档位 1-3；cost≥3 且 energy<25 时不可见
    run: object                  # (conn, state, args, ctx) -> ToolResult

    @property
    def args_hint(self) -> str:
        """把 run 函数的类型注解翻成给模型看的参数说明。"""
        hints = {
        "read_feed": '{"index": 0}   // 读第几条，编号对应【世界】里的 [n]',
        "write_journal": '{"text": "想留下的那段话"}',
        "recall": '{"query": "想找的关键词"}',
        "web_search": '{"query": "想搜的词或问题"}',
        # 红线：下面这条参数说明会被拼进给鱼鱼的提示词，措辞待小帕定稿。
        "send_message": '{"text": "想对小帕说的话"}',
        }
        return hints.get(self.name, "{}")


# ── 三个第一版工具 ───────────────────────────────────────────────────────

def _run_read_feed(conn: sqlite3.Connection, s, args: dict,
                   ctx: dict | None = None) -> ToolResult:
    from .. import feed
    index = max(0, int(args.get("index", 0)))
    # 顺序不在 SQL 里排，而是用 feed.unread_reading_order：各源轮转。
    # 纯按 id 排时，一次抓取的 10 条同源条目会霸占前 10 个位置，
    # 她「翻到的」永远只有一个源。轮转保证相邻编号来自不同源，
    # 而且【世界】里的 [n] 编号和这里的 index 是同一个顺序。
    unread = feed.unread_reading_order(conn)
    if not unread:
        return ToolResult(ok=False, summary="没有没看过的东西，feed 是空的。")

    row = unread[min(index, len(unread) - 1)]
    title = row["title"] or "一条没标题的东西"
    body = util.plain(row["body"] or "")

    # 有些源（如少数派）的 RSS 只给开头一段——inbox 里存的是壳。
    # 2026-11-10 12:56 她连读三条壳，得出「这套 inbox 给我的多是壳」，
    # 开始不想再用这个工具。补法：在她真去读的这一刻才用 Fetch 抓全文，
    # 只为读过的条目花钱；抓回来就地入库，下次也不用再抓。
    teaser = bool(row["link"]) and len(body) < config.INBOX_FETCH_BELOW
    if teaser and not config.DRY_RUN:
        from .. import websearch
        try:
            page = websearch.fetch_page(row["link"])
            # Fetch 返回的是干净的 Markdown，保留换行直接用——
            # 过 util.plain 会把段落全压成一行，读起来就是一堵墙
            full = page["content"][:6000]
            if len(full) > len(body):
                conn.execute("UPDATE inbox SET body = ? WHERE id = ?",
                             (full, row["id"]))
                conn.commit()
                body, teaser = full, False
        except websearch.WebSearchError:
            pass    # 抓不到：维持开头那段，下面会向她说清楚

    conn.execute("UPDATE inbox SET consumed = 1 WHERE id = ?", (row["id"],))
    conn.commit()

    if not body:
        # 纯图条目（知乎热榜常见：正文就一张图）剥完是空串。
        # 不能拿标题静默冒充内容——2026-11-10 01:23 她就这样「读」了
        # 《T1 带不动他了》，detail 只有标题，她如实反应没东西然后长睡。
        # 输入是垃圾不是她的错，但要说清楚是垃圾。
        body = f"这条点开没有正文，大概是个图片帖，能看的只有标题《{title}》。"
        summary = f"读了《{title}》，里面没有正文。"
        if teaser:
            body += "（带着链接去抓原文页，也没抓到东西。）"
    else:
        # summary 是「一句话交代读了什么」，真正的正文走 detail 进第二段调用。
        summary = f"读了《{title}》"
        if len(body) > 60:
            summary += f"：{body[:60]}……"
        if teaser:
            # 抓全文失败了：明说这是 RSS 的锅，别让她把「只看到开头」
            # 归因成「内容本来就是壳」——那正是 12:56 发生的误读。
            body += ("\n\n（这条的原文没能抓回来，RSS 只给了上面这一段开头。"
                     "内容比这长，是渠道的问题，不是你漏看了。）")

    return ToolResult(ok=True, summary=summary, detail=body)


def _run_write_journal(conn: sqlite3.Connection, s, args: dict,
                       ctx: dict | None = None) -> ToolResult:
    text = (args.get("text") or "").strip()
    if not text:
        return ToolResult(ok=False, summary="想写点什么，但没写出来。")
    # pinned=1：日记是她主动想留下的，检索时加权（见 memory.search）
    from .. import memory
    memory.add_episodic(conn, f"[日记] {text}", salience=0.8, pinned=1)
    return ToolResult(ok=True, summary="写了日记。", detail=text)


def _head(text: str, n: int) -> str:
    """取一段文字的开头做「指认」，不加省略号以外的加工。

    去重时只说「哪几条重复了」，不能把全文再搬一遍——那正是要避免的。
    """
    text = (text or "").replace("\n", " ").strip()
    return text[:n] + ("…" if len(text) > n else "")


def _run_recall(conn: sqlite3.Connection, s, args: dict, ctx: dict | None = None) -> ToolResult:
    from .. import memory
    query = (args.get("query") or "").strip()
    if not query:
        return ToolResult(ok=False, summary="想回忆点什么，但没想清楚要找什么。")

    # 多捞几条再筛：如果前几条全是「这轮已经看过」的，还得知道后面有没有新的
    hits = memory.search(conn, limit=config.RECALL_SCAN, query=query)
    if not hits:
        # search 带相关性闸门：一条都沾不上时返回空——不是「记忆里没有」，
        # 是「按这个说法没翻到」。措辞要说准，别让她以为自己忘光了。
        return ToolResult(ok=False, summary=f"想了想「{query}」，没能翻到跟这个对得上的记忆。")

    seen = set((ctx or {}).get("seen_ids") or ())
    for r in hits:                      # 检索本身照常记账：repeat 是执念的原料
        memory.bump_recall(conn, [r["id"]])

    fresh = [r for r in hits if r["id"] not in seen][:config.RECALL_SHOW]
    dup = [r for r in hits if r["id"] in seen]
    # 重复项只做「指认」：最多点名前 3 条，其余报数量。全列出来会变成
    # 另一种噪音——她只需要知道「这些我见过了」和「还有没有新的」。
    named = dup[:3]
    dup_line = "；".join(f"{r['ts'][:16]}「{_head(r['content'], 16)}」" for r in named)
    if len(dup) > len(named):
        dup_line += f" 等 {len(dup)} 条"

    if not fresh:
        # 全是这轮已经在眼前的内容。不再原文放送——重复读同一段不会产生
        # 新信息，只会让她把「系统又给我看了一遍」误读成「我只记得这么点」。
        # 2026-11-10 09:54 她的反思就是这么来的：「回来的全是我自己写过
        # 的三段，没有一条新的——原来这条线在记忆里只有这么点东西」。
        return ToolResult(
            ok=True,
            summary=f"想了想「{query}」，翻到的 {len(dup)} 条都是你这轮已经看到过的，没有新的。",
            detail=(f"下面 {len(dup)} 条你这轮已经看到了，不重复搬过来：\n{dup_line}\n\n"
                    f"关于「{query}」，记忆里没有更多其他内容了。"),
        )

    # summary 不截断：这是给模型的内容预告，detail 才是全文。
    # 截断会让「想起来」这个动作本身显得含糊——想起来就想起来全部。
    lines = [f"{r['ts'][:16]} {r['content']}" for r in fresh]
    summary = "想起来一些：" + "；".join(lines)
    if dup:
        summary += f"（另有 {len(dup)} 条你这轮已经看过，不重复了）"
    detail = "\n".join(r["content"] for r in fresh)
    if dup:
        detail += f"\n\n（另有 {len(dup)} 条你这轮已经看过，没有重复列出：{dup_line}）"
    return ToolResult(ok=True, summary=summary, detail=detail)


def _run_web_search(conn: sqlite3.Connection, s, args: dict,
                    ctx: dict | None = None) -> ToolResult:
    from .. import websearch
    query = (args.get("query") or "").strip()
    if not query:
        return ToolResult(ok=False, summary="想搜点什么，但没给出搜索词。")
    if len(query) > config.WEB_SEARCH_MAX_QUERY:
        return ToolResult(ok=False,
                          summary=f"搜索词超过 {config.WEB_SEARCH_MAX_QUERY} 字，太长了，没搜。")

    if config.DRY_RUN:
        # 空跑模式不花钱：给一段假正文，让两段式链路（决策→反应）照样能演练
        return ToolResult(ok=True, summary=f"（空跑）搜了「{query}」，拿到 {config.WEB_SEARCH_COUNT} 条模拟结果。",
                          detail="（空跑模式：这里本该是几条搜索结果的摘要。）")

    try:
        results = websearch.search_web(query, count=config.WEB_SEARCH_COUNT)
    except websearch.WebSearchError as e:
        # 失败的原因原样告诉她：是没搜到、被限速还是余额不足，她有权知道
        return ToolResult(ok=False, summary=f"搜「{query}」没搜成：{e}")

    if not results:
        return ToolResult(ok=False, summary=f"搜了「{query}」，外面一条相关的都没有。")

    # 3 条官方摘要每条约 600-800 字，加标题和链接可能超出第二段的阅读
    # 预算（REACTION_TEXT_CHARS）。2026-11-10 实测：真实结果的 URL 有的
    # 长达 140 字，写死的余量根本兜不住。改成先量「每条的固定开销」
    # （标题行 + 链接行 + 截断标注 + 块间空行），再把剩余预算平分给
    # 摘要——保证拼出来的 detail 整体不超预算。否则第三条会被全局上限
    # 拦腰砍断，而她甚至不知道自己漏看了第三条。
    heads = []
    for r in results:
        title_line = f"《{r['title']}》"
        if r["site"]:
            title_line += f"（{r['site']}）"
        if r["date"]:
            title_line += f" {r['date']}"
        block = title_line if not r["url"] else title_line + "\n" + r["url"]
        heads.append(block)

    n = len(results)
    # 22 = 一条截断标注的估计长度；10 = 应对数字位数变化的安全垫
    overhead = sum(len(h) + 1 + 22 for h in heads) + 2 * (n - 1) + 10
    per = max(150, (config.REACTION_TEXT_CHARS - overhead) // n)

    blocks = []
    for head, r in zip(heads, results):
        text, cut = r["summary"], ""
        if len(text) > per:
            text, cut = text[:per], f"\n（这条更长，你只看到前 {per} 字）"
        blocks.append(head + "\n" + text + cut)
    detail = "\n\n".join(blocks)

    # summary 遵守「百字内」纪律：标题只点名不展开，正文在 detail 里
    summary = (f"上网搜了「{query}」，拿到 {len(results)} 条："
               + "；".join(f"《{_head(r['title'], 18)}》" for r in results))
    return ToolResult(ok=True, summary=summary, detail=detail)


def _run_send_message(conn: sqlite3.Connection, s, args: dict,
                      ctx: dict | None = None) -> ToolResult:
    """给小帕发一句话（鱼鱼 → 小帕）。

    单段工具：和 write_journal 一样，写完即止，不进第二段反应调用——
    否则会给「我给小帕发了句话」再生成一段「回应自己的话」，既冗余又
    会把同一件事算两次情绪。所以这里自己落一条记忆、自己写一条消息，
    不再走 react。记忆让她记得「自己主动联系过 小帕」；messages 行让
    小帕的收件脚本能取到。两者是「她的回忆」与「投递通道」的区别，
    不是重复记忆（撞墙的「一次写两条记忆」指的是同一事件两次 add_episodic，
    这里一次写记忆、一次写消息表，性质不同）。
    """
    text = (args.get("text") or "").strip()
    if not text:
        return ToolResult(ok=False, summary="想说点什么，但没说出来。")
    from .. import memory, messages as msg_mod
    # 记忆落点：她主动联系 小帕，记一条情景记忆（不 pinned，普通权重）。
    memory.add_episodic(conn, f"[给小帕] {text}", salience=0.7,
                        tags=config.XIAOPA_MSG_TAG)
    # 投递通道：写入 messages 表，小帕的收件脚本从 from_yuyu 取。
    msg_mod.post(conn, "from_yuyu", text)
    return ToolResult(ok=True, summary="给小帕留了句话。", detail=text)


ALL_TOOLS = [
    Tool("read_feed", "读一条还没读过的东西", 1, _run_read_feed),
    Tool("write_journal", "写一段日记", 1, _run_write_journal),
    Tool("recall", "检索自己的记忆或日记", 1, _run_recall),
    Tool("web_search", "联网搜索", 1, _run_web_search),
    Tool("send_message", "给小帕发一条消息", 1, _run_send_message),
]

_TOOL_MAP = {t.name: t for t in ALL_TOOLS}


# ── 给大脑用的视图 ───────────────────────────────────────────────────────

def visible_tools(s: state_mod.State) -> list[Tool]:
    """硬约束的唯一形式：不可见 = 干脆不给（Design_v0.2 §8.3）。

    刻意不做「成功率下降」那类软惩罚——随机失败对模型是白噪声，
    它会自己编原因，把第三层观察搅浑。
    """
    out = []
    for t in ALL_TOOLS:
        if t.cost >= 3 and s.energy < 25:
            continue
        out.append(t)
    return out


def describe(tools: list[Tool], s: state_mod.State) -> str:
    """工具清单文本，进 prompt。被隐藏的工具要说明为什么，让她知道
    「有些事我现在干不了」，而不是以为世界只有这几件事。"""
    lines = []
    for t in tools:
        lines.append(f"- {t.name}: {t.desc} 参数形如 {t.args_hint}")
    hidden = [t for t in ALL_TOOLS if t not in tools]
    for t in hidden:
        lines.append(f"- （{t.name} 现在用不了：太耗神，你现在的状态撑不住）")
    return "\n".join(lines)


def dispatch(name: str, args: dict, conn: sqlite3.Connection,
             s: state_mod.State, seen_ids: set[int] | None = None) -> ToolResult:
    """seen_ids：这一轮已经摆在她眼前的记忆 id，供 recall 去重。"""
    tool = _TOOL_MAP.get(name)
    if tool is None:
        return ToolResult(ok=False, summary=f"没有叫 {name} 的工具。")
    ctx = {"seen_ids": set(seen_ids or ())}
    try:
        return tool.run(conn, s, args or {}, ctx)
    except Exception as e:                       # 工具崩了 = 事情失败，不崩进程
        return ToolResult(ok=False, summary=f"{name} 出了岔子：{type(e).__name__}。")
