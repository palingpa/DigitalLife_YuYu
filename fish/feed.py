"""世界输入：定时抓 RSS，塞进 inbox。

源列表在 config.FEED_SOURCES（小帕自己维护），支持 ("名字", url) 起显示名。
有些源只给开头一段（teaser），全文靠 read_feed 读时用 Fetch 接口补——
见 tools._run_read_feed 和 2026-11-10 的修复记录。

关于代理（2026-11-09 踩坑实录）：
某些源（如 zhihu-hot.ayanamist.com）直连不通，必须走代理。
浏览器之所以「看起来能打开」，是因为浏览器走了系统代理；
而终端里的 Python 进程没继承代理设置，直连就超时。
所以这里实现了三档策略 + 会话内记忆：
    1. config.FEED_PROXY 显式指定的代理（最优先）
    2. 系统默认（环境变量 / Windows 注册表里的代理）
    3. 直连（无代理）
一旦某档成功，本次运行期间就记住它，不再反复试错。
"""

import asyncio
import sqlite3
import xml.etree.ElementTree as ET   # 标准库自带，解析 RSS 够用
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener, urlopen

from . import config, util


def _split_source(entry) -> tuple[str | None, str]:
    """把 FEED_SOURCES 的一条配置拆成 (显示名, URL)。

    两种写法都认：裸字符串 "https://…" 或 ("名字", "https://…")。
    裸字符串的显示名返回 None，由 source_name 兜底取域名。
    """
    if isinstance(entry, (tuple, list)) and len(entry) == 2:
        return str(entry[0]), str(entry[1])
    return None, str(entry)


def source_name(url: str) -> str:
    """给鱼鱼看的源名字。库里存的 source 是完整 URL，她读 URL 没有意义。

    优先用 config 里起的名字（("少数派", url) 的第一个元素），
    没起名字或源已下线的旧条目，退回去掉 www. 的域名。
    """
    for entry in config.FEED_SOURCES:
        name, u = _split_source(entry)
        if u == url and name:
            return name
    host = urlparse(url).netloc or url
    return host.removeprefix("www.")


def is_nofulltext(row) -> bool:
    """这条在【世界】预览时该不该标「无正文」（见 prompt.build_messages）。

    判据（2026-11-11 小帕拍板）：
    1. 源在 config.FEED_NOFULLTEXT_SOURCES（默认知乎热榜）——这类源只给
       标题/问题，链接指向问题页、补抓基本失败，没法当长文读；
    2. 兜底：body 剥完纯文本啥也没有（空）且没链接可补抓。
    渲染时无法预判「补抓到底成不成功」，所以只能靠这两个可观测属性；
    想要更准得真去抓一次（那要花钱、且改动 feed_loop 语义），暂不做。
    """
    name = source_name(row["source"])
    if name in config.FEED_NOFULLTEXT_SOURCES:
        return True
    body = (row["body"] or "").strip()
    link = row["link"]
    return (not body) and (not link)


def unread_reading_order(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """她「看 inbox」的顺序：各源轮转，谁最新谁先。

    为什么不纯按时间：RSS 是整批入库的，一次抓取最多 10 条全是同一个
    源，纯按时间排，她连着看到的都是同一批——世界在她眼里就只剩一个
    源（2026-11-10 小帕把少数派加进来后立刻撞上：最新 3 条全是知乎）。

    为什么不用随机：随机不保证多样（3 条仍可能同源），也保不住
    「index 0 = 最新」的语义；更要命的是【世界】里的 [n] 编号和
    read_feed 的 index 必须一一对应，两处调用各随机一次就对不上了。

    轮转的规则：源内部新→旧（id 倒序）；源之间按「各自最新一条」排，
    所以 index 0 永远是全场最新，index 1 是另一个源的最新。
    """
    rows = conn.execute("SELECT * FROM inbox WHERE consumed = 0").fetchall()
    groups: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        groups.setdefault(r["source"], []).append(r)
    for g in groups.values():
        # id 递增方向就是时间递增方向（feed_loop 反向插入保证的），
        # 同一批抓取的 ts 完全相同，按 ts 排会退化成随机顺序
        g.sort(key=lambda r: r["id"], reverse=True)
    queues = sorted(groups.values(), key=lambda g: g[0]["id"], reverse=True)

    out = []
    while any(queues):          # 每一轮，从每个还有剩的源各取一条
        for q in queues:
            if q:
                out.append(q.pop(0))
    return out



def _make_req(url: str) -> Request:
    # 不少站点会拦截没有 User-Agent 的请求，当成爬虫直接挂起
    return Request(url, headers={"User-Agent": "fish/0.2"})


def _try_fetch(url: str, mode: str, timeout: float) -> bytes:
    """按指定模式抓一次。mode 决定代理行为，失败抛异常。

    三种 mode：
    "direct"  = ProxyHandler({})，显式清空代理，强制直连
    "system"  = 不构建 opener，urllib 自动读环境变量/系统设置
    "http://…" = 显式代理地址
    """
    if mode == "direct":
        opener = build_opener(ProxyHandler({}))
        resp = opener.open(_make_req(url), timeout=timeout)
    elif mode == "system":
        resp = urlopen(_make_req(url), timeout=timeout)
    else:
        opener = build_opener(ProxyHandler({"http": mode, "https": mode}))
        resp = opener.open(_make_req(url), timeout=timeout)
    try:
        return resp.read()
    finally:
        resp.close()


# 会话内记忆：一旦某档成功，本次运行期间锁定它
_working_mode: str | None = None


def _fetch_rss(url: str, timeout: float = 15.0) -> list[dict]:
    """抓一个 RSS 源，返回 [{title, body}]。含代理降级逻辑（见文件头）。

    故意用标准库 urllib 而不是 httpx/aiohttp：抓取本身丢在
    to_thread 里跑（见 feed_loop），不需要异步 HTTP 客户端。
    """
    global _working_mode

    if _working_mode:
        modes = [_working_mode]                       # 已知好用的通道，直接用
    else:
        modes = []
        if config.FEED_PROXY:
            modes.append(config.FEED_PROXY)           # 显式配置最优先
        modes.append("system")                        # 其次系统默认
        if "direct" not in modes:
            modes.append("direct")                    # 最后兜底直连

    data = None
    last_err: Exception | None = None
    for mode in modes:
        try:
            data = _try_fetch(url, mode, timeout)
            _working_mode = mode                      # 记住它，之后不再试错
            break
        except Exception as e:
            last_err = e
    if data is None:
        raise last_err                                # 全部失败才往外抛

    return _parse(data)


def _parse(data: bytes) -> list[dict]:
    """把 RSS/Atom 的字节流解析成 [{title, body, link}]。纯函数，好测试。

    两种主流格式都要认：
    RSS 2.0 的条目是 <item>，正文在 <description>（全文在 <content:encoded>）
    Atom 的条目是 <entry>（带命名空间），正文在 <content>，<summary> 往往只是摘要

    2026-11-09 的坑：只读 <summary> 的话，阮一峰的周刊正文只有 24 个字
    （「这里记录每周值得分享的科技内容，周五发布。」），而真正的正文
    13KB 在 <content> 里。她「读了」一期周刊，实际什么也没读到。
    2026-11-10 的坑：少数派的 description 只有开头一段 +「查看全文」——
    正文是壳。所以要带上 <link>，让 read_feed 能在她真去读时补抓全文。
    """
    items = []
    root = ET.fromstring(data)

    if root.tag == "rss" or root.find("channel/item") is not None:
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            body = (item.findtext("description") or "").strip()
            for tag in ("{http://purl.org/rss/1.0/modules/content/}encoded",):
                fuller = item.findtext(tag) or ""
                if len(fuller) > len(body):
                    body = fuller.strip()
            link = (item.findtext("link") or "").strip()
            if title or body:
                items.append({"title": title, "body": body, "link": link})
    else:   # Atom：元素名前面带 http://www.w3.org/2005/Atom 命名空间
        ns = "{http://www.w3.org/2005/Atom}"
        for entry in root.iter(f"{ns}entry"):
            title = (entry.findtext(f"{ns}title") or "").strip()
            body = ""
            for tag in (f"{ns}content", f"{ns}summary"):   # content 优先
                got = (entry.findtext(tag) or "").strip()
                if len(got) > len(body):
                    body = got
            # Atom 的链接在属性里不在文本里：<link href="…"/>，
            # 有多个时取第一个（一般就是正文页）
            link_el = entry.find(f"{ns}link")
            link = (link_el.get("href") or "").strip() if link_el is not None else ""
            if title or body:
                items.append({"title": title, "body": body, "link": link})
    return items


async def feed_loop(conn: sqlite3.Connection) -> None:
    if not config.FEED_SOURCES:
        return                                # 源为空：整个循环直接休眠
    while True:
        for entry in config.FEED_SOURCES:
            _, url = _split_source(entry)     # 条目可能是 ("名字", url)，拆开用
            try:
                items = await asyncio.to_thread(_fetch_rss, url)
            except Exception as e:
                print(f"[feed] {url} 抓取失败：{type(e).__name__}: {e}", flush=True)
                continue
            # 顺序很重要，这是 2026-11-09 踩过的坑：
            # RSS / Atom 的惯例是「新的排在前面」，直接按列表顺序插入，
            # 会导致同一批里最新的那条 id 最小 → 「index 0 = 最新」变成
            # 「index 0 = 最旧」。所以先取前 10 条（最新的），再反过来插，
            # 让 id 递增方向 == 时间递增方向。
            for it in reversed(items[:10]):   # 每源每轮最多 10 条，防止刷屏
                # 入库前就剥成纯文本：inbox.body 永远是干净可读的文字。
                # 之前存原始 HTML，读取时才剥——结果知乎热榜的纯图条目
                # （正文就一张 <img>）一路畅通地进了她的世界，
                # 她「读」完什么都没看到（2026-11-10 01:23 的 bug）。
                body = util.plain(it["body"])[:6000]
                conn.execute(
                    """INSERT INTO inbox (ts, source, title, body, link)
                       SELECT ?, ?, ?, ?, ?
                       WHERE NOT EXISTS (SELECT 1 FROM inbox WHERE title = ?)""",
                    (util.now_iso(), url, it["title"], body, it["link"] or None,
                     it["title"]),
                )
                # 顺手自愈：之前解析器只抓到摘要的那几条，下次抓到全文就补上。
                # 去重靠标题，所以正文变长不会被重复插入，只会被就地更新。
                conn.execute(
                    """UPDATE inbox SET body = ?
                       WHERE title = ? AND length(body) < length(?)""",
                    (body, it["title"], body),
                )
                # link 同理自愈：老条目（link 列刚加上时的历史数据）
                # 只要这轮 feed 里还有它，就把链接补上。
                if it["link"]:
                    conn.execute(
                        "UPDATE inbox SET link = ? WHERE title = ? AND link IS NULL",
                        (it["link"], it["title"]),
                    )
            conn.commit()
            print(f"[feed] {url} 抓到 {len(items)} 条（通道：{_working_mode}）", flush=True)
        await asyncio.sleep(config.FEED_INTERVAL_MIN * 60)
