from __future__ import annotations

import tempfile
import re
import unittest
from pathlib import Path

from creative_claw.blueprint_agents import AgentResult, DeterministicAgentRegistry
from creative_claw.blueprint_models import BLUEPRINT_DIMENSIONS
from creative_claw.blueprint_orchestrator import BlueprintOrchestrator
from creative_claw.blueprint_repository import BlueprintRepository
from creative_claw.db import Database
from creative_claw.repository import Repository
from creative_claw.util import sha256_text


class BlueprintOrchestratorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.database = Database(self.root / "creative-claw.db")
        self.database.initialize()
        self.project = Repository(self.database).create_project("蓝图项目", self.root / "project")
        self.blueprints = BlueprintRepository(self.database)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def create_job(self, text: str, key: str = "reference:test") -> dict:
        return self.blueprints.create_job(
            self.project["id"],
            job_type="reference",
            input_json={"title": "参考", "text": text, "source_length": len(text)},
            idempotency_key=key,
        )

    def test_fixed_dag_waits_for_specialists_and_emits_all_dimensions(self) -> None:
        job = self.create_job("第一章\n林岚作出选择，因此失去归途。")
        registry = DeterministicAgentRegistry()
        orchestrator = BlueprintOrchestrator(self.database, registry)

        result = orchestrator.run_job(self.project["id"], job["id"])

        self.assertEqual(registry.calls[0], "segmentation_agent")
        self.assertLess(
            registry.calls.index("event_causality_agent"),
            registry.calls.index("hierarchy_synthesis_agent"),
        )
        self.assertLess(
            registry.calls.index("hierarchy_synthesis_agent"),
            registry.calls.index("interpretation_conflict_agent"),
        )
        self.assertEqual(set(result["blueprint"]["dimensions"]), set(BLUEPRINT_DIMENSIONS))
        self.assertEqual(result["status"], "completed")
        for dimension in result["blueprint"]["dimensions"].values():
            self.assertIn(dimension["state"], {"observed", "not_observed", "uncertain"})

        calls_after_first = list(registry.calls)
        second = orchestrator.run_job(self.project["id"], job["id"])
        self.assertEqual(second["status"], "completed")
        self.assertEqual(registry.calls, calls_after_first)

    def test_missing_required_agent_blocks_merge(self) -> None:
        job = self.create_job("一段参考文本", "reference:missing")
        registry = DeterministicAgentRegistry(excluded={"event_causality_agent"})

        result = BlueprintOrchestrator(self.database, registry).run_job(
            self.project["id"], job["id"]
        )

        self.assertEqual(result["status"], "blocked")
        self.assertIn("event_causality_agent", result["error"]["missing_agents"])
        self.assertNotIn("hierarchy_synthesis_agent", registry.calls)

    def test_long_text_batches_use_absolute_ranges_and_resume_idempotently(self) -> None:
        chapter = "第{n}章\n" + ("风暴之后人物推进目标。" * 900)
        text = "\n".join(chapter.format(n=index) for index in range(1, 5))
        self.assertGreater(len(text), 35_000)
        job = self.create_job(text, "reference:long")
        registry = DeterministicAgentRegistry()
        orchestrator = BlueprintOrchestrator(self.database, registry)

        partial = orchestrator.run_job(self.project["id"], job["id"], max_batches=1)
        batches = self.blueprints.list_batches(self.project["id"], job["id"])
        self.assertEqual(partial["status"], "resumable")
        self.assertGreater(len(batches), 1)
        for batch in batches:
            self.assertLessEqual(batch["overlap_start"], 800)
            self.assertLessEqual(batch["end_offset"] - batch["start_offset"], 12_800)
            self.assertEqual(
                batch["source_hash"],
                sha256_text(text[batch["start_offset"] : batch["end_offset"]]),
            )

        calls_after_partial = len(registry.calls)
        self.blueprints.set_job_desired_state(self.project["id"], job["id"], "paused")
        paused = orchestrator.run_job(self.project["id"], job["id"])
        self.assertEqual(paused["status"], "paused")
        self.assertEqual(len(registry.calls), calls_after_partial)

        self.blueprints.set_job_desired_state(self.project["id"], job["id"], "running")
        completed = orchestrator.run_job(self.project["id"], job["id"])
        self.assertEqual(completed["status"], "completed")
        evidence = completed["blueprint"]["evidence"]
        self.assertTrue(evidence)
        self.assertTrue(all(0 <= item["start"] < item["end"] <= len(text) for item in evidence))

        run_count = len(self.blueprints.list_agent_runs(self.project["id"], job["id"]))
        orchestrator.run_job(self.project["id"], job["id"])
        self.assertEqual(
            len(self.blueprints.list_agent_runs(self.project["id"], job["id"])), run_count
        )

    def test_cancelled_job_never_calls_agent(self) -> None:
        job = self.create_job("不会运行", "reference:cancel")
        registry = DeterministicAgentRegistry()
        self.blueprints.set_job_desired_state(self.project["id"], job["id"], "cancelled")

        result = BlueprintOrchestrator(self.database, registry).run_job(
            self.project["id"], job["id"]
        )

        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(registry.calls, [])

    def test_retryable_failed_run_is_not_reused_as_completed(self) -> None:
        job = self.create_job("失败后应创建新尝试", "reference:retry-attempt")
        registry = DeterministicAgentRegistry()
        successful = registry.get("event_causality_agent")

        class FailOnceAgent:
            name = "event_causality_agent"
            output_schema = "creative-claw.blueprint-agent.v1"
            attempts = 0

            def run(self, task):
                self.attempts += 1
                if self.attempts == 1:
                    raise TimeoutError("injected retryable failure")
                return successful.run(task)

        registry.register(FailOnceAgent())
        orchestrator = BlueprintOrchestrator(self.database, registry)

        first = orchestrator.run_job(self.project["id"], job["id"])
        self.assertEqual(first["status"], "blocked")
        self.blueprints.set_job_desired_state(self.project["id"], job["id"], "running")
        second = orchestrator.run_job(self.project["id"], job["id"])

        runs = [
            run for run in self.blueprints.list_agent_runs(self.project["id"], job["id"])
            if run["agent_name"] == "event_causality_agent"
        ]
        self.assertEqual(second["status"], "completed")
        self.assertEqual([run["status"] for run in runs], ["retryable_failed", "completed"])
        self.assertNotEqual(runs[0]["idempotency_key"], runs[1]["idempotency_key"])

    def test_pause_requested_by_final_conflict_prevents_job_completion(self) -> None:
        job = self.create_job("尾部暂停竞态", "reference:tail-pause")
        registry = DeterministicAgentRegistry()
        successful = registry.get("interpretation_conflict_agent")
        repository = self.blueprints
        project_id = self.project["id"]

        class PauseAfterRunAgent:
            name = "interpretation_conflict_agent"
            output_schema = "creative-claw.blueprint-agent.v1"

            def run(self, task):
                result = successful.run(task)
                repository.set_job_desired_state(project_id, task.job_id, "paused")
                return result

        registry.register(PauseAfterRunAgent())

        result = BlueprintOrchestrator(self.database, registry).run_job(project_id, job["id"])
        batch = self.blueprints.list_batches(project_id, job["id"])[0]

        self.assertEqual(result["status"], "paused")
        self.assertEqual(batch["status"], "completed")
        self.assertNotIn("artifact_version_id", result["checkpoint"])

    def test_out_of_batch_evidence_marks_run_invalid_and_blocks_merge(self) -> None:
        job = self.create_job("本地证据边界", "reference:evidence-invalid")
        registry = DeterministicAgentRegistry()
        successful = registry.get("evidence_locator_agent")

        class InvalidEvidenceAgent:
            name = "evidence_locator_agent"
            output_schema = "creative-claw.blueprint-agent.v1"

            def run(self, task):
                result = successful.run(task)
                result.evidence = [{"id": "bad", "start": 0, "end": len(task.context["text"]) + 1,
                                    "source_length": len(task.context["text"]), "confidence": 0.9}]
                return result

        registry.register(InvalidEvidenceAgent())

        result = BlueprintOrchestrator(self.database, registry).run_job(self.project["id"], job["id"])
        run = next(
            item for item in self.blueprints.list_agent_runs(self.project["id"], job["id"])
            if item["agent_name"] == "evidence_locator_agent"
        )

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(run["status"], "evidence_invalid")
        self.assertEqual(run["error_category"], "evidence_invalid")

    def test_batch_hierarchy_requires_evidence_from_current_response(self) -> None:
        dimensions = {
            name: {"state": "not_observed", "value": None,
                   "confidence": 0.0, "evidence_refs": []}
            for name in BLUEPRINT_DIMENSIONS
        }
        dimensions["narrative_function"] = {
            "state": "observed", "value": {"mechanism_class": "costly_choice"},
            "confidence": 0.9, "evidence_refs": ["prior-response-evidence"],
        }
        data = {
            "dimensions": dimensions,
            "nodes": [{"stable_key": "work", "key_scope": "global", "node_type": "work"}],
        }
        current_evidence = [{
            "id": "current-response-evidence", "start": 0, "end": 1,
            "source_length": 1, "confidence": 0.9,
        }]

        with self.assertRaisesRegex(ValueError, "references missing evidence"):
            BlueprintOrchestrator._validate_agent_result(
                "hierarchy_synthesis_agent", data, current_evidence, 1
            )

    def test_typed_agent_outputs_become_hierarchy_edges_interpretations_and_conflicts(self) -> None:
        job = self.create_job("真实类型化输出", "reference:typed-merge")
        registry = DeterministicAgentRegistry()

        class TypedAgent:
            output_schema = "creative-claw.blueprint-agent.v1"

            def __init__(self, name, data):
                self.name = name
                self.data = data

            def run(self, task):
                refs = [
                    ref for dimension in self.data.get("dimensions", {}).values()
                    for ref in dimension.get("evidence_refs", [])
                ]
                evidence = ([{"id": refs[0], "start": 0, "end": 1,
                              "source_length": len(task.context["text"]), "confidence": 0.9}]
                            if refs else [])
                return AgentResult(data=self.data, evidence=evidence, confidence=0.88,
                                   model={"provider": "test"})

        registry.register(TypedAgent("segmentation_agent", {"nodes": [
            {"stable_key": "chapter:1", "node_type": "chapter", "title": "真实章节", "parent_key": "work"},
            {"stable_key": "scene:1", "node_type": "scene", "title": "真实场景", "parent_key": "chapter:1"},
        ]}))
        registry.register(TypedAgent("event_causality_agent", {"edges": [
            {"source_key": "chapter:1", "target_key": "scene:1", "edge_type": "contains", "confidence": 0.91}
        ]}))
        style_dimensions = {
            name: {"state": "not_observed", "value": None, "confidence": 0.0, "evidence_refs": []}
            for name in BLUEPRINT_DIMENSIONS
        }
        style_dimensions["style_statistics"] = {
            "state": "observed", "value": {"rare_phrases": ["仅安全代理可读指纹"]},
            "confidence": 0.93, "evidence_refs": ["style-evidence"],
        }
        registry.register(TypedAgent("style_fingerprint_agent", {"dimensions": style_dimensions}))
        registry.register(TypedAgent("hierarchy_synthesis_agent", {
            "dimensions": {name: {"state": "not_observed", "value": None, "confidence": 0.0,
                                  "evidence_refs": []} for name in BLUEPRINT_DIMENSIONS},
            "nodes": [{"stable_key": "work", "node_type": "work", "title": "真实作品"}],
        }))
        registry.register(TypedAgent("interpretation_conflict_agent", {
            "interpretations": [
                {"stable_key": "chapter:1", "dimension": "narrative_function",
                 "value": {"explanation": "解释甲"}, "confidence": 0.7, "conflict_group_id": "cg:1"},
                {"stable_key": "chapter:1", "dimension": "narrative_function",
                 "value": {"explanation": "解释乙"}, "confidence": 0.6, "conflict_group_id": "cg:1"},
            ],
            "conflicts": [{"conflict_group_id": "cg:1", "relation_type": "mutually_exclusive",
                           "interpretation_indexes": [0, 1]}],
        }))

        result = BlueprintOrchestrator(self.database, registry).run_job(self.project["id"], job["id"])
        blueprint = result["blueprint"]

        self.assertEqual([node["stable_key"] for node in blueprint["nodes"]],
                         ["work", "batch:0:chapter:1", "batch:0:scene:1"])
        self.assertEqual(blueprint["nodes"][2]["parent_key"], "batch:0:chapter:1")
        self.assertEqual(blueprint["edges"][0]["edge_type"], "contains")
        self.assertEqual(len(blueprint["interpretations"]), 2)
        self.assertEqual(blueprint["conflicts"][0]["conflict_group_id"], "cg:1")
        self.assertEqual(
            blueprint["dimensions"]["style_statistics"]["value"]["rare_phrases"],
            ["仅安全代理可读指纹"],
        )


    def test_multibatch_explicit_global_hierarchy_merges_shared_nodes_once(self) -> None:
        text = "\n".join(f"Chapter {index} " + ("storm consequence " * 900)
                         for index in range(1, 5))
        job = self.create_job(text, "reference:multibatch-hierarchy")
        registry = DeterministicAgentRegistry()

        class TypedAgent:
            output_schema = "creative-claw.blueprint-agent.v1"

            def __init__(self, name, factory):
                self.name = name
                self.factory = factory

            def run(self, task):
                return AgentResult(data=self.factory(task), confidence=0.9,
                                   model={"provider": "test"})

        registry.register(TypedAgent("segmentation_agent", lambda task: {"nodes": [
            {"stable_key": "chapter:shared", "key_scope": "global", "node_type": "chapter",
             "title": "Segment chapter", "parent_key": "work", "start": 0, "end": len(task.context["text"])},
            {"stable_key": "scene:shared", "key_scope": "global", "node_type": "scene",
             "title": "Segment scene", "parent_key": "chapter:shared", "start": 0, "end": len(task.context["text"])},
        ]}))
        registry.register(TypedAgent("event_causality_agent", lambda _task: {"edges": [
            {"source_key": "chapter:shared", "target_key": "scene:shared",
             "edge_type": "contains", "confidence": 0.91},
        ]}))
        def hierarchy_factory(task):
            dimensions = {name: {"state": "not_observed", "value": None,
                                 "confidence": 0.0, "evidence_refs": []}
                          for name in BLUEPRINT_DIMENSIONS}
            stage = task.context.get("synthesis_stage", "chapter")
            nodes = [{"stable_key": "work", "key_scope": "global",
                      "node_type": "work", "title": "Hierarchy work"}]
            if stage != "work":
                nodes.append({"stable_key": "volume:1", "key_scope": "global",
                              "node_type": "volume", "title": "Hierarchy volume",
                              "parent_key": "work"})
            if stage == "chapter":
                nodes.extend([
                    {"stable_key": "chapter:shared", "key_scope": "global",
                     "node_type": "chapter", "title": "Hierarchy chapter",
                     "summary": "Enriched chapter", "parent_key": "volume:1"},
                    {"stable_key": "scene:shared", "key_scope": "global",
                     "node_type": "scene", "title": "Hierarchy scene",
                     "parent_key": "chapter:shared"},
                    {"stable_key": "beat:shared", "key_scope": "global",
                     "node_type": "beat", "title": "Hierarchy beat",
                     "parent_key": "scene:shared"},
                ])
            return {"dimensions": dimensions, "nodes": nodes}

        registry.register(TypedAgent("hierarchy_synthesis_agent", hierarchy_factory))
        registry.register(TypedAgent("interpretation_conflict_agent", lambda task: {
            "interpretations": [{
                "stable_key": "chapter:shared", "dimension": "narrative_function",
                "value": {"variant": task.batch_id}, "confidence": 0.7,
                "conflict_group_id": f"conflict:{task.batch_id}",
            }],
            "conflicts": [{
                "conflict_group_id": f"conflict:{task.batch_id}",
                "relation_type": "alternative", "interpretation_indexes": [0],
            }],
        }))

        result = BlueprintOrchestrator(self.database, registry).run_job(
            self.project["id"], job["id"]
        )
        self.assertEqual(result["status"], "completed", result)
        blueprint = result["blueprint"]
        by_key = {node["stable_key"]: node for node in blueprint["nodes"]}
        batch_count = len(self.blueprints.list_batches(self.project["id"], job["id"]))

        self.assertGreater(batch_count, 1)
        self.assertEqual(by_key["chapter:shared"]["title"], "Hierarchy chapter")
        self.assertEqual(by_key["chapter:shared"]["summary"], "Enriched chapter")
        self.assertEqual(by_key["chapter:shared"]["parent_key"], "volume:1")
        self.assertEqual(by_key["chapter:shared"]["source_locator"]["start"], 0)
        self.assertEqual(by_key["beat:shared"]["parent_key"], "scene:shared")
        self.assertEqual(
            [item["interpretation_indexes"] for item in blueprint["conflicts"]],
            [[0]],
        )
        self.assertEqual(len(blueprint["interpretations"]), 1)

    def _run_invalid_hierarchy(self, key: str, nodes: list[dict]) -> dict:
        job = self.create_job("Hierarchy validation source.", key)
        registry = DeterministicAgentRegistry()

        class InvalidHierarchyAgent:
            name = "hierarchy_synthesis_agent"
            output_schema = "creative-claw.blueprint-agent.v1"

            def run(self, _task):
                dimensions = {
                    name: {"state": "not_observed", "value": None,
                           "confidence": 0.0, "evidence_refs": []}
                    for name in BLUEPRINT_DIMENSIONS
                }
                return AgentResult(data={"dimensions": dimensions, "nodes": nodes}, confidence=0.9)

        registry.register(InvalidHierarchyAgent())
        return BlueprintOrchestrator(self.database, registry).run_job(self.project["id"], job["id"])

    def test_global_hierarchy_rejects_orphan_parent(self) -> None:
        result = self._run_invalid_hierarchy("reference:orphan", [
            {"stable_key": "work", "node_type": "work"},
            {"stable_key": "chapter:orphan", "node_type": "chapter", "parent_key": "volume:missing"},
        ])
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["error"]["category"], "hierarchy_invalid")

    def test_global_hierarchy_rejects_cycle(self) -> None:
        result = self._run_invalid_hierarchy("reference:cycle", [
            {"stable_key": "work", "node_type": "work"},
            {"stable_key": "volume:cycle", "node_type": "volume", "parent_key": "chapter:cycle"},
            {"stable_key": "chapter:cycle", "node_type": "chapter", "parent_key": "volume:cycle"},
        ])
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["error"]["category"], "hierarchy_invalid")

    def test_global_hierarchy_rejects_illegal_parent_type(self) -> None:
        result = self._run_invalid_hierarchy("reference:illegal-parent", [
            {"stable_key": "work", "node_type": "work"},
            {"stable_key": "scene:illegal", "node_type": "scene", "parent_key": "work"},
        ])
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["error"]["category"], "hierarchy_invalid")

    def test_multibatch_reused_local_keys_are_namespaced_and_all_references_remapped(self) -> None:
        text = "x" * 26_000
        job = self.create_job(text, "reference:canonical-batch-keys")
        registry = DeterministicAgentRegistry()

        class ScopedAgent:
            output_schema = "creative-claw.blueprint-agent.v1"

            def __init__(self, name):
                self.name = name

            def run(self, task):
                stage = task.context.get("synthesis_stage", "chapter")
                dimensions = {
                    name: {"state": "not_observed", "value": None,
                           "confidence": 0.0, "evidence_refs": []}
                    for name in BLUEPRINT_DIMENSIONS
                }
                data = {"dimensions": dimensions}
                if self.name == "segmentation_agent":
                    data = {"nodes": [
                        {"stable_key": "chapter:1", "key_scope": "batch", "node_type": "chapter",
                         "parent_key": "volume:shared", "start": 0, "end": len(task.context["text"])},
                        {"stable_key": "scene:1", "key_scope": "batch", "node_type": "scene",
                         "parent_key": "chapter:1", "start": 0, "end": len(task.context["text"])},
                    ]}
                elif self.name == "event_causality_agent":
                    data["edges"] = [{"source_key": "chapter:1", "target_key": "scene:1",
                                      "edge_type": "contains", "confidence": 0.9}]
                elif self.name == "hierarchy_synthesis_agent":
                    if stage == "volume":
                        data["nodes"] = [
                            {"stable_key": "work", "key_scope": "global", "node_type": "work"},
                            {"stable_key": "volume:shared", "key_scope": "global",
                             "node_type": "volume", "parent_key": "work"},
                        ]
                    elif stage == "work":
                        data["nodes"] = [
                            {"stable_key": "work", "key_scope": "global", "node_type": "work"}
                        ]
                    else:
                        data["nodes"] = [
                            {"stable_key": "work", "key_scope": "global", "node_type": "work"},
                            {"stable_key": "volume:shared", "key_scope": "global",
                             "node_type": "volume", "parent_key": "work"},
                            {"stable_key": "chapter:1", "key_scope": "batch", "node_type": "chapter",
                             "parent_key": "volume:shared"},
                            {"stable_key": "scene:1", "key_scope": "batch", "node_type": "scene",
                             "parent_key": "chapter:1"},
                            {"stable_key": "beat:1", "key_scope": "batch", "node_type": "beat",
                             "parent_key": "scene:1"},
                        ]
                elif self.name == "interpretation_conflict_agent":
                    canonical = list(task.context.get("canonical_nodes") or [])
                    chapter_keys = [node["stable_key"] for node in canonical
                                    if node.get("node_type") == "chapter"]
                    if not chapter_keys:
                        chapter_keys = ["chapter:1"]
                    data["interpretations"] = [
                        {"stable_key": key, "dimension": "narrative_function",
                         "value": {"variant": key}, "confidence": 0.7,
                         "conflict_group_id": f"conflict:{key}"}
                        for key in chapter_keys
                    ]
                    data["conflicts"] = [
                        {"conflict_group_id": f"conflict:{key}", "relation_type": "alternative",
                         "interpretation_indexes": [index]}
                        for index, key in enumerate(chapter_keys)
                    ]
                return AgentResult(data=data, confidence=0.9, model={"provider": "test"})

        for name in ("segmentation_agent", "event_causality_agent",
                     "hierarchy_synthesis_agent", "interpretation_conflict_agent"):
            registry.register(ScopedAgent(name))

        result = BlueprintOrchestrator(self.database, registry).run_job(
            self.project["id"], job["id"]
        )
        blueprint = result["blueprint"]
        by_type = {
            node_type: [node for node in blueprint["nodes"] if node["node_type"] == node_type]
            for node_type in ("volume", "chapter", "scene", "beat")
        }

        self.assertEqual(len(self.blueprints.list_batches(self.project["id"], job["id"])), 3)
        self.assertEqual({name: len(nodes) for name, nodes in by_type.items()},
                         {"volume": 1, "chapter": 3, "scene": 3, "beat": 3})
        self.assertEqual(
            {node["stable_key"] for node in by_type["chapter"]},
            {"batch:0:chapter:1", "batch:1:chapter:1", "batch:2:chapter:1"},
        )
        self.assertTrue(all(node["parent_key"] == "volume:shared" for node in by_type["chapter"]))
        self.assertEqual(
            {(edge["source_key"], edge["target_key"]) for edge in blueprint["edges"]},
            {(f"batch:{index}:chapter:1", f"batch:{index}:scene:1") for index in range(3)},
        )
        self.assertEqual(
            {item["stable_key"] for item in blueprint["interpretations"]},
            {f"batch:{index}:chapter:1" for index in range(3)},
        )
        self.assertEqual(len({item["conflict_group_id"] for item in blueprint["conflicts"]}), 3)

    def _run_cross_run_declaration_conflict(
        self, key: str, segmentation_node: dict, hierarchy_node: dict
    ) -> dict:
        job = self.create_job("Cross-run declaration source.", key)
        registry = DeterministicAgentRegistry()

        class DeclarationAgent:
            output_schema = "creative-claw.blueprint-agent.v1"

            def __init__(self, name):
                self.name = name

            def run(self, _task):
                if self.name == "segmentation_agent":
                    data = {"nodes": [segmentation_node]}
                else:
                    dimensions = {
                        name: {"state": "not_observed", "value": None,
                               "confidence": 0.0, "evidence_refs": []}
                        for name in BLUEPRINT_DIMENSIONS
                    }
                    data = {"dimensions": dimensions, "nodes": [
                        {"stable_key": "work", "key_scope": "global", "node_type": "work"},
                        hierarchy_node,
                    ]}
                return AgentResult(data=data, confidence=0.9, model={"provider": "test"})

        registry.register(DeclarationAgent("segmentation_agent"))
        registry.register(DeclarationAgent("hierarchy_synthesis_agent"))
        return BlueprintOrchestrator(self.database, registry).run_job(
            self.project["id"], job["id"]
        )

    def test_cross_run_scope_conflict_blocks_as_invalid_hierarchy(self) -> None:
        result = self._run_cross_run_declaration_conflict(
            "reference:scope-conflict",
            {"stable_key": "chapter:collision", "key_scope": "batch",
             "node_type": "chapter", "parent_key": "work"},
            {"stable_key": "chapter:collision", "key_scope": "global",
             "node_type": "chapter", "parent_key": "work"},
        )

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["error"]["category"], "hierarchy_invalid")

    def test_cross_run_type_conflict_blocks_as_invalid_hierarchy(self) -> None:
        result = self._run_cross_run_declaration_conflict(
            "reference:type-conflict",
            {"stable_key": "unit:collision", "key_scope": "batch",
             "node_type": "chapter", "parent_key": "work"},
            {"stable_key": "unit:collision", "key_scope": "batch",
             "node_type": "episode", "parent_key": "work"},
        )

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["error"]["category"], "hierarchy_invalid")

    def test_global_node_parent_conflict_across_hierarchy_runs_is_rejected(self) -> None:
        job = self.create_job("x" * 26_000, "reference:parent-conflict")
        registry = DeterministicAgentRegistry()

        class EmptySegmentation:
            name = "segmentation_agent"
            output_schema = "creative-claw.blueprint-agent.v1"

            def run(self, _task):
                return AgentResult(data={"nodes": []}, confidence=0.9)

        class EmptyEdges:
            name = "event_causality_agent"
            output_schema = "creative-claw.blueprint-agent.v1"

            def run(self, _task):
                dimensions = {
                    name: {"state": "not_observed", "value": None,
                           "confidence": 0.0, "evidence_refs": []}
                    for name in BLUEPRINT_DIMENSIONS
                }
                return AgentResult(data={"dimensions": dimensions, "edges": []}, confidence=0.9)

        class ConflictingParents:
            name = "hierarchy_synthesis_agent"
            output_schema = "creative-claw.blueprint-agent.v1"
            chapter_calls = 0

            def run(self, task):
                dimensions = {
                    name: {"state": "not_observed", "value": None,
                           "confidence": 0.0, "evidence_refs": []}
                    for name in BLUEPRINT_DIMENSIONS
                }
                stage = task.context.get("synthesis_stage", "chapter")
                nodes = [
                    {"stable_key": "work", "key_scope": "global", "node_type": "work"},
                ]
                if stage != "work":
                    nodes.extend([
                        {"stable_key": "volume:a", "key_scope": "global",
                         "node_type": "volume", "parent_key": "work"},
                        {"stable_key": "volume:b", "key_scope": "global",
                         "node_type": "volume", "parent_key": "work"},
                    ])
                if stage == "chapter":
                    self.chapter_calls += 1
                    nodes.append({
                        "stable_key": "chapter:shared", "key_scope": "global",
                        "node_type": "chapter",
                        "parent_key": "volume:a" if self.chapter_calls == 1 else "volume:b",
                    })
                return AgentResult(data={"dimensions": dimensions, "nodes": nodes}, confidence=0.9)

        registry.register(EmptySegmentation())
        registry.register(EmptyEdges())
        registry.register(ConflictingParents())

        result = BlueprintOrchestrator(self.database, registry).run_job(
            self.project["id"], job["id"]
        )

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["error"]["category"], "hierarchy_invalid")

    def test_global_conflict_stage_cannot_use_ambiguous_raw_batch_key(self) -> None:
        job = self.create_job("x" * 26_000, "reference:ambiguous-global-reference")
        registry = DeterministicAgentRegistry()
        successful = registry.get("interpretation_conflict_agent")

        class AmbiguousConflict:
            name = "interpretation_conflict_agent"
            output_schema = "creative-claw.blueprint-agent.v1"

            def run(self, task):
                result = successful.run(task)
                result.data["interpretations"][0]["stable_key"] = "chapter:1"
                return result

        registry.register(AmbiguousConflict())

        result = BlueprintOrchestrator(self.database, registry).run_job(
            self.project["id"], job["id"]
        )

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["error"]["category"], "hierarchy_invalid")

    def test_global_hierarchy_accepts_phase_episode_branch(self) -> None:
        result = self._run_invalid_hierarchy("reference:phase-episode", [
            {"stable_key": "work", "key_scope": "global", "node_type": "work"},
            {"stable_key": "phase:1", "key_scope": "batch", "node_type": "phase",
             "parent_key": "work"},
            {"stable_key": "episode:1", "key_scope": "batch", "node_type": "episode",
             "parent_key": "phase:1"},
            {"stable_key": "scene:1", "key_scope": "batch", "node_type": "scene",
             "parent_key": "episode:1"},
            {"stable_key": "beat:1", "key_scope": "batch", "node_type": "beat",
             "parent_key": "scene:1"},
        ])

        self.assertEqual(result["status"], "completed")
        by_key = {node["stable_key"]: node for node in result["blueprint"]["nodes"]}
        self.assertEqual(by_key["batch:0:phase:1"]["parent_key"], "work")
        self.assertEqual(by_key["batch:0:episode:1"]["parent_key"], "batch:0:phase:1")
        self.assertEqual(by_key["batch:0:scene:1"]["parent_key"], "batch:0:episode:1")
        self.assertEqual(by_key["batch:0:beat:1"]["parent_key"], "batch:0:scene:1")

    def test_global_hierarchy_accepts_short_work_episode_branch(self) -> None:
        result = self._run_invalid_hierarchy("reference:work-episode", [
            {"stable_key": "work", "key_scope": "global", "node_type": "work"},
            {"stable_key": "episode:1", "key_scope": "batch", "node_type": "episode",
             "parent_key": "work"},
            {"stable_key": "scene:1", "key_scope": "batch", "node_type": "scene",
             "parent_key": "episode:1"},
            {"stable_key": "beat:1", "key_scope": "batch", "node_type": "beat",
             "parent_key": "scene:1"},
        ])

        self.assertEqual(result["status"], "completed")
        by_key = {node["stable_key"]: node for node in result["blueprint"]["nodes"]}
        self.assertEqual(by_key["batch:0:episode:1"]["parent_key"], "work")
        self.assertEqual(by_key["batch:0:scene:1"]["parent_key"], "batch:0:episode:1")
        self.assertEqual(by_key["batch:0:beat:1"]["parent_key"], "batch:0:scene:1")

    def test_long_job_runs_chapter_volume_work_then_final_conflict_with_safe_global_contexts(self) -> None:
        sentinel = "REFERENCE_STAGE_SENTINEL_MUST_NOT_ENTER_GLOBAL_CONTEXT"
        job = self.create_job((sentinel + "\n") + ("x" * 26_000), "reference:staged-barriers")
        registry = DeterministicAgentRegistry()

        result = BlueprintOrchestrator(self.database, registry).run_job(self.project["id"], job["id"])
        staged = [
            (item["agent"], item["context"].get("synthesis_stage"))
            for item in registry.inputs
            if item["agent"] in {"hierarchy_synthesis_agent", "interpretation_conflict_agent"}
        ]
        global_contexts = [
            item["context"] for item in registry.inputs
            if item["context"].get("synthesis_stage") in {"volume", "work", "conflict"}
        ]

        self.assertEqual(result["status"], "completed")
        self.assertEqual(staged, [
            ("hierarchy_synthesis_agent", "chapter"),
            ("hierarchy_synthesis_agent", "chapter"),
            ("hierarchy_synthesis_agent", "chapter"),
            ("hierarchy_synthesis_agent", "volume"),
            ("hierarchy_synthesis_agent", "work"),
            ("interpretation_conflict_agent", "conflict"),
        ])
        self.assertEqual(len(global_contexts), 3)
        forbidden_keys = {"text", "quote", "rare_phrases", "style_fingerprints",
                          "raw_agent_response"}

        def nested_keys(value):
            if isinstance(value, dict):
                return set(value).union(*(nested_keys(item) for item in value.values()))
            if isinstance(value, list):
                return set().union(*(nested_keys(item) for item in value))
            return set()

        for context in global_contexts:
            serialized = repr(context)
            self.assertNotIn(sentinel, serialized)
            self.assertTrue({"synthesis_stage", "canonical_nodes", "lower_level_summaries",
                             "evidence_metadata"}.issubset(context))
            self.assertTrue(forbidden_keys.isdisjoint(nested_keys(context)))

    def test_partial_batches_never_start_global_synthesis_or_conflict(self) -> None:
        job = self.create_job("x" * 26_000, "reference:partial-no-global-stage")
        registry = DeterministicAgentRegistry()

        result = BlueprintOrchestrator(self.database, registry).run_job(
            self.project["id"], job["id"], max_batches=1
        )

        self.assertEqual(result["status"], "resumable")
        self.assertNotIn("interpretation_conflict_agent", registry.calls)
        self.assertFalse(any(
            item["context"].get("synthesis_stage") in {"volume", "work", "conflict"}
            for item in registry.inputs
        ))

    def test_pause_after_volume_stage_prevents_work_and_resumes_without_repeating_volume(self) -> None:
        job = self.create_job("x" * 26_000, "reference:pause-after-volume")
        registry = DeterministicAgentRegistry()
        successful = registry.get("hierarchy_synthesis_agent")
        repository = self.blueprints
        project_id = self.project["id"]

        class PauseAfterVolume:
            name = "hierarchy_synthesis_agent"
            output_schema = "creative-claw.blueprint-agent.v1"

            def run(self, task):
                result = successful.run(task)
                if task.context.get("synthesis_stage") == "volume":
                    repository.set_job_desired_state(project_id, task.job_id, "paused")
                return result

        registry.register(PauseAfterVolume())
        orchestrator = BlueprintOrchestrator(self.database, registry)

        paused = orchestrator.run_job(project_id, job["id"])
        first_stages = [item["context"].get("synthesis_stage") for item in registry.inputs]
        self.assertEqual(paused["status"], "paused")
        self.assertIn("volume", first_stages)
        self.assertNotIn("work", first_stages)
        self.assertNotIn("conflict", first_stages)

        repository.set_job_desired_state(project_id, job["id"], "running")
        completed = orchestrator.run_job(project_id, job["id"])
        final_stages = [item["context"].get("synthesis_stage") for item in registry.inputs]
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(final_stages.count("volume"), 1)
        self.assertEqual(final_stages.count("work"), 1)
        self.assertEqual(final_stages.count("conflict"), 1)

    def test_cancel_after_volume_stage_prevents_later_global_stages(self) -> None:
        job = self.create_job("x" * 26_000, "reference:cancel-after-volume")
        registry = DeterministicAgentRegistry()
        successful = registry.get("hierarchy_synthesis_agent")
        repository = self.blueprints
        project_id = self.project["id"]

        class CancelAfterVolume:
            name = "hierarchy_synthesis_agent"
            output_schema = "creative-claw.blueprint-agent.v1"

            def run(self, task):
                result = successful.run(task)
                if task.context.get("synthesis_stage") == "volume":
                    repository.set_job_desired_state(project_id, task.job_id, "cancelled")
                return result

        registry.register(CancelAfterVolume())

        result = BlueprintOrchestrator(self.database, registry).run_job(project_id, job["id"])
        stages = [item["context"].get("synthesis_stage") for item in registry.inputs]

        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(stages.count("volume"), 1)
        self.assertNotIn("work", stages)
        self.assertNotIn("conflict", stages)

    def test_retryable_work_stage_uses_new_attempt_without_repeating_completed_volume(self) -> None:
        job = self.create_job("x" * 26_000, "reference:retry-work-stage")
        registry = DeterministicAgentRegistry()
        successful = registry.get("hierarchy_synthesis_agent")

        class FailOnceWork:
            name = "hierarchy_synthesis_agent"
            output_schema = "creative-claw.blueprint-agent.v1"
            attempts = 0

            def run(self, task):
                if task.context.get("synthesis_stage") == "work":
                    self.attempts += 1
                    if self.attempts == 1:
                        raise TimeoutError("temporary work synthesis timeout")
                return successful.run(task)

        registry.register(FailOnceWork())
        orchestrator = BlueprintOrchestrator(self.database, registry)

        first = orchestrator.run_job(self.project["id"], job["id"])
        self.assertEqual(first["status"], "blocked")
        self.blueprints.set_job_desired_state(self.project["id"], job["id"], "running")
        second = orchestrator.run_job(self.project["id"], job["id"])
        stage_inputs = [item["context"].get("synthesis_stage") for item in registry.inputs]
        work_runs = [
            run for run in self.blueprints.list_agent_runs(self.project["id"], job["id"])
            if run["prompt_version"] == "prompt-v1:synthesis:work"
        ]

        self.assertEqual(second["status"], "completed")
        self.assertEqual(stage_inputs.count("volume"), 1)
        self.assertEqual([run["status"] for run in work_runs], ["retryable_failed", "completed"])
        self.assertNotEqual(work_runs[0]["idempotency_key"], work_runs[1]["idempotency_key"])

    def test_global_hierarchy_reuses_existing_evidence_and_merges_typed_dimensions(self) -> None:
        job = self.create_job("x" * 26_000, "reference:global-existing-evidence")
        registry = DeterministicAgentRegistry()
        successful = registry.get("hierarchy_synthesis_agent")
        stage_contexts = {}

        def stage_dimensions(stage, evidence_ref, state="observed"):
            dimensions = {
                name: {"state": "not_observed", "value": None,
                       "confidence": 0.0, "evidence_refs": []}
                for name in BLUEPRINT_DIMENSIONS
            }
            dimensions["narrative_function"] = {
                "state": state,
                "value": {"mechanism_class": f"{stage}-synthesis"},
                "confidence": 0.81 if stage == "volume" else 0.93,
                "evidence_refs": [evidence_ref],
            }
            return dimensions

        class EvidenceAwareHierarchy:
            name = "hierarchy_synthesis_agent"
            output_schema = "creative-claw.blueprint-agent.v1"

            def run(self, task):
                result = successful.run(task)
                stage = task.context.get("synthesis_stage", "chapter")
                if stage == "chapter":
                    evidence_ref = result.evidence[0]["id"]
                    dimensions = stage_dimensions("chapter", evidence_ref, "uncertain")
                    result.data["dimensions"] = dimensions
                    for node in result.data["nodes"]:
                        if node["node_type"] == "chapter":
                            node["dimensions"] = dimensions
                    return result

                stage_contexts[stage] = task.context
                evidence_ref = task.context["blueprint_dimensions"]["narrative_function"][
                    "evidence_refs"
                ][0]
                dimensions = stage_dimensions(stage, evidence_ref)
                result.data["dimensions"] = dimensions
                for node in result.data["nodes"]:
                    if node["node_type"] == stage:
                        node["dimensions"] = dimensions
                    if node["node_type"] == "work":
                        node["dimensions"] = dimensions
                result.evidence = []
                return result

        registry.register(EvidenceAwareHierarchy())

        result = BlueprintOrchestrator(self.database, registry).run_job(
            self.project["id"], job["id"]
        )

        self.assertEqual(result["status"], "completed", result)
        blueprint = result["blueprint"]
        final_ids = [item["id"] for item in blueprint["evidence"]]
        original_ids = [item["id"] for item in stage_contexts["volume"]["evidence_metadata"]]
        self.assertEqual(final_ids, original_ids)
        self.assertEqual(len(final_ids), len(set(final_ids)))

        top = blueprint["dimensions"]["narrative_function"]
        by_key = {node["stable_key"]: node for node in blueprint["nodes"]}
        volume = by_key["volume:1"]["dimensions"]["narrative_function"]
        work = by_key["work"]["dimensions"]["narrative_function"]
        self.assertEqual(top["value"], {"mechanism_class": "work-synthesis"})
        self.assertEqual(top["confidence"], 0.93)
        self.assertEqual(work, top)
        self.assertEqual(volume["value"], {"mechanism_class": "volume-synthesis"})
        self.assertEqual(set(top["evidence_refs"] + volume["evidence_refs"]), {top["evidence_refs"][0]})
        self.assertIn(top["evidence_refs"][0], final_ids)

        forbidden = {"text", "quote", "rare_phrases", "style_fingerprints",
                     "raw_response", "raw_agent_response"}

        def nested_keys(value):
            if isinstance(value, dict):
                return set(value).union(*(nested_keys(item) for item in value.values()))
            if isinstance(value, list):
                return set().union(*(nested_keys(item) for item in value))
            return set()

        for context in stage_contexts.values():
            self.assertIn("blueprint_dimensions", context)
            self.assertEqual(set(context["blueprint_dimensions"]), set(BLUEPRINT_DIMENSIONS))
            self.assertTrue(forbidden.isdisjoint(nested_keys(context)))
            for dimension in context["blueprint_dimensions"].values():
                self.assertEqual(set(dimension), {"state", "value", "confidence", "evidence_refs"})
            for node in context["lower_level_summaries"]:
                self.assertEqual(set(node["dimensions"]), set(BLUEPRINT_DIMENSIONS))
                for dimension in node["dimensions"].values():
                    self.assertEqual(
                        set(dimension), {"state", "value", "confidence", "evidence_refs"}
                    )

    def test_global_context_recursively_redacts_reference_derived_payloads(self) -> None:
        sentinel = "GLOBAL_CONTEXT_REFERENCE_SENTINEL_R8Q4"
        source_text = sentinel + "\n" + ("x" * 26_000)
        source_hash = sha256_text(source_text)
        job = self.create_job(source_text, "reference:global-context-redaction")
        registry = DeterministicAgentRegistry()
        successful = registry.get("hierarchy_synthesis_agent")

        def injected_dimensions(evidence_ref):
            dimensions = {
                name: {"state": "not_observed", "value": None,
                       "confidence": 0.0, "evidence_refs": []}
                for name in BLUEPRINT_DIMENSIONS
            }
            dimensions["narrative_function"] = {
                "state": "observed", "value": sentinel,
                "confidence": 0.86, "evidence_refs": [evidence_ref],
            }
            dimensions["causality"] = {
                "state": "observed",
                "value": {"mechanism_class": sentinel, "safe_class": "costly_choice",
                          "count": 7, "enabled": True},
                "confidence": 0.87, "evidence_refs": [evidence_ref],
            }
            dimensions["emotion_kline"] = {
                "state": "uncertain", "value": {"Quote": sentinel, "safe_level": 3},
                "confidence": 0.71, "evidence_refs": [evidence_ref],
            }
            dimensions["style_statistics"] = {
                "state": "observed",
                "value": {"RAW-Agent-Response": sentinel,
                          "Style_Fingerprints": sentinel, "safe_flag": False},
                "confidence": 0.68, "evidence_refs": [evidence_ref],
            }
            return dimensions

        class ReferenceEchoHierarchy:
            name = "hierarchy_synthesis_agent"
            output_schema = "creative-claw.blueprint-agent.v1"

            def run(self, task):
                result = successful.run(task)
                if task.context.get("synthesis_stage") != "chapter":
                    return result
                evidence_ref = result.evidence[0]["id"]
                dimensions = injected_dimensions(evidence_ref)
                result.data["dimensions"] = dimensions
                for node in result.data["nodes"]:
                    if node["node_type"] == "chapter":
                        node["title"] = sentinel
                        node["summary"] = {
                            "nested": {"Quote": sentinel, "safe_class": "costly_choice"}
                        }
                        node["dimensions"] = dimensions
                    if node["node_type"] == "beat":
                        node["source_locator"] = {"text": sentinel, "line": 7}
                return result

        registry.register(ReferenceEchoHierarchy())

        result = BlueprintOrchestrator(self.database, registry).run_job(
            self.project["id"], job["id"]
        )
        global_contexts = [
            item["context"] for item in registry.inputs
            if item["context"].get("synthesis_stage") in {"volume", "work", "conflict"}
        ]
        global_runs = [
            run for run in self.blueprints.list_agent_runs(self.project["id"], job["id"])
            if run["batch_id"] is None
        ]

        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(len(global_contexts), 3)

        forbidden = {"text", "quote", "rare_phrases", "style_fingerprints",
                     "raw_response", "raw_agent_response"}

        def normalized_key(value):
            return re.sub(r"[^a-z0-9]+", "_", str(value).casefold()).strip("_")

        def nested_keys(value):
            if isinstance(value, dict):
                return {normalized_key(name) for name in value}.union(
                    *(nested_keys(item) for item in value.values())
                )
            if isinstance(value, list):
                return set().union(*(nested_keys(item) for item in value))
            return set()

        for context in global_contexts:
            serialized = repr(context)
            self.assertNotIn(source_text, serialized)
            self.assertNotIn(sentinel, serialized)
            self.assertNotIn(source_hash, serialized)
            self.assertTrue(forbidden.isdisjoint(nested_keys(context)))
            causality = context["blueprint_dimensions"]["causality"]
            self.assertEqual(causality["state"], "observed")
            self.assertEqual(causality["confidence"], 0.87)
            self.assertTrue(causality["evidence_refs"])
            self.assertEqual(causality["value"]["safe_class"], "costly_choice")
            self.assertEqual(causality["value"]["count"], 7)
            self.assertIs(causality["value"]["enabled"], True)
            self.assertEqual(
                context["blueprint_dimensions"]["style_statistics"]["value"]["safe_flag"],
                False,
            )
            chapter = next(
                node for node in context["canonical_nodes"] if node["node_type"] == "chapter"
            )
            self.assertNotEqual(chapter["title"], sentinel)
            self.assertEqual(chapter["summary"]["nested"]["safe_class"], "costly_choice")
            beat = next(node for node in context["canonical_nodes"] if node["node_type"] == "beat")
            self.assertEqual(beat["source_locator"], {"line": 7})

        for run in global_runs:
            visible = {name: value for name, value in run.items() if name != "input_hash"}
            serialized = repr(visible)
            self.assertNotIn(source_text, serialized)
            self.assertNotIn(sentinel, serialized)
            self.assertNotIn(source_hash, serialized)
            self.assertNotEqual(run["input_hash"], source_hash)

    def test_global_context_redacts_wrapped_source_ngrams_and_forbidden_key_variants(self) -> None:
        english_sentinel = "EN_SENTINEL_R9Q4"
        chinese_sentinel = "星河密令"
        source_text = f"{english_sentinel}\n{chinese_sentinel}\n" + ("x" * 26_000)
        job = self.create_job(source_text, "reference:global-context-ngram-redaction")
        registry = DeterministicAgentRegistry()
        successful = registry.get("hierarchy_synthesis_agent")

        def injected_dimensions(evidence_ref):
            dimensions = {
                name: {"state": "not_observed", "value": None,
                       "confidence": 0.0, "evidence_refs": []}
                for name in BLUEPRINT_DIMENSIONS
            }
            dimensions["narrative_function"] = {
                "state": "observed", "value": f"abstract: {english_sentinel} suffix",
                "confidence": 0.88, "evidence_refs": [evidence_ref],
            }
            dimensions["causality"] = {
                "state": "observed",
                "value": {
                    "mechanism_class": f"prefix-{english_sentinel}-suffix",
                    "safe_class": "costly_choice", "count": 11, "enabled": True,
                    "Reference-Text": english_sentinel,
                    "SOURCE_TEXTS": english_sentinel,
                    "Rare-Phrase": chinese_sentinel,
                    "RARE_PHRASES": chinese_sentinel,
                    "Style-Fingerprint": english_sentinel,
                    "STYLE_FINGERPRINTS": english_sentinel,
                    "Passage": chinese_sentinel,
                    "PASSAGES": chinese_sentinel,
                    "Reference_Passage": english_sentinel,
                    "SOURCE-PASSAGES": english_sentinel,
                    "RAW-MODEL-RESPONSE": english_sentinel,
                    "Raw_Model_Responses": english_sentinel,
                    "Fingerprint": english_sentinel,
                    "FINGERPRINTS": english_sentinel,
                },
                "confidence": 0.89, "evidence_refs": [evidence_ref],
            }
            return dimensions

        class WrappedEchoHierarchy:
            name = "hierarchy_synthesis_agent"
            output_schema = "creative-claw.blueprint-agent.v1"

            def run(self, task):
                result = successful.run(task)
                if task.context.get("synthesis_stage") != "chapter":
                    return result
                evidence_ref = result.evidence[0]["id"]
                dimensions = injected_dimensions(evidence_ref)
                result.data["dimensions"] = dimensions
                for node in result.data["nodes"]:
                    if node["node_type"] == "chapter":
                        node["title"] = f"title::{english_sentinel}::wrapped"
                        node["summary"] = f"摘要：{chinese_sentinel}（抽象包装）"
                        node["dimensions"] = dimensions
                    if node["node_type"] == "beat":
                        node["source_locator"] = {
                            "description": f"locator {english_sentinel} wrapper",
                            "Reference-Text": chinese_sentinel,
                            "line": 9,
                        }
                return result

        registry.register(WrappedEchoHierarchy())

        result = BlueprintOrchestrator(self.database, registry).run_job(
            self.project["id"], job["id"]
        )
        global_contexts = [
            item["context"] for item in registry.inputs
            if item["context"].get("synthesis_stage") in {"volume", "work", "conflict"}
        ]

        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(len(global_contexts), 3)
        forbidden = {
            "reference", "references", "source", "sources",
            "reference_text", "reference_texts", "source_text", "source_texts",
            "rare_phrase", "rare_phrases", "style_fingerprint", "style_fingerprints",
            "passage", "passages", "reference_passage", "reference_passages",
            "source_passage", "source_passages", "raw_model_response",
            "raw_model_responses", "fingerprint", "fingerprints",
        }

        def normalized_key(value):
            return re.sub(r"[^a-z0-9]+", "_", str(value).casefold()).strip("_")

        def nested_keys(value):
            if isinstance(value, dict):
                return {normalized_key(name) for name in value}.union(
                    *(nested_keys(item) for item in value.values())
                )
            if isinstance(value, list):
                return set().union(*(nested_keys(item) for item in value))
            return set()

        for context in global_contexts:
            serialized = repr(context)
            self.assertNotIn(english_sentinel, serialized)
            self.assertNotIn(chinese_sentinel, serialized)
            self.assertTrue(forbidden.isdisjoint(nested_keys(context)))
            causality = context["blueprint_dimensions"]["causality"]
            self.assertEqual(causality["state"], "observed")
            self.assertEqual(causality["confidence"], 0.89)
            self.assertTrue(causality["evidence_refs"])
            self.assertEqual(causality["value"]["safe_class"], "costly_choice")
            self.assertEqual(causality["value"]["count"], 11)
            self.assertIs(causality["value"]["enabled"], True)
            chapter = next(
                node for node in context["canonical_nodes"] if node["node_type"] == "chapter"
            )
            self.assertEqual(chapter["title"], "[redacted-reference]")
            self.assertEqual(chapter["summary"], "[redacted-reference]")
            beat = next(node for node in context["canonical_nodes"] if node["node_type"] == "beat")
            self.assertEqual(beat["source_locator"], {
                "description": "[redacted-reference]", "line": 9,
            })

    def test_global_context_redacts_split_cjk_and_camelcase_forbidden_keys(self) -> None:
        chinese_sentinel = "星河密令门"
        split_sentinel = "星、 河\t🧭密—令\n门"
        source_text = f"source-prefix::{chinese_sentinel}::source-suffix\n" + ("x" * 26_000)
        job = self.create_job(source_text, "reference:global-context-split-cjk-camelcase")
        registry = DeterministicAgentRegistry()
        successful = registry.get("hierarchy_synthesis_agent")

        def injected_dimensions(evidence_ref):
            dimensions = {
                name: {"state": "not_observed", "value": None,
                       "confidence": 0.0, "evidence_refs": []}
                for name in BLUEPRINT_DIMENSIONS
            }
            dimensions["causality"] = {
                "state": "observed",
                "value": {
                    "mechanism_class": f"wrapper::{split_sentinel}::tail",
                    "safe_class": "costly_choice", "count": 17, "enabled": True,
                    "ReferenceText": chinese_sentinel,
                    "SourceText": chinese_sentinel,
                    "RarePhrase": chinese_sentinel,
                    "StyleFingerprint": chinese_sentinel,
                    "nested": {
                        "ReferencePassage": chinese_sentinel,
                        "SourcePassage": chinese_sentinel,
                        "RawAgentResponse": chinese_sentinel,
                        "RawModelResponse": chinese_sentinel,
                        "RAWAgentResponse": chinese_sentinel,
                        "RAWModelResponse": chinese_sentinel,
                    },
                },
                "confidence": 0.91,
                "evidence_refs": [evidence_ref],
            }
            return dimensions

        class SplitCjkEchoHierarchy:
            name = "hierarchy_synthesis_agent"
            output_schema = "creative-claw.blueprint-agent.v1"

            def run(self, task):
                result = successful.run(task)
                if task.context.get("synthesis_stage") != "chapter":
                    return result
                evidence_ref = result.evidence[0]["id"]
                dimensions = injected_dimensions(evidence_ref)
                result.data["dimensions"] = dimensions
                for node in result.data["nodes"]:
                    if node["node_type"] == "chapter":
                        node["title"] = f"title::{split_sentinel}::wrapped"
                        node["summary"] = f"summary::{split_sentinel}::wrapped"
                        node["dimensions"] = dimensions
                    if node["node_type"] == "beat":
                        node["source_locator"] = {
                            "description": f"locator::{split_sentinel}::wrapped",
                            "ReferenceText": chinese_sentinel,
                            "line": 13,
                        }
                return result

        registry.register(SplitCjkEchoHierarchy())

        result = BlueprintOrchestrator(self.database, registry).run_job(
            self.project["id"], job["id"]
        )
        global_contexts = [
            item["context"] for item in registry.inputs
            if item["context"].get("synthesis_stage") in {"volume", "work", "conflict"}
        ]

        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(len(global_contexts), 3)
        forbidden = {
            "reference_text", "source_text", "rare_phrase", "style_fingerprint",
            "reference_passage", "source_passage", "raw_agent_response",
            "raw_model_response",
        }

        def normalized_key(value):
            with_acronym_boundaries = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", str(value))
            with_word_boundaries = re.sub(
                r"([a-z0-9])([A-Z])", r"\1_\2", with_acronym_boundaries
            )
            return re.sub(
                r"[^a-z0-9]+", "_", with_word_boundaries.casefold()
            ).strip("_")

        def nested_keys(value):
            if isinstance(value, dict):
                return {normalized_key(name) for name in value}.union(
                    *(nested_keys(item) for item in value.values())
                )
            if isinstance(value, list):
                return set().union(*(nested_keys(item) for item in value))
            return set()

        def nested_strings(value):
            if isinstance(value, dict):
                return [item for nested in value.values() for item in nested_strings(nested)]
            if isinstance(value, list):
                return [item for nested in value for item in nested_strings(nested)]
            return [value] if isinstance(value, str) else []

        for context in global_contexts:
            serialized = repr(context)
            self.assertNotIn(chinese_sentinel, serialized)
            for value in nested_strings(context):
                cjk_only = "".join(re.findall(r"[\u3400-\u9fff]", value))
                self.assertNotIn(chinese_sentinel, cjk_only)
            self.assertTrue(forbidden.isdisjoint(nested_keys(context)))
            causality = context["blueprint_dimensions"]["causality"]
            self.assertEqual(causality["state"], "observed")
            self.assertEqual(causality["confidence"], 0.91)
            self.assertTrue(causality["evidence_refs"])
            self.assertEqual(causality["value"]["safe_class"], "costly_choice")
            self.assertEqual(causality["value"]["count"], 17)
            self.assertIs(causality["value"]["enabled"], True)

    def test_synthesis_context_handles_empty_and_short_references(self) -> None:
        blueprint = {"nodes": [], "evidence": [], "dimensions": {}}

        for reference_text in ("", "短"):
            with self.subTest(reference_text=reference_text):
                context = BlueprintOrchestrator._synthesis_context(
                    blueprint, "volume", reference_text=reference_text
                )
                self.assertEqual(context["synthesis_stage"], "volume")
                self.assertEqual(context["canonical_nodes"], [])
                self.assertEqual(context["evidence_metadata"], [])
                self.assertEqual(set(context["blueprint_dimensions"]), set(BLUEPRINT_DIMENSIONS))

    def test_global_hierarchy_unknown_existing_evidence_ref_is_schema_failed(self) -> None:
        job = self.create_job("x" * 26_000, "reference:global-unknown-evidence")
        registry = DeterministicAgentRegistry()
        successful = registry.get("hierarchy_synthesis_agent")

        class UnknownEvidenceHierarchy:
            name = "hierarchy_synthesis_agent"
            output_schema = "creative-claw.blueprint-agent.v1"

            def run(self, task):
                result = successful.run(task)
                if task.context.get("synthesis_stage") == "volume":
                    dimensions = {
                        name: {"state": "not_observed", "value": None,
                               "confidence": 0.0, "evidence_refs": []}
                        for name in BLUEPRINT_DIMENSIONS
                    }
                    dimensions["narrative_function"] = {
                        "state": "observed", "value": {"mechanism_class": "invalid"},
                        "confidence": 0.9, "evidence_refs": ["unknown-global-evidence"],
                    }
                    result.data["dimensions"] = dimensions
                    result.evidence = []
                return result

        registry.register(UnknownEvidenceHierarchy())

        result = BlueprintOrchestrator(self.database, registry).run_job(
            self.project["id"], job["id"]
        )
        volume_runs = [
            run for run in self.blueprints.list_agent_runs(self.project["id"], job["id"])
            if run["prompt_version"] == "prompt-v1:synthesis:volume"
        ]

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(volume_runs[-1]["status"], "schema_failed")
        self.assertEqual(volume_runs[-1]["error_category"], "schema_failed")

    def test_global_hierarchy_cannot_create_new_evidence_range(self) -> None:
        job = self.create_job("x" * 26_000, "reference:global-new-evidence")
        registry = DeterministicAgentRegistry()
        successful = registry.get("hierarchy_synthesis_agent")

        class ForgedEvidenceHierarchy:
            name = "hierarchy_synthesis_agent"
            output_schema = "creative-claw.blueprint-agent.v1"

            def run(self, task):
                result = successful.run(task)
                if task.context.get("synthesis_stage") == "volume":
                    result.evidence = [{"id": "forged", "start": 0, "end": 1,
                                        "source_length": 1, "confidence": 0.9}]
                return result

        registry.register(ForgedEvidenceHierarchy())

        result = BlueprintOrchestrator(self.database, registry).run_job(
            self.project["id"], job["id"]
        )
        volume_runs = [
            run for run in self.blueprints.list_agent_runs(self.project["id"], job["id"])
            if run["prompt_version"] == "prompt-v1:synthesis:volume"
        ]

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(volume_runs[-1]["status"], "evidence_invalid")
        self.assertEqual(volume_runs[-1]["error_category"], "evidence_invalid")


if __name__ == "__main__":
    unittest.main()
