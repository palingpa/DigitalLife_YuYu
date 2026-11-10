"""调度层：tick 时钟、唤醒判断、单飞锁。整个项目的「作息表」。

先分清两个词（Design_v0.2 §6）：
    tick  = 时钟走一格，纯本地判断，不调 API
    wake  = 真正烧钱的一次唤醒循环

这里有两处对初学者最「精髓」的地方：
1. asyncio.Lock 没有非阻塞的 acquire()。想要「正在跑就跳过本次」，
   正确姿势是先看 .locked() 再 await acquire()，而不是加参数。
2. 所有 await 的地方都是「可能等很久」的网络调用；数据库读写
   故意保持同步——毫秒级操作不值得为它引入异步复杂度。
"""

import asyncio
import difflib
import json
import re
import sqlite3

from . import brain, config, db, memory, prompt, state as state_mod, tools, trace, util

# 单飞锁：同一时刻全宇宙只允许一个 wake 在跑（不变量 I3）
wake_lock = asyncio.Lock()


# ── 到期判断 ─────────────────────────────────────────────────────────────

def _next_due(conn: sqlite3.Connection, now) -> "object":
    """下次该醒的时间。

    只看「带闹钟的行」（wake/heartbeat，它们写入时都填了 next_wake_at），
    跳过 self_review / error 这类旁支事件。2026-11-10 夜里的教训：自省
    事件比唤醒晚 5 秒落库、next_wake_at 为 NULL，latest_event 取到它之后
    走了 ts+HARD_MAX_GAP 的兜底，把她自己定的 75 分钟变成了 240 分钟。
    旁支事件没有闹钟，不该参与「几点该醒」的计算。
    """
    row = conn.execute(
        "SELECT ts, next_wake_at FROM events WHERE next_wake_at IS NOT NULL "
        "ORDER BY ts DESC, id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return now                      # 从未醒过 → 立即醒
    try:
        return util.parse_iso(row["next_wake_at"])
    except (ValueError, TypeError):
        return util.parse_iso(row["ts"]) + _minutes(config.HARD_MAX_GAP)

def _minutes(n: int):
    from datetime import timedelta
    return timedelta(minutes=n)


def _should_wake(conn: sqlite3.Connection, s: state_mod.State, now) -> tuple[bool, str]:
    """三条唤醒路径（Design_v0.2 §6.1 + 2026-11-10 通信模块）：
    外部消息 / 到期 / 憋不住。

    外部消息优先级最高：小帕发来的、鱼鱼还没看过的消息，应当尽快让她知道。
    但受 EXTERNAL_WAKE_MIN_GAP 约束——小帕连发时不能把唤醒节奏打得太碎。
    消息在「被读」之前一直留库，间隔一到下一 tick 自然触发，不会丢。
    """
    latest = db.latest_event(conn)
    since_wake = util.hours_between(now, util.parse_iso(latest["ts"])) * 60 if latest else 1e9

    from . import messages as msg_mod
    if msg_mod.has_unseen_to_yuyu(conn) and since_wake >= config.EXTERNAL_WAKE_MIN_GAP:
        return True, "xiaopa"

    if now >= _next_due(conn, now):
        return True, "due"
    if s.boredom > config.BOREDOM_URGENT and since_wake > config.URGENT_COOLDOWN_MIN:
        return True, "boredom_urgent"
    return False, ""


# ── 唤醒循环 ─────────────────────────────────────────────────────────────

def _repeat_streak(conn: sqlite3.Connection, tool: str) -> int:
    """加上这一次，连续第几次做同一件事（给边际递减用）。"""
    rows = conn.execute(
        "SELECT tool FROM events WHERE kind = 'event' ORDER BY ts DESC, id DESC LIMIT 10"
    ).fetchall()
    streak = 1
    for r in rows:
        if r["tool"] == tool:
            streak += 1
        else:
            break
    return streak


def _event_salience(valence_before: float, valence_after: float, tool: str) -> float:
    """事件「重要度」：情绪冲击 + 主动书写加权。写入时定死，之后不再改。"""
    sal = 0.5 + abs(valence_after - valence_before) / 20.0
    if tool == "write_journal":
        sal += 0.3
    return max(0.0, min(1.0, sal))


async def wake(conn: sqlite3.Connection, client, trigger: str) -> None:
    """一次完整的唤醒循环。§11 第 4-7 步全部串在这里。"""
    t0 = util.now()
    tid = trace.new_id()          # 本次唤醒的编号：events 行和轨迹行靠它对上
    s = state_mod.load(conn)

    # 睡眠恢复：上次唤醒到现在，间隔够长就算「睡了一觉」，energy 回血。
    # 必须在 build_messages 之前结算——她睁眼看到的【身体】要先包含这一觉。
    last = db.latest_event(conn)
    if last is not None:
        sleep_min = (t0 - util.parse_iso(last["ts"])).total_seconds() / 60.0
        bonus = state_mod.apply_sleep_recovery(s, sleep_min)
        if bonus > 0:   # 没睡够时长就不记，避免轨迹被打盹刷屏
            trace.record("sleep", tid, minutes=round(sleep_min), bonus=round(bonus, 2))

    before = (s.energy, s.valence, s.boredom)

    messages, shown_ids = prompt.build_messages(conn, s, t0, dry_run=config.DRY_RUN)
    d = await asyncio.to_thread(brain.decide, client, messages, tid)
    # to_thread：把同步的 API 调用丢进工作线程，避免卡住事件循环。
    # 这是「同步库 + asyncio 共存」的标准手法，值得记住。

    # ── DRY_RUN 短路：只观察、不执行 ────────────────────────────────────
    # 空跑语义是「不调 API、只打印上下文、记假决策」，绝不能真的 dispatch 工具、
    # 真的给小帕发消息 / 写日记、或提交状态库。上面 brain.decide 在 DRY_RUN 下
    # 走 _dry_run_decision：已把完整提示词写入 logs/last_prompt.txt、返回假决策
    # （不花 token）；build_messages 也已按 dry_run 跳过写库。到这里必须直接返回，
    # 否则会出现「假消息真发出去」——2026-11-11 因此产生了跨进程的孤儿轨迹。
    if config.DRY_RUN:
        trace.record("decision", tid, action=d.action, tool=d.tool, why=d.why,
                     felt_delta=d.felt_delta, next_wake_in_min=d.next_wake_in_min,
                     dry_run=True)
        print(f"[DRY_RUN] 已生成假决策：action={d.action} tool={d.tool} "
              f"why={d.why}（完整提示词见 logs/last_prompt.txt）trace_id={tid}")
        return

    result = None
    reaction = None
    memory_text = ""

    if d.action == "tool" and d.tool:
        result = tools.dispatch(d.tool, d.args, conn, s, seen_ids=shown_ids,
                                client=client, trace_id=tid)
        ok = result.ok

        # 工具返回的完整内容也进轨迹：summary 给模型看，detail 只在这里留底。
        # 想看「她到底读到了什么」，就查这一层。
        trace.record("tool_result", tid, tool=d.tool, ok=ok,
                     summary=result.summary, detail=result.detail)

        # ── 两段式的分叉点 ────────────────────────────────────────────
        # 判据是「有没有产生可消化的内容」，不是「她选了哪个工具」。
        # 通常加新工具不用改这里：只要它返回 detail 就会自动进第二段。
        # 唯一的例外是 write_journal：日记本身就是反思，不需要再把刚写的
        # 原文念一遍让她「回应自己的日记」（那等于对反思再做一次反思）。
        # send_message 同理：给小帕的一句话写完即止，不进第二段，否则会给
        # 「我给小帕发了句话」再生成一段「回应自己的话」，既冗余又重复算情绪。
        # 空转和失败没有 detail，也就不多花那一次调用。
        if ok and (result.detail or "").strip() and d.tool not in (
                "write_journal", "send_message", "self_review"):
            reaction = await asyncio.to_thread(
                brain.react, client, messages, d, d.tool,
                result.summary, result.detail, tid,
            )
            # 情绪以后果为准：第一段那个 felt_delta 是「猜」，
            # 这一段才是「看完之后的感觉」。两者冲突时听后者。
            state_mod.apply_felt_delta(s, reaction.felt_delta)
            memory_text = reaction.memory if reaction.parse_ok else ""
            if reaction.next_wake_in_min:
                d.next_wake_in_min = reaction.next_wake_in_min   # 读完可以改主意
        else:
            # 走这里有两种情况：
            #  - 空转 / 失败 / 工具没返回可消化内容（如 inbox 是空的）——
            #    挫败感交给规则侧（apply_tool_effect 扣 valence），不额外调用。
            #  - 刻意单段的工具（write_journal）：日记写完即止，不回念。
            # 这些情况下「看完之后的感觉」不存在，就用第一段给的 felt_delta。
            state_mod.apply_felt_delta(s, d.felt_delta)

        state_mod.apply_tool_effect(s, d.tool, ok, _repeat_streak(conn, d.tool))
    else:
        # 空转：没有后果，只有「此刻醒来时的感觉」，用第一段给的值
        state_mod.apply_felt_delta(s, d.felt_delta)
        ok = False   # 空转不算成功做事

    after = (s.energy, s.valence, s.boredom)

    kind = "heartbeat" if d.action == "none" else ("error" if not d.parse_ok else "event")
    if result and not result.ok and d.action == "tool":
        kind = "event"   # 调了工具但没成，也算一次 event（结果记 summary）

    next_wake = util.now() + _minutes(d.next_wake_in_min)
    # 一次唤醒可能是两次调用，token 要累加，否则成本账会少算一半
    tokens_in, tokens_out = d.tokens
    if reaction is not None:
        tokens_in += reaction.tokens[0]
        tokens_out += reaction.tokens[1]

    # 注意：这里必须显式写出六个值的对应关系。
    # 曾经用 *before, *after 解包，但列是 before/after 交错的，
    # 结果 energy_after 落的是 valence……这种错位会静默毁掉
    # §10.2 的全部观测数据，而且测试发现不了——列名都对，只是串了。
    cur = conn.execute(
        """INSERT INTO events
           (ts, kind, trigger, tool, args_json, result_summary, why,
            energy_before, energy_after, valence_before, valence_after,
            boredom_before, boredom_after, next_wake_at,
            tokens_in, tokens_out, duration_ms, parse_ok, trace_id)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            util.now_iso(), kind, trigger, d.tool,
            json.dumps(d.args, ensure_ascii=False) if d.args else None,
            # 空转也要留下「事实」，否则下一轮的【最近在做的事】里她
            # 看不到自己刚空转过一次——2026-11-10 她凌晨选择发呆，天亮
            # 后的自己对此一无所知，连续性缺了一块。
            result.summary if result else (
                "空转了一轮，什么都没做" if kind == "heartbeat" else None
            ),
            d.why or "（没说理由）",
            before[0], after[0],        # energy
            before[1], after[1],        # valence
            before[2], after[2],        # boredom
            next_wake.isoformat(timespec="seconds"),
            tokens_in, tokens_out,
            int((util.now() - t0).total_seconds() * 1000),
            1 if d.parse_ok else 0,
            tid,
        ),
    )

    # I1 的执行点：只有真实做了事才允许写情景记忆。
    # 现在的 memory 只可能来自第二段（看到结果之后），空转结构上就没有这个词。
    # source_event_id 把「事后想法」挂回「那次唤醒」：下一轮的
    # 【最近在做的事】要靠它把反思渲染进上下文，不靠检索碰运气。
    if kind == "event" and memory_text:
        memory.add_episodic(
            conn, memory_text,
            salience=_event_salience(before[1], after[1], d.tool or ""),
            tags=d.tool,
            source_event_id=cur.lastrowid,
        )

    # 自省：每 N 次 wake 重写一次自我叙事（Design_v0.2 §5.3）。
    # 同轮守卫：如果她这一轮自己选了 self_review 工具，就不再强制自动自省，
    # 避免「主动做一次 + 定时又来一次」重复刷新同一份叙事。
    if db.wake_count(conn) % config.SELF_REVIEW_EVERY == 0 and d.tool != "self_review":
        # 自省是「制度」，不是「能力」：它坏了不该拖垮整次唤醒。
        # 注意 state_mod.save 在下面——这里一旦抛异常，这次唤醒的
        # 状态变更就全部丢失，而 events 已经写进去了，账会对不上。
        try:
            await _self_review(conn, client, tid)
        except Exception as e:
            trace.record("error", tid, where="self_review",
                         error=f"{type(e).__name__}: {e}")

    state_mod.save(conn, s)
    conn.commit()


# 保留率闸门（_retained_ratio / _review_decision）已迁到 brain.py：
# 「执行自省」既要被定时路径调用、又要被 self_review 工具调用，放在 brain
# 这一层谁 import 都不会成环（scheduler / tools 都 import brain，brain 不回 import 它们）。



async def _self_review(conn: sqlite3.Connection, client, trace_id: str = "") -> None:
    """核心记忆自省：重写自我叙事 + 关系叙事（Design_v0.2 §5.3）。

    执行体在 brain.run_self_review——定时路径与 self_review 工具共用
    同一套闸门/落库逻辑，不会出现两套不一致的叙事。

    注意：run_self_review 必须同步、在当前（事件循环）线程里跑。它会用
    conn 做落库，而 SQLite 连接不能跨线程使用——所以这里不能用
    asyncio.to_thread 包它（那会把主线程的 conn 丢进工作线程报错）。
    自省里的那次 LLM 调用会短暂阻塞事件循环，但每 10 次唤醒才发生一次，
    可接受；工具路径同理（dispatch 本就在主线程同步执行）。
    """
    brain.run_self_review(conn, client, trace_id)


# ── 主循环 ───────────────────────────────────────────────────────────────

async def run(conn: sqlite3.Connection, client) -> None:
    print(f"鱼鱼醒了。（模式：{'DRY_RUN 空跑' if config.DRY_RUN else '真实 API'}）")
    while True:
        now = util.now()
        s = state_mod.load(conn)

        # 时间的自然流逝先记账：状态回归按 updated_at 到现在折算
        updated = util.parse_iso(
            conn.execute("SELECT updated_at FROM state WHERE id=1").fetchone()["updated_at"]
        )
        state_mod.tick_regress(s, (now - updated).total_seconds() / 60.0)
        state_mod.save(conn, s)

        go, trigger = _should_wake(conn, s, now)
        if go:
            if wake_lock.locked():
                pass                       # I3：上一次还没跑完，跳过本次 tick
            else:
                async with wake_lock:
                    try:
                        await wake(conn, client, trigger)
                        latest = db.latest_event(conn)
                        print(f"[{now:%H:%M}] wake({trigger}) → {latest['kind']}: "
                              f"{(latest['result_summary'] or latest['why'])[:50]}")
                    except Exception as e:
                        print(f"wake 崩了：{type(e).__name__}: {e}")
                        # wake 内部崩了也要留痕，否则日志会出现空洞
                        conn.execute(
                            "INSERT INTO events (ts, kind, trigger, why) VALUES (?, 'error', ?, ?)",
                            (util.now_iso(), trigger, f"{type(e).__name__}: {e}"),
                        )
                        conn.commit()

        await asyncio.sleep(config.TICK_SECONDS)
