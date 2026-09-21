#!/usr/bin/env python3
"""评测入口：make eval → 逐题驱动真实服务并输出整份报告（eval#3）。

用法（仓库根目录）：
  make eval
环境变量：
  EVAL_API_BASE   API 基址（默认 http://127.0.0.1:39800/api/v1）
  EVAL_OUTLINE_TIMEOUT_SECONDS / EVAL_SLIDE_TIMEOUT_SECONDS /
  EVAL_EXPORT_TIMEOUT_SECONDS / EVAL_POLL_INTERVAL_SECONDS   各阶段超时
  EVAL_NOTE        本次 run 的备注（写入 eval_runs.note）
  EVAL_SKIP_DB     非空时跳过入库（报告只打印，行为退回 eval#3）

真实全量评测由维护者本地发起；本脚本不设指标门禁，退出码只在
环境性错误（连不上 API / 认证失败）时非零。跑完报告后直连数据库
写入 eval_runs（async_session_factory）：脚本本就在后端代码库内、
与 API 共用一套模型与迁移，经 API 中转只多一层认证与校验，无收益。
报告先打印再写库，写库失败给出明确中文错误且不吞掉已输出的报告。
"""

from __future__ import annotations

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


async def _main() -> int:
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


def main() -> int:
    return asyncio.run(_main())


if __name__ == "__main__":
    raise SystemExit(main())
