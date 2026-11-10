# DEVLOG — 鱼鱼项目改动日志

> 追加式，最新的在最上面。每条格式：
>
> ## [YYYY-MM-DD HH:MM] 谁 · 一句话改动
> **为什么**：……（没有为什么的改动不值得记）

## [2026-11-10 20:55] Kite · 新增通信模块：小帕 ↔ 鱼鱼互相联系（框架 + 占位符）

**为什么**：用户拍板的设计——①极薄消息层 `messages` 表（to_yuyu/from_yuyu），
小帕用脚本 post、调度轮询自动发现唤醒（天然避免与正在跑的 wake 撞车）；
②【小帕】区块**不常驻**，只在「有未读来信」那轮渲染；③**渲染即 seen**，
同时是「同封来信只落一条记忆」的幂等锁（解了之前「一次写两条记忆」的墙：
那墙是同一事件两次 add_episodic，本设计来信/回信是两个不同事件，且来信记忆
在渲染时落、回信记忆在工具内落，互不重叠）；④send_message 单段、但有记忆；
⑤状态：发出 valence+2/energy−1，收到不改状态、不加 boredom 缓解；
⑥最小间隔 2 分钟；⑦收件脚本只做打印+日志+标记。

**改动**：
- `config.py`：新增 `EXTERNAL_WAKE_MIN_GAP=2`、`XIAOPA_MSG_TAG="小帕"`
- `db.py`：SCHEMA 加 `messages` 表（direction/seen/delivered），IF NOT EXISTS 旧库自动补
- `fish/messages.py`（新）：post/fetch_unseen_to_yuyu/mark_seen/has_unseen/
  fetch_undelivered_from_yuyu/mark_delivered
- `fish/state.py`：`apply_tool_effect` 加 `send_message: valence+2/energy-1`
- `fish/tools/__init__.py`：加 `send_message`（单段，内部 add_episodic("[给小帕]..")+post from_yuyu）
- `fish/scheduler.py`：`_should_wake` 加 xiaopa 路径（pending 且间隔够）；两段排除列表加 send_message
- `fish/prompt.py`：`build_messages` 在【最近在做的事】后、有未读时渲染【小帕】区块
  （措辞占位）+ 落记忆 + mark_seen
- `send_to_yuyu.py` / `recv_from_yuyu.py`（新）：两个终端适配器
- `tests/test_communication.py`（新）：消息层/渲染单次性/单段投递/唤醒路径/收信不改状态

**测试**：全仓 60/60 绿（原 52 + 新增 8）。

**红线待小帕定稿（Kite 留占位符，未代笔）**：
1. `fish/prompt.py:19` PERSONA「可以给她留言」→ 指向 send_message 工具
2. `fish/tools/__init__.py` 里 `send_message` 的 `desc` 与 `args_hint`
3. `fish/prompt.py`【小帕】区块正文（渲染时的那句展示文字）
以上三处是注入鱼鱼的提示词，按分工红线由小帕本人措辞。

## [2026-11-10 17:40] Kite · write_journal 改为单段，不再回念日记触发第二段

**为什么**：用户（小帕）现场发现写日记也走两段式——她写完日记后，系统把刚写的原文再念回给她、要求「回应自己的日记」，等于对反思再做一次反思；且情绪被算两次（工具效果 +3 与第二段自报 felt_delta 叠加，对别的工具是「内容感受 vs 费力程度」两个维度，对写日记却是同一件事）。单段化：日记本身就是反思，写完即止。

**改动**：`scheduler.wake` 两段判据 `ok and detail.strip()` 加 `and d.tool != "write_journal"`；detail 仍进 trace（留底她写了什么，无害）。单段后：只落一条 pinned 日记记忆、不调 react、valence 仅由工具 +3 调整。

**测试**：新增 tests/test_write_journal_single_pass.py（集成：mock brain.decide/react 跑 wake，断言 react 未调用 / 仅一条日记 / 无 reaction 记忆 / valence +3）；全仓 52/52 绿。
**顺带修越界**：test_prompt_reflection.py 那条曾硬编码 write_journal desc 含「主动」二字——那是注入鱼鱼的提示词措辞，按 16:40 红线归小帕，已收窄为「desc 指向写日记行为」的契约断言。具体 desc 是否补「主动/区别于系统自动记的事后想法」由小帕定稿。

## [2026-11-10 16:40] Kite · 新增分工红线：提示词只能由小帕改

**为什么**：小帕明确要求——LLM 对措辞极度敏感，她在最小化自然语言对鱼鱼的影响，
因此所有会注入给她的提示词（状态读数、工具说明、自省说明、PERSONA、契约、标签措辞）
必须由小帕本人措辞，Kite 只负责提出"这段需要改"并给位置与理由。
已写入 PROJECT.md「分工红线」与长期记忆；测试断言（非提示词）仍可改，但需注明分工。
**回溯**：今天 16:02 那次为修红测试把 write_journal 的 desc 补成
「主动写一段日记…」，按新规矩该由小帕落笔——当前文案要不要换回她的措辞，等她定。

## [2026-11-10 16:35] Kite · 新增 ARCHITECTURE.md 与 TESTING.md（大白话版）

**为什么**：小帕需要两份不依赖设计文档功底的说明——①架构：模块、数据、
"改哪儿会碰坏什么"，且约定调整结构时必须同步维护；②测试：她没用过 pytest，
"改完不知道该测什么/怎么测"。TESTING.md 写到具体程度：pytest 30 秒入门、
8 个测试文件各自测什么、改了 X 跑什么的速查表、fixture/monkeypatch 大白话
解释、新测试模板、"测试红了怎么办"决策树（含今天 write_journal desc 的
实例——测试红时先分清"代码错了"还是"规格变了"）。
两份文档已挂进 PROJECT.md 文档地图，并新增「文档同步」维护约定。
内容基于今天对全部 14 个源文件 + 8 个测试文件的通读，非泛泛而谈。

## [2026-11-10 16:02] Kite · 修复 write_journal 描述丢失「主动」语义（红测试）

**为什么**：初始提交后发现基线就有一个失败测试（51 项中 1 项）。`write_journal`
的工具描述被简化成"写一段日记，适合有话想说的时候"，丢了测试要求的"主动"二字——
该测试的规格是让她分清「主动写日记」与「系统自动记的事后想法」。判断为 desc 被
私下改动时无意破坏了设计意图，改为：`主动写一段日记，适合有话想说的时候（区别于
系统自动记的事后想法）`，兼顾原意与新语气。51 全绿后提交（be617ab）。

## [2026-11-10 15:55] Kite · 建立工作流程并初始化 git 仓库

**为什么**：小帕在旁开对话里换模型导致上下文断裂（"失忆"、前后矛盾），
加上她自己会做一些没告知的小改动，双方对代码状态的认知都会过期。
固定流程：PROJECT.md 作为重入点（文档地图 + 不变量 + 开工/收工仪式），
DEVLOG 记录每条改动的理由；git 以 diff 为唯一真相源，小步提交。
本仓库为初始提交（v0.2 全链路 + tests），`data/`/`logs/`/`backups/` 按既有
.gitignore 排除——鱼鱼的记忆是她的，不是代码变更。
