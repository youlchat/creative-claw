from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from creative_claw.api import create_app


class WorkflowApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.app = create_app(self.root / "creative-claw.db")
        self.client = self.app.test_client()
        response = self.client.post(
            "/v1/projects",
            json={
                "id": "prj_phase2_api",
                "name": "顾遥长篇",
                "root_path": str(self.root / "project"),
            },
        )
        self.assertEqual(response.status_code, 201)
        self.project_id = response.get_json()["id"]

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _create_artifact(self, artifact_type: str, title: str, stage_id: str) -> dict:
        response = self.client.post(
            f"/v1/projects/{self.project_id}/artifacts",
            json={
                "artifact_type": artifact_type,
                "title": title,
                "stage_id": stage_id,
            },
        )
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        return response.get_json()

    def _save(self, artifact_id: str, content: str, expected: str | None) -> dict:
        response = self.client.post(
            f"/v1/projects/{self.project_id}/artifacts/{artifact_id}/versions",
            json={
                "content": content,
                "expected_current_version_id": expected,
                "change_summary": "正式保存",
            },
        )
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        return response.get_json()

    def test_no_model_workflow_api_propagates_impacts_and_keeps_utf8(self) -> None:
        templates_response = self.client.get("/v1/workflow-templates")
        self.assertEqual(templates_response.status_code, 200)
        self.assertIn("长篇小说标准流程", templates_response.get_data(as_text=True))

        workflow_response = self.client.post(
            f"/v1/projects/{self.project_id}/workflow", json={"template_key": "novel"}
        )
        self.assertEqual(workflow_response.status_code, 201)
        workflow = workflow_response.get_json()
        self.assertEqual(len(workflow["stages"]), 13)

        volume_response = self.client.post(
            f"/v1/projects/{self.project_id}/production-units",
            json={"unit_type": "volume", "title": "第一卷", "position": 1},
        )
        self.assertEqual(volume_response.status_code, 201)
        chapter_response = self.client.post(
            f"/v1/projects/{self.project_id}/production-units",
            json={
                "unit_type": "chapter",
                "title": "第一章",
                "parent_id": volume_response.get_json()["id"],
                "position": 1,
            },
        )
        self.assertEqual(chapter_response.status_code, 201)

        bible = self._create_artifact(
            "story_bible", "顾遥故事圣经", workflow["stages"][2]["id"]
        )
        manuscript = self._create_artifact(
            "manuscript", "第一章正文", workflow["stages"][7]["id"]
        )
        bible_v1 = self._save(bible["id"], "顾遥不会离开故乡。", None)["version"]
        manuscript_v1 = self._save(
            manuscript["id"], "顾遥留在故乡。", None
        )["version"]
        for status in ("ready_for_review", "approved"):
            transition = self.client.post(
                f"/v1/projects/{self.project_id}/artifacts/{manuscript['id']}/transition",
                json={"status": status},
            )
            self.assertEqual(transition.status_code, 200, transition.get_data(as_text=True))

        dependency_response = self.client.post(
            f"/v1/projects/{self.project_id}/artifact-dependencies",
            json={
                "upstream_artifact_id": bible["id"],
                "downstream_artifact_id": manuscript["id"],
                "dependency_type": "constrains",
            },
        )
        self.assertEqual(dependency_response.status_code, 201)
        review_response = self.client.post(
            f"/v1/projects/{self.project_id}/reviews",
            json={
                "artifact_id": manuscript["id"],
                "review_type": "continuity",
                "input_version_id": manuscript_v1["id"],
                "summary": "连续性通过",
            },
        )
        self.assertEqual(review_response.status_code, 201)
        review = review_response.get_json()

        bible_v2 = self._save(
            bible["id"], "顾遥必须在第一幕离开故乡。", bible_v1["id"]
        )
        self.assertEqual(bible_v2["sync"]["stale_review_ids"], [review["id"]])
        self.assertEqual(len(bible_v2["sync"]["impact_ids"]), 1)

        impacts_response = self.client.get(
            f"/v1/projects/{self.project_id}/impacts?status=open"
        )
        self.assertEqual(impacts_response.status_code, 200)
        impacts = impacts_response.get_json()["impacts"]
        self.assertEqual(len(impacts), 1)
        self.assertEqual(
            impacts[0]["dependency_path"], [bible["id"], manuscript["id"]]
        )
        self.assertIn("summary", impacts[0])
        self.assertEqual(
            impacts[0]["summary"],
            "顾遥故事圣经 的变更“正式保存”影响 第一章正文",
        )
        self.assertIn("顾遥故事圣经", impacts_response.get_data(as_text=True))
        artifact_response = self.client.get(
            f"/v1/projects/{self.project_id}/artifacts/{manuscript['id']}"
        )
        self.assertEqual(artifact_response.get_json()["status"], "stale")
        self.assertIn("第一章正文", artifact_response.get_data(as_text=True))
        ledger = self.client.get(
            f"/v1/projects/{self.project_id}/ledger/verify"
        ).get_json()
        self.assertTrue(ledger["valid"])
        structured = self.client.get(
            f"/v1/projects/{self.project_id}/stats"
        ).get_json()["structured"]
        expected_counts = {
            "project_workflows": 1,
            "workflow_stages": 13,
            "production_units": 2,
            "artifacts": 2,
            "artifact_versions": 3,
            "artifact_dependencies": 1,
            "reviews": 1,
            "impact_records": 1,
        }
        self.assertTrue(
            expected_counts.keys() <= structured.keys(),
            f"Missing structured counters: {sorted(expected_counts.keys() - structured.keys())}",
        )
        self.assertEqual(
            {key: structured[key] for key in expected_counts},
            expected_counts,
        )
        self.assertIn("timeline_events", structured)

    def test_stale_version_returns_409_without_partial_write(self) -> None:
        workflow_response = self.client.post(
            f"/v1/projects/{self.project_id}/workflow", json={"template_key": "novel"}
        )
        self.assertEqual(workflow_response.status_code, 201)
        workflow = workflow_response.get_json()
        artifact = self._create_artifact(
            "manuscript", "冲突测试正文", workflow["stages"][7]["id"]
        )
        first = self._save(artifact["id"], "版本一", None)["version"]
        second = self._save(artifact["id"], "版本二", first["id"])["version"]
        ledger_before = self.client.get(
            f"/v1/projects/{self.project_id}/ledger/verify"
        ).get_json()

        conflict = self.client.post(
            f"/v1/projects/{self.project_id}/artifacts/{artifact['id']}/versions",
            json={
                "content": "不应写入",
                "expected_current_version_id": first["id"],
                "change_summary": "过期写入",
            },
        )

        self.assertEqual(conflict.status_code, 409)
        self.assertIn("version conflict", conflict.get_json()["error"].lower())
        current = self.client.get(
            f"/v1/projects/{self.project_id}/artifacts/{artifact['id']}"
        ).get_json()
        versions = self.client.get(
            f"/v1/projects/{self.project_id}/artifacts/{artifact['id']}/versions"
        ).get_json()["versions"]
        self.assertEqual(current["current_version_id"], second["id"])
        self.assertEqual([row["content"] for row in versions], ["版本一", "版本二"])
        self.assertEqual(
            self.client.get(
                f"/v1/projects/{self.project_id}/ledger/verify"
            ).get_json(),
            ledger_before,
        )

    def test_phase2_errors_remain_json_and_unknown_projects_are_404(self) -> None:
        malformed = self.client.post(
            f"/v1/projects/{self.project_id}/workflow",
            data="{",
            content_type="application/json",
        )
        self.assertEqual(malformed.status_code, 400)
        self.assertTrue(malformed.is_json)
        self.assertIn("error", malformed.get_json())

        array_body = self.client.post(
            f"/v1/projects/{self.project_id}/workflow", json=[]
        )
        self.assertEqual(array_body.status_code, 400)
        self.assertTrue(array_body.is_json)
        self.assertIn("JSON body must be an object", array_body.get_json()["error"])

        missing_impacts = self.client.get("/v1/projects/prj_missing/impacts")
        self.assertEqual(missing_impacts.status_code, 404)
        self.assertTrue(missing_impacts.is_json)

        workflow = self.client.post(
            f"/v1/projects/{self.project_id}/workflow", json={"template_key": "novel"}
        )
        self.assertEqual(workflow.status_code, 201)
        missing_parent = self.client.post(
            f"/v1/projects/{self.project_id}/production-units",
            json={
                "unit_type": "chapter",
                "title": "缺失父单元",
                "parent_id": "unit_missing",
            },
        )
        self.assertEqual(missing_parent.status_code, 404)
        self.assertTrue(missing_parent.is_json)

        missing_dependency = self.client.post(
            f"/v1/projects/{self.project_id}/artifact-dependencies",
            json={
                "upstream_artifact_id": "art_missing_a",
                "downstream_artifact_id": "art_missing_b",
                "dependency_type": "requires",
            },
        )
        self.assertEqual(missing_dependency.status_code, 404)
        self.assertTrue(missing_dependency.is_json)


if __name__ == "__main__":
    unittest.main()
