"""pytest 配置：命令行参数、服务可用性检查、用例隔离。

三个 fixture 撑起整套评测：
- client  : 一个极简 HTTP 客户端（纯标准库，不额外引入 requests）
- dataset : 加载并校验评测集
- （用例本身由 test_eval.py 参数化注入）
"""

import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from evals.report import REPORTER

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATASET = PROJECT_ROOT / "data" / "eval_cases.json"


# ---------------------------------------------------------------------------
# 命令行参数
# ---------------------------------------------------------------------------


def pytest_addoption(parser):
    group = parser.getgroup("eval", "香水 Agent 评测")
    group.addoption(
        "--split",
        action="store",
        default="all",
        choices=["all", "tune", "holdout"],
        help="跑哪一部分：all（默认）/ tune / holdout",
    )
    group.addoption(
        "--repeat",
        action="store",
        type=int,
        default=1,
        help="每条用例重复执行次数，用于观察输出稳定性（默认 1）",
    )
    group.addoption(
        "--base-url",
        action="store",
        default="http://127.0.0.1:8000",
        help="被测服务地址（默认 http://127.0.0.1:8000）",
    )
    group.addoption(
        "--dataset",
        action="store",
        default=str(DEFAULT_DATASET),
        help="评测集路径",
    )


def pytest_configure(config):
    # 失败信息已经自己控制成"一段可读文本"，不要让 pytest 再刷一堆堆栈
    config.option.tbstyle = "line"
    config.addinivalue_line("markers", "eval: 香水 Agent 端到端评测用例")


# ---------------------------------------------------------------------------
# HTTP 客户端
# ---------------------------------------------------------------------------


class EvalClient:
    """够用就好的 HTTP 封装，只依赖标准库。"""

    def __init__(self, base_url: str, timeout: int = 120):
        self.base = base_url.rstrip("/")
        self.timeout = timeout

    def _request(self, method: str, path: str, body=None):
        url = f"{self.base}{path}"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                return exc.code, json.loads(raw)
            except json.JSONDecodeError:
                return exc.code, {"detail": raw}

    # -- 业务接口 ---------------------------------------------------------

    def chat(self, user_id: str, message: str) -> dict:
        status, payload = self._request(
            "POST", "/api/chat", {"user_id": user_id, "message": message}
        )
        if status != 200 or payload.get("code") != 0:
            raise AssertionError(f"/api/chat 调用失败：HTTP {status} {payload}")
        return payload.get("data") or {}

    def health(self) -> bool:
        try:
            status, _ = self._request("GET", "/")
            return status == 200
        except Exception:
            return False

    # -- 评测钩子 ---------------------------------------------------------

    def set_profile(self, user_id: str, profile: dict) -> None:
        status, payload = self._request(
            "POST", "/_test/profile", {"user_id": user_id, "profile": profile}
        )
        if status != 200:
            raise AssertionError(f"注入画像失败：HTTP {status} {payload}")

    def clear_profile(self, user_id: str) -> None:
        status, payload = self._request("DELETE", f"/_test/profile/{user_id}")
        if status != 200:
            raise AssertionError(f"清理画像失败：HTTP {status} {payload}")

    def hooks_ready(self) -> bool:
        try:
            status, _ = self._request("GET", "/_test/health")
            return status == 200
        except Exception:
            return False


# ---------------------------------------------------------------------------
# fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def client(request) -> EvalClient:
    base = request.config.getoption("--base-url")
    c = EvalClient(base)

    if not c.health():
        pytest.exit(
            f"\n连不上被测服务 {base}\n"
            f"请先启动：EVAL_HOOKS=1 uvicorn app.main:app --port 8000\n",
            returncode=2,
        )
    if not c.hooks_ready():
        pytest.exit(
            f"\n服务 {base} 在跑，但评测钩子未启用（/_test/health 返回 404）。\n"
            f"评测需要注入初始画像，请用下面的方式启动：\n\n"
            f"  EVAL_HOOKS=1 uvicorn app.main:app --port 8000\n\n"
            f"（PowerShell: $env:EVAL_HOOKS='1'; uvicorn app.main:app --port 8000）\n",
            returncode=2,
        )
    return c


@pytest.fixture(scope="session")
def dataset(request) -> dict:
    path = Path(request.config.getoption("--dataset"))
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    if not path.exists():
        pytest.exit(f"评测集不存在：{path}", returncode=2)
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="session", autouse=True)
def _init_reporter(request, dataset) -> None:
    split = request.config.getoption("--split")
    cases = dataset.get("cases") or []
    expected = cases if split == "all" else [c for c in cases if c.get("split") == split]
    REPORTER.begin(
        meta=dataset.get("meta") or {},
        dataset_path=str(request.config.getoption("--dataset")),
        split=split,
        repeat=request.config.getoption("--repeat"),
        expected_total=len(expected),
    )


# ---------------------------------------------------------------------------
# 结束时的统计表 + 落盘
# ---------------------------------------------------------------------------


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    if not REPORTER.rows:
        return

    terminalreporter.write_sep("=", "评测统计（按类别 × split）")
    for line in REPORTER.render_table().splitlines():
        terminalreporter.write_line(line)

    stability = REPORTER.stability_table()
    if stability:
        terminalreporter.write_line("")
        for line in stability.splitlines():
            terminalreporter.write_line(line)

    declared_total = (REPORTER.meta or {}).get("total")
    declared_split = (REPORTER.meta or {}).get("split") or {}
    actual_total = len(REPORTER.rows)
    actual_split = {s: b["total"] for s, b in REPORTER.totals_by_split().items()}

    terminalreporter.write_line("")
    terminalreporter.write_line(
        f"数据集声明 total={declared_total} {declared_split}｜实际跑 total={actual_total} {actual_split}"
    )

    # 只有"该跑的全都跑了"时，数据集条数才算数；-k 过滤的子集运行不参与比较
    if REPORTER.expected_total and actual_total < REPORTER.expected_total:
        terminalreporter.write_line(
            f"  （本次是 -k 过滤的子集运行，实际 {actual_total}/{REPORTER.expected_total} 条，跳过数据集一致性检查）"
        )
    elif declared_total != actual_total:
        terminalreporter.write_line(
            f"  ⚠ 数据集 meta 与用例条数不一致（声明 {declared_total}，实际 {actual_total}），"
            f"请核对 data/eval_cases.json"
        )

    json_path, md_path = REPORTER.save()
    terminalreporter.write_sep("-", "结果已留档")
    terminalreporter.write_line(f"  {json_path.relative_to(PROJECT_ROOT)}")
    terminalreporter.write_line(f"  {md_path.relative_to(PROJECT_ROOT)}")
