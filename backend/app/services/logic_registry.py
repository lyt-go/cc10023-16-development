"""可执行计算逻辑注册表：特征版本通过 logic 键引用这里的纯函数。

每个逻辑接收 (原始输入, 上游列值) 返回列值；依赖编排执行时按拓扑序调用，
上游列值来自已计算完成的直接依赖。新增口径（如 count@v2）只需注册新函数。
"""
from __future__ import annotations

from typing import Any, Callable

LogicFn = Callable[[dict[str, Any], dict[str, Any]], Any]


def _count_click_events(raw: dict[str, Any], deps: dict[str, Any]) -> int:
    """count@v1 口径：只统计 click 事件。"""
    return sum(1 for event in raw.get("events", []) if event.get("type") == "click")


def _count_all_events(raw: dict[str, Any], deps: dict[str, Any]) -> int:
    """count@v2 口径：click 与 view 事件都计入。"""
    return sum(1 for event in raw.get("events", []) if event.get("type") in ("click", "view"))


def _freq_per_window(raw: dict[str, Any], deps: dict[str, Any]) -> int:
    """freq@v1：窗口内事件频次 = count / 窗口天数。"""
    days = int(raw.get("window_days", 1)) or 1
    return int(deps["count"]) // days


def _sum_base_salary(raw: dict[str, Any], deps: dict[str, Any]) -> int:
    """base_income@v2 口径：汇总薪资记录。"""
    return sum(int(record.get("amount", 0)) for record in raw.get("salary_records", []))


def _income_with_bonus(raw: dict[str, Any], deps: dict[str, Any]) -> int:
    """income@v1：基本工资 + 10% 奖金。"""
    base = int(deps["base_income"])
    return base + base // 10


def _cycle_passthrough(raw: dict[str, Any], deps: dict[str, Any]) -> Any:
    """环路字段占位逻辑：编译期即被环路诊断拦截，永远不会执行。"""
    return next(iter(deps.values()), None)


def _legacy_base_income(raw: dict[str, Any], deps: dict[str, Any]) -> Any:
    """base_income@v1 旧口径：版本已废弃，编译期拦截，不应被执行。"""
    raise RuntimeError("legacy_base_income 已废弃，不应进入可执行计划")


LOGIC_REGISTRY: dict[str, LogicFn] = {
    "count_click_events": _count_click_events,
    "count_all_events": _count_all_events,
    "freq_per_window": _freq_per_window,
    "sum_base_salary": _sum_base_salary,
    "income_with_bonus": _income_with_bonus,
    "cycle_passthrough": _cycle_passthrough,
    "legacy_base_income": _legacy_base_income,
}


def default_raw_input() -> dict[str, Any]:
    """演示用原始输入：同一份数据在 count@v1 口径下得 freq=2，在 count@v2 口径下得 freq=3。"""
    return {
        "window_days": 2,
        "events": [
            {"id": 1, "type": "click"},
            {"id": 2, "type": "click"},
            {"id": 3, "type": "click"},
            {"id": 4, "type": "click"},
            {"id": 5, "type": "view"},
            {"id": 6, "type": "view"},
        ],
        "salary_records": [
            {"month": "2026-09", "amount": 6000},
            {"month": "2026-10", "amount": 6000},
        ],
    }
