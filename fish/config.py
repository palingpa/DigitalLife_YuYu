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
# 测试轨迹单独目录：pytest 运行时由 tests/conftest.py 切到这里，
# 绝不进 logs/traces/，避免测试数据污染真实日志。测试触发的 record 用的是
# 和真机一样的 12 位 hex trace_id，靠 trace_id 无法区分，只能靠目录隔离。
TRACE_TEST_DIR = LOGS_DIR / "traces_test"

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

# ── 小帕 ↔ 鱼鱼 通信（2026-11-10 新增）──────────────────────────────────
# 外部消息（小帕 → 鱼鱼）的唤醒最小间隔（分钟）。小帕连发时不让唤醒节奏
# 被打得太碎：消息没被读之前一直留在库里，间隔一到下一 tick 自然触发，
# 不会丢。设 2 分钟是用户拍板的值。
EXTERNAL_WAKE_MIN_GAP = 2
# 与「小帕」相关的记忆统一打这个 tag，方便日后检索「她和小帕之间的事」。
# 同时用于 send_message / 来信记忆的 tags——属于注入鱼鱼记忆的内容，
# 具体措辞待小帕定稿（见 tools/__init__.py 与 prompt.py 的占位符）。
XIAOPA_MSG_TAG = "小帕"

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

# ── 工作记忆（2026-11-12 新增，Design_v0.3 §5.5 M0）──────────────────────
# 一个「给之后醒来的自己」的短提示：它存的不是发生过的事，是还没做完的事。
# 独立单条槽（working_note 表），刻意不进 memories：不参与检索打分、不进自省
# 素材、也不受 I1 管辖（I1 管的是 memories 表；空转轮恰恰最需要给下一轮留话）。
# 为什么必须有硬 TTL：契约里「最多安排 3 轮」只是软约束，模型不会可靠遵守
# （why 说好 40 字、core 说好 600 字，最后都靠代码兜底）。真正保证不会留下
# 一份永远清不掉的僵尸计划的，是这里的计数，不是措辞。
WORKING_NOTE_TTL = 3            # 写下后最多被之后 3 次 wake 读到，归零自动删
WORKING_NOTE_MAX_CHARS = 200    # 硬上限。超了拒绝写入，不截断：截断会造出半句话
# 下面这个标签会拼进 prompt 的区块头，属于注入鱼鱼的自然语言，【小帕定稿】。
# 当前是占位：一眼可辨为占位、不含任何语义引导，等小帕换成正式措辞后真机
# 才会稳定使用这个字段。Kite 只留位置，不代笔。
WORKING_NOTE_LABEL = "{占位：给之后醒来的你，待小帕定稿}"

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
# 自省时，从 messages 表取最近多少条双向往来作为「你认识了谁」的素材（grounding）。
# 不为 0 即启用；关系叙事 v1 无长度闸门，只靠提示词劝简短。
REVIEW_RELATION_EXCHANGES = 20

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

# 「抓不回原文」的源名单（用 display name，见上面 FEED_SOURCES 起的名字）。
# 这些源只给标题/问题流（如知乎热榜），链接指向问题页、补抓基本失败，
# 在【世界】预览时直接标「无正文」，让鱼鱼别把它们当成能读的长文。
# 想让哪些源「标无正文」，往这个集合里加名字即可（和 FEED_SOURCES 一起维护）。
FEED_NOFULLTEXT_SOURCES: set[str] = {
    "知乎热榜",
}
# 【世界】里标在「抓不回原文」条目前的记号。自然语言，【小帕定稿】——
# 把下面这行换成你定的最终措辞（例如你示例里的「（*无正文*）」）。
INBOX_NOFULLTEXT_MARK = "(无正文)"

# 【你是谁】板块里展示「上次自省」的措辞。自然语言，【小帕定稿】——
# 把下面两行换成你定的最终文案（例如标签用「上次自省：」、没有自省记录时
# 用「你还没正经做过一次自省」之类）。时间部分由代码用 _relative_time 拼，
# 与【最近在做的事】的「x小时前」质感一致，无需你手写。
SELF_REVIEW_LAST_LABEL = "\n上一次自省的时间："
SELF_REVIEW_LAST_NONE = " "

# 鱼鱼主动选「idle（休息）」时，写进【最近在做的事】的那条事实文案。
# 自然语言，【小帕定稿】——把下面占位符换成你定的最终措辞（例如「主动休息了一轮」）。
IDLE_FACT = "主动休息了一轮"

# 抓源用的代理。留空 = 先试系统默认（环境变量/注册表），失败再直连。
# 有些源（比如 zhihu-hot.ayanamist.com）直连不通；浏览器能打开是因为
# 浏览器走了代理工具，而终端里的 Python 没继承。如果你用 Clash 之类，
# 把本地端口填进来即可，例如 "http://127.0.0.1:7890"。
# 也可以用环境变量 FISH_FEED_PROXY 指定，不用改代码。
FEED_PROXY = os.environ.get("FISH_FEED_PROXY", "http://127.0.0.1:7890")
