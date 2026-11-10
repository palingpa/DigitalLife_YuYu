"""大脑层：调 DeepSeek API，把自然语言变成结构化决策。

这一层最重要的纪律：模型说什么不算数，只有解析成功、
通过校验的东西才往下传。解析失败绝不「沿用上次的结果」——
那会造出一个卡在循环里的东西（Design_v0.2 §7.4）。

关于「思考过程」：推理类模型会把思维链放在响应消息的
reasoning_content 字段里，和正式的 content 分开返回。
下面 api_reply.reasoning 抓的就是它，一并写进轨迹，
不然 Prompt 里那句「why」只是结论，看不到她怎么绕到那的。
"""

import json
import time
from dataclasses import dataclass, field

from . import config, prompt, state as state_mod, trace


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
