"""依赖编排接口：目录版本管理、批量编译、计划受理与执行、进度与快照查询。

通用资源路由只覆盖清单/登记/动作，依赖编排的复合操作由这里显式暴露。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services.compute_plan import ComputePlanService
from app.services.feature_compute import FeatureComputeService
from app.services.feature_define import FeatureDefineService
from app.services.feature_snapshot import FeatureSnapshotService

router = APIRouter(prefix="/api/orchestration", tags=["依赖编排"])

plan_service = ComputePlanService()
compute_service = FeatureComputeService()
define_service = FeatureDefineService()
snapshot_service = FeatureSnapshotService()


class CompilePayload(BaseModel):
    define_ids: list[int]
    plan_name: str | None = None


class ExecutePayload(BaseModel):
    raw_data: dict[str, Any] | None = None


class VersionPayload(BaseModel):
    logic: str
    deps: list[str] = Field(default_factory=list)
    status: str = "已生效"
    source: str = ""


class DefaultVersionPayload(BaseModel):
    version: str


@router.get("/catalog")
def export_catalog() -> dict[str, Any]:
    """从特征定义目录导出字段版本数据（默认版本指针 + 各版本依赖/逻辑/状态）。"""
    return {"catalog": define_service.export_field_versions()}


@router.post("/catalog/{field}/versions/{version}")
def upsert_catalog_version(field: str, version: str, payload: VersionPayload) -> dict[str, Any]:
    """向目录写入字段新版本；不影响已受理计划锁定的版本。"""
    row, message = define_service.upsert_field_version(field, version, payload.model_dump())
    if row is None:
        raise HTTPException(status_code=404, detail=message)
    return {"ok": True, "message": message}


@router.post("/catalog/{field}/default")
def switch_catalog_default(field: str, payload: DefaultVersionPayload) -> dict[str, Any]:
    """切换字段默认版本；只影响之后编译的新计划。"""
    row, message = define_service.set_default_version(field, payload.version)
    if row is None:
        raise HTTPException(status_code=404, detail=message)
    return {"ok": True, "message": message}


@router.post("/compile")
def compile_plan(payload: CompilePayload) -> dict[str, Any]:
    """选中多条定义一次提交，编译回执逐条列出执行或拒绝结果。"""
    return plan_service.compile_batch(payload.define_ids, payload.plan_name)


@router.post("/plans/{plan_id}/accept")
def accept_plan(plan_id: int) -> dict[str, Any]:
    """受理计划：编号与输入版本冻结。"""
    plan, message = plan_service.accept_plan(plan_id)
    if plan is None:
        raise HTTPException(status_code=409, detail=message)
    return {"ok": True, "message": message, "plan": plan}


@router.get("/plans/{plan_id}")
def plan_detail(plan_id: int) -> dict[str, Any]:
    """计划详情：冻结的可执行清单、阻塞诊断与版本锁定表。"""
    plan = plan_service.get_plan_detail(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"计算计划 {plan_id} 不存在或已归档")
    return plan


@router.post("/plans/{plan_id}/execute")
def execute_plan(plan_id: int, payload: ExecutePayload | None = None) -> dict[str, Any]:
    """以受理计划为输入执行计算，生成结果快照并回写任务详情。"""
    result = compute_service.execute_plan(plan_id, (payload or ExecutePayload()).raw_data)
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("message"))
    return result


@router.get("/tasks/{task_id}/progress")
def task_progress(task_id: int) -> dict[str, Any]:
    """任务进度：实际执行范围、被阻塞的列及原因、定义版本。"""
    progress = compute_service.query_compute_progress(task_id)
    if not progress.get("ok"):
        raise HTTPException(status_code=404, detail=f"计算任务 {task_id} 不存在")
    return progress


@router.get("/snapshots/{snapshot_id}")
def snapshot_detail(snapshot_id: int) -> dict[str, Any]:
    """结果快照：实际产出的列、所用依赖和各自版本。"""
    snapshot = snapshot_service.get_snapshot_detail(snapshot_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail=f"特征快照 {snapshot_id} 不存在")
    return snapshot
