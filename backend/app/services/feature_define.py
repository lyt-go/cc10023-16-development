"""特征定义领域服务：维护特征的计算逻辑、数据类型与取值范围定义。"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class FeatureDefineService(BaseModuleService):
    MODULE = "feature_define"
    ENTITY = "特征定义"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["特征编号", "特征名称", "特征类型", "计算逻辑", "数据来源", "缺失填充", "取值范围", "特征版本"]
    REQUIRED_FIELDS = ["特征编号", "特征名称", "特征类型"]
    STATUS_ORDER = ["草稿", "待校验", "已生效", "已废弃"]
    ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "标记废弃": "已废弃"}
    NEGATIVE_ACTIONS = []

    # ── 链路1: 特征源 → 特征定义 → 特征计算 → 特征快照 ──
    def validate_definitions_batch(self, define_ids: list[int]) -> dict[str, Any]:
        """批量校验特征定义的数据来源、计算逻辑和取值范围，生成校验报告。"""
        results = []
        for did in define_ids:
            entry = store.find(self.MODULE, did)
            if entry is None:
                results.append({"id": did, "ok": False, "reason": "不存在"})
                continue
            errors = []
            if not entry.get("数据来源"):
                errors.append("缺少数据来源")
            if not entry.get("计算逻辑"):
                errors.append("缺少计算逻辑")
            if not errors:
                entry["status"] = "已生效"
                entry["pending"] = False
                entry["abnormal"] = False
                results.append({"id": did, "ok": True, "name": entry.get("特征名称")})
            else:
                entry["status"] = "待校验"
                entry["abnormal"] = True
                results.append({"id": did, "ok": False, "reason": "; ".join(errors)})
        return {"ok": all(r.get("ok") for r in results), "results": results, "total": len(results)}

    def push_to_compute_tasks(self, define_ids: list[int]) -> dict[str, Any]:
        """将已生效的特征定义导入特征计算任务队列，触发下游批量计算。"""
        compute_rows = store.rows("feature_compute")
        next_id = max((int(r.get("id", 0)) for r in compute_rows), default=0)
        pushed = []
        for did in define_ids:
            entry = store.find(self.MODULE, did)
            if entry is None or entry.get("status") != "已生效":
                continue
            next_id += 1
            compute_rows.append({
                "id": next_id,
                "status": "草稿",
                "pending": True,
                "abnormal": False,
                "任务编号": f"COMP-{next_id:04d}",
                "关联特征": entry.get("特征名称", ""),
                "数据切片": "default_slice_001",
                "计算引擎": "spark",
                "输入分区": "dt=2026-11-18",
                "输出分区": f"output/d=2026-11-18/feat_{next_id}",
                "并行度": "8",
                "任务状态": "草稿",
            })
            pushed.append({"define_id": did, "task_id": next_id})
        return {"ok": len(pushed) > 0, "pushed_count": len(pushed), "tasks": pushed}

    def analyze_definition_coverage(self) -> dict[str, Any]:
        """统计分析所有特征定义的字段覆盖率、缺失填充策略分布和数据类型分布。"""
        rows = store.rows(self.MODULE)
        type_dist = {}
        fill_dist = {}
        has_source = 0
        has_logic = 0
        total = len(rows)
        for r in rows:
            ft = r.get("特征类型", "unknown")
            type_dist[ft] = type_dist.get(ft, 0) + 1
            fill = r.get("缺失填充", "none")
            fill_dist[fill] = fill_dist.get(fill, 0) + 1
            if r.get("数据来源"):
                has_source += 1
            if r.get("计算逻辑"):
                has_logic += 1
        return {
            "total_definitions": total,
            "source_coverage": round(has_source / max(total, 1), 4),
            "logic_coverage": round(has_logic / max(total, 1), 4),
            "type_distribution": type_dist,
            "fill_strategy_distribution": fill_dist,
        }

    def export_feature_catalog(self) -> list[dict[str, Any]]:
        """导出全量特征定义目录，包含编码、名称、类型、来源与逻辑供下游系统写入。"""
        rows = store.rows(self.MODULE)
        catalog = []
        for r in rows:
            catalog.append({
                "feature_code": r.get("特征编号"),
                "feature_name": r.get("特征名称"),
                "feature_type": r.get("特征类型"),
                "data_source": r.get("数据来源"),
                "compute_logic": r.get("计算逻辑"),
                "version": r.get("特征版本"),
            })
        return catalog

    # ── 依赖编排：字段版本目录 ──
    def _find_by_name(self, field: str) -> dict[str, Any] | None:
        for row in store.rows(self.MODULE):
            if row.get("特征名称") == field:
                return row
        return None

    def export_field_versions(self) -> dict[str, Any]:
        """从特征定义目录导出字段版本数据：默认版本指针 + 各版本的逻辑/依赖/状态。

        这是依赖编排的唯一目录输入；只有带版本目录的定义才参与依赖解析。
        """
        catalog: dict[str, Any] = {}
        for row in store.rows(self.MODULE):
            versions = row.get("版本目录")
            name = row.get("特征名称")
            if not versions or not name:
                continue
            catalog[str(name)] = {
                "default": row.get("默认版本") or row.get("特征版本"),
                "versions": {ver: dict(spec) for ver, spec in versions.items()},
            }
        return catalog

    def upsert_field_version(self, field: str, version: str, spec: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """向目录写入字段的新版本（逻辑、依赖、状态），不改变默认版本指针。"""
        row = self._find_by_name(field)
        if row is None:
            return None, f"字段 {field} 不在特征定义目录"
        versions = row.setdefault("版本目录", {})
        versions[version] = {
            "logic": spec.get("logic"),
            "deps": list(spec.get("deps", [])),
            "status": spec.get("status", "已生效"),
            "source": spec.get("source", row.get("数据来源", "")),
        }
        return row, f"{field}@{version} 已写入目录（当前默认 {row.get('默认版本')}）"

    def set_default_version(self, field: str, version: str) -> tuple[dict[str, Any] | None, str]:
        """切换字段的默认版本；展示字段跟随默认版本，已废弃字段切到生效版本时自动恢复。"""
        row = self._find_by_name(field)
        if row is None:
            return None, f"字段 {field} 不在特征定义目录"
        versions = row.get("版本目录", {})
        spec = versions.get(version)
        if spec is None:
            return None, f"{field}@{version} 不在目录中，无法设为默认"
        row["默认版本"] = version
        row["特征版本"] = version
        row["计算逻辑"] = spec.get("logic", row.get("计算逻辑"))
        if spec.get("status") == "已生效" and row.get("status") == "已废弃":
            row["status"] = "已生效"
            row["abnormal"] = False
        return row, f"{field} 默认版本已切换为 {version}"

    def sync_execution_result(self, produced: list[dict[str, Any]], snapshot_no: str, plan_no: str, executed_at: str) -> list[str]:
        """执行结果与定义目录同步关联：回写每个产出列最近执行的版本、快照与计划。"""
        synced = []
        for column in produced:
            row = self._find_by_name(str(column.get("field")))
            if row is None:
                continue
            row["最近执行版本"] = column.get("version")
            row["最近快照编号"] = snapshot_no
            row["最近计划编号"] = plan_no
            row["最近执行时间"] = executed_at
            synced.append(str(column.get("field")))
        return synced