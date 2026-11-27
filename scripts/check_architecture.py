#!/usr/bin/env python3
"""检查 Friday Python 应用的静态导入环，输出可复查的依赖图。"""

import argparse
import ast
import json
from importlib.util import resolve_name
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ("homeplane", "friday")


def imports(node, package):
    if isinstance(node, ast.Import):
        yield from (name.name for name in node.names)
    elif isinstance(node, ast.ImportFrom):
        target = node.module or ""
        if node.level:
            target = resolve_name("." * node.level + target, package)
        yield target
        yield from (f"{target}.{name.name}" for name in node.names)


def dependency_graph(root):
    sources = {}
    for name in PACKAGES:
        for path in sorted((root / name).rglob("*.py")):
            if "vendor" in path.relative_to(root).parts:
                continue
            parts = path.relative_to(root).with_suffix("").parts
            module = ".".join(parts[:-1] if path.name == "__init__.py" else parts)
            sources[module] = path
    graph = {}
    for module, path in sources.items():
        package = module if path.name == "__init__.py" else module.rpartition(".")[0]
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        graph[module] = {
            dependency
            for node in ast.walk(tree)
            for dependency in imports(node, package)
            if dependency in sources and dependency != module
        }
    return graph


def reachable(source, graph):
    seen, pending = {source}, [source]
    while pending:
        unvisited = graph[pending.pop()] - seen
        seen.update(unvisited)
        pending.extend(unvisited)
    return seen


def cycle_edges(graph):
    """只保留起点可从终点到达的有向边。"""
    reach = {source: reachable(source, graph) for source in graph}
    return {
        (source, target)
        for source, targets in graph.items()
        for target in targets
        if source in reach[target]
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--graph", action="store_true", help="输出模块到依赖的 JSON 图")
    args = parser.parse_args()
    graph = dependency_graph(ROOT)
    if args.graph:
        print(json.dumps({source: sorted(targets) for source, targets in graph.items()}, indent=2, sort_keys=True))
        return 0
    allowed = json.loads(Path(__file__).with_name("architecture_cycles.json").read_text(encoding="utf-8"))
    known = {(source, target) for source, targets in allowed.items() for target in targets}
    actual = cycle_edges(graph)
    for source, target in sorted(actual - known):
        print(f"新增循环依赖：{source} → {target}")
    for source, target in sorted(known - actual):
        print(f"已解除的循环依赖，请移除旧记录：{source} → {target}")
    if actual != known:
        return 1
    print(f"导入检查通过。{len(graph)} 个模块，{len(actual)} 条已登记的循环边。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
