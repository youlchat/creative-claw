from __future__ import annotations

import json
import sqlite3
import threading
import time
from copy import deepcopy
from typing import Any

from .blueprint_agents import (
    MIGRATION_AGENT_DAG,
    REFERENCE_AGENT_DAG,
    AgentRegistry,
    AgentTask,
    validate_agent_payload,
)
from .blueprint_models import (
    BLUEPRINT_DIMENSIONS,
    RIGHTS_BASES,
    empty_dimensions,
    validate_node,
)
from .blueprint_orchestrator import BlueprintOrchestrator, SHORT_TEXT_LIMIT
from .blueprint_repository import BlueprintRepository
from .blueprint_similarity import assess_similarity
from .db import Database
from .indexer import Indexer
from .util import json_dumps, json_loads, new_id, sha256_text, utc_now
from .workflow import VersionConflictError, WorkflowService


TARGET_SETTING_FIELDS = (
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
)

_CONTROLLED_MECHANISM_CLASSES = {
    "narrative_function": frozenset({
        "costly_choice", "transformation", "revelation", "escalation", "resolution", "setup_payoff",
    }),
    "causality": frozenset({"cause_effect", "trigger", "constraint", "feedback", "enablement"}),
    "emotion_kline": frozenset({"rise", "fall", "reversal", "oscillation", "plateau"}),
}

_REFERENCE_SUBMISSION_LOCKS: dict[tuple[str, str, str, str], threading.Lock] = {}
_REFERENCE_SUBMISSION_LOCKS_GUARD = threading.Lock()


def _reference_submission_lock(
    database: Database, project_id: str, source_hash: str, rights_basis: str
) -> threading.Lock:
    key = (str(database.path), project_id, source_hash, rights_basis)
    with _REFERENCE_SUBMISSION_LOCKS_GUARD:
        return _REFERENCE_SUBMISSION_LOCKS.setdefault(key, threading.Lock())


class ContextFirewallError(ValueError):
    """Raised before model invocation when reference provenance reaches drafting."""


class _PublicationInterrupted(RuntimeError):
    pass


class DraftContextBuilder:
    def __init__(self, database: Database):
        self.database = database
        self.repository = BlueprintRepository(database)
        self.workflow = WorkflowService(database)

    @staticmethod
    def _find_forbidden(value: Any, path: str = "$") -> list[str]:
        findings: list[str] = []
        if isinstance(value, dict):
            for key, child in value.items():
                child_path = f"{path}.{key}"
                lowered = str(key).lower()
                if lowered in {"reference_text", "quote", "rare_phrase", "rare_phrases", "raw_agent_response"}:
                    findings.append(child_path)
                if lowered in {"provenance", "source_kind", "source_type"} and str(child).lower().startswith("reference"):
                    findings.append(child_path)
                findings.extend(DraftContextBuilder._find_forbidden(child, child_path))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                findings.extend(DraftContextBuilder._find_forbidden(child, f"{path}[{index}]"))
        return findings

    def build(
        self, project_id: str, target_blueprint_id: str, unit_id: str, artifact_id: str
    ) -> dict[str, Any]:
        target = self.workflow.get_artifact(project_id, target_blueprint_id)
        if target["artifact_type"] != "target_blueprint":
            raise ValueError("draft context requires a target blueprint")
        if target["status"] != "approved" or target["attrs"].get("confirmation_status") != "confirmed":
            raise ValueError("target blueprint must be confirmed before drafting")
        manuscript = self.workflow.get_artifact(project_id, artifact_id)
        with self.database.connect() as connection:
            unit = connection.execute(
                "SELECT * FROM production_units WHERE id=? AND project_id=?",
                (unit_id, project_id),
            ).fetchone()
            if unit is None:
                raise KeyError(f"Unknown production unit: {unit_id}")
            if manuscript.get("production_unit_id") not in {None, unit_id}:
                raise ValueError("draft artifact must belong to the selected production unit")
            setting_row = connection.execute(
                """
                SELECT ts.* FROM artifact_dependencies d
                JOIN artifacts a ON a.id=d.upstream_artifact_id AND a.artifact_type='target_setting'
                JOIN target_settings ts ON ts.artifact_id=a.id AND ts.artifact_version_id=a.current_version_id
                WHERE d.project_id=? AND d.downstream_artifact_id=? AND d.dependency_type='constrains'
                """,
                (project_id, target_blueprint_id),
            ).fetchone()
            if setting_row is None:
                raise ValueError("confirmed target blueprint is missing its target setting")
            entity_rows = connection.execute(
                "SELECT name, entity_type, attrs_json FROM entities WHERE project_id=? ORDER BY entity_type, name",
                (project_id,),
            ).fetchall()
            timeline_rows = connection.execute(
                "SELECT label, description, story_time, episode, scene FROM timeline_events WHERE project_id=? AND branch=? ORDER BY created_at",
                (project_id, target["branch"]),
            ).fetchall()
            kline_rows = connection.execute(
                "SELECT character_name, dimension, period_id, open, high, low, close FROM ohlc_points WHERE project_id=? AND branch=? ORDER BY sort_key",
                (project_id, target["branch"]),
            ).fetchall()
        nodes = self.repository.list_nodes_for_version(project_id, target["current_version_id"])
        safe_nodes = [
            {
                "stable_key": node["stable_key"],
                "node_type": node["node_type"],
                "title": node["title"],
                "summary": node["summary"],
                "dimensions": node["dimensions"],
            }
            for node in nodes
        ]
        previous_text = ""
        if manuscript.get("current_version_id"):
            with self.database.connect() as connection:
                previous = connection.execute(
                    "SELECT content FROM artifact_versions WHERE id=?",
                    (manuscript["current_version_id"],),
                ).fetchone()
            previous_text = str(previous["content"]) if previous else ""
        context = {
            "target_setting": json_loads(setting_row["structured_json"], {}),
            "target_blueprint": {"artifact_id": target_blueprint_id, "version_id": target["current_version_id"], "nodes": safe_nodes},
            "target_metadata": target["attrs"],
            "unit": {"id": unit["id"], "unit_type": unit["unit_type"], "title": unit["title"]},
            "canon": {
                "entities": [
                    {"name": row["name"], "entity_type": row["entity_type"], "attrs": json_loads(row["attrs_json"], {})}
                    for row in entity_rows
                ],
                "timeline": [dict(row) for row in timeline_rows],
                "kline": [dict(row) for row in kline_rows],
            },
            "previous_accepted_target_text": previous_text,
        }
        findings = self._find_forbidden(context)
        if findings:
            self.workflow.ledger.append(
                project_id,
                "context_firewall_blocked",
                {
                    "target_blueprint_id": target_blueprint_id,
                    "unit_id": unit_id,
                    "artifact_id": artifact_id,
                    "finding_paths": findings,
                },
                "security",
            )
            raise ContextFirewallError("reference provenance is forbidden in draft context")
        return {
            "payload": context,
            "provenance": [
                {"kind": "target_setting", "version_id": setting_row["artifact_version_id"]},
                {"kind": "target_blueprint", "version_id": target["current_version_id"]},
                {"kind": "target_project_canon", "project_id": project_id},
            ],
        }


class BlueprintService:
    def __init__(self, database: Database, registry: AgentRegistry):
        self.database = database
        self.registry = registry
        self.repository = BlueprintRepository(database)
        self.workflow = WorkflowService(database)
        self.orchestrator = BlueprintOrchestrator(database, registry)

    def create_reference_job(
        self,
        project_id: str,
        *,
        title: str,
        text: str,
        rights_basis: str,
        run_async: bool | None = None,
    ) -> dict[str, Any]:
        clean_title = str(title or "").strip()
        clean_text = str(text or "")
        if not clean_title:
            raise ValueError("reference title is required")
        if not clean_text.strip():
            raise ValueError("reference text is required")
        if rights_basis not in RIGHTS_BASES:
            raise ValueError(f"unsupported rights basis: {rights_basis}")
        source_hash = sha256_text(clean_text)
        with _reference_submission_lock(self.database, project_id, source_hash, rights_basis):
            return self._create_reference_job_locked(
                project_id, clean_title=clean_title, clean_text=clean_text,
                rights_basis=rights_basis, source_hash=source_hash, run_async=run_async,
            )

    def _create_reference_job_locked(
        self,
        project_id: str,
        *,
        clean_title: str,
        clean_text: str,
        rights_basis: str,
        source_hash: str,
        run_async: bool | None,
    ) -> dict[str, Any]:
        stable_key = f"reference:{source_hash}:{rights_basis}:prompt-v1"
        with self.database.connect() as connection:
            existing = connection.execute(
                "SELECT id FROM blueprint_jobs WHERE project_id=? AND idempotency_key=?",
                (project_id, stable_key),
            ).fetchone()
        if existing is not None:
            return self.repository.get_job(project_id, existing["id"])
        indexed = None
        for attempt in range(5):
            try:
                indexed = Indexer(self.database).index_text(
                    project_id,
                    f"reference-blueprints/{source_hash}.txt",
                    clean_text,
                    title=clean_title,
                    metadata={"rights_basis": rights_basis, "purpose": "blueprint_reference"},
                    canon_status="reference",
                )
                break
            except (sqlite3.IntegrityError, sqlite3.OperationalError) as exc:
                message = str(exc).lower()
                retryable_race = (
                    "documents.project_id, documents.path" in message
                    or "database is locked" in message
                )
                if not retryable_race or attempt == 4:
                    raise
                time.sleep(0.02 * (attempt + 1))
        if indexed is None:
            raise RuntimeError("reference document indexing did not complete")
        source_version_id = self._get_or_create_reference_source(
            project_id, clean_title=clean_title, clean_text=clean_text,
            rights_basis=rights_basis, source_hash=source_hash,
            document_id=indexed.document_id,
        )
        job = self.repository.create_job(
            project_id,
            job_type="reference",
            input_json={
                "title": clean_title,
                "text": clean_text,
                "source_length": len(clean_text),
                "source_hash": source_hash,
            },
            idempotency_key=stable_key,
            rights_basis=rights_basis,
            source_document_id=indexed.document_id,
            source_version_id=source_version_id,
        )
        missing_agents = [name for name in REFERENCE_AGENT_DAG if self.registry.get(name) is None]
        if missing_agents:
            return self.repository.update_job(
                project_id, job["id"], status="blocked",
                error={"category": "automation_unavailable", "code": "automation_unavailable",
                       "missing_agents": missing_agents},
            )
        should_run_async = len(clean_text) > SHORT_TEXT_LIMIT if run_async is None else bool(run_async)
        if should_run_async:
            return job
        result = self.orchestrator.run_job(project_id, job["id"])
        if result["status"] == "completed" and not result.get("output_artifact_id"):
            return self._publish_reference_blueprint(project_id, result, clean_text)
        return result

    def _get_or_create_reference_source(
        self,
        project_id: str,
        *,
        clean_title: str,
        clean_text: str,
        rights_basis: str,
        source_hash: str,
        document_id: str,
    ) -> str:
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            source = connection.execute(
                """SELECT id, current_version_id FROM artifacts
                   WHERE project_id=? AND artifact_type='reference_source'
                     AND json_extract(attrs_json, '$.source_hash')=?
                     AND json_extract(attrs_json, '$.rights_basis')=?
                   ORDER BY created_at LIMIT 1""",
                (project_id, source_hash, rights_basis),
            ).fetchone()
            if source is None:
                artifact_id = new_id("art")
                connection.execute(
                    """INSERT INTO artifacts(
                           id, project_id, artifact_type, title, status, branch, attrs_json,
                           created_at, updated_at
                       ) VALUES (?, ?, 'reference_source', ?, 'empty', 'main', ?, ?, ?)""",
                    (artifact_id, project_id, clean_title,
                     json_dumps({"document_id": document_id, "rights_basis": rights_basis,
                                 "source_hash": source_hash}), now, now),
                )
                self.workflow.ledger.append(
                    project_id, "artifact.created",
                    {"artifact_id": artifact_id, "artifact_type": "reference_source",
                     "title": clean_title, "stage_id": None, "unit_id": None},
                    "blueprint", connection=connection,
                )
                current_version_id = None
            else:
                artifact_id = str(source["id"])
                current_version_id = source["current_version_id"]
            if current_version_id is not None:
                return str(current_version_id)
            version_id = new_id("ver")
            connection.execute(
                """INSERT INTO artifact_versions(
                       id, artifact_id, version_number, parent_version_id, content, content_format,
                       source_kind, change_summary, actor, metadata_json, created_at
                   ) VALUES (?, ?, 1, NULL, ?, 'text/plain', 'reference_input',
                             'Store authorized reference source', 'blueprint', ?, ?)""",
                (version_id, artifact_id, clean_text,
                 json_dumps({"rights_basis": rights_basis, "source_hash": source_hash}), now),
            )
            connection.execute(
                "UPDATE artifacts SET current_version_id=?, status='draft', updated_at=? WHERE id=?",
                (version_id, now, artifact_id),
            )
            self.workflow.ledger.append(
                project_id, "artifact.version_created",
                {"artifact_id": artifact_id, "version_id": version_id, "version_number": 1,
                 "parent_version_id": None, "change_summary": "Store authorized reference source",
                 "source_kind": "reference_input",
                 "sync": {"stale_review_ids": [], "impact_ids": [], "affected_artifact_ids": []}},
                "blueprint", connection=connection,
            )
            return version_id

    def _publish_reference_blueprint(
        self, project_id: str, job: dict[str, Any], source_text: str
    ) -> dict[str, Any]:
        blueprint = job["blueprint"]
        evidence_specs = list(blueprint.get("evidence") or [])
        if not evidence_specs:
            raise ValueError("reference blueprint cannot be published without evidence")
        logical_evidence_ids = [str(item.get("id") or "") for item in evidence_specs]
        if any(not identifier for identifier in logical_evidence_ids):
            raise ValueError("reference evidence requires a logical id")
        if len(set(logical_evidence_ids)) != len(logical_evidence_ids):
            raise ValueError("reference evidence logical ids must be unique")
        logical_to_database = {
            logical_id: new_id("bpev") for logical_id in logical_evidence_ids
        }

        def map_evidence_refs(items: dict[str, Any]) -> dict[str, Any]:
            mapped = deepcopy(items)
            for name, item in mapped.items():
                if item["state"] not in {"observed", "uncertain"}:
                    continue
                refs = list(item.get("evidence_refs") or [])
                missing = [str(ref) for ref in refs if str(ref) not in logical_to_database]
                if missing:
                    raise ValueError(
                        f"reference dimension {name} cites missing logical evidence: {', '.join(missing)}"
                    )
                item["evidence_refs"] = [logical_to_database[str(ref)] for ref in refs]
            return mapped

        dimensions = deepcopy(blueprint["dimensions"])
        dimensions = map_evidence_refs(dimensions)
        root_payload = validate_node({"stable_key": "work", "node_type": "work", "dimensions": dimensions})
        blueprint_nodes = list(blueprint.get("nodes") or [])
        if not any(str(item.get("stable_key")) == "work" for item in blueprint_nodes):
            blueprint_nodes.insert(0, {"stable_key": "work", "node_type": "work", "title": job["input"]["title"]})
        content = json_dumps(
            {
                "schema": "creative-claw.reference-blueprint.v1",
                "title": job["input"]["title"],
                "dimensions": root_payload["dimensions"],
                "node_count": len(blueprint_nodes),
            }
        )
        artifact_id = new_id("art")
        version_id = new_id("ver")
        now = utc_now()
        runs = [run for run in self.repository.list_agent_runs(project_id, job["id"]) if run["status"] == "completed"]
        agent_run_ids = [run["id"] for run in runs]
        conflict_run = next((run["id"] for run in runs if run["agent_name"] == "interpretation_conflict_agent"), None)
        try:
            with self.database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                current = connection.execute(
                    "SELECT desired_state, output_artifact_id FROM blueprint_jobs WHERE id=? AND project_id=?",
                    (job["id"], project_id),
                ).fetchone()
                if current is None:
                    raise KeyError(f"Unknown blueprint job: {job['id']}")
                if current["desired_state"] != "running" or current["output_artifact_id"] is not None:
                    raise _PublicationInterrupted()
                attrs = {"job_id": job["id"], "rights_basis": job.get("rights_basis")}
                connection.execute(
                    """INSERT INTO artifacts(id, project_id, artifact_type, title, status, branch, attrs_json,
                                              current_version_id, created_at, updated_at)
                       VALUES (?, ?, 'reference_blueprint', ?, 'draft', 'main', ?, NULL, ?, ?)""",
                    (artifact_id, project_id, f"{job['input']['title']} · 创作机制蓝图", json_dumps(attrs), now, now),
                )
                self.workflow.ledger.append(
                    project_id, "artifact.created",
                    {"artifact_id": artifact_id, "artifact_type": "reference_blueprint",
                     "title": f"{job['input']['title']} · 创作机制蓝图", "stage_id": None, "unit_id": None},
                    "blueprint", connection=connection,
                )
                connection.execute(
                    """INSERT INTO artifact_versions(
                           id, artifact_id, version_number, parent_version_id, content, content_format,
                           source_kind, change_summary, actor, metadata_json, created_at
                       ) VALUES (?, ?, 1, NULL, ?, 'text/plain', 'agent_candidate_accepted', ?, 'blueprint', ?, ?)""",
                    (version_id, artifact_id, content, "Publish extracted reference mechanism blueprint",
                     json_dumps({"job_id": job["id"], "source_hash": job["input"]["source_hash"]}), now),
                )
                connection.execute(
                    "UPDATE artifacts SET current_version_id=?, updated_at=? WHERE id=?",
                    (version_id, now, artifact_id),
                )
                self.workflow.ledger.append(
                    project_id, "artifact.version_created",
                    {"artifact_id": artifact_id, "version_id": version_id, "version_number": 1,
                     "parent_version_id": None, "change_summary": "Publish extracted reference mechanism blueprint",
                     "source_kind": "agent_candidate_accepted",
                     "sync": {"stale_review_ids": [], "impact_ids": [], "affected_artifact_ids": []}},
                    "blueprint", connection=connection,
                )
                node_by_key: dict[str, dict[str, Any]] = {}
                parent_keys: dict[str, str | None] = {}
                for item in blueprint_nodes:
                    stable_key = str(item["stable_key"])
                    node_dimensions = deepcopy(item.get("dimensions") or (
                        root_payload["dimensions"] if stable_key == "work" else empty_dimensions()
                    ))
                    if stable_key != "work" or item.get("dimensions"):
                        node_dimensions = map_evidence_refs(node_dimensions)
                    row = self.repository.create_node(
                        project_id, artifact_version_id=version_id, job_id=None, stable_key=stable_key,
                        node_type=str(item.get("node_type") or "chapter"), dimensions=node_dimensions,
                        title=str(item.get("title") or (job["input"]["title"] if stable_key == "work" else "")),
                        summary=str(item.get("summary") or ("由全部专业代理逐层综合的作品级可观察创作机制" if stable_key == "work" else "")),
                        source_locator=dict(item.get("source_locator") or ({"start": 0, "end": len(source_text), "source": "reference"} if stable_key == "work" else {})),
                        status="pending_author", confidence=float(item.get("confidence", 0.9)),
                        agent_run_ids=agent_run_ids, connection=connection,
                    )
                    node_by_key[stable_key] = row
                    parent_keys[stable_key] = item.get("parent_key")
                for stable_key, parent_key in parent_keys.items():
                    if parent_key and parent_key in node_by_key:
                        connection.execute(
                            "UPDATE blueprint_nodes SET parent_id=? WHERE id=?",
                            (node_by_key[str(parent_key)]["id"], node_by_key[stable_key]["id"]),
                        )
                root = node_by_key["work"]
                for item in evidence_specs:
                    evidence_id = logical_to_database[str(item["id"])]
                    start, end = int(item["start"]), int(item["end"])
                    self.repository.create_evidence(
                        project_id, root["id"], evidence_id=evidence_id, start=start, end=end,
                        source_length=len(source_text), quote=source_text[start:end],
                        confidence=float(item.get("confidence", 0.9)), agent_run_id=item.get("agent_run_id"),
                        source_document_id=job.get("source_document_id"),
                        locator={"absolute_start": start, "absolute_end": end}, connection=connection,
                    )
                created_interpretations: list[dict[str, Any]] = []
                for item in blueprint.get("interpretations") or []:
                    node = node_by_key.get(str(item.get("stable_key") or "work"), root)
                    created_interpretations.append(self.repository.create_interpretation(
                        project_id, node["id"], dimension=str(item.get("dimension") or "narrative_function"),
                        value=deepcopy(item.get("value")), confidence=float(item.get("confidence", 0.0)),
                        conflict_group_id=item.get("conflict_group_id"), agent_run_id=conflict_run,
                        connection=connection,
                    ))
                for item in blueprint.get("conflicts") or []:
                    indexes = [int(index) for index in item.get("interpretation_indexes", [])]
                    interpretation_ids = [created_interpretations[index]["id"] for index in indexes]
                    self.repository.create_conflict(
                        project_id, version_id, conflict_group_id=str(item["conflict_group_id"]),
                        relation_type=str(item.get("relation_type") or "unresolved"),
                        interpretation_ids=interpretation_ids, connection=connection,
                    )
                for item in blueprint.get("edges") or []:
                    source = node_by_key.get(str(item.get("source_key")))
                    target = node_by_key.get(str(item.get("target_key")))
                    if source is None or target is None:
                        raise ValueError("blueprint edge references an unknown stable key")
                    self.repository.create_edge(
                        project_id, artifact_version_id=version_id, source_node_id=source["id"],
                        target_node_id=target["id"], edge_type=str(item["edge_type"]),
                        attrs=dict(item.get("attrs") or {}), confidence=float(item.get("confidence", 1.0)),
                        connection=connection,
                    )
                checkpoint = {**job["checkpoint"], "blueprint": blueprint, "artifact_version_id": version_id}
                cursor = connection.execute(
                    """UPDATE blueprint_jobs SET status='completed', output_artifact_id=?, checkpoint_json=?, updated_at=?
                       WHERE id=? AND project_id=? AND desired_state='running' AND output_artifact_id IS NULL""",
                    (artifact_id, json_dumps(checkpoint), now, job["id"], project_id),
                )
                if cursor.rowcount != 1:
                    raise _PublicationInterrupted()
                self.workflow.ledger.append(
                    project_id, "reference_blueprint.published",
                    {"job_id": job["id"], "artifact_id": artifact_id, "version_id": version_id,
                     "node_count": len(node_by_key), "evidence_count": len(evidence_specs)},
                    "blueprint", connection=connection,
                )
        except _PublicationInterrupted:
            current = self.repository.get_job(project_id, job["id"])
            if current.get("output_artifact_id"):
                return current
            return self.repository.update_job(
                project_id, job["id"],
                status="cancelled" if current["desired_state"] == "cancelled" else "paused",
            )
        return self.repository.get_job(project_id, job["id"])

    def create_manual_reference_blueprint(
        self, project_id: str, *, title: str, nodes: list[dict[str, Any]]
    ) -> dict[str, Any]:
        return self._create_manual_blueprint(
            project_id, artifact_type="reference_blueprint", title=title, nodes=nodes,
            target_setting_id=None, reference_blueprint_id=None,
        )

    def create_manual_target_blueprint(
        self,
        project_id: str,
        *,
        title: str,
        nodes: list[dict[str, Any]],
        target_setting_id: str,
        reference_blueprint_id: str | None = None,
    ) -> dict[str, Any]:
        return self._create_manual_blueprint(
            project_id, artifact_type="target_blueprint", title=title, nodes=nodes,
            target_setting_id=target_setting_id, reference_blueprint_id=reference_blueprint_id,
        )

    def _create_manual_blueprint(
        self,
        project_id: str,
        *,
        artifact_type: str,
        title: str,
        nodes: list[dict[str, Any]],
        target_setting_id: str | None,
        reference_blueprint_id: str | None,
    ) -> dict[str, Any]:
        clean_title = str(title or "").strip()
        if not clean_title:
            raise ValueError("manual blueprint title is required")
        if artifact_type not in {"reference_blueprint", "target_blueprint"}:
            raise ValueError("unsupported manual blueprint type")
        validated = [validate_node(deepcopy(node)) for node in nodes]
        if not validated or sum(node["node_type"] == "work" for node in validated) != 1:
            raise ValueError("manual blueprint requires exactly one work node")
        artifact_id, version_id, now = new_id("art"), new_id("ver"), utc_now()
        attrs = {"creation_mode": "manual"}
        if artifact_type == "target_blueprint":
            attrs.update({"confirmation_status": "proposed", "structural_risk": "manual_review"})
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone() is None:
                raise KeyError(f"Unknown project: {project_id}")
            for dependency_id, expected_type in (
                (target_setting_id, "target_setting"), (reference_blueprint_id, "reference_blueprint")
            ):
                if dependency_id is not None and connection.execute(
                    "SELECT 1 FROM artifacts WHERE id=? AND project_id=? AND artifact_type=?",
                    (dependency_id, project_id, expected_type),
                ).fetchone() is None:
                    raise KeyError(f"Unknown {expected_type}: {dependency_id}")
            connection.execute(
                """INSERT INTO artifacts(id, project_id, artifact_type, title, status, branch, attrs_json,
                                          current_version_id, created_at, updated_at)
                   VALUES (?, ?, ?, ?, 'draft', 'main', ?, NULL, ?, ?)""",
                (artifact_id, project_id, artifact_type, clean_title, json_dumps(attrs), now, now),
            )
            self.workflow.ledger.append(
                project_id, "artifact.created",
                {"artifact_id": artifact_id, "artifact_type": artifact_type, "title": clean_title,
                 "stage_id": None, "unit_id": None}, "author", connection=connection,
            )
            connection.execute(
                """INSERT INTO artifact_versions(
                       id, artifact_id, version_number, parent_version_id, content, content_format,
                       source_kind, change_summary, actor, metadata_json, created_at
                   ) VALUES (?, ?, 1, NULL, ?, 'text/plain', 'user', ?, 'author', ?, ?)""",
                (version_id, artifact_id,
                 json_dumps({"schema": "creative-claw.blueprint.v1", "nodes": validated}),
                 "Create manual blueprint", json_dumps({"creation_mode": "manual"}), now),
            )
            connection.execute(
                "UPDATE artifacts SET current_version_id=?, updated_at=? WHERE id=?",
                (version_id, now, artifact_id),
            )
            self.workflow.ledger.append(
                project_id, "artifact.version_created",
                {"artifact_id": artifact_id, "version_id": version_id, "version_number": 1,
                 "parent_version_id": None, "change_summary": "Create manual blueprint",
                 "source_kind": "user",
                 "sync": {"stale_review_ids": [], "impact_ids": [], "affected_artifact_ids": []}},
                "author", connection=connection,
            )
            rows_by_source_id: dict[str, dict[str, Any]] = {}
            pending_parents: list[tuple[str, str]] = []
            for original, checked in zip(nodes, validated):
                row = self.repository.create_node(
                    project_id, artifact_version_id=version_id, job_id=None,
                    stable_key=checked["stable_key"], node_type=checked["node_type"],
                    dimensions=checked["dimensions"], title=str(original.get("title") or ""),
                    summary=str(original.get("summary") or ""),
                    source_locator=dict(original.get("source_locator") or {}),
                    status=str(original.get("status") or "pending_author"),
                    confidence=float(original.get("confidence", 1.0)), connection=connection,
                )
                rows_by_source_id[str(original.get("id") or checked["stable_key"])] = row
                parent = original.get("parent_id") or original.get("parent_key")
                if parent:
                    pending_parents.append((row["id"], str(parent)))
            for row_id, parent_key in pending_parents:
                parent = rows_by_source_id.get(parent_key)
                if parent is None:
                    raise ValueError("manual blueprint parent reference is invalid")
                connection.execute("UPDATE blueprint_nodes SET parent_id=? WHERE id=?", (parent["id"], row_id))
            for upstream_id, dependency_type in (
                (reference_blueprint_id, "derives_from"), (target_setting_id, "constrains")
            ):
                if upstream_id is None:
                    continue
                dependency_id = new_id("dep")
                connection.execute(
                    """INSERT INTO artifact_dependencies(
                           id, project_id, upstream_artifact_id, downstream_artifact_id,
                           dependency_type, created_at) VALUES (?, ?, ?, ?, ?, ?)""",
                    (dependency_id, project_id, upstream_id, artifact_id, dependency_type, now),
                )
                self.workflow.ledger.append(
                    project_id, "artifact_dependency.created",
                    {"dependency_id": dependency_id, "upstream_artifact_id": upstream_id,
                     "downstream_artifact_id": artifact_id, "dependency_type": dependency_type},
                    "author", connection=connection,
                )
        return self.get_blueprint(project_id, artifact_id)

    def get_job(self, project_id: str, job_id: str) -> dict[str, Any]:
        return self.repository.get_job(project_id, job_id)

    def pause_job(self, project_id: str, job_id: str) -> dict[str, Any]:
        self.repository.set_job_desired_state(project_id, job_id, "paused")
        return self.repository.update_job(project_id, job_id, status="paused")

    def resume_job(self, project_id: str, job_id: str) -> dict[str, Any]:
        self.repository.set_job_desired_state(project_id, job_id, "running")
        return self.execute_job(project_id, job_id)

    def execute_job(self, project_id: str, job_id: str) -> dict[str, Any]:
        result = self.orchestrator.run_job(project_id, job_id)
        if result["status"] == "completed" and result["job_type"] == "reference" and not result.get("output_artifact_id"):
            return self._publish_reference_blueprint(project_id, result, result["input"]["text"])
        return result

    def cancel_job(self, project_id: str, job_id: str) -> dict[str, Any]:
        self.repository.set_job_desired_state(project_id, job_id, "cancelled")
        return self.repository.update_job(project_id, job_id, status="cancelled")

    def _version_row(self, project_id: str, version_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT av.* FROM artifact_versions av
                JOIN artifacts a ON a.id=av.artifact_id
                WHERE av.id=? AND a.project_id=?
                """,
                (version_id, project_id),
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown artifact version: {version_id}")
        result = dict(row)
        result["metadata"] = json_loads(result.pop("metadata_json"), {})
        return result

    def get_blueprint(
        self, project_id: str, artifact_id: str, *, include_quotes: bool = True
    ) -> dict[str, Any]:
        artifact = self.workflow.get_artifact(project_id, artifact_id)
        if artifact["artifact_type"] not in {"reference_blueprint", "target_blueprint"}:
            raise ValueError("artifact is not a blueprint")
        version_id = artifact.get("current_version_id")
        if not version_id:
            raise ValueError("blueprint has no published version")
        version = self._version_row(project_id, version_id)
        return {
            "artifact": artifact,
            "version": version,
            "nodes": self.repository.list_nodes_for_version(project_id, version_id),
            "evidence": self.repository.list_evidence_for_version(
                project_id, version_id, include_quotes=include_quotes
            ),
            "interpretations": self.repository.list_interpretations_for_version(project_id, version_id),
            "conflicts": self.repository.list_conflicts_for_version(project_id, version_id),
            "edges": self.repository.list_edges_for_version(project_id, version_id),
        }

    def save_blueprint_version(
        self,
        project_id: str,
        artifact_id: str,
        nodes: list[dict[str, Any]],
        *,
        expected_current_version_id: str | None,
        change_summary: str,
        interpretation_decisions: dict[str, str] | None = None,
        conflict_resolutions: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        artifact = self.workflow.get_artifact(project_id, artifact_id)
        if artifact["artifact_type"] not in {"reference_blueprint", "target_blueprint"}:
            raise ValueError("artifact is not a blueprint")
        validated = [validate_node(node) for node in deepcopy(nodes)]
        old_evidence = self.repository.list_evidence_for_version(
            project_id, expected_current_version_id, include_quotes=True
        ) if expected_current_version_id else []
        evidence_by_node: dict[str, list[dict[str, Any]]] = {}
        for evidence in old_evidence:
            evidence_by_node.setdefault(evidence["node_id"], []).append(evidence)
        prepared: list[tuple[dict[str, Any], list[tuple[str, dict[str, Any]]]]] = []
        for original, node in zip(nodes, validated):
            cloned: list[tuple[str, dict[str, Any]]] = []
            prior = evidence_by_node.get(str(original.get("id") or ""), [])
            if prior:
                new_ids = [new_id("bpev") for _ in prior]
                for dimension in node["dimensions"].values():
                    if dimension["state"] in {"observed", "uncertain"}:
                        dimension["evidence_refs"] = list(new_ids)
                cloned = list(zip(new_ids, prior))
            prepared.append((node, cloned))
        old_interpretations = (
            self.repository.list_interpretations_for_version(project_id, expected_current_version_id)
            if expected_current_version_id else []
        )
        old_conflicts = (
            self.repository.list_conflicts_for_version(project_id, expected_current_version_id)
            if expected_current_version_id else []
        )
        old_edges = (
            self.repository.list_edges_for_version(project_id, expected_current_version_id)
            if expected_current_version_id else []
        )
        decisions = dict(interpretation_decisions or {})
        resolutions = dict(conflict_resolutions or {})
        known_interpretations = {item["id"] for item in old_interpretations}
        known_conflicts = {item["id"] for item in old_conflicts}
        if not set(decisions).issubset(known_interpretations):
            raise KeyError("Unknown blueprint interpretation decision")
        if any(status not in {"pending", "confirmed", "rejected"} for status in decisions.values()):
            raise ValueError("unsupported interpretation decision")
        if not set(resolutions).issubset(known_conflicts):
            raise KeyError("Unknown blueprint conflict resolution")
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            saved = self.workflow.save_artifact_version(
                project_id, artifact_id,
                json_dumps({"schema": "creative-claw.blueprint.v1", "nodes": validated}),
                expected_current_version_id=expected_current_version_id,
                change_summary=change_summary, source_kind="user", actor="author",
                connection=connection,
            )
            id_map: dict[str, str] = {}
            created: list[tuple[dict[str, Any], dict[str, Any], list[tuple[str, dict[str, Any]]]]] = []
            for original, (node, cloned) in zip(nodes, prepared):
                parent_id = id_map.get(str(original.get("parent_id") or ""))
                row = self.repository.create_node(
                    project_id, artifact_version_id=saved["version"]["id"], job_id=None,
                    parent_id=parent_id, stable_key=node["stable_key"], node_type=node["node_type"],
                    dimensions=node["dimensions"], title=str(original.get("title") or ""),
                    summary=str(original.get("summary") or ""),
                    source_locator=dict(original.get("source_locator") or {}),
                    status=str(original.get("status") or "pending_author"),
                    confidence=float(original.get("confidence", 1.0)),
                    agent_run_ids=list(original.get("agent_run_ids") or []), connection=connection,
                )
                if original.get("id"):
                    id_map[str(original["id"])] = row["id"]
                created.append((original, row, cloned))
            for _original, node, cloned in created:
                for evidence_id, evidence in cloned:
                    self.repository.create_evidence(
                        project_id, node["id"], evidence_id=evidence_id,
                        start=evidence["start"], end=evidence["end"],
                        source_length=evidence["source_length"], quote=evidence.get("quote", ""),
                        confidence=evidence["confidence"], agent_run_id=evidence.get("agent_run_id"),
                        source_document_id=evidence.get("source_document_id"), chunk_id=evidence.get("chunk_id"),
                        locator=evidence.get("locator", {}), connection=connection,
                    )
            interpretation_map: dict[str, str] = {}
            for interpretation in old_interpretations:
                new_node_id = id_map.get(str(interpretation["node_id"]))
                if new_node_id is None:
                    continue
                cloned_interpretation = self.repository.create_interpretation(
                    project_id, new_node_id, dimension=interpretation["dimension"],
                    value=interpretation["value"], confidence=interpretation["confidence"],
                    conflict_group_id=interpretation.get("conflict_group_id"),
                    agent_run_id=interpretation.get("agent_run_id"),
                    author_status=decisions.get(
                        interpretation["id"], interpretation.get("author_status", "pending")
                    ), connection=connection,
                )
                interpretation_map[interpretation["id"]] = cloned_interpretation["id"]
            for conflict in old_conflicts:
                cloned_ids = [interpretation_map[item] for item in conflict["interpretation_ids"] if item in interpretation_map]
                if cloned_ids:
                    resolution_spec = resolutions.get(conflict["id"], {})
                    status = str(resolution_spec.get("status") or conflict["status"])
                    if status not in {"pending_author", "resolved", "dismissed"}:
                        raise ValueError("unsupported conflict resolution status")
                    cloned_conflict = self.repository.create_conflict(
                        project_id, saved["version"]["id"],
                        conflict_group_id=conflict["conflict_group_id"],
                        relation_type=conflict["relation_type"], interpretation_ids=cloned_ids,
                        status=status, connection=connection,
                    )
                    resolution = deepcopy(resolution_spec.get("resolution") or conflict.get("resolution") or {})
                    selected = resolution.get("selected_interpretation_id")
                    if selected in interpretation_map:
                        resolution["selected_interpretation_id"] = interpretation_map[selected]
                    connection.execute(
                        "UPDATE blueprint_conflicts SET resolution_json=?, updated_at=? WHERE id=?",
                        (json_dumps(resolution), utc_now(), cloned_conflict["id"]),
                    )
            for edge in old_edges:
                source_id, target_id = id_map.get(edge["source_node_id"]), id_map.get(edge["target_node_id"])
                if source_id and target_id:
                    self.repository.create_edge(
                        project_id, artifact_version_id=saved["version"]["id"],
                        source_node_id=source_id, target_node_id=target_id, edge_type=edge["edge_type"],
                        attrs=edge["attrs"], confidence=edge["confidence"], connection=connection,
                    )
        return {**saved, "nodes": [row for _original, row, _cloned in created]}

    def _structure_setting(self, text: str, overrides: dict[str, Any] | None) -> dict[str, Any]:
        clean = str(text or "").strip()
        if not clean:
            raise ValueError("target setting text is required")
        structured: dict[str, Any] = {
            "genre": "fantasy" if any(token in clean for token in ("奇幻", "魔法", "云城")) else "unspecified",
            "audience": "adult" if "成年" in clean else "general",
            "media_type": "novel" if any(token in clean for token in ("小说", "长篇")) else "unspecified",
            "scale": "long" if "长篇" in clean else "unspecified",
            "world_rules": [clean],
            "characters": [{"name": "主角", "description": clean}],
            "character_goals": [clean],
            "core_conflict": clean,
            "stakes": clean,
            "themes": [],
            "narrative_preferences": {},
            "must_include": [],
            "must_avoid": [],
            "ending_direction": clean,
        }
        for key, value in (overrides or {}).items():
            if key not in TARGET_SETTING_FIELDS:
                raise ValueError(f"unknown target setting field: {key}")
            structured[key] = deepcopy(value)
        return {key: structured[key] for key in TARGET_SETTING_FIELDS}

    def create_target_setting(
        self, project_id: str, text: str, *, overrides: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        clean_text = str(text or "").strip()
        if not clean_text:
            raise ValueError("target setting text is required")
        agent = self.registry.get("target_setting_agent")
        if agent is None:
            if set((overrides or {}).keys()) != set(TARGET_SETTING_FIELDS):
                raise ValueError("automation_unavailable: missing target_setting_agent")
            structured = {key: deepcopy((overrides or {})[key]) for key in TARGET_SETTING_FIELDS}
            setting_job = None
            setting_run = None
        else:
            setting_job = self.repository.create_job(
                project_id, job_type="target_setting",
                input_json={"text_hash": sha256_text(clean_text), "character_count": len(clean_text)},
                idempotency_key=f"target-setting:{sha256_text(clean_text)}:prompt-v1",
            )
            setting_run = self._run_migration_agent(
                project_id, setting_job, "target_setting_agent", {"target_setting_text": clean_text}
            )
            payload = setting_run["result"].get("data", {}).get("structured")
            if not isinstance(payload, dict):
                raise ValueError("target_setting_agent returned invalid structured output")
            missing = [field for field in TARGET_SETTING_FIELDS if field not in payload]
            if missing:
                raise ValueError(f"target_setting_agent missing fields: {', '.join(missing)}")
            structured = {key: deepcopy(payload[key]) for key in TARGET_SETTING_FIELDS}
            for key, value in (overrides or {}).items():
                if key not in TARGET_SETTING_FIELDS:
                    raise ValueError(f"unknown target setting field: {key}")
                structured[key] = deepcopy(value)
        artifact = self.workflow.create_artifact(
            project_id,
            "target_setting",
            "新作品结构化设定",
            attrs={"confirmation_status": "proposed"},
            actor="blueprint",
        )
        saved = self.workflow.save_artifact_version(
            project_id,
            artifact["id"],
            json_dumps(structured),
            expected_current_version_id=None,
            change_summary="Structure target setting from natural language",
            source_kind="agent_proposal" if setting_run is not None else "user",
            actor="blueprint",
        )
        record = self.repository.create_target_setting_record(
            project_id,
            artifact_id=artifact["id"],
            artifact_version_id=saved["version"]["id"],
            source_text=clean_text,
            structured=structured,
            status="proposed",
        )
        if setting_job is not None:
            self.repository.update_job(
                project_id, setting_job["id"], status="completed",
                output_artifact_id=artifact["id"], progress={"completed_agents": 1, "total_agents": 1},
                checkpoint={"artifact_version_id": saved["version"]["id"], "agent_run_id": setting_run["id"]},
            )
        return {**record, "artifact": saved["artifact"], "version": saved["version"], "structured": structured}

    def confirm_target_setting(
        self,
        project_id: str,
        artifact_id: str,
        *,
        expected_current_version_id: str,
        structured: dict[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(structured, dict):
            raise ValueError("structured target setting is required")
        missing = [field for field in TARGET_SETTING_FIELDS if field not in structured]
        extra = [field for field in structured if field not in TARGET_SETTING_FIELDS]
        if missing or extra:
            raise ValueError("structured target setting fields are incomplete")
        confirmed = {field: deepcopy(structured[field]) for field in TARGET_SETTING_FIELDS}
        current_record = self.repository.get_target_setting(project_id, artifact_id)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            saved = self.workflow.save_artifact_version(
                project_id, artifact_id, json_dumps(confirmed),
                expected_current_version_id=expected_current_version_id,
                change_summary="Author confirms structured target setting",
                source_kind="user", actor="author", connection=connection,
            )
            connection.execute(
                "UPDATE target_settings SET status='superseded', updated_at=? WHERE artifact_id=? AND project_id=?",
                (utc_now(), artifact_id, project_id),
            )
            record = self.repository.create_target_setting_record(
                project_id, artifact_id=artifact_id, artifact_version_id=saved["version"]["id"],
                source_text=current_record["source_text"], structured=confirmed, status="confirmed",
                connection=connection,
            )
            row = connection.execute("SELECT attrs_json FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
            attrs = json_loads(row["attrs_json"], {})
            attrs.update({"confirmation_status": "confirmed", "confirmed_version_id": saved["version"]["id"]})
            connection.execute(
                "UPDATE artifacts SET attrs_json=?, updated_at=? WHERE id=?",
                (json_dumps(attrs), utc_now(), artifact_id),
            )
            self.workflow.ledger.append(
                project_id, "target_setting.confirmed",
                {"artifact_id": artifact_id, "version_id": saved["version"]["id"]},
                "author", connection=connection,
            )
        return {**record, "artifact": self.workflow.get_artifact(project_id, artifact_id),
                "version": saved["version"], "structured": confirmed}

    def _abstract_reference(self, blueprint: dict[str, Any]) -> list[dict[str, Any]]:
        allowed = ("narrative_function", "causality", "emotion_kline")
        result = []
        for node in blueprint["nodes"]:
            dimensions = {}
            for name in allowed:
                source = node["dimensions"][name]
                value = source.get("value") if isinstance(source.get("value"), dict) else {}
                proposed_class = str(value.get("mechanism_class") or "").strip().lower()
                mechanism_class = (
                    proposed_class
                    if proposed_class in _CONTROLLED_MECHANISM_CLASSES[name]
                    else f"generic:{name}"
                )
                dimensions[name] = {
                    "state": source["state"],
                    "mechanism_class": mechanism_class,
                    "confidence": source["confidence"],
                }
            result.append(
                {"stable_key": node["stable_key"], "node_type": node["node_type"], "dimensions": dimensions}
            )
        return result

    def _run_migration_agent(
        self, project_id: str, job: dict[str, Any], name: str, context: dict[str, Any]
    ) -> dict[str, Any]:
        agent = self.registry.get(name)
        if agent is None:
            raise ValueError(f"automation_unavailable: missing {name}")
        base_key = f"migration:{job['id']}:{name}:prompt-v1"
        prior = [
            run for run in self.repository.list_agent_runs(project_id, job["id"])
            if run["agent_name"] == name and run["prompt_version"] == "prompt-v1"
        ]
        completed = next((run for run in reversed(prior) if run["status"] == "completed"), None)
        if completed is not None:
            return completed
        key = f"{base_key}:attempt:{len(prior) + 1}"
        task = AgentTask(
            project_id=project_id,
            job_id=job["id"],
            batch_id=None,
            source_version_id=job.get("source_version_id"),
            context=context,
            allowed_context_types=("abstract_reference_blueprint", "target_setting", "typed_mapping"),
            prompt_version="prompt-v1",
            idempotency_key=key,
        )
        try:
            result = agent.run(task)
            validate_agent_payload(name, result.data, result.evidence, batch_length=None)
        except ValueError as exc:
            message = str(exc)
            error = message if message.startswith(f"{name} schema_failed:") else f"{name} schema_failed: {message}"
            self.repository.create_agent_run(
                project_id, job["id"], batch_id=None, agent_name=name,
                prompt_version=task.prompt_version, model={},
                input_hash=sha256_text(json.dumps(task.context, ensure_ascii=False, sort_keys=True)),
                output_hash=None, status="schema_failed", result={}, warnings=[],
                idempotency_key=key,
                diagnostic_hash=sha256_text(f"{type(exc).__name__}:{message}"),
                error_category="schema_failed",
            )
            raise ValueError(error) from exc
        except Exception as exc:
            self.repository.create_agent_run(
                project_id, job["id"], batch_id=None, agent_name=name,
                prompt_version=task.prompt_version, model={},
                input_hash=sha256_text(json.dumps(task.context, ensure_ascii=False, sort_keys=True)),
                output_hash=None, status="retryable_failed", result={}, warnings=[],
                idempotency_key=key,
                diagnostic_hash=sha256_text(f"{type(exc).__name__}:{exc}"),
                error_category="retryable_failed",
            )
            raise
        return self.repository.create_agent_run(
            project_id,
            job["id"],
            batch_id=None,
            agent_name=name,
            prompt_version=task.prompt_version,
            model=result.model,
            input_hash=result.input_hash,
            output_hash=result.output_hash,
            status="completed",
            result={"data": result.data, "evidence": [], "confidence": result.confidence},
            warnings=result.warnings,
            idempotency_key=key,
        )

    def _stop_migration_if_requested(
        self, project_id: str, job_id: str
    ) -> dict[str, Any] | None:
        current = self.repository.get_job(project_id, job_id)
        if current["desired_state"] == "running":
            return None
        status = "cancelled" if current["desired_state"] == "cancelled" else "paused"
        return self.repository.update_job(project_id, job_id, status=status)

    def create_migration_job(
        self, project_id: str, reference_blueprint_id: str, target_setting_id: str
    ) -> dict[str, Any]:
        reference = self.get_blueprint(project_id, reference_blueprint_id, include_quotes=False)
        if reference["artifact"]["artifact_type"] != "reference_blueprint":
            raise ValueError("migration requires a reference blueprint")
        setting = self.repository.get_target_setting(project_id, target_setting_id)
        setting_artifact = self.workflow.get_artifact(project_id, target_setting_id)
        if setting["status"] != "confirmed" or setting_artifact["attrs"].get("confirmation_status") != "confirmed":
            raise ValueError("target setting must be explicitly confirmed before migration")
        abstract = self._abstract_reference(reference)
        job = self.repository.create_job(
            project_id,
            job_type="migration",
            input_json={
                "reference_blueprint_id": reference_blueprint_id,
                "reference_version_id": reference["version"]["id"],
                "target_setting_id": target_setting_id,
                "target_setting_version_id": setting["artifact_version_id"],
            },
            idempotency_key=f"migration:{reference['version']['id']}:{setting['artifact_version_id']}",
            source_version_id=reference["version"]["id"],
        )
        if job.get("output_artifact_id"):
            return job
        mapping_context = {"abstract_reference_blueprint": abstract, "target_setting": setting["structured"]}
        mapping_run = self._run_migration_agent(project_id, job, MIGRATION_AGENT_DAG[0], mapping_context)
        interrupted = self._stop_migration_if_requested(project_id, job["id"])
        if interrupted is not None:
            return interrupted
        mapping_plan = mapping_run["result"].get("data", {}).get("mappings")
        if not isinstance(mapping_plan, list):
            raise ValueError("mechanism_mapping_agent returned invalid mappings")
        reference_keys = {node["stable_key"] for node in abstract}
        covered: list[str] = []
        for item in mapping_plan:
            if not isinstance(item, dict) or item.get("action") not in {"preserve", "transform", "drop", "add"}:
                raise ValueError("mechanism_mapping_agent returned invalid mapping action")
            reference_key = item.get("reference_stable_key")
            if item["action"] == "add":
                if reference_key not in {None, ""}:
                    raise ValueError("add mappings cannot reference a reference node")
            elif reference_key not in reference_keys:
                raise ValueError("mapping references an unknown reference node")
            else:
                covered.append(str(reference_key))
        if sorted(covered) != sorted(reference_keys):
            raise ValueError("mapping must cover every reference node exactly once")
        target_context = {
            "target_setting": setting["structured"],
            "typed_mapping": mapping_plan,
            "abstract_mechanisms": abstract,
        }
        target_run = self._run_migration_agent(project_id, job, MIGRATION_AGENT_DAG[1], target_context)
        interrupted = self._stop_migration_if_requested(project_id, job["id"])
        if interrupted is not None:
            return interrupted
        target_data = target_run["result"].get("data", {})
        raw_target_nodes = target_data.get("nodes")
        if not isinstance(raw_target_nodes, list) or not raw_target_nodes:
            raise ValueError("target_blueprint_agent returned invalid nodes")
        target_nodes_payload = []
        for item in raw_target_nodes:
            checked = validate_node(item)
            target_nodes_payload.append({**deepcopy(item), **checked})
        target_keys = {item["stable_key"] for item in target_nodes_payload}
        for item in mapping_plan:
            if item["action"] != "drop" and item.get("target_stable_key") not in target_keys:
                raise ValueError("mapping target is missing from target blueprint output")
        artifact_id, version_id, now = new_id("art"), new_id("ver"), utc_now()
        attrs = {
            "confirmation_status": "proposed", "migration_job_id": job["id"],
            "structural_risk": str(target_data.get("structural_risk") or "passed"),
        }
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current_job = connection.execute(
                "SELECT desired_state, output_artifact_id FROM blueprint_jobs WHERE id=? AND project_id=?",
                (job["id"], project_id),
            ).fetchone()
            if current_job is None:
                raise KeyError(f"Unknown blueprint job: {job['id']}")
            if current_job["output_artifact_id"] is not None:
                raise VersionConflictError("Migration job is already published")
            if current_job["desired_state"] != "running":
                raise ValueError("migration job is not running")
            connection.execute(
                """INSERT INTO artifacts(id, project_id, artifact_type, title, status, branch, attrs_json,
                                          current_version_id, created_at, updated_at)
                   VALUES (?, ?, 'target_blueprint', '新作品生产蓝图', 'draft', 'main', ?, NULL, ?, ?)""",
                (artifact_id, project_id, json_dumps(attrs), now, now),
            )
            self.workflow.ledger.append(
                project_id, "artifact.created",
                {"artifact_id": artifact_id, "artifact_type": "target_blueprint",
                 "title": "新作品生产蓝图", "stage_id": None, "unit_id": None},
                "blueprint", connection=connection,
            )
            connection.execute(
                """INSERT INTO artifact_versions(
                       id, artifact_id, version_number, parent_version_id, content, content_format,
                       source_kind, change_summary, actor, metadata_json, created_at
                   ) VALUES (?, ?, 1, NULL, ?, 'text/plain', 'agent_proposal', ?, 'blueprint', ?, ?)""",
                (version_id, artifact_id,
                 json_dumps({"schema": "creative-claw.target-blueprint.v1", "nodes": target_nodes_payload}),
                 "Propose migrated target production blueprint",
                 json_dumps({"mapping_run_id": mapping_run["id"], "target_run_id": target_run["id"]}), now),
            )
            connection.execute(
                "UPDATE artifacts SET current_version_id=?, updated_at=? WHERE id=?",
                (version_id, now, artifact_id),
            )
            self.workflow.ledger.append(
                project_id, "artifact.version_created",
                {"artifact_id": artifact_id, "version_id": version_id, "version_number": 1,
                 "parent_version_id": None, "change_summary": "Propose migrated target production blueprint",
                 "source_kind": "agent_proposal",
                 "sync": {"stale_review_ids": [], "impact_ids": [], "affected_artifact_ids": []}},
                "blueprint", connection=connection,
            )
            target_rows = []
            for payload in target_nodes_payload:
                target_rows.append(self.repository.create_node(
                    project_id, artifact_version_id=version_id, job_id=None,
                    stable_key=payload["stable_key"], node_type=payload["node_type"],
                    dimensions=payload["dimensions"], title=str(payload.get("title") or payload["stable_key"]),
                    summary=str(payload.get("summary") or ""), status="proposed", confidence=0.85,
                    agent_run_ids=[mapping_run["id"], target_run["id"]], connection=connection,
                ))
            reference_by_key = {node["stable_key"]: node for node in reference["nodes"]}
            target_by_key = {node["stable_key"]: node for node in target_rows}
            for item in mapping_plan:
                source = reference_by_key.get(item.get("reference_stable_key"))
                target = target_by_key.get(item.get("target_stable_key"))
                self.repository.create_mapping(
                    project_id, job_id=job["id"], reference_version_id=reference["version"]["id"],
                    target_version_id=version_id, reference_node_id=source["id"] if source else None,
                    target_node_id=target["id"] if target else None, action=item["action"],
                    rationale=str(item.get("rationale") or ""), risk=dict(item.get("risk") or {}),
                    connection=connection,
                )
            for upstream_id, dependency_type in (
                (reference_blueprint_id, "derives_from"), (target_setting_id, "constrains")
            ):
                dependency_id = new_id("dep")
                connection.execute(
                    """INSERT INTO artifact_dependencies(
                           id, project_id, upstream_artifact_id, downstream_artifact_id,
                           dependency_type, created_at) VALUES (?, ?, ?, ?, ?, ?)""",
                    (dependency_id, project_id, upstream_id, artifact_id, dependency_type, now),
                )
                self.workflow.ledger.append(
                    project_id, "artifact_dependency.created",
                    {"dependency_id": dependency_id, "upstream_artifact_id": upstream_id,
                     "downstream_artifact_id": artifact_id, "dependency_type": dependency_type},
                    "blueprint", connection=connection,
                )
            cursor = connection.execute(
                """UPDATE blueprint_jobs SET status='completed', output_artifact_id=?, progress_json=?,
                       checkpoint_json=?, error_json='{}', updated_at=?
                   WHERE id=? AND project_id=? AND desired_state='running' AND output_artifact_id IS NULL""",
                (artifact_id, json_dumps({"completed_agents": 2, "total_agents": 2}),
                 json_dumps({"target_version_id": version_id}), now, job["id"], project_id),
            )
            if cursor.rowcount != 1:
                raise VersionConflictError("Migration job changed during target blueprint publication")
        return self.repository.get_job(project_id, job["id"])

    def list_mappings(self, project_id: str, job_id: str) -> list[dict[str, Any]]:
        return self.repository.list_mappings(project_id, job_id)

    def _reference_text_for_target(self, project_id: str, target_blueprint_id: str) -> str:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT source_version.content AS reference_text
                FROM artifact_dependencies d
                JOIN artifacts reference_blueprint
                  ON reference_blueprint.id=d.upstream_artifact_id
                 AND reference_blueprint.artifact_type='reference_blueprint'
                JOIN blueprint_jobs job
                  ON job.id=json_extract(reference_blueprint.attrs_json, '$.job_id')
                JOIN artifact_versions source_version ON source_version.id=job.source_version_id
                WHERE d.project_id=? AND d.downstream_artifact_id=?
                  AND d.dependency_type='derives_from'
                """,
                (project_id, target_blueprint_id),
            ).fetchone()
        if row is None:
            raise ValueError("target blueprint is missing reference lineage")
        return str(row["reference_text"])

    def _reference_features_for_target(
        self, project_id: str, target_blueprint_id: str
    ) -> dict[str, Any]:
        with self.database.connect() as connection:
            lineage = connection.execute(
                """SELECT rb.current_version_id AS reference_version_id,
                          source_version.content AS reference_text
                   FROM artifact_dependencies d
                   JOIN artifacts rb ON rb.id=d.upstream_artifact_id
                     AND rb.artifact_type='reference_blueprint' AND rb.project_id=d.project_id
                   JOIN blueprint_jobs job ON job.id=json_extract(rb.attrs_json, '$.job_id')
                     AND job.project_id=d.project_id
                   JOIN artifact_versions source_version ON source_version.id=job.source_version_id
                   WHERE d.project_id=? AND d.downstream_artifact_id=? AND d.dependency_type='derives_from'""",
                (project_id, target_blueprint_id),
            ).fetchone()
        if lineage is None:
            raise ValueError("target blueprint is missing reference lineage")
        version_id = str(lineage["reference_version_id"])
        nodes = self.repository.list_nodes_for_version(project_id, version_id)
        evidence = self.repository.list_evidence_for_version(project_id, version_id, include_quotes=True)
        rare_phrases: list[str] = []
        style_fingerprints: list[str] = []
        reference_beats: list[dict[str, Any]] = []
        for node in nodes:
            style = node["dimensions"].get("style_statistics", {}).get("value")
            if isinstance(style, dict):
                rare_phrases.extend(str(item) for item in style.get("rare_phrases", []) if str(item).strip())
                style_fingerprints.extend(str(item) for item in style.get("fingerprints", []) if str(item).strip())
            if node["node_type"] == "beat":
                characters = node["dimensions"].get("characters", {}).get("value") or {}
                events = node["dimensions"].get("events", {}).get("value") or {}
                causality = node["dimensions"].get("causality", {}).get("value") or {}
                beat = {
                    "role_function": characters.get("role_function") if isinstance(characters, dict) else None,
                    "event_function": events.get("event_function") if isinstance(events, dict) else None,
                    "outcome": (
                        events.get("outcome") if isinstance(events, dict) and events.get("outcome") is not None
                        else causality.get("outcome") if isinstance(causality, dict) else None
                    ),
                }
                if any(value not in {None, ""} for value in beat.values()):
                    reference_beats.append(beat)
        return {
            "reference_text": str(lineage["reference_text"]),
            "quotes": [str(item.get("quote") or "") for item in evidence],
            "rare_phrases": list(dict.fromkeys(rare_phrases)),
            "style_fingerprints": list(dict.fromkeys(style_fingerprints)),
            "reference_beats": reference_beats,
        }

    def _assert_generation_request_safe(
        self,
        project_id: str,
        target_blueprint_id: str,
        unit_id: str,
        artifact_id: str,
        request_payload: dict[str, Any],
        reference_features: dict[str, Any],
        *,
        phase: str,
    ) -> None:
        serialized = json_dumps(request_payload)
        normalized_request = "".join(character.lower() for character in serialized if character.isalnum())
        matches: list[str] = []
        candidates = [
            ("reference_text", reference_features.get("reference_text", ""), 12),
            *[("reference_quote", value, 12) for value in reference_features.get("quotes", [])],
            *[("rare_phrase", value, 4) for value in reference_features.get("rare_phrases", [])],
            *[("style_fingerprint", value, 4) for value in reference_features.get("style_fingerprints", [])],
        ]
        for kind, value, minimum in candidates:
            normalized = "".join(character.lower() for character in str(value) if character.isalnum())
            if len(normalized) >= minimum and normalized in normalized_request:
                matches.append(kind)
        paths = DraftContextBuilder._find_forbidden(request_payload)
        if matches or paths:
            self.workflow.ledger.append(
                project_id, "context_firewall_blocked",
                {"target_blueprint_id": target_blueprint_id, "unit_id": unit_id,
                 "artifact_id": artifact_id, "phase": phase,
                 "finding_kinds": sorted(set(matches)), "finding_paths": paths},
                "security",
            )
            raise ContextFirewallError("reference content is forbidden in the actual generation request")

    def _run_generation_agent(
        self, project_id: str, job: dict[str, Any], name: str, context: dict[str, Any]
    ) -> dict[str, Any]:
        agent = self.registry.get(name)
        if agent is None:
            raise ValueError(f"automation_unavailable: missing {name}")
        base_key = f"generation:{job['id']}:{name}:prompt-v1"
        prior = [
            run for run in self.repository.list_agent_runs(project_id, job["id"])
            if run["agent_name"] == name and run["prompt_version"] == "prompt-v1"
        ]
        completed = next((run for run in reversed(prior) if run["status"] == "completed"), None)
        if completed is not None:
            return completed
        key = f"{base_key}:attempt:{len(prior) + 1}"
        task = AgentTask(
            project_id=project_id,
            job_id=job["id"],
            batch_id=None,
            source_version_id=None,
            context=context,
            allowed_context_types=("target_setting", "target_blueprint", "target_canon", "candidate"),
            prompt_version="prompt-v1",
            idempotency_key=key,
        )
        try:
            result = agent.run(task)
            validate_agent_payload(name, result.data, result.evidence, batch_length=None)
        except ValueError as exc:
            message = str(exc)
            error = message if message.startswith(f"{name} schema_failed:") else f"{name} schema_failed: {message}"
            self.repository.create_agent_run(
                project_id, job["id"], batch_id=None, agent_name=name,
                prompt_version=task.prompt_version, model={},
                input_hash=sha256_text(json.dumps(task.context, ensure_ascii=False, sort_keys=True)),
                output_hash=None, status="schema_failed", result={}, warnings=[],
                idempotency_key=key,
                diagnostic_hash=sha256_text(f"{type(exc).__name__}:{message}"),
                error_category="schema_failed",
            )
            raise ValueError(error) from exc
        except Exception as exc:
            self.repository.create_agent_run(
                project_id, job["id"], batch_id=None, agent_name=name,
                prompt_version=task.prompt_version, model={},
                input_hash=sha256_text(json.dumps(task.context, ensure_ascii=False, sort_keys=True)),
                output_hash=None, status="retryable_failed", result={}, warnings=[],
                idempotency_key=key,
                diagnostic_hash=sha256_text(f"{type(exc).__name__}:{exc}"),
                error_category="retryable_failed",
            )
            raise
        return self.repository.create_agent_run(
            project_id,
            job["id"],
            batch_id=None,
            agent_name=name,
            prompt_version=task.prompt_version,
            model=result.model,
            input_hash=result.input_hash,
            output_hash=result.output_hash,
            status="completed",
            result={"data": result.data, "evidence": [], "confidence": result.confidence},
            warnings=result.warnings,
            idempotency_key=key,
        )

    def create_draft_candidate(
        self, project_id: str, target_blueprint_id: str, unit_id: str, artifact_id: str
    ) -> dict[str, Any]:
        safe_context = DraftContextBuilder(self.database).build(
            project_id, target_blueprint_id, unit_id, artifact_id
        )
        reference_features = self._reference_features_for_target(project_id, target_blueprint_id)
        self._assert_generation_request_safe(
            project_id, target_blueprint_id, unit_id, artifact_id, safe_context["payload"],
            reference_features, phase="unit_planner_agent",
        )
        target = self.get_blueprint(project_id, target_blueprint_id, include_quotes=False)
        artifact = self.workflow.get_artifact(project_id, artifact_id)
        job = self.repository.create_job(
            project_id,
            job_type="draft",
            input_json={
                "target_blueprint_id": target_blueprint_id,
                "target_version_id": target["version"]["id"],
                "unit_id": unit_id,
                "artifact_id": artifact_id,
            },
            idempotency_key=f"draft:{new_id('request')}",
            source_version_id=target["version"]["id"],
        )
        planner = self._run_generation_agent(
            project_id, job, "unit_planner_agent", safe_context["payload"]
        )
        unit_plan = planner["result"]["data"].get("unit_plan", {})
        draft_input = {"target_context": safe_context["payload"], "unit_plan": unit_plan}
        self._assert_generation_request_safe(
            project_id, target_blueprint_id, unit_id, artifact_id, draft_input,
            reference_features, phase="draft_writer_agent",
        )
        writer = self._run_generation_agent(project_id, job, "draft_writer_agent", draft_input)
        candidate_text = str(writer["result"]["data"].get("draft") or "").strip()
        if not candidate_text:
            raise ValueError("draft writer returned empty candidate text")
        continuity = self._run_generation_agent(
            project_id,
            job,
            "continuity_review_agent",
            {"target_context": safe_context["payload"], "unit_plan": unit_plan, "candidate": candidate_text},
        )
        reference_text = str(reference_features["reference_text"])
        migration_job_id = target["artifact"]["attrs"].get("migration_job_id")
        mappings = self.repository.list_mappings(project_id, migration_job_id) if migration_job_id else []
        target_beats = list(unit_plan.get("beats") or []) if isinstance(unit_plan, dict) else []
        assessment = assess_similarity(
            candidate_text,
            reference_text,
            candidate_beats=target_beats,
            reference_beats=list(reference_features["reference_beats"]),
            mappings=mappings,
            rare_phrases=list(reference_features["rare_phrases"]),
            style_fingerprints=list(reference_features["style_fingerprints"]),
        )
        safety_run = self._run_generation_agent(
            project_id,
            job,
            "similarity_safety_agent",
            {
                "candidate": candidate_text,
                "reference_text": reference_text,
                "metrics": assessment.to_dict(),
                "instruction": "return metrics, locations and remediation only; never return reference passages",
            },
        )
        verdict = safety_run["result"].get("data", {}).get("verdict")
        if not isinstance(verdict, dict) or verdict.get("gate_status") not in {"passed", "review_required", "blocked"}:
            raise ValueError("similarity_safety_agent returned an invalid typed verdict")
        safety_findings = verdict.get("findings")
        if not isinstance(safety_findings, list) or any(not isinstance(item, dict) for item in safety_findings):
            raise ValueError("similarity_safety_agent findings must be typed objects")
        if DraftContextBuilder._find_forbidden(verdict):
            raise ValueError("similarity_safety_agent verdict contains forbidden reference fields")
        severity = {"passed": 0, "review_required": 1, "blocked": 2}
        gate_status = max(
            (assessment.gate_status, str(verdict["gate_status"])), key=lambda status: severity[status]
        )
        findings = [*assessment.findings, *safety_findings]
        candidate = self.repository.create_candidate(
            project_id,
            target_blueprint_version_id=target["version"]["id"],
            unit_id=unit_id,
            artifact_id=artifact_id,
            unit_plan=unit_plan,
            text=candidate_text,
            base_version_id=artifact.get("current_version_id"),
            generation_metadata={
                "job_id": job["id"],
                "planner_run_id": planner["id"],
                "writer_run_id": writer["id"],
                "continuity_run_id": continuity["id"],
                "safety_run_id": safety_run["id"],
                "context_provenance": safe_context["provenance"],
            },
        )
        stored_assessment = self.repository.create_similarity_assessment(
            project_id,
            candidate["id"],
            expression=assessment.expression,
            structure=assessment.structure,
            mechanism=assessment.mechanism,
            gate_status=gate_status,
            findings=findings,
        )
        updated = self.repository.update_candidate(
            project_id, candidate["id"], status=gate_status
        )
        self.repository.update_job(
            project_id,
            job["id"],
            status="completed",
            progress={"completed_agents": 4, "total_agents": 4},
            checkpoint={"candidate_id": candidate["id"], "gate_status": gate_status},
        )
        return {**updated, "similarity": stored_assessment}

    def get_candidate(self, project_id: str, candidate_id: str) -> dict[str, Any]:
        candidate = self.repository.get_candidate(project_id, candidate_id)
        return {
            **candidate,
            "similarity": self.repository.get_similarity_assessment(project_id, candidate_id),
        }

    def accept_candidate(
        self,
        project_id: str,
        candidate_id: str,
        *,
        expected_current_version_id: str | None,
    ) -> dict[str, Any]:
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT c.*, a.current_version_id FROM draft_candidates c
                   JOIN artifacts a ON a.id=c.artifact_id AND a.project_id=c.project_id
                   WHERE c.id=? AND c.project_id=?""",
                (candidate_id, project_id),
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown draft candidate: {candidate_id}")
            candidate = dict(row)
            if candidate["status"] == "accepted":
                raise VersionConflictError("Candidate has already been accepted")
            if candidate["status"] == "blocked":
                raise ValueError("blocked candidate cannot be accepted")
            if candidate["status"] == "review_required":
                raise ValueError("review_required candidate must be remediated and reassessed")
            if candidate["status"] != "passed":
                raise ValueError(f"candidate cannot be accepted from status {candidate['status']}")
            if not (
                expected_current_version_id
                == candidate["base_version_id"]
                == candidate["current_version_id"]
            ):
                raise VersionConflictError(
                    "Candidate base version, requested version, and artifact current version must match"
                )
            saved = self.workflow.save_artifact_version(
                project_id, candidate["artifact_id"], candidate["candidate_text"],
                expected_current_version_id=expected_current_version_id,
                change_summary="Accept similarity-gated draft candidate",
                source_kind="agent_candidate_accepted", actor="author",
                metadata={"candidate_id": candidate_id,
                          "target_blueprint_version_id": candidate["target_blueprint_version_id"]},
                connection=connection,
            )
            cursor = connection.execute(
                """UPDATE draft_candidates SET status='accepted', accepted_version_id=?, updated_at=?
                   WHERE id=? AND project_id=? AND status='passed'
                     AND base_version_id IS ?""",
                (saved["version"]["id"], utc_now(), candidate_id, project_id, expected_current_version_id),
            )
            if cursor.rowcount != 1:
                raise VersionConflictError("Candidate state changed while accepting")
            self.workflow.ledger.append(
                project_id, "draft_candidate.accepted",
                {"candidate_id": candidate_id, "version_id": saved["version"]["id"]},
                "author", connection=connection,
            )
        updated = self.repository.get_candidate(project_id, candidate_id)
        return {**updated, "version": saved["version"], "sync": saved["sync"]}

    def reject_candidate(
        self, project_id: str, candidate_id: str, *, reason: str
    ) -> dict[str, Any]:
        clean_reason = str(reason or "").strip()
        if not clean_reason:
            raise ValueError("candidate rejection reason is required")
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM draft_candidates WHERE id=? AND project_id=?",
                (candidate_id, project_id),
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown draft candidate: {candidate_id}")
            if row["status"] in {"accepted", "rejected"}:
                raise ValueError(f"{row['status']} candidate cannot be rejected")
            cursor = connection.execute(
                """UPDATE draft_candidates SET status='rejected', rejection_reason=?, updated_at=?
                   WHERE id=? AND project_id=? AND status NOT IN ('accepted', 'rejected')""",
                (clean_reason, utc_now(), candidate_id, project_id),
            )
            if cursor.rowcount != 1:
                raise VersionConflictError("Candidate state changed while rejecting")
            self.workflow.ledger.append(
                project_id, "draft_candidate.rejected",
                {"candidate_id": candidate_id, "reason": clean_reason},
                "author", connection=connection,
            )
        return self.repository.get_candidate(project_id, candidate_id)

    def confirm_target_blueprint(
        self,
        project_id: str,
        artifact_id: str,
        *,
        expected_current_version_id: str,
    ) -> dict[str, Any]:
        artifact = self.workflow.get_artifact(project_id, artifact_id)
        if artifact["artifact_type"] != "target_blueprint":
            raise ValueError("only target blueprints can be confirmed")
        if artifact["current_version_id"] != expected_current_version_id:
            raise VersionConflictError("Target blueprint version conflict")
        if artifact["status"] == "draft":
            self.workflow.transition_artifact_status(project_id, artifact_id, "ready_for_review", actor="author")
            self.workflow.transition_artifact_status(project_id, artifact_id, "approved", actor="author")
        now = utc_now()
        with self.database.connect() as connection:
            row = connection.execute("SELECT attrs_json FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
            attrs = json_loads(row["attrs_json"], {})
            attrs["confirmation_status"] = "confirmed"
            attrs["confirmed_version_id"] = expected_current_version_id
            connection.execute(
                "UPDATE artifacts SET attrs_json=?, updated_at=? WHERE id=?",
                (json_dumps(attrs), now, artifact_id),
            )
            self.workflow.ledger.append(
                project_id,
                "target_blueprint.confirmed",
                {"artifact_id": artifact_id, "version_id": expected_current_version_id},
                "author",
                connection=connection,
            )
        return self.get_blueprint(project_id, artifact_id)
