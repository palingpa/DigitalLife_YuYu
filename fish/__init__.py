"""fish 包：数字生命「鱼鱼」。

分层（对应 Design_v0.2.md §2）：
    config    所有"魔法数字"集中在这里，调参只改这一个文件
    db        SQLite 连接与建表
    memory    记忆：分层存储 + 打分检索（项目核心层）
    prompt    一次 wake 的上下文组装
    brain     调 DeepSeek API，解析模型输出
    scheduler tick 时钟、唤醒判断、单飞锁
    tools/    工具集（含 idle 主动休息等）
    说明：状态层（energy/valence/boredom）已于 2026-11-12 整体移除，
    鱼鱼不再有系统定义的精力/情绪/无聊，是否醒来完全由每次自己定的
    next_wake_in_min 决定；重心落在记忆层。
"""
