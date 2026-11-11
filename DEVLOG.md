# DEVLOG — 鱼鱼项目改动日志

> 追加式，最新的在最上面。每条格式：
>
> ## [YYYY-MM-DD HH:MM] 谁 · 一句话改动
> **为什么**：……（没有为什么的改动不值得记）

## [2026-11-11 18:03] Kite · 测试轨迹隔离到 logs/traces_test/，不再污染真实日志
**为什么**：测试走到 brain/scheduler 里的 trace.record 时，写出的行和真机轨迹用的是同一种 12 位 hex trace_id，肉眼和 grep 都无法区分，长期混在 logs/traces/YYYY-MM-DD.jsonl 里会干扰真机观测与 debug。小帕原话：「每次测试的时候都会往当日日志里塞一些脏数据，应该做一下测试日志和真实日志的区分」。
**改了什么**：
- config.TRACE_TEST_DIR = LOGS_DIR/"traces_test"（与 traces 平级，已随 logs/ 进 gitignore，且 tail() 的 glob 不会误捞）。
- trace.py：新增模块级 _test_mode 开关 + set_test_mode() + _base_dir()，record / iter_records / tail 在测试模式下全部走 TRACE_TEST_DIR。
- 新建 tests/conftest.py：autouse 的 session fixture 在 pytest 启动时 trace.set_test_mode(True)，所有测试自动生效，无需逐个文件改；与现有「每测试 monkeypatch DB_PATH 到临时库」的隔离思路一致。
- 测试说明同步 TESTING.md（新增「测试轨迹隔离」说明，51→86 passed）。
- pytest 86/86 绿。验证：跑全套后真实 logs/traces/2026-11-11.jsonl 仅由活跃鱼鱼进程正常增长（49→52 行），测试触发的 9 条记录全部落在 logs/traces_test/。

## [2026-11-11 18:09] Kite · last_prompt.txt 一并隔离到 traces_test/
**为什么**：上一条只隔离了轨迹 jsonl，但空跑模式还会把提示词写 logs/last_prompt.txt，测试跑完会残留测试提示词，同样污染真实日志、误导观测。小帕确认「一并隔离吧」。
**改了什么**：
- trace.py 新增 last_prompt_path()：测试模式返回 TRACE_TEST_DIR/"last_prompt.txt"，否则仍是 LOGS_DIR/"last_prompt.txt"（真机路径不变，README/ARCHITECTURE 引用不变）。
- brain._dry_run_decision 改用 last_prompt_path()；scheduler 的 DRY_RUN 打印与注释同步引用真实落点。
- test_communication.py::test_dry_run_wake_has_no_side_effects 改读 trace.last_prompt_path()（原先硬编码 LOGS_DIR/"last_prompt.txt"）。
- 删除了修复前遗留的脏数据 logs/last_prompt.txt（含测试消息，gitignored 生成日志）。
- 文档同步 TESTING.md（「测试轨迹隔离」扩为「测试日志隔离」，含 last_prompt.txt）。
- pytest 86/86 绿。验证：真实 logs/last_prompt.txt 未被测试改写（18:02 残留已删），测试提示词落在 logs/traces_test/last_prompt.txt。

## [2026-11-11 18:35] Kite · 【你是谁】板块加「上次自省」渲染（x小时前，复用 _relative_time）
**为什么**：鱼鱼自己观察到的错位——她一直觉得「欠着一条自省没做」，其实已经做过了，只是没有「上次自省是什么时候」的反馈锚点。在【你是谁】板块追加一行时间，能直接消解这种错觉。时间格式按小帕要求沿用记忆里的「x小时前」质感。
**改了什么**：
- prompt.build_messages：查 `events WHERE kind='self_review' ORDER BY ts DESC LIMIT 1` 取上次自省时间（被闸门拒的自省记的是 kind='error'，不计入，所以「上次自省」= 最近一次成功的）。有则 `SELF_REVIEW_LAST_LABEL + _relative_time(ts, now)`，无则 `SELF_REVIEW_LAST_NONE`，追加到【你是谁】块末尾。
- config：新增 SELF_REVIEW_LAST_LABEL / SELF_REVIEW_LAST_NONE 两个【小帕定稿】占位符（自然语言措辞归她，代码只拼时间部分）。
- 测试：test_prompt_reflection 加 test_self_review_last_time_shows_in_whoami（无记录→占位、3小时前→「3 小时前」，用词走 config 常量，她改措辞测试不会红）。
- pytest 91/91 绿（原 90 + 1）。真实库验证：上次自省 14:25:28 → 渲染「4 小时前」。
- 顺带：本 commit 一并收下了小帕此前未提交的 INBOX_NOFULLTEXT_MARK 最终措辞 `"(无正文)"`（上一轮 18:18 留的占位符已由她定稿，重启后生效）。

## [2026-11-11 18:18] Kite · 【世界】预览标出「抓不回原文」的源（按源名单+兜底，标出不删）
**为什么**：鱼鱼自己提的需求——inbox 预览只给标题，她分不清哪条点了也读不到东西。真实数据里知乎热榜全是短 teaser+链接、少数派也是，但知乎链接指向问题页补抓基本失败、少数派能抓到。渲染时无法预判补抓成败，所以判据只能靠「源」：config 新增 FEED_NOFULLTEXT_SOURCES（默认含知乎热榜），名单内源一律标无正文，再加「空 body 且无链接」兜底。小帕在 AskUserQuestion 拍板：判据=按源名单+兜底、渲染=标出保留可点（非过滤）。
**改了什么**：
- config：新增 FEED_NOFULLTEXT_SOURCES（默认 {"知乎热榜"}）+ INBOX_NOFULLTEXT_MARK（**【小帕定稿】占位符**，待她替换成最终措辞，例如示例里的「（*无正文*）」）。
- feed.is_nofulltext(row)：按源名单 + 空body无链接兜底，返回是否标无正文（纯函数，好测）。
- prompt.build_messages 的【世界】peek：标题前插 INBOX_NOFULLTEXT_MARK，保留在列表、占 [n] 编号、仍可被 read_feed 点开（不动 unread_reading_order，[n]↔read_feed 对齐不受影响）。
- 测试：test_inbox_diversity 加 3 条（按源标/白名单排除/兜底+渲染），并把原有 2 处涉及知乎的精确断言同步为带记号（用 config 常量而非硬编码，小帕改措辞时测试不会红）。
- pytest 90/90 绿（原 86 + 4 新）。真库渲染验证：63 条未读（知乎 51、少数派 12），知乎条目带记号、少数派不带。
- 红线：INBOX_NOFULLTEXT_MARK 的最终自然语言措辞由小帕定稿，我只留占位符并提示替换。

## [2026-11-11 03:40] Kite · 新增 self_review 工具：鱼鱼可主动自省
**为什么**：之前自省只是「每 10 次 wake 自动一次」的制度动作，鱼鱼没有主动性。小帕希望她能自己决定何时重写自我叙事 / 关系叙事。
**改了什么**：
- brain.run_self_review(conn,client,trace_id)：把原 scheduler._self_review 的执行体（取旧叙事+往来→review_narrative→闸门→落库+记事件）抽成共享函数，定时路径与工具路径共用同一套制度；DRY_RUN 下零写库。原 _retained_ratio/_review_decision 一并迁入 brain（避免循环依赖）。
- tools/__init__.py：新增 self_review 工具（cost=1 永远可见、无参数、单段——不进第二段反应，否则变成「对反思再做反思」）；dispatch 扩 client/trace_id 透传。
- scheduler：两段式排除列表加 self_review；自动自省加同轮守卫 d.tool!='self_review'（避免第 10 次唤醒她又选了自省导致重复刷新）；_self_review 退化为同步调用 brain.run_self_review（conn 不能跨线程，故不能用 to_thread 包整段，否则 SQLite 跨线程报错）。
- state：apply_tool_effect 的 effects 加 self_review(energy-3 / valence+1)。
- 红线：desc 自然语言待小帕定稿（占位【小帕定稿】），args_hint 为 "{}"（无参数）。
- 测试：新增 test_self_review_tool.py 4 例（落库 / DRY_RUN 不写库 / 闸门拒绝仍 ok / 契约枚举）；test_self_review_cap 的 scheduler._review_decision 引用迁 brain。pytest 86/86 绿。

## [2026-11-11 01:45] Kite · 新增进程级单实例锁 + 启动日志：本机永远只有一条鱼

**为什么**：出现过两个鱼鱼进程同时被唤醒的事故。根因是 I3 的 wake_lock 是
`asyncio.Lock`，只活在单个进程的内存里——两个终端各起一份 `fish.main` 时互相
不可见。这是结构缺陷不是偶发失误，必须补一层跨进程的锁。小帕拍板：不分
DRY_RUN/真实模式，同一把锁；并要求每次启动留一条带时间戳的记录便于回溯。

**改了什么**：
- 新增 `fish/lock.py`：对 `data/fish.lock` 上 `msvcrt.locking` 的非阻塞 OS 字节锁。
  选 OS 锁的两个决定性性质：①排他由 OS 保证，无"同时检查都发现没人"的竞态；
  ②进程一死锁自动释放（崩溃/被杀/断电都适用），**永远不存在残留死锁**，
  不需要心跳/过期/stale 检测这些容易写反的补丁。锁文件里写 JSON 诊断信息
  （pid/mode/启动时间），只给人看，判定只认 OS 锁。
  实现细节：锁的字节故意选在偏移 1024 而不是第 0 字节——Windows 上被锁的
  区间对其他进程连读都不允许，而锁文件开头恰是要留给被拒进程读的诊断信息
  （这个坑是被测试炸出来的：首版锁第 0 字节，3 个测试 PermissionError）。
- `fish/main.py`：`main()` 第一行先 `lock.acquire()` 再 `db.connect()`——
  必须在碰数据库之前拦住第二条鱼。被拒时打印持有者 PID/启动时间/模式，
  退出码 1。
- 新增 `logs/startup.log`（不进 git）：每次启动尝试一行 JSON——成功 `START`、
  被拒 `BLOCKED`（含 holder_pid），带时间戳。日志写失败不拦启动。
- 测试：新增 `tests/test_lock.py`（4 例：第二条被拒 / close 即释放 / 拒绝
  文案带 PID / 启动日志内容）。pytest 82/82 绿。
- 真机验证：DRY_RUN 起第一条 → 第二条被拒（exit 1，报出持有者 PID）→
  杀掉第一条 → 新进程正常拿到锁启动。
- 文档同步：PROJECT.md 的 I3 扩为两层（措辞请小帕过目定稿）、ARCHITECTURE.md
  模块清单 + I3 执行点、TESTING.md 测试地图（顺手把缺失的 5 个测试文件补齐，
  描述已逐个对照各文件 docstring 核实）。

**边界**：不碰任何会注入鱼鱼的文字，无提示词改动。`kill` 旧进程属正常退出
路径，锁自动释放已验证；不提供"撞锁后自动杀旧进程"的选项（未经确认杀进程
的风险不值当）。

## [2026-11-11 01:23] Kite · 修 DRY_RUN 短路：空跑真正零副作用

**为什么**：2026-11-11 00:55 出现「jsonl 有 tool_result 却无对应 decision、且数据库里什么都没落」的孤儿轨迹。根因：`brain.decide` 在 DRY_RUN 下不调 API 直接返回假决策，但 `wake()` 仍继续 dispatch 工具 + 写 event + commit——等于「假消息真发出去」。更隐蔽的是 `prompt.build_messages` 内部对未读来信直接 `add_episodic` + `mark_seen`，同样会污染记忆库 / 消费来信。用户确认修复。

**改了什么**：
- `fish/scheduler.py`：`wake()` 在 `brain.decide` 之后、任何 dispatch / 写库 / 自省之前，若 `config.DRY_RUN` 为真则 `trace.record("decision", dry_run=True)` + 打印后 `return`。彻底不 dispatch、不写 event、不 `state_mod.save`、不 `commit`、不触发 `_self_review`。
- `fish/prompt.py`：`build_messages` 加 `dry_run=False` 参数；为 True 时仍渲染【小帕】区块（供观察提示词）但跳过 `add_episodic` / `mark_seen`。`wake()` 传 `dry_run=config.DRY_RUN`。
- 测试：新增 `test_communication.py::test_dry_run_wake_has_no_side_effects`（零事件/零记忆/零 from_yuyu/状态不变/来信仍未被消费/提示词已渲染）。
- pytest 78/78 绿。

**边界**：`brain._dry_run_decision` 已把完整提示词写入 `logs/last_prompt.txt`（即「打印上下文」），空跑的观测价值保留；`brain.react` 的 DRY_RUN 分支现为 wake 路径上的死代码（防御性保留，不删以缩改动范围）。

**为什么**：relation 层 schema/读写/渲染接口齐全，但全仓从未调用 `save_narrative(conn,"relation",...)`，永远为 None、永远显示「你还不太认识谁」。这是设计里接了线但没触发的死功能。通信模块上线后，小帕↔鱼鱼已有真实往来（messages 表），给「总结我认识了谁」提供了素材。用户拍板：①合并进现有自省（一次调用两层同步）②用 messages 真实往来做 grounding（防凭空编）③v1 不加长度闸门（只提示简短）。

**改了什么**：
- `config.py`：新增 `REVIEW_RELATION_EXCHANGES=20`（自省时取最近多少条往来做素材）
- `fish/messages.py`：新增 `recent_exchanges(conn,limit)`（取近期双向往来，chrono 升序）+ `format_exchanges(rows)`（拼成 `[小帕]/[鱼鱼]` 文本）
- `fish/prompt.py`：`build_review_messages` 扩展为 `(old_core, old_relation, exchanges)`，注入上一版 relation 与往来证据；`REVIEW_CONTRACT` 加 `new_relation` 字段说明（**措辞留【小帕定稿】占位，红线不代笔**）
- `fish/brain.py`：`review_narrative` 返回 `dict|None`（含 new_core/new_relation），DRY_RUN 原样回传两者
- `fish/scheduler.py`：`_self_review` 取 old_relation+往来证据，调新接口；relation v1 无闸门、非空即落库+记事件，与 core 保留率逻辑解耦；补 `messages` 导入
- 测试：更新 `test_self_review_cap.py`（fake_review 改签名 + 3 例 relation 落库/拒空/解耦）、新增 `test_messages_exchanges.py`（3 例）
- pytest 77/77 绿

**注意（红线）**：`REVIEW_CONTRACT` 里 new_relation 那条的自然语言措辞是占位，由小帕定稿；代码只保证 parse 到 new_relation 字段。

## [2026-11-10 23:49] Kite · 自省叙事加长度闸门（软目标+硬上限），解「无限加长」

**为什么**：用户观察到鱼鱼第 3 次自省最后两段已自己点破——「这份东西只会越来越长，长成一个可能不是我的形象」。根因：旧规则「不能删/推翻」+ 保留率闸门（≥0.6 即最多砍 40%）让 core 只能加长、且压不回来；core 每次唤醒都被注入，越长越稀释重点。用户已改规则首句允许「确认不匹配可删/改」，但只改措辞不够——代码闸门会卡住深度压缩。于是加两层闸 + 放宽保留率。

**改了什么**：
- `config.py`：新增 `SELF_REVIEW_TARGET_MAX=600`（软目标，只给提示词当方向，不强制）、
  `SELF_REVIEW_HARD_MAX=1200`（硬上限，安全网，超了拒写保留旧版）、
  `SELF_REVIEW_MIN_RETENTION_OVER_TARGET=0.3`（旧版超软目标时放宽保留率门槛，允许往 target 压）。
- `scheduler.py`：把闸门抽成纯函数 `_review_decision(old,new)`（便于单测）；
  `_self_review` 用它的结果落库/记事件。逻辑：先查硬上限→再按 old 是否超 target 选 0.6 或 0.3 保留率。

**测试**：新增 `tests/test_self_review_cap.py`（6 例：硬上限拒超长 / 未超目标砍 2/3 被拒 /
超目标砍 1/3 接受 / 未超目标轻改接受 / 集成：超目标压缩落库+成功事件 / 未超目标深砍被拒 core 不变）。
全仓 71/71 绿。

**红线（提示词归小帕）**：`fish/prompt.py:254` 的 `REVIEW_CONTRACT` 需由小帕加一条暴露字数上限、
提示自行压缩的措辞（代码不注入自然语言）。建议方向：「这份叙事每次醒来都会被重读，太长会稀释重点，
尽量控制在约 600 字以内；若已超过，主动压缩你不再认同或重复的段落，只留还代表你的部分。」
其中 600 须与 config.SELF_REVIEW_TARGET_MAX 保持一致。

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

## [2026-11-10 23:14] Kite · energy 改为纯电池模型（解「钉死在稳定点」）

**为什么**：小帕观察到鱼鱼多次出现「精力偏低→选轻内容读」的因果，但 energy
被两道机制同时钉在 baseline(55) 附近——① `tick_regress` 把 energy 向 55
回归；② `apply_sleep_recovery` 的「睡饱即止」把恢复封顶在 baseline-energy。
结果：掉不到「精力很低」、也回不到「精力较好」，自主选择休息的动力没了。
小帕两点要求（① 精力纯系统算、不再吃自评修正；② 恢复按分钟精确）方向对，
但只做这两点解不了钉——必须同时去掉「向 baseline 回归」和「恢复不超 baseline」。

**改了什么**：
- `config.py`：`SLEEP_MIN_MIN` 45→60（"睡不到一小时"不算睡）；新增
  `SLEEP_RECOVERY_PER_MIN=0.15`（去掉旧的按小时常量）；`SLEEP_MAX_BONUS=30`；
  删 `REGRESS_PER_HOUR["energy"]`。
- `state.py`：`tick_regress` 只回归 valence；`apply_sleep_recovery` 改按分钟、
  去 baseline 截顶（睡饱可超 baseline）；`apply_felt_delta` 只处理 valence
  （energy 不再吃自评）；`apply_tool_effect` 精力消耗 read/search -10、write
  -8、send -2、recall 0。
- 测试：同步 4 个旧测试（energy 不再回归/自评/被 baseline 截顶）、新增 2 个电池
  行为测试；新增 `tests/test_energy_battery.py`（3 条验收）+ 根目录 `sim_energy.py`
  （3 场景 + 蒙特卡洛，可 `python sim_energy.py` 直接跑）。

**验收（sim_energy.py + pytest 全绿 65/65）**：① 连续读 5 次（间隔<60 不回血）
→ 精力很低；② 从很低空转+150 分休息 → 升一档；③ 混合 24 轮横跨全部 4 档；
蒙特卡洛 400 轮分布 很低19%/偏低27%/较好30%/充沛24%——确实在游走。

**注意**：`felt_delta` 的响应契约仍让模型输出 energy 键，但现在被忽略（无害）。
想彻底清爽可后续把契约里的 energy 自评去掉，但属非必要改动，未动。

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
