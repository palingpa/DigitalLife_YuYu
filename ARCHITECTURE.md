# ARCHITECTURE.md — 鱼鱼架构说明（大白话版）

> 给"想知道这摊东西怎么转起来"的人看。不复制 Design_v0.2 的论证，只讲**有什么、在哪、改它会碰坏什么**。
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
              - 还是无聊到憋不住了？
                       │
                   该醒了 → wake()（一次完整的"醒来"，加锁：同时只有一个）
                       │
        ┌──────────────┼──────────────────────────────┐
        ▼              ▼                              ▼
   睡眠回血结算    prompt.build_messages           brain.decide
   （state.py）   组装发给模型的全部上下文          调 API，解析 JSON 回复
        │              │                              │
        │              ▼                              ▼
        │      模型说"我要调 XX 工具"  →  tools.dispatch() 执行
        │              │
        │              ▼ 工具产出了正文（detail）？
        │      是 → brain.react() 第二段调用："看完感觉怎么样"
        │          （情绪 + 记忆都产生在这一段）
        │      否 → 不花钱，直接用第一段的自评
        │
        ▼
   记账收尾：events 表写一行（动作+前后状态+下次醒的时间），
   第二段的"事后想法"存进 memories 表，
   每 10 次 wake 做一次自省（重写"你是谁"，有保留率闸门），
   最后 state 存回库。
```

## 模块清单（fish/ 目录）

| 文件 | 大白话是什么 | 改它要小心什么 |
|---|---|---|
| `main.py` | 入口，34 行。启动两个循环 | 基本不用动 |
| `config.py` | **所有数字都住这里**，每个数字注明了理由和 Design 出处 | 改数字 = 改鱼鱼的性格/作息，先想清楚；别把密钥写进来 |
| `db.py` | 建表、开库、旧库补列（migration） | 改表结构必须同时改 `_migrate`，否则旧库直接报"no such column" |
| `state.py` | 鱼鱼的"身体"：三个数值怎么随时间变。**全项目最重要的 170 行** | 这里的错误是静默的——数据照记，全是废的。改完必跑 pytest |
| `memory.py` | 记忆的存取和打分检索（不用向量库，土办法：两字组匹配+时间衰减） | 改打分公式会影响"她想起点什么"，有专门测试 |
| `prompt.py` | 把上下文拼成发给模型的文字。三份合同：CONTRACT（第一段）、REACTION_CONTRACT（第二段）、REVIEW_CONTRACT（自省） | **措辞就是她的世界观**。改一个词（如"记下"→"心里想"）都会改变她的行为，改完看对应测试 |
| `brain.py` | 调 API、解析 JSON、解析失败重试一次再降级。模型说什么不算数，解析通过才算 | 新增字段要过 `_clamp_decision` 的钳制，别让模型原始输出直接生效 |
| `scheduler.py` | 作息表：tick 时钟、唤醒判断、单飞锁、自省闸门。`wake()` 是全项目的主动脉 | 唤醒判断改坏 = 她闷睡 4 小时或疯狂醒来 |
| `tools/__init__.py` | 鱼鱼能干的 4 件事：read_feed / write_journal / recall / web_search。加新工具在这登记 ALL_TOOLS | summary ≤ 百字的纪律别破；新工具返回 detail 就自动进第二段，不用改调度 |
| `feed.py` | 抓 RSS（含代理三档降级）、解析、入库。排序是"各源轮转" | `unread_reading_order` 同时供【世界】编号和 read_feed 用，两处必须同序 |
| `websearch.py` | DeepSeek 官方搜索/抓全文 API 的薄封装（标准库 urllib，不引第三方） | 计费接口。测试里全用假函数替换，别在测试里真调 |
| `trace.py` | 每次 API 调用的完整原文（含思考过程），存 `logs/traces/日期.jsonl`。`python -m fish.trace 3` 看 | 追加日志，随便动 |
| `util.py` | 零碎：HTML 剥文本（plain）、时间戳 | `plain` 喂给 RSS 入库和阅读两头，别只改一头 |

## 数据都在哪

**一个 SQLite 库**（`data/fish.db`，不进 git）+ 一堆日志文件（`logs/`，不进 git）：

| 表 | 是什么 | 关键字段 |
|---|---|---|
| `state` | 鱼鱼此刻的身体，**永远只有 1 行**（id=1） | energy / valence / boredom，0-100 |
| `events` | 观察层：一次唤醒一行，流水账 | kind（event/heartbeat/error/self_review）、六个 before/after 值、next_wake_at（闹钟）、trace_id |
| `memories` | 她的记忆 | layer=episodic（流水记忆，大量）/ core（自我叙事，单条覆盖）/ relation；`source_event_id` 把"事后想法"挂回那次唤醒 |
| `inbox` | 世界输入：RSS 文章 | consumed=0 是没读过的；body 入库时已是干净纯文本 |
| `logs/traces/*.jsonl` | 原料层：每次 API 调用的完整收发，想看"她到底读到/想到了什么"查这里 | 用 trace_id 和 events 表对上号 |

## 三条不变量在哪里执行（违反 = bug）

- **I1** heartbeat 不写记忆 → 执行点在 `scheduler.wake()` 末尾：`if kind == "event" and memory_text` 才写。
- **I2** 模型输出不直接成为状态值 → 执行点在 `state.apply_felt_delta`（delta 除以 10 + clamp）和 `brain._clean_felt`（boredom 永远不在白名单里）。
- **I3** 同时只有一个 wake → 执行点在 `scheduler.run` 的 `wake_lock`。

## 改动影响速查（最常撞见的几种）

| 你改了什么 | 连带发生什么 |
|---|---|
| `config.py` 里的数字 | 只改这一个文件就行，但 state/config 的数字大多有配套测试，改完跑 pytest |
| `prompt.py` 的文案 | 她的行为会变（这是特性不是 bug）；如果改动和某条测试锁定的措辞有关，测试会红——判断是"代码错"还是"规格变了" |
| 加新工具 | `tools/__init__.py` 的 ALL_TOOLS 加一行 + 写 `_run_xxx` + 在 `Tool.args_hint` 的 hints 里补参数说明；契约枚举是自动生成的，不用手改；返回 detail 就自动进第二段 |
| `db.py` 加列 | SCHEMA 和 `_migrate` 两处都要改，且加一个迁移测试（照 `test_migration_adds_link_column` 抄） |
| `state.py` 动力学 | **必跑 pytest**，这里的坏是静默的 |
| 改工具排序/编号逻辑 | 【世界】的 [n] 和 read_feed 的 index 是同一个顺序，动一个等于动两个 |

## 日常观察命令（看，不花钱）

```bash
# 最近 20 次唤醒干了啥
sqlite3 data/fish.db "SELECT ts, kind, tool, why FROM events ORDER BY id DESC LIMIT 20"

# 心情轨迹
sqlite3 data/fish.db "SELECT ts, energy_after, valence_after, boredom_after FROM events ORDER BY id DESC LIMIT 30"

# 最近一次调用的完整输入输出和思考过程（最有用）
python -m fish.trace 3
```
