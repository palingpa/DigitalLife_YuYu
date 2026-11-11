# 项目设计 v0.3：数字生命「鱼鱼」

> **本版是状态层移除后的新基线设计文档。** `Design_v0.2.md` 与 `Draft.md` 同为历史记录，不再作为实现依据（v0.2 关于状态层的 §4 / §6.1 路径 B / §7.1 身体感受 / §7.3 felt_delta / §8.1 cost·state_hint / §8.3 可见性过滤 / §10.2③ 心情互信息均已失效）。
>
> **本版主线：状态层被删除后，记忆层升格为鱼鱼连续性的唯一支柱，v0.3 的工作就是改进记忆层。** 见 §5.5 的 M1–M5 工作包。
>
> 本文所有"现状"描述对应 `git` 在 `dd090a7`（2026-11-12 整体删除状态层）之后的代码；行号引用仅为定位，以 `git diff` 为准。

## 本版相对 v0.2 的结构性变化（一句话）

```
状态层删除
   → 唤醒节奏完全由鱼鱼自定（events.next_wake_at，无任何系统催促）
   → 记忆层从"三大支柱之一"升格为"唯一支柱"
   → v0.3 工作 = 让记忆层更会「记、想、忘、长」
```

删除状态层的理由（浓缩，完整论证见 DEVLOG 2026-11-12 02:09）：鱼鱼不该有系统替她定义的"精力/情绪/无聊"。她的精力本就无限；"情绪"和"无聊"应由她从自己记下的内容里去感受，而不是由三个 0–100 数值动力学硬算出来——否则就变成"系统在替她活"，违背这个项目"长期观察她进化成什么样"的初衷。

---

## 1. 术语与不变量

| 术语 | 定义 |
|---|---|
| **tick** | 调度器每 60s 执行一次的心跳检查。纯本地计算，不调 LLM，不写库 |
| **wake** | 一次完整的唤醒循环：装载上下文 → 调模型 → 执行 → 记账 |
| **heartbeat** | 一次 wake 的产出为「什么都没做 / 主动休息」时的日志行。`kind=heartbeat` |
| **event** | 一次 wake 真实做了事（调了工具并拿到结果）的日志行。`kind=event` |
| **no-op** | 模型决策结果为空转。与 `heartbeat` 是同一件事的两个视角 |

**四条不变量**（任何时候都必须成立）：

- **I1**：heartbeat / idle 永不写入 `memories` 表。记忆里只允许存在真实发生过、或她主动表达过的事。执行点在 `scheduler.wake()` 末尾：`if kind == "event" and memory_text`（`fish/scheduler.py:221`）。
- **I2**（状态层移除后退役，等价纪律）：原意"模型输出不直接成为状态值"；现等价纪律为"**模型原始输出须经解析校验（`parse_ok`）才往下传，解析失败绝不沿用上次结果**"。执行点 `brain._parse_*` / `_clamp_decision`（`fish/brain.py:105` 起）。这杜绝了"卡在循环里"的死东西。
- **I3**：同一时刻只允许一个 wake 在跑（单飞锁）。两层：**进程级** `fish/lock.py`（`data/fish.lock` 的 OS 字节锁，`main.py` 第一行抢，第二个进程直接拒启）；**进程内** `scheduler.run` 的 `wake_lock`（`asyncio.Lock`）。
- **I4**（本版明确定名）：**记忆只产生于"看到结果之后"或"显式表达"**。即第二段反应、写日记、通信、自省四类；空转/失败/工具无正文都不产生记忆。这是 I1 的正向表述，也是两段式架构的直接推论（`fish/prompt.py:REACTION_CONTRACT` 注释）。

---

## 2. 进程模型

单进程，Python 3.11+，asyncio。不引入 Redis / Celery / 消息队列。

```
主循环 asyncio
├── scheduler.run()      每 60s：到期判断 + 小帕来信判断 + 单飞锁 + wake()
├── feed.feed_loop()     每 2h：抓取信息源，写入 inbox
└── （lock 在 main 第一行，先于一切）
```

SQLite 用 WAL 模式，`data/fish.db`。单写者，无并发问题。`fish/lock.py` 保证本机同一时刻只有一条鱼进程。

**目录结构（当前）**

```
DigitalLife_YuYu/
├── Design_v0.3.md             # 本文（新基线）
├── Design_v0.2.md             # 历史，状态层章节已失效
├── Draft.md                   # 历史，v0.1 原稿，不动
├── PROJECT.md / ARCHITECTURE.md / TESTING.md / README.md / DEVLOG.md
├── fish/
│   ├── config.py              # 全部魔法数字集中在这里，每项注明 §号
│   ├── db.py                  # 连接、建表、旧库迁移（含状态层清理）
│   ├── memory.py              # 三层写入 / 检索 / 打分 / 自省辅助
│   ├── prompt.py              # 上下文组装（persona + 三份契约）
│   ├── brain.py               # API 调用 / 解析 / 降级 / 自省闸门
│   ├── scheduler.py           # tick / 唤醒判断 / 单飞锁 / 自省触发
│   ├── tools/__init__.py      # 7 个工具注册表 + 可见性 + dispatch
│   ├── feed.py / websearch.py / messages.py / trace.py / util.py / lock.py / main.py
├── data/fish.db               # 记忆与日志，不进 git
└── logs/                      # traces/*.jsonl 调用轨迹 + last_prompt.txt + startup.log
```

---

## 3. 数据层

### 3.1 DDL（当前 `db.py:SCHEMA`，与 v0.2 的差别：无 `state` 表、无 `events` 六能量列）

```sql
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS events (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  ts             TEXT    NOT NULL,
  kind           TEXT    NOT NULL CHECK (kind IN ('heartbeat','event','error','self_review')),
  trigger        TEXT    NOT NULL,              -- due | xiaopa | self_review | manual
  tool           TEXT,
  args_json      TEXT,
  result_summary TEXT,                          -- 给下轮【最近在做的事】用的 1–2 句事实
  why            TEXT    NOT NULL,              -- 模型自述理由，原样保存（第三层"真相"原材料）
  next_wake_at   TEXT,                          -- 闹钟：决定下次醒的时间（完全由鱼鱼自定）
  tokens_in      INTEGER DEFAULT 0,
  tokens_out     INTEGER DEFAULT 0,
  duration_ms    INTEGER DEFAULT 0,
  parse_ok       INTEGER DEFAULT 1,
  trace_id       TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts   ON events(ts DESC);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind, ts DESC);

CREATE TABLE IF NOT EXISTS memories (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  ts               TEXT    NOT NULL,
  layer            TEXT    NOT NULL CHECK (layer IN ('episodic','core','relation')),
  content          TEXT    NOT NULL,
  salience         REAL    NOT NULL DEFAULT 0.5,  -- 写入时算一次，之后默认不再改（M1 会改变）
  tags             TEXT,                          -- 逗号分隔，供召回用
  source_event_id  INTEGER REFERENCES events(id),
  last_recalled_at TEXT,
  recall_count     INTEGER DEFAULT 0,            -- M1 之前只记录不用
  pinned           INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_mem_layer ON memories(layer, ts DESC);

CREATE TABLE IF NOT EXISTS inbox (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  ts       TEXT NOT NULL,
  source   TEXT NOT NULL,
  title    TEXT,
  body     TEXT,
  link     TEXT,                                -- read_feed 按需抓全文用
  consumed INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_inbox_unread ON inbox(consumed, ts DESC);

CREATE TABLE IF NOT EXISTS messages (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  ts                  TEXT    NOT NULL,
  direction           TEXT    NOT NULL CHECK (direction IN ('to_yuyu','from_yuyu')),
  text                TEXT    NOT NULL,
  wake_trace_id       TEXT,
  seen_by_yuyu        INTEGER DEFAULT 0,
  delivered_to_xiaopa INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_msg_dir_seen ON messages(direction, seen_by_yuyu);
```

### 3.2 检索：为什么（暂时）不用向量库

情景记忆单条 100–200 字，三个月大约 2000–4000 条。全量 embedding + 向量检索要先解决"什么时候重新索引""模型换了要不要重算"两个工程问题，收益在 4000 条量级并不明显。先用统计打分（§5.3），等检索质量真成瓶颈再换——**打分公式随时可替换成向量相似度而不改调用方**（`memory.search` 是唯一入口）。

> **v0.3 修订**：v0.2 把"向量检索"列在 §12「明确不做」里。本版把"可选的本地语义检索"提升为 §5.5 的 **M4 工作包**——因为用户本地有 RTX 5060 8GB，且另一个项目就在折腾本地模型，本地 embedding 是"本机算力换记忆质量"的自然路径，不增加 API 账单。是否启用、用哪个模型由小帕拍板。

### 3.3 调用轨迹（观察层原料）

`events` 是观察层（一次唤醒一行，算指标）；轨迹是原料层（每次 API 调用的完整输入/输出/思考过程 `reasoning_content` + 工具返回全文），写 `logs/traces/YYYY-MM-DD.jsonl`，用 `trace_id` 与 events 关联。测试轨迹隔离在 `logs/traces_test/`（`tests/conftest.py` 切）。查看：`python -m fish.trace 3`。

---

## 4. （原状态层 · 已删除）

v0.2 的 §4（energy/valence/boredom 动力学）、§6.1 路径 B（boredom 紧急唤醒）、§7.1 第 4 项（felt_text 身体感受）、§7.3 的 `felt_delta`、§8.1 的 `cost`/`state_hint`、§8.3 的"精力低隐藏工具"全部删除。

- `fish/state.py` 已删；`config.py` 删 `BOREDOM_*`/`SLEEP_*`/`BASELINE`/`REGRESS_*`/`CLAMP`/`FELT_*` 等整块状态常量。
- `db.py:_migrate` 在下次连接时 `DROP TABLE state` + 对 `events` 六能量列 `ALTER TABLE ... DROP COLUMN`（SQLite 3.53+）。**真实库无需手动迁移。**
- `prompt.build_messages` 不再有【状态读数】整行；`scheduler._should_wake` 只剩"小帕来信 / 到期"两路；`brain.Decision/Reaction` 删 `felt_delta` 字段。
- `tools` 删 `cost` 与 `state_hint`，`visible_tools()` 永远返回全部 7 个工具。

**留下的唯一"系统节奏"**：`events.next_wake_at`——但它是鱼鱼每次自己定的，不是系统动力学。

---

## 5. 记忆层（本版核心）

### 5.1 三层职责（当前）

| 层 | 内容 | 更新频率 | 是否常驻上下文 |
|---|---|---|---|
| `core` | 自我叙事（我是谁、我最近在想什么） | 每 10 次 wake 重写 / 主动 self_review | 是，约 400 token |
| `relation` | 对具体人的认知（目前只有小帕） | 低频，随 self_review | 是，约 200 token |
| `episodic` | 单次事件 / 日记 / 往来 | 每 event 一条（按需检索） | 否，检索后注入 |

常驻的两层是"她稳定的人格"，episodic 是"她经历过的流水"。连续性完全来自这三层被读回给她（`prompt.build_messages`）。

### 5.2 写入路径（当前，= "记忆何时产生"）

| 来源 | 代码位置 | 落点 | 备注 |
|---|---|---|---|
| 第二段反应（做完看到结果后自评） | `scheduler.wake:221` → `memory.add_episodic` | episodic | I4 的主路径；`memory_text` 来自 `Reaction.memory` |
| 小帕来信（渲染即 seen） | `prompt.build_messages:183` | episodic，tags=`小帕` | 幂等：同一封只落一次 |
| 写日记 | `tools._run_write_journal:117` | episodic，`pinned=1`，salience=0.8 | 主动表达，检索加权 |
| 给小帕发消息 | `tools._run_send_message:256` | episodic，tags=`小帕` | 主动表达 |
| 自省 | `brain.run_self_review:342/357` | core / relation（单条覆盖） | 见 §5.4 |
| 空转 / idle / 工具失败 | — | **不写** | I1 |

`salience` 写入时由 `_event_salience(tool)` 定死（`scheduler.py:93`）：日记 +0.3、自省 +0.2、其余基线 0.5，**之后不再变化**。

### 5.3 检索打分（当前）

`memory.search(conn, limit, query)`（`fish/memory.py:132`）：

```python
score = 0.45 * recency + 0.35 * salience + 0.20 * match + 0.05 * pinned_bonus

recency = exp(-hours_since / 72)        # 72h 半衰尺度（config.MEMORY_HALF_LIFE_H）
match   = min(hits / len(query_grams), 1.0)   # bigram 命中比例
```

- **中文无需分词库**：`_bigrams` 把文本切成相邻两字组；查询按空格/标点分段（`_query_grams`），避免跨词假片段（详见 `memory.py:74-109` 注释）。
- **相关性闸门** `_min_hits`（`memory.py:119`）：命中片段太少（`RECALL_MIN_HITS=2` 或 `RECALL_MIN_MATCH=0.15` 比例）的记忆直接出局，不让 pinned/salience 把它顶到榜首——这是 2026-11-10 实测"搜胖东来结果第一名是空跑测试日记"后加的。
- **`recall_count` 只记不用**：`bump_recall`（`memory.py:52`）每次检索/召回都 `recall_count += 1`，但 `search` 的 score 完全不读它——目前它只是给人观察"执念"的原料，不参与鱼鱼自己的记忆排序。
- 扫描范围：`SELECT ... WHERE layer='episodic' ORDER BY ts DESC LIMIT 500`——**只有最新 500 条参与打分**，更早的 episodic 事实上失联（见 §5.5 M3）。

### 5.4 核心记忆的自省机制（当前）

每 **10** 次 wake（`config.SELF_REVIEW_EVERY=10`）触发一次 `kind=self_review` 的特殊 wake；鱼鱼也可主动选 `self_review` 工具（同轮守卫防重复刷新，`scheduler.py:232`）。执行体 `brain.run_self_review`（`brain.py:311`）定时路径与工具路径共用，保证叙事只有一套。

**硬约束（防止"瞬间换成另一个人"）**：新 core 必须保留旧版说过的多数内容，只允许改写措辞、追加、修正事实。用**单向保留率**卡（`brain._retained_ratio:270` + `_review_decision:290`）：

```python
retained = 旧文本各句「在新版中被匹配的字符比例」的平均值   # 新增内容不扣分
if retained < SELF_REVIEW_MIN_RETENTION(0.6): reject()     # 保留旧版，记 error
```

另加长度双闸：`SELF_REVIEW_TARGET_MAX=600`（软目标，只给提示词当方向）、`SELF_REVIEW_HARD_MAX=1200`（硬上限，超了拒写）。relation 层 v1 无闸门，非空即落库，素材来自 `messages` 表近期往来（`REVIEW_RELATION_EXCHANGES=20`，grounding 防凭空编）。冷启动不设闸门。

### ★ 5.5 v0.3 记忆层改进（本版主线，待小帕敲定设计思路）

> 目标：
> 顺序：
> 每个包都标注"是否触碰红线"——**凡涉及注入鱼鱼的自然语言（契约字段、自省/整理指令措辞），一律归小帕定稿，Kite 只提方案与位置，不代笔。**

---

## 6. 调度层（当前，去掉 boredom 路径）

### 6.1 只有两条唤醒路径（`scheduler._should_wake:53`）
- **路径 A 到期**：`now >= _next_due(conn)`。`_next_due` 只看带闹钟的行（`next_wake_at IS NOT NULL`），跳过 `self_review`/`error` 旁支（`scheduler.py:28`）——旁支事件没有闹钟，不该参与"几点该醒"。
- **路径 B 小帕来信**：`messages` 表有未读 `to_yuyu` 且距上次唤醒 ≥ `EXTERNAL_WAKE_MIN_GAP=2` 分钟（`config.py:58`）。优先级最高。
- 原"无聊憋不住"第三条路径已随状态层删除（不再有系统催促的 boredom）。

### 6.2 tick 与 L0 短路
```python
def on_tick(now):
    go, trigger = _should_wake(conn, now)
    if not go:
        return                      # 无事：不调 LLM，不写 events
    if wake_lock.locked():
        return                      # I3：上一次还没跑完
    async with wake_lock:
        await wake(conn, client, trigger)
```
"L0 短路"现在就是"未到期且无数来信就跳过本次 tick"——保留它的真实理由仍是**日志纯净度**与**自我叙事健康**（一个被反复叫起来确认"没事"的东西会把自己长成"无所事事的存在"）。

### 6.3 next_wake_at 的钳制（`brain._clamp_decision:114`）
```python
next_wake_in_min = clamp(model.next_wake_in_min, WAKE_MIN_GAP=10, WAKE_MAX_GAP=180)
```
模型可赖床，但最多 3 小时。上限不是为省钱，是防"睡过去"导致日志断档超过半天、连续性断了观察就断。

---

## 7. 大脑层

### 7.1 单次调用的上下文顺序（当前，作用从弱到强）
```
[1] system   : persona（约 450 token）
[2] system   : 输出契约（JSON schema + 示例）
[3] user     : 时间锚「现在 …，距上次醒来 N 分钟」
[4] user     : 【你是谁】core + 【上次自省时间】
[5] user     : 【你认识的人】relation
[6] user     : 【最近在做的事】最近 N 次唤醒事实 + 事后想法
[7] user     : 【小帕】仅"有未读来信"那轮渲染、且渲染即 seen
[8] user     : 【想起来的事】检索到的情景记忆（top 5，带"x小时前"）
[9] user     : 【你现在能做的事】工具清单
[10] user    : 【世界】inbox 未读数与前几条标题（只给标题不给全文）
```
> 注意：**不再有 v0.2 第 4 项的"身体感受 felt_text"**（状态层已删）。连续性的全部主观质感现在来自 [4][6][8] 三块记忆。

### 7.2 persona（当前 `prompt.PERSONA`）
刻意留白「你怎么看自己」「你想要什么」——这些应由她在 core 记忆里长出来。第一版写满，后面就没得观察。

### 7.3 输出契约（两段式，当前）
第一段只决定"做什么"：
```json
{ "action": "tool" | "none", "tool": "... | null",
  "args": {}, "why": "第一人称 ≤40 字（空转也要写）",
  "next_wake_in_min": 10–180 }
```
第二段（做完看到结果后）自评：
```json
{ "memory": "1–2 句你以后会记得的版本", "next_wake_in_min": 10–180（可选） }
```
`memory` 只存在于第二段（I1 从"校验后清空"变成"根本不存在"）。判据落在"结果有没有可消化内容"而非"选了哪个工具"（`scheduler.wake:164`），所以写日记/send_message/self_review 这类"反思即动作"的工具被排除出第二段，避免"对反思再做反思"。

### 7.4 解析与降级（`brain.decide:144`）
```
第 1 次：temperature=1.0，json_object 模式 → 失败
第 2 次：temperature=0.3，把上次坏输出贴回去让她修 → 再失败
强制空转：kind=error, action=none，不写记忆（等价于 I2 等价纪律）
```
**绝不因为解析失败就"按上次选择继续"**——那会造出卡在循环里的东西。

### 7.5 自定唤醒是核心
`next_wake_in_min` 是主线（决策 2：先只做自主循环）。观察指标"其分布"应单独看：若永远 10 或永远 180，说明自主性没真正运行（§10④）。

---

## 8. 工具层（当前，7 个工具）

```python
ALL_TOOLS = [
    Tool("read_feed",    "读一条还没读过的东西",        _run_read_feed),
    Tool("write_journal","写一段日记",                  _run_write_journal),
    Tool("recall",       "检索自己的记忆或日记",        _run_recall),
    Tool("web_search",   "联网搜索",                    _run_web_search),
    Tool("send_message", "给小帕发一条消息",            _run_send_message),
    Tool("self_review",  "改动一次【你是谁】/【你认识的人】", _run_self_review),
    Tool("idle",         "空转一轮，休息（叙事标记）",   _run_idle),
]
```

**`summary` / `detail` 必须分开**（`ToolResult`）：`detail` 可能是 3000 字全文，直接进 prompt 会炸；`summary` 控制百字内，是她"读完记住的那点东西"。这条纪律比任何调度优化都重要（§9）。

**可见性**：状态层移除后不再有"精力低隐藏工具"，`visible_tools()` 永远返回全部 7 个（`tools/__init__.py:313`）。契约枚举从 `ALL_TOOLS` 自动生成，加工具不用手改契约。

---

## 9. 成本：不是约束

与 v0.2 §9 估算基本一致（状态层移除不显著改变单次 token）。要点：

- 一次唤醒 ≈ 729 输入 / 257 输出（初期记忆空，稳态上限 ≈ 2950 输入）；第二段 +约 1500 输入 / 200 输出。
- 月成本 ≈ ¥5–15（DeepSeek 量级），任何为省 token 的设计妥协都亏。
- **v0.3 新增的潜在成本**：M4 本地 embedding 走本机 GPU，不增 API 账单；M3 自省摘要复用既有 self_review 调用，不增次数。**记忆改进基本不抬高 API 成本。**

---

## 10. 观测层（v0.3 版，记忆健康五指标）

### 10.1 每次 wake 落库字段
见 §3.1 `events`。`why` 原样保存——它是第三层"真相"的原材料，清洗了就没了。

### 10.2 一周后该看的五个数（记忆视角，替换失效的心情互信息）

**① heartbeat / event 比例** — 沿用。健康 30%–70%。低于 30% 太闲（世界输入少，在饿着）；高于 70% 无聊推太猛、为动而动。

**② 记忆写入 / 调用比** — *新增*。
```
ratio = (episodic 日均新增) / (recall 日均调用)
```
`<1` = 囤积（写得多想得少，记忆在堆积）；`>3` = 空转（想得多写得少）。健康区间约 1–3。

**③ recall 命中率** — *新增，M2/M4 的目标*。
```
hit_rate = recall_ok / recall_total
```
低 = 记忆难找（M2 自动标签、M4 语义检索的直接目标）。

**④ 执念长尾** — 沿用 `recall_count` 分布。哪些记忆被反复调取 = 鱼鱼的执念；M1 后这条长尾应更陡峭（复用强化生效）。

**⑤ 自省演化** — 沿用并强化。core 的**保留率**（每次自省 `>0.6` 才接受）与**长度趋势**（软目标 600 / 硬上限 1200）。若 30 天后 core 与第一天几乎一样 → 只是复读机；若剧烈跳变 → 漂移过快（闸门在挡）。M3 后还应看"摘要覆盖了多少老 episodic"。

### 10.3 验收标准
连续跑满 72 小时不崩，且 ① 落在区间内；`parse_ok=0` 比例 < 5%。崩了不要紧，日志在。

---

## 11. 改造顺序（v0.3 记忆工作包）


---

## 12. 本版明确不做 / 路线图

| 项 | 原因 | 什么时候做 |
|---|---|---|
| 被动唤醒（OneBot） | 决策 2 延伸 | v0.3+，与记忆层解耦，可独立排期 |
| 看视频 / 玩游戏 | 本地抽帧+多模态重 | 等 `summary/detail` 纪律跑稳 |
| 前端"窝"可视化 | 先有数据才有价值 | v0.4 |
| 向量检索（通用云服务） | 4000 条内不需要 |  |

---

## 13. 决策记录（截至 2026-11-12 拍板项）

1. **语言：Python 3.11 + asyncio。** 小帕借项目学 Python，精髓/不显然片段顺手加初学者注释（asyncio 调度、单飞锁、记忆检索）。
2. **全走 DeepSeek 远端 API**，成本 ¥5–15/月，不为省钱牺牲设计。
3. **只做自主/小帕唤醒循环**，OneBot 推后
4. **世界输入源（RSS）暂留空**，由小帕填；空库预期鱼鱼高频写日记、反复 recall，本身即初始观察素材。
5. **self_review 频率 = 每 10 次 wake**（原 20，初期让自我叙事快长出来，一周后评估）。
6. **（2026-11-12 新增）状态层整体删除**，记忆层升格为唯一支柱，v0.3 主线 = 改进记忆层
7. **分工红线**：凡注入鱼鱼的自然语言（persona / 契约 / 工具 desc / 标签措辞 / 自省与 M2-B/M3 的指令）一律由小帕定稿，Kite 只提"这段要改"的位置与理由，不代笔。
