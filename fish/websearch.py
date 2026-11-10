"""联网能力：DeepSeek 的 Search / Fetch API 薄封装。

文档：https://api-docs.deepseek.com/zh-cn/guides/search
POST https://api.deepseek.com/v1/tools/search   按词搜网页，返回结构化结果
POST https://api.deepseek.com/v1/tools/fetch    抓单个网页，返回 Markdown 正文
和聊天 API 用同一个 key、同一个 base URL，按成功请求次数计费
（各为每 1000 次成功请求 5 元；搜索无结果 / 抓不到正文不计费）。

故意用标准库 urllib 而不是 openai SDK：SDK 只包了聊天那些端点，
这两个是独立的 JSON API，自己写请求反而最短。风格与 feed.py 一致。
"""

import json
import urllib.error
import urllib.request

from . import config


class WebSearchError(Exception):
    """一次失败的搜索。message 写成鱼鱼能读懂的句子，直接进 ToolResult.summary。"""


# HTTP 状态码 → 人话。文档明确说：408/429/5xx 可以等一等再试，
# 其它 4xx 重试也是白搭。这里选择不做自动重试——失败会如实反馈给她，
# 要不要再试由她下一次唤醒自己决定，这本身就是观察数据。
_HTTP_REASONS = {
    400: "搜索请求本身不合法（搜索词可能太长或格式不对）",
    401: "API key 无效",
    402: "账号余额不足",
    408: "搜索超时了",
    429: "搜得太频繁，被限速了",
}


def search_web(query: str, count: int = 3) -> list[dict]:
    """搜一次，返回精简后的结果列表。

    只保留要用的字段（title / url / site / date / summary）。API 还会回
    rerank_score、site_icon、authority 之类，但鱼鱼要看的就是「标题、
    出处、时间、说了什么」——多余字段进上下文是白花钱。
    """
    if not config.DEEPSEEK_API_KEY:
        raise WebSearchError("没有配置 API key，搜不了。")

    body = json.dumps({"query": query, "count": count}).encode("utf-8")
    req = urllib.request.Request(
        config.BASE_URL + "/v1/tools/search",
        data=body,
        headers={
            "Authorization": f"Bearer {config.DEEPSEEK_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=config.WEB_SEARCH_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # HTTPError 也是 OSError 的子类，必须放在它前面接，否则读不到状态码
        if e.code in _HTTP_REASONS:
            reason = _HTTP_REASONS[e.code]
        elif e.code >= 500:
            reason = f"搜索服务出错了（HTTP {e.code}）"
        else:
            reason = f"搜索失败（HTTP {e.code}）"
        raise WebSearchError(reason) from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        # URLError / 超时 / 连接重置——全是「路不通」，不是搜索本身的错
        raise WebSearchError(f"网络出了问题（{type(e).__name__}），没搜成。") from None

    out = []
    for r in data.get("results") or []:
        # date 是 UTC 的 RFC 3339（如 2026-11-08T12:00:00+00:00），
        # 只取前 10 个字符当「哪天说的」就够用
        out.append({
            "title": (r.get("title") or "（没标题）").strip(),
            "url": (r.get("url") or "").strip(),
            "site": (r.get("site_name") or "").strip(),
            "date": (r.get("date") or "")[:10],
            "summary": (r.get("summary") or r.get("snippet") or "").strip(),
        })
    return out


def fetch_page(url: str) -> dict:
    """抓一个网页，返回 {"title": …, "content": …}（content 是 Markdown）。

    用途：有些 RSS 源（如少数派）的 description 只给开头一段 +
    「查看全文」链接——inbox 里存的是 teaser，她读到的是壳。
    在她真正点开读的那一刻用这个接口补全文，只为读过的条目花钱。
    """
    if not config.DEEPSEEK_API_KEY:
        raise WebSearchError("没有配置 API key，抓不了网页。")

    body = json.dumps({"url": url}).encode("utf-8")
    req = urllib.request.Request(
        config.BASE_URL + "/v1/tools/fetch",
        data=body,
        headers={
            "Authorization": f"Bearer {config.DEEPSEEK_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=config.WEB_SEARCH_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # HTTPError 也是 OSError 的子类，必须放在它前面接，否则读不到状态码
        if e.code in _HTTP_REASONS:
            reason = _HTTP_REASONS[e.code]
        elif e.code == 403:
            reason = "对方站点不让抓（安全策略拒绝）"
        elif e.code == 404:
            reason = "这页抓不到正文（可能是反爬或纯脚本渲染的页面）"
        elif e.code >= 500:
            reason = f"抓取服务出错了（HTTP {e.code}）"
        else:
            reason = f"抓取失败（HTTP {e.code}）"
        raise WebSearchError(reason) from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise WebSearchError(f"网络出了问题（{type(e).__name__}），没抓成。") from None

    return {
        "title": (data.get("title") or "").strip(),
        "content": (data.get("content") or "").strip(),
    }
