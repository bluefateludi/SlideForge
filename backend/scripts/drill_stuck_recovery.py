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

# setup 阶段把创建者登录态存这里，verify/retry 复用（换账号拿不到项目所有权）
SESSION_FILE = BACKEND_ROOT / "var" / "drill_session.json"


def _load_session() -> dict:
    import json

    return json.loads(SESSION_FILE.read_text(encoding="utf-8"))


def _save_session(token: str, project_id: str) -> None:
    import json

    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    SESSION_FILE.write_text(
        json.dumps({"token": token, "project_id": project_id}), encoding="utf-8"
    )


async def _client() -> httpx.AsyncClient:
    # trust_env=False：绕过 Windows 系统代理（应用侧 httpx 同款做法），
    # 否则 localhost 请求会被系统代理劫持返回非 JSON
    return httpx.AsyncClient(base_url=BASE, timeout=30.0, trust_env=False)


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
        _save_session(headers["Authorization"], project_id)
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
    session = _load_session()
    headers = {"Authorization": session["token"]}
    async with await _client() as client:
        response = await client.get(f"/projects/{project_id}/deck", headers=headers)
        response.raise_for_status()
        deck = response.json()
    print(f"deck status={deck['status']} ready={deck['ready']} failed={deck['failed']}")
    for slide in deck["slides"]:
        print(
            f"  第{slide['position']}页 status={slide['status']}"
            f" error_code={slide.get('error_code')} error={slide.get('error')}"
        )
    generating = [s for s in deck["slides"] if s["status"] == "generating"]
    worker_dead = [s for s in deck["slides"] if s.get("error_code") == "worker_dead"]
    if generating:
        raise SystemExit(f"仍有 {len(generating)} 个 generating 页未被对账——API 是否跑的新代码？")
    if not worker_dead:
        raise SystemExit("没有页被复位为 worker_dead——演练前提不成立")
    print("验证通过：卡死页已全部复位（failed/worker_dead），可重试恢复")


async def cmd_retry(project_id: str) -> None:
    """重新生成全部非 ready 页（等价于用户在界面点「重新生成」）并轮询到收敛。"""
    session = _load_session()
    headers = {"Authorization": session["token"]}
    async with await _client() as client:
        accepted = await client.post(
            f"/projects/{project_id}/deck/generate", json={}, headers=headers
        )
        if accepted.status_code == 409 and "均已生成" in accepted.text:
            print("所有页面均已就绪，无需重试")
            return
        accepted.raise_for_status()
        print(f"再生成已受理：{accepted.json()['pending']} 页")

        deck = {}
        for _ in range(90):
            await asyncio.sleep(2)
            response = await client.get(f"/projects/{project_id}/deck", headers=headers)
            response.raise_for_status()
            deck = response.json()
            if deck["ready"] + deck["failed"] == deck["total"]:
                break
        else:
            raise SystemExit("再生成超时未收敛")
        print(f"最终：status={deck['status']} ready={deck['ready']} failed={deck['failed']}")
        for slide in deck["slides"]:
            print(f"  第{slide['position']}页 status={slide['status']}")
        if deck["failed"]:
            raise SystemExit(f"仍有 {deck['failed']} 页失败")
        print("演练闭环：kill worker → 对账复位 → 重试 → 全部就绪")


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
