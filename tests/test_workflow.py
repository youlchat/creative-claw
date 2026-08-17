from __future__ import annotations

import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from creative_claw.db import Database
from creative_claw.repository import Repository
from creative_claw.util import json_dumps, new_id, utc_now
from creative_claw.workflow import WorkflowService


class WorkflowServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.database = Database(self.root / "creative-claw.db")
        self.database.initialize()
        self.repository = Repository(self.database)
        self.project_a = self.repository.create_project("长篇项目", self.root / "novel")
        self.project_b = self.repository.create_project("短剧项目", self.root / "drama")
        self.service = WorkflowService(self.database)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_instantiates_both_templates_and_validates_unit_parent(self) -> None:
        templates = self.service.list_templates()
        novel = self.service.instantiate_workflow(self.project_a["id"], "novel")
        drama = self.service.instantiate_workflow(
            self.project_b["id"], "vertical_short_drama"
        )

        self.assertEqual(
            {item["template_key"] for item in templates},
            {"novel", "vertical_short_drama"},
        )
        self.assertEqual(novel["media_type"], "novel")
        self.assertEqual(len(novel["stages"]), 13)
        self.assertEqual([stage["position"] for stage in novel["stages"]], list(range(1, 14)))
        self.assertEqual(drama["media_type"], "vertical_short_drama")
        self.assertEqual(len(drama["stages"]), 16)
        self.assertEqual(novel["status_counts"], {"not_started": 13})

        with self.assertRaisesRegex(ValueError, "already has a workflow"):
            self.service.instantiate_workflow(self.project_a["id"], "novel")

        volume = self.service.create_production_unit(
            self.project_a["id"], "volume", "第一卷", position=1
        )
        chapter = self.service.create_production_unit(
            self.project_a["id"],
            "chapter",
            "第一章",
            parent_id=volume["id"],
            position=1,
        )
        self.assertEqual(chapter["parent_id"], volume["id"])
        self.assertEqual(chapter["workflow_id"], novel["id"])

        with self.assertRaisesRegex(ValueError, "same project and branch"):
            self.service.create_production_unit(
                self.project_b["id"], "scene", "越界场景", parent_id=volume["id"]
            )
        with self.assertRaisesRegex(ValueError, "Unsupported production unit type"):
            self.service.create_production_unit(
                self.project_a["id"], "shot", "不支持的镜头"
            )

        self.assertTrue(self.repository.ledger.verify(self.project_a["id"])["valid"])

    def test_stage_transitions_require_valid_path_artifacts_and_skip_reason(self) -> None:
        workflow = self.service.instantiate_workflow(self.project_a["id"], "novel")
        first_stage = workflow["stages"][0]
        second_stage = workflow["stages"][1]

        in_progress = self.service.transition_stage(
            self.project_a["id"], first_stage["id"], "in_progress"
        )
        self.assertEqual(in_progress["status"], "in_progress")
        with self.assertRaisesRegex(ValueError, "Invalid stage transition"):
            self.service.transition_stage(
                self.project_a["id"], first_stage["id"], "passed"
            )

        pending = self.service.transition_stage(
            self.project_a["id"], first_stage["id"], "pending_review"
        )
        self.assertEqual(pending["status"], "pending_review")
        with self.assertRaisesRegex(ValueError, "Required artifacts not approved"):
            self.service.transition_stage(
                self.project_a["id"], first_stage["id"], "passed"
            )

        required_type = first_stage["completion_criteria"]["required_artifact_types"][0]
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO artifacts(
                    id, project_id, workflow_stage_id, artifact_type, title,
                    status, branch, attrs_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'approved', 'main', ?, ?, ?)
                """,
                (
                    new_id("art"),
                    self.project_a["id"],
                    first_stage["id"],
                    required_type,
                    "已批准交付物",
                    json_dumps({}),
                    now,
                    now,
                ),
            )

        passed = self.service.transition_stage(
            self.project_a["id"], first_stage["id"], "passed"
        )
        locked = self.service.transition_stage(
            self.project_a["id"], first_stage["id"], "locked"
        )
        self.assertEqual(passed["status"], "passed")
        self.assertEqual(locked["status"], "locked")
        with self.assertRaisesRegex(ValueError, "Invalid stage transition"):
            self.service.transition_stage(
                self.project_a["id"], first_stage["id"], "stale"
            )

        with self.assertRaisesRegex(ValueError, "Skip reason is required"):
            self.service.transition_stage(
                self.project_a["id"], second_stage["id"], "skipped"
            )
        skipped = self.service.transition_stage(
            self.project_a["id"],
            second_stage["id"],
            "skipped",
            exception_reason="已有可信研究资料",
        )
        self.assertEqual(skipped["exception_reason"], "已有可信研究资料")
        self.assertTrue(self.repository.ledger.verify(self.project_a["id"])["valid"])

    def test_stage_transition_rolls_back_when_audit_write_fails(self) -> None:
        workflow = self.service.instantiate_workflow(self.project_a["id"], "novel")
        stage_id = workflow["stages"][0]["id"]

        def fail_audit(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
            raise RuntimeError("simulated audit failure")

        self.service.ledger.append = fail_audit
        with self.assertRaisesRegex(RuntimeError, "simulated audit failure"):
            self.service.transition_stage(
                self.project_a["id"], stage_id, "in_progress"
            )

        stage = next(
            item
            for item in self.service.get_project_workflow(self.project_a["id"])[
                "stages"
            ]
            if item["id"] == stage_id
        )
        self.assertEqual(stage["status"], "not_started")

    def test_workflow_and_unit_creation_roll_back_when_audit_write_fails(self) -> None:
        def fail_audit(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
            raise RuntimeError("simulated audit failure")

        self.service.ledger.append = fail_audit
        with self.assertRaisesRegex(RuntimeError, "simulated audit failure"):
            self.service.instantiate_workflow(self.project_a["id"], "novel")
        with self.database.connect() as connection:
            workflow_count = connection.execute(
                "SELECT COUNT(*) AS n FROM project_workflows WHERE project_id=?",
                (self.project_a["id"],),
            ).fetchone()["n"]
        self.assertEqual(workflow_count, 0)

        self.service = WorkflowService(self.database)
        self.service.instantiate_workflow(self.project_a["id"], "novel")
        self.service.ledger.append = fail_audit
        with self.assertRaisesRegex(RuntimeError, "simulated audit failure"):
            self.service.create_production_unit(
                self.project_a["id"], "chapter", "不会留下的章节"
            )
        with self.database.connect() as connection:
            unit_count = connection.execute(
                "SELECT COUNT(*) AS n FROM production_units WHERE project_id=?",
                (self.project_a["id"],),
            ).fetchone()["n"]
        self.assertEqual(unit_count, 0)

    def test_artifact_versions_are_immutable_and_conflicts_roll_back(self) -> None:
        artifact = self.service.create_artifact(
            self.project_a["id"], "manuscript", "第一章正文"
        )
        first = self.service.save_artifact_version(
            self.project_a["id"],
            artifact["id"],
            "第一版正文",
            expected_current_version_id=None,
            change_summary="完成初稿",
        )
        self.assertEqual(first["artifact"]["status"], "draft")
        self.service.transition_artifact_status(
            self.project_a["id"], artifact["id"], "ready_for_review"
        )
        self.service.transition_artifact_status(
            self.project_a["id"], artifact["id"], "approved"
        )
        second = self.service.save_artifact_version(
            self.project_a["id"],
            artifact["id"],
            "第二版正文",
            expected_current_version_id=first["version"]["id"],
            change_summary="修订转折",
        )
        ledger_before_conflict = self.repository.ledger.verify(self.project_a["id"])

        with self.assertRaisesRegex(ValueError, "version conflict"):
            self.service.save_artifact_version(
                self.project_a["id"],
                artifact["id"],
                "丢失更新",
                expected_current_version_id=first["version"]["id"],
                change_summary="基于旧版本保存",
            )

        versions = self.service.list_artifact_versions(
            self.project_a["id"], artifact["id"]
        )
        self.assertEqual([row["content"] for row in versions], ["第一版正文", "第二版正文"])
        self.assertEqual(
            self.service.get_artifact(self.project_a["id"], artifact["id"])[
                "current_version_id"
            ],
            second["version"]["id"],
        )
        self.assertEqual(
            self.repository.ledger.verify(self.project_a["id"]), ledger_before_conflict
        )

        self.service.transition_artifact_status(
            self.project_a["id"], artifact["id"], "ready_for_review"
        )
        self.service.transition_artifact_status(
            self.project_a["id"], artifact["id"], "approved"
        )
        self.service.transition_artifact_status(
            self.project_a["id"], artifact["id"], "locked"
        )
        with self.assertRaisesRegex(ValueError, "locked artifact"):
            self.service.save_artifact_version(
                self.project_a["id"],
                artifact["id"],
                "锁定后修改",
                expected_current_version_id=second["version"]["id"],
                change_summary="不应成功",
            )

    def test_concurrent_saves_return_one_conflict_without_sqlite_error(self) -> None:
        for attempt in range(5):
            artifact = self.service.create_artifact(
                self.project_a["id"], "manuscript", f"并发正文 {attempt}"
            )
            first = self.service.save_artifact_version(
                self.project_a["id"],
                artifact["id"],
                "基础版本",
                expected_current_version_id=None,
                change_summary="建立基础版本",
            )["version"]
            barrier = threading.Barrier(2)

            def save_concurrently(label: str) -> str:
                service = WorkflowService(self.database)
                barrier.wait()
                try:
                    service.save_artifact_version(
                        self.project_a["id"],
                        artifact["id"],
                        label,
                        expected_current_version_id=first["id"],
                        change_summary=f"并发保存 {label}",
                    )
                    return "saved"
                except ValueError as error:
                    if "version conflict" in str(error).lower():
                        return "conflict"
                    return type(error).__name__
                except Exception as error:  # noqa: BLE001
                    return type(error).__name__

            with ThreadPoolExecutor(max_workers=2) as executor:
                outcomes = list(executor.map(save_concurrently, ("候选甲", "候选乙")))
            self.assertEqual(
                sorted(outcomes),
                ["conflict", "saved"],
                f"attempt {attempt + 1}: {outcomes}",
            )

    def test_upstream_version_stales_recursive_reviews_and_records_paths(self) -> None:
        bible = self.service.create_artifact(
            self.project_a["id"], "story_bible", "故事圣经"
        )
        outline = self.service.create_artifact(
            self.project_a["id"], "book_outline", "全书大纲"
        )
        manuscript = self.service.create_artifact(
            self.project_a["id"], "manuscript", "第一章正文"
        )
        bible_v1 = self.service.save_artifact_version(
            self.project_a["id"],
            bible["id"],
            "主角拒绝离乡。",
            expected_current_version_id=None,
            change_summary="建立正典",
        )["version"]
        outline_v1 = self.service.save_artifact_version(
            self.project_a["id"],
            outline["id"],
            "第一幕发生在故乡。",
            expected_current_version_id=None,
            change_summary="完成大纲",
        )["version"]
        manuscript_v1 = self.service.save_artifact_version(
            self.project_a["id"],
            manuscript["id"],
            "她留在故乡。",
            expected_current_version_id=None,
            change_summary="完成正文",
        )["version"]
        for artifact_id in (outline["id"], manuscript["id"]):
            self.service.transition_artifact_status(
                self.project_a["id"], artifact_id, "ready_for_review"
            )
            self.service.transition_artifact_status(
                self.project_a["id"], artifact_id, "approved"
            )
        outline_review = self.service.create_review(
            self.project_a["id"], outline["id"], "structure", outline_v1["id"]
        )
        manuscript_review = self.service.create_review(
            self.project_a["id"], manuscript["id"], "continuity", manuscript_v1["id"]
        )
        self.service.add_dependency(
            self.project_a["id"], bible["id"], outline["id"], "constrains"
        )
        self.service.add_dependency(
            self.project_a["id"], outline["id"], manuscript["id"], "derives_from"
        )

        result = self.service.save_artifact_version(
            self.project_a["id"],
            bible["id"],
            "主角必须在第一幕离乡。",
            expected_current_version_id=bible_v1["id"],
            change_summary="改变第一幕正典",
        )

        self.assertEqual(
            set(result["sync"]["stale_review_ids"]),
            {outline_review["id"], manuscript_review["id"]},
        )
        self.assertEqual(len(result["sync"]["impact_ids"]), 2)
        self.assertEqual(
            self.service.get_artifact(self.project_a["id"], outline["id"])["status"],
            "stale",
        )
        self.assertEqual(
            self.service.get_artifact(self.project_a["id"], manuscript["id"])[
                "status"
            ],
            "stale",
        )
        self.assertEqual(
            self.service.list_artifact_versions(self.project_a["id"], outline["id"])[0][
                "content"
            ],
            "第一幕发生在故乡。",
        )
        impacts = self.service.list_impacts(self.project_a["id"], status="open")
        paths = {row["affected_artifact_id"]: row["dependency_path"] for row in impacts}
        self.assertEqual(paths[outline["id"]], [bible["id"], outline["id"]])
        self.assertEqual(
            paths[manuscript["id"]], [bible["id"], outline["id"], manuscript["id"]]
        )
        with self.database.connect() as connection:
            review_rows = connection.execute(
                "SELECT id, status, stale_at FROM reviews WHERE project_id=? ORDER BY id",
                (self.project_a["id"],),
            ).fetchall()
        self.assertEqual({row["status"] for row in review_rows}, {"stale"})
        self.assertTrue(all(row["stale_at"] for row in review_rows))
        self.assertTrue(self.repository.ledger.verify(self.project_a["id"])["valid"])

    def test_dependencies_reject_cross_project_self_edges_and_cycles(self) -> None:
        first = self.service.create_artifact(
            self.project_a["id"], "story_bible", "上游"
        )
        second = self.service.create_artifact(
            self.project_a["id"], "book_outline", "中游"
        )
        third = self.service.create_artifact(
            self.project_a["id"], "manuscript", "下游"
        )
        foreign = self.service.create_artifact(
            self.project_b["id"], "manuscript", "其他项目"
        )
        with self.assertRaisesRegex(ValueError, "itself"):
            self.service.add_dependency(
                self.project_a["id"], first["id"], first["id"], "requires"
            )
        with self.assertRaisesRegex(ValueError, "same project"):
            self.service.add_dependency(
                self.project_a["id"], first["id"], foreign["id"], "requires"
            )
        self.service.add_dependency(
            self.project_a["id"], first["id"], second["id"], "requires"
        )
        self.service.add_dependency(
            self.project_a["id"], second["id"], third["id"], "requires"
        )
        with self.assertRaisesRegex(ValueError, "cycle"):
            self.service.add_dependency(
                self.project_a["id"], third["id"], first["id"], "requires"
            )
        with self.database.connect() as connection:
            count = connection.execute(
                "SELECT COUNT(*) AS n FROM artifact_dependencies WHERE project_id=?",
                (self.project_a["id"],),
            ).fetchone()["n"]
        self.assertEqual(count, 2)


if __name__ == "__main__":
    unittest.main()
