from __future__ import annotations

import sqlite3
import tempfile
import unittest
from array import array
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

from creative_claw.db import SCHEMA, SCHEMA_VERSION, Database
from creative_claw.indexer import Indexer
from creative_claw.llm import OpenAICompatibleWriter
from creative_claw.repository import Repository
from creative_claw.retrieval import HybridRetriever
from creative_claw.runtime import AgentRuntime
from creative_claw.tools import CreativeToolset


class FailingEmbeddingProvider:
    name = "openai-compatible:offline-test"
    dimension = 3

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise ConnectionError("test endpoint unavailable")


class CreativeClawMaintenanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = Database(self.root / "knowledge.db")
        self.database.initialize()
        self.repository = Repository(self.database)
        self.repository.create_project("维护测试", self.root, "demo")
        self.indexer = Indexer(self.database)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_schema_migrates_unversioned_embedding_metadata(self) -> None:
        legacy_path = self.root / "legacy.db"
        legacy_schema = SCHEMA.replace(
            "    embedding_provider TEXT NOT NULL DEFAULT 'hash-v1',\n", ""
        )
        with closing(sqlite3.connect(legacy_path)) as connection:
            connection.executescript(legacy_schema)
            connection.execute("PRAGMA user_version = 1")
            connection.commit()
        legacy = Database(legacy_path)
        legacy.initialize()
        self.assertEqual(legacy.schema_version(), SCHEMA_VERSION)
        with legacy.connect() as connection:
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(chunks)")}
        self.assertIn("embedding_provider", columns)

    def test_schema_v3_backfills_unambiguous_scene_links(self) -> None:
        legacy_path = self.root / "legacy-v2.db"
        legacy_schema = SCHEMA.replace(
            "    timeline_event_id TEXT REFERENCES timeline_events(id) ON DELETE SET NULL,\n",
            "",
        )
        with closing(sqlite3.connect(legacy_path)) as connection:
            connection.executescript(legacy_schema)
            connection.execute(
                "INSERT INTO projects(id, name, root_path, created_at) VALUES ('p', '旧项目', '.', 'now')"
            )
            connection.execute(
                """
                INSERT INTO timeline_events(id, project_id, label, episode, scene, description, branch, attrs_json, created_at)
                VALUES ('scene-1', 'p', '旧场景', 18, 7, '旧正文', 'main', '{}', 'now')
                """
            )
            connection.execute(
                """
                INSERT INTO ohlc_points(id, project_id, character_name, dimension, period_type, period_id,
                                        parent_period_id, sort_key, open, high, low, close, branch, attrs_json, created_at, updated_at)
                VALUES ('o-1', 'p', '沈霜', '知情度', 'scene', 'E18-S07', 'E18', 18.07, 40, 55, 35, 50,
                        'main', '{}', 'now', 'now')
                """
            )
            connection.execute("PRAGMA user_version = 2")
            connection.commit()
        legacy = Database(legacy_path)
        legacy.initialize()
        with legacy.connect() as connection:
            row = connection.execute("SELECT timeline_event_id FROM ohlc_points WHERE id='o-1'").fetchone()
        self.assertEqual(row["timeline_event_id"], "scene-1")

    def test_ohlc_scene_link_rejects_cross_project_and_keeps_parent_unlinked(self) -> None:
        scene = self.repository.add_timeline_event(
            "demo", "偏殿", "沈霜听见密谈。", episode=18, scene=7, branch="main"
        )
        linked = self.repository.upsert_ohlc(
            "demo",
            "沈霜",
            "知情度",
            "scene",
            "E18-S07",
            18.07,
            40,
            62,
            35,
            57,
            parent_period_id="E18",
            timeline_event_id=scene["id"],
        )
        self.assertEqual(linked["timeline_event_id"], scene["id"])
        series = self.repository.ohlc_series("demo", "沈霜", "知情度")
        self.assertIsNone(next(row for row in series if row["period_id"] == "E18")["timeline_event_id"])

        other = self.repository.create_project("别的项目", self.root / "other", "other")
        other_scene = self.repository.add_timeline_event(
            other["id"], "别处", "不属于 demo。", episode=1, scene=1
        )
        with self.assertRaisesRegex(ValueError, "same project and branch"):
            self.repository.upsert_ohlc(
                "demo",
                "沈霜",
                "知情度",
                "scene",
                "E18-S08",
                18.08,
                57,
                65,
                50,
                60,
                timeline_event_id=other_scene["id"],
            )

    def test_document_lifecycle_stats_and_embedding_backfill(self) -> None:
        source = self.root / "E18.md"
        source.write_text("E18：沈霜在偏殿得知遗孤仍在人世。", encoding="utf-8")
        imported = self.indexer.import_file(
            "demo", source, metadata={"episode": 18}, canon_status="canon"
        )
        first_version = imported.version
        stats = self.repository.knowledge_stats("demo")
        self.assertEqual(stats["documents"], 1)
        self.assertEqual(stats["chunks"], 1)
        self.assertEqual(stats["schema_version"], SCHEMA_VERSION)
        self.assertTrue(stats["ledger"]["valid"])

        reindexed = self.indexer.reindex_document("demo", imported.document_id)
        self.assertEqual(reindexed.version, first_version + 1)

        with self.database.connect() as connection:
            connection.execute(
                "UPDATE chunks SET embedding=NULL, embedding_dim=NULL WHERE document_id=?",
                (imported.document_id,),
            )
        backfill = self.indexer.backfill_embeddings("demo")
        self.assertEqual(backfill["updated_chunks"], 1)

        deleted = self.indexer.delete_document("demo", imported.document_id)
        self.assertEqual(deleted["deleted_chunks"], 1)
        self.assertTrue(source.is_file())
        self.assertEqual(self.repository.knowledge_stats("demo")["documents"], 0)

    def test_branch_isolation_invalid_ohlc_and_vector_failure(self) -> None:
        self.indexer.index_text(
            "demo", "main/E18.md", "主线密诏写明齐尧身份。", branch="main", canon_status="canon"
        )
        self.indexer.index_text(
            "demo", "what-if/E18.md", "支线密诏已被焚毁。", branch="what-if", canon_status="draft"
        )
        retriever = HybridRetriever(self.database)
        main_hits = retriever.search("demo", "密诏", filters={"branch": "main"})
        branch_hits = retriever.search("demo", "密诏", filters={"branch": "what-if"})
        self.assertTrue(main_hits and branch_hits)
        self.assertIn("齐尧", main_hits[0].text)
        self.assertIn("焚毁", branch_hits[0].text)

        with self.assertRaises(ValueError):
            self.repository.upsert_ohlc(
                "demo", "沈霜", "知情度", "scene", "E18-S01", 18.01, 50, 40, 30, 45
            )

        # Mark one stored vector as remote to emulate a previously available
        # compatible endpoint. A failed query must retain lexical results.
        packed = array("f", [1.0, 0.0, 0.0]).tobytes()
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE chunks SET embedding=?, embedding_dim=3, embedding_provider=?
                WHERE id=(SELECT id FROM chunks WHERE branch='main' LIMIT 1)
                """,
                (packed, FailingEmbeddingProvider.name),
            )
        degraded = HybridRetriever(self.database, FailingEmbeddingProvider()).build_context(
            "demo", "主线密诏", filters={"branch": "main"}
        )
        self.assertTrue(degraded["citations"])
        self.assertTrue(degraded["retrieval_policy"]["vector"]["degraded"])
        self.assertIn("lexical", degraded["retrieval_policy"]["vector"]["warning"])

    def test_write_tool_can_be_explicitly_rejected(self) -> None:
        runtime = AgentRuntime(self.database, CreativeToolset(self.database).build_registry())
        with self.assertRaises(ValueError):
            runtime.create_task(
                "demo", "无效计划", [{"tool": "export_excel", "args": {"output_path": "x.xlsx"}}]
            )
        task = runtime.create_task(
            "demo",
            "导出未经批准的表格",
            [
                {
                    "tool": "export_excel",
                    "args": {
                        "output_path": "artifacts/rejected.xlsx",
                        "sheets": [{"name": "数据", "rows": [["状态"], ["拒绝"]]}],
                    },
                }
            ],
        )
        pending = runtime.step(task["id"])
        self.assertEqual(pending["status"], "awaiting_approval")
        rejected = runtime.reject(task["id"], reason="不要覆盖现有输出")
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(rejected["tool_runs"][0]["approval_status"], "rejected")
        self.assertFalse((self.root / "artifacts" / "rejected.xlsx").exists())

    def test_minimax_openai_compatible_writer_contract(self) -> None:
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "model": "MiniMax-M3",
            "choices": [{"message": {"content": "<think>内部推理不能外露</think>\nE18 只能确认遗孤存在。[C1]"}}],
            "usage": {"total_tokens": 128},
        }
        with patch("creative_claw.llm.requests.post", return_value=response) as post:
            result = OpenAICompatibleWriter(
                "https://api.minimaxi.com/v1", "test-secret", "MiniMax-M3"
            ).answer(
                "检查沈霜的知情边界",
                {"context": "[C1] E18 未确认身份", "graph": {}, "timeline": [], "ohlc": []},
                mode="consistency",
            )
        self.assertEqual(result["model"], "MiniMax-M3")
        self.assertIn("[C1]", result["answer"])
        self.assertNotIn("内部推理", result["answer"])
        self.assertTrue(result["reasoning_filtered"])
        call = post.call_args
        self.assertEqual(call.args[0], "https://api.minimaxi.com/v1/chat/completions")
        self.assertEqual(call.kwargs["json"]["model"], "MiniMax-M3")
        self.assertEqual(call.kwargs["headers"]["Authorization"], "Bearer test-secret")


if __name__ == "__main__":
    unittest.main()
