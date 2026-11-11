"""测试共用 fixture：把测试产物从真实日志隔离出去。

为什么：测试会走到 brain / scheduler 里的 trace.record（写 logs/traces/日期.jsonl），
空跑模式还会把提示词写 logs/last_prompt.txt。这些测试产物和真机用的是同一种
12 位 hex trace_id、同样会误导观测——测试跑完真实日志里就混进了脏数据（这正是
小帕要「区分」的原因）。这里用 autouse 的 session fixture 在 pytest 启动时切到
logs/traces_test/，所有测试自动生效，不用每个测试文件单独改——和现有「每个测试
monkeypatch DB_PATH 到临时库」的隔离思路一致（只是日志层在 conftest 里集中处理）。

不在此清理 traces_test/：它和真实日志完全独立，保留便于排查；要清就整目录删。
"""
import pytest

from fish import trace


@pytest.fixture(autouse=True, scope="session")
def _isolate_test_traces():
    trace.set_test_mode(True)
    yield
    # 不重置、不清理：pytest 进程结束即止；测试轨迹留在 logs/traces_test/，
    # 与真实日志互不影响。
