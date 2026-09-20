"""评测执行器：HTTP 驱动真实服务跑完主流程（eval#3）。

单题流程：注册/登录（run 级一次） → 创建项目 → 文档题先上传材料 →
POST outline/generate → 轮询 GET outline → POST outline/confirm →
POST deck/generate → 轮询 GET deck（逐页重试 failed） → GET deck/export。

以轮询代替 SSE 跟进各阶段状态；单题失败不中断整轮（失败原因收进结果）。
Token 用量埋点在 worker 进程内，HTTP 层取不到（take_usage_records 不可跨
进程），v1 按 0 记录，报告注明口径。
"""

from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass, field

import httpx
from pydantic import BaseModel

from app.eval.cases import EvalCase
from app.eval.evaluator import (
    CaseScore,
    deck_from_response,
    deck_full_text,
    score_structure,
)
from app.eval.hallucination import check_numbers_against_source
from app.llm.errors import InvalidModelOutputError, LLMNotConfiguredError


class EvalTimeouts(BaseModel):
    """各阶段轮询超时（秒），总量按题组合；全部可配。"""

    outline_seconds: float = 120.0
    per_slide_seconds: float = 90.0
    export_seconds: float = 60.0
    poll_interval_seconds: float = 2.0


@dataclass(slots=True)
class CaseArtifacts:
    """单题执行产物：成功时三样齐全，失败时带原因与阶段。"""

    case_id: str
    ok: bool
    stage: str  # 失败/完成时所处的阶段名
    error: str | None = None
    project_id: str | None = None
    deck_response: dict | None = None
    export_succeeded: bool = False
    elapsed_seconds: float = 0.0
    prompt_tokens: int = 0  # 口径：worker 进程埋点不可达，v1 恒 0
    completion_tokens: int = 0
    # 重试口径：生成阶段出现过 failed 状态的页数（ARQ 层 job 重试不可见于 HTTP）
    failed_slide_seen: int = 0
    total_slides: int = 0
    retried_slides: int = 0  # 评测器主动触发逐页重试端点的次数
    events: list[str] = field(default_factory=list)


class EvalEnvironmentError(RuntimeError):
    """环境性错误（连不上 API/认证失败），应中断整轮并以非零退出。"""


class EvalRunner:
    """逐题驱动真实服务的执行器。接口刻意窄：run(cases) → artifacts 列表。"""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        timeouts: EvalTimeouts | None = None,
        judge=None,
    ) -> None:
        self._client = client
        self._timeouts = timeouts or EvalTimeouts()
        self._judge = judge

    async def run(self, cases: list[EvalCase]) -> list[CaseArtifacts]:
        token = await self._authenticate()
        headers = {"Authorization": f"Bearer {token}"}
        results: list[CaseArtifacts] = []
        for case in cases:
            results.append(await self._run_case(case, headers))
        return results

    # ---------- 认证 ----------

    async def _authenticate(self) -> str:
        # 随机用户名避免与其他评测 run / 本地账号冲突；注册失败再退登录
        email = f"eval-{secrets.token_hex(6)}@slideforge.eval"
        password = secrets.token_hex(12)
        try:
            response = await self._client.post(
                "/auth/register", json={"email": email, "password": password}
            )
            if response.status_code == 409:
                response = await self._client.post(
                    "/auth/login", json={"email": email, "password": password}
                )
        except httpx.HTTPError as error:
            raise EvalEnvironmentError(f"无法连接 API：{error}") from error
        if response.status_code >= 500:
            raise EvalEnvironmentError(
                f"API 服务不可用（HTTP {response.status_code}）：{response.text[:200]}"
            )
        if response.status_code != 200 and response.status_code != 201:
            raise EvalEnvironmentError(
                f"评测账号注册/登录失败（HTTP {response.status_code}）：{response.text[:200]}"
            )
        token = response.json().get("access_token")
        if not token:
            raise EvalEnvironmentError("认证响应缺少 access_token")
        return token

    # ---------- 单题编排 ----------

    async def _run_case(self, case: EvalCase, headers: dict) -> CaseArtifacts:
        artifacts = CaseArtifacts(case_id=case.id, ok=False, stage="start")
        started = time.monotonic()
        try:
            await self._execute(case, artifacts, headers)
            artifacts.ok = True
            artifacts.stage = "done"
        except EvalEnvironmentError:
            raise
        except Exception as error:  # 单题失败不中断整轮
            artifacts.error = f"{type(error).__name__}: {error}"
        finally:
            artifacts.elapsed_seconds = time.monotonic() - started
        return artifacts

    async def _execute(self, case: EvalCase, artifacts: CaseArtifacts, headers: dict) -> None:
        project_id = await self._create_project(case, artifacts, headers)

        if case.category == "documents":
            await self._upload_source(case, project_id, artifacts, headers)

        await self._generate_outline(project_id, artifacts, headers)
        revision = await self._wait_outline(project_id, artifacts, headers)
        await self._confirm_outline(project_id, revision, artifacts, headers)

        deck = await self._generate_deck(project_id, case, artifacts, headers)
        export_ok = await self._export(project_id, artifacts, headers)

        artifacts.deck_response = deck
        artifacts.export_succeeded = export_ok

    async def _create_project(self, case: EvalCase, artifacts: CaseArtifacts, headers: dict) -> str:
        artifacts.stage = "create_project"
        response = await self._request(
            "POST",
            "/projects",
            json={
                "title": _title_from_case(case),
                "tone": "professional",
                "page_count": case.expected_pages,
                "theme_id": "ivory",
                "layout_mode": "flex",
                "content_density": "medium",
            },
            headers=headers,
        )
        artifacts.project_id = response["id"]
        return response["id"]

    async def _upload_source(
        self, case: EvalCase, project_id: str, artifacts: CaseArtifacts, headers: dict
    ) -> None:
        assert case.source_path is not None
        artifacts.stage = "upload_source"
        data = case.source_path.read_bytes()
        filename = case.source_path.name
        await self._request(
            "POST",
            f"/projects/{project_id}/sources/upload",
            files={"file": (filename, data, "text/markdown")},
            headers=headers,
        )

    async def _generate_outline(
        self, project_id: str, artifacts: CaseArtifacts, headers: dict
    ) -> None:
        artifacts.stage = "outline_generate"
        await self._request("POST", f"/projects/{project_id}/outline/generate", headers=headers)

    async def _wait_outline(self, project_id: str, artifacts: CaseArtifacts, headers: dict) -> int:
        artifacts.stage = "outline_wait"
        deadline = time.monotonic() + self._timeouts.outline_seconds
        while True:
            outline = await self._request("GET", f"/projects/{project_id}/outline", headers=headers)
            status = outline.get("status")
            if status == "failed":
                raise RuntimeError(f"大纲生成失败：{outline.get('error') or '无错误信息'}")
            if status in {"draft", "confirmed"}:
                return int(outline["revision"])
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"大纲生成超时（{self._timeouts.outline_seconds:.0f}s，状态 {status}）"
                )
            await asyncio.sleep(self._timeouts.poll_interval_seconds)

    async def _confirm_outline(
        self, project_id: str, revision: int, artifacts: CaseArtifacts, headers: dict
    ) -> None:
        artifacts.stage = "outline_confirm"
        await self._request(
            "POST",
            f"/projects/{project_id}/outline/confirm",
            json={"revision": revision},
            headers=headers,
        )

    async def _generate_deck(
        self, project_id: str, case: EvalCase, artifacts: CaseArtifacts, headers: dict
    ) -> dict:
        artifacts.stage = "deck_generate"
        await self._request(
            "POST", f"/projects/{project_id}/deck/generate", json={}, headers=headers
        )
        return await self._wait_deck(project_id, case, artifacts, headers)

    async def _wait_deck(
        self, project_id: str, case: EvalCase, artifacts: CaseArtifacts, headers: dict
    ) -> dict:
        artifacts.stage = "deck_wait"
        deadline = time.monotonic() + self._timeouts.per_slide_seconds * max(1, case.expected_pages)
        failed_seen: set[str] = set()
        while True:
            deck = await self._request("GET", f"/projects/{project_id}/deck", headers=headers)
            slides = deck.get("slides", [])
            artifacts.total_slides = len(slides)
            for slide in slides:
                if slide.get("status") == "failed":
                    failed_seen.add(slide["id"])

            all_settled = slides and all(
                slide.get("status") in {"ready", "failed"} for slide in slides
            )
            if all_settled:
                failed = [s for s in slides if s.get("status") == "failed"]
                if failed and time.monotonic() < deadline:
                    # 逐页重试：口径上等价于用户在界面点「重试」
                    for slide in failed:
                        await self._request(
                            "POST",
                            f"/projects/{project_id}/deck/slides/{slide['id']}/retry",
                            headers=headers,
                        )
                        artifacts.retried_slides += 1
                    await asyncio.sleep(self._timeouts.poll_interval_seconds)
                    continue
                if failed:
                    raise RuntimeError(
                        f"{len(failed)} 页重试后仍失败："
                        + "；".join(
                            (slide.get("error") or slide.get("title") or slide["id"])
                            for slide in failed[:3]
                        )
                    )
                artifacts.failed_slide_seen = len(failed_seen)
                return deck

            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"页面生成超时（{self._timeouts.per_slide_seconds:.0f}s/页 × "
                    f"{case.expected_pages} 页，进度 "
                    f"{sum(1 for s in slides if s.get('status') == 'ready')}/{len(slides)}）"
                )
            await asyncio.sleep(self._timeouts.poll_interval_seconds)

    async def _export(self, project_id: str, artifacts: CaseArtifacts, headers: dict) -> bool:
        artifacts.stage = "export"
        try:
            response = await self._client.get(
                f"/projects/{project_id}/deck/export", headers=headers
            )
        except httpx.HTTPError as error:
            artifacts.events.append(f"导出请求异常：{error}")
            return False
        if response.status_code == 200:
            return True
        artifacts.events.append(f"导出失败（HTTP {response.status_code}）：{response.text[:200]}")
        return False

    async def _request(self, method: str, url: str, **kwargs) -> dict:
        try:
            response = await self._client.request(method, url, **kwargs)
        except httpx.HTTPError as error:
            raise EvalEnvironmentError(f"无法连接 API：{error}") from error
        if response.status_code >= 500:
            raise EvalEnvironmentError(
                f"API 服务错误（{method} {url} HTTP {response.status_code}）：{response.text[:200]}"
            )
        if response.status_code >= 400:
            raise RuntimeError(
                f"{method} {url} 返回 HTTP {response.status_code}：{response.text[:300]}"
            )
        if response.status_code == 204 or not response.content:
            return {}
        return response.json()


def _title_from_case(case: EvalCase) -> str:
    # 标题取题面主旨：输入指令通常形如「生成一份《X》的 N 页 PPT」
    for sep in ("《", "》"):
        if sep in case.input:
            start = case.input.find("《") + 1
            end = case.input.find("》")
            if 0 < start < end:
                return case.input[start:end]
    return f"评测-{case.id.replace('/', '-')}"


# ---------- 评分编排：把单题产物交给三路评分 ----------


async def score_case(
    case: EvalCase,
    artifacts: CaseArtifacts,
    judge,
) -> CaseScore:
    """对单题产物做三路评分。runner 与评分解耦：本函数不触网。"""
    score = CaseScore()
    if not artifacts.ok or artifacts.deck_response is None:
        return score

    deck = deck_from_response(artifacts.deck_response)
    score.structural = score_structure(deck)

    if judge is not None:
        try:
            score.judge = await judge.judge(case, deck)
        except (InvalidModelOutputError, LLMNotConfiguredError) as error:
            score.judge_error = f"{type(error).__name__}: {error}"

    if case.category == "documents" and case.source_path is not None:
        source_text = case.source_path.read_text(encoding="utf-8")
        titles = {
            slide["id"]: slide.get("title", "")
            for slide in artifacts.deck_response.get("slides", [])
        }
        full_text = deck_full_text(deck, title_by_slide_id=titles)
        score.hallucination = check_numbers_against_source(full_text, source_text)
    return score


__all__ = [
    "CaseArtifacts",
    "CaseScore",
    "EvalEnvironmentError",
    "EvalRunner",
    "EvalTimeouts",
    "score_case",
]
