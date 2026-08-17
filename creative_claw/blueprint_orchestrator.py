from __future__ import annotations

import json
import re
import threading
from copy import deepcopy
from typing import Any

from .blueprint_agents import (
    AgentRegistry,
    AgentTask,
    REFERENCE_AGENT_DAG,
    REFERENCE_BATCH_AGENT_DAG,
    validate_agent_payload,
)
from .blueprint_models import BLUEPRINT_DIMENSIONS, empty_dimensions
from .blueprint_repository import BlueprintRepository
from .db import Database
from .util import sha256_text


SHORT_TEXT_LIMIT = 20_000
BATCH_TARGET = 12_000
MAX_BATCH_OVERLAP = 800
_CHAPTER_RE = re.compile(r"(?m)^(?:第\s*[0-9一二三四五六七八九十百千]+\s*[章节卷]|Chapter\s+\d+)\b", re.IGNORECASE)
_JOB_LOCKS: dict[tuple[str, str], threading.Lock] = {}
_JOB_LOCKS_GUARD = threading.Lock()


class EvidenceRangeError(ValueError):
    pass


def _job_lock(database: Database, job_id: str) -> threading.Lock:
    key = (str(database.path), job_id)
    with _JOB_LOCKS_GUARD:
        return _JOB_LOCKS.setdefault(key, threading.Lock())


def split_reference_text(text: str) -> list[dict[str, int | str]]:
    if not text:
        raise ValueError("reference text is required")
    if len(text) <= SHORT_TEXT_LIMIT:
        return [
            {
                "ordinal": 0,
                "start_offset": 0,
                "end_offset": len(text),
                "overlap_start": 0,
                "source_hash": sha256_text(text),
            }
        ]
    headings = [match.start() for match in _CHAPTER_RE.finditer(text)]
    batches: list[dict[str, int | str]] = []
    start = 0
    ordinal = 0
    while start < len(text):
        proposed = min(start + BATCH_TARGET, len(text))
        candidates = [value for value in headings if start + 2_000 <= value <= proposed]
        end = max(candidates) if candidates else proposed
        if end <= start:
            end = proposed
        overlap = 0 if not batches else min(MAX_BATCH_OVERLAP, start)
        batch_start = 0 if not batches else start - overlap
        segment = text[batch_start:end]
        batches.append(
            {
                "ordinal": ordinal,
                "start_offset": batch_start,
                "end_offset": end,
                "overlap_start": overlap,
                "source_hash": sha256_text(segment),
            }
        )
        ordinal += 1
        start = end
    return batches


class BlueprintOrchestrator:
    def __init__(self, database: Database, registry: AgentRegistry):
        self.database = database
        self.repository = BlueprintRepository(database)
        self.registry = registry

    def _ensure_batches(self, project_id: str, job: dict[str, Any]) -> list[dict[str, Any]]:
        existing = self.repository.list_batches(project_id, job["id"])
        if existing:
            return existing
        text = str(job["input"].get("text") or "")
        for item in split_reference_text(text):
            self.repository.create_batch(
                project_id,
                job["id"],
                ordinal=int(item["ordinal"]),
                start_offset=int(item["start_offset"]),
                end_offset=int(item["end_offset"]),
                overlap_start=int(item["overlap_start"]),
                source_hash=str(item["source_hash"]),
                idempotency_key=f"batch:{item['ordinal']}:{item['source_hash']}",
            )
        return self.repository.list_batches(project_id, job["id"])

    def _run_agent(
        self,
        project_id: str,
        job: dict[str, Any],
        batch: dict[str, Any],
        agent_name: str,
        text: str,
    ) -> dict[str, Any]:
        base_key = f"{batch['id']}:{agent_name}:prompt-v1"
        prior_runs = [
            run for run in self.repository.list_agent_runs(project_id, job["id"])
            if run["batch_id"] == batch["id"] and run["agent_name"] == agent_name
            and run["prompt_version"] == "prompt-v1"
        ]
        completed = next((run for run in reversed(prior_runs) if run["status"] == "completed"), None)
        if completed is not None:
            return completed
        key = f"{base_key}:attempt:{len(prior_runs) + 1}"
        agent = self.registry.get(agent_name)
        if agent is None:
            raise KeyError(agent_name)
        task = AgentTask(
            project_id=project_id,
            job_id=job["id"],
            batch_id=batch["id"],
            source_version_id=job.get("source_version_id"),
            context={
                "text": text,
                "absolute_start": batch["start_offset"],
                "absolute_end": batch["end_offset"],
                **({"synthesis_stage": "chapter"}
                   if agent_name == "hierarchy_synthesis_agent" else {}),
                "prior_results": [
                    run["result"]
                    for run in self.repository.list_agent_runs(project_id, job["id"])
                    if run["batch_id"] == batch["id"] and run["status"] == "completed"
                ],
            },
            allowed_context_types=("reference_text", "prior_typed_results"),
            prompt_version="prompt-v1",
            idempotency_key=key,
        )
        try:
            result = agent.run(task)
            self._validate_agent_result(agent_name, result.data, result.evidence, len(text))
            return self.repository.create_agent_run(
                project_id,
                job["id"],
                batch_id=batch["id"],
                agent_name=agent_name,
                prompt_version=task.prompt_version,
                model=result.model,
                input_hash=result.input_hash or sha256_text(json.dumps(task.context, ensure_ascii=False, sort_keys=True)),
                output_hash=result.output_hash or sha256_text(json.dumps(result.data, ensure_ascii=False, sort_keys=True)),
                status="completed",
                result={"data": result.data, "evidence": result.evidence, "confidence": result.confidence},
                warnings=result.warnings,
                idempotency_key=key,
            )
        except Exception as exc:
            category = (
                "evidence_invalid" if isinstance(exc, EvidenceRangeError)
                else "schema_failed" if isinstance(exc, (ValueError, json.JSONDecodeError))
                else "retryable_failed"
            )
            diagnostic_hash = sha256_text(f"{type(exc).__name__}:{exc}")
            self.repository.create_agent_run(
                project_id,
                job["id"],
                batch_id=batch["id"],
                agent_name=agent_name,
                prompt_version=task.prompt_version,
                model={},
                input_hash=sha256_text(json.dumps(task.context, ensure_ascii=False, sort_keys=True)),
                output_hash=None,
                status=category,
                result={},
                warnings=[],
                idempotency_key=key,
                diagnostic_hash=diagnostic_hash,
                error_category=category,
            )
            raise

    def _run_synthesis_agent(
        self,
        project_id: str,
        job: dict[str, Any],
        agent_name: str,
        stage: str,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        prompt_version = f"prompt-v1:synthesis:{stage}"
        prior_runs = [
            run for run in self.repository.list_agent_runs(project_id, job["id"])
            if run["batch_id"] is None and run["agent_name"] == agent_name
            and run["prompt_version"] == prompt_version
        ]
        completed = next((run for run in reversed(prior_runs) if run["status"] == "completed"), None)
        if completed is not None:
            return completed
        key = f"{job['id']}:{agent_name}:{prompt_version}:attempt:{len(prior_runs) + 1}"
        agent = self.registry.get(agent_name)
        if agent is None:
            raise KeyError(agent_name)
        task = AgentTask(
            project_id=project_id,
            job_id=job["id"],
            batch_id=None,
            source_version_id=job.get("source_version_id"),
            context=context,
            allowed_context_types=(
                "synthesis_stage", "canonical_nodes", "lower_level_summaries",
                "evidence_metadata", "blueprint_dimensions",
            ),
            prompt_version=prompt_version,
            idempotency_key=key,
        )
        try:
            result = agent.run(task)
            existing_evidence_refs = {
                str(item.get("id")) for item in context.get("evidence_metadata", [])
                if str(item.get("id") or "")
            }
            self._validate_agent_result(
                agent_name, result.data, result.evidence, 0,
                existing_evidence_refs=existing_evidence_refs,
            )
            return self.repository.create_agent_run(
                project_id, job["id"], batch_id=None, agent_name=agent_name,
                prompt_version=prompt_version, model=result.model,
                input_hash=result.input_hash or sha256_text(
                    json.dumps(task.context, ensure_ascii=False, sort_keys=True)
                ),
                output_hash=result.output_hash or sha256_text(
                    json.dumps(result.data, ensure_ascii=False, sort_keys=True)
                ),
                status="completed",
                result={"data": result.data, "evidence": result.evidence,
                        "confidence": result.confidence},
                warnings=result.warnings, idempotency_key=key,
            )
        except Exception as exc:
            category = (
                "evidence_invalid" if isinstance(exc, EvidenceRangeError)
                else "schema_failed" if isinstance(exc, (ValueError, json.JSONDecodeError))
                else "retryable_failed"
            )
            self.repository.create_agent_run(
                project_id, job["id"], batch_id=None, agent_name=agent_name,
                prompt_version=prompt_version, model={},
                input_hash=sha256_text(json.dumps(task.context, ensure_ascii=False, sort_keys=True)),
                output_hash=None, status=category, result={}, warnings=[],
                idempotency_key=key,
                diagnostic_hash=sha256_text(f"{type(exc).__name__}:{exc}"),
                error_category=category,
            )
            raise

    @staticmethod
    def _synthesis_context(
        blueprint: dict[str, Any], stage: str, *, reference_text: str
    ) -> dict[str, Any]:
        forbidden_keys = {
            "text", "quote",
            "reference", "references", "source", "sources",
            "reference_text", "reference_texts", "source_text", "source_texts",
            "rare_phrase", "rare_phrases", "style_fingerprint", "style_fingerprints",
            "passage", "passages", "reference_passage", "reference_passages",
            "source_passage", "source_passages", "raw_response", "raw_responses",
            "raw_agent_response", "raw_agent_responses", "raw_model_response",
            "raw_model_responses", "fingerprint", "fingerprints",
        }
        normalized_reference = re.sub(r"[^a-z0-9]+", "", reference_text.casefold())

        def ngrams(value: str, width: int) -> set[str]:
            return {
                value[index:index + width]
                for index in range(max(0, len(value) - width + 1))
            }

        normalized_reference_chinese = "".join(
            re.findall(r"[\u3400-\u9fff]", reference_text)
        )
        reference_chinese_grams = ngrams(normalized_reference_chinese, 2)
        reference_latin_grams = ngrams(normalized_reference, 8)

        def normalized_key(value: Any) -> str:
            bounded = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", str(value))
            bounded = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", bounded)
            return re.sub(r"[^a-z0-9]+", "_", bounded.casefold()).strip("_")

        def is_reference_fragment(value: str) -> bool:
            stripped = value.strip()
            normalized_candidate_chinese = "".join(
                re.findall(r"[\u3400-\u9fff]", stripped)
            )
            candidate_chinese_grams = ngrams(normalized_candidate_chinese, 2)
            if candidate_chinese_grams & reference_chinese_grams:
                return True
            normalized_value = re.sub(r"[^a-z0-9]+", "", stripped.casefold())
            return bool(ngrams(normalized_value, 8) & reference_latin_grams)

        def safe_value(value: Any) -> Any:
            if isinstance(value, dict):
                return {
                    str(name): safe_value(item)
                    for name, item in value.items()
                    if normalized_key(name) not in forbidden_keys
                }
            if isinstance(value, list):
                return [safe_value(item) for item in value]
            if isinstance(value, str) and is_reference_fragment(value):
                return "[redacted-reference]"
            return deepcopy(value)

        def closed_dimensions(raw: Any) -> dict[str, dict[str, Any]]:
            source = raw if isinstance(raw, dict) else {}
            closed: dict[str, dict[str, Any]] = {}
            for name in BLUEPRINT_DIMENSIONS:
                item = source.get(name) if isinstance(source.get(name), dict) else {}
                closed[name] = {
                    "state": item.get("state", "not_observed"),
                    "value": safe_value(item.get("value")),
                    "confidence": float(item.get("confidence", 0.0)),
                    "evidence_refs": list(item.get("evidence_refs") or []),
                }
            return closed

        canonical_nodes: list[dict[str, Any]] = []
        for node in blueprint.get("nodes", []):
            canonical_nodes.append({
                "stable_key": node.get("stable_key"),
                "node_type": node.get("node_type"),
                "parent_key": node.get("parent_key"),
                "title": safe_value(node.get("title", "")),
                "summary": safe_value(node.get("summary", "")),
                "source_locator": safe_value(dict(node.get("source_locator") or {})),
                "dimensions": closed_dimensions(node.get("dimensions")),
            })
        if stage == "volume":
            lower_types = {"chapter", "episode", "scene", "beat"}
        elif stage == "work":
            upper = [node for node in canonical_nodes if node["node_type"] in {"volume", "phase"}]
            lower_types = {"volume", "phase"} if upper else {"chapter", "episode"}
        else:
            lower_types = {"work", "volume", "phase", "chapter", "episode", "scene", "beat"}
        evidence_metadata = [
            {name: item.get(name) for name in (
                "id", "start", "end", "source_length", "confidence", "agent_run_id"
            )}
            for item in blueprint.get("evidence", [])
        ]
        return {
            "synthesis_stage": stage,
            "canonical_nodes": canonical_nodes,
            "lower_level_summaries": [
                node for node in canonical_nodes if node["node_type"] in lower_types
            ],
            "evidence_metadata": evidence_metadata,
            "blueprint_dimensions": closed_dimensions(blueprint.get("dimensions")),
        }

    @staticmethod
    def _validate_agent_result(
        agent_name: str,
        data: dict[str, Any],
        evidence: list[dict[str, Any]],
        batch_length: int,
        *,
        existing_evidence_refs: set[str] | None = None,
    ) -> None:
        if not isinstance(data, dict):
            raise ValueError(f"{agent_name} output must be an object")
        if existing_evidence_refs is not None and evidence:
            raise EvidenceRangeError("global synthesis must not create new evidence ranges")
        for item in evidence:
            try:
                start, end = int(item["start"]), int(item["end"])
            except (KeyError, TypeError, ValueError) as exc:
                raise EvidenceRangeError("evidence range is malformed") from exc
            if not 0 <= start < end <= batch_length:
                raise EvidenceRangeError(
                    f"evidence range must be local to batch: {start}:{end}/{batch_length}"
                )
        validate_agent_payload(
            agent_name,
            data,
            evidence,
            batch_length=batch_length,
            existing_evidence_refs=existing_evidence_refs,
        )

    def _build_blueprint(self, project_id: str, job: dict[str, Any]) -> dict[str, Any]:
        batches = self.repository.list_batches(project_id, job["id"])
        runs = self.repository.list_agent_runs(project_id, job["id"])
        batch_by_id = {batch["id"]: batch for batch in batches}
        declarations: dict[str, dict[str, set[str]]] = {}
        declared_in_batch: set[tuple[str, str]] = set()
        for run in runs:
            if run["agent_name"] not in {"segmentation_agent", "hierarchy_synthesis_agent"}:
                continue
            for node in run["result"].get("data", {}).get("nodes", []):
                raw_key = str(node.get("stable_key") or "")
                node_type = str(node.get("node_type") or "")
                scope = str(node.get("key_scope") or ("global" if node_type == "work" else "batch"))
                declaration = declarations.setdefault(raw_key, {"scopes": set(), "types": set()})
                declaration["scopes"].add(scope)
                declaration["types"].add(node_type)
                if run["batch_id"] is not None:
                    declared_in_batch.add((str(run["batch_id"]), raw_key))
        for raw_key, declaration in declarations.items():
            if len(declaration["scopes"]) != 1:
                raise ValueError(f"stable key {raw_key} has conflicting key scopes")
            if len(declaration["types"]) != 1:
                raise ValueError(f"stable key {raw_key} has conflicting node types")
        global_raw_keys = {
            raw_key for raw_key, declaration in declarations.items()
            if declaration["scopes"] == {"global"}
        }
        canonical_keys = set(global_raw_keys)
        for batch_id, raw_key in declared_in_batch:
            if raw_key not in global_raw_keys:
                canonical_keys.add(f"batch:{int(batch_by_id[batch_id]['ordinal'])}:{raw_key}")

        def canonical_key(raw_value: Any, batch_id: str | None) -> str:
            raw_key = str(raw_value or "")
            if raw_key in canonical_keys:
                return raw_key
            if raw_key in global_raw_keys:
                return raw_key
            if batch_id is not None and (batch_id, raw_key) in declared_in_batch:
                return f"batch:{int(batch_by_id[batch_id]['ordinal'])}:{raw_key}"
            matches = sorted(key for key in canonical_keys if key.endswith(f":{raw_key}"))
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                raise ValueError(f"stable key reference {raw_key} is ambiguous across batches")
            raise ValueError(f"stable key reference {raw_key} has no declared node")

        def canonical_conflict_group(raw_value: Any, batch_id: str | None) -> str:
            raw_group = str(raw_value or "")
            if batch_id is None:
                return raw_group
            prefix = f"batch:{int(batch_by_id[batch_id]['ordinal'])}:"
            return raw_group if raw_group.startswith(prefix) else f"{prefix}{raw_group}"

        global_hierarchy_parents: dict[str, set[str]] = {}
        for run in runs:
            if run["agent_name"] != "hierarchy_synthesis_agent":
                continue
            for node in run["result"].get("data", {}).get("nodes", []):
                node_type = str(node.get("node_type") or "")
                scope = str(node.get("key_scope") or ("global" if node_type == "work" else "batch"))
                if scope != "global" or node_type == "work" or not node.get("parent_key"):
                    continue
                stable_key = canonical_key(node.get("stable_key"), run["batch_id"])
                parent_key = canonical_key(node.get("parent_key"), run["batch_id"])
                global_hierarchy_parents.setdefault(stable_key, set()).add(parent_key)
        for stable_key, parent_keys in global_hierarchy_parents.items():
            if len(parent_keys) != 1:
                raise ValueError(f"hierarchy node {stable_key} has conflicting parents")

        dimensions = empty_dimensions()
        evidence: list[dict[str, Any]] = []
        nodes_by_key: dict[str, dict[str, Any]] = {}
        node_priorities: dict[str, int] = {}
        edges: list[dict[str, Any]] = []
        interpretations: list[dict[str, Any]] = []
        conflicts: list[dict[str, Any]] = []
        for batch in batches:
            batch_runs = [run for run in runs if run["batch_id"] == batch["id"]]
            source_length = int(job["input"].get("source_length") or len(job["input"].get("text", "")))
            for run in batch_runs:
                data = deepcopy(run["result"].get("data", {}))
                logical_to_global: dict[str, str] = {}
                for item in run["result"].get("evidence", []):
                    logical_id = str(item["id"])
                    global_id = f"{run['id']}:{logical_id}"
                    logical_to_global[logical_id] = global_id
                    start = int(batch["start_offset"]) + int(item["start"])
                    end = int(batch["start_offset"]) + int(item["end"])
                    evidence.append(
                        {
                            **item,
                            "id": global_id,
                            "logical_id": logical_id,
                            "start": start,
                            "end": end,
                            "source_length": source_length,
                            "agent_run_id": run["id"],
                        }
                    )

                def remap_dimensions(items: dict[str, Any] | None) -> None:
                    if not isinstance(items, dict):
                        return
                    for item in items.values():
                        if isinstance(item, dict):
                            item["evidence_refs"] = [
                                logical_to_global.get(str(ref), str(ref))
                                for ref in item.get("evidence_refs", [])
                            ]

                incoming_dimensions = data.get("dimensions")
                if incoming_dimensions:
                    remap_dimensions(incoming_dimensions)
                    for name in BLUEPRINT_DIMENSIONS:
                        incoming = incoming_dimensions[name]
                        current = dimensions[name]
                        if current["state"] == "not_observed" or incoming["state"] == "observed":
                            dimensions[name] = incoming
                if run["agent_name"] in {"segmentation_agent", "hierarchy_synthesis_agent"}:
                    for item in data.get("nodes", data.get("segments", [])):
                        node = dict(item)
                        if isinstance(node.get("dimensions"), dict):
                            node["dimensions"] = deepcopy(node["dimensions"])
                            remap_dimensions(node["dimensions"])
                        if "start" in node and "end" in node:
                            node["source_locator"] = {
                                "start": int(batch["start_offset"]) + int(node.pop("start")),
                                "end": int(batch["start_offset"]) + int(node.pop("end")),
                            }
                        stable_key = canonical_key(node["stable_key"], run["batch_id"])
                        node["stable_key"] = stable_key
                        node.pop("key_scope", None)
                        if node.get("parent_key"):
                            node["parent_key"] = canonical_key(node["parent_key"], run["batch_id"])
                        priority = 2 if run["agent_name"] == "hierarchy_synthesis_agent" else 1
                        current = nodes_by_key.get(stable_key)
                        if current is None:
                            nodes_by_key[stable_key] = node
                            node_priorities[stable_key] = priority
                        elif current.get("node_type") != node.get("node_type"):
                            raise ValueError(f"stable key {stable_key} has conflicting node types")
                        elif priority >= node_priorities[stable_key]:
                            merged = dict(current)
                            for name, value in node.items():
                                if name == "source_locator" and name in merged:
                                    continue
                                if name == "parent_key" and not str(value or "").strip():
                                    continue
                                merged[name] = value
                            nodes_by_key[stable_key] = merged
                            node_priorities[stable_key] = priority
                        elif "source_locator" not in current and "source_locator" in node:
                            current["source_locator"] = node["source_locator"]
                for edge in data.get("edges", []):
                    canonical_edge = dict(edge)
                    canonical_edge["source_key"] = canonical_key(edge.get("source_key"), run["batch_id"])
                    canonical_edge["target_key"] = canonical_key(edge.get("target_key"), run["batch_id"])
                    if canonical_edge not in edges:
                        edges.append(canonical_edge)
                if run["agent_name"] == "interpretation_conflict_agent":
                    offset = len(interpretations)
                    for item in data.get("interpretations", []):
                        interpretation = dict(item)
                        interpretation["stable_key"] = canonical_key(
                            item.get("stable_key"), run["batch_id"]
                        )
                        if interpretation.get("conflict_group_id"):
                            interpretation["conflict_group_id"] = canonical_conflict_group(
                                interpretation["conflict_group_id"], run["batch_id"]
                            )
                        interpretations.append(interpretation)
                    for item in data.get("conflicts", []):
                        conflict = dict(item)
                        conflict["conflict_group_id"] = canonical_conflict_group(
                            conflict.get("conflict_group_id"), run["batch_id"]
                        )
                        conflict["interpretation_indexes"] = [
                            offset + int(index) for index in conflict.get("interpretation_indexes", [])
                        ]
                        conflicts.append(conflict)
        global_dimension_priorities = {name: 0 for name in BLUEPRINT_DIMENSIONS}
        global_node_dimension_priorities: dict[str, dict[str, int]] = {}

        def merge_global_dimensions(
            target: dict[str, Any],
            incoming: Any,
            priorities: dict[str, int],
            stage_priority: int,
        ) -> None:
            if not isinstance(incoming, dict):
                return
            for name in BLUEPRINT_DIMENSIONS:
                item = incoming.get(name)
                if not isinstance(item, dict):
                    continue
                current = target.get(name)
                current_state = current.get("state") if isinstance(current, dict) else None
                incoming_state = item.get("state")
                informative = incoming_state in {"observed", "uncertain"}
                if (
                    informative and stage_priority >= priorities.get(name, 0)
                    or current_state in {None, "not_observed"} and stage_priority >= priorities.get(name, 0)
                ):
                    target[name] = deepcopy(item)
                    priorities[name] = stage_priority

        for run in [item for item in runs if item["batch_id"] is None and item["status"] == "completed"]:
            data = deepcopy(run["result"].get("data", {}))
            if run["agent_name"] == "hierarchy_synthesis_agent":
                stage_priority = 4 if run["prompt_version"].endswith(":work") else 3
                merge_global_dimensions(
                    dimensions, data.get("dimensions"), global_dimension_priorities, stage_priority
                )
                for item in data.get("nodes", []):
                    node = dict(item)
                    incoming_node_dimensions = node.pop("dimensions", None)
                    stable_key = canonical_key(node["stable_key"], None)
                    node["stable_key"] = stable_key
                    node.pop("key_scope", None)
                    if node.get("parent_key"):
                        node["parent_key"] = canonical_key(node["parent_key"], None)
                    current = nodes_by_key.get(stable_key)
                    if current is None:
                        nodes_by_key[stable_key] = node
                        node_priorities[stable_key] = stage_priority
                    elif current.get("node_type") != node.get("node_type"):
                        raise ValueError(f"stable key {stable_key} has conflicting node types")
                    elif stage_priority >= node_priorities[stable_key]:
                        merged = dict(current)
                        for name, value in node.items():
                            if name == "source_locator" and name in merged:
                                continue
                            if name == "parent_key" and not str(value or "").strip():
                                continue
                            merged[name] = value
                        nodes_by_key[stable_key] = merged
                        node_priorities[stable_key] = stage_priority
                    if isinstance(incoming_node_dimensions, dict):
                        target_node = nodes_by_key[stable_key]
                        target_dimensions = target_node.get("dimensions")
                        if not isinstance(target_dimensions, dict):
                            target_dimensions = empty_dimensions()
                            target_node["dimensions"] = target_dimensions
                        priorities = global_node_dimension_priorities.setdefault(
                            stable_key, {name: 0 for name in BLUEPRINT_DIMENSIONS}
                        )
                        merge_global_dimensions(
                            target_dimensions,
                            incoming_node_dimensions,
                            priorities,
                            stage_priority,
                        )
            if run["agent_name"] == "interpretation_conflict_agent":
                offset = len(interpretations)
                for item in data.get("interpretations", []):
                    interpretation = dict(item)
                    interpretation["stable_key"] = canonical_key(item.get("stable_key"), None)
                    interpretations.append(interpretation)
                for item in data.get("conflicts", []):
                    conflict = dict(item)
                    conflict["interpretation_indexes"] = [
                        offset + int(index) for index in conflict.get("interpretation_indexes", [])
                    ]
                    conflicts.append(conflict)
        work = nodes_by_key.pop(
            "work",
            {"stable_key": "work", "node_type": "work", "title": job["input"].get("title", "")},
        )
        nodes_by_key = {"work": work, **nodes_by_key}
        self._validate_global_hierarchy(nodes_by_key)
        type_order = {
            "work": 0, "volume": 1, "phase": 1,
            "chapter": 2, "episode": 2, "scene": 3, "beat": 4,
        }
        ordered_nodes = sorted(
            nodes_by_key.values(), key=lambda node: type_order[str(node["node_type"])]
        )
        return {
            "dimensions": dimensions,
            "nodes": ordered_nodes,
            "evidence": evidence,
            "interpretations": interpretations,
            "conflicts": conflicts,
            "edges": edges,
        }

    @staticmethod
    def _validate_global_hierarchy(nodes_by_key: dict[str, dict[str, Any]]) -> None:
        roots = [node for node in nodes_by_key.values() if node.get("node_type") == "work"]
        if len(roots) != 1 or roots[0].get("stable_key") != "work" or roots[0].get("parent_key"):
            raise ValueError("hierarchy requires exactly one parentless work root")
        allowed_parents = {
            "volume": {"work"},
            "phase": {"work"},
            "chapter": {"work", "volume", "phase"},
            "episode": {"work", "volume", "phase"},
            "scene": {"chapter", "episode"},
            "beat": {"scene"},
        }
        for stable_key, node in nodes_by_key.items():
            node_type = str(node.get("node_type") or "")
            if node_type == "work":
                continue
            if node_type not in allowed_parents:
                raise ValueError(f"unsupported hierarchy node type: {node_type}")
            parent = nodes_by_key.get(str(node.get("parent_key") or ""))
            if parent is None:
                raise ValueError(f"hierarchy node {stable_key} has an orphan parent")
            if parent.get("node_type") not in allowed_parents[node_type]:
                raise ValueError(f"hierarchy node {stable_key} has an illegal parent type")
            seen = {stable_key}
            cursor = parent
            while cursor.get("node_type") != "work":
                cursor_key = str(cursor.get("stable_key") or "")
                if cursor_key in seen:
                    raise ValueError(f"hierarchy cycle detected at {cursor_key}")
                seen.add(cursor_key)
                cursor = nodes_by_key.get(str(cursor.get("parent_key") or ""))
                if cursor is None:
                    raise ValueError(f"hierarchy node {stable_key} does not reach work")

    def run_job(
        self, project_id: str, job_id: str, *, max_batches: int | None = None
    ) -> dict[str, Any]:
        with _job_lock(self.database, job_id):
            return self._run_job_locked(project_id, job_id, max_batches=max_batches)

    def _run_job_locked(
        self, project_id: str, job_id: str, *, max_batches: int | None = None
    ) -> dict[str, Any]:
        job = self.repository.get_job(project_id, job_id)
        if job["desired_state"] == "cancelled":
            return self.repository.update_job(project_id, job_id, status="cancelled")
        if job["desired_state"] == "paused":
            return self.repository.update_job(project_id, job_id, status="paused")
        if job["status"] == "completed" and job["checkpoint"].get("blueprint"):
            return {**job, "blueprint": job["checkpoint"]["blueprint"]}

        missing = [name for name in REFERENCE_AGENT_DAG if self.registry.get(name) is None]
        if missing:
            return self.repository.update_job(
                project_id,
                job_id,
                status="blocked",
                error={"category": "merge_inputs_missing", "missing_agents": missing},
            )

        batches = self._ensure_batches(project_id, job)
        remaining = [batch for batch in batches if batch["status"] != "completed"]
        selected = remaining if max_batches is None else remaining[: max(0, max_batches)]
        source_text = str(job["input"].get("text") or "")
        self.repository.update_job(project_id, job_id, status="running")
        for batch in selected:
            current = self.repository.get_job(project_id, job_id)
            if current["desired_state"] != "running":
                break
            self.repository.update_batch(project_id, batch["id"], status="running")
            segment = source_text[int(batch["start_offset"]) : int(batch["end_offset"])]
            interrupted = False
            try:
                for name in REFERENCE_BATCH_AGENT_DAG:
                    current = self.repository.get_job(project_id, job_id)
                    if current["desired_state"] != "running":
                        interrupted = True
                        break
                    for attempt in range(3):
                        try:
                            self._run_agent(project_id, job, batch, name, segment)
                            break
                        except Exception:
                            if attempt == 2:
                                raise
                    current = self.repository.get_job(project_id, job_id)
                    if current["desired_state"] != "running":
                        interrupted = True
                        break
            except Exception as exc:
                self.repository.update_batch(
                    project_id,
                    batch["id"],
                    status="blocked",
                    checkpoint={"error_hash": sha256_text(f"{type(exc).__name__}:{exc}")},
                )
                return self.repository.update_job(
                    project_id,
                    job_id,
                    status="blocked",
                    error={"category": "agent_failed", "batch_id": batch["id"]},
                )
            if interrupted:
                self.repository.update_batch(project_id, batch["id"], status="resumable")
                current = self.repository.get_job(project_id, job_id)
                interrupted_status = (
                    "cancelled"
                    if current["desired_state"] == "cancelled"
                    else "paused"
                    if current["desired_state"] == "paused"
                    else "resumable"
                )
                return self.repository.update_job(
                    project_id,
                    job_id,
                    status=interrupted_status,
                )
            completed_names = {
                run["agent_name"] for run in self.repository.list_agent_runs(project_id, job_id)
                if run["batch_id"] == batch["id"] and run["status"] == "completed"
            }
            missing_completed = [name for name in REFERENCE_BATCH_AGENT_DAG if name not in completed_names]
            if missing_completed:
                self.repository.update_batch(project_id, batch["id"], status="blocked")
                return self.repository.update_job(
                    project_id, job_id, status="blocked",
                    error={"category": "merge_inputs_missing", "missing_agents": missing_completed},
                )
            if not self.repository.complete_batch_if_job_running(project_id, batch["id"]):
                current = self.repository.get_job(project_id, job_id)
                self.repository.update_batch(project_id, batch["id"], status="resumable")
                return self.repository.update_job(
                    project_id, job_id,
                    status="cancelled" if current["desired_state"] == "cancelled" else "paused",
                )

        batches = self.repository.list_batches(project_id, job_id)
        completed = sum(batch["status"] == "completed" for batch in batches)
        if completed != len(batches):
            try:
                blueprint = self._build_blueprint(project_id, self.repository.get_job(project_id, job_id))
            except ValueError as exc:
                return self.repository.update_job(
                    project_id, job_id, status="blocked",
                    error={"category": "hierarchy_invalid",
                           "diagnostic_hash": sha256_text(f"{type(exc).__name__}:{exc}")},
                )
            updated = self.repository.update_job(
                project_id, job_id, status="resumable",
                progress={"completed_batches": completed, "total_batches": len(batches)},
                checkpoint={"blueprint": blueprint, "completed_batches": completed}, error={},
            )
            return {**updated, "blueprint": blueprint}
        for agent_name, stage in (
            ("hierarchy_synthesis_agent", "volume"),
            ("hierarchy_synthesis_agent", "work"),
            ("interpretation_conflict_agent", "conflict"),
        ):
            current = self.repository.get_job(project_id, job_id)
            if current["desired_state"] != "running":
                return self.repository.update_job(
                    project_id, job_id,
                    status="cancelled" if current["desired_state"] == "cancelled" else "paused",
                )
            try:
                lower_blueprint = self._build_blueprint(project_id, current)
            except ValueError as exc:
                return self.repository.update_job(
                    project_id, job_id, status="blocked",
                    error={"category": "hierarchy_invalid", "synthesis_stage": stage,
                           "diagnostic_hash": sha256_text(f"{type(exc).__name__}:{exc}")},
                )
            context = self._synthesis_context(
                lower_blueprint,
                stage,
                reference_text=str(current.get("input", {}).get("text") or ""),
            )
            try:
                for attempt in range(3):
                    try:
                        self._run_synthesis_agent(project_id, current, agent_name, stage, context)
                        break
                    except Exception:
                        if attempt == 2:
                            raise
            except Exception as exc:
                return self.repository.update_job(
                    project_id, job_id, status="blocked",
                    error={"category": "agent_failed", "synthesis_stage": stage,
                           "diagnostic_hash": sha256_text(f"{type(exc).__name__}:{exc}")},
                )
            current = self.repository.get_job(project_id, job_id)
            if current["desired_state"] != "running":
                return self.repository.update_job(
                    project_id, job_id,
                    status="cancelled" if current["desired_state"] == "cancelled" else "paused",
                )
        try:
            blueprint = self._build_blueprint(project_id, self.repository.get_job(project_id, job_id))
        except ValueError as exc:
            return self.repository.update_job(
                project_id, job_id, status="blocked",
                error={"category": "hierarchy_invalid",
                       "diagnostic_hash": sha256_text(f"{type(exc).__name__}:{exc}")},
            )
        updated = self.repository.complete_job_if_running(
            project_id, job_id,
            progress={"completed_batches": completed, "total_batches": len(batches)},
            checkpoint={"blueprint": blueprint, "completed_batches": completed},
        )
        if updated is None:
            current = self.repository.get_job(project_id, job_id)
            updated = self.repository.update_job(
                project_id, job_id,
                status="cancelled" if current["desired_state"] == "cancelled" else "paused",
            )
        return {**updated, "blueprint": blueprint}
