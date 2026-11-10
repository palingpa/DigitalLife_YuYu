"""零碎的通用工具函数。"""

import html
import re
from datetime import datetime

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")


def plain(text: str) -> str:
    """把 HTML 变成人读的纯文本。抓取入库和阅读时都要过这一道。

    RSS 的 description 字段通常是一整段 HTML：<p>、<a href>、&quot; 全在
    里面。不洗掉的话，她「读」到的是源码而不是内容——2026-11-09 的空跑里，
    她读到的第一条就是 `<p>财联社11月8日讯 当AI基建浪潮本周深陷&quot;中期
    选举焦虑&quot;之际`。这种输入下，"读过"跟"没读过"没区别。
    """
    text = _TAG_RE.sub(" ", text or "")
    text = html.unescape(text)              # &quot; &amp; &#39; → 真正的字符
    text = text.replace("\u3000", " ")
    text = _WS_RE.sub(" ", text)
    return text.strip()

def now() -> datetime:
    """本地当前时间（不含时区信息）。

    为什么不用 UTC？这个项目的一切观察都以"鱼鱼的当地时间"为准，
    日志里存本地时间最直观，省得每次看日志都做时区换算。
    """
    return datetime.now()

def now_iso() -> str:
    """统一的时间戳格式，存库用。精确到秒。"""
    return now().isoformat(timespec="seconds")

def parse_iso(s: str) -> datetime:
    """和 now_iso 配对的解析函数。"""
    return datetime.fromisoformat(s)

def hours_between(later: datetime, earlier: datetime) -> float:
    """两个时刻相差多少小时（later 应晚于 earlier）。"""
    return (later - earlier).total_seconds() / 3600.0
