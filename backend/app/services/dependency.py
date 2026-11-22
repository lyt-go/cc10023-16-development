"""依赖编排引擎：从字段版本目录解析依赖闭包，识别环路与废弃引用，生成可执行清单。

纯函数实现，不读写数据仓库；输入是特征定义目录导出的字段版本数据：

    {
        "count": {
            "default": "v1",
            "versions": {
                "v1": {"logic": "count_click_events", "deps": [], "status": "已生效"},
            },
        },
        ...
    }

编译结果分两部分：可执行列按拓扑序锁定确切版本（含直接与间接依赖），
被阻塞列带诊断（阻塞根字段、缺失/环路路径），不进入可执行部分。
"""
from __future__ import annotations

from typing import Any

REASON_TEXT = {
    "cycle": "依赖环路",
    "deprecated": "引用已废弃版本",
    "missing": "依赖缺失",
}


def resolve_field(field: str, catalog: dict[str, Any], path: tuple[str, ...] = ()) -> dict[str, Any]:
    """按目录默认版本解析单个字段的依赖子树。

    返回节点：{"field", "version", "logic", "deps": [...], "error": None|{type, path}}。
    节点带 error 时不再向下展开；path 记录从请求字段到当前字段的完整链路。
    """
    if field in path:
        return {"field": field, "version": None, "logic": None, "deps": [],
                "error": {"type": "cycle", "path": [*path, field]}}
    info = catalog.get(field)
    if not info or not info.get("versions"):
        return {"field": field, "version": None, "logic": None, "deps": [],
                "error": {"type": "missing", "path": [*path, field]}}
    version = info.get("default")
    spec = info["versions"].get(version)
    if spec is None:
        return {"field": field, "version": version, "logic": None, "deps": [],
                "error": {"type": "missing", "path": [*path, field]}}
    node = {"field": field, "version": version, "logic": spec.get("logic"), "deps": [], "error": None}
    if spec.get("status") == "已废弃":
        node["error"] = {"type": "deprecated", "path": [*path, field]}
        return node
    for dep in spec.get("deps", []):
        node["deps"].append(resolve_field(dep, catalog, (*path, field)))
    return node


def _walk(node: dict[str, Any]) -> list[dict[str, Any]]:
    nodes = [node]
    for child in node.get("deps", []):
        nodes.extend(_walk(child))
    return nodes


def _first_error(node: dict[str, Any]) -> dict[str, Any] | None:
    """先序找到离请求字段最近的阻塞节点（即阻塞的根字段）。"""
    if node.get("error"):
        return node
    for child in node.get("deps", []):
        found = _first_error(child)
        if found is not None:
            return found
    return None


def _pin(node: dict[str, Any]) -> dict[str, Any]:
    """定版：节点自身版本与直接依赖的确切版本一并锁定。"""
    return {
        "field": node["field"],
        "version": node["version"],
        "logic": node["logic"],
        "deps": {child["field"]: child["version"] for child in node["deps"]},
    }


def _diagnostic(field: str, error_node: dict[str, Any]) -> dict[str, Any]:
    err = error_node["error"]
    path = " → ".join(err["path"])
    item = {
        "field": field,
        "root": error_node["field"],
        "reason": err["type"],
        "reason_text": REASON_TEXT[err["type"]],
        "path": path,
    }
    if err["type"] == "cycle":
        item["message"] = f"依赖环路：{path}"
    elif err["type"] == "deprecated":
        item["root_version"] = error_node.get("version")
        item["message"] = (
            f"引用已废弃版本：{path}（{error_node['field']}@{error_node.get('version')} 已废弃）"
        )
    else:
        item["message"] = f"依赖缺失：{path}（目录中无 {error_node['field']} 的可用版本）"
    return item


def _topo_sort(nodes: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Kahn 拓扑排序：依赖在前，同层按字段名排序保证结果稳定。"""
    remaining = {name: set(node["deps"]) for name, node in nodes.items()}
    ordered: list[dict[str, Any]] = []
    while remaining:
        ready = sorted(name for name, deps in remaining.items() if not deps)
        if not ready:
            raise ValueError(f"依赖图存在环，无法拓扑排序：{sorted(remaining)}")
        for name in ready:
            ordered.append(nodes[name])
            del remaining[name]
            for deps in remaining.values():
                deps.discard(name)
    return ordered


def compile_fields(requested: list[str], catalog: dict[str, Any]) -> dict[str, Any]:
    """批量编译：对选中的字段逐一解析依赖闭包，输出可执行清单与阻塞诊断。

    返回：
    - executable：拓扑排序后的可执行节点，直接/间接依赖版本全部锁定；
    - blocked：被阻塞列的诊断（根字段、缺失/环路路径），不进入可执行部分；
    - receipt：编译回执，逐条列出每个选中字段的执行或拒绝结果；
    - locked_versions：整个可执行闭包的字段→版本锁定表。
    """
    executable: dict[str, dict[str, Any]] = {}
    blocked: list[dict[str, Any]] = []
    receipt: list[dict[str, Any]] = []
    seen: list[str] = []
    for field in requested:
        if field not in seen:
            seen.append(field)
    for field in seen:
        root = resolve_field(field, catalog)
        error_node = _first_error(root)
        if error_node is not None:
            item = _diagnostic(field, error_node)
            blocked.append(item)
            receipt.append({"field": field, "result": "拒绝", "reason": item["message"]})
            continue
        for node in _walk(root):
            executable[node["field"]] = _pin(node)
        receipt.append({"field": field, "result": "执行", "reason": None})
    ordered = _topo_sort(executable)
    return {
        "executable": ordered,
        "blocked": blocked,
        "receipt": receipt,
        "locked_versions": {node["field"]: node["version"] for node in ordered},
    }
