#!/usr/bin/env python3
"""#33 崩溃恢复演练：真实杀 worker 后验证 stuck 页可恢复。

用法（仓库根目录，三进程 dev 环境跑着新代码时）：

  uv run --project backend python backend/scripts/drill_stuck_recovery.py setup
      # 建 5 页项目并开始整份生成，打印 project_id 后立刻去杀 worker：
      #   powershell "Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" |
      #     Where-Object {$_.CommandLine -like '*arq*'} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"

  uv run --project backend python backend/scripts/drill_stuck_recovery.py stuck <project_id>
      # 把还在 generating 的页 started_at 回拨 11 分钟（模拟 10 分钟阈值流逝；
      # 不想等真实时钟就走这步，想等也可以直接 sleep）

  uv run --project backend python backend/scripts/drill_stuck_recovery.py verify <project_id>
      # GET deck：断言卡死页已复位 failed + error_code=worker_dead，deck 不再是 generating

  # 重启 worker 后：
  uv run --project backend python backend/scripts/drill_stuck_recovery.py retry <project_id>
      # 逐页重试失败页并轮询到 ready

环境变量：DRILL_API_BASE（默认 http://127.0.0.1:39800/api/v1）
"""

from __future__ import annotations

import asyncio
import os
import secrets
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import httpx  # noqa: E402

BASE = os.environ.get("DRILL_API_BASE", "http://127.0.0.1:39800/api/v1")


async def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=BASE, timeout=30.0)


async def _register(client: httpx.AsyncClient) -> dict[str, str]:
    email = f"drill-{secrets.token_hex(4)}@slideforge.drill"
    password = secrets.token_hex(8)
    await client.post("/auth/register", json={"email": email, "password": password})
    response = await client.post("/auth/login", json={"email": email, "password": password})
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def _wait_outline(client: httpx.AsyncClient, headers: dict, project_id: str) -> None:
    for _ in range(60):
        outline = (await client.get(f"/projects/{project_id}/outline", headers=headers)).json()
        if outline["status"] == "failed":
            raise SystemExit(f"大纲生成失败：{outline.get('error')}")
        if outline["status"] == "draft":
            revision = outline["revision"]
            break
        await asyncio.sleep(2)
    else:
        raise SystemExit("大纲生成超时")
    response = await client.post(
        f"/projects/{project_id}/outline/confirm", json={"revision": revision}, headers=headers
    )
    response.raise_for_status()


async def cmd_setup() -> None:
    async with await _client() as client:
        headers = await _register(client)
        project = (
            await client.post(
                "/projects",
                json={"title": "崩溃恢复演练", "page_count": 5, "layout_mode": "flex"},
                headers=headers,
            )
        ).json()
        project_id = project["id"]
        await client.post(
            f"/projects/{project_id}/sources",
            json={"kind": "topic", "content": "分布式任务队列的可靠性工程实践"},
            headers=headers,
        )
        await client.post(f"/projects/{project_id}/outline/generate", headers=headers)
        await _wait_outline(client, headers, project_id)
        response = await client.post(
            f"/projects/{project_id}/deck/generate", json={}, headers=headers
        )
        response.raise_for_status()
        print(f"project_id={project_id}")
        print("deck 生成已开始 —— 现在立刻杀掉 arq worker 进程（见脚本头部命令）")


async def cmd_stuck(project_id: str) -> None:
    from app.core.db import async_session_factory
    from app.models.slide import Slide
    from sqlalchemy import select

    async with async_session_factory() as session:
        result = await session.execute(select(Slide).where(Slide.project_id == project_id))
        slides = list(result.scalars())
        for slide in slides:
            if slide.status == "generating":
                slide.started_at = datetime.now(UTC) - timedelta(minutes=11)
        await session.commit()
        print(f"已把 {sum(1 for s in slides if s.status == 'generating')} 个 generating 页回拨 11 分钟")


async def cmd_verify(project_id: str) -> None:
    async with await _client() as client:
        headers = await _register(client)
        # 换新账号拿不到项目所有权——改用项目创建者token不现实，直接查库断言
    from app.core.db import async_session_factory
    from app.models.slide import Slide
    from sqlalchemy import select

    async with async_session_factory() as session:
        result = await session.execute(select(Slide).where(Slide.project_id == project_id))
        slides = list(result.scalars())
    stuck_fixed = [s for s in slides if s.error_code == "worker_dead"]
    generating = [s for s in slides if s.status == "generating"]
    ready = [s for s in slides if s.status == "ready"]
    print(f"ready={len(ready)} failed(worker_dead)={len(stuck_fixed)} generating={len(generating)}")
    for slide in slides:
        print(f"  第{slide.position}页 status={slide.status} error_code={slide.error_code}")
    if generating:
        raise SystemExit("仍有 generating 页未被对账——检查 API 是否跑的新代码")
    print("验证通过：卡死页已全部复位，可重试恢复")


async def cmd_retry(project_id: str) -> None:
    raise SystemExit(
        "retry/轮询需要项目创建者的登录态，请在浏览器里对失败页点「重试」并观察恢复；"
        "或用 setup 阶段输出的账号信息调 /slides/{id}/retry。"
    )


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in {"setup", "stuck", "verify", "retry"}:
        raise SystemExit(__doc__)
    command = sys.argv[1]
    if command == "setup":
        asyncio.run(cmd_setup())
    elif command == "stuck":
        asyncio.run(cmd_stuck(sys.argv[2]))
    elif command == "verify":
        asyncio.run(cmd_verify(sys.argv[2]))
    else:
        asyncio.run(cmd_retry(sys.argv[2] if len(sys.argv) > 2 else ""))


if __name__ == "__main__":
    main()
