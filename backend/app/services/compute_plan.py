"""计算计划领域服务：依赖编排的编译、受理与版本冻结。

批量编译从特征定义目录导出字段版本数据，解析依赖、识别环路与废弃引用，
生成可供特征计算执行的输入计划；计划受理后编号与输入版本冻结，
同一编号重下发不跟随目录默认版本变更。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services.base import BaseModuleService
from app.services.dependency import compile_fields
from app.services.feature_define import FeatureDefineService
from app.store import store


class ComputePlanService(BaseModuleService):
    MODULE = "compute_plan"
    ENTITY = "计算计划"
    OPERATION_TERM = "编排"
    LIST_FIELDS = ["计划编号", "计划名称", "来源定义", "可执行列", "阻塞列", "依赖锁定", "受理时间", "计划状态"]
    REQUIRED_FIELDS = ["计划名称"]
    STATUS_ORDER = ["草稿", "已受理", "已执行", "已归档"]
    ACTION_RULES = {"受理计划": "已受理", "归档计划": "已归档"}
    NEGATIVE_ACTIONS = []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        """受理走专门的冻结逻辑，其余动作沿用通用状态流转。"""
        if action == "受理计划":
            return self.accept_plan(entry_id)
        return super().run_action(entry_id, action)

    # ── 依赖编排：批量编译 → 输入计划 ──
    def compile_batch(self, define_ids: list[int], plan_name: str | None = None) -> dict[str, Any]:
        """选中多条特征定义一次提交，逐条编译并生成一份输入计划。

        回执逐条列出执行或拒绝结果；被拒绝的列带阻塞诊断（根字段、缺失/环路路径），
        不进入可执行部分；独立列仍按拓扑顺序进入可执行清单并锁定确切版本。
        """
        define_service = FeatureDefineService()
        catalog = define_service.export_field_versions()
        requested: list[str] = []
        id_by_field: dict[str, int] = {}
        receipt_extra: list[dict[str, Any]] = []
        for did in define_ids:
            row = define_service.get_entry(did)
            if row is None:
                receipt_extra.append({"define_id": did, "field": None, "result": "拒绝", "reason": "定义不存在或已归档"})
                continue
            name = str(row.get("特征名称") or "").strip()
            if not name:
                receipt_extra.append({"define_id": did, "field": None, "result": "拒绝", "reason": "定义缺少特征名称"})
                continue
            if name not in id_by_field:
                requested.append(name)
            id_by_field[name] = did
        compiled = compile_fields(requested, catalog)
        receipt = [
            {"define_id": id_by_field.get(item["field"]), **item}
            for item in compiled["receipt"]
        ]
        receipt.extend(receipt_extra)
        executable = compiled["executable"]
        blocked = compiled["blocked"]
        locked = compiled["locked_versions"]
        rows = store.rows(self.MODULE)
        plan_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        plan_no = f"PLAN-{plan_id:04d}"
        plan = {
            "id": plan_id,
            "status": "草稿",
            "pending": True,
            "abnormal": bool(blocked),
            "计划编号": plan_no,
            "计划名称": plan_name or f"依赖编排计划 {plan_no}",
            "来源定义": ", ".join(requested),
            "可执行列": ", ".join(f"{n['field']}@{n['version']}" for n in executable) or "—",
            "阻塞列": ", ".join(b["field"] for b in blocked) or "—",
            "依赖锁定": ", ".join(f"{f}={v}" for f, v in locked.items()) or "—",
            "受理时间": "",
            "计划状态": "草稿",
            # 定版明细：直接与间接依赖的确切版本在此冻结，受理后不再变化
            "executable": executable,
            "blocked": blocked,
            "receipt": receipt,
            "locked_versions": locked,
            "执行记录": [],
        }
        rows.append(plan)
        return {
            "ok": True,
            "plan_id": plan_id,
            "plan_no": plan_no,
            "receipt": receipt,
            "executable": executable,
            "blocked": blocked,
            "locked_versions": locked,
            "message": f"编译完成：{len(executable)} 列可执行，{len(blocked)} 列被阻塞",
        }

    def accept_plan(self, plan_id: int) -> tuple[dict[str, Any] | None, str]:
        """受理计划：编号与输入版本正式冻结，重复受理被拒绝。"""
        plan = store.find(self.MODULE, plan_id)
        if plan is None:
            return None, f"计算计划 {plan_id} 不存在或已归档"
        if plan.get("status") != "草稿":
            return None, f"计划 {plan.get('计划编号')} 已受理，编号与输入已冻结，不能重复受理"
        plan["status"] = "已受理"
        plan["计划状态"] = "已受理"
        plan["受理时间"] = datetime.now().isoformat(timespec="seconds")
        plan["pending"] = True
        return plan, f"计划 {plan.get('计划编号')} 已受理，编号与输入版本冻结"

    def get_plan_detail(self, plan_id: int) -> dict[str, Any] | None:
        """计划详情：含冻结的可执行清单、阻塞诊断与版本锁定表。"""
        return store.find(self.MODULE, plan_id)
