from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from creative_claw.api import create_app
from creative_claw.blueprint_agents import DeterministicAgentRegistry
from creative_claw.blueprint_models import BLUEPRINT_DIMENSIONS


class BlueprintApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.registry = DeterministicAgentRegistry()
        self.app = create_app(
            self.root / "creative-claw.db",
            blueprint_registry=self.registry,
            run_blueprint_jobs_inline=True,
        )
        self.client = self.app.test_client()
        created = self.client.post(
            "/v1/projects",
            json={"id": "prj_blueprint_api", "name": "蓝图项目", "root_path": str(self.root / "project")},
        )
        self.assertEqual(created.status_code, 201)
        self.project_id = created.get_json()["id"]

    def tearDown(self) -> None:
        executor = self.app.extensions.get("blueprint_executor")
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=True)
        self.temp_dir.cleanup()

    def test_short_text_http_flow_to_accepted_candidate(self) -> None:
        reference_response = self.client.post(
            f"/v1/projects/{self.project_id}/blueprint-jobs/reference",
            json={
                "title": "参考文本",
                "text": "第一章\n守门人舍弃旧钥匙，因此再也无法回家。",
                "rights_basis": "research_reference",
            },
        )
        self.assertEqual(reference_response.status_code, 201, reference_response.get_data(as_text=True))
        reference_job = reference_response.get_json()
        self.assertNotIn("text", reference_job.get("input", {}))
        reference_id = reference_job["output_artifact_id"]
        blueprint_response = self.client.get(
            f"/v1/projects/{self.project_id}/reference-blueprints/{reference_id}?include_evidence=1"
        )
        self.assertEqual(blueprint_response.status_code, 200)
        self.assertIn("创作机制蓝图", blueprint_response.get_data(as_text=True))
        self.assertTrue(blueprint_response.get_json()["evidence"])
        self.assertNotIn("quote", blueprint_response.get_json()["evidence"][0])

        setting_response = self.client.post(
            f"/v1/projects/{self.project_id}/target-settings",
            json={"text": "云城修补师要阻止永夜，失败会失去自己的名字。", "overrides": {"media_type": "novel"}},
        )
        self.assertEqual(setting_response.status_code, 201)
        setting = setting_response.get_json()
        setting_confirm = self.client.post(
            f"/v1/projects/{self.project_id}/target-settings/{setting['artifact']['id']}/confirm",
            json={"expected_current_version_id": setting["version"]["id"],
                  "structured": setting["structured"]},
        )
        self.assertEqual(setting_confirm.status_code, 200, setting_confirm.get_data(as_text=True))
        setting = setting_confirm.get_json()
        migration_response = self.client.post(
            f"/v1/projects/{self.project_id}/blueprint-jobs/migration",
            json={"reference_blueprint_id": reference_id, "target_setting_id": setting["artifact"]["id"]},
        )
        self.assertEqual(migration_response.status_code, 201, migration_response.get_data(as_text=True))
        migration = migration_response.get_json()
        target_id = migration["output_artifact_id"]
        target = self.client.get(
            f"/v1/projects/{self.project_id}/target-blueprints/{target_id}"
        ).get_json()
        confirm = self.client.post(
            f"/v1/projects/{self.project_id}/target-blueprints/{target_id}/confirm",
            json={"expected_current_version_id": target["version"]["id"]},
        )
        self.assertEqual(confirm.status_code, 200, confirm.get_data(as_text=True))

        unit = self.client.post(
            f"/v1/projects/{self.project_id}/production-units",
            json={"unit_type": "scene", "title": "第一场"},
        ).get_json()
        artifact = self.client.post(
            f"/v1/projects/{self.project_id}/artifacts",
            json={"artifact_type": "manuscript", "title": "第一场正文", "unit_id": unit["id"]},
        ).get_json()
        candidate_response = self.client.post(
            f"/v1/projects/{self.project_id}/draft-candidates",
            json={"target_blueprint_id": target_id, "unit_id": unit["id"], "artifact_id": artifact["id"]},
        )
        self.assertEqual(candidate_response.status_code, 201, candidate_response.get_data(as_text=True))
        candidate = candidate_response.get_json()
        self.assertEqual(candidate["status"], "passed")
        accepted_response = self.client.post(
            f"/v1/projects/{self.project_id}/draft-candidates/{candidate['id']}/accept",
            json={"expected_current_version_id": None},
        )
        self.assertEqual(accepted_response.status_code, 200, accepted_response.get_data(as_text=True))
        self.assertEqual(accepted_response.get_json()["status"], "accepted")
        stats = self.client.get(f"/v1/projects/{self.project_id}/stats").get_json()
        self.assertGreaterEqual(stats["structured"]["blueprint_jobs"], 3)
        self.assertGreaterEqual(stats["structured"]["draft_candidates"], 1)

    def test_background_long_job_pause_resume_cancel_and_json_errors(self) -> None:
        executor = self.app.extensions.get("blueprint_executor")
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=True)
        self.registry = DeterministicAgentRegistry(delay_seconds=0.03)
        self.app = create_app(
            self.root / "background.db",
            blueprint_registry=self.registry,
            run_blueprint_jobs_inline=False,
        )
        self.client = self.app.test_client()
        project = self.client.post(
            "/v1/projects",
            json={"id": "prj_background", "name": "长篇", "root_path": str(self.root / "background")},
        ).get_json()
        text = "\n".join(f"第{i}章\n" + "潮汐推动人物改变。" * 900 for i in range(1, 5))
        response = self.client.post(
            f"/v1/projects/{project['id']}/blueprint-jobs/reference",
            json={"title": "长篇参考", "text": text, "rights_basis": "owned"},
        )
        self.assertEqual(response.status_code, 202, response.get_data(as_text=True))
        job = response.get_json()
        paused = self.client.post(
            f"/v1/projects/{project['id']}/blueprint-jobs/{job['id']}/pause", json={}
        )
        self.assertEqual(paused.status_code, 200)
        calls = len(self.registry.calls)
        time.sleep(0.15)
        self.assertLessEqual(len(self.registry.calls) - calls, 1)
        status = self.client.get(
            f"/v1/projects/{project['id']}/blueprint-jobs/{job['id']}"
        )
        self.assertEqual(status.status_code, 200)
        self.assertNotIn("text", status.get_json().get("input", {}))
        self.assertEqual(
            set(status.get_json()),
            {"id", "project_id", "job_type", "status", "desired_state", "input", "rights_basis",
             "source_document_id", "source_version_id", "output_artifact_id", "progress", "error",
             "created_at", "updated_at"},
        )
        self.assertNotIn("checkpoint", status.get_json())
        resumed = self.client.post(
            f"/v1/projects/{project['id']}/blueprint-jobs/{job['id']}/resume", json={}
        )
        self.assertIn(resumed.status_code, {200, 202})
        cancelled = self.client.post(
            f"/v1/projects/{project['id']}/blueprint-jobs/{job['id']}/cancel", json={}
        )
        self.assertEqual(cancelled.status_code, 200)
        self.assertEqual(cancelled.get_json()["status"], "cancelled")

        unknown = self.client.get(f"/v1/projects/{project['id']}/blueprint-jobs/missing")
        array_json = self.client.post(
            f"/v1/projects/{project['id']}/target-settings", json=["not", "object"]
        )
        malformed = self.client.post(
            f"/v1/projects/{project['id']}/target-settings",
            data="{bad",
            content_type="application/json",
        )
        self.assertEqual(unknown.status_code, 404)
        self.assertEqual(array_json.status_code, 400)
        self.assertEqual(malformed.status_code, 400)

    def test_manual_reference_and_target_blueprint_endpoints_work_without_model_calls(self) -> None:
        dimensions = {
            name: {"state": "not_observed", "value": None, "confidence": 1.0, "evidence_refs": []}
            for name in BLUEPRINT_DIMENSIONS
        }
        reference_response = self.client.post(
            f"/v1/projects/{self.project_id}/reference-blueprints/manual",
            json={"title": "手工参考蓝图", "nodes": [
                {"stable_key": "work", "node_type": "work", "title": "手工参考", "dimensions": dimensions}
            ]},
        )
        self.assertEqual(reference_response.status_code, 201, reference_response.get_data(as_text=True))
        reference = reference_response.get_json()
        complete_setting = {
            "genre": "manual", "audience": "general", "media_type": "novel", "scale": "short",
            "world_rules": [], "characters": [], "character_goals": [], "core_conflict": "manual",
            "stakes": "manual", "themes": [], "narrative_preferences": {}, "must_include": [],
            "must_avoid": [], "ending_direction": "manual",
        }
        setting_response = self.client.post(
            f"/v1/projects/{self.project_id}/target-settings",
            json={"text": "手工设定", "overrides": complete_setting},
        )
        self.assertEqual(setting_response.status_code, 201)
        setting = setting_response.get_json()
        setting = self.client.post(
            f"/v1/projects/{self.project_id}/target-settings/{setting['artifact']['id']}/confirm",
            json={"expected_current_version_id": setting["version"]["id"],
                  "structured": setting["structured"]},
        ).get_json()
        target_response = self.client.post(
            f"/v1/projects/{self.project_id}/target-blueprints/manual",
            json={"title": "手工目标蓝图", "target_setting_id": setting["artifact"]["id"],
                  "reference_blueprint_id": reference["artifact"]["id"], "nodes": [
                      {"stable_key": "work", "node_type": "work", "title": "手工目标",
                       "dimensions": dimensions}
                  ]},
        )
        self.assertEqual(target_response.status_code, 201, target_response.get_data(as_text=True))
        self.assertEqual(target_response.get_json()["artifact"]["attrs"]["creation_mode"], "manual")


if __name__ == "__main__":
    unittest.main()
