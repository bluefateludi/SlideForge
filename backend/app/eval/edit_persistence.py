"""编辑报告独立口径：未知用量保存 null，不使用生成指标或基线。"""

from __future__ import annotations

from dataclasses import asdict

from app.eval.edit_cases import EditCase, compute_edit_cases_version
from app.eval.edit_runner import EditCaseArtifacts
from app.models.eval_run import EvalRun


def build_edit_run(cases: list[EditCase], artifacts: list[EditCaseArtifacts]) -> EvalRun:
    if not cases or [case.id for case in cases] != [item.case_id for item in artifacts]:
        raise ValueError("编辑报告题目与结果不一致")
    total = len(artifacts)
    passed = sum(item.ok for item in artifacts)
    rows = [
        {**asdict(item), "category": "edit", "edit_sample": case.model_dump(mode="json")}
        for case, item in zip(cases, artifacts, strict=True)
    ]

    def measured(name, *, mean):
        values = [getattr(item, name) for item in artifacts]
        if any(value is None for value in values):
            return None
        return sum(values) / total if mean else sum(values)

    return EvalRun(
        cases_version=compute_edit_cases_version(cases),
        total_cases=total,
        ok_cases=passed,
        success_rate=passed / total,
        failure_rate=1 - passed / total,
        schema_valid_cases=0,
        schema_scored_cases=0,
        schema_valid_rate=0,
        pages_met_cases=0,
        pages_scored_cases=0,
        pages_met_rate=0,
        export_succeeded_cases=0,
        avg_judge_coverage=None,
        avg_judge_score=None,
        judge_failed_cases=0,
        doc_total_numbers=0,
        doc_fabricated_numbers=0,
        hallucination_rate=None,
        avg_elapsed_seconds=sum(item.elapsed_seconds for item in artifacts) / total,
        avg_prompt_tokens=measured("prompt_tokens", mean=True),
        avg_completion_tokens=measured("completion_tokens", mean=True),
        avg_cost=measured("cost", mean=True),
        total_cost=measured("cost", mean=False),
        total_failed_slides=0,
        total_slides=0,
        retry_slide_rate=None,
        total_retried_slides=0,
        status="completed",
        rows=rows,
        category_scores={
            "edit": {
                "total_cases": total,
                "ok_cases": passed,
                "avg_judge_score": None,
                "success_rate": passed / total,
            }
        },
        note="固定页面 AI 编辑评测：程序断言，独立于生成评测和生成基线",
    )
