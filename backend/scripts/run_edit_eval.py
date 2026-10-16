#!/usr/bin/env python3
"""ai_edit 工具环评测入口（obs/10）：make eval-edit。

单题 = 生成一个真实小项目 → 锁块（按题面）→ ai-edit 提案 → 纯规则断言。
断言打在提案层（Agent 环的全部决策输出）；apply 属于人工审批边界，不在射程。
结果打印报告并写入 eval_runs（cases_version 前缀 edit-，note 标注），
/eval 列表页与主评测共用一套展示。

用法（仓库根目录，需 dev 栈跑着）：
  make eval-edit
环境变量：EVAL_API_BASE / EVAL_SKIP_DB（与 run_eval 同义）。
注意：真实跑批会驱动 deck 生成——若 Worker 配置了生图凭证会计费，
请先确认再跑。

退出码：0 正常（含有题未过）；1 入库失败；2 环境性错误。
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import httpx  # noqa: E402

from app.eval.edit_cases import compute_edit_cases_version, load_edit_cases  # noqa: E402
from app.eval.edit_runner import EditEvalRunner  # noqa: E402

DEFAULT_API_BASE = "http://127.0.0.1:39800/api/v1"


def _base_url() -> str:
    api_base = os.environ.get("EVAL_API_BASE", DEFAULT_API_BASE).rstrip("/")
    return api_base if api_base.endswith("/api/v1") else api_base + "/api/v1"


def _format_report(artifacts) -> str:
    lines = ["=== ai_edit 工具环评测（obs/10）===", ""]
    passed = sum(1 for item in artifacts if item.ok)
    lines.append(f"题数：{len(artifacts)}　通过：{passed}（{passed / len(artifacts):.0%}）")
    lines.append("")
    for item in artifacts:
        flag = "✅" if item.ok else "❌"
        lines.append(
            f"{flag} {item.case_id}　{item.operations_count} 个操作　"
            + f"{item.elapsed_seconds:.0f}s"
            + (f"　@{item.stage}：{item.error}" if item.error else "")
        )
        for check in item.checks:
            mark = "·" if check["passed"] else "×"
            detail = f"（{check['detail']}）" if check["detail"] else ""
            lines.append(f"    {mark} {check['name']}{detail}")
    lines.append("")
    lines.append("口径：断言打在 ai-edit 提案层（op 类型/数量/关键词/locked 零误改），")
    lines.append("apply 属人工审批边界，不在评测射程。")
    return "\n".join(lines) + "\n"


async def _persist(cases, artifacts) -> int:
    if os.environ.get("EVAL_SKIP_DB", "").strip():
        print("[edit-eval] EVAL_SKIP_DB 已设置，跳过入库", file=sys.stderr)
        return 0
    from datetime import UTC, datetime

    from app.core.db import async_session_factory
    from app.models.eval_run import EvalRun

    passed = sum(1 for item in artifacts if item.ok)
    total = len(artifacts)
    rows = [
        {
            "case_id": item.case_id,
            "category": "edit",
            "ok": item.ok,
            "stage": item.stage,
            "error": item.error,
            "operations_count": item.operations_count,
            "locked_ids": item.locked_ids,
            "checks": item.checks,
            "elapsed_seconds": item.elapsed_seconds,
        }
        for item in artifacts
    ]
    run = EvalRun(
        cases_version=compute_edit_cases_version(cases),
        total_cases=total,
        ok_cases=passed,
        success_rate=passed / total if total else 0.0,
        failure_rate=1 - passed / total if total else 0.0,
        schema_valid_cases=0,
        schema_scored_cases=0,
        schema_valid_rate=0.0,
        pages_met_cases=0,
        pages_scored_cases=0,
        pages_met_rate=0.0,
        export_succeeded_cases=0,
        avg_judge_coverage=None,
        avg_judge_score=None,
        judge_failed_cases=0,
        doc_total_numbers=0,
        doc_fabricated_numbers=0,
        hallucination_rate=None,
        avg_elapsed_seconds=(
            sum(item.elapsed_seconds for item in artifacts) / total if total else 0.0
        ),
        avg_prompt_tokens=0.0,
        avg_completion_tokens=0.0,
        avg_cost=0.0,
        total_cost=0.0,
        total_failed_slides=0,
        total_slides=0,
        total_retried_slides=0,
        status="completed",
        rows=rows,
        category_scores={
            "edit": {
                "total_cases": total,
                "ok_cases": passed,
                "avg_judge_score": None,
                "success_rate": passed / total if total else 0.0,
                "updated_at": datetime.now(UTC).isoformat(),
            }
        },
        note="ai_edit 工具环评测（obs/10）：提案层规则断言，非主评测口径",
    )
    try:
        async with async_session_factory() as session:
            session.add(run)
            await session.commit()
    except Exception as error:
        print(f"[edit-eval] 入库失败：{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print(f"[edit-eval] 已写入 eval_runs：{run.id}", file=sys.stderr)
    return 0


async def _main() -> int:
    cases = load_edit_cases()
    base = _base_url()
    print(f"[edit-eval] 题集 {len(cases)} 题，API：{base}", file=sys.stderr)
    # trust_env=False：只面向本地 dev 栈，避免系统代理劫走 127.0.0.1 流量
    async with httpx.AsyncClient(base_url=base, timeout=300.0, trust_env=False) as client:
        runner = EditEvalRunner(client)
        try:
            artifacts = await runner.run(cases)
        except RuntimeError as error:
            print(f"[edit-eval] 环境错误，评测中止：{error}", file=sys.stderr)
            return 2
    sys.stdout.write(_format_report(artifacts))
    return await _persist(cases, artifacts)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
