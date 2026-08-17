from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import creative_claw.blueprint_agents as blueprint_agents
from creative_claw.blueprint_agents import (
    AgentResult,
    AgentTask,
    DeterministicAgentRegistry,
    OpenAICompatibleBlueprintAgent,
)
from creative_claw.blueprint_service import BlueprintService
from creative_claw.db import Database
from creative_claw.repository import Repository


class BlueprintAgentContractTests(unittest.TestCase):
    def test_real_blueprint_agent_strips_reasoning_before_fenced_json(self) -> None:
        class ReasoningResponse:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict:
                return {
                    "choices": [
                        {
                            "message": {
                                "content": (
                                    "<think>private reasoning</think>\n"
                                    "```json\n"
                                    '{"nodes": [], "evidence": [], "confidence": 1, "warnings": []}'
                                    "\n```"
                                )
                            }
                        }
                    ]
                }

        task = AgentTask(
            project_id="project",
            job_id="job",
            batch_id="batch",
            source_version_id=None,
            context={"text": "public-domain reference"},
            allowed_context_types=("reference_text",),
            prompt_version="prompt-v1",
            idempotency_key="attempt-1",
        )
        with patch.dict(
            os.environ,
            {
                "CREATIVE_CLAW_LLM_API_KEY": "test-key",
                "CREATIVE_CLAW_LLM_BASE_URL": "https://api.minimaxi.com/v1",
                "CREATIVE_CLAW_LLM_MODEL": "MiniMax-M3",
            },
        ):
            with patch(
                "creative_claw.blueprint_agents.requests.post",
                return_value=ReasoningResponse(),
            ):
                result = OpenAICompatibleBlueprintAgent("segmentation_agent").run(task)

        self.assertEqual(result.data["nodes"], [])
        self.assertEqual(result.evidence, [])

    def test_real_blueprint_agent_allows_slow_provider_response(self) -> None:
        class SuccessfulResponse:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict:
                return {
                    "choices": [
                        {"message": {"content": '{"nodes": [], "confidence": 1}'}}
                    ]
                }

        task = AgentTask(
            project_id="project",
            job_id="job",
            batch_id="batch",
            source_version_id=None,
            context={"text": "public-domain reference"},
            allowed_context_types=("reference_text",),
            prompt_version="prompt-v1",
            idempotency_key="attempt-1",
        )
        with patch.dict(
            os.environ,
            {
                "CREATIVE_CLAW_LLM_API_KEY": "test-key",
                "CREATIVE_CLAW_LLM_BASE_URL": "https://api.minimaxi.com/v1",
                "CREATIVE_CLAW_LLM_MODEL": "MiniMax-M3",
            },
        ):
            with patch(
                "creative_claw.blueprint_agents.requests.post",
                return_value=SuccessfulResponse(),
            ) as post:
                result = OpenAICompatibleBlueprintAgent("segmentation_agent").run(task)

        self.assertEqual(result.data["nodes"], [])
        self.assertEqual(post.call_args.kwargs["timeout"], 300.0)

    def test_every_agent_prompt_embeds_its_machine_readable_contract(self) -> None:
        required_api = ("agent_output_contract", "build_agent_system_prompt", "validate_agent_payload")
        self.assertTrue(all(hasattr(blueprint_agents, name) for name in required_api), required_api)
        expected_field = {
            "segmentation_agent": "nodes",
            "event_causality_agent": "edges",
            "hierarchy_synthesis_agent": "nodes",
            "interpretation_conflict_agent": "interpretations",
            "target_setting_agent": "structured",
            "mechanism_mapping_agent": "mappings",
            "target_blueprint_agent": "nodes",
            "unit_planner_agent": "unit_plan",
            "draft_writer_agent": "draft",
            "continuity_review_agent": "continuity",
            "similarity_safety_agent": "verdict",
        }
        all_agents = (
            *blueprint_agents.REFERENCE_AGENT_DAG,
            "target_setting_agent",
            *blueprint_agents.MIGRATION_AGENT_DAG,
            "unit_planner_agent",
            "draft_writer_agent",
            "continuity_review_agent",
            "similarity_safety_agent",
        )
        for name in all_agents:
            with self.subTest(agent=name):
                contract = blueprint_agents.agent_output_contract(name)
                prompt = blueprint_agents.build_agent_system_prompt(name)
                parsed = json.loads(prompt.split("OUTPUT_CONTRACT_JSON=", 1)[1])
                self.assertEqual(parsed, contract)
                self.assertEqual(contract["agent"], name)
                self.assertEqual(contract["type"], "object")
                self.assertIn("evidence", contract["properties"])
                if name in blueprint_agents.REFERENCE_AGENT_DAG:
                    self.assertIn("dimensions", contract["properties"])
                if name in expected_field:
                    self.assertIn(expected_field[name], contract["properties"])
                if name in {"segmentation_agent", "hierarchy_synthesis_agent"}:
                    node_contract = contract["properties"]["nodes"]["items"]
                    self.assertIn("key_scope", node_contract["required"])
                    self.assertEqual(
                        node_contract["properties"]["key_scope"]["enum"],
                        ["batch", "global"],
                    )

    def test_event_causality_prompt_lists_every_allowed_edge_type(self) -> None:
        contract = blueprint_agents.agent_output_contract("event_causality_agent")
        edge_contract = contract["properties"]["edges"]["items"]

        self.assertEqual(
            edge_contract["properties"]["edge_type"]["enum"],
            sorted(blueprint_agents.EDGE_TYPES),
        )

    def test_validator_rejects_every_invalid_downstream_payload_shape(self) -> None:
        self.assertTrue(hasattr(blueprint_agents, "validate_agent_payload"))
        invalid = {
            "segmentation_agent": {"nodes": [{"node_type": "chapter"}]},
            "event_causality_agent": {"edges": [{"source_key": "a", "target_key": "b",
                                                    "edge_type": "invalid"}]},
            "hierarchy_synthesis_agent": {"nodes": [{"stable_key": "work", "node_type": "work"}]},
            "interpretation_conflict_agent": {"interpretations": [], "conflicts": [{
                "conflict_group_id": "g", "relation_type": "x", "interpretation_indexes": [0]}]},
            "target_setting_agent": {"structured": {"genre": "only-one-field"}},
            "mechanism_mapping_agent": {"mappings": [{"action": "copy"}]},
            "target_blueprint_agent": {"nodes": [{"stable_key": "work", "node_type": "work"}],
                                         "structural_risk": "passed"},
            "unit_planner_agent": {"unit_plan": {"goal": "g", "beats": "not-a-list"}},
            "draft_writer_agent": {"draft": ""},
            "continuity_review_agent": {"continuity": {"status": "unknown", "issues": []}},
            "similarity_safety_agent": {"verdict": {"gate_status": "unknown", "findings": []}},
        }
        for name, payload in invalid.items():
            with self.subTest(agent=name), self.assertRaisesRegex(ValueError, name):
                blueprint_agents.validate_agent_payload(name, payload, [], batch_length=10)

    def test_service_records_schema_failed_for_invalid_non_reference_agent_payload(self) -> None:
        self.assertTrue(hasattr(blueprint_agents, "validate_agent_payload"))

        class InvalidSettingAgent:
            name = "target_setting_agent"
            output_schema = "creative-claw.blueprint-agent.v1"

            def run(self, _task):
                return AgentResult(data={"structured": {"genre": "incomplete"}}, confidence=0.9)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = Database(root / "creative-claw.db")
            database.initialize()
            repository = Repository(database)
            project = repository.create_project("agent schema", root / "project")
            registry = DeterministicAgentRegistry()
            registry.register(InvalidSettingAgent())
            service = BlueprintService(database, registry)

            with self.assertRaisesRegex(ValueError, "target_setting_agent schema_failed"):
                service.create_target_setting(project["id"], "不完整设定")
            with database.connect() as connection:
                row = connection.execute(
                    "SELECT status, error_category FROM blueprint_agent_runs "
                    "WHERE project_id=? AND agent_name='target_setting_agent' ORDER BY created_at DESC LIMIT 1",
                    (project["id"],),
                ).fetchone()
            self.assertEqual((row["status"], row["error_category"]), ("schema_failed", "schema_failed"))

    def test_reference_job_retries_transient_agent_schema_failures(self) -> None:
        registry = DeterministicAgentRegistry()
        successful = registry.get("evidence_locator_agent")

        class FailTwiceEvidenceAgent:
            name = "evidence_locator_agent"
            output_schema = "creative-claw.blueprint-agent.v1"

            def __init__(self) -> None:
                self.attempts = 0

            def run(self, task):
                self.attempts += 1
                if self.attempts < 3:
                    raise ValueError("transient provider schema failure")
                return successful.run(task)

        flaky = FailTwiceEvidenceAgent()
        registry.register(flaky)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = Database(root / "creative-claw.db")
            database.initialize()
            repository = Repository(database)
            project = repository.create_project("reference retry", root / "project")
            service = BlueprintService(database, registry)

            job = service.create_reference_job(
                project["id"],
                title="Public-domain reference",
                text="A public-domain social reversal exposes every relationship.",
                rights_basis="public_domain",
                run_async=False,
            )

            runs = [
                run
                for run in service.repository.list_agent_runs(project["id"], job["id"])
                if run["agent_name"] == "evidence_locator_agent"
            ]
            self.assertEqual(job["status"], "completed")
            self.assertEqual(flaky.attempts, 3)
            self.assertEqual(
                [run["status"] for run in runs],
                ["schema_failed", "schema_failed", "completed"],
            )

    def test_non_reference_timeout_records_failed_attempt_then_retry_completes(self) -> None:
        class FailOncePlanner:
            name = "unit_planner_agent"
            output_schema = "creative-claw.blueprint-agent.v1"
            attempts = 0

            def run(self, _task):
                self.attempts += 1
                if self.attempts == 1:
                    raise TimeoutError("temporary planner timeout")
                return AgentResult(data={"unit_plan": {"goal": "retry", "beats": []}}, confidence=0.9)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = Database(root / "creative-claw.db")
            database.initialize()
            repository = Repository(database)
            project = repository.create_project("retry agent", root / "project")
            registry = DeterministicAgentRegistry()
            registry.register(FailOncePlanner())
            service = BlueprintService(database, registry)
            job = service.repository.create_job(
                project["id"], job_type="draft", input_json={}, idempotency_key="draft:retry-agent"
            )
            with self.assertRaises(TimeoutError):
                service._run_generation_agent(project["id"], job, "unit_planner_agent", {})
            completed = service._run_generation_agent(project["id"], job, "unit_planner_agent", {})
            runs = [run for run in service.repository.list_agent_runs(project["id"], job["id"])
                    if run["agent_name"] == "unit_planner_agent"]
            self.assertEqual([run["status"] for run in runs], ["retryable_failed", "completed"])
            self.assertNotEqual(runs[0]["idempotency_key"], runs[1]["idempotency_key"])
            self.assertEqual(completed["status"], "completed")

    def test_target_setting_timeout_records_failed_attempt_then_retry_completes(self) -> None:
        successful_registry = DeterministicAgentRegistry()
        successful = successful_registry.get("target_setting_agent")

        class FailOnceSettingAgent:
            name = "target_setting_agent"
            output_schema = "creative-claw.blueprint-agent.v1"
            attempts = 0

            def run(self, task):
                self.attempts += 1
                if self.attempts == 1:
                    raise TimeoutError("temporary setting timeout")
                return successful.run(task)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = Database(root / "creative-claw.db")
            database.initialize()
            repository = Repository(database)
            project = repository.create_project("retry setting", root / "project")
            registry = DeterministicAgentRegistry()
            registry.register(FailOnceSettingAgent())
            service = BlueprintService(database, registry)

            with self.assertRaises(TimeoutError):
                service.create_target_setting(project["id"], "A stable target setting.")
            result = service.create_target_setting(project["id"], "A stable target setting.")
            with database.connect() as connection:
                rows = connection.execute(
                    """SELECT status, idempotency_key FROM blueprint_agent_runs
                       WHERE project_id=? AND agent_name='target_setting_agent' ORDER BY created_at""",
                    (project["id"],),
                ).fetchall()

            self.assertEqual([row["status"] for row in rows], ["retryable_failed", "completed"])
            self.assertNotEqual(rows[0]["idempotency_key"], rows[1]["idempotency_key"])
            self.assertEqual(result["artifact"]["artifact_type"], "target_setting")


if __name__ == "__main__":
    unittest.main()
