# ARCHITECTURE.md — 鱼鱼架构说明（大白话版）

> 给"想知道这摊东西怎么转起来"的人看。设计基线见 `Design_v0.3.md`（记忆层为重心）；本文不复制它的论证，只讲**有什么、在哪、改它会碰坏什么**。
> **维护约定：调整项目结构、加删模块、改数据表时，必须同步更新本文件**（收工仪式的一部分）。

## 一句话总览

鱼鱼 = 一个死循环程序。它每隔一段时间"醒来"一次，把"自己是谁 + 最近干了啥 + 世界里有什么"拼成一段文字发给 DeepSeek，模型回一个 JSON 说"我要做什么"，系统替她做掉，把结果再发回去问"感觉怎么样"，最后把这一切记进 SQLite。如此往复。

## 全景图

```
                ┌────────────────────────────────────────────┐
                │  main.py  python -m fish.main 启动           │
                │  同时跑两个死循环（asyncio.gather）：          │
                └────────────┬──────────────────┬────────────┘
                             │                  │
              ① scheduler.run（作息表）   ② feed.feed_loop（订报纸）
              每 60 秒看一次钟：          每 2 小时抓一次 RSS，
              - 到该醒的时间了？           把新文章塞进 inbox 表
                       │
                   该醒了 → wake()（一次完整的"醒来"，加锁：同时只有一个）
                       │
        ┌──────────────┼──────────────────────────────┐
        ▼              ▼                              ▼
   prompt.build_messages           brain.decide
   组装发给模型的全部上下文          调 API，解析 JSON 回复
        │              │                              │
        │              ▼                              ▼
        │      模型说"我要调 XX 工具"  →  tools.dispatch() 执行
        │              │
        │              ▼ 工具产出了正文（detail）？
        │      是 → brain.react() 第二段调用："看完感觉怎么样"
        │          （记忆产生在这一段）
        │      否 → 不花钱，直接用第一段的自评
        │
        ▼
   记账收尾：events 表写一行（动作 + 下次醒的时间 + 留给下一轮的提示），
   第二段的"事后想法"存进 memories 表，
   工作记忆在这里结算（写新 / 清空 / 扣一轮，3 轮自动删），
   每 10 次 wake 做一次自省（重写"你是谁"，有保留率闸门）。
   自省是第三条独立的上下文组装路径：旧 core + 旧 relation + 全量往来
   +【上次自省以来她自己写下的东西】+ 时间锚点（工作记忆不进这里）。
```

## 模块清单（fish/ 目录）

| 文件 | 大白话是什么 | 改它要小心什么 |
|---|---|---|
| `main.py` | 入口，34 行。先抢单实例锁（同机只许一条鱼），再启动两个循环 | 基本不用动 |
| `lock.py` | **单实例锁**：对 `data/fish.lock` 上 OS 级字节锁，第二个进程启动直接被拒（不分 DRY_RUN/真实）。进程一死锁自动消失，没有残留死锁。顺便往 `logs/startup.log` 记每次启动尝试（START/BLOCKED，带时间戳） | 锁的获取必须在碰数据库之前（main 第一行）；fd 永远不要主动 close |
| `config.py` | **所有数字都住这里**，每个数字注明了理由和 Design 出处 | 改数字 = 改鱼鱼的性格/作息，先想清楚；别把密钥写进来 |
| `db.py` | 建表、开库、旧库补列（migration） | 改表结构必须同时改 `_migrate`，否则旧库直接报"no such column" |
| `memory.py` | 记忆的存取和打分检索（不用向量库，土办法：两字组匹配+时间衰减）；外加**工作记忆单条槽**的读写与 TTL 结算、以及自省的增量素材查询 | 改打分公式会影响"她想起点什么"，有专门测试；`settle_working_note` 的三态（None/空串/文本）不能混；`her_own_writings` 靠 `tags` 排除「小帕」类记录，别随手改成全量 episodic |
| `prompt.py` | 把上下文拼成发给模型的文字。三份合同：CONTRACT（第一段）、REACTION_CONTRACT（第二段）、REVIEW_CONTRACT（自省）；另有自省专用的 `build_review_messages` | **措辞就是她的世界观**。改一个词（如"记下"→"心里想"）都会改变她的行为，改完看对应测试。自省那三条新标签在 `config.py`，属小帕定稿范围 |
| `brain.py` | 调 API、解析 JSON、解析失败重试一次再降级。模型说什么不算数，解析通过才算；`run_self_review` 是定时与工具两条路径共用的执行体 | 新增字段要过 `_clamp_decision` 的钳制，别让模型原始输出直接生效；`note_for_next` 还要过 `normalize_note` 与 `resolve_note`；自省那次 `_call_api` 必须传 `kind="review"`（曾漏传，7 次自省全被标成 decision） |
| `scheduler.py` | 作息表：tick 时钟、唤醒判断、单飞锁、自省闸门。`wake()` 是全项目的主动脉 | 唤醒判断改坏 = 她闷睡 4 小时或疯狂醒来；`wake()` 末尾每轮恰好结算一次工作记忆，别搬到 `build_messages`（那边是「读」） |
| `tools/__init__.py` | 鱼鱼能干的 7 件事：read_feed / write_journal / recall / web_search / idle（主动休息，纯叙事标记）。加新工具在这登记 ALL_TOOLS | summary ≤ 百字的纪律别破；新工具返回 detail 就自动进第二段，不用改调度 |
| `feed.py` | 抓 RSS（含代理三档降级）、解析、入库。排序是"各源轮转" | `unread_reading_order` 同时供【世界】编号和 read_feed 用，两处必须同序 |
| `websearch.py` | DeepSeek 官方搜索/抓全文 API 的薄封装（标准库 urllib，不引第三方） | 计费接口。测试里全用假函数替换，别在测试里真调 |
| `trace.py` | 每次 API 调用的完整原文（含思考过程），存 `logs/traces/日期.jsonl`。`python -m fish.trace 3` 看 | 追加日志，随便动；但查看器里 `review` 必须和 decision/reaction 一样走「打印完整 messages」分支，否则自省看过什么查不出来 |
| `util.py` | 零碎：HTML 剥文本（plain）、时间戳 | `plain` 喂给 RSS 入库和阅读两头，别只改一头 |

## 数据都在哪

**一个 SQLite 库**（`data/fish.db`，不进 git）+ 一堆日志文件（`logs/`，不进 git）：

| 表 | 是什么 | 关键字段 |
|---|---|---|
| `events` | 观察层：一次唤醒一行，流水账 | kind（event/heartbeat/error/self_review）、next_wake_at（闹钟）、trace_id、`note_for_next`（本轮留给下一轮的提示，三态，append 历史） |
| `memories` | 她的记忆（发生过的事） | layer=episodic（流水记忆，大量）/ core（自我叙事，单条覆盖）/ relation；`source_event_id` 把"事后想法"挂回那次唤醒 |
| `working_note` | **工作记忆**（接下来打算做什么）：永远只有 id=1 一行 | content、remaining_wakes（每轮 -1，归零删）、born_event_id。刻意不进 `memories`：不参与检索/打分/自省 |
| `inbox` | 世界输入：RSS 文章 | consumed=0 是没读过的；body 入库时已是干净纯文本 |
| `logs/traces/*.jsonl` | 原料层：每次 API 调用的完整收发，想看"她到底读到/想到了什么"查这里 | 用 trace_id 和 events 表对上号 |

## 三条不变量在哪里执行（违反 = bug）

- **I1** heartbeat 不写记忆 → 执行点在 `scheduler.wake()` 末尾：`if kind == "event" and memory_text` 才写。（M0 补注：管的是 `memories` 表；`working_note` 存的是意图不是记忆，空转轮也允许留提示。）
- **I2** （状态层 2026-11-12 移除后退役）原意"模型输出不直接成为状态值"；现等价纪律为"模型原始输出须经解析校验（parse_ok）才往下传，解析失败绝不沿用上次结果"（见 `brain._parse_*`）。
- **I3** 同一时刻只有一条鱼、只有一个 wake → 两层：**进程级**在 `lock.py`（`data/fish.lock` 的 OS 锁，main 第一行抢，第二个进程直接拒启）；**进程内**在 `scheduler.run` 的 `wake_lock`（asyncio.Lock）。

## 改动影响速查（最常撞见的几种）

| 你改了什么 | 连带发生什么 |
|---|---|
| `config.py` 里的数字 | 只改这一个文件就行，但 config 的数字大多有配套测试，改完跑 pytest |
| `prompt.py` 的文案 | 她的行为会变（这是特性不是 bug）；如果改动和某条测试锁定的措辞有关，测试会红——判断是"代码错"还是"规格变了" |
| 加新工具 | `tools/__init__.py` 的 ALL_TOOLS 加一行 + 写 `_run_xxx` + 在 `Tool.args_hint` 的 hints 里补参数说明；契约枚举是自动生成的，不用手改；返回 detail 就自动进第二段 |
| `db.py` 加列 | SCHEMA 和 `_migrate` 两处都要改，且加一个迁移测试（照 `test_migration_adds_link_column` 抄） |
| 工作记忆三态 / TTL | `tests/test_working_note.py` + 全部 | 「轮」= 一次 `wake()`，别用 `db.wake_count`（它把自省旁支也算进去）；DRY_RUN 必须零副作用 |
| 自省输入 / 增量素材 | `tests/test_review_context.py` + `test_self_review_cap.py` + `test_self_review_tool.py` | 增量以「上次成功自省」为界（error 事件不算）；工作记忆提示**不许**进自省；trace `kind` 必须是 `review`；改 `review_narrative` 签名要同步三处测试 fake |
| 改工具排序/编号逻辑 | 【世界】的 [n] 和 read_feed 的 index 是同一个顺序，动一个等于动两个 |

## 日常观察命令（看，不花钱）

```bash
# 最近 20 次唤醒干了啥
sqlite3 data/fish.db "SELECT ts, kind, tool, why FROM events ORDER BY id DESC LIMIT 20"

# 记忆层观察（状态层已移除，不再有心情数值；看她"记住了什么"）
sqlite3 data/fish.db "SELECT ts, layer, substr(content,1,40) FROM memories ORDER BY id DESC LIMIT 20"

# 工作记忆：她给自己留过什么话，下轮有没有照做
sqlite3 data/fish.db "SELECT ts, tool, why, note_for_next FROM events WHERE note_for_next IS NOT NULL ORDER BY id DESC LIMIT 20"

# 当前还有效的一条（remaining_wakes 归零会被自动删）
sqlite3 data/fish.db "SELECT * FROM working_note"

# 最近一次调用的完整输入输出和思考过程（最有用）
python -m fish.trace 3

# 自省那次实际看见了什么：kind=review 的完整 messages 原文
python -m fish.trace 20 | grep -A 200 '自省'
```
