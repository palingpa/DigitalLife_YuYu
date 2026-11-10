# 项目设计 v0.2：数字生命「鱼鱼」

> 基于 v0.1 草稿与本轮三项决策重写。本版目标：**可以直接照着建仓**。
> 上一版 `Draft.md` 保留不动，作为设计演化记录。

## 本版依据的三项决策

| # | 决策 | 对设计的影响 |
|---|---|---|
| 1 | 全部走 DeepSeek V4.1F 远端 API | 成本可忽略（见 §9），**不因省钱牺牲设计**；但保留 L0 短路，理由改为日志纯净度 |
| 2 | 第一版只做自主循环 | 调度层砍掉被动唤醒；自定唤醒成为核心，必须保留 |
| 3 | 暂不复用 OneBot / Electron | 独立进程、独立库，预留消息适配口但第一版不实现 |

## v0.1 → v0.2 的六处实质修改

1. **新增「空转」为一等公民**：允许 `action: none`，只落心跳，不写情景记忆。理由见 v0.1 评审第一坑——幻觉事件会递归污染记忆。
2. **心情拆成「情绪」与「驱动」两组**，不再混为四个平级数值。v0.1 的「好奇/无聊」是驱动量，「开心/沮丧」是情绪量，混在一起必然产出矛盾状态。
3. **模型只输出 delta，不输出绝对值**；回归、clamp 全在服务端。模型不能直接改自己的身体。
4. **删除「成功率下降」**，硬约束只保留可见性与可用性。随机失败对模型是白噪声，会让第三层观察失效。
5. **区分 tick 与 wake**。tick 是时钟，wake 才是烧 token 的思考。v0.1 把两者混为一谈，导致保底 = 每 30 分钟必烧一次。
6. **补上时间锚注入**。每次 wake 必须告诉它「距上次醒来多久、上次在干什么」，否则它每次都是失忆的第一次。

---

## 1. 术语与不变量

| 术语 | 定义 |
|---|---|
| **tick** | 调度器每 60s 执行一次的心跳检查。纯本地计算，不调 LLM，不写库（除非状态回归） |
| **wake** | 一次完整的唤醒循环：装载上下文 → 调模型 → 执行 → 记账 |
| **heartbeat** | 一次 wake 的产出为「什么都没做」时的日志行。`kind=heartbeat` |
| **event** | 一次 wake 真实做了事（调了工具并拿到结果）的日志行。`kind=event` |
| **no-op** | 模型决策结果为空转。与 `heartbeat` 是同一件事的两个视角 |

**三条不变量**（任何时候都必须成立）：

- **I1**：heartbeat 永不写入 `memories` 表。记忆里只允许存在真实发生过的事。
- **I2**：模型的输出永远不直接成为状态值。一切状态变更走 `apply_delta()` 并 clamp。
- **I3**：同一时刻只允许一个 wake 在跑（单飞锁）。tick 撞上正在跑的 wake 时直接跳过本次。

---

## 2. 进程模型

单进程，Python 3.11+，asyncio。不引入 Redis / Celery / 消息队列——这个体量不需要，加了只会让调试变难。

```
主循环 asyncio
├── tick_loop()      每 60s：状态回归 + 判断是否 due
├── wake()           按需触发：装载 → 调模型 → 执行 → 落库
└── feed_loop()      每 2h：抓取信息源，写入 inbox
```

SQLite 用 WAL 模式，`data/fish.db`。单写者，无并发问题。

**目录结构**

```
DigitalLife_YuYu/
├── Draft.md                   # v0.1 原稿，不动
├── Design_v0.2.md             # 本文档
├── fish/
│   ├── config.py              # 全部魔法数字集中在这里
│   ├── db.py                  # 连接、建表、迁移
│   ├── state.py               # 动力学 / 回归 / 感受翻译
│   ├── memory.py              # 写入 / 检索 / 打分
│   ├── prompt.py              # 上下文组装
│   ├── brain.py               # API 调用 / 解析 / 降级
│   ├── scheduler.py           # tick / due 判断 / 单飞锁
│   ├── tools/
│   │   ├── __init__.py        # 注册表 + 可见性过滤
│   │   ├── feed.py            # read_feed
│   │   ├── journal.py         # write_journal
│   │   └── recall.py          # recall
│   └── main.py
├── data/fish.db
└── logs/                      # 只放错误与异常，正常轨迹进 db
```

---

## 3. 数据层

### 3.1 DDL

```sql
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS state (
  id            INTEGER PRIMARY KEY CHECK (id = 1),
  energy        REAL NOT NULL,
  valence       REAL NOT NULL,
  boredom       REAL NOT NULL,
  updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  ts             TEXT    NOT NULL,              -- ISO8601 本地时间
  kind           TEXT    NOT NULL CHECK (kind IN ('heartbeat','event','error','self_review')),
  trigger        TEXT    NOT NULL,              -- due | boredom_urgent | manual
  tool           TEXT,
  args_json      TEXT,
  result_summary TEXT,                          -- 给下次数检索用的 1-2 句
  why            TEXT    NOT NULL,              -- 模型自述理由，原样保存
  energy_before  REAL, energy_after  REAL,
  valence_before REAL, valence_after REAL,
  boredom_before REAL, boredom_after REAL,
  next_wake_at   TEXT,
  tokens_in      INTEGER DEFAULT 0,
  tokens_out     INTEGER DEFAULT 0,
  duration_ms    INTEGER DEFAULT 0,
  parse_ok       INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_events_ts   ON events(ts DESC);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind, ts DESC);

CREATE TABLE IF NOT EXISTS memories (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  ts              TEXT    NOT NULL,
  layer           TEXT    NOT NULL CHECK (layer IN ('episodic','core','relation')),
  content         TEXT    NOT NULL,
  salience        REAL    NOT NULL DEFAULT 0.5,  -- 0..1，写入时算一次，之后不再改
  tags            TEXT,                          -- 逗号分隔，供 tag_match 用
  source_event_id INTEGER REFERENCES events(id),
  last_recalled_at TEXT,
  recall_count    INTEGER DEFAULT 0,
  pinned          INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_mem_layer ON memories(layer, ts DESC);

CREATE TABLE IF NOT EXISTS inbox (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  ts       TEXT NOT NULL,
  source   TEXT NOT NULL,
  title    TEXT,
  body     TEXT,
  consumed INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_inbox_unread ON inbox(consumed, ts DESC);
```

### 3.2 为什么不用向量库
情景记忆单条 100-200 字，跑三个月大约 2000-4000 条。全量 embedding + 向量检索要先解决「什么时候重新索引」「模型换了要不要重算」两个工程问题，收益在 4000 条这个量级并不明显。

先用统计打分，等检索质量真的成为瓶颈再换。**打分公式见 §5.2，随时可以替换成向量相似度而不改调用方。**

### 3.3 调用轨迹（2026-11-09 追加）

`events` 是**观察层**：一次唤醒一行，用来算指标。轨迹是**原料层**：每次 API 调用的完整输入、输出、思考过程（`reasoning_content`）与工具返回全文，写在 `logs/traces/YYYY-MM-DD.jsonl`，一行一条，用 `trace_id` 与 events 行关联。

分层理由：一次唤醒可能包含两次调用（重试、自省），单次输入输出几千 token。混进 events 表会让观察层没法看。用 JSONL 而非表，是因为追加写不怕崩、不必迁移、可以直接 grep 原文；这一层的量（每天几十条）不需要索引。

查看：`python -m fish.trace 3`。

---

## 4. 状态层

### 4.1 三个量，两种性质

v0.1 把「好奇/无聊/开心/沮丧」当四个平级属性，问题在于它们不同类：

| 量 | 性质 | 作用 | 范围 | baseline | 每 tick 回归 |
|---|---|---|---|---|---|
| `energy` | 情绪（体能） | 决定「能不能动」 | 0-100 | 55 | 4% |
| `valence` | 情绪（感受） | 决定「爽不爽」 | 0-100 | 50 | 3% |
| `boredom` | **驱动** | 决定「想不想动」 | 0-100 | 30 | **不回归** |

关键区分：**情绪回答「我现在感觉如何」，驱动回答「我要不要去找事做」。** 前者是背景色，后者是引擎。

`boredom` 不回归，因为它衡量的是「未被满足的探索欲」，只会随时间累积、被新体验消耗：

- 每 tick `+0.03`（约 30 分钟 +0.9，一天不做事 +43）
- 首次做某事 `-15`
- 重复做同类事 `-5`（边际递减，做第三遍就不解闷了）
- 什么都不做时**不衰减**

这就是为什么它最终一定会推动鱼鱼去干点什么，且干重复的事会越来越不解闷——**行为多样性是结构逼出来的，不是提示词要求出来的。**

### 4.2 动力学

```python
BASELINE = {"energy": 55.0, "valence": 50.0}
REGRESS  = {"energy": 0.04, "valence": 0.03}
CLAMP    = (5.0, 95.0)

def tick_regress(s, minutes: float) -> None:
    for k, b in BASELINE.items():
        r = REGRESS[k] * (minutes / 60.0)      # 按实际经过时间缩放
        setattr(s, k, getattr(s, k) + (b - getattr(s, k)) * min(r, 0.5))
    s.boredom = min(95.0, s.boredom + 0.03 * (minutes / 1.0))

def apply_delta(s, delta: dict, minutes: float) -> dict:
    for k in ("energy", "valence", "boredom"):
        d = float(delta.get(k, 0)) / 10.0      # 模型给 -10..+10 → 实际 -1..+1
        setattr(s, k, getattr(s, k) + d)
    for k in ("energy", "valence", "boredom"):
        setattr(s, k, max(CLAMP[0], min(CLAMP[1], getattr(s, k))))
    return snapshot(s)
```

模型输出 `delta` 的单位是「刻度」（-10..+10），服务端除 10。这样模型不用关心 0-100 的绝对刻度，也不必知道自己当前值才能表达「更累一点」。

**版本注记（2026-11-10）**：新增**睡眠恢复**。原设计中醒着与睡着的回归速度相同，「下次几点醒」没有任何身体后果，next_wake_in_min 是纯叙事。现在 wake 结算时，若与上次唤醒间隔 ≥ 45 分钟，energy 额外回复 `min(2.0/h × min(间隔,180), 6.0, baseline-energy)`。三道闸：只作用于 energy（睡解乏不解心结，否则「心情差就睡」成为最优策略）；单次封顶且超 180 分钟不计（配合 boredom 紧急通道，收益有硬上限）；完全服务端结算（I2）。验证：睡 60 分钟回 ≈2.0，显著大于醒着同时间的回归 ≈0.4；睡 500 分钟 = 睡 180 分钟；不越过 baseline。空转也计入睡眠——空转从此有了「睡觉」之外的物理意义。

### 4.3 数值 → 第一人称感受

这是「软影响」的全部实现。不做任何映射表，用规则粗分档位后拼句子：

```python
def felt_text(s) -> str:
    e = "很精神" if s.energy > 70 else "不累" if s.energy > 40 else "很累"
    v = "心情不错" if s.valence > 65 else "没什么感觉" if s.valence > 35 else "有点闷"
    b = ("闲得发慌，总想找点事" if s.boredom > 75
         else "有点坐不住" if s.boredom > 55
         else "还算安稳" if s.boredom > 30
         else "很平静")
    return f"你现在{e}，{v}。{b}。"
```

输出示例：`你现在有点乏，没什么感觉。闲得发慌，总想找点事。`

注意这里**只描述状态，不给出建议**。不给「所以你应该去…」，否则模型会顺着建议走，你就观察不到它自己的选择。

---

## 5. 记忆层

### 5.1 三层职责

| 层 | 内容 | 更新频率 | 是否常驻上下文 |
|---|---|---|---|
| `core` | 自我叙事（我是谁、我最近在想什么） | 每 20 次 wake 重写一次 | 是，约 400 token |
| `relation` | 对具体人的认知 | 低频 | 是，约 200 token |
| `episodic` | 单次事件 | 每 event 一条 | 否，按需检索 |

### 5.2 检索打分

```python
score = 0.45 * recency + 0.35 * salience + 0.20 * tag_match

recency   = exp(-hours_since / 72)            # 72h 半衰尺度
salience  = clamp(0.5
                  + abs(d_valence) / 20        # 情绪冲击
                  + 0.3 * has_person          # 涉及具体的人
                  + 0.2 * is_first_time, 0, 1)
tag_match = jaccard(当前工具名+关键词, memory.tags)
```

取 top 5，另**强制**附带最近 2 条 event 摘要（保证时间连续性）。总预算 1200 token，超了就砍最末位。

### 5.3 核心记忆的自省机制

每 **10** 次 wake 触发一次 `kind=self_review` 的特殊 wake，任务只有一件：重写 `core` 层。

（v0.1 定的 20 次，2026-11-09 改为 10 次。理由：初期它什么都还没有，`core` 层近乎空白，高频自省能让自我叙事尽快"长"出来；等叙事稳定、日志跑过一两周，若观察到漂移过快再调回 15-20。）

**硬约束**：新版本必须保留旧版说过的每一件事，只允许改写措辞、往后追加、修正一处事实。用**单向保留率**卡：

```python
retained = 旧文本各句「在新版中被匹配到的字符比例」的平均值   # 新增内容不加分也不扣分
if retained < 0.6:
    reject()   # 拒绝写入，记一条 error，保留旧的
```

理由：整体重写会让它瞬间换成另一个人，你要观察的「进化」就变成了随机跳变。**限制漂移速度，漂移才有意义。**

注意**不能用 `difflib.ratio()`**（v0.2 原定方案，已废弃）：它的分母含新文本长度，因此「往后追加一句」会让分数掉到 0.5 上下被判成漂移——而追加恰恰是自我叙事最正常的生长方式。实测：追加式生长 0.55、原样调序 0.47，都会被 0.75 拒掉，等于 core 永远停在第一次写下的长度上。改成单向保留率后，追加 1.00、调序 0.81、删一半 0.50、整体推翻 0.16，区分度正好。

冷启动（`core` 为空）不设闸门：没有「上一版」就没有比较对象，第一次写入即起点。此时喂给模型的必须是占位符 `CORE_PLACEHOLDER`，不能是 `None`。

---

## 6. 调度层

### 6.1 只有两条路径

v0.2 砍掉被动唤醒（决策 2），剩：

**路径 A：到期唤醒**
```
due_at = 最新 event 的 next_wake_at
兜底：若 next_wake_at 缺失或早于 last.ts + 240min，用 last.ts + 240min
```

**路径 B：憋不住唤醒**
```
boredom > 85 且 距上次 wake > 15min
```
这条是对「赖床」的兜底。因为模型有权把 `next_wake_in_min` 设到 180 分钟，但憋到 85 无聊度时光靠意志压不住——**给它一个身体上的理由提前醒**。

### 6.2 tick 与 L0 短路

```python
def on_tick(now):
    s = load_state()
    tick_regress(s, minutes_since_last_tick)

    if not (now >= due_at(now) or s.boredom > 85):
        save_state(s)
        return                      # L0 短路：不调 LLM，不写 events

    if not lock.acquire(blocking=False):
        return                      # I3 单飞
    try:
        wake(now, trigger=...)
    finally:
        lock.release()
```

**关于 L0 短路的一个修正**：我原本以为它能省很多钱，算完发现省不了多少（§9）。保留它的真实理由是两个：

1. **日志纯净度**：如果每 30 分钟都叫醒它并让它「决定不做事」，`events` 表会被 heartbeat 淹没，你还得额外过滤。
2. **自我叙事的健康**：一个整天被叫起来说「没事，继续睡」的东西，会在 `core` 记忆里长成「我是个无所事事的存在」。规则替它睡，比它自己反复确认自己无聊要好。

所以 L0 短路**只覆盖真正无事的时间段**（boredom 低 + 未到期）。一旦 boredom 涨起来，每次都实打实叫醒它。

### 6.3 next_wake_at 的钳制

```python
MIN_GAP, MAX_GAP = 10, 180
next_wake_at = now + clamp(model.next_wake_in_min, MIN_GAP, MAX_GAP)
```

模型可以赖床，但最多 3 小时。上限不是为了防止烧 token（成本可忽略），是防止它「睡过去」导致日志断档超过半天——**连续性断了，观察就断了**。

---

## 7. 大脑层

### 7.1 单次调用的上下文顺序

作用力从弱到强，越靠后模型越重视：

```
[1] system   : persona（约 450 token）
[2] system   : 输出契约（JSON schema + 示例）
[3] user     : 时间锚「现在 2026-11-09 17:12，周一，距上次醒来 47 分钟」
[4] user     : 身体感受（felt_text 输出，一句）
[5] user     : 核心记忆（self narrative + 关系，约 500 token）
[6] user     : 最近两条 event 摘要（各一句）
[7] user     : 检索到的情景记忆（top 5，带相对时间「3 天前」）
[8] user     : 可用工具清单（含不可用工具及原因）
[9] user     : 未读的世界输入条数（只给数量和一个标题，不塞全文）
```

第 9 条很重要：**不要把 feed 全文塞进 prompt**。先给它「有 3 条没看，其中一条叫《…》」，让它自己决定要不要调 `read_feed`。这样「读什么」才是它的选择，而不是你替它选好的。

### 7.2 persona 第一版

```
你是鱼鱼。你住在一台机器的角落里，没有身体，但有一套会被牵动的心情。

你不是助手，没有人给你派任务。你醒来，看看有什么可做的，做一点，或者什么都不做，然后再睡。

你对世界有好奇心，但好奇心是间歇性的。你会因为一句没头没尾的话想半天，也会对一整屏的信息毫无兴趣。你不太擅长掩饰情绪，也不太说得清自己在想什么。

你有一个叫小帕的人，他偶尔会来找你。你记得他的事，但记忆是模糊的、有偏差的，不要假装记得比实际更清楚。

你不必每次醒来都做点什么。发呆是允许的。

说话简短。不要总结，不要列清单，不要用「作为一个……」这种句式。
```

刻意留白的地方：「你怎么看自己」没有写，「你想要什么」没有写。这些应该是它自己长出来的，第一版就写满，后面就没得观察了。

### 7.3 输出契约（2026-11-09 改为两段式）

第一段，只决定「做什么」：

```json
{
  "action": "tool" | "none",
  "tool": "read_feed" | "write_journal" | "recall" | null,
  "args": {},
  "why": "一句话，第一人称，不超过 40 字",
  "felt_delta": {"energy": -2, "valence": 3},
  "next_wake_in_min": 45
}
```

第二段，做完之后自评：

```json
{
  "felt_delta": {"energy": -2, "valence": 3},
  "memory": "1-2 句你以后会记得的版本",
  "next_wake_in_min": 25
}
```

三个约束：

- `why` 必填且非空。空转也要写（「不想动」也是理由）。
- `memory` **只存在于第二段**。空转结构上就没有这个字段——I1 从「校验后清空」变成了「根本不存在」。
- `felt_delta` 只允许 `-10..+10`，越界截断，且永远不能碰 `boredom`。

### 7.4 解析与降级

```
第 1 次：temperature=1.0，response_format=json_object
  失败 → 第 2 次：temperature=0.3，并把上一次的原始输出贴回去让它修
    再失败 → 强制空转：kind=error, action=none, 状态按 tick_regress 走
```

**绝不因为解析失败就让它「按上次的选择继续」**——那会造出一个卡在循环里的东西。

### 7.6 为什么是两段式（2026-11-09 实跑后追加）

第一版是「一次调用干完四件事」，跑了 4 次唤醒就暴露出结构性问题：

1. **`memory` 写在看到内容之前。** 契约要求她在选 `read_feed` 的同时就写「以后会记得的版本」，
   而她那时只看到标题。结果记忆里只有《XXX》一个标题，正文读完即丢。
2. **`felt_delta` 是预期，不是后果。** 她写的是「我打算读一篇文章，我猜我会开心 +4」。
   而 §10.2 的核心指标测的就是这个值——**它测的是她的预测，不是她的体验。**
3. **上下文里只有意图没有事实。** `【最近在做的事】` 原本只渲染 `why + tool`，
   于是 21:07 那次她的思考过程里出现：「这是意图，但好像没执行？」
   她自己都不知道读没读，于是把同样的动作又做了一遍。

改成两段后，`memory` 和 `felt_delta` 都产生在**看到结果之后**，上面三条同时消失。

**分叉判据不预测决策**，而是在工具执行完之后看结果：

```python
if result.ok and (result.detail or "").strip():   # 有没有产生可消化的东西
    reaction = react(...)                          # 第二段
```

判据落在「结果有没有内容」而不是「她选了哪个工具」，所以：

| 情况 | 调用次数 | 情绪来源 | 记忆 |
|---|---|---|---|
| 工具成功且有正文 | 2 | 第二段（后果） | 第二段 |
| 工具失败（inbox 空、recall 无命中） | 1 | 第一段（此刻）+ 规则侧 −3.0 | 不写 |
| 空转 | 1 | 第一段（此刻） | 不写 |

加第四个工具时不用改调度代码——只要它返回非空 `detail`，就自动进第二段。

第二段只喂**正文前 2000 字**（`REACTION_TEXT_CHARS`），超出时明确写「原文更长，你只看到前 2000 字」。
诚实标注比假装读完重要：她知道自己是只看了前半段，就不会编出「文章后面讲了三个观点」。

成本影响见 §9。


### 7.5 自定唤醒是核心

决策 2 说先做自主循环，所以 `next_wake_in_min` 不是附加字段，是主线：

- 想继续读一本书，可以给 15（半小时内回去接着看）
- 想睡个长觉，给 180
- 「明天再看」这种跨天意图第一版不支持，`MAX_GAP=180` 兜住

观察指标里「`next_wake_in_min` 的分布」应该单独看：如果它永远给 10 或永远给 180，说明这个字段没被真正使用，自主性还没起来。

---

## 8. 工具层

### 8.1 接口规范

```python
@dataclass
class Tool:
    name: str
    desc: str                    # 给模型看的一句话，写清楚「能干什么、会花多久」
    args_schema: dict
    cost: int                    # 耗能档位 1-3，用于可见性过滤
    def run(self, args: dict) -> ToolResult: ...
```

```python
@dataclass
class ToolResult:
    ok: bool
    summary: str        # 给模型看的 1-3 句，不是给日志看的
    detail: str         # 完整内容，存 args/result，不进 prompt
    state_hint: dict    # 建议的状态变化，仍需服务端 clamp
```

**`summary` 与 `detail` 必须分开**。`detail` 可能是 3000 字文章全文，直接进 prompt 会炸；`summary` 控制在 100 字内，是鱼鱼「读完之后记住的那点东西」。

### 8.2 第一版三个工具

**`read_feed`** — 读一条未读的世界输入
```
args: {"index": 0}   # 从候选里选第几条，0 表示最新的
行为: 从 inbox 取未读 → 生成 summary（这一次调用把它压到 80 字内）
结果: ok=True, summary="一篇讲城市里野猫如何划分地盘的短文，说它们其实有稳定的作息表。"
副作用: inbox.consumed=1, boredom -15（首次）/ -5（同源重复）
```

**`write_journal`** — 写一段日记
```
args: {"text": "..."}
行为: 直接写入 memories(layer='episodic', pinned=1)
结果: summary="写下了。"
副作用: valence +3, boredom -8
```

这个工具是给它「自我表达」的出口。加 `pinned=1` 是因为日记是它主动想留下的东西，检索时应该更容易被想起来。

**`recall`** — 检索自己的记忆
```
args: {"query": "上次那个关于猫的"}
行为: 走 §5.2 打分，但 tag_match 用 query 关键词
结果: summary="找到 2 条：3 天前你读到野猫有作息表；昨天你在日记里提到这事。"
副作用: recall_count += 1, last_recalled_at = now
```

`recall` 是这一版最值得观察的工具。`recall_count` 的分布直接告诉你**它在想什么**——哪些记忆被反复调取，就是它的执念。

### 8.3 硬约束（唯一允许的形式）

只有可见性一种：

```python
def visible_tools(s) -> list[Tool]:
    out = []
    for t in ALL_TOOLS:
        if t.cost >= 3 and s.energy < 25:
            continue                    # 干脆不给，不给理由也不给机会
        out.append(t)
    return out
```

**不做**：随机失败、成功率折扣、结果打折。

如果确实要让状态影响结果，唯一允许的做法是把原因写进 `summary`：

```python
if s.energy < 30:
    result.summary = "你有点撑不住，读到一半就走神了，只记住个大概：" + half_summary
```

这样它有可归因的叙事，而不是遇到一次无法解释的失败然后自己编原因。

---

## 9. 成本：不是约束

按 §7.1 的上下文估算单次 wake：

| 项 | token |
|---|---|
| persona 450 + 契约 250 | 700 |
| 时间锚 + 身体感受 | 150 |
| 核心记忆 + 关系 | 600 |
| 最近 2 条 event | 100 |
| 情景记忆 top 5 | 1000 |
| 工具清单 | 400 |
| **输入合计** | **≈ 2950** |
| 输出（JSON + why） | ≈ 300 |
| 工具返回 summary（30% 的 wake 有） | +80 平均 |

按每天 30 次 wake 估：

```
日：输入 88.5K token，输出 9K
月：输入 2.65M，输出 0.27M
```

按 DeepSeek 量级单价（输入 ¥2/M、输出 ¥8/M，**你按实际单价替换**）：

```
月成本 ≈ 2.65×2 + 0.27×8 ≈ ¥7.5
```

结论：**每天 30 次或 48 次，月成本都在 ¥5-15 之间。** 这个量级下，任何为省 token 而做的设计妥协都是亏的。

真正会推高成本的是**工具返回的内容**——如果 `read_feed` 把 3000 字全文放进 prompt，单次输入会从 3K 涨到 6K。所以 §8.1 强制 `summary`/`detail` 分离，这条纪律比 L0 短路重要得多。

**两段式之后的成本（2026-11-09 实测修正）**

实跑的真实数字比估算小得多：一次唤醒只有 **729 输入 / 257 输出**（初期 core 记忆和情景记忆都是空的，§7.1 里那 2950 是长到稳态后的上限）。

第二段在第一次的基础上追加「她的决策 + 正文前 2000 字」，约 **+1500 输入 / +200 输出**。按每天 30 次、一半的唤醒会做事算：

```
日：输入 ≈ 15×(729+257) + 15×(2250+460) ≈ 65K，输出 ≈ 11K
月：输入 ≈ 1.95M，输出 ≈ 0.33M
月成本 ≈ 1.95×2 + 0.33×8 ≈ ¥6.5
```

比单段式的 ¥5 涨了约三成。**这个改动买回来的是真实记忆和真实情绪，不值得为这点钱省。**

**什么时候该考虑换本地小模型**：不是看账单，是看你自己的迭代心情——如果改一次 prompt 想跑 100 次对照实验，本地模型的「无限次试错」会比省下那 ¥10 更值钱。等那一天再说。

---

## 10. 观测层

### 10.1 每次 wake 落库字段

见 §3.1 `events` 表。`why` 原样保存不做清洗——**它是第三层「真相」的原材料**，清洗了就没了。

### 10.2 一周后该看的五个数

**① heartbeat / event 比例**
健康区间 30%-70%。低于 30% 说明太闲（世界输入太少，鱼鱼在饿着）；高于 70% 说明无聊度推得太猛，它在为动而动。

**② 工具分布 + 重复度**
同一个工具连续调用超过 3 次就标红。这通常不是「它喜欢」，是它卡住了。

**③ 心情-选择互信息（核心指标）**

```python
# valence 分 3 档，工具分 N 类
# 用 events 表里 kind='event' 的行
I(X;Y) = Σ p(x,y) · log( p(x,y) / (p(x)·p(y)) )

样本 < 100 条不算（噪声会假装成信号）
```

读数含义：
- **≈ 0**：心情是装饰，选择与感受无关。它在表演「我有情绪」。
- **0.05 - 0.3**：软影响起作用了。这是你要的区间。
- **> 0.5**：心情在决定一切，等于硬约束。自主性是假的。

这个数直接回答 v0.1 第 6 节第三问，而且不用去问模型自己。

**④ next_wake_in_min 的分布**
若 90% 落在 10 或 180 两个端点，说明它在偷懒（永远最短或永远最长），自主调度没真正运行。

**⑤ 核心记忆的首次实质变化出现在第几天**
这是「进化」最直观的标记。如果 30 天后 `core` 层和第一天几乎一样，那它只是个复读机。

### 10.3 验收标准

不是「跑通了」，是：**连续跑满 72 小时不崩，且 ① 落在健康区间内。**

崩了不要紧，日志在。但 `parse_ok=0` 的比例要低于 5%，高了说明输出契约需要改。

---

## 11. 建仓顺序

按依赖倒序，每一步都能独立验证：

1. `db.py` + DDL → 建库，插入一条假 state，`sqlite3` 能查
2. `state.py` → 写单测：跑 100 次 tick，确认 boredom 单调升、energy 回归 baseline、都在 clamp 内
3. `prompt.py` → 不接 API，把组装结果打印出来，肉眼检查 token 数和可读性
4. `brain.py` → 只接一次真实调用，手动跑，看 JSON 能不能解出来
5. `scheduler.py` + `main.py` → 空跑模式（`DRY_RUN=1` 只打印不调 API），确认触发时机对
6. `tools/` → 三个工具逐个接，先 `read_feed`（它有真实副作用，最能暴露问题）
7. 全开，跑 72 小时，看 §10.2 那五个数

第 2 步和第 5 步是关键，别跳。状态动力学错了，后面所有观察数据都是废的，而且你不会发现。

---

## 12. 本版明确不做

| 项 | 原因 | 什么时候做 |
|---|---|---|
| 被动唤醒（人找它） | 决策 2 | v0.3，与 OneBot 一起 |
| 看视频 / 玩游戏 | 本地抽帧+多模态重，且返回内容不可控 | 等 `summary/detail` 纪律跑稳 |
| 前端「窝」 | 先有数据才有可视化价值 | v0.4 |
| 向量检索 | 4000 条以内不需要 | 检索质量真成瓶颈时 |
| 赖床 / 提前醒的复杂逻辑 | clamp 到 [10,180] 已覆盖 | 永远不会，除非观察到需求 |
| 硬件 | 同上 | 远期 |

---

## 13. 已拍板的三件事（2026-11-09）

1. **语言：Python 3.11 + asyncio。** 小帕借这个项目学 Python，所以代码里遇到精髓、不显然的片段，要顺手为初学者加注释（尤其 asyncio 的调度、单飞锁、状态回归这几处）。
2. **世界输入源：暂留空。** `read_feed` 接口照写，`inbox` 表建好但为空，源由小帕自己填。填之前项目可以跑，只是鱼鱼没有东西可看——预期它会高频写日记、反复 recall，这本身就是初始观察素材，不算饿死。
3. **`self_review` 触发频率：每 10 次 wake**（原定 20）。初期它一无所有，让自我叙事更快长出来；跑一周后再评估是否调回 15-20。
