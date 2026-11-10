"""大脑层：调 DeepSeek API，把自然语言变成结构化决策。

这一层最重要的纪律：模型说什么不算数，只有解析成功、
通过校验的东西才往下传。解析失败绝不「沿用上次的结果」——
那会造出一个卡在循环里的东西（Design_v0.2 §7.4）。

关于「思考过程」：推理类模型会把思维链放在响应消息的
reasoning_content 字段里，和正式的 content 分开返回。
下面 api_reply.reasoning 抓的就是它，一并写进轨迹，
不然 Prompt 里那句「why」只是结论，看不到她怎么绕到那的。
"""

import difflib
import json
import re
import sqlite3
import time
from dataclasses import dataclass, field

from . import config, prompt, state as state_mod, trace, util


@dataclass
class Decision:
    action: str                  # "tool" 或 "none"
    tool: str | None
    args: dict = field(default_factory=dict)
    why: str = ""
    felt_delta: dict = field(default_factory=dict)   # 只表示「此刻的感觉」
    next_wake_in_min: int = 30   # 默认值：模型没给时的兜底
    raw: str = ""
    parse_ok: bool = True
    tokens: tuple[int, int] = (0, 0)      # (输入, 输出)，用来记账
    # 注意这里没有 memory：记忆只在第二段（看到结果之后）才产生。


@dataclass
class Reaction:
    """第二段调用的产物：这件事在她身上留下了什么。

    跟 Decision 刻意分开：Decision 是「想做什么」，Reaction 是「做完了怎么样」。
    合成一个类，就会重蹈「还没看到内容就已经写完记忆」的覆辙。
    """
    felt_delta: dict = field(default_factory=dict)
    memory: str = ""
    next_wake_in_min: int | None = None    # None = 不改，沿用第一段定的
    raw: str = ""
    parse_ok: bool = True
    tokens: tuple[int, int] = (0, 0)


@dataclass
class ApiReply:
    content: str
    reasoning: str
    tokens: tuple[int, int]
    duration_ms: int


def make_client():
    """创建 API 客户端。DRY_RUN 下返回 None，调用走假通道。"""
    if config.DRY_RUN:
        return None
    from openai import OpenAI
    return OpenAI(api_key=config.DEEPSEEK_API_KEY, base_url=config.BASE_URL)


def _call_api(client, messages: list[dict], temperature: float,
              trace_id: str = "", attempt: int = 1,
              kind: str = "decision") -> ApiReply:
    """发一次请求，顺手把完整收发写进轨迹。

    注意 API 里的两处兼容处理：
    - reasoning_content 不是官方 SDK 的必备字段，用 getattr 兜底，
      换成不支持的模型也不会崩。
    - 请求消息原样记录（含重试时追加的修复对话），
      这样「为什么第二次调用是那样」永远查得到。

    kind 只影响轨迹里的标签（decision / reaction），方便按段筛选：
    一次唤醒最多两段，两段用同一个 trace_id。
    """
    t0 = time.time()
    resp = client.chat.completions.create(
        model=config.MODEL,
        messages=messages,
        temperature=temperature,
        response_format={"type": "json_object"},   # DeepSeek 支持 JSON 模式
    )
    msg = resp.choices[0].message
    content = msg.content or ""
    reasoning = getattr(msg, "reasoning_content", None) or ""
    usage = resp.usage
    duration = int((time.time() - t0) * 1000)

    trace.record(
        kind, trace_id,
        model=config.MODEL, temperature=temperature, attempt=attempt,
        messages=messages,
        content=content, reasoning=reasoning,
        tokens_in=usage.prompt_tokens, tokens_out=usage.completion_tokens,
        duration_ms=duration,
    )
    return ApiReply(content, reasoning,
                    (usage.prompt_tokens, usage.completion_tokens), duration)


def _clean_felt(raw) -> dict:
    """只留 energy/valence，每个值截到 -10..+10。

    注意 boredom 永远不在允许名单里：她能给自己加无聊的话，
    就能靠「我很无聊」触发紧急唤醒，「自主」在结构上就变成假的了。
    """
    clean = {}
    for k in ("energy", "valence"):
        try:
            clean[k] = max(-config.FELT_DELTA_RANGE,
                           min(config.FELT_DELTA_RANGE, float((raw or {}).get(k, 0))))
        except (TypeError, ValueError):
            pass
    return clean


def _clamp_decision(d: Decision) -> Decision:
    """模型输出 → 能安全生效的决策。

    三道闸门：
    1. felt_delta 只留 energy/valence，每个值截到 -10..+10
    2. 没选工具时，把 action/tool 归一化成空转
    3. next_wake_in_min 钳进 [10, 180]
    """
    d.felt_delta = _clean_felt(d.felt_delta)

    if d.action != "tool" or not d.tool:
        d.action, d.tool = "none", None
    d.next_wake_in_min = max(config.WAKE_MIN_GAP, min(config.WAKE_MAX_GAP, d.next_wake_in_min))
    return d


def _parse_decision(reply: ApiReply) -> Decision:
    try:
        obj = json.loads(reply.content)
    except json.JSONDecodeError:
        return Decision(action="none", why="（没说清楚）", raw=reply.content,
                        parse_ok=False, tokens=reply.tokens)

    d = Decision(
        action=str(obj.get("action", "none")),
        tool=obj.get("tool"),
        args=obj.get("args") if isinstance(obj.get("args"), dict) else {},
        why=str(obj.get("why", ""))[:200],
        felt_delta=obj.get("felt_delta") if isinstance(obj.get("felt_delta"), dict) else {},
        next_wake_in_min=_to_int(obj.get("next_wake_in_min"), 30),
        raw=reply.content,
        tokens=reply.tokens,
    )
    return _clamp_decision(d)


def _to_int(v, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def decide(client, messages: list[dict], trace_id: str = "") -> Decision:
    """一次 wake 的决策。两级重试，再失败就强制空转（记 error）。"""
    if config.DRY_RUN:
        return _dry_run_decision(messages)

    reply = _call_api(client, messages, config.TEMPERATURE, trace_id, attempt=1)
    d = _parse_decision(reply)
    if d.parse_ok:
        return d

    # 第二次：降温度 + 把上次的坏输出贴回去让她自己修
    repair = messages + [
        {"role": "assistant", "content": reply.content},
        {"role": "user", "content": "上面的 JSON 格式不对。重新用正确格式回复一遍。"},
    ]
    reply2 = _call_api(client, repair, 0.3, trace_id, attempt=2)
    d2 = _parse_decision(reply2)
    if d2.parse_ok:
        return d2

    return Decision(action="none", why="（两次都没能给出有效回复）",
                    parse_ok=False, raw=reply2.content,
                    tokens=(reply.tokens[0] + reply2.tokens[0],
                            reply.tokens[1] + reply2.tokens[1]))


def _parse_reaction(reply: ApiReply) -> Reaction:
    try:
        obj = json.loads(reply.content)
    except json.JSONDecodeError:
        return Reaction(parse_ok=False, raw=reply.content, tokens=reply.tokens)
    if not isinstance(obj, dict):
        return Reaction(parse_ok=False, raw=reply.content, tokens=reply.tokens)

    nxt = None
    if obj.get("next_wake_in_min") is not None:
        # 读完才知道值不值得早醒，所以允许她改主���；但同样要钳进合法区间
        nxt = max(config.WAKE_MIN_GAP,
                  min(config.WAKE_MAX_GAP, _to_int(obj.get("next_wake_in_min"), 0)))
    return Reaction(
        felt_delta=_clean_felt(obj.get("felt_delta")),
        memory=str(obj.get("memory") or "").strip(),
        next_wake_in_min=nxt,
        raw=reply.content,
        tokens=reply.tokens,
    )


def react(client, base_messages: list[dict], decision: Decision, tool: str,
          summary: str, detail: str, trace_id: str = "") -> Reaction:
    """第二段：看着刚才的结果自评。

    解析失败会重试一次（降温度）；再失败就降级——状态不动、记忆不写。
    这里降级不致命：`summary` 已经进了 events，下一轮【最近在做的事】
    仍然写着「你读了什么」，连续性断不了。
    """
    if config.DRY_RUN:
        return Reaction(felt_delta={"valence": 1},
                        memory=f"（空跑模式）看了 {tool} 的结果。")

    msgs = prompt.build_reaction_messages(base_messages, decision.raw, tool, summary, detail)
    reply = _call_api(client, msgs, config.TEMPERATURE, trace_id, 1, "reaction")
    r = _parse_reaction(reply)
    if r.parse_ok:
        return r

    repair = msgs + [
        {"role": "assistant", "content": reply.content},
        {"role": "user", "content": "上面的 JSON 格式不对。重新用正确格式回复一遍。"},
    ]
    reply2 = _call_api(client, repair, 0.3, trace_id, 2, "reaction")
    r2 = _parse_reaction(reply2)
    if r2.parse_ok:
        return r2
    return Reaction(parse_ok=False, raw=reply2.content,
                    tokens=(reply.tokens[0] + reply2.tokens[0],
                            reply.tokens[1] + reply2.tokens[1]))


def _dry_run_decision(messages: list[dict]) -> Decision:
    """空跑通道：不花钱，固定返回「写一条日记」。

    目的只有一个：验证组装出的上下文和整条执行链路是否通畅。
    """
    log = config.LOGS_DIR / "last_prompt.txt"
    config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log.write_text(
        "\n\n---\n\n".join(f"[{m['role']}]\n{m['content']}" for m in messages),
        encoding="utf-8",
    )
    return Decision(
        action="tool", tool="write_journal", args={"text": "（空跑模式：这是测试日记）"},
        why="（空跑模式）", felt_delta={"valence": 2},
        next_wake_in_min=15, parse_ok=True,
    )


def review_narrative(client, old_core: str, old_relation: str = "",
                     exchanges: str = "", trace_id: str = "") -> dict | None:
    """自省：请求重写自我叙事 + 关系叙事。

    返回 {"new_core": str|None, "new_relation": str|None}；解析失败返回 None。
    保留率检查与写入由调用方（scheduler）负责，因为保留率门槛是「制度」，
    不是「能力」——它不该由模型感知。relation 的落库规则也由 scheduler 决定。
    """
    if config.DRY_RUN:
        # 空跑模式不自省，原样回传当前两版（不会落库，scheduler 不写）
        return {"new_core": old_core, "new_relation": old_relation or ""}
    reply = _call_api(
        client,
        prompt.build_review_messages(old_core, old_relation, exchanges),
        0.7, trace_id,
    )
    try:
        data = json.loads(reply.content)
        new_core = str(data.get("new_core") or "").strip() or None
        new_relation = str(data.get("new_relation") or "").strip() or None
        return {"new_core": new_core, "new_relation": new_relation}
    except (json.JSONDecodeError, AttributeError):
        return None


# ── 自省闸门（纯函数，便于单测，不碰库/网络）────────────────────────────────
# 原本在 scheduler 里，但「执行自省」既要被定时路径（每 N 次 wake）调用，
# 又要被 self_review 工具调用——放在 brain 这一层谁 import 都不会成环
# （scheduler / tools 都 import brain，而 brain 不 import 它们）。

def _retained_ratio(old: str, new: str) -> float:
    """旧叙事有多大比例被新版保留（单向：新增内容不加分也不扣分）。

    这里刻意不用 difflib.ratio()：它的分母含新文本长度，于是「往后追加
    一句」会让分数掉到 0.5 上下被当成漂移拒掉——而追加恰恰是自我叙事
    最正常的生长方式。改成按句子算「旧内容有多少被保留」，只有删掉或
    推翻才会低分。阈值见 config.SELF_REVIEW_MIN_RETENTION。
    """
    sents = [s for s in re.split(r"[。！？\n]", old) if len(s.strip()) >= 4]
    if not sents:
        return 1.0
    total = 0.0
    for s in sents:
        sm = difflib.SequenceMatcher(None, s, new, autojunk=False)
        # 匹配块求和，而不是只取最长一段：一句话中间改几个字，
        # 不该被判成「这句没保留」。
        total += sum(b.size for b in sm.get_matching_blocks()) / len(s)
    return total / len(sents)


def _review_decision(old: str, new: str) -> tuple[bool, str]:
    """决定自省结果是否被接受。

    返回 (accepted, detail)：detail 用于事件日志，调用方再按接受/拒绝加前缀。
    两层闸：
      1) 硬上限：new 超过 SELF_REVIEW_HARD_MAX → 拒绝（安全网，正常几乎不触发）。
      2) 保留率：平时要求保留 ≥MIN_RETENTION 的旧句（防一把推翻自己）；
         但旧版已超软目标时放宽到 MIN_RETENTION_OVER_TARGET，允许往 target 压。
    """
    if len(new) > config.SELF_REVIEW_HARD_MAX:
        return False, (f"超长：{len(new)} > {config.SELF_REVIEW_HARD_MAX}"
                       f"（保留上一版，下轮请压缩）")
    eff_min = (config.SELF_REVIEW_MIN_RETENTION_OVER_TARGET
               if len(old) > config.SELF_REVIEW_TARGET_MAX
               else config.SELF_REVIEW_MIN_RETENTION)
    retained = _retained_ratio(old, new)
    if retained < eff_min:
        return False, f"保留率 {retained:.2f} < {eff_min}（旧叙事被删太多）"
    return True, f"保留率 {retained:.2f}，{len(old)}→{len(new)}字"


def run_self_review(conn: sqlite3.Connection, client, trace_id: str = "") -> dict:
    """执行一次完整的自省（core + relation 重写）并落库、记事件。

    定时路径（scheduler._self_review）与 self_review 工具共用这一份执行体，
    所以无论她是「被安排每 N 次做一次」还是「自己想做」，产物走同一个制度，
    不会出现两套不一致的叙事。返回 {"summary": str, "new_core": str|None}。

    DRY_RUN 下不调 API、不写库——空跑语义的延伸（见 scheduler.wake 的
    DRY_RUN 短路）：自省也是「制度动作」，不该在空跑时真的改她的自我认知。
    """
    if config.DRY_RUN:
        return {"summary": "（空跑）自省未实际执行", "new_core": None}
    from . import memory, messages

    old_core = memory.get_narrative(conn, "core")
    old_relation = memory.get_narrative(conn, "relation")
    # 冷启动时没有「上一版」。喂给模型的必须是占位符而不是 None，
    # 否则 review_narrative 会拼出「【你现在的自我叙事】None」。
    seed_core = old_core if old_core is not None else prompt.CORE_PLACEHOLDER
    seed_relation = old_relation if old_relation is not None else ""

    rows = messages.recent_exchanges(conn, limit=config.REVIEW_RELATION_EXCHANGES)
    exchanges = messages.format_exchanges(rows)

    result = review_narrative(client, seed_core, seed_relation, exchanges, trace_id)
    if not result:
        return {"summary": "自省失败：模型没返回有效结果", "new_core": None}
    new_core = result.get("new_core")
    new_relation = result.get("new_relation")

    # ── 关系叙事：v1 无闸门，非空即落库 + 记事件 ──
    if new_relation:
        memory.save_narrative(conn, "relation", new_relation)
        conn.execute(
            """INSERT INTO events (ts, kind, trigger, why, trace_id)
               VALUES (?, 'self_review', 'self_review', ?, ?)""",
            (util.now_iso(), f"关系叙事已更新（{len(new_relation)}字）", trace_id or None),
        )
        conn.commit()

    # ── 自我叙事：沿用既有保留率闸门 ──
    if not new_core:
        return {"summary": "自省跳过：模型未给出新的自我叙事", "new_core": None}
    if old_core is None:
        # 第一版没有比较对象，不设闸门。但要记一条事件，
        # 否则「core 什么时候长出来的」在日志里是空白。
        memory.save_narrative(conn, "core", new_core)
        conn.execute(
            """INSERT INTO events (ts, kind, trigger, why, trace_id)
               VALUES (?, 'self_review', 'self_review', ?, ?)""",
            (util.now_iso(), f"自我叙事首次建立（{len(new_core)}字）", trace_id or None),
        )
        conn.commit()
        return {"summary": f"自我叙事首次建立（{len(new_core)}字）", "new_core": new_core}

    accepted, detail = _review_decision(old_core, new_core)
    if not accepted:
        conn.execute(
            """INSERT INTO events (ts, kind, trigger, why, trace_id)
               VALUES (?, 'error', 'self_review', ?, ?)""",
            (util.now_iso(), f"自省被拒：{detail}", trace_id or None),
        )
        conn.commit()
        return {"summary": f"自省被拒：{detail}", "new_core": None}

    memory.save_narrative(conn, "core", new_core)
    conn.execute(
        """INSERT INTO events (ts, kind, trigger, why, trace_id)
           VALUES (?, 'self_review', 'self_review', ?, ?)""",
        (util.now_iso(), f"自我叙事已更新（{detail}）", trace_id or None),
    )
    conn.commit()
    return {"summary": f"自我叙事已更新（{detail}）", "new_core": new_core}
