"""参数化测试主体：一条用例 = 一个 pytest item。

每条用例的生命周期（严格隔离，避免串味）：
  清理 eval_{id} 的画像
    → 注入 profile_before（如果有）
      → POST /api/chat 发 input
        → 三类断言
          → 清理画像

任何一条用例失败只打印"一段"可诊断文本，不刷堆栈。
"""

import json
from pathlib import Path

import pytest

from evals.assertions import evaluate, render_failure
from evals.report import REPORTER

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_cases(config) -> list[dict]:
    """读评测集并按时 split 过滤。

    刻意不依赖 conftest 的 fixture：pytest_generate_tests 阶段 fixture 还不可用。
    """
    path = Path(config.getoption("--dataset"))
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    dataset = json.loads(path.read_text(encoding="utf-8"))
    cases = dataset.get("cases") or []
    split = config.getoption("--split")
    if split != "all":
        cases = [c for c in cases if c.get("split") == split]
    return cases


def pytest_generate_tests(metafunc):
    if "case" in metafunc.fixturenames:
        cases = _load_cases(metafunc.config)
        metafunc.parametrize("case", cases, ids=[c.get("id", "?") for c in cases])


@pytest.mark.eval
def test_case(client, case, request):
    repeat = max(1, int(request.config.getoption("--repeat")))
    user_id = f"eval_{case['id']}"

    pass_count = 0
    first_problems: list[str] = []
    last_data: dict = {}

    try:
        for _ in range(repeat):
            # 1. 测前清理，保证这条用例从干净画像开始
            client.clear_profile(user_id)

            # 2. 前置画像必须真的写进去，不能靠"把条件塞进 input"糊弄
            before = case.get("profile_before") or {}
            if before:
                client.set_profile(user_id, before)

            # 3. 发真实请求
            try:
                last_data = client.chat(user_id, case["input"])
            except Exception as exc:  # 接口挂了也算用例失败，不炸整轮
                first_problems = [f"请求异常：{exc}"]
                break

            # 4. 断言
            passed, problems = evaluate(case, last_data)
            if passed:
                pass_count += 1
            elif not first_problems:
                first_problems = problems
    finally:
        # 5. 测后回收
        try:
            client.clear_profile(user_id)
        except Exception:
            pass

    REPORTER.record(case, pass_count, repeat, first_problems)

    if pass_count < repeat:
        pytest.fail(render_failure(case, last_data, first_problems), pytrace=False)
