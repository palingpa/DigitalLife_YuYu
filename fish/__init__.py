"""fish 包：数字生命「鱼鱼」。

分层（对应 Design_v0.2.md §2）：
    config    所有"魔法数字"集中在这里，调参只改这一个文件
    db        SQLite 连接与建表
    state     身体：能量/效价/无聊 三个数值的动力学
    memory    记忆：分层存储 + 打分检索
    prompt    一次 wake 的上下文组装
    brain     调 DeepSeek API，解析模型输出
    scheduler tick 时钟、唤醒判断、单飞锁
    tools/    三个第一版工具
"""
