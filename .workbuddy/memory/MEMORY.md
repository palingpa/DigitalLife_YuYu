# 鱼鱼项目长期笔记（D:/DigitalLife_YuYu）

## 项目概况
娱乐型项目：在本地养一只名为「鱼鱼」的自主 agent（DeepSeek V4.1F 远端 API），
通过状态库、工具层、调度与分层记忆，长期观察它"进化"成什么样。
- `Draft.md`：v0.1 原稿，**保留不动**，作为设计演化记录。
- `Design_v0.2.md`：落地规格（DDL、状态动力学参数、prompt 模板、空转规则、观测指标），照它建仓。

## 用户拍板的决策（2026-11-09）
- 语言：Python 3.11 + asyncio。小帕借本项目学 Python。
- 全走远端 API，成本约 ¥5-15/月，不为省钱牺牲设计。
- 第一版只做自主循环；被动唤醒/OneBot 复用推到 v0.3。
- 世界输入源（RSS）暂留空，由小帕自己填。
- self_review 触发频率 = 每 10 次 wake（原 20，初期让自我叙事快长出来，一周后再评估）。

## 项目约定
- **代码注释**：小帕是 Python 初学者，精髓/不显然的片段（asyncio 调度、单飞锁、状态回归、delta clamp）要顺手加初学者友好的注释。
- 三条不变量：I1 heartbeat 永不写 memories；I2 模型输出不直接成为状态值；I3 单飞锁，同一时刻仅一个 wake。
- mood 分两组：energy/valence（回归 baseline）+ boredom（不回归，驱动量）。
- 核心观测指标：valence 3 档 × 工具选择的互信息（0=表演，0.05-0.3 健康，>0.5 假自主）。

## 关键提醒
- 提及用户用「她」（小帕是女性）。
- 建仓顺序见 Design_v0.2.md §11：db → state(单测!) → prompt(打印肉眼查) → brain → scheduler(DRY_RUN) → tools → 72h 实跑。

## 工作流程（2026-11-10 固定，硬性约定）
背景：小帕会在对话中换模型（上下文断裂）且偶尔私下改代码（认知过期）。对策：
- **项目根有 `PROJECT.md`（重入点：文档地图/不变量/当前状态/仪式）+ `DEVLOG.md`（追加式改动日志，"为什么"必填）**。
- **开工仪式**：读 PROJECT.md → `git status --short --branch` + `git diff` + `git log --oneline -5`（diff 为唯一真相源）→ 读 DEVLOG 末尾 20 行。
- **收工仪式**：动了 state.py/tests → pytest → 小步 commit → DEVLOG 追加。谁改谁提交，双方共用一条历史。
- data/logs/backups/.workbuddy 不进 git；密钥只走环境变量。
- git 身份为仓库级配置（user.name=Xiaopa）；我的提交可在 --author 标 Kite。
- 换模型后不用对话续命：照 PROJECT.md + git diff + DEVLOG 即可重入。
