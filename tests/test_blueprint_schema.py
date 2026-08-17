from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from creative_claw import db as db_module
from creative_claw.blueprint_models import (
    BLUEPRINT_DIMENSIONS,
    validate_evidence,
    validate_node,
)
from creative_claw.blueprint_repository import BlueprintRepository
from creative_claw.db import Database
from creative_claw.repository import Repository
from creative_claw.util import json_dumps, new_id, utc_now


def complete_dimensions() -> dict:
    return {
        name: {
            "state": "not_observed",
            "value": None,
            "confidence": 1.0,
            "evidence_refs": [],
        }
        for name in BLUEPRINT_DIMENSIONS
    }


class BlueprintSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.database = Database(self.root / "creative-claw.db")
        self.database.initialize()
        self.repository = Repository(self.database)
        self.project_a = self.repository.create_project("A", self.root / "a")
        self.project_b = self.repository.create_project("B", self.root / "b")
        self.blueprints = BlueprintRepository(self.database)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_schema_v5_requires_every_dimension_and_valid_evidence(self) -> None:
        self.assertEqual(db_module.SCHEMA_VERSION, 5)
        self.assertEqual(self.database.schema_version(), 5)
        with self.database.connect() as connection:
            tables = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        self.assertTrue(
            {
                "blueprint_jobs",
                "blueprint_batches",
                "blueprint_agent_runs",
                "blueprint_nodes",
                "blueprint_evidence",
                "blueprint_interpretations",
                "blueprint_conflicts",
                "blueprint_edges",
                "target_settings",
                "blueprint_mappings",
                "draft_candidates",
                "similarity_assessments",
            }.issubset(tables)
        )

        with self.assertRaisesRegex(ValueError, "missing blueprint dimensions"):
            validate_node({"dimensions": {"causality": {"state": "observed"}}})
        with self.assertRaisesRegex(ValueError, "evidence range"):
            validate_evidence({"start": 9, "end": 2, "source_length": 20})

        dimensions = complete_dimensions()
        dimensions["causality"] = {
            "state": "observed",
            "value": {"summary": "选择导致损失"},
            "confidence": 0.8,
            "evidence_refs": ["ev_1"],
        }
        validated = validate_node(
            {
                "stable_key": "work",
                "node_type": "work",
                "dimensions": dimensions,
            }
        )
        self.assertEqual(validated["dimensions"]["causality"]["state"], "observed")

    def test_repository_hides_cross_project_objects(self) -> None:
        job = self.blueprints.create_job(
            self.project_a["id"],
            job_type="reference",
            input_json={"title": "样本"},
            idempotency_key="reference:a",
        )
        node = self.blueprints.create_node(
            self.project_a["id"],
            artifact_version_id=None,
            job_id=job["id"],
            stable_key="work",
            node_type="work",
            dimensions=complete_dimensions(),
        )
        candidate = self.blueprints.create_candidate(
            self.project_a["id"],
            target_blueprint_version_id=None,
            unit_id=None,
            artifact_id=None,
            unit_plan={},
            text="候选",
            base_version_id=None,
            generation_metadata={},
        )

        self.assertEqual(self.blueprints.get_job(self.project_a["id"], job["id"])["id"], job["id"])
        self.assertEqual(self.blueprints.get_node(self.project_a["id"], node["id"])["id"], node["id"])
        self.assertEqual(
            self.blueprints.get_candidate(self.project_a["id"], candidate["id"])["id"],
            candidate["id"],
        )
        for getter, identifier in (
            (self.blueprints.get_job, job["id"]),
            (self.blueprints.get_node, node["id"]),
            (self.blueprints.get_candidate, candidate["id"]),
        ):
            with self.assertRaises(KeyError):
                getter(self.project_b["id"], identifier)

    def test_repository_rejects_cross_project_foreign_references_on_writes(self) -> None:
        from creative_claw.workflow import WorkflowService

        workflow = WorkflowService(self.database)
        artifact = workflow.create_artifact(self.project_a["id"], "target_setting", "A setting")
        version = workflow.save_artifact_version(
            self.project_a["id"], artifact["id"], "{}", expected_current_version_id=None,
            change_summary="A version",
        )["version"]
        job = self.blueprints.create_job(
            self.project_a["id"], job_type="migration", input_json={}, idempotency_key="cross:job"
        )
        node = self.blueprints.create_node(
            self.project_a["id"], artifact_version_id=version["id"], job_id=None,
            stable_key="work", node_type="work", dimensions=complete_dimensions(),
        )

        with self.assertRaises(KeyError):
            self.blueprints.create_target_setting_record(
                self.project_b["id"], artifact_id=artifact["id"], artifact_version_id=version["id"],
                source_text="cross", structured={},
            )
        with self.assertRaises(KeyError):
            self.blueprints.create_mapping(
                self.project_b["id"], job_id=job["id"], reference_version_id=version["id"],
                target_version_id=version["id"], reference_node_id=node["id"], target_node_id=node["id"],
                action="transform", rationale="cross",
            )
        with self.assertRaises(KeyError):
            self.blueprints.create_candidate(
                self.project_b["id"], target_blueprint_version_id=version["id"], unit_id=None,
                artifact_id=artifact["id"], unit_plan={}, text="cross", base_version_id=version["id"],
                generation_metadata={},
            )

    def test_schema_v5_startup_recovers_interrupted_running_jobs_without_dispatch(self) -> None:
        job = self.blueprints.create_job(
            self.project_a["id"], job_type="reference", input_json={"text": "running"},
            idempotency_key="recover:running", status="running",
        )
        batch = self.blueprints.create_batch(
            self.project_a["id"], job["id"], ordinal=0, start_offset=0, end_offset=7,
            overlap_start=0, source_hash="hash", idempotency_key="recover:batch",
        )
        self.blueprints.update_batch(self.project_a["id"], batch["id"], status="running")

        Database(self.database.path).initialize()

        recovered = self.blueprints.get_job(self.project_a["id"], job["id"])
        recovered_batch = self.blueprints.list_batches(self.project_a["id"], job["id"])[0]
        self.assertEqual((recovered["status"], recovered["desired_state"]), ("resumable", "paused"))
        self.assertEqual(recovered_batch["status"], "resumable")

    def test_v4_upgrade_preserves_workflow_artifacts_ohlc_and_ledger(self) -> None:
        legacy_path = self.root / "legacy-v4.db"
        legacy = Database(legacy_path)
        now = utc_now()
        project_id = new_id("prj")
        template_id = new_id("wft")
        workflow_id = new_id("wfl")
        artifact_id = new_id("art")
        version_id = new_id("ver")
        with legacy.connect() as connection:
            fixture = Path(__file__).with_name("fixtures").joinpath("schema_v4.sql")
            connection.executescript(fixture.read_text(encoding="utf-8"))
            connection.execute(
                "INSERT INTO projects(id, name, root_path, created_at) VALUES (?, 'Legacy project', ?, ?)",
                (project_id, str(self.root / "legacy"), now),
            )
            connection.execute(
                """INSERT INTO workflow_templates(
                       id, template_key, version, media_type, name, description, definition_json, created_at
                   ) VALUES (?, 'legacy-template', 1, 'novel', 'Legacy template', '', '{}', ?)""",
                (template_id, now),
            )
            connection.execute(
                """INSERT INTO project_workflows(
                       id, project_id, template_id, media_type, name, status, created_at, updated_at
                   ) VALUES (?, ?, ?, 'novel', 'Legacy workflow', 'active', ?, ?)""",
                (workflow_id, project_id, template_id, now, now),
            )
            connection.execute(
                """INSERT INTO artifacts(
                       id, project_id, artifact_type, title, status, current_version_id,
                       branch, attrs_json, created_at, updated_at
                   ) VALUES (?, ?, 'outline', 'Legacy outline', 'draft', ?, 'main', '{}', ?, ?)""",
                (artifact_id, project_id, version_id, now, now),
            )
            connection.execute(
                """INSERT INTO artifact_versions(
                       id, artifact_id, version_number, content, content_format, source_kind,
                       change_summary, actor, metadata_json, created_at
                   ) VALUES (?, ?, 1, 'legacy content', 'text/plain', 'user',
                             'legacy version', 'legacy', '{}', ?)""",
                (version_id, artifact_id, now),
            )
            connection.execute(
                """INSERT INTO ohlc_points(
                       id, project_id, character_name, dimension, period_type, period_id,
                       sort_key, open, high, low, close, branch, attrs_json, created_at, updated_at
                   ) VALUES (?, ?, 'Legacy character', 'emotion', 'scene', 'S1',
                             1, 0, 1, 0, 1, 'main', '{}', ?, ?)""",
                (new_id("ohlc"), project_id, now, now),
            )
            self.assertIsNone(connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='blueprint_jobs'"
            ).fetchone())
        repo = Repository(legacy)
        repo.ledger.append(project_id, "legacy.fixture", {"version": 4}, "test")
        before_ledger = repo.ledger.verify(project_id)

        upgraded = Database(legacy_path)
        upgraded.initialize()
        with upgraded.connect() as connection:
            self.assertEqual(
                connection.execute("SELECT title FROM artifacts WHERE id=?", (artifact_id,)).fetchone()["title"],
                "Legacy outline",
            )
            self.assertEqual(
                connection.execute("SELECT content FROM artifact_versions WHERE id=?", (version_id,)).fetchone()["content"],
                "legacy content",
            )
            self.assertEqual(
                connection.execute("SELECT name FROM project_workflows WHERE id=?", (workflow_id,)).fetchone()["name"],
                "Legacy workflow",
            )
            self.assertEqual(connection.execute("SELECT COUNT(*) AS n FROM ohlc_points").fetchone()["n"], 1)
        self.assertEqual(upgraded.schema_version(), 5)
        self.assertEqual(Repository(upgraded).ledger.verify(project_id), before_ledger)


if __name__ == "__main__":
    unittest.main()
