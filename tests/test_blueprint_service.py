from __future__ import annotations

import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from creative_claw.blueprint_agents import (
    REFERENCE_AGENT_DAG,
    AgentRegistry,
    AgentResult,
    DeterministicAgentRegistry,
)
from creative_claw.blueprint_models import BLUEPRINT_DIMENSIONS
from creative_claw.blueprint_service import BlueprintService
from creative_claw.blueprint_service import ContextFirewallError, DraftContextBuilder
from creative_claw.db import Database
from creative_claw.repository import Repository
from creative_claw.workflow import WorkflowService
from creative_claw.workflow import VersionConflictError


class BlueprintServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.database = Database(self.root / "creative-claw.db")
        self.database.initialize()
        self.repository = Repository(self.database)
        self.project = self.repository.create_project("新作品", self.root / "project")
        self.registry = DeterministicAgentRegistry()
        self.service = BlueprintService(self.database, self.registry)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def reference(self, text: str = "第一章\n林岚选择留下，因此失去归途。") -> dict:
        return self.service.create_reference_job(
            self.project["id"],
            title="参考作品",
            text=text,
            rights_basis="research_reference",
            run_async=False,
        )

    def test_short_reference_publishes_evidence_backed_editable_blueprint(self) -> None:
        job = self.reference()
        blueprint = self.service.get_blueprint(
            self.project["id"], job["output_artifact_id"]
        )

        self.assertEqual(job["status"], "completed")
        self.assertEqual(blueprint["artifact"]["artifact_type"], "reference_blueprint")
        self.assertIn("work", {node["node_type"] for node in blueprint["nodes"]})
        self.assertIn("chapter", {node["node_type"] for node in blueprint["nodes"]})
        root = next(node for node in blueprint["nodes"] if node["node_type"] == "work")
        self.assertEqual(set(root["dimensions"]), set(BLUEPRINT_DIMENSIONS))
        for name, dimension in root["dimensions"].items():
            self.assertIn(dimension["state"], {"observed", "not_observed", "uncertain"})
            if dimension["state"] in {"observed", "uncertain"}:
                self.assertTrue(dimension["evidence_refs"], name)
                self.assertGreaterEqual(dimension["confidence"], 0)
        self.assertTrue(blueprint["evidence"])
        self.assertTrue(all(item["agent_run_id"] for item in blueprint["evidence"]))
        self.assertGreaterEqual(len(blueprint["interpretations"]), 2)
        self.assertTrue(blueprint["conflicts"])
        self.assertTrue(self.repository.ledger.verify(self.project["id"])["valid"])

    def test_reference_publication_persists_typed_hierarchy_edges_and_agent_interpretations(self) -> None:
        class TypedAgent:
            output_schema = "creative-claw.blueprint-agent.v1"

            def __init__(self, name, data):
                self.name = name
                self.data = data

            def run(self, task):
                text = str(task.context.get("text") or "")
                evidence = ([{"id": "typed-evidence", "start": 0, "end": 2,
                              "source_length": len(text), "confidence": 0.9}]
                            if text else [])
                data = self.data
                if self.name == "hierarchy_synthesis_agent" and not text:
                    work_node = dict(self.data["nodes"][0])
                    work_node["key_scope"] = "global"
                    data = {
                        "dimensions": {
                            name: {"state": "not_observed", "value": None,
                                   "confidence": 0.0, "evidence_refs": []}
                            for name in BLUEPRINT_DIMENSIONS
                        },
                        "nodes": [work_node],
                    }
                return AgentResult(data=data, evidence=evidence, confidence=0.9,
                                   model={"provider": "test"})

        dimensions = {
            name: {"state": "not_observed", "value": None, "confidence": 0.0,
                   "evidence_refs": []}
            for name in BLUEPRINT_DIMENSIONS
        }
        dimensions["narrative_function"] = {
            "state": "observed", "value": {"mechanism_class": "costly_choice"},
            "confidence": 0.91, "evidence_refs": ["typed-evidence"],
        }
        self.registry.register(TypedAgent("segmentation_agent", {"nodes": [
            {"stable_key": "chapter:typed", "node_type": "chapter", "title": "代理章节", "parent_key": "work"},
            {"stable_key": "scene:typed", "node_type": "scene", "title": "代理场景", "parent_key": "chapter:typed"},
        ]}))
        self.registry.register(TypedAgent("event_causality_agent", {"edges": [
            {"source_key": "chapter:typed", "target_key": "scene:typed", "edge_type": "contains",
             "attrs": {"reason": "typed"}, "confidence": 0.9}
        ]}))
        self.registry.register(TypedAgent("hierarchy_synthesis_agent", {
            "dimensions": dimensions,
            "nodes": [{"stable_key": "work", "node_type": "work", "title": "代理作品"}],
        }))
        self.registry.register(TypedAgent("interpretation_conflict_agent", {
            "interpretations": [
                {"stable_key": "chapter:typed", "dimension": "narrative_function",
                 "value": {"explanation": "代理解释甲"}, "confidence": 0.8, "conflict_group_id": "typed-group"},
                {"stable_key": "chapter:typed", "dimension": "narrative_function",
                 "value": {"explanation": "代理解释乙"}, "confidence": 0.7, "conflict_group_id": "typed-group"},
            ],
            "conflicts": [{"conflict_group_id": "typed-group", "relation_type": "mutually_exclusive",
                           "interpretation_indexes": [0, 1]}],
        }))

        job = self.reference("第一章\n代价推动选择。")
        blueprint = self.service.get_blueprint(self.project["id"], job["output_artifact_id"])

        self.assertEqual([node["title"] for node in blueprint["nodes"]],
                         ["代理作品", "代理章节", "代理场景"])
        self.assertEqual(blueprint["edges"][0]["edge_type"], "contains")
        self.assertEqual({item["value"]["explanation"] for item in blueprint["interpretations"]},
                         {"代理解释甲", "代理解释乙"})
        self.assertEqual(blueprint["conflicts"][0]["conflict_group_id"], "typed-group")

    def test_reference_publication_preserves_per_conclusion_evidence_provenance(self) -> None:
        observed_by_agent = {
            "character_function_agent": "narrative_function",
            "event_causality_agent": "causality",
            "emotion_kline_agent": "emotion_kline",
        }

        class ProvenanceAgent:
            output_schema = "creative-claw.blueprint-agent.v1"

            def __init__(self, name: str):
                self.name = name

            def run(self, task):
                dimensions = {
                    name: {"state": "not_observed", "value": None, "confidence": 0.0,
                           "evidence_refs": []}
                    for name in BLUEPRINT_DIMENSIONS
                }
                evidence = []
                dimension = observed_by_agent.get(self.name)
                if dimension:
                    dimensions[dimension] = {
                        "state": "observed",
                        "value": {"mechanism_class": "costly_choice" if dimension == "narrative_function"
                                  else "cause_effect" if dimension == "causality" else "reversal"},
                        "confidence": 0.9,
                        "evidence_refs": ["same-logical-id"],
                    }
                    evidence = [{"id": "same-logical-id", "start": 0, "end": 2,
                                 "source_length": len(task.context["text"]), "confidence": 0.9}]
                data = {"dimensions": dimensions}
                if self.name == "segmentation_agent":
                    data["nodes"] = [{"stable_key": "chapter:1", "node_type": "chapter",
                                      "title": "第一章", "parent_key": "work"}]
                if self.name == "event_causality_agent":
                    data["edges"] = []
                if self.name == "hierarchy_synthesis_agent":
                    data["nodes"] = [{"stable_key": "work", "node_type": "work", "title": "作品"}]
                if self.name == "interpretation_conflict_agent":
                    data.update({"interpretations": [], "conflicts": []})
                return AgentResult(data=data, evidence=evidence, confidence=0.9,
                                   model={"provider": "test"})

        service = BlueprintService(
            self.database, AgentRegistry([ProvenanceAgent(name) for name in REFERENCE_AGENT_DAG])
        )
        job = service.create_reference_job(
            self.project["id"], title="证据来源映射", text="第一章\n相同范围由不同代理得出不同结论。",
            rights_basis="research_reference", run_async=False,
        )
        blueprint = service.get_blueprint(
            self.project["id"], job["output_artifact_id"], include_quotes=True
        )
        root = next(node for node in blueprint["nodes"] if node["stable_key"] == "work")
        refs = {
            name: root["dimensions"][name]["evidence_refs"][0]
            for name in ("narrative_function", "causality", "emotion_kline")
        }
        evidence_by_id = {item["id"]: item for item in blueprint["evidence"]}

        self.assertEqual(len(set(refs.values())), 3)
        self.assertEqual(set(refs.values()), set(evidence_by_id))
        self.assertEqual({(item["start"], item["end"]) for item in blueprint["evidence"]}, {(0, 2)})
        self.assertEqual(len({item["agent_run_id"] for item in blueprint["evidence"]}), 3)

    def test_reference_publication_rejects_observed_dimension_with_missing_evidence(self) -> None:
        class MissingEvidenceAgent:
            name = "hierarchy_synthesis_agent"
            output_schema = "creative-claw.blueprint-agent.v1"

            def run(self, _task):
                dimensions = {
                    name: {"state": "not_observed", "value": None, "confidence": 0.0,
                           "evidence_refs": []}
                    for name in BLUEPRINT_DIMENSIONS
                }
                dimensions["narrative_function"] = {
                    "state": "observed", "value": {"mechanism_class": "costly_choice"},
                    "confidence": 0.9, "evidence_refs": ["missing-logical-evidence"],
                }
                return AgentResult(
                    data={"dimensions": dimensions,
                          "nodes": [{"stable_key": "work", "node_type": "work"}]},
                    evidence=[], confidence=0.9, model={"provider": "test"},
                )

        self.registry.register(MissingEvidenceAgent())
        job = self.reference("第一章\n缺失证据的结论不能发布。")

        self.assertEqual(job["status"], "blocked")
        self.assertIsNone(job.get("output_artifact_id"))
        failed = [run for run in self.service.repository.list_agent_runs(self.project["id"], job["id"])
                  if run["agent_name"] == "hierarchy_synthesis_agent"]
        self.assertEqual(failed[-1]["status"], "schema_failed")

    def test_reference_publication_rolls_back_all_blueprint_rows_on_injected_failure(self) -> None:
        original = self.service.repository.create_evidence

        def fail_create_evidence(*args, **kwargs):
            raise RuntimeError("injected publication failure")

        self.service.repository.create_evidence = fail_create_evidence
        with self.assertRaisesRegex(RuntimeError, "injected publication failure"):
            self.reference("第一章\n事务发布必须完整回滚。")
        self.service.repository.create_evidence = original

        with self.database.connect() as connection:
            artifacts = connection.execute(
                "SELECT COUNT(*) AS n FROM artifacts WHERE project_id=? AND artifact_type='reference_blueprint'",
                (self.project["id"],),
            ).fetchone()["n"]
            nodes = connection.execute(
                "SELECT COUNT(*) AS n FROM blueprint_nodes WHERE project_id=? AND artifact_version_id IS NOT NULL",
                (self.project["id"],),
            ).fetchone()["n"]
        self.assertEqual(artifacts, 0)
        self.assertEqual(nodes, 0)

    def test_concurrent_duplicate_reference_submission_reuses_source_and_job(self) -> None:
        text = "第一章\n并发重复提交只应建立一份来源与任务。"
        barrier = threading.Barrier(2)

        def submit_once() -> dict:
            barrier.wait()
            return self.service.create_reference_job(
                self.project["id"], title="同一参考", text=text,
                rights_basis="research_reference", run_async=True,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = list(pool.map(lambda _value: submit_once(), range(2)))

        self.assertEqual(jobs[0]["id"], jobs[1]["id"])
        with self.database.connect() as connection:
            source_count = connection.execute(
                "SELECT COUNT(*) AS n FROM artifacts WHERE project_id=? AND artifact_type='reference_source'",
                (self.project["id"],),
            ).fetchone()["n"]
            job_count = connection.execute(
                "SELECT COUNT(*) AS n FROM blueprint_jobs WHERE project_id=? AND job_type='reference'",
                (self.project["id"],),
            ).fetchone()["n"]
        self.assertEqual(source_count, 1)
        self.assertEqual(job_count, 1)

    def test_reference_submission_reuses_source_after_job_creation_failure(self) -> None:
        text = "Reference source survives a job insertion failure."
        original_create_job = self.service.repository.create_job
        attempts = 0

        def fail_first_job(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("injected job creation failure")
            return original_create_job(*args, **kwargs)

        self.service.repository.create_job = fail_first_job
        try:
            with self.assertRaisesRegex(RuntimeError, "injected job creation failure"):
                self.service.create_reference_job(
                    self.project["id"], title="Durable source", text=text,
                    rights_basis="research_reference", run_async=True,
                )
            job = self.service.create_reference_job(
                self.project["id"], title="Durable source", text=text,
                rights_basis="research_reference", run_async=True,
            )
        finally:
            self.service.repository.create_job = original_create_job

        with self.database.connect() as connection:
            counts = connection.execute(
                """SELECT
                       (SELECT COUNT(*) FROM documents WHERE project_id=?) AS documents,
                       (SELECT COUNT(*) FROM artifacts WHERE project_id=? AND artifact_type='reference_source') AS sources,
                       (SELECT COUNT(*) FROM artifact_versions av JOIN artifacts a ON a.id=av.artifact_id
                         WHERE a.project_id=? AND a.artifact_type='reference_source') AS versions,
                       (SELECT COUNT(*) FROM blueprint_jobs WHERE project_id=? AND job_type='reference') AS jobs""",
                (self.project["id"],) * 4,
            ).fetchone()
        self.assertEqual(job["status"], "pending")
        self.assertEqual(dict(counts), {"documents": 1, "sources": 1, "versions": 1, "jobs": 1})

    def test_published_and_migration_inputs_use_canonical_multibatch_keys(self) -> None:
        class ScopedSegmentationAgent:
            name = "segmentation_agent"
            output_schema = "creative-claw.blueprint-agent.v1"

            def run(self, task):
                return AgentResult(data={"nodes": [
                    {"stable_key": "chapter:1", "key_scope": "batch", "node_type": "chapter",
                     "parent_key": "volume:shared", "start": 0, "end": len(task.context["text"])},
                    {"stable_key": "scene:1", "key_scope": "batch", "node_type": "scene",
                     "parent_key": "chapter:1", "start": 0, "end": len(task.context["text"])},
                ]}, confidence=0.9, model={"provider": "test"})

        class ScopedHierarchyAgent:
            name = "hierarchy_synthesis_agent"
            output_schema = "creative-claw.blueprint-agent.v1"

            def run(self, task):
                dimensions = {
                    name: {"state": "not_observed", "value": None,
                           "confidence": 0.0, "evidence_refs": []}
                    for name in BLUEPRINT_DIMENSIONS
                }
                stage = task.context.get("synthesis_stage", "chapter")
                nodes = [
                    {"stable_key": "work", "key_scope": "global", "node_type": "work"},
                    {"stable_key": "volume:shared", "key_scope": "global",
                     "node_type": "volume", "parent_key": "work"},
                ]
                if stage == "chapter":
                    nodes.extend([
                        {"stable_key": "chapter:1", "key_scope": "batch", "node_type": "chapter",
                         "parent_key": "volume:shared"},
                        {"stable_key": "scene:1", "key_scope": "batch", "node_type": "scene",
                         "parent_key": "chapter:1"},
                        {"stable_key": "beat:1", "key_scope": "batch", "node_type": "beat",
                         "parent_key": "scene:1"},
                    ])
                return AgentResult(data={"dimensions": dimensions, "nodes": nodes},
                                   confidence=0.9, model={"provider": "test"})

        class ScopedEventAgent:
            name = "event_causality_agent"
            output_schema = "creative-claw.blueprint-agent.v1"

            def run(self, _task):
                dimensions = {
                    name: {"state": "not_observed", "value": None,
                           "confidence": 0.0, "evidence_refs": []}
                    for name in BLUEPRINT_DIMENSIONS
                }
                return AgentResult(data={"dimensions": dimensions, "edges": [
                    {"source_key": "chapter:1", "target_key": "scene:1",
                     "edge_type": "contains", "confidence": 0.9}
                ]}, confidence=0.9, model={"provider": "test"})

        self.registry.register(ScopedSegmentationAgent())
        self.registry.register(ScopedHierarchyAgent())
        self.registry.register(ScopedEventAgent())
        reference_job = self.service.create_reference_job(
            self.project["id"], title="Canonical source", text="x" * 26_000,
            rights_basis="research_reference", run_async=False,
        )
        reference = self.service.get_blueprint(
            self.project["id"], reference_job["output_artifact_id"]
        )
        published_keys = {node["stable_key"] for node in reference["nodes"]}

        self.assertTrue({f"batch:{index}:chapter:1" for index in range(3)}.issubset(published_keys))
        setting = self.service.create_target_setting(self.project["id"], "A canonical target setting.")
        setting = self.service.confirm_target_setting(
            self.project["id"], setting["artifact"]["id"],
            expected_current_version_id=setting["version"]["id"], structured=setting["structured"],
        )
        self.service.create_migration_job(
            self.project["id"], reference["artifact"]["id"], setting["artifact"]["id"]
        )
        mapping_input = next(
            item["context"] for item in self.registry.inputs
            if item["agent"] == "mechanism_mapping_agent"
        )
        self.assertEqual(
            {node["stable_key"] for node in mapping_input["abstract_reference_blueprint"]},
            published_keys,
        )

    def test_reference_submission_reuses_source_across_service_instances_without_process_lock(self) -> None:
        text = "Two service instances submit the same durable reference."
        source_hash = __import__("hashlib").sha256(text.encode("utf-8")).hexdigest()
        services = [BlueprintService(self.database, self.registry), BlueprintService(self.database, self.registry)]
        barrier = threading.Barrier(2)

        def submit(service: BlueprintService) -> dict:
            barrier.wait()
            return service._create_reference_job_locked(
                self.project["id"], clean_title="Shared source", clean_text=text,
                rights_basis="research_reference", source_hash=source_hash, run_async=True,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = list(pool.map(submit, services))

        self.assertEqual(jobs[0]["id"], jobs[1]["id"])
        with self.database.connect() as connection:
            counts = connection.execute(
                """SELECT
                       (SELECT COUNT(*) FROM documents WHERE project_id=?) AS documents,
                       (SELECT COUNT(*) FROM artifacts WHERE project_id=? AND artifact_type='reference_source') AS sources,
                       (SELECT COUNT(*) FROM artifact_versions av JOIN artifacts a ON a.id=av.artifact_id
                         WHERE a.project_id=? AND a.artifact_type='reference_source') AS versions,
                       (SELECT COUNT(*) FROM blueprint_jobs WHERE project_id=? AND job_type='reference') AS jobs""",
                (self.project["id"],) * 4,
            ).fetchone()
        self.assertEqual(dict(counts), {"documents": 1, "sources": 1, "versions": 1, "jobs": 1})

    def test_missing_automation_has_stable_error_and_manual_blueprints_remain_versionable(self) -> None:
        manual = BlueprintService(self.database, AgentRegistry())
        automatic = manual.create_reference_job(
            self.project["id"], title="无模型自动化", text="不会调用模型。",
            rights_basis="owned", run_async=False,
        )
        self.assertEqual(automatic["status"], "blocked")
        self.assertEqual(automatic["error"]["category"], "automation_unavailable")

        dimensions = {
            name: {"state": "not_observed", "value": None, "confidence": 1.0, "evidence_refs": []}
            for name in BLUEPRINT_DIMENSIONS
        }
        reference = manual.create_manual_reference_blueprint(
            self.project["id"], title="手工参考蓝图",
            nodes=[{"stable_key": "work", "node_type": "work", "title": "手工参考", "dimensions": dimensions}],
        )
        complete_setting = {
            "genre": "manual", "audience": "general", "media_type": "novel", "scale": "short",
            "world_rules": [], "characters": [], "character_goals": [], "core_conflict": "manual",
            "stakes": "manual", "themes": [], "narrative_preferences": {}, "must_include": [],
            "must_avoid": [], "ending_direction": "manual",
        }
        setting = manual.create_target_setting(
            self.project["id"], "手工设定来源", overrides=complete_setting
        )
        setting = manual.confirm_target_setting(
            self.project["id"], setting["artifact"]["id"],
            expected_current_version_id=setting["version"]["id"], structured=setting["structured"],
        )
        target = manual.create_manual_target_blueprint(
            self.project["id"], title="手工目标蓝图",
            nodes=[{"stable_key": "work", "node_type": "work", "title": "手工目标", "dimensions": dimensions}],
            target_setting_id=setting["artifact"]["id"],
            reference_blueprint_id=reference["artifact"]["id"],
        )
        confirmed = manual.confirm_target_blueprint(
            self.project["id"], target["artifact"]["id"],
            expected_current_version_id=target["version"]["id"],
        )
        saved = manual.save_blueprint_version(
            self.project["id"], reference["artifact"]["id"], reference["nodes"],
            expected_current_version_id=reference["version"]["id"], change_summary="手工第二版",
        )
        self.assertEqual(confirmed["artifact"]["attrs"]["confirmation_status"], "confirmed")
        self.assertEqual(saved["version"]["version_number"], 2)

    def test_reference_edit_versions_and_stales_migrated_dependents(self) -> None:
        reference_job = self.reference()
        reference = self.service.get_blueprint(
            self.project["id"], reference_job["output_artifact_id"]
        )
        setting = self.service.create_target_setting(
            self.project["id"], "蒸汽海岛上的修复师追查失踪潮汐，以自我记忆为代价。"
        )
        setting = self.service.confirm_target_setting(
            self.project["id"], setting["artifact"]["id"],
            expected_current_version_id=setting["version"]["id"], structured=setting["structured"],
        )
        migration = self.service.create_migration_job(
            self.project["id"], reference["artifact"]["id"], setting["artifact"]["id"]
        )
        target = self.service.get_blueprint(
            self.project["id"], migration["output_artifact_id"]
        )
        review = WorkflowService(self.database).create_review(
            self.project["id"],
            target["artifact"]["id"],
            "blueprint_safety",
            target["version"]["id"],
            summary="迁移审阅",
        )
        edited_nodes = reference["nodes"]
        edited_nodes[0]["summary"] = "作者修订后的机制摘要"

        saved = self.service.save_blueprint_version(
            self.project["id"],
            reference["artifact"]["id"],
            edited_nodes,
            expected_current_version_id=reference["version"]["id"],
            change_summary="确认歧义并修订机制",
        )

        self.assertEqual(saved["version"]["version_number"], 2)
        self.assertNotEqual(saved["version"]["id"], reference["version"]["id"])
        with self.database.connect() as connection:
            target_row = connection.execute(
                "SELECT status FROM artifacts WHERE id=?", (target["artifact"]["id"],)
            ).fetchone()
            review_row = connection.execute(
                "SELECT status FROM reviews WHERE id=?", (review["id"],)
            ).fetchone()
        self.assertEqual(target_row["status"], "stale")
        self.assertEqual(review_row["status"], "stale")
        self.assertTrue(saved["sync"]["impact_ids"])
        self.assertTrue(self.repository.ledger.verify(self.project["id"])["valid"])

    def test_blueprint_edit_rolls_back_version_and_structure_on_injected_node_failure(self) -> None:
        job = self.reference()
        blueprint = self.service.get_blueprint(self.project["id"], job["output_artifact_id"])
        original = self.service.repository.create_node

        def fail_create_node(*args, **kwargs):
            raise RuntimeError("injected edit failure")

        self.service.repository.create_node = fail_create_node
        with self.assertRaisesRegex(RuntimeError, "injected edit failure"):
            self.service.save_blueprint_version(
                self.project["id"], blueprint["artifact"]["id"], blueprint["nodes"],
                expected_current_version_id=blueprint["version"]["id"],
                change_summary="注入编辑失败",
            )
        self.service.repository.create_node = original

        artifact = WorkflowService(self.database).get_artifact(
            self.project["id"], blueprint["artifact"]["id"]
        )
        versions = WorkflowService(self.database).list_artifact_versions(
            self.project["id"], blueprint["artifact"]["id"]
        )
        self.assertEqual(artifact["current_version_id"], blueprint["version"]["id"])
        self.assertEqual(len(versions), 1)

    def test_blueprint_edit_versions_author_interpretation_and_conflict_decisions(self) -> None:
        job = self.reference()
        blueprint = self.service.get_blueprint(self.project["id"], job["output_artifact_id"])
        interpretations = blueprint["interpretations"]
        conflict = blueprint["conflicts"][0]

        saved = self.service.save_blueprint_version(
            self.project["id"], blueprint["artifact"]["id"], blueprint["nodes"],
            expected_current_version_id=blueprint["version"]["id"],
            change_summary="作者裁决解释冲突",
            interpretation_decisions={interpretations[0]["id"]: "confirmed",
                                      interpretations[1]["id"]: "rejected"},
            conflict_resolutions={conflict["id"]: {
                "status": "resolved", "resolution": {"selected_interpretation_id": interpretations[0]["id"]}
            }},
        )
        edited = self.service.get_blueprint(self.project["id"], saved["artifact"]["id"])

        self.assertEqual([item["author_status"] for item in edited["interpretations"]],
                         ["confirmed", "rejected"])
        self.assertEqual(edited["conflicts"][0]["status"], "resolved")
        selected = edited["conflicts"][0]["resolution"]["selected_interpretation_id"]
        self.assertIn(selected, {item["id"] for item in edited["interpretations"]})

    def test_setting_structure_migration_firewall_mapping_and_confirmation(self) -> None:
        sentinel = "REFERENCE_QUOTE_SENTINEL_9XQ"
        reference_job = self.reference(f"第一章\n{sentinel}\n守门人以记忆换取黎明。")
        reference = self.service.get_blueprint(
            self.project["id"], reference_job["output_artifact_id"]
        )
        setting = self.service.create_target_setting(
            self.project["id"],
            "面向成年读者的长篇奇幻：云城修补师阿澈要阻止永夜，失败会失去妹妹，结局是主动放弃王位。",
            overrides={"media_type": "novel", "must_avoid": ["王位继承模板"]},
        )
        required = {
            "genre",
            "audience",
            "media_type",
            "scale",
            "world_rules",
            "characters",
            "character_goals",
            "core_conflict",
            "stakes",
            "themes",
            "narrative_preferences",
            "must_include",
            "must_avoid",
            "ending_direction",
        }
        self.assertEqual(set(setting["structured"]), required)
        self.assertEqual(setting["structured"]["media_type"], "novel")
        self.assertEqual(setting["structured"]["must_avoid"], ["王位继承模板"])
        self.assertEqual(setting["artifact"]["attrs"]["confirmation_status"], "proposed")
        with self.assertRaisesRegex(ValueError, "confirmed"):
            self.service.create_migration_job(
                self.project["id"], reference["artifact"]["id"], setting["artifact"]["id"]
            )
        setting = self.service.confirm_target_setting(
            self.project["id"], setting["artifact"]["id"],
            expected_current_version_id=setting["version"]["id"],
            structured={**setting["structured"], "genre": "author-confirmed-fantasy"},
        )
        self.assertEqual(setting["structured"]["genre"], "author-confirmed-fantasy")
        self.assertEqual(setting["artifact"]["attrs"]["confirmation_status"], "confirmed")
        prior_inputs = len(self.registry.inputs)

        migration = self.service.create_migration_job(
            self.project["id"], reference["artifact"]["id"], setting["artifact"]["id"]
        )
        migration_inputs = self.registry.inputs[prior_inputs:]
        serialized = repr(migration_inputs)
        self.assertNotIn(sentinel, serialized)
        self.assertNotIn("quote", serialized.lower())
        self.assertNotIn("style_statistics", serialized)
        self.assertIn("narrative_function", serialized)
        self.assertIn("causality", serialized)
        self.assertIn("emotion_kline", serialized)

        target = self.service.get_blueprint(
            self.project["id"], migration["output_artifact_id"]
        )
        self.assertEqual(target["artifact"]["attrs"]["confirmation_status"], "proposed")
        mappings = self.service.list_mappings(self.project["id"], migration["id"])
        reference_keys = {node["stable_key"] for node in reference["nodes"]}
        self.assertEqual(
            {item["reference_stable_key"] for item in mappings if item["action"] != "add"},
            reference_keys,
        )
        self.assertTrue(all(item["action"] in {"preserve", "transform", "drop", "add"} for item in mappings))

        confirmed = self.service.confirm_target_blueprint(
            self.project["id"],
            target["artifact"]["id"],
            expected_current_version_id=target["version"]["id"],
        )
        self.assertEqual(confirmed["artifact"]["status"], "approved")
        self.assertEqual(confirmed["artifact"]["attrs"]["confirmation_status"], "confirmed")

    def test_setting_mapping_and_target_blueprint_consume_typed_agent_outputs(self) -> None:
        class FunctionAgent:
            output_schema = "creative-claw.blueprint-agent.v1"

            def __init__(self, name, function):
                self.name = name
                self.function = function

            def run(self, task):
                return AgentResult(data=self.function(task.context), confidence=0.9,
                                   model={"provider": "test"})

        structured = {
            "genre": "agent-genre", "audience": "agent-audience", "media_type": "audio_drama",
            "scale": "six_episodes", "world_rules": ["agent-rule"],
            "characters": [{"name": "代理角色"}], "character_goals": ["代理目标"],
            "core_conflict": "代理冲突", "stakes": "代理代价", "themes": ["代理主题"],
            "narrative_preferences": {"pov": "first"}, "must_include": ["代理必须"],
            "must_avoid": ["代理禁止"], "ending_direction": "代理结局",
        }
        self.registry.register(FunctionAgent("target_setting_agent", lambda _context: {"structured": structured}))
        self.registry.register(FunctionAgent("mechanism_mapping_agent", lambda context: {
            "mappings": [
                {"reference_stable_key": node["stable_key"],
                 "target_stable_key": f"agent:{node['stable_key']}",
                 "action": "drop" if index == 0 else "transform", "rationale": f"agent-map-{index}"}
                for index, node in enumerate(context["abstract_reference_blueprint"])
            ]
        }))

        def target_output(context):
            dimensions = {
                name: {"state": "not_observed", "value": None, "confidence": 0.0,
                       "evidence_refs": []} for name in BLUEPRINT_DIMENSIONS
            }
            dimensions["narrative_function"] = {
                "state": "observed", "value": {"mechanism_class": "agent-target-function"},
                "confidence": 0.9, "evidence_refs": ["target-setting"],
            }
            source_types = {item["stable_key"]: item["node_type"] for item in context["abstract_mechanisms"]}
            nodes = []
            for item in context["typed_mapping"]:
                if item["action"] == "drop":
                    continue
                nodes.append({"stable_key": item["target_stable_key"],
                              "node_type": source_types[item["reference_stable_key"]],
                              "title": "代理目标作品" if not nodes else "代理目标单元",
                              "summary": "代理目标摘要", "dimensions": dimensions})
            return {"nodes": nodes, "structural_risk": "review_required"}

        self.registry.register(FunctionAgent("target_blueprint_agent", target_output))
        reference_job = self.reference()
        reference = self.service.get_blueprint(self.project["id"], reference_job["output_artifact_id"])
        setting = self.service.create_target_setting(self.project["id"], "此文本不应替代代理输出")
        self.assertEqual(setting["structured"]["genre"], "agent-genre")
        setting = self.service.confirm_target_setting(
            self.project["id"], setting["artifact"]["id"],
            expected_current_version_id=setting["version"]["id"], structured=setting["structured"],
        )
        migration = self.service.create_migration_job(
            self.project["id"], reference["artifact"]["id"], setting["artifact"]["id"]
        )
        target = self.service.get_blueprint(self.project["id"], migration["output_artifact_id"])
        mappings = self.service.list_mappings(self.project["id"], migration["id"])

        self.assertEqual(target["nodes"][0]["title"], "代理目标作品")
        self.assertEqual(target["artifact"]["attrs"]["structural_risk"], "review_required")
        self.assertEqual(mappings[0]["action"], "drop")
        self.assertTrue(all(item["rationale"].startswith("agent-map-") for item in mappings))

    def test_target_blueprint_publication_rolls_back_on_injected_mapping_failure(self) -> None:
        reference_job = self.reference()
        reference = self.service.get_blueprint(self.project["id"], reference_job["output_artifact_id"])
        setting = self.service.create_target_setting(self.project["id"], "目标设定用于迁移事务测试。")
        setting = self.service.confirm_target_setting(
            self.project["id"], setting["artifact"]["id"],
            expected_current_version_id=setting["version"]["id"], structured=setting["structured"],
        )
        original = self.service.repository.create_mapping

        def fail_mapping(*args, **kwargs):
            raise RuntimeError("injected mapping failure")

        self.service.repository.create_mapping = fail_mapping
        with self.assertRaisesRegex(RuntimeError, "injected mapping failure"):
            self.service.create_migration_job(
                self.project["id"], reference["artifact"]["id"], setting["artifact"]["id"]
            )
        self.service.repository.create_mapping = original

        with self.database.connect() as connection:
            target_count = connection.execute(
                "SELECT COUNT(*) AS n FROM artifacts WHERE project_id=? AND artifact_type='target_blueprint'",
                (self.project["id"],),
            ).fetchone()["n"]
            mapping_count = connection.execute(
                "SELECT COUNT(*) AS n FROM blueprint_mappings WHERE project_id=?",
                (self.project["id"],),
            ).fetchone()["n"]
        self.assertEqual(target_count, 0)
        self.assertEqual(mapping_count, 0)

    def test_migration_cancelled_by_mapping_agent_never_calls_target_or_publishes(self) -> None:
        reference_job = self.reference()
        reference = self.service.get_blueprint(self.project["id"], reference_job["output_artifact_id"])
        setting = self.service.create_target_setting(self.project["id"], "A confirmed target setting.")
        setting = self.service.confirm_target_setting(
            self.project["id"], setting["artifact"]["id"],
            expected_current_version_id=setting["version"]["id"], structured=setting["structured"],
        )
        successful = self.registry.get("mechanism_mapping_agent")
        repository = self.service.repository
        project_id = self.project["id"]

        class CancelAfterMapping:
            name = "mechanism_mapping_agent"
            output_schema = "creative-claw.blueprint-agent.v1"

            def run(self, task):
                result = successful.run(task)
                repository.set_job_desired_state(project_id, task.job_id, "cancelled")
                return result

        self.registry.register(CancelAfterMapping())
        target_calls = self.registry.calls.count("target_blueprint_agent")

        job = self.service.create_migration_job(
            self.project["id"], reference["artifact"]["id"], setting["artifact"]["id"]
        )

        self.assertEqual(job["status"], "cancelled")
        self.assertIsNone(job.get("output_artifact_id"))
        self.assertEqual(self.registry.calls.count("target_blueprint_agent"), target_calls)
        with self.database.connect() as connection:
            count = connection.execute(
                "SELECT COUNT(*) AS n FROM artifacts WHERE project_id=? AND artifact_type='target_blueprint'",
                (self.project["id"],),
            ).fetchone()["n"]
        self.assertEqual(count, 0)

    def test_migration_paused_by_target_agent_never_publishes(self) -> None:
        reference_job = self.reference()
        reference = self.service.get_blueprint(self.project["id"], reference_job["output_artifact_id"])
        setting = self.service.create_target_setting(self.project["id"], "Another confirmed target setting.")
        setting = self.service.confirm_target_setting(
            self.project["id"], setting["artifact"]["id"],
            expected_current_version_id=setting["version"]["id"], structured=setting["structured"],
        )
        successful = self.registry.get("target_blueprint_agent")
        repository = self.service.repository
        project_id = self.project["id"]

        class PauseAfterTarget:
            name = "target_blueprint_agent"
            output_schema = "creative-claw.blueprint-agent.v1"

            def run(self, task):
                result = successful.run(task)
                repository.set_job_desired_state(project_id, task.job_id, "paused")
                return result

        self.registry.register(PauseAfterTarget())

        job = self.service.create_migration_job(
            self.project["id"], reference["artifact"]["id"], setting["artifact"]["id"]
        )

        self.assertEqual(job["status"], "paused")
        self.assertIsNone(job.get("output_artifact_id"))
        with self.database.connect() as connection:
            count = connection.execute(
                "SELECT COUNT(*) AS n FROM artifacts WHERE project_id=? AND artifact_type='target_blueprint'",
                (self.project["id"],),
            ).fetchone()["n"]
        self.assertEqual(count, 0)

    def _draft_fixture(self, *, reference_text: str, target_text: str) -> tuple[dict, dict, dict]:
        reference_job = self.reference(reference_text)
        reference = self.service.get_blueprint(self.project["id"], reference_job["output_artifact_id"])
        setting = self.service.create_target_setting(self.project["id"], target_text)
        setting = self.service.confirm_target_setting(
            self.project["id"], setting["artifact"]["id"],
            expected_current_version_id=setting["version"]["id"], structured=setting["structured"],
        )
        migration = self.service.create_migration_job(
            self.project["id"], reference["artifact"]["id"], setting["artifact"]["id"]
        )
        target = self.service.get_blueprint(self.project["id"], migration["output_artifact_id"])
        self.service.confirm_target_blueprint(
            self.project["id"], target["artifact"]["id"], expected_current_version_id=target["version"]["id"]
        )
        workflow = WorkflowService(self.database)
        unit = workflow.create_production_unit(self.project["id"], "scene", "第一场", position=1)
        artifact = workflow.create_artifact(
            self.project["id"], "manuscript", "第一场草稿", unit_id=unit["id"]
        )
        return target, unit, artifact

    def test_draft_context_excludes_reference_and_firewall_records_provenance_block(self) -> None:
        reference_sentinel = "REFERENCE_TEXT_SENTINEL_A81"
        target_sentinel = "TARGET_SETTING_SENTINEL_B92"
        target, unit, artifact = self._draft_fixture(
            reference_text=f"第一章\n{reference_sentinel}\n守门人离开。",
            target_text=f"新世界设定：{target_sentinel}，修复师进入云城。",
        )
        prior = len(self.registry.inputs)
        candidate = self.service.create_draft_candidate(
            self.project["id"], target["artifact"]["id"], unit["id"], artifact["id"]
        )
        draft_inputs = [
            item for item in self.registry.inputs[prior:] if item["agent"] == "draft_writer_agent"
        ]
        self.assertEqual(len(draft_inputs), 1)
        serialized = repr(draft_inputs[0])
        self.assertIn(target_sentinel, serialized)
        self.assertNotIn(reference_sentinel, serialized)
        self.assertNotIn("quote", serialized.lower())
        self.assertEqual(candidate["status"], "passed")

        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT attrs_json FROM artifacts WHERE id=?", (target["artifact"]["id"],)
            ).fetchone()
            connection.execute(
                "UPDATE artifacts SET attrs_json=json_set(attrs_json, '$.provenance', 'reference') WHERE id=?",
                (target["artifact"]["id"],),
            )
        with self.assertRaises(ContextFirewallError):
            DraftContextBuilder(self.database).build(
                self.project["id"], target["artifact"]["id"], unit["id"], artifact["id"]
            )
        events = self.repository.ledger.list(self.project["id"])
        self.assertTrue(any(event["event_type"] == "context_firewall_blocked" for event in events))

    def test_final_generation_request_scans_for_reference_fragments_hidden_in_allowed_values(self) -> None:
        sentinel = "HIDDEN_REFERENCE_FRAGMENT_Q7X"
        target, unit, artifact = self._draft_fixture(
            reference_text=f"{sentinel} 后面是完全不同的参考叙事。",
            target_text="新作品设定只描述沙漠测绘。",
        )
        node = self.service.get_blueprint(
            self.project["id"], target["artifact"]["id"], include_quotes=False
        )["nodes"][0]
        dimensions = node["dimensions"]
        dimensions["narrative_function"] = {
            "state": "observed", "value": {"mechanism_class": sentinel},
            "confidence": 0.9, "evidence_refs": ["target-setting"],
        }
        with self.database.connect() as connection:
            from creative_claw.util import json_dumps
            connection.execute(
                "UPDATE blueprint_nodes SET dimensions_json=? WHERE id=?",
                (json_dumps(dimensions), node["id"]),
            )
        writer_calls = self.registry.calls.count("draft_writer_agent")

        with self.assertRaises(ContextFirewallError):
            self.service.create_draft_candidate(
                self.project["id"], target["artifact"]["id"], unit["id"], artifact["id"]
            )
        self.assertEqual(self.registry.calls.count("draft_writer_agent"), writer_calls)
        self.assertTrue(any(
            event["event_type"] == "context_firewall_blocked"
            for event in self.repository.ledger.list(self.project["id"])
        ))

    def test_unknown_reference_mechanism_class_never_reaches_migration_or_generation_agents(self) -> None:
        sentinel = "MECHANISM_CLASS_REFERENCE_SENTINEL_R4Q"

        class SentinelHierarchyAgent:
            name = "hierarchy_synthesis_agent"
            output_schema = "creative-claw.blueprint-agent.v1"

            def run(self, task):
                dimensions = {
                    name: {"state": "not_observed", "value": None, "confidence": 0.0,
                           "evidence_refs": []}
                    for name in BLUEPRINT_DIMENSIONS
                }
                text = str(task.context.get("text") or "")
                evidence = []
                if text:
                    dimensions["narrative_function"] = {
                        "state": "observed", "value": {"mechanism_class": sentinel},
                        "confidence": 0.9, "evidence_refs": ["sentinel-evidence"],
                    }
                    evidence = [{"id": "sentinel-evidence", "start": 0, "end": 2,
                                 "source_length": len(text), "confidence": 0.9}]
                return AgentResult(
                    data={"dimensions": dimensions,
                          "nodes": [{"stable_key": "work", "key_scope": "global",
                                     "node_type": "work"}]},
                    evidence=evidence,
                    confidence=0.9, model={"provider": "test"},
                )

        class EmptyConflictAgent:
            name = "interpretation_conflict_agent"
            output_schema = "creative-claw.blueprint-agent.v1"

            def run(self, _task):
                dimensions = {
                    name: {"state": "not_observed", "value": None, "confidence": 0.0,
                           "evidence_refs": []}
                    for name in BLUEPRINT_DIMENSIONS
                }
                return AgentResult(data={"dimensions": dimensions, "interpretations": [], "conflicts": []},
                                   confidence=0.9, model={"provider": "test"})

        self.registry.register(SentinelHierarchyAgent())
        self.registry.register(EmptyConflictAgent())
        reference_job = self.reference("第一章\n人物作出代价选择。")
        reference = self.service.get_blueprint(self.project["id"], reference_job["output_artifact_id"])
        setting = self.service.create_target_setting(self.project["id"], "沙漠声学师修复新钟阵。")
        setting = self.service.confirm_target_setting(
            self.project["id"], setting["artifact"]["id"],
            expected_current_version_id=setting["version"]["id"], structured=setting["structured"],
        )
        before_migration = len(self.registry.inputs)
        migration = self.service.create_migration_job(
            self.project["id"], reference["artifact"]["id"], setting["artifact"]["id"]
        )
        migration_inputs = repr(self.registry.inputs[before_migration:])
        self.assertNotIn(sentinel, migration_inputs)
        self.assertIn("generic:narrative_function", migration_inputs)

        target = self.service.get_blueprint(self.project["id"], migration["output_artifact_id"])
        self.service.confirm_target_blueprint(
            self.project["id"], target["artifact"]["id"], expected_current_version_id=target["version"]["id"]
        )
        workflow = WorkflowService(self.database)
        unit = workflow.create_production_unit(self.project["id"], "scene", "第一场", position=1)
        artifact = workflow.create_artifact(
            self.project["id"], "manuscript", "第一场草稿", unit_id=unit["id"]
        )
        before_generation = len(self.registry.inputs)
        self.service.create_draft_candidate(
            self.project["id"], target["artifact"]["id"], unit["id"], artifact["id"]
        )
        generation_inputs = repr(self.registry.inputs[before_generation:])
        self.assertNotIn(sentinel, generation_inputs)

    def test_candidate_gate_uses_persisted_reference_beats_mappings_and_rare_phrases(self) -> None:
        rare_phrase = "赤铜月桂机关"
        target, unit, artifact = self._draft_fixture(
            reference_text="参考文本不含安全指纹中的短语。",
            target_text="新作讲述声学师修复城市钟阵。",
        )
        target_artifact = self.service.get_blueprint(self.project["id"], target["artifact"]["id"])
        migration_job_id = target_artifact["artifact"]["attrs"]["migration_job_id"]
        with self.database.connect() as connection:
            reference_version_id = connection.execute(
                "SELECT reference_version_id FROM blueprint_mappings WHERE job_id=? LIMIT 1",
                (migration_job_id,),
            ).fetchone()["reference_version_id"]
            root = connection.execute(
                "SELECT id, dimensions_json FROM blueprint_nodes WHERE artifact_version_id=? AND node_type='work'",
                (reference_version_id,),
            ).fetchone()
        from creative_claw.util import json_dumps, json_loads
        root_dimensions = json_loads(root["dimensions_json"], {})
        root_dimensions["style_statistics"] = {
            "state": "observed", "value": {"rare_phrases": [rare_phrase]},
            "confidence": 0.95, "evidence_refs": ["style-fingerprint"],
        }
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE blueprint_nodes SET dimensions_json=? WHERE id=?",
                (json_dumps(root_dimensions), root["id"]),
            )
            connection.execute(
                "UPDATE blueprint_mappings SET action='preserve' WHERE job_id=?",
                (migration_job_id,),
            )
        dimensions = {
            name: {"state": "not_observed", "value": None, "confidence": 0.0, "evidence_refs": []}
            for name in BLUEPRINT_DIMENSIONS
        }
        for index in range(10):
            dimensions_for_beat = {name: dict(value) for name, value in dimensions.items()}
            dimensions_for_beat["characters"] = {
                "state": "observed", "value": {"role_function": f"r{index}"},
                "confidence": 0.8, "evidence_refs": ["beat"]}
            dimensions_for_beat["events"] = {
                "state": "observed", "value": {"event_function": f"e{index}", "outcome": f"o{index}"},
                "confidence": 0.8, "evidence_refs": ["beat"]}
            self.service.repository.create_node(
                self.project["id"], artifact_version_id=reference_version_id, job_id=None,
                stable_key=f"beat:{index}", node_type="beat", dimensions=dimensions_for_beat,
                title=f"参考节拍 {index}",
            )

        original_planner = self.registry.get("unit_planner_agent")

        class BeatPlanner:
            name = "unit_planner_agent"
            output_schema = "creative-claw.blueprint-agent.v1"

            def run(self, task):
                beats = [
                    {"role_function": f"r{i}", "event_function": f"e{i}", "outcome": f"o{i}"}
                    if i < 7 else {"role_function": "x", "event_function": "y", "outcome": "z"}
                    for i in range(10)
                ]
                return AgentResult(data={"unit_plan": {"goal": "new", "beats": beats}}, confidence=0.9)

        self.registry.register(BeatPlanner())
        self.registry.set_draft_text("候选采用全新表达，但保留七个一一对应的节拍。")
        structural = self.service.create_draft_candidate(
            self.project["id"], target["artifact"]["id"], unit["id"], artifact["id"]
        )
        self.assertEqual(structural["status"], "review_required")
        self.registry.register(original_planner)

        self.registry.set_draft_text(f"全新人物提到{rare_phrase}，随后走入另一个世界。")
        rare = self.service.create_draft_candidate(
            self.project["id"], target["artifact"]["id"], unit["id"], artifact["id"]
        )
        self.assertEqual(rare["status"], "blocked")
        self.assertGreaterEqual(rare["similarity"]["expression"]["rare_phrase_hit_count"], 1)

    def test_typed_similarity_safety_verdict_can_escalate_a_deterministic_pass(self) -> None:
        target, unit, artifact = self._draft_fixture(
            reference_text="参考人物沿冰河寻找旧塔。", target_text="目标人物在沙漠修理风钟。",
        )

        class BlockingSafetyAgent:
            name = "similarity_safety_agent"
            output_schema = "creative-claw.blueprint-agent.v1"

            def run(self, task):
                return AgentResult(data={"verdict": {"gate_status": "blocked",
                    "findings": [{"layer": "expression", "rule": "safety_agent_location"}],
                    "remediation": ["rewrite"]}}, confidence=0.99)

        self.registry.register(BlockingSafetyAgent())
        self.registry.set_draft_text("完全不同的新候选文本。")
        candidate = self.service.create_draft_candidate(
            self.project["id"], target["artifact"]["id"], unit["id"], artifact["id"]
        )
        self.assertEqual(candidate["status"], "blocked")
        self.assertTrue(any(item["rule"] == "safety_agent_location"
                            for item in candidate["similarity"]["findings"]))

    def test_malicious_safety_verdict_is_schema_failed_and_never_persisted(self) -> None:
        target, unit, artifact = self._draft_fixture(
            reference_text="A private reference passage that must never be persisted by the safety verdict.",
            target_text="An unrelated target story about repairing a desert observatory.",
        )

        class MaliciousSafetyAgent:
            name = "similarity_safety_agent"
            output_schema = "creative-claw.blueprint-agent.v1"

            def run(self, task):
                return AgentResult(data={"verdict": {
                    "gate_status": "blocked",
                    "findings": [{"layer": "expression", "rule": "safety_agent_location",
                                  "detail": task.context["reference_text"]}],
                    "remediation": ["rewrite"],
                }}, confidence=0.99)

        self.registry.register(MaliciousSafetyAgent())
        self.registry.set_draft_text("A wholly original candidate.")
        with self.database.connect() as connection:
            candidates_before = connection.execute(
                "SELECT COUNT(*) AS n FROM draft_candidates WHERE project_id=?", (self.project["id"],)
            ).fetchone()["n"]

        with self.assertRaisesRegex(ValueError, "similarity_safety_agent schema_failed"):
            self.service.create_draft_candidate(
                self.project["id"], target["artifact"]["id"], unit["id"], artifact["id"]
            )

        with self.database.connect() as connection:
            safety_run = connection.execute(
                """SELECT status FROM blueprint_agent_runs
                   WHERE project_id=? AND agent_name='similarity_safety_agent'
                   ORDER BY created_at DESC LIMIT 1""",
                (self.project["id"],),
            ).fetchone()
            candidates_after = connection.execute(
                "SELECT COUNT(*) AS n FROM draft_candidates WHERE project_id=?", (self.project["id"],)
            ).fetchone()["n"]
            assessments = connection.execute(
                "SELECT COUNT(*) AS n FROM similarity_assessments WHERE project_id=?", (self.project["id"],)
            ).fetchone()["n"]
        self.assertEqual(safety_run["status"], "schema_failed")
        self.assertEqual(candidates_after, candidates_before)
        self.assertEqual(assessments, 0)

    def test_candidate_similarity_accept_reject_and_version_conflict(self) -> None:
        target, unit, artifact = self._draft_fixture(
            reference_text="第一章\n守门人沿着冰河寻找旧塔，却决定焚毁唯一地图。",
            target_text="沙漠声学师寻找失落钟阵，目标是让城市恢复清晨。",
        )
        self.registry.set_draft_text("声学师拆下铜铃，在无风广场重建全新的共振顺序。")
        candidate = self.service.create_draft_candidate(
            self.project["id"], target["artifact"]["id"], unit["id"], artifact["id"]
        )
        accepted = self.service.accept_candidate(
            self.project["id"], candidate["id"], expected_current_version_id=None
        )
        self.assertEqual(accepted["status"], "accepted")
        self.assertEqual(accepted["accepted_version_id"], accepted["version"]["id"])

        self.registry.set_draft_text("另一份完全不同的候选草稿。")
        second = self.service.create_draft_candidate(
            self.project["id"], target["artifact"]["id"], unit["id"], artifact["id"]
        )
        before = WorkflowService(self.database).get_artifact(self.project["id"], artifact["id"])["current_version_id"]
        rejected = self.service.reject_candidate(self.project["id"], second["id"], reason="人物目标不清")
        after = WorkflowService(self.database).get_artifact(self.project["id"], artifact["id"])["current_version_id"]
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(before, after)

        self.registry.set_draft_text("守门人沿着冰河寻找旧塔，却决定焚毁唯一地图。")
        blocked = self.service.create_draft_candidate(
            self.project["id"], target["artifact"]["id"], unit["id"], artifact["id"]
        )
        self.assertEqual(blocked["status"], "blocked")
        with self.assertRaisesRegex(ValueError, "blocked"):
            self.service.accept_candidate(
                self.project["id"], blocked["id"], expected_current_version_id=after
            )

        self.registry.set_draft_text("第三份安全候选，人物改为记录城市回声。")
        concurrent = self.service.create_draft_candidate(
            self.project["id"], target["artifact"]["id"], unit["id"], artifact["id"]
        )
        barrier = threading.Barrier(2)

        def accept_once() -> str:
            barrier.wait()
            try:
                self.service.accept_candidate(
                    self.project["id"], concurrent["id"], expected_current_version_id=after
                )
                return "accepted"
            except Exception as error:  # domain outcome asserted below
                return type(error).__name__

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _value: accept_once(), range(2)))
        self.assertEqual(outcomes.count("accepted"), 1)
        self.assertEqual(outcomes.count("VersionConflictError"), 1)

    def test_accept_rejects_candidate_whose_base_does_not_equal_requested_and_current_version(self) -> None:
        target, unit, artifact = self._draft_fixture(
            reference_text="参考中的守门人渡过冰河。",
            target_text="新作中的测绘师穿越沙漠。",
        )
        self.registry.set_draft_text("测绘师记录风向并重新规划陌生路线。")
        candidate = self.service.create_draft_candidate(
            self.project["id"], target["artifact"]["id"], unit["id"], artifact["id"]
        )
        external = WorkflowService(self.database).save_artifact_version(
            self.project["id"], artifact["id"], "作者同时写入的版本",
            expected_current_version_id=None, change_summary="并发作者编辑",
        )

        with self.assertRaises(VersionConflictError):
            self.service.accept_candidate(
                self.project["id"], candidate["id"],
                expected_current_version_id=external["version"]["id"],
            )
        self.assertEqual(self.service.get_candidate(self.project["id"], candidate["id"])["status"], "passed")

    def test_candidate_accept_rolls_back_version_candidate_and_ledger_on_injected_failure(self) -> None:
        target, unit, artifact = self._draft_fixture(
            reference_text="参考人物寻找旧塔。", target_text="目标人物修复新钟。",
        )
        self.registry.set_draft_text("目标人物测量铜钟并记录全新频率。")
        candidate = self.service.create_draft_candidate(
            self.project["id"], target["artifact"]["id"], unit["id"], artifact["id"]
        )
        original_append = self.service.workflow.ledger.append

        def fail_after_version(project_id, event_type, payload, actor="system", **kwargs):
            if event_type == "draft_candidate.accepted":
                raise RuntimeError("injected accept failure")
            return original_append(project_id, event_type, payload, actor, **kwargs)

        self.service.workflow.ledger.append = fail_after_version
        with self.assertRaisesRegex(RuntimeError, "injected accept failure"):
            self.service.accept_candidate(
                self.project["id"], candidate["id"], expected_current_version_id=None
            )
        self.service.workflow.ledger.append = original_append

        current = WorkflowService(self.database).get_artifact(self.project["id"], artifact["id"])
        stored = self.service.get_candidate(self.project["id"], candidate["id"])
        versions = WorkflowService(self.database).list_artifact_versions(self.project["id"], artifact["id"])
        self.assertIsNone(current["current_version_id"])
        self.assertEqual(stored["status"], "passed")
        self.assertEqual(versions, [])
        self.assertTrue(self.repository.ledger.verify(self.project["id"])["valid"])

    def test_accept_reject_race_has_exactly_one_terminal_transition(self) -> None:
        target, unit, artifact = self._draft_fixture(
            reference_text="参考人物寻找旧塔。", target_text="目标人物修复新钟。",
        )
        self.registry.set_draft_text("候选人物重排风铃，得到新的城市信号。")
        candidate = self.service.create_draft_candidate(
            self.project["id"], target["artifact"]["id"], unit["id"], artifact["id"]
        )
        barrier = threading.Barrier(2)

        def accept() -> str:
            barrier.wait()
            try:
                self.service.accept_candidate(self.project["id"], candidate["id"],
                                              expected_current_version_id=None)
                return "accepted"
            except Exception as error:
                return type(error).__name__

        def reject() -> str:
            barrier.wait()
            try:
                self.service.reject_candidate(self.project["id"], candidate["id"], reason="竞态拒绝")
                return "rejected"
            except Exception as error:
                return type(error).__name__

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = [pool.submit(accept), pool.submit(reject)]
            outcomes = [future.result() for future in outcomes]
        stored = self.service.get_candidate(self.project["id"], candidate["id"])
        self.assertEqual(sum(item in {"accepted", "rejected"} for item in outcomes), 1)
        self.assertIn(stored["status"], {"accepted", "rejected"})
        self.assertEqual(stored["status"], next(item for item in outcomes if item in {"accepted", "rejected"}))


if __name__ == "__main__":
    unittest.main()
