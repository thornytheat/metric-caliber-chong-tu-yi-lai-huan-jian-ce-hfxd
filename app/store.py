"""SQLite 访问层：指标注册表、体检记录、影响面记录、事件流水。

命名卷 metricring-data 挂到 /app/data，重建数据只需删卷。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from . import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS metrics (
    name        TEXT    NOT NULL,
    version     INTEGER NOT NULL,
    expression  TEXT    NOT NULL,
    unit        TEXT    NOT NULL DEFAULT '',
    aggregation TEXT    NOT NULL DEFAULT 'sum',
    dimensions  TEXT    NOT NULL DEFAULT '[]',
    owner       TEXT    NOT NULL DEFAULT '',
    published   INTEGER NOT NULL DEFAULT 0,
    description TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL,
    PRIMARY KEY (name, version)
);
CREATE TABLE IF NOT EXISTS checks (
    check_id   TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    payload    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS impacts (
    impact_id  TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    name       TEXT NOT NULL,
    version    INTEGER NOT NULL,
    payload    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    at      TEXT NOT NULL,
    kind    TEXT NOT NULL,
    summary TEXT NOT NULL,
    detail  TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rename_migrations (
    migration_id TEXT PRIMARY KEY,
    fingerprint  TEXT NOT NULL,
    status       TEXT NOT NULL,
    renames      TEXT NOT NULL,
    preview      TEXT NOT NULL DEFAULT '{}',
    snapshot     TEXT NOT NULL DEFAULT '[]',
    reason       TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    committed_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS name_aliases (
    old_name     TEXT NOT NULL,
    new_name     TEXT NOT NULL,
    version      INTEGER NOT NULL,
    migration_id TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (old_name, version)
);
CREATE INDEX IF NOT EXISTS idx_name_aliases_new ON name_aliases(new_name);
CREATE INDEX IF NOT EXISTS idx_rename_migrations_status ON rename_migrations(status);
"""


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = Path(db_path or settings.DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def session(db_path: Path | None = None):
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def _row_to_metric(row: sqlite3.Row) -> dict:
    return {
        "name": row["name"],
        "version": int(row["version"]),
        "expression": row["expression"],
        "unit": row["unit"],
        "aggregation": row["aggregation"],
        "dimensions": json.loads(row["dimensions"] or "[]"),
        "owner": row["owner"],
        "published": bool(row["published"]),
        "description": row["description"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def count_metrics(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS c FROM metrics").fetchone()
    return int(row["c"])


def all_metrics(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM metrics ORDER BY name ASC, version ASC"
    ).fetchall()
    return [_row_to_metric(row) for row in rows]


def get_metric(conn: sqlite3.Connection, name: str, version: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM metrics WHERE name = ? AND version = ?", (name, int(version))
    ).fetchone()
    return _row_to_metric(row) if row else None


def upsert_metric(conn: sqlite3.Connection, metric: dict, now: str) -> None:
    conn.execute(
        """
        INSERT INTO metrics (name, version, expression, unit, aggregation, dimensions,
                             owner, published, description, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(name, version) DO UPDATE SET
            expression = excluded.expression,
            unit = excluded.unit,
            aggregation = excluded.aggregation,
            dimensions = excluded.dimensions,
            owner = excluded.owner,
            published = excluded.published,
            description = excluded.description,
            updated_at = excluded.updated_at
        """,
        (
            metric["name"],
            int(metric["version"]),
            metric["expression"],
            metric.get("unit", ""),
            metric.get("aggregation", "sum"),
            json.dumps(metric.get("dimensions", []), ensure_ascii=False),
            metric.get("owner", ""),
            1 if metric.get("published") else 0,
            metric.get("description", ""),
            now,
            now,
        ),
    )


def set_published(conn: sqlite3.Connection, name: str, version: int, published: bool, now: str) -> None:
    conn.execute(
        "UPDATE metrics SET published = ?, updated_at = ? WHERE name = ? AND version = ?",
        (1 if published else 0, now, name, int(version)),
    )


def insert_metric(conn: sqlite3.Connection, metric: dict, now: str) -> None:
    conn.execute(
        """
        INSERT INTO metrics (name, version, expression, unit, aggregation, dimensions,
                             owner, published, description, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            metric["name"],
            int(metric["version"]),
            metric["expression"],
            metric.get("unit", ""),
            metric.get("aggregation", "sum"),
            json.dumps(metric.get("dimensions", []), ensure_ascii=False),
            metric.get("owner", ""),
            1 if metric.get("published") else 0,
            metric.get("description", ""),
            now,
            now,
        ),
    )


def metric_exists(conn: sqlite3.Connection, name: str, version: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM metrics WHERE name = ? AND version = ?", (name, int(version))
    ).fetchone()
    return row is not None


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def save_check(conn: sqlite3.Connection, payload: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO checks (check_id, created_at, payload) VALUES (?, ?, ?)",
        (payload["check_id"], payload["created_at"], json.dumps(payload, ensure_ascii=False)),
    )


def load_check(conn: sqlite3.Connection, check_id: str) -> dict | None:
    row = conn.execute("SELECT payload FROM checks WHERE check_id = ?", (check_id,)).fetchone()
    return json.loads(row["payload"]) if row else None


def latest_check(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute(
        "SELECT payload FROM checks ORDER BY created_at DESC, rowid DESC LIMIT 1"
    ).fetchone()
    return json.loads(row["payload"]) if row else None


def list_checks(conn: sqlite3.Connection, limit: int = 10) -> list[dict]:
    rows = conn.execute(
        "SELECT check_id, created_at, payload FROM checks ORDER BY created_at DESC, rowid DESC LIMIT ?",
        (limit,),
    ).fetchall()
    items = []
    for row in rows:
        payload = json.loads(row["payload"])
        items.append(
            {
                "check_id": row["check_id"],
                "created_at": row["created_at"],
                "verdict": payload.get("verdict"),
                "stats": payload.get("stats", {}),
            }
        )
    return items


def save_impact(conn: sqlite3.Connection, payload: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO impacts (impact_id, created_at, name, version, payload) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            payload["impact_id"],
            payload["created_at"],
            payload["target"]["name"],
            payload["target"]["version"],
            json.dumps(payload, ensure_ascii=False),
        ),
    )


def load_impact(conn: sqlite3.Connection, impact_id: str) -> dict | None:
    row = conn.execute("SELECT payload FROM impacts WHERE impact_id = ?", (impact_id,)).fetchone()
    return json.loads(row["payload"]) if row else None


def latest_impact(conn: sqlite3.Connection, name: str | None = None) -> dict | None:
    if name:
        row = conn.execute(
            "SELECT payload FROM impacts WHERE name = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (name,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT payload FROM impacts ORDER BY created_at DESC, rowid DESC LIMIT 1"
        ).fetchone()
    return json.loads(row["payload"]) if row else None


def add_event(conn: sqlite3.Connection, kind: str, summary: str, detail: dict, now: str) -> None:
    conn.execute(
        "INSERT INTO events (at, kind, summary, detail) VALUES (?, ?, ?, ?)",
        (now, kind, summary, json.dumps(detail, ensure_ascii=False)),
    )


def list_events(conn: sqlite3.Connection, limit: int = 40) -> list[dict]:
    rows = conn.execute(
        "SELECT at, kind, summary, detail FROM events ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [
        {
            "at": row["at"],
            "kind": row["kind"],
            "summary": row["summary"],
            "detail": json.loads(row["detail"] or "{}"),
        }
        for row in rows
    ]


# --------------------------------------------------------------------------- 改名迁移


def _row_to_migration(row: sqlite3.Row) -> dict:
    return {
        "migration_id": row["migration_id"],
        "fingerprint": row["fingerprint"],
        "status": row["status"],
        "renames": json.loads(row["renames"]),
        "preview": json.loads(row["preview"] or "{}"),
        "snapshot": json.loads(row["snapshot"] or "[]"),
        "reason": row["reason"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "committed_at": row["committed_at"],
    }


def get_migration(conn: sqlite3.Connection, migration_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM rename_migrations WHERE migration_id = ?", (migration_id,)
    ).fetchone()
    return _row_to_migration(row) if row else None


def list_migrations(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    rows = conn.execute(
        "SELECT migration_id, fingerprint, status, renames, reason, created_at, updated_at, committed_at "
        "FROM rename_migrations ORDER BY rowid DESC LIMIT ?",
        (limit,),
    ).fetchall()
    items = []
    for row in rows:
        items.append(
            {
                "migration_id": row["migration_id"],
                "fingerprint": row["fingerprint"],
                "status": row["status"],
                "renames": json.loads(row["renames"]),
                "reason": row["reason"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "committed_at": row["committed_at"],
            }
        )
    return items


def upsert_migration_preview(conn: sqlite3.Connection, preview: dict, now: str) -> dict:
    """预演落库：同一组改名（同指纹）复用同一条迁移记录，不重复产生记录。"""
    existing = get_migration(conn, preview["migration_id"])
    renames_json = json.dumps(preview["renames"], ensure_ascii=False)
    preview_json = json.dumps(preview, ensure_ascii=False)
    if existing is None:
        conn.execute(
            """
            INSERT INTO rename_migrations
                (migration_id, fingerprint, status, renames, preview, snapshot, reason,
                 created_at, updated_at, committed_at)
            VALUES (?, ?, 'draft', ?, ?, '[]', '', ?, ?, '')
            """,
            (preview["migration_id"], preview["fingerprint"], renames_json, preview_json, now, now),
        )
    else:
        # 已提交的迁移不允许被预演覆盖；其余状态（draft/rolled_back）刷新预演，记录号不变
        if existing["status"] == "committed":
            return existing
        conn.execute(
            """
            UPDATE rename_migrations
               SET status = 'draft', renames = ?, preview = ?, reason = '', updated_at = ?
             WHERE migration_id = ?
            """,
            (renames_json, preview_json, now, preview["migration_id"]),
        )
    return get_migration(conn, preview["migration_id"])


def list_aliases(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT old_name, new_name, version, migration_id, created_at "
        "FROM name_aliases ORDER BY rowid DESC"
    ).fetchall()
    return [dict(row) for row in rows]


def insert_alias(
    conn: sqlite3.Connection,
    old_name: str,
    new_name: str,
    version: int,
    migration_id: str,
    now: str,
) -> None:
    # INSERT OR IGNORE：重试时同名别名已存在则保留原迁移来源，不重复写
    conn.execute(
        """
        INSERT OR IGNORE INTO name_aliases (old_name, new_name, version, migration_id, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (old_name, new_name, int(version), migration_id, now),
    )


class MigrationApplyError(RuntimeError):
    """切换失败：注册表已回滚到旧版，迁移记录标记为 rolled_back。"""


def apply_rename_migration(
    conn: sqlite3.Connection,
    renames: list[dict],
    preview: dict,
    new_metrics: list[dict],
    now: str,
) -> dict:
    """一次性切换新旧名称，单事务完成；失败回滚旧版注册表。

    幂等保证：
      - 迁移号由改名集合的确定性指纹生成，重试复用同一条记录；
      - 已 committed 的同号迁移直接返回，不再写 metrics/aliases/事件；
      - rolled_back 后允许重试，记录号与版本号都不变，不新增迁移记录或版本。
    """
    migration_id = preview["migration_id"]
    existing = get_migration(conn, migration_id)
    if existing and existing["status"] == "committed":
        return existing

    # 1) 先把迁移记录（含旧版注册表快照）单独落库提交，保证回滚后证据仍在
    snapshot = all_metrics(conn)
    snapshot_json = json.dumps(snapshot, ensure_ascii=False)
    preview_json = json.dumps(preview, ensure_ascii=False)
    renames_json = json.dumps(renames, ensure_ascii=False)
    if existing is None:
        conn.execute(
            """
            INSERT INTO rename_migrations
                (migration_id, fingerprint, status, renames, preview, snapshot, reason,
                 created_at, updated_at, committed_at)
            VALUES (?, ?, 'draft', ?, ?, ?, '', ?, ?, '')
            """,
            (migration_id, preview["fingerprint"], renames_json, preview_json,
             snapshot_json, now, now),
        )
    else:
        conn.execute(
            """
            UPDATE rename_migrations
               SET status = 'draft', renames = ?, preview = ?, snapshot = ?,
                   reason = '', updated_at = ?
             WHERE migration_id = ?
            """,
            (renames_json, preview_json, snapshot_json, now, migration_id),
        )
    conn.commit()

    mapping = {item["old_name"]: item["new_name"] for item in renames}
    try:
        # 2) 一次性切换：先清空再按新注册表写回，全部版本号保持不变（不产生新版本）
        conn.execute("DELETE FROM metrics")
        for metric in new_metrics:
            insert_metric(conn, metric, now)
        # 3) 沉淀旧名别名：旧名的每个版本都能查到当前版本与迁移来源
        for metric in snapshot:
            if metric["name"] in mapping:
                insert_alias(
                    conn,
                    metric["name"],
                    mapping[metric["name"]],
                    int(metric["version"]),
                    migration_id,
                    now,
                )
        # 4) 切换后自检：行数守恒、主键无碰撞
        switched = all_metrics(conn)
        if len(switched) != len(snapshot):
            raise MigrationApplyError(
                f"切换后指标版本数变化：{len(snapshot)} → {len(switched)}"
            )
        seen_keys: set[tuple[str, int]] = set()
        for metric in switched:
            key = (metric["name"], int(metric["version"]))
            if key in seen_keys:
                raise MigrationApplyError(f"切换后出现重复主键 {key[0]}@{key[1]}")
            seen_keys.add(key)

        conn.execute(
            "UPDATE rename_migrations SET status = 'committed', committed_at = ?, "
            "updated_at = ?, reason = '' WHERE migration_id = ?",
            (now, now, migration_id),
        )
        add_event(
            conn,
            "rename_migration",
            f"改名迁移 {migration_id} 一次性切换成功："
            + "、".join(f"{item['old_name']}→{item['new_name']}" for item in renames),
            {
                "migration_id": migration_id,
                "renames": renames,
                "affected_formula_count": preview.get("affected_summary", {}).get(
                    "affected_formula_count", 0
                ),
            },
            now,
        )
        conn.commit()
    except Exception as exc:  # 切换失败：恢复旧版注册表
        conn.rollback()  # metrics/name_aliases 回到第 1 步提交后的旧版状态
        reason = f"切换失败，注册表已恢复旧版：{exc}"
        conn.execute(
            "UPDATE rename_migrations SET status = 'rolled_back', reason = ?, updated_at = ? "
            "WHERE migration_id = ?",
            (reason, now, migration_id),
        )
        add_event(
            conn,
            "rename_migration_rollback",
            f"改名迁移 {migration_id} 切换失败并回滚：{exc}",
            {"migration_id": migration_id, "renames": renames},
            now,
        )
        conn.commit()
        # 回滚后再确认注册表确实恢复
        restored = all_metrics(conn)
        if {(m["name"], m["version"]) for m in restored} != {
            (m["name"], m["version"]) for m in snapshot
        }:
            raise MigrationApplyError("回滚后注册表与快照不一致，请人工核对")
        raise MigrationApplyError(reason) from exc

    return get_migration(conn, migration_id)
