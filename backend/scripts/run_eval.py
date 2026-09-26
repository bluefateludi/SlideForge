#!/usr/bin/env python3
"""评测入口：make eval → 逐题驱动真实服务并输出整份报告（eval#3）。

用法（仓库根目录）：
  make eval               全量评测（报告 + 入库）
  make eval-gate          跑完后再做回归门禁：指标对比 baseline.json，
                          超容差退出码 1（eval#6）
  make eval-baseline      不跑评测，从库里最近一次同题集版本的 run
                          重立基线（零模型调用）；跑完一轮后用
                          `uv run python scripts/run_eval.py --baseline`
                          可直接以本轮结果立基线
环境变量：
  EVAL_API_BASE   API 基址（默认 http://127.0.0.1:39800/api/v1）
  EVAL_OUTLINE_TIMEOUT_SECONDS / EVAL_SLIDE_TIMEOUT_SECONDS /
  EVAL_EXPORT_TIMEOUT_SECONDS / EVAL_POLL_INTERVAL_SECONDS   各阶段超时
  EVAL_NOTE        本次 run 的备注（写入 eval_runs.note）
  EVAL_SKIP_DB     非空时跳过入库（报告只打印，行为退回 eval#3）
  EVAL_GATE        非空时等价 --gate（Makefile 目标的底层开关）

退出码：0 正常；1 门禁未通过 / 未立基线 / 基线操作失败；2 环境性错误。
门禁口径见 app/eval/gate.py：成功率/Schema 率/导出零容差，judge 容差
-0.5，token/cost/P95 上限 ×1.3；cases_version 不一致拒绝对比。
真实全量评测由维护者本地发起。跑完报告后直连数据库写入 eval_runs
（async_session_factory）：脚本本就在后端代码库内、与 API 共用一套模型
与迁移，经 API 中转只多一层认证与校验，无收益。
报告先打印再写库，写库失败给出明确中文错误且不吞掉已输出的报告。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

# 允许直接 `python scripts/run_eval.py` 时找到 app 包
BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import httpx  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.eval.cases import load_cases, pick_smoke_case  # noqa: E402
from app.eval.evaluator import DeckJudge  # noqa: E402
from app.eval.gate import (  # noqa: E402
    BASELINE_PATH,
    compare_with_baseline,
    format_gate_report,
    latest_run_metrics,
    load_baseline,
    write_baseline,
    write_baseline_from_metrics,
)
from app.eval.persistence import (  # noqa: E402
    compute_cases_version,
    join_report_traces,
    report_to_run,
    save_run,
)
from app.eval.report import build_report, format_report  # noqa: E402
from app.eval.runner import (  # noqa: E402
    EvalEnvironmentError,
    EvalRunner,
    EvalTimeouts,
    score_case,
)
from app.llm.errors import LLMNotConfiguredError  # noqa: E402

DEFAULT_API_BASE = "http://127.0.0.1:39800/api/v1"


def _timeouts_from_env() -> EvalTimeouts:
    def env_float(name: str, default: float) -> float:
        raw = os.environ.get(name, "").strip()
        if not raw:
            return default
        try:
            return max(1.0, float(raw))
        except ValueError:
            return default

    return EvalTimeouts(
        outline_seconds=env_float("EVAL_OUTLINE_TIMEOUT_SECONDS", 120.0),
        per_slide_seconds=env_float("EVAL_SLIDE_TIMEOUT_SECONDS", 90.0),
        export_seconds=env_float("EVAL_EXPORT_TIMEOUT_SECONDS", 60.0),
        poll_interval_seconds=env_float("EVAL_POLL_INTERVAL_SECONDS", 2.0),
    )


def _build_judge() -> DeckJudge | None:
    settings = get_settings()
    if not settings.llm_api_key.strip():
        print("[eval] 未配置 LLM_API_KEY，跳过内容评分（结构/幻觉指标照常）", file=sys.stderr)
        return None
    try:
        return DeckJudge.for_settings(settings)
    except LLMNotConfiguredError:
        return None


async def _run_with_smoke(runner, cases):
    """冒烟预检 + 全量执行：先跑 1 道最短主题题，链路不通立即中止。

    harness 自身的问题（编排漏步骤、桩与真实 API 漂移）在分钟级暴露，
    而不是烧完一整轮后从逐题 422 里发现。冒烟通过则跳过该题跑余题，
    结果按传入顺序归位，对调用方与一次跑全量无差别。
    EVAL_SKIP_SMOKE 非空时直接全量（题集无主题题时同样直落）。

    返回 artifacts 列表；冒烟未通过返回 None（调用方中止、不产出报告）。
    """
    smoke = pick_smoke_case(cases) if not os.environ.get("EVAL_SKIP_SMOKE", "").strip() else None
    if smoke is None:
        return await runner.run(cases)

    print(f"[eval] 冒烟预检：{smoke.id}（{smoke.expected_pages} 页）", file=sys.stderr)
    smoke_artifacts = await runner.run([smoke])
    smoke_result = smoke_artifacts[0]
    if not smoke_result.ok:
        print(
            f"[eval] 冒烟未通过（阶段 {smoke_result.stage}）：{smoke_result.error}\n"
            f"[eval] 疑似 harness/环境问题，已中止，未跑全量。"
            f"确认环境无误后可 EVAL_SKIP_SMOKE=1 跳过预检。",
            file=sys.stderr,
        )
        return None

    print("[eval] 冒烟通过，起跑全量", file=sys.stderr)
    rest = [case for case in cases if case.id != smoke.id]
    rest_artifacts = await runner.run(rest)
    by_id = {smoke.id: smoke_result, **{item.case_id: item for item in rest_artifacts}}
    return [by_id[case.id] for case in cases]


async def _main(*, gate: bool = False, baseline: bool = False) -> int:
    api_base = os.environ.get("EVAL_API_BASE", DEFAULT_API_BASE).rstrip("/")
    base = api_base if api_base.endswith("/api/v1") else _with_v1(api_base)
    cases = load_cases()
    print(f"[eval] 题集 {len(cases)} 题，API：{base}", file=sys.stderr)

    judge = _build_judge()
    # trust_env=False：评测只面向本地 dev 栈，明确忽略系统/环境代理，
    # 避免本机代理把 127.0.0.1 流量劫走后返回 502。
    async with httpx.AsyncClient(base_url=base, timeout=300.0, trust_env=False) as client:
        runner = EvalRunner(client, timeouts=_timeouts_from_env(), judge=judge)
        try:
            artifacts = await _run_with_smoke(runner, cases)
        except EvalEnvironmentError as error:
            print(f"[eval] 环境错误，评测中止：{error}", file=sys.stderr)
            return 2
        if artifacts is None:
            return 2

    results = []
    for case, item in zip(cases, artifacts, strict=True):
        score = await score_case(case, item, judge)
        results.append((case.id, case.expected_pages, item, score))
    report = build_report(results)
    # obs#4：入库前按 trace_id join spans，补真实 token 与分段耗时；
    # 打印的报告因此含真实值。join 只在能连库时做（EVAL_SKIP_DB 同款开关
    # 稍后仍会跳过写入，但报告侧数值优先补齐）。
    joined = False
    if not os.environ.get("EVAL_SKIP_DB", "").strip():
        try:
            from app.core.db import async_session_factory

            async with async_session_factory() as session:
                await join_report_traces(session, report)
            joined = True
        except Exception as error:
            print(
                f"[eval] trace join 失败（token 按查不到处理）：{type(error).__name__}: {error}",
                file=sys.stderr,
            )
    sys.stdout.write(format_report(report))
    await _persist_report(report, cases, traces_joined=joined)
    cases_version = compute_cases_version(cases)

    if baseline:
        if os.environ.get("EVAL_SKIP_DB", "").strip():
            print("[baseline] EVAL_SKIP_DB 下指标未 join trace，拒绝立基线", file=sys.stderr)
            return 1
        write_baseline(BASELINE_PATH, report, cases_version=cases_version)
        print(
            f"[baseline] 已写入 {BASELINE_PATH}（cases_version={cases_version}）",
            file=sys.stderr,
        )

    if gate:
        return _run_gate(report, cases_version=cases_version)
    return 0


def _run_gate(report, *, cases_version: str) -> int:
    """门禁：对比 baseline.json，打印报告并给退出码（eval#6）。"""
    baseline_data = load_baseline(BASELINE_PATH)
    if baseline_data is None:
        print(
            f"[gate] 未找到合法基线（{BASELINE_PATH}）：先跑一轮评测后"
            " `uv run python scripts/run_eval.py --baseline`，或 make eval-baseline 从库里取",
            file=sys.stderr,
        )
        return 1
    gate_report = compare_with_baseline(report, cases_version=cases_version, baseline=baseline_data)
    sys.stdout.write(format_gate_report(gate_report))
    return 0 if gate_report.passed else 1


async def _baseline_from_db() -> int:
    """零模型调用：从库里最近一次同题集版本的 run 重立基线。"""
    cases = load_cases()
    cases_version = compute_cases_version(cases)
    from app.core.db import async_session_factory

    async with async_session_factory() as session:
        found = await latest_run_metrics(session, cases_version=cases_version)
    if found is None:
        print(
            f"[baseline] 库里没有 cases_version={cases_version} 的 run，"
            "先跑一轮 make eval 再立基线",
            file=sys.stderr,
        )
        return 1
    run_id, metrics = found
    write_baseline_from_metrics(
        BASELINE_PATH, cases_version=cases_version, metrics=metrics, source=f"eval_runs:{run_id}"
    )
    print(f"[baseline] 已写入 {BASELINE_PATH}（取自 run {run_id}）", file=sys.stderr)
    return 0


async def _persist_report(report, cases, *, traces_joined: bool) -> None:
    """报告落库（eval#4）：报告已打印在先，写库失败只报错不影响其输出。"""
    if os.environ.get("EVAL_SKIP_DB", "").strip():
        print("[eval] EVAL_SKIP_DB 已设置，跳过入库", file=sys.stderr)
        return

    from app.core.db import async_session_factory

    run = report_to_run(
        report,
        cases_version=compute_cases_version(cases),
        note=_compose_note(traces_joined),
    )
    try:
        async with async_session_factory() as session:
            saved = await save_run(session, run)
    except Exception as error:  # 明确报错但不吞掉已打印的报告
        print(
            f"[eval] 评测报告入库失败：{type(error).__name__}: {error}（报告已在上方打印，"
            f"可用 EVAL_SKIP_DB=1 仅看报告）",
            file=sys.stderr,
        )
    else:
        print(f"[eval] 已写入 eval_runs：{saved.id}", file=sys.stderr)


def _compose_note(traces_joined: bool) -> str:
    """EVAL_NOTE 前置 + token 来源标注：join 未做/失败时在 note 里说明。"""
    note = os.environ.get("EVAL_NOTE", "").strip()
    marker = "" if traces_joined else "tokens_source=unavailable（trace join 未执行）"
    return "；".join(part for part in (note, marker) if part)


def _with_v1(api_base: str) -> str:
    """容错：给了服务根地址（不含 /api/v1）时自动补全。"""
    parsed = api_base.rstrip("/")
    return parsed if parsed.endswith("/v1") else parsed + "/api/v1"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SlideForge 评测入口（口径见模块 docstring）")
    parser.add_argument("--gate", action="store_true", help="跑完后做回归门禁（等价 EVAL_GATE=1）")
    parser.add_argument("--baseline", action="store_true", help="以本轮结果重立基线 baseline.json")
    parser.add_argument(
        "--baseline-from-db",
        action="store_true",
        help="不跑评测：从库里最近一次同题集版本的 run 立基线（零模型调用）",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if args.baseline_from_db:
        return asyncio.run(_baseline_from_db())
    gate = args.gate or bool(os.environ.get("EVAL_GATE", "").strip())
    return asyncio.run(_main(gate=gate, baseline=args.baseline))


if __name__ == "__main__":
    raise SystemExit(main())
