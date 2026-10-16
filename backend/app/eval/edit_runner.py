"""固定页面 → ai-edit → 程序断言；准备阶段不调用生成或生图。"""

from __future__ import annotations

import secrets
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import httpx

from app.core.db import async_session_factory
from app.eval.edit_cases import EditCase
from app.eval.edit_scorer import score_edit_case
from app.eval.trace_join import TraceJoinResult, join_edit_trace_metrics
from app.models.project import Project, ProjectOutline
from app.models.slide import Slide


@dataclass(frozen=True)
class FixtureTarget:
    project_id: str
    slide_id: str
    revision: int = 1


@dataclass(slots=True)
class EditCaseArtifacts:
    case_id: str
    ok: bool = False
    stage: str = "start"
    error: str | None = None
    project_id: str | None = None
    slide_id: str | None = None
    trace_id: str | None = None
    locked_ids: list[str] = field(default_factory=list)
    operations_count: int = 0
    operations: list[dict] = field(default_factory=list)
    result_slide: dict | None = None
    edit_elapsed_seconds: float | None = None
    checks: list[dict] = field(default_factory=list)
    elapsed_seconds: float = 0.0
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cost: float | None = None
    measurement_complete: bool = False
    measurement_issues: list[str] = field(default_factory=list)


async def prepare_edit_fixture(owner_id: str, case: EditCase) -> FixtureTarget:
    """本地 harness 直接写固定测试项目，不提供可绕过权限的产品写入口。"""
    initial = case.initial_slide
    async with async_session_factory() as session:
        project = Project(
            user_id=uuid.UUID(owner_id),
            title=case.title,
            tone="professional",
            page_count=1,
            theme_id=case.theme_id,
            layout_mode=initial.layout_mode,
            content_density="medium",
            status="ready",
        )
        session.add(project)
        await session.flush()
        outline_page_id = uuid.uuid4()
        slide = Slide(
            project_id=project.id,
            outline_page_id=outline_page_id,
            position=1,
            title=case.initial_outline.title,
            layout_id=initial.layout_id,
            layout_mode=initial.layout_mode,
            layout_tree=initial.layout_tree.model_dump(mode="json")
            if initial.layout_tree
            else None,
            blocks=[block.model_dump(mode="json") for block in initial.blocks],
            status="ready",
            revision=1,
            issues=[],
        )
        session.add(slide)
        session.add(
            ProjectOutline(
                project_id=project.id,
                status="confirmed",
                revision=1,
                pages=[
                    {"id": str(outline_page_id), **case.initial_outline.model_dump(mode="json")}
                ],
            )
        )
        await session.commit()
        return FixtureTarget(str(project.id), str(slide.id))


async def measure_edit_trace(trace_id: str) -> TraceJoinResult:
    async with async_session_factory() as session:
        return await join_edit_trace_metrics(session, trace_id=trace_id)


class EditEvalRunner:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        prepare: Callable[[str, EditCase], Awaitable[FixtureTarget]] = prepare_edit_fixture,
        measure: Callable[[str], Awaitable[TraceJoinResult]] = measure_edit_trace,
    ):
        self._client = client
        self._prepare = prepare
        self._measure = measure

    async def run(self, cases: list[EditCase]) -> list[EditCaseArtifacts]:
        results = []
        for case in cases:
            item = EditCaseArtifacts(case_id=case.id)
            started = time.monotonic()
            try:
                await self._run_case(case, item)
            except httpx.HTTPError as error:
                raise RuntimeError(f"无法连接 API：{error}") from error
            except Exception as error:
                item.error = f"{type(error).__name__}: {error}"
            finally:
                item.elapsed_seconds = time.monotonic() - started
            results.append(item)
        return results

    async def _run_case(self, case: EditCase, item: EditCaseArtifacts) -> None:
        item.stage = "validate_fixture"
        # 防止调用方传入 model_copy / 原地改动而绕过题集加载时的校验。
        case = EditCase.model_validate(case.model_dump(mode="json"))
        item.stage = "register"
        credentials = {
            "email": f"edit-eval-{secrets.token_hex(6)}@slideforge.eval",
            "password": secrets.token_hex(12),
        }
        auth = await self._request("POST", "/auth/register", json=credentials)
        headers = {"Authorization": f"Bearer {auth['access_token']}"}
        item.stage = "prepare_fixture"
        target = await self._prepare(auth["user"]["id"], case)
        item.project_id, item.slide_id = target.project_id, target.slide_id
        item.locked_ids = [block.id for block in case.initial_slide.blocks if block.locked]

        # 确认 HTTP 服务与 harness 指向同一数据库，失败在模型调用前发生。
        deck = await self._request("GET", f"/projects/{target.project_id}/deck", headers=headers)
        slide = next((row for row in deck["slides"] if row["id"] == target.slide_id), None)
        expected = [block.model_dump(mode="json") for block in case.initial_slide.blocks]
        if (
            slide is None
            or slide["blocks"] != expected
            or slide["revision"] != target.revision
            or slide.get("layout_tree")
            != (
                case.initial_slide.layout_tree.model_dump(mode="json")
                if case.initial_slide.layout_tree
                else None
            )
        ):
            raise ValueError("固定夹具与 API 中的初始页面不一致")

        outline = await self._request(
            "GET",
            f"/projects/{target.project_id}/outline",
            headers=headers,
        )
        pages = outline.get("pages") or []
        expected_outline = case.initial_outline.model_dump(mode="json")
        if (
            outline.get("status") != "confirmed"
            or len(pages) != 1
            or {key: pages[0].get(key) for key in expected_outline} != expected_outline
        ):
            raise ValueError("固定大纲夹具与 API 中的大纲不一致")

        item.stage = "ai_edit"
        edit_started = time.monotonic()
        try:
            proposal = await self._request(
                "POST",
                f"/projects/{target.project_id}/deck/slides/{target.slide_id}/ai-edit",
                json={"instruction": case.instruction, "revision": target.revision, "history": []},
                headers=headers,
            )
        finally:
            item.edit_elapsed_seconds = time.monotonic() - edit_started
        operations = list(proposal.get("operations") or [])
        item.operations_count = len(operations)
        item.operations = operations
        score = score_edit_case(case, operations=operations)
        item.ok = score.passed
        item.result_slide = (
            score.result_slide.model_dump(mode="json") if score.result_slide else None
        )
        item.checks = [check.model_dump() for check in score.checks]
        item.trace_id = proposal.get("trace_id")
        item.stage = "scored"
        if not item.trace_id:
            item.measurement_issues = ["missing_trace_id"]
            return
        try:
            measured = await self._measure(item.trace_id)
        except Exception:
            item.measurement_issues = ["query_error"]
            return
        item.measurement_complete = measured.measurement_complete
        item.measurement_issues = measured.measurement_issues
        usage_issues = set(measured.measurement_issues) - {"price_not_configured"}
        if measured.tokens_source == "trace" and not usage_issues:
            item.prompt_tokens = measured.prompt_tokens
            item.completion_tokens = measured.completion_tokens
        if measured.measurement_complete:
            item.cost = measured.total_cost

    async def _request(self, method: str, url: str, **kwargs) -> dict:
        response = await self._client.request(method, url, **kwargs)
        if response.status_code >= 400:
            raise RuntimeError(f"{method} {url} HTTP {response.status_code}")
        return response.json()
