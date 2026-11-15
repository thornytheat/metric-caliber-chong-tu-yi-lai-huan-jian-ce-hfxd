"""指标改名迁移：把一组改名当作一次原子迁移（纯函数，不触碰数据库与 Web 层）。

背景：逐个改下游名时，注册表会处在「一半公式认旧名、一半认新名」的中间态，
体检与影响面会对同一份注册表给出两种结果。因此一组改名必须：

1. 先 **预演（preview）**：列全受影响公式、新旧名称冲突、预演后的依赖变化；
2. 预演通过后 **一次性切换（commit）**：指标行改名 + 全部下游表达式同步改写，
   注册表不存在半新半旧的中间态；
3. 切换失败由存储层回滚到旧版注册表（见 store.apply_rename_migration）；
4. 重试按改名集合的确定性指纹复用同一条迁移记录，不重复产生迁移记录或版本；
5. 迁移后旧名进入别名表，历史公式仍可用旧名解析，并可查到对应版本与迁移来源。

本模块只做「给定一份注册表 + 一组改名，算出预演结果 / 应用后的新注册表」，
事务、快照与回滚全部在 store 层完成。
"""

from __future__ import annotations

import hashlib
import re
from copy import deepcopy

from . import expr, graph as graph_mod, registry

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")

# 迁移状态
STATUS_DRAFT = "draft"          # 仅预演、尚未切换
STATUS_COMMITTED = "committed"  # 一次性切换成功
STATUS_ROLLED_BACK = "rolled_back"  # 切换失败、注册表已恢复旧版


class MigrationError(ValueError):
    """改名请求非法（HTTP 400）。"""

    def __init__(self, message: str, code: str = "invalid_migration", field: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.field = field

    def as_dict(self) -> dict:
        return {"error": self.message, "code": self.code, "field": self.field}


def normalize_renames(renames: list[dict]) -> list[dict]:
    """规整改名请求：[{'old_name','new_name'}]，去空白、按旧名排序去重。"""
    if not isinstance(renames, list) or not renames:
        raise MigrationError("改名清单不能为空", "empty_renames", "renames")
    cleaned: list[dict] = []
    seen_olds: set[str] = set()
    for index, item in enumerate(renames):
        if not isinstance(item, dict):
            raise MigrationError(f"第 {index + 1} 条改名不是对象", "bad_rename", f"renames.{index}")
        old_name = str(item.get("old_name", "")).strip()
        new_name = str(item.get("new_name", "")).strip()
        if not old_name:
            raise MigrationError(f"第 {index + 1} 条改名缺少 old_name", "missing_old_name", f"renames.{index}")
        if not new_name:
            raise MigrationError(f"第 {index + 1} 条改名缺少 new_name", "missing_new_name", f"renames.{index}")
        if old_name in seen_olds:
            raise MigrationError(f"旧名 {old_name} 在同一批改名里出现了多次", "duplicate_old_name", old_name)
        seen_olds.add(old_name)
        cleaned.append({"old_name": old_name, "new_name": new_name})
    cleaned.sort(key=lambda item: item["old_name"])
    return cleaned


def migration_fingerprint(renames: list[dict]) -> str:
    """按改名集合生成确定性指纹：同一组改名重试复用同一条迁移记录。"""
    canonical = sorted(
        (str(item["old_name"]).strip(), str(item["new_name"]).strip()) for item in renames
    )
    digest = hashlib.sha256(repr(canonical).encode("utf-8")).hexdigest()
    return digest[:12]


def migration_id(renames: list[dict]) -> str:
    return f"mig_{migration_fingerprint(renames)}"


def _valid_name(name: str) -> bool:
    return bool(NAME_RE.match(name))


def validate_renames(
    renames: list[dict],
    metrics: list[dict],
    active_aliases: list[dict] | None = None,
) -> dict:
    """静态校验一组改名，返回冲突清单（不做表达式/依赖层面的预演）。

    冲突分类：
      invalid_new_name        新名不符合命名规范
      no_change               新旧名相同
      old_name_not_found      旧名在注册表里不存在
      new_name_taken          新名已被另一个未改名的指标占用（同名碰撞）
      target_renamed_twice    新名同时是本批另一条改名的旧名（A→B、B→C 链）
      duplicate_target        两个旧名改成同一个新名
      old_name_aliased        旧名本身已是历史改名的别名（不能再次改名）
    """
    active_aliases = active_aliases or []
    existing_names = set(registry.names(metrics))
    old_names = {item["old_name"] for item in renames}
    new_names = [item["new_name"] for item in renames]
    alias_old_names = {alias["old_name"] for alias in active_aliases}

    conflicts: list[dict] = []

    # 新名命名规范 / 无变化
    for item in renames:
        if not _valid_name(item["new_name"]):
            conflicts.append(
                {
                    "code": "invalid_new_name",
                    "old_name": item["old_name"],
                    "new_name": item["new_name"],
                    "detail": f"新名 {item['new_name']!r} 只允许小写字母/数字/下划线且以字母开头",
                }
            )
        if item["old_name"] == item["new_name"]:
            conflicts.append(
                {
                    "code": "no_change",
                    "old_name": item["old_name"],
                    "new_name": item["new_name"],
                    "detail": f"{item['old_name']} 新旧名相同，无需迁移",
                }
            )

    # 旧名必须存在
    for item in renames:
        if item["old_name"] not in existing_names:
            conflicts.append(
                {
                    "code": "old_name_not_found",
                    "old_name": item["old_name"],
                    "new_name": item["new_name"],
                    "detail": f"旧名 {item['old_name']} 在注册表里不存在",
                }
            )

    # 新名碰撞：已存在、且该占用者本批不改名
    for item in renames:
        if item["new_name"] in existing_names and item["new_name"] not in old_names:
            conflicts.append(
                {
                    "code": "new_name_taken",
                    "old_name": item["old_name"],
                    "new_name": item["new_name"],
                    "detail": f"新名 {item['new_name']} 已被同名指标占用，不能改名到已存在的指标",
                }
            )

    # 改名链：新名恰好是本批另一条改名的旧名（A→B 且 B→C）
    for item in renames:
        if item["new_name"] in old_names and item["new_name"] != item["old_name"]:
            conflicts.append(
                {
                    "code": "target_renamed_twice",
                    "old_name": item["old_name"],
                    "new_name": item["new_name"],
                    "detail": (
                        f"{item['old_name']}→{item['new_name']} 的新名同时是另一条改名的旧名，"
                        "一批迁移不允许改名链，请拆成两次迁移"
                    ),
                }
            )

    # 多个旧名改成同一个新名
    target_counts: dict[str, list[str]] = {}
    for item in renames:
        target_counts.setdefault(item["new_name"], []).append(item["old_name"])
    for new_name, olds in target_counts.items():
        if len(set(olds)) > 1:
            conflicts.append(
                {
                    "code": "duplicate_target",
                    "old_name": "、".join(sorted(set(olds))),
                    "new_name": new_name,
                    "detail": f"多个旧名要改成同一个新名 {new_name}，迁移后会撞主键",
                }
            )

    # 旧名已经是别名：说明它已经是上一次改名的产物/被迁移过
    for item in renames:
        if item["old_name"] in alias_old_names:
            conflicts.append(
                {
                    "code": "old_name_aliased",
                    "old_name": item["old_name"],
                    "new_name": item["new_name"],
                    "detail": f"旧名 {item['old_name']} 已是历史改名别名，不能再次作为改名源",
                }
            )

    return {
        "ok": not conflicts,
        "conflicts": conflicts,
        "conflict_codes": sorted({item["code"] for item in conflicts}),
    }


def rename_map(renames: list[dict]) -> dict[str, str]:
    return {item["old_name"]: item["new_name"] for item in renames}


def rewrite_expression(expression: str, mapping: dict[str, str]) -> tuple[str, list[dict]]:
    """把表达式里对旧名的 metric: 引用改写成新名，返回 (新表达式, 改写明细)。

    只替换引用名，保留 @版本、运算结构与 dim: 引用；归一化口径不变。
    解析失败时原样返回并标记，交由预演校验拦截。
    """
    analysis = expr.analyze_expression(expression)
    if not analysis["ok"]:
        return expression, []
    rewritten = expression
    changes: list[dict] = []
    for ref in analysis["metrics"]:
        old = ref["name"]
        new = mapping.get(old)
        if not new or new == old:
            continue
        suffix = f"@{ref['version']}" if ref["version"] is not None else ""
        old_token = f"metric:{old}{suffix}"
        new_token = f"metric:{new}{suffix}"
        if old_token in rewritten:
            rewritten = rewritten.replace(old_token, new_token)
        # 兼容表达式里冒号两侧带空格的写法
        for spaced_old, spaced_new in (
            (f"metric: {old}{suffix}", f"metric: {new}{suffix}"),
            (f"metric :{old}{suffix}", f"metric :{new}{suffix}"),
            (f"metric : {old}{suffix}", f"metric : {new}{suffix}"),
        ):
            if spaced_old in rewritten:
                rewritten = rewritten.replace(spaced_old, spaced_new)
        changes.append(
            {
                "old_ref": old_token,
                "new_ref": new_token,
                "old_name": old,
                "new_name": new,
                "version": ref["version"],
            }
        )
    return rewritten.strip(), changes


def apply_renames(metrics: list[dict], renames: list[dict]) -> tuple[list[dict], list[dict]]:
    """对一份注册表应用改名，返回 (新注册表, 全部改写明细)。纯函数，不改入参。"""
    mapping = rename_map(renames)
    new_metrics: list[dict] = []
    all_changes: list[dict] = []
    for metric in metrics:
        updated = deepcopy(metric)
        new_expression, changes = rewrite_expression(metric["expression"], mapping)
        if changes:
            updated["expression"] = new_expression
            updated.pop("normalized", None)  # 强制重算归一化
            for change in changes:
                all_changes.append(
                    {
                        "metric_key": graph_mod.node_key(metric["name"], metric["version"]),
                        **change,
                    }
                )
        if metric["name"] in mapping:
            updated["name"] = mapping[metric["name"]]
            updated.pop("normalized", None)
        new_metrics.append(updated)
    return new_metrics, all_changes


def _edge_set(graph: graph_mod.Graph) -> set[tuple[str, str]]:
    edges: set[tuple[str, str]] = set()
    for source, targets in graph.deps.items():
        for target in targets:
            edges.add((source, target))
    return edges


def _unresolved_keys(graph: graph_mod.Graph) -> set[str]:
    return {item["target"] for item in graph.unresolved}


def build_preview(
    metrics: list[dict],
    renames: list[dict],
    active_aliases: list[dict] | None = None,
) -> dict:
    """预演一次改名迁移：受影响公式、名称冲突、依赖变化、体检指标对比。"""
    renames = normalize_renames(renames)
    validation = validate_renames(renames, metrics, active_aliases)
    fingerprint = migration_fingerprint(renames)
    mig_id = f"mig_{fingerprint}"

    base: dict = {
        "migration_id": mig_id,
        "fingerprint": fingerprint,
        "renames": renames,
        "status": STATUS_DRAFT,
        "ok": validation["ok"],
        "conflicts": validation["conflicts"],
        "conflict_codes": validation["conflict_codes"],
    }
    if not validation["ok"]:
        base["blocked"] = True
        base["reason"] = "名称冲突未消除，不能预演切换"
        return base

    mapping = rename_map(renames)
    before_graph = graph_mod.build_graph(metrics)
    new_metrics, changes = apply_renames(metrics, renames)
    after_graph = graph_mod.build_graph(new_metrics)

    # 受影响的指标版本：自身被改名，或表达式里引用了旧名
    renamed_keys = sorted(
        graph_mod.node_key(name, metric["version"])
        for metric in metrics
        for name in [metric["name"]]
        if name in mapping
    )
    changed_expression_keys = sorted({change["metric_key"] for change in changes})
    affected_keys = sorted(set(renamed_keys) | set(changed_expression_keys))

    affected_formulas = []
    for key in affected_keys:
        name, version = graph_mod.split_key(key)
        old_metric = next(
            (m for m in metrics if m["name"] == name and int(m["version"]) == version), None
        )
        new_name = mapping.get(name, name)
        new_metric = next(
            (m for m in new_metrics if m["name"] == new_name and int(m["version"]) == version),
            None,
        )
        if old_metric is None or new_metric is None:
            continue
        affected_formulas.append(
            {
                "old_key": key,
                "new_key": graph_mod.node_key(new_name, version),
                "old_name": name,
                "new_name": new_name,
                "version": version,
                "renamed_self": name in mapping,
                "expression_rewritten": old_metric["expression"] != new_metric["expression"],
                "old_expression": old_metric["expression"],
                "new_expression": new_metric["expression"],
                "normalized_before": registry.normalized_of(old_metric),
                "normalized_after": registry.normalized_of(new_metric),
                "published": bool(old_metric.get("published")),
                "owner": old_metric.get("owner", ""),
            }
        )

    # 依赖变化：边的增/删（节点键改名后整体平移，结构应当不变）
    before_edges = _edge_set(before_graph)
    after_edges = _edge_set(after_graph)
    # 把 after 的节点键按改名映射还原成旧名空间，便于对比结构是否守恒
    inverse = {new: old for old, new in mapping.items()}

    def _to_old_space(edge: tuple[str, str]) -> tuple[str, str]:
        def shift(key: str) -> str:
            n, v = graph_mod.split_key(key)
            return graph_mod.node_key(inverse.get(n, n), v)

        return (shift(edge[0]), shift(edge[1]))

    after_edges_old_space = {_to_old_space(edge) for edge in after_edges}
    removed_edges = sorted(before_edges - after_edges_old_space)
    added_edges = sorted(after_edges_old_space - before_edges)

    before_unresolved = _unresolved_keys(before_graph)
    after_unresolved = {_to_old_space((item["target"], item["target"]))[0] for item in after_graph.unresolved}
    new_unresolved = sorted(after_unresolved - before_unresolved)

    # 节点级依赖变化（面向页面：每个受影响指标的直接上下游键，改名前后各一份）
    dependency_changes = []
    for formula in affected_formulas:
        old_key = formula["old_key"]
        new_key = formula["new_key"]
        dependency_changes.append(
            {
                "old_key": old_key,
                "new_key": new_key,
                "deps_before": sorted(before_graph.deps.get(old_key, [])),
                "deps_after": sorted(after_graph.deps.get(new_key, [])),
                "dependents_before": sorted(before_graph.dependents.get(old_key, [])),
                "dependents_after": sorted(after_graph.dependents.get(new_key, [])),
            }
        )

    before_summary = graph_mod.summary(before_graph)
    after_summary = graph_mod.summary(after_graph)
    structure_preserved = (
        not added_edges
        and not removed_edges
        and not new_unresolved
        and before_summary["edge_count"] == after_summary["edge_count"]
        and before_summary["node_count"] == after_summary["node_count"]
        and before_summary["cycle_count"] == after_summary["cycle_count"]
    )

    # 预演后仍要保证没有新的不可解析引用（历史公式靠别名解析，这里只看静态结构）
    blocked = not structure_preserved
    reasons = []
    if added_edges or removed_edges:
        reasons.append("预演后依赖边发生增删，改名不是纯名称平移")
    if new_unresolved:
        reasons.append("预演后出现新的不可解析引用：" + "、".join(new_unresolved))
    if before_summary["cycle_count"] != after_summary["cycle_count"]:
        reasons.append("预演后依赖环数量变化")

    return {
        **base,
        "blocked": blocked,
        "reason": "；".join(reasons) if reasons else "",
        "mapping": mapping,
        "affected_summary": {
            "renamed_metric_count": len(set(mapping)),
            "renamed_version_count": len(renamed_keys),
            "affected_formula_count": len(affected_formulas),
            "rewritten_formula_count": len(changed_expression_keys),
            "reference_rewrite_count": len(changes),
        },
        "affected_formulas": affected_formulas,
        "dependency_changes": dependency_changes,
        "edge_diff": {
            "added": [list(edge) for edge in added_edges],
            "removed": [list(edge) for edge in removed_edges],
            "structure_preserved": structure_preserved,
        },
        "new_unresolved": new_unresolved,
        "graph_before": before_summary,
        "graph_after": after_summary,
        "aliases_to_create": [
            {
                "old_name": item["old_name"],
                "new_name": item["new_name"],
            }
            for item in renames
        ],
    }


def resolve_alias(name: str, metrics: list[dict], aliases: list[dict]) -> str:
    """旧名 → 当前名解析：当前注册表里没有、但别名表有记录时，返回当前名。"""
    current_names = set(registry.names(metrics))
    if name in current_names:
        return name
    for alias in aliases:
        if alias["old_name"] == name:
            target = alias["new_name"]
            # 支持多次迁移的链式追溯
            if target in current_names:
                return target
            nested = resolve_alias(target, metrics, aliases)
            if nested != target:
                return nested
            return target
    return name


def alias_chain(name: str, aliases: list[dict]) -> list[str]:
    """返回一个名字经过历次改名的链路（含起点）。"""
    chain = [name]
    current = name
    guard = 0
    while guard < 100:
        guard += 1
        next_step = next(
            (alias["new_name"] for alias in aliases if alias["old_name"] == current), None
        )
        if not next_step or next_step in chain:
            break
        chain.append(next_step)
        current = next_step
    return chain
