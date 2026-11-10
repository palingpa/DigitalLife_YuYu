"""全部可调参数集中在这里。

原则：任何看起来"拍脑袋定的数"，都必须能在这里找到出处和理由。
出处一律指向 Design_v0.2.md 的对应小节。
"""

import os
from pathlib import Path

# ── 路径 ────────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent      # 项目根目录
DATA_DIR = ROOT / "data"
LOGS_DIR = ROOT / "logs"

# ── 调用轨迹（fish/trace.py）─────────────────────────────────────────────
# 每次 API 调用的完整输入、输出、思考过程都存进 logs/traces/日期.jsonl。
# 关掉它：FISH_TRACE=0。每天约几十条、几 MB，无需担心。
TRACE_ENABLED = os.environ.get("FISH_TRACE", "1") != "0"
TRACE_DIR = LOGS_DIR / "traces"

# ── 大脑（DeepSeek API）─────────────────────────────────────────────────
# 密钥不写进代码，从环境变量读。设置方法见 README。
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
BASE_URL = "https://api.deepseek.com"
MODEL = os.environ.get("FISH_MODEL", "deepseek-flash")
TEMPERATURE = 1.0          # 第一次尝试的温度；重试时会降到 0.3

# 空跑模式：设环境变量 FISH_DRY_RUN=1 则不调 API，只打印上下文、记假决策。
DRY_RUN = os.environ.get("FISH_DRY_RUN", "0") == "1"

# ── 联网搜索（fish/websearch.py，POST /v1/tools/search）─────────────────
# DeepSeek 官方 Search API，和聊天用同一个 key。按成功请求次数计费：
# 每 1000 次成功请求 5 元；搜索无结果不计费；count 不影响单价。
# 文档：https://api-docs.deepseek.com/zh-cn/api/search
WEB_SEARCH_COUNT = 3        # 一次搜索拿几条结果
WEB_SEARCH_TIMEOUT = 15     # 请求超时（秒）
WEB_SEARCH_MAX_QUERY = 128  # API 硬限制：query 最多 128 个 Unicode 字符
# inbox 里正文短于它、且有原文链接的条目，read_feed 会在「她真去读」的
# 那一刻用 Fetch 接口补全文（¥5/1000 次成功请求，只为读过的花钱）。
# 背景：少数派的 RSS 只给开头一段 +「查看全文」，2026-11-10 她连读三条
# 壳，得出「这套 inbox 给我的多是壳」的结论，开始回避 read_feed。
INBOX_FETCH_BELOW = 400

# ── 调度（Design_v0.2 §6）──────────────────────────────────────────────
TICK_SECONDS = 60          # tick 周期：纯本地检查，不调 API，不写库
WAKE_MIN_GAP = 10          # 模型能定的最短再醒间隔（分钟）
WAKE_MAX_GAP = 180         # 最长再醒间隔。上限不是为省钱，是防日志断档
HARD_MAX_GAP = 240         # next_wake_at 缺失/损坏时的兜底间隔
BOREDOM_URGENT = 85        # 无聊度超过它 → 提前醒（憋不住了）
URGENT_COOLDOWN_MIN = 15   # 紧急唤醒两次之间至少隔这么久，防止连环醒

# ── 小帕 ↔ 鱼鱼 通信（2026-11-10 新增）──────────────────────────────────
# 外部消息（小帕 → 鱼鱼）的唤醒最小间隔（分钟）。小帕连发时不让唤醒节奏
# 被打得太碎：消息没被读之前一直留在库里，间隔一到下一 tick 自然触发，
# 不会丢。设 2 分钟是用户拍板的值。
EXTERNAL_WAKE_MIN_GAP = 2
# 与「小帕」相关的记忆统一打这个 tag，方便日后检索「她和小帕之间的事」。
# 同时用于 send_message / 来信记忆的 tags——属于注入鱼鱼记忆的内容，
# 具体措辞待小帕定稿（见 tools/__init__.py 与 prompt.py 的占位符）。
XIAOPA_MSG_TAG = "小帕"

# ── 状态动力学（Design_v0.2 §4，2026-11-10 夜修订）─────────────────────
# energy（精力）改成了纯「电池」模型，不再接受模型自评修正（详见 state.py）：
#   - 只在「做动作」时按动作扣（read/search 费神、write 略费、recall 不费）
#   - 只在「睡觉」（两次唤醒的间隔够长）时按分钟回血
#   - 不再向 baseline 回归、自评 delta 也不碰它——否则会被钉死在 baseline
#     附近，永远走不到「精力很低」，也就永远不会主动去休息。
# 设计意图：让她"累→休息→恢复→再累"真实地循环起来。
#
# 睡眠恢复的三道闸（防滥用，缺一道就变成「心情差就去睡」的最优策略）：
#   1. 只作用于 energy，不碰 valence——睡能解乏，不解心结
#   2. 单次封顶 SLEEP_MAX_BONUS；间隔够长也不无限累加
#   3. 结算完全在服务端：她只能选「下次几点醒」，不能「立刻躺下」（I2）
SLEEP_MIN_MIN = 60             # 间隔 < 60 分钟算打盹，不享受恢复（"睡不到一小时"）
SLEEP_RECOVERY_PER_MIN = 0.15  # 每睡一分钟回复的 energy（按分钟精确结算）
SLEEP_MAX_BONUS = 30.0         # 单次睡眠恢复上限（约略大于一个档位，不会一口气回满）
BASELINE = {"energy": 55.0, "valence": 50.0}   # valence 的回归目标；energy 仅作参考
REGRESS_PER_HOUR = {"valence": 0.03}   # 只有 valence 向 baseline 回归（energy 是电池，不回归）
BOREDOM_PER_MIN = 0.03     # 无聊度每分钟 +0.03 → 一天不做事约 +43
CLAMP = (5.0, 95.0)        # 所有属性永远留在这个区间内，防止贴死 0/100
CUTS = (25, 50, 75)        # 属性分档阈值（energy / boredom 共用）

# 模型自评 delta 的允许范围（-10..+10 的"刻度"，实际除以 10）
FELT_DELTA_RANGE = 10.0

# 第二段调用（反应）喂给她的正文上限（字符）。超过就截断，并在 prompt 里
# 明说「原文更长，你只看到这些」——假装读完比读一半更糟。
REACTION_TEXT_CHARS = 2000

# ── 记忆（Design_v0.2 §5）──────────────────────────────────────────────
SEARCH_TOP_K = 5           # 每次 wake 检索多少条情景记忆
RECALL_SCAN = 8            # recall 工具先捞几条候选（去重后只展示前几条，
                           # 多捞是为了在「前几条全是重复」时还能翻到新的）
RECALL_SHOW = 3            # recall 结果里最多展示几条新的
# recall 的相关性闸门（memory._min_hits）：命中片段不够就别端上来。
# 不设闸门时，一条都不相关的旧记忆能靠 pinned + salience 刷到榜首——
# 2026-11-10 实测：搜「胖东来 企业 原则」，第一名是空跑测试日记。
# 两道条件取较严的那个：
#   RECALL_MIN_HITS——绝对数，日常起决定作用（她习惯用短词查询）
#   RECALL_MIN_MATCH——比例，只在查询特别长时生效，防止一句话靠两个
#                      高频两字组（「什么」「自己」）蒙到一堆不相干的
RECALL_MIN_MATCH = 0.15
RECALL_MIN_HITS = 2
RECENT_EVENT_COUNT = 3     # 最近几条唤醒摘要（空转也算一条）。2 的时候她
                           # 空转两次就会把上次真正做的事挤出版本，故 +1
# 【最近在做的事】每条显示多少字。事实要够长才看得出「做成了什么」；
# 理由则不设上限——契约已把它限在 40 字内，再截一刀她连自己当时
# 想干什么都读不全，连续性反而是被预算省掉的。
RECENT_FACT_CHARS = 90
MEMORY_HALF_LIFE_H = 72.0  # recency 衰减尺度：72 小时前的事权重降到 ~37%
SELF_REVIEW_EVERY = 10     # 每 N 次 wake 做一次核心记忆自省（2026-11-09 改定）
SELF_REVIEW_MIN_RETENTION = 0.6   # 旧自我叙事保留率低于它 → 拒绝写入

# ── 自省叙事长度管理（2026-11-10 新增）────────────────────────────────────
# 自我叙事每次唤醒都会被注入，且历史上只会加长（鱼鱼自己在第 3 次自省里
# 也点破了「这份东西只会越来越长，长成一个可能不是我的形象」）。于是加两层闸：
#   1) 软目标 SELF_REVIEW_TARGET_MAX：只在提示词里暴露给鱼鱼当方向（措辞由小帕定稿），
#      不强制；超过它时放松保留率闸门，允许她往这个长度压。
#   2) 硬上限 SELF_REVIEW_HARD_MAX：安全网。超过则拒绝写入、保留上一版，
#      等她下一轮自行压缩。设得比 target 宽松，正常运营几乎不触发，只挡失控增长。
SELF_REVIEW_TARGET_MAX = 600     # 自我叙事的「软目标」长度（字符）
SELF_REVIEW_HARD_MAX = 1200      # 自我叙事的「硬上限」长度（字符）
# 当旧版已超过 target 时，允许她比平时切得更深（向 target 靠拢），否则保留率
# 闸门会把她钉在「最多砍 40%」而永远压不下来。0.3 是下限，防一把推翻自己。
SELF_REVIEW_MIN_RETENTION_OVER_TARGET = 0.3

# ── 世界输入（Design_v0.2 §13.2）───────────────────────────────────────
FEED_INTERVAL_MIN = 120    # 每 2 小时抓一次信息源
# 源由小帕自己填。条目两种写法都认：
#   "https://example.com/feed"              → 显示名自动取域名
#   ("少数派", "https://sspai.com/feed")     → 显示名用你起的名字
# 显示名会出现在鱼鱼【世界】那一栏里——她看到的是「（少数派）」，
# 而不是一串她读不出意义的 URL。
# 不填也能跑，只是鱼鱼没有东西可看（见 Design_v0.2.md §13.2 的说明）。
FEED_SOURCES: list[str | tuple[str, str]] = [

    # 1. 少数派（科技/数字生活，直连可用）
    ("少数派", "https://sspai.com/feed"),

    # 2. 知乎热榜（热度话题。直连不通，需要代理，见下面 FEED_PROXY）
    ("知乎热榜", "https://zhihu-hot.ayanamist.com/feed.xml"),

    # # 3. 阮一峰周刊（Atom 格式，长文，适合当"慢内容"）
    # ("阮一峰周刊", "https://www.ruanyifeng.com/blog/atom.xml"),

    # 原来的澎湃 https://www.thepaper.cn/rss.xml 已经废了：
    # 返回的是 HTML 页面不是 RSS。想要新闻类源可以自己找替代
    # （RSSHub 上有不少现成的路由），加进这个列表就行。
]

# 【世界】里预览几条标题。排序是「各源轮转」（见 feed.unread_reading_order），
# 所以这几条天然来自不同源——不用额外做什么就能保证她看到的世界是多样的。
INBOX_PEEK = 4

# 抓源用的代理。留空 = 先试系统默认（环境变量/注册表），失败再直连。
# 有些源（比如 zhihu-hot.ayanamist.com）直连不通；浏览器能打开是因为
# 浏览器走了代理工具，而终端里的 Python 没继承。如果你用 Clash 之类，
# 把本地端口填进来即可，例如 "http://127.0.0.1:7890"。
# 也可以用环境变量 FISH_FEED_PROXY 指定，不用改代码。
FEED_PROXY = os.environ.get("FISH_FEED_PROXY", "http://127.0.0.1:7890")
