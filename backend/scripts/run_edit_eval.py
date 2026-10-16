#!/usr/bin/env python3
"""固定页面编辑评测。只有 ai-edit 调用真实模型；程序评分不调用 judge。

make eval-edit 需 API 与同库 PostgreSQL，不需要 Worker。
本次开发只跑 pytest，不运行此真实评测。
退出码：0 全部断言通过；1 有题失败或报告入库失败；2 环境错误。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import httpx  # noqa: E402

from app.eval.edit_cases import load_edit_cases  # noqa: E402
from app.eval.edit_persistence import build_edit_run  # noqa: E402
from app.eval.edit_runner import EditEvalRunner  # noqa: E402


def format_edit_report(artifacts) -> str:
    passed = sum(item.ok for item in artifacts)
    lines = [f"=== 固定页面编辑评测：{passed}/{len(artifacts)} 通过 ==="]
    for item in artifacts:
        lines.append(
            f"[{'OK' if item.ok else 'FAIL'}] {item.case_id} "
            f"{item.operations_count} 个操作 / {item.elapsed_seconds:.1f}s"
        )
        if item.error:
            lines.append(f"  {item.stage}: {item.error}")
        for check in item.checks:
            lines.append(
                f"  [{'OK' if check['passed'] else 'FAIL'}] {check['name']}"
                + (f": {check['detail']}" if check.get("detail") else "")
            )
        lines.append(f"  trace_id={item.trace_id or '未采集'}")
        if item.measurement_issues:
            lines.append("  用量/成本未完整采集：" + ", ".join(item.measurement_issues))
    return "\n".join(lines) + "\n"


async def _main(case_ids: str = "") -> int:
    cases = load_edit_cases()
    if case_ids:
        requested = set(case_ids.split(","))
        available = {case.id for case in cases}
        if not requested <= available:
            print("未知编辑题号：" + ", ".join(sorted(requested - available)), file=sys.stderr)
            return 2
        cases = [case for case in cases if case.id in requested]
    base = os.environ.get("EVAL_API_BASE", "http://127.0.0.1:39800/api/v1").rstrip("/")
    if not base.endswith("/api/v1"):
        base += "/api/v1"
    async with httpx.AsyncClient(base_url=base, timeout=300, trust_env=False) as client:
        try:
            artifacts = await EditEvalRunner(client).run(cases)
        except RuntimeError as error:
            print(f"[edit-eval] 环境错误：{error}", file=sys.stderr)
            return 2
    sys.stdout.write(format_edit_report(artifacts))
    if not os.environ.get("EVAL_SKIP_DB", "").strip():
        from app.core.db import async_session_factory

        try:
            async with async_session_factory() as session:
                run = build_edit_run(cases, artifacts)
                session.add(run)
                await session.commit()
                print(f"[edit-eval] 已写入 eval_runs：{run.id}", file=sys.stderr)
        except Exception as error:
            print(f"[edit-eval] 报告入库失败：{type(error).__name__}", file=sys.stderr)
            return 1
    return 0 if all(item.ok for item in artifacts) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", default="", help="逗号分隔的编辑题号；缺省全量")
    return asyncio.run(_main(parser.parse_args().cases))


if __name__ == "__main__":
    raise SystemExit(main())
