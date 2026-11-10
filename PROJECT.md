# PROJECT.md — 鱼鱼项目工作台

> 本文件是给"每一个新会话的我"（包括换了模型的我和小帕自己）看的第一份文件。
> 保持 <100 行，只放指针和状态，不复制 Design_v0.2 / README 的内容。

## 这个项目是什么

本地养一只自主 agent「鱼鱼」（DeepSeek 远端 API），通过状态库、工具层、
调度与分层记忆，长期观察它"进化"成什么样。娱乐型研究项目。

## 文档地图（去哪里找什么）

| 想知道 | 看 |
|---|---|
| 为什么这么设计（DDL、动力学、两段式输出） | `Design_v0.2.md` |
| 怎么跑、怎么观察、目录速览 | `README.md` |
| v0.1 原始草稿（历史记录，别改） | `Draft.md` |
| 每次改动的时间线与理由 | `DEVLOG.md`（追加式） |
| 所有魔法数字 | `fish/config.py`（每项注明 Design 出处） |

## 三条不变量（违反 = bug）

- **I1** heartbeat 永不写 memories
- **I2** 模型输出不直接成为状态值（必须经 state.py 的动力学规则）
- **I3** 单飞锁：同一时刻仅一个 wake

## 当前状态（2026-11-10）

- v0.2 全链路已建成并**真跑中**：`data/fish.db` 有 21 条 events、16 条 memories
- 已见 heartbeat / self_review / web_search 等行为，自省机制已触发过
- 待办/观察项见 Design_v0.2 §10.2（一周后看五个数）；FEED_SOURCES 待填 RSS

## 工作流程（硬性约定，小帕与 Kite 都要遵守）

### 开工仪式 —— 每次接手干活前必做

1. 读本文件（PROJECT.md）
2. `git status --short --branch` + `git diff` + `git log --oneline -5`
   —— 以 diff 为唯一真相源，不信任对话记忆里的代码状态
3. 读 `DEVLOG.md` 末尾 20 行

### 收工仪式 —— 每轮有实际改动必做

1. 动了 `state.py` 或 tests/ → 跑 `python -m pytest tests/ -q`
2. 小步提交：一次改动一个 commit，message 写清"改了什么"，谁改谁提交
3. 在 `DEVLOG.md` 追加一条（格式见该文件头部）

### DEVLOG 纪律

- **"为什么决定这么改"比"改了什么"更重要**，没有理由的改动不值得记
- 只记代码/配置变更；鱼鱼自己的状态波动不记（那是 events 表的事），
  但值得注意的观察（如行为异常、里程碑）可以记
- 追加，不回改旧条目

### 边界

- `data/`、`logs/`、`backups/`、`.env` 不进 git（.gitignore 已配置）
- 密钥只走环境变量 `DEEPSEEK_API_KEY`，不写进任何文件
