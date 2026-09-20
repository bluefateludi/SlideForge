"""执行器编排（eval#3）：mock httpx 传输层驱动的确定性整轮测试。

不启动真实服务、不消耗 LLM/API 额度：用 httpx.MockTransport 模拟
真实 API 的状态机（注册 → 建项目 → 上传 → 大纲生成中/完成 → 确认 →
逐页生成 → 导出），验证编排顺序、轮询、失败收集与环境性错误语义。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.eval.cases import EvalCase
from app.eval.evaluator import CaseScore, JudgeVerdict
from app.eval.runner import (
    CaseArtifacts,
    EvalEnvironmentError,
    EvalRunner,
    EvalTimeouts,
    score_case,
)

API = "http://eval.test/api/v1"


def _topic_case(case_id: str = "technology/rag") -> EvalCase:
    return EvalCase(
        id=case_id,
        input="生成一份《RAG 入门》的 5 页 PPT",
        expected_pages=5,
        requirements=["包含检索流程"],
        category="technology",
    )


def _doc_case(tmp_path: Path) -> EvalCase:
    source = tmp_path / "q3.md"
    source.write_text("Q3 总营收 1.86 亿元，同比增长 24%", encoding="utf-8")
    return EvalCase(
        id="documents/q3_sales",
        input="根据提供的材料生成一份《Q3 销售总结》的 5 页 PPT",
        expected_pages=5,
        requirements=["包含总营收"],
        category="documents",
        source="q3.md",
        source_path=source,
    )


def _response(data: Any, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status,
        json=data,
        request=httpx.Request("POST", API + "/x"),  # 占位，transport 会重建
    )


class FakeService:
    """状态机式假服务：覆盖主流程全部端点与阶段转换。"""

    def __init__(
        self,
        *,
        pages: int = 5,
        outline_polls_before_done: int = 1,
        deck_polls_before_done: int = 1,
        fail_outline: bool = False,
        fail_deck_after_retry: bool = False,
        fail_export: bool = False,
        reject_422: bool = False,
    ) -> None:
        self.pages = pages
        self.outline_polls_before_done = outline_polls_before_done
        self.deck_polls_before_done = deck_polls_before_done
        self.fail_outline = fail_outline
        self.fail_deck_after_retry = fail_deck_after_retry
        self.fail_export = fail_export
        self.reject_422 = reject_422
        self.calls: list[tuple[str, str]] = []
        self.outline_polls = 0
        self.deck_polls = 0
        self.project_ids: list[str] = []
        self.retry_calls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path
        self.calls.append((method, path))

        if path.endswith("/auth/register"):
            return _response({"access_token": "tok-123"})
        if path == "/api/v1/projects" and method == "POST":
            if self.reject_422:
                return _response({"detail": "未知主题"}, status=422)
            project_id = f"proj-{len(self.project_ids) + 1}"
            self.project_ids.append(project_id)
            return _response({"id": project_id}, status=201)
        if "/sources/upload" in path:
            if not request.content:
                return _response({"detail": "空文件"}, status=422)
            return _response({"id": "src-1", "char_count": 30}, status=201)
        if path.endswith("/outline/generate"):
            return _response({"job_id": "j1", "status": "generating"}, status=202)
        if path.endswith("/outline") and method == "GET":
            self.outline_polls += 1
            if self.fail_outline:
                return _response({"status": "failed", "revision": 1, "error": "模型返回不合格式"})
            if self.outline_polls <= self.outline_polls_before_done:
                return _response({"status": "generating", "revision": 1})
            return _response({"status": "draft", "revision": 3, "pages": []})
        if path.endswith("/outline/confirm"):
            body = json.loads(request.content)
            assert body["revision"] == 3, "确认必须带回轮询拿到的 revision"
            return _response({"status": "confirmed", "revision": 4})
        if path.endswith("/deck/generate"):
            return _response(
                {
                    "job_id": "j2",
                    "status": "generating",
                    "total": self.pages,
                    "pending": self.pages,
                },
                status=202,
            )
        if "/deck/slides/" in path and path.endswith("/retry"):
            self.retry_calls += 1
            return _response(
                {
                    "job_id": "j3",
                    "status": "generating",
                    "total": self.pages,
                    "pending": 1,
                },
                status=202,
            )
        if path.endswith("/deck") and method == "GET":
            self.deck_polls += 1
            if self.deck_polls <= self.deck_polls_before_done:
                return _response(self._deck_payload(status_mix="generating"))
            if self.fail_deck_after_retry and self.deck_polls == self.deck_polls_before_done + 2:
                # 重试一轮后仍失败
                return _response(self._deck_payload(status_mix="failed_stuck"))
            return _response(self._deck_payload(status_mix="ready"))
        if path.endswith("/deck/export"):
            if self.fail_export:
                return _response({"detail": "导出前检查未通过"}, status=409)
            return httpx.Response(
                200,
                content=b"PK-pptx-bytes",
                headers={"content-type": "application/vnd..."},
                request=request,
            )
        return _response({"detail": f"未模拟的路径 {path}"}, status=404)

    def _deck_payload(self, *, status_mix: str) -> dict:
        slides = []
        for i in range(self.pages):
            if status_mix == "generating":
                status = "ready" if i < 2 else "generating"
            elif status_mix == "failed_stuck":
                status = "failed"
            else:
                status = "ready"
            slides.append(
                {
                    "id": f"s{i}",
                    "layout_id": "two-column",
                    "layout_mode": "flex",
                    "layout_tree": {
                        "type": "column",
                        "id": "root",
                        "children": [{"type": "block", "id": f"l{i}", "block_id": f"b{i}"}],
                    },
                    "title": f"第{i}页",
                    "status": status,
                    "blocks": [
                        {"id": f"b{i}", "slot_id": "title", "type": "text", "text": f"内容{i}"}
                    ],
                    "error": None if status != "failed" else "生成超时",
                }
            )
        return {"project_id": "proj-1", "title": "t", "theme_id": "ivory", "slides": slides}


def _client(service: FakeService) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=API, transport=httpx.MockTransport(service.handler))


def _fast_timeouts() -> EvalTimeouts:
    return EvalTimeouts(
        outline_seconds=1.0,
        per_slide_seconds=0.5,
        export_seconds=1.0,
        poll_interval_seconds=0.01,
    )


class TestHappyPath:
    async def test_topic_case_full_flow_order(self) -> None:
        service = FakeService(pages=5)
        runner = EvalRunner(_client(service), timeouts=_fast_timeouts())
        results = await runner.run([_topic_case()])

        assert len(results) == 1
        artifacts = results[0]
        assert artifacts.ok is True
        assert artifacts.stage == "done"
        assert artifacts.export_succeeded is True
        assert artifacts.total_slides == 5
        assert artifacts.deck_response is not None

        # 关键编排顺序：注册 → 建项目 → 大纲生成 → 轮询 → 确认 → 页面生成 → 轮询 → 导出
        expected_order = [
            ("POST", "/api/v1/auth/register"),
            ("POST", "/api/v1/projects"),
            ("POST", "/api/v1/projects/proj-1/outline/generate"),
            ("GET", "/api/v1/projects/proj-1/outline"),
            ("POST", "/api/v1/projects/proj-1/outline/confirm"),
            ("POST", "/api/v1/projects/proj-1/deck/generate"),
            ("GET", "/api/v1/projects/proj-1/deck"),
            ("GET", "/api/v1/projects/proj-1/deck/export"),
        ]
        calls = service.calls
        for step in expected_order:
            assert step in calls, f"缺少编排步骤 {step}；实际调用：{calls}"
        # 主题题不应上传材料
        assert not any("/sources/upload" in path for _, path in calls)

    async def test_document_case_uploads_source(self, tmp_path: Path) -> None:
        service = FakeService(pages=5)
        runner = EvalRunner(_client(service), timeouts=_fast_timeouts())
        results = await runner.run([_doc_case(tmp_path)])

        assert results[0].ok is True
        assert any("/sources/upload" in path for _, path in service.calls)


class TestFailureCollection:
    async def test_outline_failure_collected_per_case(self) -> None:
        service = FakeService(pages=5, fail_outline=True)
        runner = EvalRunner(_client(service), timeouts=_fast_timeouts())
        results = await runner.run([_topic_case()])

        artifacts = results[0]
        assert artifacts.ok is False
        assert artifacts.stage == "outline_wait"
        assert "大纲生成失败" in (artifacts.error or "")

    async def test_outline_timeout_collected(self) -> None:
        service = FakeService(pages=5, outline_polls_before_done=10_000)
        runner = EvalRunner(_client(service), timeouts=_fast_timeouts())
        results = await runner.run([_topic_case()])

        assert results[0].ok is False
        assert "大纲生成超时" in (results[0].error or "")

    async def test_single_case_failure_does_not_break_run(self) -> None:
        # 两题：第一题大纲失败，第二题正常 → 整轮完成且各自结果独立
        service = FakeService(pages=5)

        class _Switching:
            def __init__(self) -> None:
                self.first = FakeService(pages=5, fail_outline=True)
                self.second = FakeService(pages=5)
                self.count = 0

            async def __call__(self, request):
                service.calls.append((request.method, request.url.path))
                if request.url.path.endswith("/auth/register"):
                    return _response({"access_token": "tok"})
                if request.url.path == "/api/v1/projects" and request.method == "POST":
                    self.count += 1
                    return _response({"id": f"proj-{self.count}"}, status=201)
                target = self.first if self.count == 1 else self.second
                return target.handler(request)

        switching = _Switching()
        switching.calls = service.calls
        client = httpx.AsyncClient(base_url=API, transport=httpx.MockTransport(switching))
        results = await EvalRunner(client, timeouts=_fast_timeouts()).run(
            [_topic_case("technology/rag"), _topic_case("technology/agent")]
        )

        assert results[0].ok is False
        assert results[1].ok is True

    async def test_http_422_reported_as_case_failure(self) -> None:
        service = FakeService(pages=5, reject_422=True)
        runner = EvalRunner(_client(service), timeouts=_fast_timeouts())
        results = await runner.run([_topic_case()])

        assert results[0].ok is False
        assert "HTTP 422" in (results[0].error or "")

    async def test_export_failure_recorded_not_fatal(self) -> None:
        service = FakeService(pages=5, fail_export=True)
        runner = EvalRunner(_client(service), timeouts=_fast_timeouts())
        results = await runner.run([_topic_case()])

        artifacts = results[0]
        # 导出失败不判定整题失败：主流程完成即 ok，导出成功与否单独记录
        assert artifacts.ok is True
        assert artifacts.export_succeeded is False
        assert any("导出失败" in event for event in artifacts.events)


class TestEnvironmentErrors:
    async def test_connection_error_aborts_run(self) -> None:
        def unreachable(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        client = httpx.AsyncClient(base_url=API, transport=httpx.MockTransport(unreachable))
        runner = EvalRunner(client, timeouts=_fast_timeouts())
        with pytest.raises(EvalEnvironmentError, match="无法连接 API"):
            await runner.run([_topic_case()])

    async def test_5xx_on_register_aborts_run(self) -> None:
        def server_error(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/auth/register"):
                return _response({"detail": "boom"}, status=503)
            return _response({}, status=404)

        client = httpx.AsyncClient(base_url=API, transport=httpx.MockTransport(server_error))
        runner = EvalRunner(client, timeouts=_fast_timeouts())
        with pytest.raises(EvalEnvironmentError):
            await runner.run([_topic_case()])


class TestRetrySemantics:
    async def test_failed_slide_triggers_retry_then_success(self) -> None:
        # 第一次轮询 generating → 第二次 ready；构造中途出现 failed 页
        service = FakeService(pages=5)
        poll_state = {"n": 0}

        original = service._deck_payload

        def mixed_payload(*, status_mix: str) -> dict:
            poll_state["n"] += 1
            if poll_state["n"] == 1:
                return original(status_mix="generating")
            if poll_state["n"] == 2:
                # 全部落定，但其中一页 failed：触发评测器逐页重试
                payload = original(status_mix="ready")
                payload["slides"][3]["status"] = "failed"
                payload["slides"][3]["error"] = "瞬时失败"
                return payload
            return original(status_mix="ready")

        service._deck_payload = mixed_payload  # type: ignore[method-assign]
        runner = EvalRunner(_client(service), timeouts=_fast_timeouts())
        results = await runner.run([_topic_case()])

        artifacts = results[0]
        assert artifacts.ok is True
        assert service.retry_calls == 1
        assert artifacts.retried_slides == 1
        assert artifacts.failed_slide_seen == 1

    async def test_failed_slide_after_retry_reports_failure(self) -> None:
        service = FakeService(pages=5, fail_deck_after_retry=True)
        poll_state = {"n": 0}
        original = service._deck_payload

        def stuck_payload(*, status_mix: str) -> dict:
            poll_state["n"] += 1
            if poll_state["n"] == 1:
                return original(status_mix="generating")
            if poll_state["n"] == 2:
                # 全部落定且一页 failed：触发重试
                payload = original(status_mix="ready")
                payload["slides"][0]["status"] = "failed"
                payload["slides"][0]["error"] = "永久失败"
                return payload
            # 重试轮询后仍 failed
            return original(status_mix="failed_stuck")

        service._deck_payload = stuck_payload  # type: ignore[method-assign]
        runner = EvalRunner(_client(service), timeouts=_fast_timeouts())
        results = await runner.run([_topic_case()])

        artifacts = results[0]
        assert artifacts.ok is False
        assert "重试后仍失败" in (artifacts.error or "")

    async def test_deck_timeout_collected(self) -> None:
        service = FakeService(pages=5, deck_polls_before_done=10_000)
        runner = EvalRunner(_client(service), timeouts=_fast_timeouts())
        results = await runner.run([_topic_case()])

        assert results[0].ok is False
        assert "页面生成超时" in (results[0].error or "")


class TestTitleExtraction:
    def test_title_from_book_brackets(self) -> None:
        from app.eval.runner import _title_from_case

        case = EvalCase(
            id="business/saas_model",
            input="生成一份《SaaS 商业模式》的 8 页 PPT",
            expected_pages=8,
            requirements=["包含定价模型"],
            category="business",
        )
        assert _title_from_case(case) == "SaaS 商业模式"
        plain = EvalCase(
            id="technology/rag",
            input="讲一下检索增强生成，8 页",
            expected_pages=8,
            requirements=["包含流程"],
            category="technology",
        )
        assert _title_from_case(plain).startswith("评测-")


class TestScoreCase:
    async def test_score_case_skips_when_not_ok(self, tmp_path: Path) -> None:
        artifacts = CaseArtifacts(case_id="x", ok=False, stage="outline_wait", error="超时")
        score = await score_case(_topic_case(), artifacts, None)
        assert score.structural is None
        assert score.judge is None
        assert score.hallucination is None

    async def test_score_case_document_runs_hallucination(self, tmp_path: Path) -> None:
        service = FakeService(pages=2)
        runner = EvalRunner(_client(service), timeouts=_fast_timeouts())
        artifacts = (await runner.run([_doc_case(tmp_path)]))[0]
        assert artifacts.ok

        class _FakeJudge:
            async def judge(self, case, deck):
                return JudgeVerdict.model_validate(
                    {
                        "requirements": [{"text": "包含总营收", "pass": True, "reason": "有"}],
                        "content_score": 8.0,
                    }
                )

        score = await score_case(_doc_case(tmp_path), artifacts, _FakeJudge())
        assert score.structural is not None
        assert score.judge is not None
        assert score.judge.content_score == 8.0
        # 材料与 deck 都含 1.86/24（fake deck 内容没有数字 → 0 个数字也成立）
        assert score.hallucination is not None

    async def test_score_case_judge_error_collected(self) -> None:
        service = FakeService(pages=2)
        runner = EvalRunner(_client(service), timeouts=_fast_timeouts())
        artifacts = (await runner.run([_topic_case()]))[0]
        assert artifacts.ok

        from app.llm.errors import InvalidModelOutputError

        class _BrokenJudge:
            async def judge(self, case, deck):
                raise InvalidModelOutputError("模型返回内容不符合约定结构")

        score: CaseScore = await score_case(_topic_case(), artifacts, _BrokenJudge())
        assert score.judge is None
        assert "InvalidModelOutputError" in (score.judge_error or "")
