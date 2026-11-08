"""统计与报告生成。

一次评测跑完要回答两个问题：
1. 哪些类别在掉分？（所以必须按 category × split 分组，总通过率没有信息量）
2. 这次比上次好还是差？（所以结果要落盘成可比对的文件，带数据集版本和 commit）
"""

import json
import subprocess
import time
from pathlib import Path

RESULTS_DIR = Path(__file__).resolve().parent / "results"


def _git_commit() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).resolve().parent.parent,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


class Collector:
    """收集每条用例的结果，最后统一出表 + 落盘。

    用单例（REPORTER）是因为 pytest 的 fixture 和 hook 分散在不同文件里，
    共享一个收集器比层层传参简单。
    """

    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.meta: dict = {}
        self.dataset_path: str = ""
        self.split: str = "all"
        self.repeat: int = 1
        self.expected_total: int = 0
        self.started_at: str = ""

    def begin(
        self,
        meta: dict,
        dataset_path: str,
        split: str,
        repeat: int,
        expected_total: int = 0,
    ) -> None:
        self.__init__()
        self.meta = meta or {}
        self.dataset_path = dataset_path
        self.split = split
        self.repeat = repeat
        self.expected_total = expected_total
        self.started_at = time.strftime("%Y-%m-%d %H:%M:%S")

    def record(
        self,
        case: dict,
        pass_count: int,
        runs: int,
        failures: list[str],
    ) -> None:
        self.rows.append(
            {
                "id": case.get("id"),
                "category": case.get("category"),
                "split": case.get("split"),
                "input": case.get("input"),
                "pass_count": pass_count,
                "runs": runs,
                "passed": pass_count == runs,
                "failures": failures,
            }
        )

    # ------------------------------------------------------------------
    # 统计
    # ------------------------------------------------------------------

    def category_order(self) -> list[str]:
        """按数据集里的类别声明顺序排列，而不是字典序。"""
        declared = self.meta.get("categories") or []
        seen = []
        for c in declared:
            if c not in seen:
                seen.append(c)
        for row in self.rows:
            if row["category"] not in seen:
                seen.append(row["category"])
        return seen

    def split_columns(self) -> list[str]:
        if self.split != "all":
            return [self.split]
        declared = (self.meta.get("split") or {}).keys()
        cols = [s for s in declared if s in ("tune", "holdout")]
        for row in self.rows:
            if row["split"] not in cols:
                cols.append(row["split"])
        return cols or ["all"]

    def by_category_split(self) -> dict:
        stats: dict[str, dict[str, dict[str, int]]] = {}
        for row in self.rows:
            cat = row["category"]
            sp = row["split"]
            bucket = stats.setdefault(cat, {}).setdefault(sp, {"passed": 0, "total": 0})
            bucket["total"] += 1
            if row["passed"]:
                bucket["passed"] += 1
        return stats

    def totals_by_split(self) -> dict:
        totals: dict[str, dict[str, int]] = {}
        for row in self.rows:
            b = totals.setdefault(row["split"], {"passed": 0, "total": 0})
            b["total"] += 1
            if row["passed"]:
                b["passed"] += 1
        return totals

    # ------------------------------------------------------------------
    # 渲染
    # ------------------------------------------------------------------

    def render_table(self) -> str:
        cols = self.split_columns()
        stats = self.by_category_split()
        cats = self.category_order()

        header = "类别".ljust(20) + "".join(c.ljust(12) for c in cols)
        lines = [header]
        for cat in cats:
            cells = ""
            for c in cols:
                b = stats.get(cat, {}).get(c, {"passed": 0, "total": 0})
                cells += f"{b['passed']}/{b['total']}".ljust(12)
            lines.append(cat.ljust(20) + cells)

        totals = self.totals_by_split()
        line = "-" * (20 + 12 * len(cols))
        total_cells = "".join(
            f"{totals.get(c, {'passed': 0, 'total': 0})['passed']}/{totals.get(c, {'passed': 0, 'total': 0})['total']}".ljust(12)
            for c in cols
        )
        lines.append(line)
        lines.append("合计".ljust(20) + total_cells)
        return "\n".join(lines)

    def stability_table(self) -> str:
        """--repeat>1 时，输出每条用例的通过次数，看模型输出稳不稳。"""
        if self.repeat <= 1:
            return ""
        lines = ["各用例重复通过次数（观察稳定性）："]
        for row in sorted(self.rows, key=lambda r: r["id"]):
            if row["passed"]:
                flag = ""
            elif row["pass_count"] == 0:
                flag = "  ← 稳定失败"
            else:
                flag = "  ← 不稳定（时好时坏）"
            lines.append(f"  {row['id']}: {row['pass_count']}/{row['runs']}{flag}")
        return "\n".join(lines)

    def render_markdown(self) -> str:
        cols = self.split_columns()
        stats = self.by_category_split()
        totals = self.totals_by_split()

        lines = [
            f"# 评测报告 {self.started_at}",
            "",
            f"- 数据集：`{self.dataset_path}`（版本 {self.meta.get('version', 'unknown')}）",
            f"- split：`{self.split}`　repeat：`{self.repeat}`",
            f"- commit：`{_git_commit()}`",
            "",
            "## 分组通过率",
            "",
            "| 类别 | " + " | ".join(cols) + " |",
            "|---|" + "---|" * len(cols),
        ]
        for cat in self.category_order():
            cells = []
            for c in cols:
                b = stats.get(cat, {}).get(c, {"passed": 0, "total": 0})
                cells.append(f"{b['passed']}/{b['total']}")
            lines.append(f"| {cat} | " + " | ".join(cells) + " |")

        total_cells = []
        for c in cols:
            b = totals.get(c, {"passed": 0, "total": 0})
            total_cells.append(f"**{b['passed']}/{b['total']}**")
        lines.append(f"| **合计** | " + " | ".join(total_cells) + " |")

        failed = [r for r in self.rows if not r["passed"]]
        lines += ["", f"## 失败用例（{len(failed)} 条）", ""]
        if not failed:
            lines.append("无。")
        for row in failed:
            lines.append(f"### {row['id']} · {row['category']} · {row['split']}")
            lines.append("")
            lines.append(f"- 输入：{row['input']}")
            lines.append(f"- 通过次数：{row['pass_count']}/{row['runs']}")
            for f in row["failures"]:
                lines.append(f"- 问题：{f}")
            lines.append("")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 落盘
    # ------------------------------------------------------------------

    def save(self) -> tuple[Path, Path]:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        version = str(self.meta.get("version", "unknown")).replace(".", "")
        stamp = time.strftime("%Y%m%d_%H%M%S")
        base = RESULTS_DIR / f"{stamp}_{version}"

        payload = {
            "started_at": self.started_at,
            "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "dataset": self.dataset_path,
            "dataset_version": self.meta.get("version"),
            "commit": _git_commit(),
            "split": self.split,
            "repeat": self.repeat,
            "declared": {
                "total": self.meta.get("total"),
                "split": self.meta.get("split"),
            },
            "actual_total": len(self.rows),
            "by_category_split": self.by_category_split(),
            "totals_by_split": self.totals_by_split(),
            "cases": self.rows,
        }
        json_path = base.with_suffix(".json")
        md_path = base.with_suffix(".md")
        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        md_path.write_text(self.render_markdown(), encoding="utf-8")
        return json_path, md_path


REPORTER = Collector()
