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
