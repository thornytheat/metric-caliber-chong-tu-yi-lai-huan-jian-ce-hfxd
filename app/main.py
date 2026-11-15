"""FastAPI 路由（薄）：页面渲染 + JSON API，规则全部在 app/expr|graph|conflict|gate。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from . import conflict, expr, gate, graph as graph_mod, migration, registry, report, seed, settings, store

APP_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))

@asynccontextmanager
async def lifespan(application: FastAPI):
    with store.session() as conn:
        application.state.seed_result = seed.seed(conn)
    yield


app = FastAPI(title=settings.APP_NAME, version="1.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")


def load_metrics() -> list[dict]:
    with store.session() as conn:
        store.init_schema(conn)
        metrics = store.all_metrics(conn)
    return [registry.with_normalized(metric) for metric in metrics]


def load_aliases() -> list[dict]:
    with store.session() as conn:
        store.init_schema(conn)
        return store.list_aliases(conn)


def base_context(request: Request, **extra: Any) -> dict:
    metrics = load_metrics()
    aliases = load_aliases()
    overview = report.overview(metrics, aliases)
    context = {
        "app_name": settings.APP_NAME,
        "app_short": settings.APP_SHORT,
        "overview": overview,
        "metrics": metrics,
        "aliases": aliases,
        "error": None,
        "notice": None,
    }
    context.update(extra)
    return context


# --------------------------------------------------------------------------- 请求模型


class ParseRequest(BaseModel):
    expression: str = Field(default="")
    strict: bool = False


class ImpactRequest(BaseModel):
    name: str
    version: int | None = None
    expression: str
    aggregation: str | None = None
    dimensions: list[str] | None = None


class PublishRequest(BaseModel):
    name: str
    version: int


class RegistryRequest(BaseModel):
    name: str
    expression: str
    version: int | None = None
    aggregation: str = "sum"
    dimensions: list[str] = Field(default_factory=list)
    unit: str = ""
    owner: str = ""
    description: str = ""
    published: bool = False
    allow_missing_reference: bool = False


class RenameItem(BaseModel):
    old_name: str
    new_name: str


class MigrationPreviewRequest(BaseModel):
    renames: list[RenameItem] = Field(default_factory=list)
    persist: bool = True


class MigrationCommitRequest(BaseModel):
    renames: list[RenameItem] = Field(default_factory=list)


# --------------------------------------------------------------------------- 页面


@app.get("/health")
def health() -> dict:
    with store.session() as conn:
        store.init_schema(conn)
        rows = store.count_metrics(conn)
    return {"status": "ok", "app": settings.APP_SHORT, "metrics": rows}


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    metrics = load_metrics()
    aliases = load_aliases()
    overview = report.overview(metrics, aliases)
    with store.session() as conn:
        store.init_schema(conn)
        recent_checks = store.list_checks(conn, limit=6)
        migrations = store.list_migrations(conn, limit=5)
    top = sorted(
        overview["conflicts"], key=lambda item: item["variant_count"], reverse=True
    )[:3]
    cycles = overview["cycles"]
    context = {
        "app_name": settings.APP_NAME,
        "app_short": settings.APP_SHORT,
        "overview": overview,
        "metrics": metrics,
        "aliases": aliases,
        "top_conflicts": top,
        "cycles": cycles,
        "seed_result": getattr(app.state, "seed_result", {"seeded": False, "reason": "启动中"}),
        "recent_checks": recent_checks,
        "migrations": migrations,
        "error": None,
        "notice": None,
        "page": "index",
        "active": "index",
    }
    return templates.TemplateResponse(request, "index.html", context)


@app.get("/workbench", response_class=HTMLResponse)
def workbench(request: Request) -> HTMLResponse:
    metrics = load_metrics()
    aliases = load_aliases()
    names = registry.names(metrics)
    versions = {
        name: [
            {
                "key": graph_mod.node_key(metric["name"], metric["version"]),
                "version": metric["version"],
                "expression": metric["expression"],
                "normalized": registry.normalized_of(metric),
                "aggregation": metric["aggregation"],
                "dimensions": metric["dimensions"],
                "unit": metric["unit"],
                "published": metric["published"],
                "owner": metric["owner"],
            }
            for metric in registry.versions_of(metrics, name)
        ]
        for name in names
    }
    focus = registry.versions_of(metrics, "gmv_daily")
    focus_metric = focus[0] if focus else None
    graph = graph_mod.build_graph(metrics, aliases)
    context = {
        "app_name": settings.APP_NAME,
        "app_short": settings.APP_SHORT,
        "overview": report.overview(metrics, aliases),
        "metrics": metrics,
        "aliases": aliases,
        "names": names,
        "versions": versions,
        "focus": focus_metric,
        "focus_key": graph_mod.node_key(focus_metric["name"], focus_metric["version"]) if focus_metric else "",
        "focus_rows": graph_mod.dependency_rows(
            graph,
            graph_mod.node_key(focus_metric["name"], focus_metric["version"]),
        )
        if focus_metric
        else [],
        "focus_downstream": graph_mod.downstream(
            graph,
            graph_mod.node_key(focus_metric["name"], focus_metric["version"]),
        )
        if focus_metric
        else [],
        "error": None,
        "notice": None,
        "page": "workbench",
        "active": "workbench",
    }
    return templates.TemplateResponse(request, "workbench.html", context)


@app.get("/result/{check_id}", response_class=HTMLResponse)
def result_page(request: Request, check_id: str) -> HTMLResponse:
    with store.session() as conn:
        payload = store.load_check(conn, check_id)
    metrics = load_metrics()
    aliases = load_aliases()
    context = {
        "app_name": settings.APP_NAME,
        "app_short": settings.APP_SHORT,
        "overview": report.overview(metrics, aliases),
        "metrics": metrics,
        "aliases": aliases,
        "check": payload,
        "check_id": check_id,
        "missing": payload is None,
        "error": None if payload else f"体检记录 {check_id} 不存在，请回到首页重新体检",
        "notice": None,
        "page": "result",
        "active": "result",
    }
    return templates.TemplateResponse(request, "result.html", context)


@app.get("/review", response_class=HTMLResponse)
def review(request: Request) -> HTMLResponse:
    metrics = load_metrics()
    aliases = load_aliases()
    with store.session() as conn:
        latest = store.latest_check(conn)
        impact = store.latest_impact(conn)
        events = store.list_events(conn, limit=25)
        checks = store.list_checks(conn, limit=8)
    context = {
        "app_name": settings.APP_NAME,
        "app_short": settings.APP_SHORT,
        "overview": report.overview(metrics, aliases),
        "metrics": metrics,
        "aliases": aliases,
        "latest": latest,
        "impact": impact,
        "events": events,
        "checks": checks,
        "error": None,
        "notice": None,
        "page": "review",
        "active": "review",
    }
    return templates.TemplateResponse(request, "review.html", context)


# --------------------------------------------------------------------------- 注册表


@app.get("/api/registry")
def api_registry() -> dict:
    metrics = load_metrics()
    aliases = load_aliases()
    overview = report.overview(metrics, aliases)
    return {
        "app": settings.APP_SHORT,
        "stats": {
            "metric_entries": overview["metric_entries"],
            "metric_names": overview["metric_names"],
            "published_entries": overview["published_entries"],
            "unpublished_entries": overview["unpublished_entries"],
            "conflict_count": overview["conflict_count"],
            "cycle_count": overview["cycle_count"],
            "publish_blocked_count": overview["publish_blocked_count"],
            "unresolved_count": overview["unresolved_count"],
            "cross_version_count": overview["cross_version_count"],
            "alias_count": len(aliases),
        },
        "metrics": [
            {
                "key": graph_mod.node_key(metric["name"], metric["version"]),
                "name": metric["name"],
                "version": metric["version"],
                "expression": metric["expression"],
                "normalized": registry.normalized_of(metric),
                "unit": metric["unit"],
                "aggregation": metric["aggregation"],
                "dimensions": metric["dimensions"],
                "owner": metric["owner"],
                "published": metric["published"],
            }
            for metric in metrics
        ],
    }


@app.post("/api/registry", status_code=201)
def api_register(payload: RegistryRequest) -> JSONResponse:
    metrics = load_metrics()
    version = payload.version if payload.version else registry.next_version(metrics, payload.name)
    if store_metric_exists(metrics, payload.name, version):
        raise HTTPException(
            status_code=409,
            detail=f"{payload.name}@{version} 已存在，请递增 version 后再登记",
        )
    try:
        metric = registry.validate_metric(
            {
                "name": payload.name,
                "version": version,
                "expression": payload.expression,
                "aggregation": payload.aggregation,
                "dimensions": payload.dimensions,
                "unit": payload.unit,
                "owner": payload.owner,
                "description": payload.description,
                "published": payload.published,
            }
        )
    except registry.RegistryError as exc:
        raise HTTPException(status_code=400, detail={"error": exc.message, "field": exc.field})

    analysis = expr.analyze_expression(payload.expression)
    unknown_dims = _unknown_dimensions(analysis, metrics)
    missing_refs = [
        finding["target"]
        for finding in gate.reference_findings(metric, metrics)
        if finding["reason"] in ("version_not_found", "metric_not_found", "no_published_version")
    ]
    unpublished_refs = [
        finding["target"]
        for finding in gate.reference_findings(metric, metrics)
        if finding["published"] is False
    ]
    if missing_refs and not payload.allow_missing_reference:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "表达式引用了不存在或没有已发布版本的指标",
                "missing_references": missing_refs,
                "field": "expression",
            },
        )
    if unpublished_refs and payload.published:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "引用了未发布版本，不能直接登记为已发布",
                "unpublished_references": unpublished_refs,
                "field": "expression",
            },
        )

    now = report.now_iso()
    with store.session() as conn:
        store.init_schema(conn)
        store.insert_metric(conn, metric, now)
        store.add_event(
            conn,
            "register",
            f"登记指标 {metric['name']}@{metric['version']}（{metric['normalized']}）",
            {
                "key": f"{metric['name']}@{metric['version']}",
                "normalized": metric["normalized"],
                "published": metric["published"],
            },
            now,
        )
    conflicts = conflict.caliber_groups(metrics + [metric], metric["name"])
    return JSONResponse(
        status_code=201,
        content={
            "ok": True,
            "key": f"{metric['name']}@{metric['version']}",
            "name": metric["name"],
            "version": metric["version"],
            "expression": metric["expression"],
            "normalized": metric["normalized"],
            "aggregation": metric["aggregation"],
            "dimensions": metric["dimensions"],
            "published": metric["published"],
            "unknown_dims": unknown_dims,
            "missing_references": missing_refs,
            "unpublished_references": unpublished_refs,
            "caliber_group_count": len(conflicts),
            "caliber_conflict": len(conflicts) > 1,
        },
    )


def store_metric_exists(metrics: list[dict], name: str, version: int) -> bool:
    return any(
        metric["name"] == name and int(metric["version"]) == int(version) for metric in metrics
    )


def _unknown_dimensions(analysis: dict, metrics: list[dict]) -> list[str]:
    known = set()
    for metric in metrics:
        known.update(metric.get("dimensions") or [])
    return [ref["name"] for ref in analysis["dims"] if ref["name"] not in known]


# --------------------------------------------------------------------------- 解析 / 体检 / 影响面 / 发布


@app.post("/api/parse")
def api_parse(payload: ParseRequest = Body(default=ParseRequest())) -> JSONResponse:
    analysis = expr.analyze_expression(payload.expression)
    if not analysis["ok"]:
        return JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "error": analysis["error"],
                "position": analysis["position"],
                "normalized": None,
            },
        )
    metrics = load_metrics()
    index = registry.index_by_key(metrics)
    published = registry.latest_published(metrics)
    unresolved = []
    for ref in analysis["metrics"]:
        resolved = gate._resolve(ref, metrics, index, published)
        if resolved["reason"] or resolved["published"] is False:
            unresolved.append(
                {
                    "target": resolved["target"],
                    "reason": resolved["reason"] or "unpublished",
                    "published": resolved["published"],
                }
            )
    if unresolved and payload.strict:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "表达式引用了不可解析的指标", "unresolved": unresolved},
        )
    return JSONResponse(
        content={
            "ok": True,
            "raw": analysis["raw"],
            "normalized": analysis["normalized"],
            "changed": analysis["raw"] != analysis["normalized"],
            "references": analysis["references"],
            "metrics": [ref["name"] for ref in analysis["metrics"]],
            "dims": [ref["name"] for ref in analysis["dims"]],
            "functions": analysis["functions"],
            "unresolved": unresolved,
        }
    )


def _run_and_store_check() -> dict:
    metrics = load_metrics()
    aliases = load_aliases()
    payload = report.build_check(metrics, aliases=aliases)
    now = report.now_iso()
    with store.session() as conn:
        store.init_schema(conn)
        store.save_check(conn, payload)
        store.add_event(
            conn,
            "check",
            f"执行注册表体检：冲突 {payload['stats']['conflict_count']} 组 / 环 {payload['stats']['cycle_count']} 条 / 发布阻断 {payload['stats']['publish_blocked_count']} 个",
            {"check_id": payload["check_id"], "verdict": payload["verdict"]},
            now,
        )
    return payload


@app.post("/api/check")
def api_check() -> dict:
    return _run_and_store_check()


@app.post("/check")
def check_form() -> RedirectResponse:
    payload = _run_and_store_check()
    return RedirectResponse(url=f"/result/{payload['check_id']}", status_code=303)


@app.get("/api/check/{check_id}")
def api_check_detail(check_id: str) -> dict:
    with store.session() as conn:
        payload = store.load_check(conn, check_id)
    if payload is None:
        raise HTTPException(status_code=404, detail=f"体检记录 {check_id} 不存在")
    return payload


@app.post("/api/impact")
def api_impact(payload: ImpactRequest) -> dict:
    metrics = load_metrics()
    aliases = load_aliases()
    if not any(metric["name"] == payload.name for metric in metrics):
        raise HTTPException(status_code=404, detail=f"指标 {payload.name} 未注册")
    version = payload.version
    if not version:
        latest = registry.latest_version(metrics, payload.name)
        version = latest["version"] if latest else 1
    try:
        impact = report.build_impact(
            metrics,
            name=payload.name,
            version=int(version),
            expression=payload.expression,
            aggregation=payload.aggregation,
            dimensions=payload.dimensions,
            aliases=aliases,
        )
    except gate.ExpressionInvalid as exc:
        raise HTTPException(
            status_code=400, detail={"error": exc.message, "position": exc.position}
        )
    except gate.TargetNotFound as exc:
        raise HTTPException(status_code=404, detail={"error": exc.message})
    now = report.now_iso()
    with store.session() as conn:
        store.init_schema(conn)
        store.save_impact(conn, impact)
        store.add_event(
            conn,
            "impact",
            f"评估 {impact['target']['key']} 变更：下游 {impact['downstream_count']} 个，breaking {impact['breaking_count']} 个",
            {"impact_id": impact["impact_id"], "key": impact["target"]["key"]},
            now,
        )
    return impact


@app.post("/api/publish")
def api_publish(payload: PublishRequest) -> JSONResponse:
    metrics = load_metrics()
    aliases = load_aliases()
    try:
        result = gate.publish_check(metrics, payload.name, int(payload.version), aliases)
    except gate.GateError as exc:
        raise HTTPException(status_code=404, detail={"error": exc.message})
    if not result["ok"]:
        return JSONResponse(status_code=409, content=result)
    now = report.now_iso()
    with store.session() as conn:
        store.init_schema(conn)
        store.set_published(conn, payload.name, int(payload.version), True, now)
        store.add_event(
            conn,
            "publish",
            f"发布指标 {result['key']}（{result['normalized']}）",
            {"key": result["key"], "normalized": result["normalized"]},
            now,
        )
    result["published"] = True
    return JSONResponse(status_code=200, content=result)


@app.get("/api/metrics")
def api_metrics() -> dict:
    metrics = load_metrics()
    return {
        "count": len(metrics),
        "names": registry.names(metrics),
        "metrics": [
            {
                "key": graph_mod.node_key(metric["name"], metric["version"]),
                "name": metric["name"],
                "version": metric["version"],
                "expression": metric["expression"],
                "normalized": registry.normalized_of(metric),
                "aggregation": metric["aggregation"],
                "dimensions": metric["dimensions"],
                "published": metric["published"],
            }
            for metric in metrics
        ],
    }


# --------------------------------------------------------------------------- 改名迁移


def _renames_from_request(items: list[RenameItem]) -> list[dict]:
    return [{"old_name": item.old_name.strip(), "new_name": item.new_name.strip()} for item in items]


@app.post("/api/migrations/preview")
def api_migration_preview(payload: MigrationPreviewRequest) -> JSONResponse:
    """预演：受影响公式、新旧名称冲突、预演后的依赖变化（不落注册表）。"""
    metrics = load_metrics()
    aliases = load_aliases()
    try:
        renames = migration.normalize_renames(_renames_from_request(payload.renames))
        preview = migration.build_preview(metrics, renames, aliases)
    except migration.MigrationError as exc:
        raise HTTPException(status_code=400, detail=exc.as_dict())
    status_code = 200
    if payload.persist:
        now = report.now_iso()
        with store.session() as conn:
            store.init_schema(conn)
            record = store.upsert_migration_preview(conn, preview, now)
        preview["stored_status"] = record["status"]
    return JSONResponse(status_code=status_code, content=preview)


@app.post("/api/migrations/commit")
def api_migration_commit(payload: MigrationCommitRequest) -> JSONResponse:
    """预演通过后一次性切换；切换失败回滚旧版注册表并返回 409。"""
    try:
        renames = migration.normalize_renames(_renames_from_request(payload.renames))
    except migration.MigrationError as exc:
        raise HTTPException(status_code=400, detail=exc.as_dict())
    mig_id = migration.migration_id(renames)

    # 幂等优先：同指纹迁移已切换过则直接返回，不依赖当前注册表状态
    # （重试时旧名可能已不存在，不能再拿当前注册表重跑预演）
    with store.session() as conn:
        store.init_schema(conn)
        existing = store.get_migration(conn, mig_id)
    if existing and existing["status"] == "committed":
        return JSONResponse(
            status_code=200,
            content={
                "ok": True,
                "idempotent": True,
                "migration_id": mig_id,
                "status": "committed",
                "renames": renames,
                "note": "该组改名已切换过，本次为幂等重试，未重复产生迁移记录或版本",
            },
        )

    metrics = load_metrics()
    aliases = load_aliases()
    preview = migration.build_preview(metrics, renames, aliases)

    if not preview["ok"] or preview["blocked"]:
        return JSONResponse(
            status_code=409,
            content={
                "ok": False,
                "code": "preview_blocked",
                "migration_id": preview["migration_id"],
                "conflicts": preview["conflicts"],
                "reason": preview.get("reason", "预演未通过，拒绝切换"),
                "edge_diff": preview.get("edge_diff"),
                "new_unresolved": preview.get("new_unresolved"),
            },
        )

    new_metrics, _changes = migration.apply_renames(metrics, renames)
    now = report.now_iso()
    try:
        with store.session() as conn:
            store.init_schema(conn)
            record = store.apply_rename_migration(conn, renames, preview, new_metrics, now)
    except store.MigrationApplyError as exc:
        with store.session() as conn:
            failed = store.get_migration(conn, mig_id)
        return JSONResponse(
            status_code=409,
            content={
                "ok": False,
                "code": "rolled_back",
                "migration_id": mig_id,
                "status": failed["status"] if failed else "rolled_back",
                "reason": str(exc),
                "note": "切换失败，注册表已恢复到迁移前的旧版；可修正后重试，迁移号与版本号不变",
            },
        )
    return JSONResponse(
        status_code=200,
        content={
            "ok": True,
            "idempotent": False,
            "migration_id": record["migration_id"],
            "status": record["status"],
            "renames": renames,
            "affected_summary": preview["affected_summary"],
            "aliases": preview["aliases_to_create"],
        },
    )


@app.get("/api/migrations")
def api_migration_list(limit: int = 20) -> dict:
    with store.session() as conn:
        store.init_schema(conn)
        items = store.list_migrations(conn, limit=limit)
    return {"count": len(items), "migrations": items}


@app.get("/api/migrations/{migration_id}")
def api_migration_detail(migration_id: str) -> dict:
    with store.session() as conn:
        store.init_schema(conn)
        record = store.get_migration(conn, migration_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"迁移记录 {migration_id} 不存在")
    return record


@app.get("/api/aliases")
def api_aliases() -> dict:
    """旧名 → 当前名/版本/迁移来源，支撑历史公式兼容与追溯。"""
    aliases = load_aliases()
    metrics = load_metrics()
    current_names = set(registry.names(metrics))
    enriched = []
    for alias in aliases:
        chain = migration.alias_chain(alias["old_name"], aliases)
        current = chain[-1]
        enriched.append(
            {
                **alias,
                "current_name": current if current in current_names else alias["new_name"],
                "chain": chain,
                "current_exists": current in current_names,
            }
        )
    return {"count": len(enriched), "aliases": enriched}


@app.get("/api/names/resolve")
def api_resolve_name(name: str) -> dict:
    """按（可能是历史旧名的）名字查当前指标版本与迁移来源。"""
    metrics = load_metrics()
    aliases = load_aliases()
    current = migration.resolve_alias(name, metrics, aliases)
    versions = registry.versions_of(metrics, current)
    source = next((alias for alias in aliases if alias["old_name"] == name), None)
    if not versions and source is None:
        raise HTTPException(status_code=404, detail=f"名字 {name} 既不是当前指标也不是历史旧名")
    return {
        "query": name,
        "current_name": current,
        "is_historical_name": current != name,
        "migration_source": source["migration_id"] if source else None,
        "chain": migration.alias_chain(name, aliases),
        "versions": [
            {
                "key": graph_mod.node_key(metric["name"], metric["version"]),
                "name": metric["name"],
                "version": metric["version"],
                "expression": metric["expression"],
                "normalized": registry.normalized_of(metric),
                "published": bool(metric["published"]),
            }
            for metric in versions
        ],
    }


@app.get("/migrations", response_class=HTMLResponse)
def migrations_page(request: Request) -> HTMLResponse:
    metrics = load_metrics()
    aliases = load_aliases()
    with store.session() as conn:
        store.init_schema(conn)
        records = store.list_migrations(conn, limit=20)
    context = {
        "app_name": settings.APP_NAME,
        "app_short": settings.APP_SHORT,
        "overview": report.overview(metrics, aliases),
        "metrics": metrics,
        "aliases": aliases,
        "migration_records": records,
        "metric_names": registry.names(metrics),
        "error": None,
        "notice": None,
        "page": "migrations",
        "active": "migrations",
    }
    return templates.TemplateResponse(request, "migrations.html", context)
