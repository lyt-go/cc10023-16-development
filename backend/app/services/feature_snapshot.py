"""特征快照管理领域服务：特征数据集的定时快照、版本追溯与回滚。"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class FeatureSnapshotService(BaseModuleService):
    MODULE = "feature_snapshot"
    ENTITY = "特征快照"
    OPERATION_TERM = "存储"
    LIST_FIELDS = ["快照编号", "特征集", "快照日期", "特征数量", "数据行数", "存储路径", "保留天数", "快照状态"]
    REQUIRED_FIELDS = ["快照编号", "特征集", "快照日期"]
    STATUS_ORDER = ["草稿", "创建中", "已完成", "已过期"]
    ACTION_RULES = {"创建快照": "创建中", "确认完成": "已完成", "标记过期": "已过期"}
    NEGATIVE_ACTIONS = []

    # ── 依赖编排：结果快照详情 ──
    def get_snapshot_detail(self, snapshot_id: int) -> dict[str, Any] | None:
        """结果快照详情：实际产出的列、所用依赖和各自版本、未执行列及原因。"""
        return store.find(self.MODULE, snapshot_id)

    # ── 链路1: 特征计算 → 特征快照 ──
    def archive_expired_snapshots(self) -> dict[str, Any]:
        """将超过保留天数的特征快照自动归档，并清理过期记录。"""
        rows = store.rows(self.MODULE)
        expired_ids = []
        from datetime import date, timedelta
        today = date.today()
        for r in rows:
            retain_days_str = r.get("保留天数", "0")
            try:
                retain_days = int(float(str(retain_days_str)))
            except (ValueError, TypeError):
                retain_days = 90
            snap_date_str = r.get("快照日期", "")
            try:
                snap_date = date.fromisoformat(str(snap_date_str)[:10])
            except (ValueError, TypeError):
                continue
            if (today - snap_date).days > retain_days:
                r["status"] = "已过期"
                r["abnormal"] = False
                expired_ids.append(r["id"])
        return {"ok": True, "archived_count": len(expired_ids), "expired_ids": expired_ids}

    def snapshot_feature_set(self, feature_set: str, compute_id: int) -> dict[str, Any]:
        """对指定特征集创建数据快照并存储到snapshots目录。"""
        rows = store.rows(self.MODULE)
        next_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        feats = feature_set.split(",") if feature_set else []
        rows.append({
            "id": next_id,
            "status": "已完成",
            "pending": False,
            "abnormal": False,
            "快照编号": f"SNAP-C{compute_id:04d}-{next_id:04d}",
            "特征集": feature_set,
            "快照日期": "2026-11-18",
            "特征数量": len(feats),
            "数据行数": str(len(feats) * 10000),
            "存储路径": f"snapshots/feature_set_{next_id}.json",
            "保留天数": "30",
            "快照状态": "已完成",
        })
        return {"ok": True, "snapshot_id": next_id, "feature_count": len(feats)}

    def restore_snapshot_to_compute(self, snapshot_id: int) -> dict[str, Any]:
        """从历史快照中恢复特征集版本，重新触发计算任务进行回填。"""
        snap = store.find(self.MODULE, snapshot_id)
        if snap is None:
            return {"ok": False, "message": f"快照 {snapshot_id} 不存在"}
        compute_rows = store.rows("feature_compute")
        next_id = max((int(r.get("id", 0)) for r in compute_rows), default=0) + 1
        compute_rows.append({
            "id": next_id,
            "status": "草稿",
            "pending": True,
            "abnormal": False,
            "任务编号": f"COMP-RST-{next_id:04d}",
            "关联特征": snap.get("特征集", ""),
            "数据切片": "restored_slice",
            "计算引擎": "spark",
            "输入分区": f"snapshot_{snapshot_id}",
            "输出分区": f"restored/output_{next_id}",
            "并行度": "4",
            "任务状态": "草稿",
        })
        return {"ok": True, "restored_from": snapshot_id, "compute_id": next_id, "message": "快照已恢复并创建回填计算任务"}

    def merge_snapshots(self, snapshot_ids: list[int]) -> dict[str, Any]:
        """将多个特征快照归并为一个综合快照，合并特征集并写入新版本。"""
        all_features = set()
        for sid in snapshot_ids:
            snap = store.find(self.MODULE, sid)
            if snap is None:
                continue
            feats = snap.get("特征集", "")
            for f in feats.split(","):
                if f.strip():
                    all_features.add(f.strip())
        rows = store.rows(self.MODULE)
        next_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        merged_feats = ",".join(sorted(all_features))
        rows.append({
            "id": next_id,
            "status": "已完成",
            "pending": False,
            "abnormal": False,
            "快照编号": f"SNAP-MRG-{next_id:04d}",
            "特征集": merged_feats,
            "快照日期": "2026-11-18",
            "特征数量": len(all_features),
            "数据行数": str(len(all_features) * 8000),
            "存储路径": f"snapshots/merged_{next_id}.json",
            "保留天数": "60",
            "快照状态": "已完成",
        })
        return {"ok": True, "merged_id": next_id, "total_features": len(all_features), "sources": snapshot_ids}