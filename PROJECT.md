# PROJECT.md — 鱼鱼项目工作台

> 本文件是给"每一个新会话的我"（包括换了模型的我和小帕自己）看的第一份文件。
> 保持 <100 行，只放指针和状态，不复制 Design_v0.2 / README 的内容。

## 这个项目是什么

本地养一只自主 agent「鱼鱼」（DeepSeek 远端 API），通过工具层、
调度与分层记忆，长期观察它"进化"成什么样。娱乐型研究项目。

## 文档地图（去哪里找什么）

| 想知道 | 看 |
|---|---|
| 为什么这么设计（DDL、动力学、两段式输出） | `Design_v0.2.md` |
| 怎么跑、怎么观察、目录速览 | `README.md` |
| **架构大白话：模块/数据/改哪儿会碰坏什么** | `ARCHITECTURE.md` |
| **测试大白话：pytest 入门 + 8 个测试文件地图 + 改了 X 跑什么** | `TESTING.md` |
| v0.1 原始草稿（历史记录，别改） | `Draft.md` |
| 每次改动的时间线与理由 | `DEVLOG.md`（追加式） |
| 所有魔法数字 | `fish/config.py`（每项注明 Design 出处） |

## 三条不变量（违反 = bug）

- **I1** heartbeat 永不写 memories
- **I2** （状态层 2026-11-12 整体移除后退役）原意为「模型输出不直接成为状态值」；
  现等价纪律为「模型原始输出须经解析校验（parse_ok）才往下传，解析失败绝不沿用上次结果」
  （见 `brain._parse_*`）。没有状态值可成为，这条的本质仍是「模型说的不算数，校验过的才算」。
- **I3** 单飞，两层保证：进程级——本机同一时刻只有一个鱼鱼进程（`fish/lock.py`，
  `data/fish.lock` 的 OS 锁，不分 DRY_RUN/真实，`main` 第一行抢锁）；进程内——
  同一时刻仅一个 wake（`scheduler.py` 的 `wake_lock`）

## 当前状态（2026-11-12）

- v0.2 全链路已建成并**真跑中**；**状态层（energy/valence/boredom）已于本日整体删除**：
  不再有系统定义的精力/情绪/无聊，是否醒来完全由鱼鱼每次自己定的 `next_wake_in_min` 决定；
  项目重心落在记忆层（详见 DEVLOG 末条 + Design_v0.2 关于状态层的章节现已部分失效）。
- 已见 heartbeat / self_review / web_search / idle 等行为，自省机制已触发过
- 待办/观察项见 Design_v0.2 §10.2（一周后看五个数）；FEED_SOURCES 待填 RSS
- 注意：状态层删除后，`data/fish.db` 的 `state` 表与 `events` 的六个能量列由
  `db._migrate()` 在下次连接时 DROP（SQLite 3.53+ 支持），真实库无需手动迁移

## 工作流程（硬性约定，小帕与 Kite 都要遵守）

### 开工仪式 —— 每次接手干活前必做

1. 读本文件（PROJECT.md）
2. `git status --short --branch` + `git diff` + `git log --oneline -5`
   —— 以 diff 为唯一真相源，不信任对话记忆里的代码状态
3. 读 `DEVLOG.md` 末尾 20 行

### 收工仪式 —— 每轮有实际改动必做

1. 动了 `fish/` 核心模块或 tests/ → 跑 `python -m pytest tests/ -q`
2. 小步提交：一次改动一个 commit，message 写清"改了什么"，谁改谁提交
3. 在 `DEVLOG.md` 追加一条（格式见该文件头部）

### 文档同步（谁改谁维护）

- 调整项目结构、加删模块、改数据表 → 同步 `ARCHITECTURE.md`
- 加删测试文件、改测试跑法 → 同步 `TESTING.md`
- 状态推进到新阶段（新行为出现、里程碑）→ 更新本文件「当前状态」

### 分工红线：提示词只由小帕改（2026-11-10 定）

**凡是会直接注入给鱼鱼的自然语言，一律由小帕本人措辞、由她动手改。**
Kite 的职责是提出「这段提示词需要改」，说明位置（文件:行）、理由、可选方向，
**不落笔**。理由：LLM 对措辞极度敏感，她在最小化自然语言对鱼鱼的影响。

范围（不限于）：
- `fish/prompt.py`：PERSONA、CONTRACT、REACTION_CONTRACT、REVIEW_CONTRACT、
  CORE_PLACEHOLDER，以及 `build_messages` 里所有【】标签与措辞（如「你事后心里想」）
- `fish/tools/__init__.py`：每个工具的 `desc`、`args_hint` 文字
- 任何未来会被拼进 messages 的字符串

Kite 可以做的：改测试断言（测试不是提示词，文案变了导致红，可以同步改）——
但必须在 DEVLOG 里写清楚"文案由小帕改，测试由 Kite 同步"。

### DEVLOG 纪律

- **"为什么决定这么改"比"改了什么"更重要**，没有理由的改动不值得记
- 只记代码/配置变更；鱼鱼自己的状态波动不记（那是 events 表的事），
  但值得注意的观察（如行为异常、里程碑）可以记
- 追加，不回改旧条目

### 边界

- `data/`、`logs/`、`backups/`、`.env` 不进 git（.gitignore 已配置）
- 密钥只走环境变量 `DEEPSEEK_API_KEY`，不写进任何文件
