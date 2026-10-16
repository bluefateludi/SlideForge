"""ai_edit 工具环评测执行器（obs/10）：HTTP 驱动真实栈跑「生成 → 锁块 → 局部修改」。

单题流程：注册（题级独立账号，保项目所有权）→ 建 2 页项目 + topic 材料 →
大纲生成并确认 → 页面生成 → 选内容块最多的页为靶页 → 按题面锁块
（PATCH 重发原内容即 locked）→ POST ai-edit 拿提案 → 纯规则评分。
断言打在「提案」层而不是 apply 之后：提案就是 Agent 环的全部决策输出，
apply 属于人工审批边界（HITL），不在评测射程内。
"""

from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass, field

import httpx

from app.eval.edit_cases import EditCase
from app.eval.edit_scorer import EditCaseScore, score_edit_case


@dataclass(slots=True)
class EditCaseArtifacts:
    case_id: str
    ok: bool
    stage: str
    error: str | None = None
    project_id: str | None = None
    slide_id: str | None = None
    locked_ids: list[str] = field(default_factory=list)
    operations_count: int = 0
    checks: list[dict] = field(default_factory=list)
    elapsed_seconds: float = 0.0


class EditEvalRunner:
    """逐题驱动：接口刻意窄（run(cases) → artifacts 列表），与主评测执行器同风格。"""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        outline_seconds: float = 120.0,
        per_slide_seconds: float = 90.0,
        poll_interval_seconds: float = 2.0,
    ) -> None:
        self._client = client
        self._outline_seconds = outline_seconds
        self._per_slide_seconds = per_slide_seconds
        self._poll_interval_seconds = poll_interval_seconds

    async def run(self, cases: list[EditCase]) -> list[EditCaseArtifacts]:
        results: list[EditCaseArtifacts] = []
        for case in cases:
            started = time.monotonic()
            artifacts = EditCaseArtifacts(case_id=case.id, ok=False, stage="start")
            try:
                score = await self._run_case(case, artifacts)
                artifacts.ok = score.passed
                artifacts.checks = [check.model_dump() for check in score.checks]
                artifacts.stage = "scored"
            except httpx.HTTPError as error:
                raise RuntimeError(f"无法连接 API：{error}") from error
            except Exception as error:  # 单题失败不中断整轮
                artifacts.error = f"{type(error).__name__}: {error}"
            finally:
                artifacts.elapsed_seconds = time.monotonic() - started
            results.append(artifacts)
        return results

    async def _run_case(self, case: EditCase, artifacts: EditCaseArtifacts) -> EditCaseScore:
        headers = await self._register()
        project_id = await self._seed_project(case, artifacts, headers)
        slide, revision = await self._wait_ready_deck(case, artifacts, project_id, headers)

        locked_ids: list[str] = []
        if case.lock != "none":
            locked_ids = await self._lock_block(case, project_id, slide, revision, headers)
            revision += 1  # 锁块提交后 revision +1，ai-edit 须带新值
        artifacts.locked_ids = locked_ids

        artifacts.stage = "ai_edit"
        proposal = await self._request(
            "POST",
            f"/projects/{project_id}/deck/slides/{slide['id']}/ai-edit",
            json={"instruction": case.instruction, "revision": revision, "history": []},
            headers=headers,
        )
        operations = list(proposal.get("operations") or [])
        artifacts.operations_count = len(operations)
        return score_edit_case(case, operations=operations, locked_ids=locked_ids)

    # ---------- 题级独立账号：保住项目所有权，verify/retry 类断言未来可复用 ----------

    async def _register(self) -> dict[str, str]:
        email = f"edit-eval-{secrets.token_hex(6)}@slideforge.eval"
        password = secrets.token_hex(12)
        credentials = {"email": email, "password": password}
        await self._client.post("/auth/register", json=credentials)
        response = await self._client.post("/auth/login", json=credentials)
        if response.status_code >= 500:
            raise RuntimeError(f"API 服务不可用（HTTP {response.status_code}）")
        if response.status_code not in (200, 201):
            raise RuntimeError(f"评测账号注册/登录失败（HTTP {response.status_code}）")
        return {"Authorization": f"Bearer {response.json()['access_token']}"}

    async def _seed_project(
        self, case: EditCase, artifacts: EditCaseArtifacts, headers: dict
    ) -> str:
        artifacts.stage = "create_project"
        project = await self._request(
            "POST",
            "/projects",
            json={
                "title": case.seed.title,
                "tone": "professional",
                "page_count": case.seed.page_count,
                "theme_id": "ivory",
                "layout_mode": "flex",
                "content_density": "medium",
            },
            headers=headers,
        )
        artifacts.project_id = project["id"]
        artifacts.stage = "add_topic_source"
        await self._request(
            "POST",
            f"/projects/{project['id']}/sources",
            json={"kind": "topic", "content": case.seed.topic},
            headers=headers,
        )
        artifacts.stage = "outline_generate"
        await self._request(
            "POST", f"/projects/{project['id']}/outline/generate", headers=headers
        )
        return project["id"]

    async def _wait_ready_deck(
        self, case: EditCase, artifacts: EditCaseArtifacts, project_id: str, headers: dict
    ) -> tuple[dict, int]:
        artifacts.stage = "outline_wait"
        deadline = time.monotonic() + self._outline_seconds
        while True:
            outline = await self._request("GET", f"/projects/{project_id}/outline", headers=headers)
            if outline.get("status") == "failed":
                raise RuntimeError(f"大纲生成失败：{outline.get('error') or '无错误信息'}")
            if outline.get("status") in {"draft", "confirmed"}:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError(f"大纲生成超时（状态 {outline.get('status')}）")
            await asyncio.sleep(self._poll_interval_seconds)
        await self._request(
            "POST",
            f"/projects/{project_id}/outline/confirm",
            json={"revision": int(outline["revision"])},
            headers=headers,
        )

        artifacts.stage = "deck_wait"
        deadline = time.monotonic() + self._per_slide_seconds * case.seed.page_count
        while True:
            deck = await self._request("GET", f"/projects/{project_id}/deck", headers=headers)
            slides = deck.get("slides") or []
            settled = slides and all(s.get("status") in {"ready", "failed"} for s in slides)
            if settled:
                failed = [s for s in slides if s.get("status") == "failed"]
                if failed:
                    raise RuntimeError(f"{len(failed)} 页生成失败，无法进入编辑评测")
                # 靶页：可编辑块最多的 ready 页（首页常是封面，块少且带图）
                target = max(
                    (s for s in slides if s.get("blocks")),
                    key=lambda s: len(s["blocks"]),
                )
                return target, int(target["revision"])
            if time.monotonic() >= deadline:
                raise TimeoutError("页面生成超时")
            await asyncio.sleep(self._poll_interval_seconds)

    async def _lock_block(
        self, case: EditCase, project_id: str, slide: dict, revision: int, headers: dict
    ) -> list[str]:
        """PATCH 重发原内容即锁定（服务端置 locked=True），内容零变化。"""
        blocks = [b for b in slide["blocks"] if b.get("type") in {"text", "bullets"}]
        if not blocks:
            return []
        target = blocks[0] if case.lock == "title" else blocks[-1]
        body: dict = {"revision": revision, "type": target["type"]}
        if target["type"] == "text":
            body["text"] = target.get("text", "")
        else:
            body["items"] = list(target.get("items") or [])
        await self._request(
            "PATCH",
            f"/projects/{project_id}/deck/slides/{slide['id']}/blocks/{target['id']}",
            json=body,
            headers=headers,
        )
        return [str(target["id"])]

    async def _request(self, method: str, url: str, **kwargs) -> dict:
        response = await self._client.request(method, url, **kwargs)
        if response.status_code >= 500:
            raise RuntimeError(
                f"API 服务错误（{method} {url} HTTP {response.status_code}）：{response.text[:200]}"
            )
        if response.status_code >= 400:
            raise RuntimeError(
                f"{method} {url} 返回 HTTP {response.status_code}：{response.text[:300]}"
            )
        if response.status_code == 204 or not response.content:
            return {}
        return response.json()


__all__ = ["EditCaseArtifacts", "EditEvalRunner"]
