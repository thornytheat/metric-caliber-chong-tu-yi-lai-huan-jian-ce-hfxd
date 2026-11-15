"""改名迁移 HTTP 端到端：预演、一次性切换、失败回滚、幂等重试、历史别名兼容。"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """每个用例一个全新的临时库，避免与其它模块共享 seed 后的注册表状态。"""
    db_dir = Path(tempfile.mkdtemp(prefix="metricring-mig-api-"))
    monkeypatch.setenv("METRICRING_DATA_DIR", str(db_dir))
    monkeypatch.setenv("METRICRING_DB", str(db_dir / "migration.sqlite3"))
    # settings 在导入时已固化路径，这里直接改模块常量让 store 落到临时库
    from app import settings, store
    monkeypatch.setattr(settings, "DATA_DIR", db_dir)
    monkeypatch.setattr(settings, "DB_PATH", db_dir / "migration.sqlite3")
    monkeypatch.setattr(store, "settings", settings)
    # 延迟导入 app.main，确保其引用的 store/settings 已指向临时库
    from app.main import app
    with TestClient(app) as test_client:
        yield test_client


def test_preview_reports_conflict_for_taken_name(client):
    response = client.post(
        "/api/migrations/preview",
        json={"renames": [{"old_name": "gmv_daily", "new_name": "gmv"}], "persist": False},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert "new_name_taken" in body["conflict_codes"]
    assert body["blocked"] is True


def test_preview_rejects_empty_and_unknown_old_name(client):
    response = client.post("/api/migrations/preview", json={"renames": []})
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "empty_renames"

    response = client.post(
        "/api/migrations/preview",
        json={"renames": [{"old_name": "not_exist", "new_name": "whatever_x"}]},
    )
    body = response.json()
    assert body["ok"] is False
    assert "old_name_not_found" in body["conflict_codes"]


def test_preview_lists_affected_formulas_and_preserves_edges(client):
    response = client.post(
        "/api/migrations/preview",
        json={
            "renames": [{"old_name": "gmv_daily", "new_name": "net_gmv_daily"}],
            "persist": True,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["blocked"] is False
    assert body["migration_id"].startswith("mig_")
    assert body["affected_summary"]["renamed_version_count"] == 1
    assert body["affected_summary"]["affected_formula_count"] == 6
    assert body["edge_diff"]["structure_preserved"] is True
    assert body["edge_diff"]["added"] == []
    assert body["edge_diff"]["removed"] == []
    # 预演不写注册表：旧名仍在
    registry = client.get("/api/registry").json()
    assert any(m["name"] == "gmv_daily" for m in registry["metrics"])


def test_commit_blocked_when_preview_fails(client):
    response = client.post(
        "/api/migrations/commit",
        json={"renames": [{"old_name": "gmv_daily", "new_name": "gmv"}]},
    )
    assert response.status_code == 409
    assert response.json()["code"] == "preview_blocked"
    # 注册表未被改动
    registry = client.get("/api/registry").json()
    assert any(m["name"] == "gmv_daily" for m in registry["metrics"])


def test_full_migration_switch_alias_and_idempotent_retry(client):
    renames = [{"old_name": "uv", "new_name": "unique_visitor"}]
    # 预演
    preview = client.post("/api/migrations/preview", json={"renames": renames}).json()
    mig_id = preview["migration_id"]
    assert preview["ok"] is True

    # 一次性切换
    committed = client.post("/api/migrations/commit", json={"renames": renames})
    assert committed.status_code == 200
    body = committed.json()
    assert body["ok"] is True
    assert body["migration_id"] == mig_id
    assert body["status"] == "committed"

    # 注册表：旧名消失、新名出现，版本号不变（uv 有 @1/@2 两个版本）
    registry = client.get("/api/registry").json()
    names = {m["name"] for m in registry["metrics"]}
    assert "uv" not in names
    uv_versions = sorted(m["version"] for m in registry["metrics"] if m["name"] == "unique_visitor")
    assert uv_versions == [1, 2]
    # 引用 uv 的下游（uv_per_gmv 写的是 metric:uv@2）已改写为新名
    uv_per_gmv = next(m for m in registry["metrics"] if m["name"] == "uv_per_gmv")
    assert "metric:unique_visitor@2" in uv_per_gmv["expression"]
    assert "metric:uv@2" not in uv_per_gmv["expression"]

    # 别名沉淀，旧名可查版本与迁移来源
    aliases = client.get("/api/aliases").json()["aliases"]
    uv_aliases = [a for a in aliases if a["old_name"] == "uv"]
    assert {a["version"] for a in uv_aliases} == {1, 2}
    assert all(a["migration_id"] == mig_id for a in uv_aliases)

    resolved = client.get("/api/names/resolve?name=uv").json()
    assert resolved["is_historical_name"] is True
    assert resolved["current_name"] == "unique_visitor"
    assert resolved["migration_source"] == mig_id
    assert len(resolved["versions"]) == 2

    # 体检仍能跑通，且没有因改名产生新的不可解析引用
    check = client.post("/api/check").json()
    assert check["stats"]["unresolved_count"] == 0

    # 幂等重试：同组改名不重复产生迁移记录或版本
    retry = client.post("/api/migrations/commit", json={"renames": renames})
    assert retry.status_code == 200
    retry_body = retry.json()
    assert retry_body["idempotent"] is True
    assert retry_body["migration_id"] == mig_id
    records = client.get("/api/migrations").json()["migrations"]
    same = [m for m in records if m["migration_id"] == mig_id]
    assert len(same) == 1
    registry_after = client.get("/api/registry").json()
    assert registry_after["stats"]["metric_entries"] == registry["stats"]["metric_entries"]


def test_historical_formula_keeps_working_after_rename(client):
    # 先把 order_amount 改名
    renames = [{"old_name": "refund_amount", "new_name": "refund_amt_metric"}]
    preview = client.post("/api/migrations/preview", json={"renames": renames}).json()
    assert preview["ok"] is True
    client.post("/api/migrations/commit", json={"renames": renames})

    # 登记一条仍使用旧名 metric:refund_amount 的历史公式（allow_missing_reference 兜底注册）
    legacy = client.post(
        "/api/registry",
        json={
            "name": "legacy_refund_report",
            "expression": "sum(metric:refund_amount)",
            "aggregation": "sum",
            "dimensions": ["dt"],
            "allow_missing_reference": True,
        },
    )
    assert legacy.status_code == 201
    # 体检里该旧名引用经别名解析，不算不可解析引用
    check = client.post("/api/check").json()
    unresolved_targets = {item["target"] for item in check["unresolved"]}
    assert "refund_amount" not in unresolved_targets


def test_migration_detail_and_listing(client):
    renames = [{"old_name": "user_count", "new_name": "distinct_user_count"}]
    client.post("/api/migrations/preview", json={"renames": renames})
    committed = client.post("/api/migrations/commit", json={"renames": renames})
    assert committed.status_code == 200
    mig_id = committed.json()["migration_id"]

    listing = client.get("/api/migrations").json()
    assert listing["count"] >= 1
    assert any(m["migration_id"] == mig_id for m in listing["migrations"])
    detail = client.get(f"/api/migrations/{mig_id}")
    assert detail.status_code == 200
    assert detail.json()["migration_id"] == mig_id
    assert detail.json()["status"] == "committed"
    # 详情里保留了旧版注册表快照，支持回滚与审计
    assert len(detail.json()["snapshot"]) == 24

    missing = client.get("/api/migrations/mig_doesnotexist")
    assert missing.status_code == 404


def test_migrations_page_renders(client):
    response = client.get("/migrations")
    assert response.status_code == 200
    assert "指标改名迁移" in response.text
    assert "一次性切换" in response.text


def test_batch_rename_in_one_migration(client):
    # 用不与其它用例碰撞的目标名；cpc/cpa 若已被前面的用例改名则按当前名取
    registry = client.get("/api/registry").json()
    current_names = {m["name"] for m in registry["metrics"]}
    pair = None
    for old_a, old_b, new_a, new_b in (
        ("cpc", "cpa", "click_cost", "acquire_cost"),
        ("click_cost", "acquire_cost", "click_cost_v2", "acquire_cost_v2"),
    ):
        if old_a in current_names and old_b in current_names:
            pair = (old_a, old_b, new_a, new_b)
            break
    assert pair is not None
    old_a, old_b, new_a, new_b = pair
    renames = [
        {"old_name": old_a, "new_name": new_a},
        {"old_name": old_b, "new_name": new_b},
    ]
    preview = client.post("/api/migrations/preview", json={"renames": renames}).json()
    assert preview["ok"] is True
    committed = client.post("/api/migrations/commit", json={"renames": renames})
    assert committed.status_code == 200
    registry = client.get("/api/registry").json()
    names = {m["name"] for m in registry["metrics"]}
    assert {new_a, new_b} <= names
    assert not ({old_a, old_b} & names)
    # 环结构守恒
    check = client.post("/api/check").json()
    assert check["stats"]["cycle_count"] == 2
