"""run_eval 冒烟预检（脚本层）：全量起跑前先跑 1 道最短主题题。

scripts/ 不是包，用 importlib 按文件路径加载；_run_with_smoke 只依赖
runner.run(cases) → list[CaseArtifacts] 的窄接口，用桩 runner 验证：
通过后余题不漏跑且顺序归位、未通过时立即中止不跑全量、EVAL_SKIP_SMOKE 逃生口。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from app.eval.cases import EvalCase

_MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "run_eval.py"


def _load_run_eval():
    spec = importlib.util.spec_from_file_location("run_eval_under_test", _MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _case(case_id: str, pages: int) -> EvalCase:
    # id 前缀必须与 category 一致（cases.py 的加载校验）
    return EvalCase(
        id=case_id,
        input=f"生成一份测试的 {pages} 页 PPT",
        expected_pages=pages,
        requirements=["包含测试要求"],
        category=case_id.split("/")[0],
    )


class _StubRunner:
    """按 case_id 预设成败的桩执行器，记录每次 run 的题序。"""

    def __init__(self, outcomes: dict[str, bool]) -> None:
        self._outcomes = outcomes
        self.runs: list[list[str]] = []

    async def run(self, cases: list[EvalCase]) -> list:
        from app.eval.runner import CaseArtifacts

        self.runs.append([case.id for case in cases])
        return [
            CaseArtifacts(
                case_id=case.id,
                ok=self._outcomes.get(case.id, True),
                stage="done" if self._outcomes.get(case.id, True) else "outline_generate",
                error=None if self._outcomes.get(case.id, True) else "RuntimeError: 冒烟失败演练",
            )
            for case in cases
        ]


def _three_cases() -> list[EvalCase]:
    # business/a（6 页）是页数最少的主题题 → 冒烟题
    return [
        _case("business/a", 6),
        _case("technology/b", 10),
        _case("documents/c", 5),
    ]


class TestRunWithSmoke:
    async def test_smoke_pass_then_rest_runs_and_order_restored(self) -> None:
        run_eval = _load_run_eval()
        runner = _StubRunner({})
        cases = _three_cases()

        artifacts = await run_eval._run_with_smoke(runner, cases)

        # 冒烟单独先跑，余题（含文档题）一次跑完，全量不重跑冒烟题；
        # 余题保持传入顺序（load_cases 已按 id 排序）
        assert runner.runs == [["business/a"], ["technology/b", "documents/c"]]
        # 返回结果按传入 cases 的顺序归位，与题集 zip 时一一对应
        assert [item.case_id for item in artifacts] == [case.id for case in cases]
        assert all(item.ok for item in artifacts)

    async def test_smoke_failure_aborts_before_full_run(self) -> None:
        run_eval = _load_run_eval()
        runner = _StubRunner({"business/a": False})

        artifacts = await run_eval._run_with_smoke(runner, _three_cases())

        assert artifacts is None  # 中止信号：不跑全量、不产出报告
        assert runner.runs == [["business/a"]]

    async def test_skip_smoke_env_runs_all_at_once(self, monkeypatch) -> None:
        run_eval = _load_run_eval()
        monkeypatch.setenv("EVAL_SKIP_SMOKE", "1")
        runner = _StubRunner({})
        cases = _three_cases()

        artifacts = await run_eval._run_with_smoke(runner, cases)

        assert runner.runs == [[case.id for case in cases]]
        assert [item.case_id for item in artifacts] == [case.id for case in cases]
