# TESTING.md — 测试说明（写给没用过 pytest 的你）

> **维护约定：加/删测试文件、改测试跑法时同步更新本文件。**

## pytest 是什么？30 秒版

pytest 是一个"自动检查工"。你把检查规则写成普通 Python 函数，函数名以 `test_` 开头，里面用 `assert` 写下"我预期是什么"。pytest 会把所有这样的函数找出来挨个跑：

- `assert` 后面的条件成立 → 这个测试**过**（pytest 显示一个绿点）
- 条件不成立 → 这个测试**挂**，pytest 会打印出是哪一行、实际值是什么

所以"写测试"其实就是写普通函数 + 多写几个 assert，没有魔法。

## 怎么跑（就在项目根目录）

```bash
python -m pytest tests/ -q          # 跑全部测试，-q = 安静模式，只报结果
python -m pytest tests/test_state.py -q      # 只跑一个文件
python -m pytest tests/test_state.py::test_valence_tiers_match_her_actual_range -q   # 只跑一个测试
python -m pytest tests/ -v          # 详细模式，每个测试的名字都列出来
```

项目根目录的 `pytest.ini` 只做一件事：指定测试都在 `tests/` 里。
这是必须的——否则 `backups/` 里的旧测试快照会被一起收集，撞名报错。

**绿 vs 红**：全过显示 `51 passed`；有挂的会列出 FAILED 和失败原因，**先读它打印的 assert 信息**，多半一眼能看懂。

## 8 个测试文件分别测什么

| 文件 | 测的是 | 典型场景 |
|---|---|---|
| `test_state.py` | **身体动力学**（最重要的一组）：回归 baseline、boredom 只涨不跌、模型 delta 除以 10、模型碰不到 boredom、解闷边际递减、睡眠恢复三道闸、分档读数 | 动了 `state.py` 或 `config.py` 的动力学数字 |
| `test_scheduler.py` | 闹钟计算 `_next_due`：自省/错误这些"旁支行"不能抢闹钟 | 动了 `scheduler.py` 的唤醒判断 |
| `test_memory_search.py` | 检索的相关性闸门：不相关的记忆不许靠置顶刷榜、空格是词边界、单字查询能命中 | 动了 `memory.py` 的检索/打分 |
| `test_recall_dedup.py` | recall 去重：这轮已经看过的记忆不重复放送、全重复时如实说没有新的 | 动了 `tools._run_recall` 或 prompt 的上下文组装 |
| `test_inbox_diversity.py` | inbox 各源轮转排序、【世界】的 [n] 编号和 read_feed 同序、源显示名 | 动了 `feed.unread_reading_order` 或【世界】的渲染 |
| `test_fulltext_fetch.py` | RSS 解析带出 link、旧库补 link 列、读时抓全文、抓不到如实说明、DRY_RUN 不花钱 | 动了 `feed._parse`、`db._migrate`、`tools._run_read_feed` |
| `test_web_search.py` | web_search 的参数校验、结果排版、预算平分、报错诚实、DRY_RUN 不发请求、契约枚举覆盖所有工具 | 加新工具（最后两个测试是通用的，必看） |
| `test_prompt_reflection.py` | "事后想法"不能被读成"写日记"：渲染用"心里想"、write_journal 说明要点明区别 | 改 `prompt.py` 的反思渲染或工具 desc 文案 |

## 我改了 X，要跑什么？（速查表）

拿不准就全跑，反正 2 秒钟：`python -m pytest tests/ -q`

| 你改了 | 必须跑 | 特别注意 |
|---|---|---|
| `state.py` | 全部 | 这里的错是**静默的**，数据照记但全是废的 |
| `config.py` 的动力学数字 | `test_state.py` | 很多数字有配套断言（如睡眠三道闸） |
| `prompt.py` 文案 | 全部 | 见下面"红了怎么办"第 2 条 |
| `scheduler.py` | `test_scheduler.py` + 全部 | |
| `memory.py` | `test_memory_search.py` + `test_recall_dedup.py` | |
| `tools/__init__.py` | `test_recall_dedup.py` / `test_fulltext_fetch.py` / `test_web_search.py`（按动的工具选） | 新工具记得把契约枚举测试跑过 |
| `feed.py` | `test_inbox_diversity.py` + `test_fulltext_fetch.py` | |
| `db.py` 表结构 | 全部 + 手动开一次真库确认没报 "no such column" | |

## 测试里反复出现的三样东西（看懂就能自己写）

**1. `conn` fixture——每人一个干净的假数据库**

```python
@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")   # 把库的路径改到临时目录
    c = db.connect()          # 在那儿建一个真 DDL 的新库
    db.init_state(c)
    return c
```

大白话：`fixture` 是"每个测试开始前自动准备的道具"。这里是"一个建在临时文件夹、跑完就扔的空数据库"——**永远不碰 `data/fish.db` 里鱼鱼的真实记忆**。`tmp_path` 和 `monkeypatch` 都是 pytest 自带的：前者是本次测试专属的临时目录，后者是"偷偷把某个东西换成假的"的工具。

**2. `monkeypatch` 换假函数——测试不出网、不花钱**

```python
def fake(url):
    calls.append(url)          # 顺便记一笔"它真的被调用了"
    return {"title": "页标题", "content": "假装抓到的全文"}
monkeypatch.setattr(websearch, "fetch_page", fake)
```

大白话：测试里凡是会调 API、访问网络的，都换成假函数。`calls` 列表是为了防"假绿"——万一代码根本没调 fetch_page，断言 `calls == [...]` 也能抓住。

**3. `make_state`——造一个已知数值的身体**

`test_state.py` 里的 `make_state(valence=55.45)`：造一个初始值已知的 State，方便精确断言"这步之后应该变成多少"。

## 写一个新测试的模板

```python
"""一句话说明这组测试防的是什么回归。"""
import pytest
from fish import db, tools, state

@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    c = db.connect()
    db.init_state(c)
    return c

def test_某行为(conn):
    # 1. 准备：往假库里塞数据 / 造状态 / 换假函数
    # 2. 动作：调用要测的函数（如 tools.dispatch(...)）
    # 3. 断言：assert 结果和预期一致
```

好测试的三个标准（现有测试都在遵守）：
- **测行为不测实现**：断言"输出里有'不是你漏看了'"，而不是断言"调了哪行代码"。
- **防回归**：每个测试文件开头都写着它当年抓的是哪个 bug。你修了一个 bug，就补一个让它永远抓得到的测试。
- **不碰真库、不出网**。

## 测试红了怎么办（决策树）

1. **先读 pytest 打印的信息**：哪个文件哪个函数、assert 的是什么、实际值是什么。
2. 问自己：是**代码错了**还是**规格变了**？
   - 代码错了（比如算错数、忘了 clamp）→ 修代码。
   - 规格变了（你故意改了文案/规则）→ 同步改测试的断言。**先改代码还是先改测试，取决于哪边代表新意图**。已发生两次的真实案例：felt_text 文案改成"状态读数"风格、write_journal 的 desc 被简化——两次都是"测试代表的旧规格是对的，代码改动把设计意图弄丢了"，所以以测试为准修代码。改之前想清楚这边。
3. 挂的不止一个且看不出共同点 → 可能是你改崩了公共依赖（比如 db 的 schema），回到上一次 commit 用 `git diff` 对照。
4. 实在找不到 → `git stash` 或回滚，别让红的过夜。

## 硬规矩（收工仪式的一部分）

- 动了 `state.py`、`tests/` 或任何有对应测试的行为 → `python -m pytest tests/ -q` 全绿才能 commit。
- 测试全绿 ≠ 没问题，它只保证**已知会坏的地方没坏**。真正的验收还是看鱼鱼自己的轨迹（`python -m fish.trace 3`）。
