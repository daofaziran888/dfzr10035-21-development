"""采集缺口复核接口：按管道复核与采集证据检索、标定、派发和核对。

这些接口放在通用 ``/api/{module}`` 资源路由之前注册，避免固定路径被动态路径抢先匹配。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.services import service_for
from app.services.collection_gap import CollectionGapService

router = APIRouter(prefix="/api/collection-gap", tags=["采集缺口复核"])


def _service() -> CollectionGapService:
    service = service_for("collection_gap")
    if not isinstance(service, CollectionGapService):  # pragma: no cover - 注册保证
        raise HTTPException(status_code=500, detail="采集缺口服务未注册")
    return service


class SelectPayload(BaseModel):
    group_ids: list[int] = Field(default_factory=list)
    selected: bool = True


class EvidencePayload(BaseModel):
    pipeline: str
    start: int
    end: int
    conclusion: str = "失败"
    source: str = "人工上报"
    source_id: str = ""
    group_id: int | None = None
    remark: str = ""


class MarkCollectedPayload(BaseModel):
    pipeline: str
    start: int
    end: int
    source: str = "人工标定"
    source_id: str = "manual-check"
    group_id: int | None = None
    remark: str = ""


class RangeAdjustPayload(BaseModel):
    start: int
    end: int


class DispatchPayload(BaseModel):
    group_ids: list[int] = Field(default_factory=list)
    start: int | None = None
    end: int | None = None
    operator: str = "治理员"
    interrupt: bool = False


@router.get("/search")
def search(
    review_no: str | None = Query(default=None),
    pipeline: str | None = Query(default=None),
    start: int | None = Query(default=None),
    end: int | None = Query(default=None),
    status: str | None = Query(default=None),
    keyword: str | None = Query(default=None),
    include_duplicates: bool = Query(default=False),
    only_pending: bool = Query(default=True),
) -> dict[str, Any]:
    """按管道复核记录和采集证据关联，筛选待核对的采集异常组。"""
    return _service().search_groups(
        review_no=review_no,
        pipeline=pipeline,
        start=start,
        end=end,
        status=status,
        keyword=keyword,
        include_duplicates=include_duplicates,
        only_pending=only_pending,
    )


@router.post("/select")
def select(payload: SelectPayload) -> dict[str, Any]:
    service = _service()
    items = []
    for group_id in payload.group_ids:
        group = service.select_group(group_id, payload.selected)
        if group is None:
            raise HTTPException(status_code=404, detail=f"采集异常组 {group_id} 不存在")
        items.append(group)
    return {
        "ok": True,
        "message": f"已{'选中' if payload.selected else '取消选中'}{len(items)}个采集异常组",
        "items": items,
    }


@router.post("/evidence")
def add_evidence(payload: EvidencePayload) -> dict[str, Any]:
    """登记采集证据；完全相同的同一事实只保留一份。"""
    return _service().add_evidence(
        pipeline=payload.pipeline,
        start=payload.start,
        end=payload.end,
        conclusion=payload.conclusion,
        source=payload.source,
        source_id=payload.source_id,
        group_id=payload.group_id,
        remark=payload.remark,
    )


@router.post("/mark-collected")
def mark_collected(payload: MarkCollectedPayload) -> dict[str, Any]:
    """提交前把一段区间标成已经实采，后续判定会自动扣减。"""
    return _service().mark_collected(
        pipeline=payload.pipeline,
        start=payload.start,
        end=payload.end,
        source=payload.source,
        source_id=payload.source_id,
        group_id=payload.group_id,
        remark=payload.remark,
    )


@router.post("/dispatch")
def dispatch(payload: DispatchPayload) -> dict[str, Any]:
    """按选中的异常组派发补采；支持中断并保留原检索条件。"""
    return _service().dispatch(
        group_ids=payload.group_ids,
        start=payload.start,
        end=payload.end,
        operator=payload.operator,
        interrupt=payload.interrupt,
    )


@router.post("/dispatch/{dispatch_id}/retry")
def retry_dispatch(dispatch_id: int) -> dict[str, Any]:
    """派发中断后按原检索条件重试。"""
    return _service().retry_dispatch(dispatch_id)


@router.get("/review-list")
def review_list() -> dict[str, Any]:
    """复核清单：显示派发结果和实际影响证据。"""
    return {"items": _service().review_list(), "total": len(_service().review_list())}


@router.get("/dispatches")
def dispatches() -> dict[str, Any]:
    items = _service().list_dispatches()
    return {"items": items, "total": len(items)}


@router.get("/tasks")
def tasks() -> dict[str, Any]:
    items = _service().collection_tasks()
    return {"items": items, "total": len(items)}


@router.get("/verify")
def verify(group_id: int | None = Query(default=None)) -> dict[str, Any]:
    """核对检索结果、治理记录可执行区间和重放队列对应的采集任务。"""
    return _service().verify(group_id=group_id)


@router.get("/{group_id}/detail")
def detail(group_id: int) -> dict[str, Any]:
    entry = _service().get_group_detail(group_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"采集异常组 {group_id} 不存在")
    return entry


@router.post("/{group_id}/range-adjust")
def range_adjust(group_id: int, payload: RangeAdjustPayload) -> dict[str, Any]:
    result = _service().adjust_group_range(group_id, payload.start, payload.end)
    if result is None:
        raise HTTPException(status_code=404, detail=f"采集异常组 {group_id} 不存在")
    return result
