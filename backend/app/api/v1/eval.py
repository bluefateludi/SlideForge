"""评测 run 只读查询端点（eval#4）。

run 记录由 scripts/run_eval.py 跑完报告后直连数据库写入；这里只提供
列表与详情两个 GET，无创建/更新/删除（评测结果的唯一入口是评测流程）。
任何登录用户可查：数据不含用户维度的隔离信息。
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.db import get_session
from app.models.eval_run import EvalRun
from app.models.user import User
from app.schemas.eval import EvalRunDetail, EvalRunPublic

router = APIRouter(prefix="/eval/runs", tags=["eval"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
CurrentUser = Annotated[User, Depends(get_current_user)]


@router.get("", response_model=list[EvalRunPublic])
async def list_runs(
    session: SessionDep,
    current_user: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[EvalRun]:
    """按时间倒序返回历史 run 列表（只含聚合指标列）。"""
    result = await session.execute(
        select(EvalRun).order_by(EvalRun.created_at.desc(), EvalRun.id.desc()).limit(limit)
    )
    return list(result.scalars())


@router.get("/{run_id}", response_model=EvalRunDetail)
async def get_run(
    run_id: Annotated[uuid.UUID, Path()],
    session: SessionDep,
    current_user: CurrentUser,
) -> EvalRun:
    """单次 run 详情：聚合指标 + 逐题明细 + 分类均分。"""
    run = await session.get(EvalRun, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="评测 run 不存在")
    return run
