# 鱼鱼 · 运行手册

设计文档见 `Design_v0.3.md`（为什么这么设计），本文只讲怎么跑。

## 首次运行

```bash
# 1. 装依赖
python -m pip install -r requirements.txt

# 2. 空跑验证（不调 API，不花钱）
#    Windows PowerShell:  $env:FISH_DRY_RUN="1"; python -m fish.main
#    Git Bash:            FISH_DRY_RUN=1 python -m fish.main
#    看到 wake(due) 的输出、logs/last_prompt.txt 有内容即为链路通。

# 3. 跑测试（全链路回归，改动核心模块后务必重跑）
python -m pytest tests/ -q

# 4. 真跑
#    PowerShell:  $env:DEEPSEEK_API_KEY="sk-..."; python -m fish.main
#    Git Bash:    DEEPSEEK_API_KEY=sk-... python -m fish.main
```

建议先 `FISH_DRY_RUN=1` 连续空跑一晚，确认没有报错、`data/fish.db`
里 events 在正常积累，再切真实 API。

## 日常观察

```bash
# 最近 20 条唤醒
sqlite3 data/fish.db "SELECT ts, kind, tool, why FROM events ORDER BY id DESC LIMIT 20"

# 记忆层观察（状态层已移除，不再有心情数值；看她"记住了什么"）
sqlite3 data/fish.db "SELECT ts, layer, substr(content,1,40) FROM memories ORDER BY id DESC LIMIT 20"

# 工作记忆：她给下一轮的自己留了什么话（NULL=没写，空串=不留）
sqlite3 data/fish.db "SELECT ts, tool, why, note_for_next FROM events WHERE note_for_next IS NOT NULL ORDER BY id DESC LIMIT 20"

# 最近一次调用的完整输入/输出/思考过程（最有用）
python -m fish.trace 3
```

（没装 sqlite3 命令行的话，用 Python：`python -c "import sqlite3; ..."` 同理。）

## 调用轨迹（完整输入输出）

每次 API 调用都会在 `logs/traces/YYYY-MM-DD.jsonl` 追加一行，包含：

- `messages`：这一发给模型的**全部**内容（system + user），含重试时追加的修复对话
- `content`：模型正式的 JSON 回复
- `reasoning`：**思考过程**（推理模型才有；若是空字符串说明这个模型不返回思维链）
- `tokens_in` / `tokens_out` / `duration_ms` / `attempt`
- `kind`：`decision`（第一段·决策）/ `reaction`（第二段·反应）/ `review`（自省）/ `tool_result` / `error`

**一次唤醒可能是两段调用**，用同一个 `trace_id`。第二段是「看完结果之后的反应」——
`memory` 产生在第二段，所以 `reaction` 那行才是她真正记住的东西。
自省是第三种调用（`review`），每 10 次 wake 一次，或她自己选 `self_review` 工具时发生；
它的输入是另一套上下文：旧自我叙事 + 关系叙事 + 往来 + 上次自省以来她自己写下的东西。
`python -m fish.trace 3` 会按顺序把这几段都打出来。

另外每次工具调用的 `detail`（文章全文等）也会以 `tool_result` 记一行——
想看「她到底读到了什么」，查这里，而不是 events 表。

`events` 表和轨迹用 `trace_id` 关联：

```sql
SELECT ts, kind, tool, why, trace_id FROM events ORDER BY id DESC LIMIT 5;
```

然后可以在 `logs/traces/` 里搜同一个 id。

关掉轨迹：`FISH_TRACE=0`。一天几十条、几 MB，长期留着也不占地方。

## 配置

全部参数在 `fish/config.py`，每个数字都注明了出处（Design_v0.3 的 §号）。
最常动的三个：

- `MODEL`：接好 V4.1F 后改成官方 model id
- `FEED_SOURCES`：RSS 地址列表，填上鱼鱼才有东西可看
- `SELF_REVIEW_EVERY`：自省频率，跑一周后回头评估

密钥走环境变量 `DEEPSEEK_API_KEY`，不写进任何文件。

## 停止 / 重启

Ctrl+C 停止。状态、记忆、日志全部实时落库（SQLite WAL），重启接着跑。
中途强杀也不丢数据——最多丢掉正在进行的这一次 wake。

## 目录速览

```
fish/
  config.py      所有魔法数字
  db.py          SQLite + DDL + 老库迁移
  memory.py      记忆：三层存储 + 打分检索 + 工作记忆单条槽
  prompt.py      一次 wake 的上下文组装
  brain.py       DeepSeek 调用 + 解析 + 降级 + 写轨迹
  trace.py       调用轨迹的写入与查看（python -m fish.trace）
  scheduler.py   tick / 唤醒判断 / 单飞锁 / 自省
  tools/         read_feed / write_journal / recall / web_search / send_message / self_review / idle（主动休息）
  feed.py        RSS/Atom 抓取（含代理降级）
  websearch.py   DeepSeek Search/Fetch API 封装（联网搜索与按需抓全文）
  main.py        入口
tests/           回归测试（记忆层/工具/通信/调度）
data/            fish.db（记忆与日志，已在 .gitignore）
logs/            traces/*.jsonl 调用轨迹 + last_prompt.txt
```
