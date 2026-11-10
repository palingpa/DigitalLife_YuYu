"""小帕 → 鱼鱼：发一条消息（终端适配器，v1）。

用法：
    python send_to_yuyu.py "鱼鱼，今天过得还好吗？"

消息写入 messages 表（direction='to_yuyu'）。下一 tick（≤60 秒）调度器自动
发现并唤醒鱼鱼、渲染【小帕】区块——这里不直接唤醒，而是只把消息插进数据库，
由调度轮询去发现。这样天然避免和正在跑的 wake 撞车（见 Design_v0.2 的
scheduler 轮询设计：外部消息只需入库，下一 tick 自动被 _should_wake 看到）。

以后接聊天软件（微信/邮件…）时，新增一个适配器把外部消息转成这行 post 即可，
鱼鱼这边的逻辑一行不用改。
"""

import sys

from fish import db, messages


def main() -> None:
    text = " ".join(sys.argv[1:]).strip()
    if not text:
        print('用法：python send_to_yuyu.py "想对鱼鱼说的话"')
        return
    conn = db.connect()
    mid = messages.post(conn, "to_yuyu", text)
    if mid is None:
        print("消息为空，没发送。")
        return
    print(f"已送进鱼鱼信箱（消息 #{mid}）：{text}")
    print("下一轮唤醒时她会看到（≤60 秒）。")


if __name__ == "__main__":
    main()
