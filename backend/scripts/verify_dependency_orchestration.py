"""依赖编排端到端验证脚本。

场景：
1. 批量编译三点环 C→D→E→C、依赖废弃 base_income 的 income、独立链 freq→count，
   编译回执逐条列出执行或拒绝结果；
2. 受理计划并执行：count@v1 口径下 freq=2，快照没有环内列及 income；
3. 目录变更：count 默认版切为 v2（两个可见版本），补齐收入上游 base_income；
4. 同一编号重下发旧计划：仍只得到 freq=2，不跟随目录默认版本变更；
5. 提交新计划：按新定义口径重算，快照得到 freq=3 和有效的 income，环路列仍无输出；
6. 从任务进度和快照读出实际执行范围、未执行原因和定义版本；旧快照按原版本保留。

运行：cd backend && .venv/bin/python scripts/verify_dependency_orchestration.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.compute_plan import ComputePlanService
from app.services.dependency import compile_fields
from app.services.feature_compute import FeatureComputeService
from app.services.feature_define import FeatureDefineService
from app.services.feature_snapshot import FeatureSnapshotService

define = FeatureDefineService()
plans = ComputePlanService()
compute = FeatureComputeService()
snapshots = FeatureSnapshotService()

PASSED = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASSED
    assert condition, f"断言失败：{label} {detail}"
    PASSED += 1
    print(f"  ✓ {label}{('：' + detail) if detail else ''}")


def define_id(name: str) -> int:
    for row in define.list_entries(size=200)[0]:
        if row.get("特征名称") == name:
            return int(row["id"])
    raise AssertionError(f"目录中找不到字段 {name}")


def produced_map(result: dict) -> dict:
    return {c["field"]: c for c in result["produced"]}


print("步骤0  目录初始状态：count@v1 默认、base_income@v1 已废弃、C/D/E 成环")
catalog = define.export_field_versions()
check("count 默认版本为 v1", catalog["count"]["default"] == "v1")
check("base_income@v1 已废弃", catalog["base_income"]["versions"]["v1"]["status"] == "已废弃")
check("C→D→E→C 依赖已登记", catalog["C"]["versions"]["v1"]["deps"] == ["D"]
      and catalog["D"]["versions"]["v1"]["deps"] == ["E"]
      and catalog["E"]["versions"]["v1"]["deps"] == ["C"])

print("\n步骤1  选中 C、D、E、income、freq 一次提交，批量编译")
selected = ["C", "D", "E", "income", "freq"]
ids = [define_id(name) for name in selected]
compiled = plans.compile_batch(ids, "批量编译验证-首轮")
receipt = {item["field"]: item for item in compiled["receipt"]}
for item in compiled["receipt"]:
    print(f"  回执 {item['field']}: {item['result']}"
          + (f"（{item['reason']}）" if item.get("reason") else ""))
check("回执逐条覆盖全部选中定义", set(receipt) == set(selected))
check("C/D/E 因环路被拒绝", all(receipt[f]["result"] == "拒绝" and "环路" in receipt[f]["reason"] for f in "CDE"))
check("income 因废弃引用被拒绝", receipt["income"]["result"] == "拒绝" and "已废弃" in receipt["income"]["reason"])
check("freq 编译通过进入可执行", receipt["freq"]["result"] == "执行")

plan1_id = compiled["plan_id"]
plan1 = plans.get_plan_detail(plan1_id)
check("可执行清单按拓扑序为 count→freq",
      [n["field"] for n in plan1["executable"]] == ["count", "freq"])
check("定版锁定 count@v1、freq@v1", plan1["locked_versions"] == {"count": "v1", "freq": "v1"})
check("freq 的直接依赖锁定确切版本", plan1["executable"][1]["deps"] == {"count": "v1"})
blocked = {b["field"]: b for b in plan1["blocked"]}
check("受影响列不进入可执行部分", set(blocked) == {"C", "D", "E", "income"})
check("环路诊断写明根字段与环路路径",
      blocked["C"]["root"] == "C" and blocked["C"]["path"] == "C → D → E → C")
check("废弃诊断写明根字段与引用路径",
      blocked["income"]["root"] == "base_income" and blocked["income"]["path"] == "income → base_income")

print("\n步骤2  受理计划：编号与输入版本冻结")
plan, message = plans.accept_plan(plan1_id)
check("计划已受理", plan is not None and plan["status"] == "已受理" and bool(plan["受理时间"]))
again, message2 = plans.accept_plan(plan1_id)
check("重复受理被拒绝", again is None and "已受理" in message2)

print("\n步骤3  执行计划：count@v1 口径计算 freq=2")
run1 = compute.execute_plan(plan1_id)
values1 = produced_map(run1)
check("freq=2", values1["freq"]["value"] == 2)
check("count=4（click 事件）", values1["count"]["value"] == 4)
check("快照没有环内列及 income", set(values1) == {"count", "freq"})
snap1 = snapshots.get_snapshot_detail(run1["snapshot_id"])
check("快照列出实际产出的列与各自版本",
      {c["field"]: c["version"] for c in snap1["产出明细"]} == {"count": "v1", "freq": "v1"})
check("快照列出所用依赖版本", snap1["依赖版本"] == {"count": "v1", "freq": "v1"})
progress1 = compute.query_compute_progress(run1["task_id"])
check("进度中能查到被阻塞的列",
      {b["field"] for b in progress1["blocked_columns"]} == {"C", "D", "E", "income"})

print("\n步骤4  目录变更：count 默认版切为 v2（两个可见版本），补齐收入上游")
define.upsert_field_version("count", "v2", {"logic": "count_all_events", "deps": [], "status": "已生效"})
define.set_default_version("count", "v2")
define.upsert_field_version("base_income", "v2", {"logic": "sum_base_salary", "deps": [], "status": "已生效"})
define.set_default_version("base_income", "v2")
catalog2 = define.export_field_versions()
check("count 存在 v1/v2 两个可见版本且默认为 v2",
      set(catalog2["count"]["versions"]) == {"v1", "v2"} and catalog2["count"]["default"] == "v2")
check("base_income 上游已补齐（v2 生效）",
      catalog2["base_income"]["default"] == "v2"
      and catalog2["base_income"]["versions"]["v2"]["status"] == "已生效")

print("\n步骤5  同一编号重下发旧计划：不跟随目录默认版本变更")
plan1_after = plans.get_plan_detail(plan1_id)
check("旧计划编号与输入保持稳定",
      plan1_after["计划编号"] == "PLAN-0001" and plan1_after["locked_versions"] == {"count": "v1", "freq": "v1"})
run2 = compute.execute_plan(plan1_id)
values2 = produced_map(run2)
check("重跑旧计划仍只得到 freq=2", values2["freq"]["value"] == 2)
check("重跑仍使用 count@v1", values2["count"]["version"] == "v1" and values2["count"]["value"] == 4)
check("旧计划两次执行各留一条快照", run2["snapshot_no"] != run1["snapshot_no"])

print("\n步骤6  补齐依赖后另交新计划，按新定义口径重算")
compiled2 = plans.compile_batch(ids, "批量编译验证-补齐后")
receipt2 = {item["field"]: item for item in compiled2["receipt"]}
for item in compiled2["receipt"]:
    print(f"  回执 {item['field']}: {item['result']}"
          + (f"（{item['reason']}）" if item.get("reason") else ""))
check("income 编译通过", receipt2["income"]["result"] == "执行")
check("环路列仍被拒绝", all(receipt2[f]["result"] == "拒绝" for f in "CDE"))
plan2_id = compiled2["plan_id"]
plan2 = plans.get_plan_detail(plan2_id)
check("新计划锁定 count@v2 与 base_income@v2",
      plan2["locked_versions"] == {"count": "v2", "freq": "v1", "base_income": "v2", "income": "v1"})
check("新计划可执行清单拓扑序",
      [n["field"] for n in plan2["executable"]] == ["base_income", "count", "freq", "income"])
plans.accept_plan(plan2_id)
run3 = compute.execute_plan(plan2_id)
values3 = produced_map(run3)
check("同一份原始数据按新定义得 freq=3", values3["freq"]["value"] == 3)
check("count@v2 口径得 count=6", values3["count"]["value"] == 6 and values3["count"]["version"] == "v2")
check("income 有效（base_income 12000 + 10% 奖金）",
      values3["income"]["value"] == 13200 and values3["base_income"]["value"] == 12000)
check("环路列仍无输出", not {"C", "D", "E"} & set(values3))

print("\n步骤7  旧快照按原版本保留")
snap1_kept = snapshots.get_snapshot_detail(run1["snapshot_id"])
kept = {c["field"]: (c["version"], c["value"]) for c in snap1_kept["产出明细"]}
check("首轮快照仍记录 count@v1 / freq=2",
      kept == {"count": ("v1", 4), "freq": ("v1", 2)})

print("\n步骤8  从任务进度和快照读出执行范围、未执行原因和定义版本")
progress3 = compute.query_compute_progress(run3["task_id"])
check("进度读出实际执行范围",
      progress3["executed_columns"] == ["base_income@v2", "count@v2", "freq@v1", "income@v1"])
check("进度读出未执行原因",
      {b["field"]: b["reason"] for b in progress3["blocked_columns"]} == {"C": "cycle", "D": "cycle", "E": "cycle"})
check("进度读出定义版本", progress3["locked_versions"]["count"] == "v2"
      and progress3["locked_versions"]["base_income"] == "v2")
snap3 = snapshots.get_snapshot_detail(run3["snapshot_id"])
check("快照读出产出列所用依赖及版本",
      {c["field"]: c["deps"] for c in snap3["产出明细"] if c["field"] == "income"} == {"income": {"base_income": "v2"}})

print("\n步骤9  执行结果与定义目录同步关联")
freq_row = [r for r in define.list_entries(size=200)[0] if r.get("特征名称") == "freq"][0]
check("freq 定义回写最近执行版本与快照",
      freq_row.get("最近执行版本") == "v1" and freq_row.get("最近快照编号") == run3["snapshot_no"])
income_row = [r for r in define.list_entries(size=200)[0] if r.get("特征名称") == "income"][0]
check("income 定义回写最近计划编号", income_row.get("最近计划编号") == "PLAN-0002")

print("\n步骤10  引擎级检查：缺失依赖诊断与间接依赖定版")
synthetic = {
    "a": {"default": "v1", "versions": {"v1": {"logic": "x", "deps": ["b"], "status": "已生效"}}},
    "b": {"default": "v2", "versions": {"v2": {"logic": "y", "deps": ["c"], "status": "已生效"}}},
    "c": {"default": "v3", "versions": {"v3": {"logic": "z", "deps": [], "status": "已生效"}}},
    "d": {"default": "v1", "versions": {"v1": {"logic": "w", "deps": ["ghost"], "status": "已生效"}}},
}
engine = compile_fields(["a", "d"], synthetic)
check("直接与间接依赖版本全部锁定",
      engine["locked_versions"] == {"a": "v1", "b": "v2", "c": "v3"})
check("缺失依赖写明根字段与路径",
      engine["blocked"][0]["root"] == "ghost" and engine["blocked"][0]["path"] == "d → ghost")

print(f"\n全部 {PASSED} 项断言通过。")
