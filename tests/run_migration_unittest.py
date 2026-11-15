"""改名迁移纯逻辑验证（只用标准库，环境无 pytest 时也能跑）。

覆盖：
1. 预演列出受影响公式、名称冲突、依赖变化；
2. 一次性切换后下游表达式全部认新名、版本号不变、归一化口径不变；
3. 切换后旧名经别名仍可解析（历史公式兼容）；
4. 指纹确定性 + 重试不重复产生迁移记录（store 层，用临时 sqlite）；
5. 切换失败回滚旧版注册表，重试成功且迁移号/版本不变；
6. 改名链 / 撞名 / 多对一 等冲突被拦截。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DATA_DIR = Path(tempfile.mkdtemp(prefix="metricring-mig-test-"))
os.environ.setdefault("METRICRING_DATA_DIR", str(DATA_DIR))
os.environ.setdefault("METRICRING_DB", str(DATA_DIR / "test.sqlite3"))

from app import graph as graph_mod  # noqa: E402
from app import migration, registry, report, store  # noqa: E402
from app.settings import SAMPLE_PATH  # noqa: E402


def sample_metrics() -> list[dict]:
    return registry.load_sample(SAMPLE_PATH)["metrics"]


class MigrationPreviewTests(unittest.TestCase):
    def setUp(self):
        self.metrics = sample_metrics()

    def test_preview_lists_affected_formulas_and_preserves_structure(self):
        renames = migration.normalize_renames([
            {"old_name": "gmv_daily", "new_name": "net_gmv_daily"}
        ])
        preview = migration.build_preview(self.metrics, renames)
        self.assertTrue(preview["ok"], preview["conflicts"])
        self.assertFalse(preview["blocked"], preview["reason"])
        # gmv_daily 自身 1 个版本 + 5 个直接引用它的下游公式（第 6 个下游是传递依赖）
        summary = preview["affected_summary"]
        self.assertEqual(summary["renamed_version_count"], 1)
        self.assertEqual(summary["affected_formula_count"], 6)
        self.assertEqual(summary["reference_rewrite_count"], 5)
        self.assertTrue(preview["edge_diff"]["structure_preserved"])
        self.assertEqual(preview["edge_diff"]["added"], [])
        self.assertEqual(preview["edge_diff"]["removed"], [])
        # 归一化口径结构守恒：把新名映射回旧名后，归一化表达式必须完全一致
        inverse = {v: k for k, v in preview["mapping"].items()}
        for formula in preview["affected_formulas"]:
            before = formula["normalized_before"]
            after = formula["normalized_after"]
            for new_name, old_name in inverse.items():
                after = after.replace(f"metric:{new_name}", f"metric:{old_name}")
            self.assertEqual(before, after)

    def test_conflict_new_name_taken(self):
        renames = migration.normalize_renames([
            {"old_name": "gmv_daily", "new_name": "gmv"}  # gmv 已存在且本批不改
        ])
        preview = migration.build_preview(self.metrics, renames)
        self.assertFalse(preview["ok"])
        self.assertIn("new_name_taken", preview["conflict_codes"])

    def test_conflict_rename_chain_and_duplicate_target(self):
        renames = migration.normalize_renames([
            {"old_name": "cpc", "new_name": "cpa"},   # cpa 是另一条改名的旧名
            {"old_name": "cpa", "new_name": "cpa_v2"},
        ])
        validation = migration.validate_renames(renames, self.metrics)
        self.assertIn("target_renamed_twice", validation["conflict_codes"])

        dup = migration.normalize_renames([
            {"old_name": "cpc", "new_name": "shared_name"},
            {"old_name": "cpa", "new_name": "shared_name"},
        ])
        validation = migration.validate_renames(dup, self.metrics)
        self.assertIn("duplicate_target", validation["conflict_codes"])

    def test_old_name_not_found_and_invalid_name(self):
        renames = migration.normalize_renames([
            {"old_name": "not_a_metric", "new_name": "whatever"},
            {"old_name": "cpc", "new_name": "Bad-Name"},
        ])
        validation = migration.validate_renames(renames, self.metrics)
        self.assertIn("old_name_not_found", validation["conflict_codes"])
        self.assertIn("invalid_new_name", validation["conflict_codes"])

    def test_fingerprint_is_order_independent_and_stable(self):
        a = migration.normalize_renames([
            {"old_name": "cpc", "new_name": "cpc_v2"},
            {"old_name": "cpa", "new_name": "cpa_v2"},
        ])
        b = migration.normalize_renames([
            {"old_name": "cpa", "new_name": "cpa_v2"},
            {"old_name": "cpc", "new_name": "cpc_v2"},
        ])
        self.assertEqual(migration.migration_id(a), migration.migration_id(b))

    def test_batch_two_renames_in_one_migration(self):
        renames = migration.normalize_renames([
            {"old_name": "cpc", "new_name": "click_cost"},
            {"old_name": "cpa", "new_name": "acquire_cost"},
        ])
        preview = migration.build_preview(self.metrics, renames)
        self.assertTrue(preview["ok"], preview["conflicts"])
        new_metrics, _ = migration.apply_renames(self.metrics, renames)
        names = {m["name"] for m in new_metrics}
        self.assertIn("click_cost", names)
        self.assertIn("acquire_cost", names)
        self.assertNotIn("cpc", names)
        # cpc/cpa 互相引用的环在换名后仍然存在（结构守恒）
        graph = graph_mod.build_graph(new_metrics)
        self.assertEqual(len(graph_mod.find_cycles(graph)), 2)
        # 环的节点换成了新名
        cycle_nodes = graph_mod.cycle_nodes(graph)
        self.assertIn("click_cost@1", cycle_nodes)
        self.assertIn("acquire_cost@1", cycle_nodes)


class OneShotSwitchAndAliasTests(unittest.TestCase):
    def setUp(self):
        self.metrics = sample_metrics()
        self.renames = migration.normalize_renames([
            {"old_name": "gmv_daily", "new_name": "net_gmv_daily"}
        ])

    def test_switch_rewrites_all_downstream_at_once(self):
        new_metrics, changes = migration.apply_renames(self.metrics, self.renames)
        # 不允许半新半旧：没有任何表达式还引用旧名
        for metric in new_metrics:
            analysis = __import__("app.expr", fromlist=["analyze_expression"]).analyze_expression(
                metric["expression"]
            )
            self.assertNotIn("gmv_daily", [ref["name"] for ref in analysis["metrics"]])
        # 版本号全部保持不变（不产生新版本）
        old_keys = {(m["name"], m["version"]) for m in self.metrics}
        new_keys = {(m["name"], m["version"]) for m in new_metrics}
        self.assertEqual(len(old_keys), len(new_keys))
        renamed = next(m for m in new_metrics if m["name"] == "net_gmv_daily")
        self.assertEqual(renamed["version"], 1)
        # 下游 gmv_7d 表达式已认新名
        gmv_7d = next(m for m in new_metrics if m["name"] == "gmv_7d")
        self.assertIn("metric:net_gmv_daily", gmv_7d["expression"])

    def test_historical_formula_with_old_name_resolves_via_alias(self):
        new_metrics, _ = migration.apply_renames(self.metrics, self.renames)
        aliases = [
            {"old_name": "gmv_daily", "new_name": "net_gmv_daily", "version": 1,
             "migration_id": migration.migration_id(self.renames)}
        ]
        # 构造一条仍写旧名的历史公式
        historical = registry.normalize_metric({
            "name": "legacy_report",
            "version": 1,
            "expression": "sum(metric:gmv_daily) / 7",
            "aggregation": "sum",
            "dimensions": ["dt"],
            "published": False,
        })
        graph = graph_mod.build_graph(new_metrics + [historical], aliases)
        # 旧名引用经别名解析到新名，不产生不可解析引用
        unresolved_targets = [u["target"] for u in graph.unresolved]
        self.assertNotIn("gmv_daily", unresolved_targets)
        self.assertIn("net_gmv_daily@1", graph.deps["legacy_report@1"])

        # resolve_alias 与链路追溯
        self.assertEqual(
            migration.resolve_alias("gmv_daily", new_metrics, aliases), "net_gmv_daily"
        )
        self.assertEqual(
            migration.alias_chain("gmv_daily", aliases), ["gmv_daily", "net_gmv_daily"]
        )

    def test_chained_alias_across_two_migrations(self):
        first, _ = migration.apply_renames(
            self.metrics,
            migration.normalize_renames([{"old_name": "gmv_daily", "new_name": "net_gmv_daily"}]),
        )
        second, _ = migration.apply_renames(
            first,
            migration.normalize_renames([{"old_name": "net_gmv_daily", "new_name": "settled_gmv"}]),
        )
        aliases = [
            {"old_name": "gmv_daily", "new_name": "net_gmv_daily", "version": 1, "migration_id": "m1"},
            {"old_name": "net_gmv_daily", "new_name": "settled_gmv", "version": 1, "migration_id": "m2"},
        ]
        self.assertEqual(migration.resolve_alias("gmv_daily", second, aliases), "settled_gmv")
        self.assertEqual(
            migration.alias_chain("gmv_daily", aliases),
            ["gmv_daily", "net_gmv_daily", "settled_gmv"],
        )


class StoreTransactionTests(unittest.TestCase):
    def setUp(self):
        self.db = DATA_DIR / f"store_{os.getpid()}_{id(self)}.sqlite3"
        self.conn = store.connect(self.db)
        store.init_schema(self.conn)
        self.metrics = sample_metrics()
        now = report.now_iso()
        for metric in self.metrics:
            store.insert_metric(self.conn, metric, now)
        self.conn.commit()
        self.renames = migration.normalize_renames([
            {"old_name": "gmv_daily", "new_name": "net_gmv_daily"}
        ])
        self.preview = migration.build_preview(self.metrics, self.renames)
        self.new_metrics, _ = migration.apply_renames(self.metrics, self.renames)

    def tearDown(self):
        self.conn.close()

    def test_commit_then_rollback_then_retry_is_idempotent(self):
        mig_id = self.preview["migration_id"]
        now = report.now_iso()
        record = store.apply_rename_migration(
            self.conn, self.renames, self.preview, self.new_metrics, now
        )
        self.assertEqual(record["status"], "committed")

        # 别名沉淀：旧名版本可查迁移来源
        aliases = store.list_aliases(self.conn)
        self.assertTrue(any(a["old_name"] == "gmv_daily" and a["migration_id"] == mig_id
                            for a in aliases))
        # 版本号不变
        switched = store.all_metrics(self.conn)
        self.assertEqual(len(switched), len(self.metrics))
        self.assertTrue(any(m["name"] == "net_gmv_daily" and m["version"] == 1 for m in switched))

        # 幂等重试：同指纹直接返回 committed，不新增迁移记录/别名/事件
        before_events = len(store.list_events(self.conn, limit=1000))
        again = store.apply_rename_migration(
            self.conn, self.renames, self.preview, self.new_metrics, report.now_iso()
        )
        self.assertEqual(again["status"], "committed")
        self.assertEqual(again["migration_id"], mig_id)
        after_events = len(store.list_events(self.conn, limit=1000))
        self.assertEqual(before_events, after_events)
        alias_count = len(store.list_aliases(self.conn))
        store.apply_rename_migration(
            self.conn, self.renames, self.preview, self.new_metrics, report.now_iso()
        )
        self.assertEqual(len(store.list_aliases(self.conn)), alias_count)

    def test_failure_rolls_back_and_retry_succeeds_with_same_id(self):
        mig_id = self.preview["migration_id"]
        snapshot = store.all_metrics(self.conn)

        # 构造一个必然失败的切换：new_metrics 里制造重复主键
        broken = [dict(m) for m in self.new_metrics]
        broken[0]["name"] = broken[1]["name"]
        broken[0]["version"] = broken[1]["version"]
        with self.assertRaises(store.MigrationApplyError):
            store.apply_rename_migration(
                self.conn, self.renames, self.preview, broken, report.now_iso()
            )

        # 注册表已恢复旧版
        restored = store.all_metrics(self.conn)
        self.assertEqual(
            {(m["name"], m["version"]) for m in restored},
            {(m["name"], m["version"]) for m in snapshot},
        )
        record = store.get_migration(self.conn, mig_id)
        self.assertEqual(record["status"], "rolled_back")
        self.assertIn("恢复旧版", record["reason"])

        # 重试成功：迁移号不变，不新增迁移记录
        ok = store.apply_rename_migration(
            self.conn, self.renames, self.preview, self.new_metrics, report.now_iso()
        )
        self.assertEqual(ok["migration_id"], mig_id)
        self.assertEqual(ok["status"], "committed")
        records = [m for m in store.list_migrations(self.conn, limit=50)
                   if m["migration_id"] == mig_id]
        self.assertEqual(len(records), 1)

    def test_preview_upsert_keeps_single_record(self):
        now = report.now_iso()
        store.upsert_migration_preview(self.conn, self.preview, now)
        store.upsert_migration_preview(self.conn, self.preview, now)
        records = [m for m in store.list_migrations(self.conn, limit=50)
                   if m["migration_id"] == self.preview["migration_id"]]
        self.assertEqual(len(records), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
