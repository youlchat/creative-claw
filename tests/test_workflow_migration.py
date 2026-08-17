from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from creative_claw.db import SCHEMA_VERSION, Database
from creative_claw.indexer import Indexer
from creative_claw.repository import Repository
from creative_claw.util import json_loads


class WorkflowMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.database = Database(self.root / "creative-claw.db")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_current_schema_seeds_both_media_templates(self) -> None:
        self.database.initialize()

        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT template_key, version, media_type, definition_json "
                "FROM workflow_templates ORDER BY template_key"
            ).fetchall()

        self.assertEqual(self.database.schema_version(), SCHEMA_VERSION)
        self.assertEqual(
            [(row["template_key"], row["version"], row["media_type"]) for row in rows],
            [
                ("novel", 1, "novel"),
                ("vertical_short_drama", 1, "vertical_short_drama"),
            ],
        )
        self.assertEqual(
            {
                row["template_key"]: len(json_loads(row["definition_json"])["stages"])
                for row in rows
            },
            {"novel": 13, "vertical_short_drama": 16},
        )

    def test_v3_rows_map_to_production_objects_idempotently(self) -> None:
        self.database.initialize()
        repository = Repository(self.database)
        project = repository.create_project("迁移项目", self.root / "project")
        source = Indexer(self.database).index_text(
            project["id"],
            "legacy/source.md",
            "第一段旧来源。\n\n第二段旧来源。",
            title="旧来源",
            branch="main",
            canon_status="reference",
        )
        scene = repository.add_timeline_event(
            project["id"],
            "旧场景",
            "旧场景正文",
            episode=1,
            scene=2,
            branch="main",
        )
        point = repository.upsert_ohlc(
            project["id"],
            "顾遥",
            "信任度",
            "scene",
            "E1-S02",
            1.02,
            40,
            60,
            35,
            55,
            timeline_event_id=scene["id"],
        )
        ledger_before = repository.ledger.verify(project["id"])

        with self.database.connect() as connection:
            connection.execute("PRAGMA user_version = 3")

        self.database.initialize()

        with self.database.connect() as connection:
            source_artifact = connection.execute(
                "SELECT * FROM artifacts WHERE source_document_id=?",
                (source.document_id,),
            ).fetchone()
            scene_unit = connection.execute(
                "SELECT * FROM production_units WHERE source_timeline_event_id=?",
                (scene["id"],),
            ).fetchone()
            scene_artifact = connection.execute(
                "SELECT * FROM artifacts WHERE source_timeline_event_id=?",
                (scene["id"],),
            ).fetchone()
            version_rows = connection.execute(
                "SELECT av.content FROM artifact_versions av "
                "JOIN artifacts a ON a.id=av.artifact_id "
                "WHERE a.source_document_id=? OR a.source_timeline_event_id=? "
                "ORDER BY a.artifact_type",
                (source.document_id, scene["id"]),
            ).fetchall()
            ohlc_row = connection.execute(
                "SELECT id, timeline_event_id FROM ohlc_points WHERE id=?", (point["id"],)
            ).fetchone()
            mapped_counts = {
                "units": connection.execute(
                    "SELECT COUNT(*) AS n FROM production_units WHERE project_id=?",
                    (project["id"],),
                ).fetchone()["n"],
                "artifacts": connection.execute(
                    "SELECT COUNT(*) AS n FROM artifacts WHERE project_id=?",
                    (project["id"],),
                ).fetchone()["n"],
                "versions": connection.execute(
                    "SELECT COUNT(*) AS n FROM artifact_versions av "
                    "JOIN artifacts a ON a.id=av.artifact_id WHERE a.project_id=?",
                    (project["id"],),
                ).fetchone()["n"],
            }

        self.assertIsNotNone(source_artifact)
        self.assertEqual(source_artifact["artifact_type"], "source")
        self.assertIsNotNone(scene_unit)
        self.assertEqual(scene_unit["unit_type"], "scene")
        self.assertIsNotNone(scene_artifact)
        self.assertEqual(scene_artifact["artifact_type"], "manuscript")
        self.assertEqual(
            {row["content"] for row in version_rows},
            {"第一段旧来源。\n\n第二段旧来源。", "旧场景正文"},
        )
        self.assertEqual(ohlc_row["timeline_event_id"], scene["id"])
        self.assertEqual(repository.ledger.verify(project["id"]), ledger_before)

        self.database.initialize()
        with self.database.connect() as connection:
            counts_after_second_run = {
                "units": connection.execute(
                    "SELECT COUNT(*) AS n FROM production_units WHERE project_id=?",
                    (project["id"],),
                ).fetchone()["n"],
                "artifacts": connection.execute(
                    "SELECT COUNT(*) AS n FROM artifacts WHERE project_id=?",
                    (project["id"],),
                ).fetchone()["n"],
                "versions": connection.execute(
                    "SELECT COUNT(*) AS n FROM artifact_versions av "
                    "JOIN artifacts a ON a.id=av.artifact_id WHERE a.project_id=?",
                    (project["id"],),
                ).fetchone()["n"],
            }
        self.assertEqual(counts_after_second_run, mapped_counts)


if __name__ == "__main__":
    unittest.main()
