from __future__ import annotations

from collections import Counter
from contextlib import nullcontext
from typing import Any

from .db import Database
from .ledger import Ledger
from .util import json_dumps, json_loads, new_id, utc_now


STAGE_TRANSITIONS = {
    "not_started": {"in_progress", "skipped"},
    "in_progress": {"pending_review", "needs_revision", "skipped"},
    "pending_review": {"passed", "needs_revision"},
    "passed": {"stale", "locked"},
    "needs_revision": {"in_progress"},
    "stale": {"in_progress"},
    "locked": set(),
    "skipped": set(),
}

PRODUCTION_UNIT_TYPES = {
    "work",
    "volume",
    "chapter",
    "episode",
    "act",
    "scene",
    "sequence",
    "beat",
    "quest",
    "branch",
}

ARTIFACT_TRANSITIONS = {
    "empty": set(),
    "draft": {"ready_for_review"},
    "ready_for_review": {"approved", "needs_revision"},
    "approved": {"stale", "locked"},
    "needs_revision": {"draft"},
    "stale": {"draft"},
    "locked": set(),
}

DEPENDENCY_TYPES = {
    "contains",
    "requires",
    "constrains",
    "derives_from",
    "measures",
    "affects",
    "evidence_for",
    "adapts_from",
}


def _template_dict(row: Any) -> dict[str, Any]:
    result = dict(row)
    result["definition"] = json_loads(result.pop("definition_json"), {})
    return result


def _stage_dict(row: Any) -> dict[str, Any]:
    result = dict(row)
    result["entry_criteria"] = json_loads(result.pop("entry_criteria_json"), [])
    result["completion_criteria"] = json_loads(
        result.pop("completion_criteria_json"), {}
    )
    return result


def _unit_dict(row: Any) -> dict[str, Any]:
    result = dict(row)
    result["attrs"] = json_loads(result.pop("attrs_json"), {})
    return result


def _artifact_dict(row: Any) -> dict[str, Any]:
    result = dict(row)
    result["attrs"] = json_loads(result.pop("attrs_json"), {})
    return result


def _version_dict(row: Any) -> dict[str, Any]:
    result = dict(row)
    result["metadata"] = json_loads(result.pop("metadata_json"), {})
    return result


def _review_dict(row: Any) -> dict[str, Any]:
    result = dict(row)
    result["metadata"] = json_loads(result.pop("metadata_json"), {})
    return result


def _impact_dict(row: Any) -> dict[str, Any]:
    result = dict(row)
    result["dependency_path"] = json_loads(result.pop("dependency_path_json"), [])
    if result.get("source_title") and result.get("affected_title"):
        result["summary"] = (
            f"{result['source_title']} 的变更“{result.get('change_summary') or '正式保存'}”"
            f"影响 {result['affected_title']}"
        )
    return result


class VersionConflictError(ValueError):
    """Raised when an artifact save is based on a stale current version."""


class WorkflowService:
    def __init__(self, database: Database):
        self.database = database
        self.ledger = Ledger(database)

    def _require_project(self, project_id: str) -> None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT id FROM projects WHERE id=?", (project_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown project: {project_id}")

    def list_templates(self) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM workflow_templates ORDER BY template_key, version"
            ).fetchall()
        return [_template_dict(row) for row in rows]

    def instantiate_workflow(
        self,
        project_id: str,
        template_key: str,
        *,
        version: int | None = None,
        name: str | None = None,
    ) -> dict[str, Any]:
        self._require_project(project_id)
        clean_key = str(template_key or "").strip()
        if not clean_key:
            raise ValueError("template_key is required")
        now = utc_now()
        workflow_id = new_id("wf")
        with self.database.connect() as connection:
            existing = connection.execute(
                "SELECT id FROM project_workflows WHERE project_id=?", (project_id,)
            ).fetchone()
            if existing is not None:
                raise ValueError("Project already has a workflow")
            if version is None:
                template = connection.execute(
                    "SELECT * FROM workflow_templates WHERE template_key=? "
                    "ORDER BY version DESC LIMIT 1",
                    (clean_key,),
                ).fetchone()
            else:
                template = connection.execute(
                    "SELECT * FROM workflow_templates WHERE template_key=? AND version=?",
                    (clean_key, int(version)),
                ).fetchone()
            if template is None:
                raise KeyError(f"Unknown workflow template: {clean_key}")
            definition = json_loads(template["definition_json"], {})
            stages = list(definition.get("stages") or [])
            workflow_name = str(name or template["name"]).strip()
            if not workflow_name:
                raise ValueError("Workflow name is required")
            connection.execute(
                """
                INSERT INTO project_workflows(
                    id, project_id, template_id, media_type, name, status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?)
                """,
                (
                    workflow_id,
                    project_id,
                    template["id"],
                    template["media_type"],
                    workflow_name,
                    now,
                    now,
                ),
            )
            for position, stage in enumerate(stages, start=1):
                connection.execute(
                    """
                    INSERT INTO workflow_stages(
                        id, workflow_id, template_stage_key, position, name,
                        description, entry_criteria_json, completion_criteria_json,
                        status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'not_started', ?, ?)
                    """,
                    (
                        new_id("stage"),
                        workflow_id,
                        stage["key"],
                        position,
                        stage["name"],
                        stage["description"],
                        json_dumps(stage.get("entry_criteria") or []),
                        json_dumps(stage.get("completion_criteria") or {}),
                        now,
                        now,
                    ),
                )
            self.ledger.append(
                project_id,
                "workflow.instantiated",
                {
                    "workflow_id": workflow_id,
                    "template_key": clean_key,
                    "template_version": int(template["version"]),
                    "stage_count": len(stages),
                },
                connection=connection,
            )
        return self.get_project_workflow(project_id)

    def get_project_workflow(self, project_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            workflow = connection.execute(
                """
                SELECT pw.*, wt.template_key, wt.version AS template_version
                FROM project_workflows pw
                JOIN workflow_templates wt ON wt.id=pw.template_id
                WHERE pw.project_id=?
                """,
                (project_id,),
            ).fetchone()
            if workflow is None:
                raise KeyError(f"Project has no workflow: {project_id}")
            stage_rows = connection.execute(
                "SELECT * FROM workflow_stages WHERE workflow_id=? ORDER BY position",
                (workflow["id"],),
            ).fetchall()
        stages = [_stage_dict(row) for row in stage_rows]
        result = dict(workflow)
        result["stages"] = stages
        result["status_counts"] = dict(Counter(stage["status"] for stage in stages))
        return result

    def create_production_unit(
        self,
        project_id: str,
        unit_type: str,
        title: str,
        *,
        parent_id: str | None = None,
        position: int = 0,
        branch: str = "main",
        attrs: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        clean_type = str(unit_type or "").strip()
        if clean_type not in PRODUCTION_UNIT_TYPES:
            raise ValueError(f"Unsupported production unit type: {clean_type}")
        clean_title = str(title or "").strip()
        if not clean_title:
            raise ValueError("Production unit title is required")
        clean_branch = str(branch or "main").strip() or "main"
        now = utc_now()
        unit_id = new_id("unit")
        with self.database.connect() as connection:
            project = connection.execute(
                "SELECT id FROM projects WHERE id=?", (project_id,)
            ).fetchone()
            if project is None:
                raise KeyError(f"Unknown project: {project_id}")
            workflow = connection.execute(
                "SELECT id FROM project_workflows WHERE project_id=?", (project_id,)
            ).fetchone()
            if parent_id is not None:
                parent = connection.execute(
                    "SELECT project_id, branch FROM production_units WHERE id=?",
                    (parent_id,),
                ).fetchone()
                if parent is None:
                    raise KeyError(f"Unknown production unit: {parent_id}")
                if parent["project_id"] != project_id or parent["branch"] != clean_branch:
                    raise ValueError("Parent production unit must belong to the same project and branch")
            connection.execute(
                """
                INSERT INTO production_units(
                    id, project_id, workflow_id, parent_id, unit_type, title,
                    position, branch, attrs_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    unit_id,
                    project_id,
                    workflow["id"] if workflow else None,
                    parent_id,
                    clean_type,
                    clean_title,
                    int(position),
                    clean_branch,
                    json_dumps(attrs or {}),
                    now,
                    now,
                ),
            )
            self.ledger.append(
                project_id,
                "production_unit.created",
                {
                    "unit_id": unit_id,
                    "unit_type": clean_type,
                    "title": clean_title,
                    "parent_id": parent_id,
                    "branch": clean_branch,
                },
                connection=connection,
            )
            row = connection.execute(
                "SELECT * FROM production_units WHERE id=?", (unit_id,)
            ).fetchone()
        return _unit_dict(row)

    def transition_stage(
        self,
        project_id: str,
        stage_id: str,
        status: str,
        *,
        exception_reason: str | None = None,
        actor: str = "user",
    ) -> dict[str, Any]:
        next_status = str(status or "").strip()
        now = utc_now()
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT ws.* FROM workflow_stages ws
                JOIN project_workflows pw ON pw.id=ws.workflow_id
                WHERE ws.id=? AND pw.project_id=?
                """,
                (stage_id, project_id),
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown workflow stage: {stage_id}")
            current_status = str(row["status"])
            if next_status not in STAGE_TRANSITIONS.get(current_status, set()):
                raise ValueError(
                    f"Invalid stage transition: {current_status} -> {next_status}"
                )
            clean_reason = str(exception_reason or "").strip() or None
            if next_status == "skipped" and clean_reason is None:
                raise ValueError("Skip reason is required")
            if next_status == "passed":
                criteria = json_loads(row["completion_criteria_json"], {})
                required = set(criteria.get("required_artifact_types") or [])
                if required:
                    approved_rows = connection.execute(
                        """
                        SELECT DISTINCT artifact_type FROM artifacts
                        WHERE project_id=? AND workflow_stage_id=?
                          AND status IN ('approved', 'locked')
                        """,
                        (project_id, stage_id),
                    ).fetchall()
                    approved = {item["artifact_type"] for item in approved_rows}
                    missing = sorted(required - approved)
                    if missing:
                        raise ValueError(
                            "Required artifacts not approved: " + ", ".join(missing)
                        )
            connection.execute(
                """
                UPDATE workflow_stages
                SET status=?, exception_reason=?, updated_at=?
                WHERE id=?
                """,
                (next_status, clean_reason, now, stage_id),
            )
            self.ledger.append(
                project_id,
                "workflow_stage.transitioned",
                {
                    "stage_id": stage_id,
                    "from": current_status,
                    "to": next_status,
                    "exception_reason": clean_reason,
                },
                actor,
                connection=connection,
            )
            updated = connection.execute(
                "SELECT * FROM workflow_stages WHERE id=?", (stage_id,)
            ).fetchone()
        return _stage_dict(updated)

    def create_artifact(
        self,
        project_id: str,
        artifact_type: str,
        title: str,
        *,
        stage_id: str | None = None,
        unit_id: str | None = None,
        branch: str = "main",
        attrs: dict[str, Any] | None = None,
        actor: str = "user",
    ) -> dict[str, Any]:
        clean_type = str(artifact_type or "").strip()
        clean_title = str(title or "").strip()
        clean_branch = str(branch or "main").strip() or "main"
        if not clean_type:
            raise ValueError("Artifact type is required")
        if not clean_title:
            raise ValueError("Artifact title is required")
        now = utc_now()
        artifact_id = new_id("art")
        with self.database.connect() as connection:
            project = connection.execute(
                "SELECT id FROM projects WHERE id=?", (project_id,)
            ).fetchone()
            if project is None:
                raise KeyError(f"Unknown project: {project_id}")
            if stage_id is not None:
                stage = connection.execute(
                    """
                    SELECT ws.id, pw.project_id FROM workflow_stages ws
                    JOIN project_workflows pw ON pw.id=ws.workflow_id
                    WHERE ws.id=?
                    """,
                    (stage_id,),
                ).fetchone()
                if stage is None:
                    raise KeyError(f"Unknown workflow stage: {stage_id}")
                if stage["project_id"] != project_id:
                    raise ValueError("Workflow stage must belong to the same project")
            if unit_id is not None:
                unit = connection.execute(
                    "SELECT project_id, branch FROM production_units WHERE id=?",
                    (unit_id,),
                ).fetchone()
                if unit is None:
                    raise KeyError(f"Unknown production unit: {unit_id}")
                if unit["project_id"] != project_id or unit["branch"] != clean_branch:
                    raise ValueError(
                        "Production unit must belong to the same project and branch"
                    )
            connection.execute(
                """
                INSERT INTO artifacts(
                    id, project_id, workflow_stage_id, production_unit_id,
                    artifact_type, title, status, branch, attrs_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'empty', ?, ?, ?, ?)
                """,
                (
                    artifact_id,
                    project_id,
                    stage_id,
                    unit_id,
                    clean_type,
                    clean_title,
                    clean_branch,
                    json_dumps(attrs or {}),
                    now,
                    now,
                ),
            )
            self.ledger.append(
                project_id,
                "artifact.created",
                {
                    "artifact_id": artifact_id,
                    "artifact_type": clean_type,
                    "title": clean_title,
                    "stage_id": stage_id,
                    "unit_id": unit_id,
                },
                actor,
                connection=connection,
            )
            row = connection.execute(
                "SELECT * FROM artifacts WHERE id=?", (artifact_id,)
            ).fetchone()
        return _artifact_dict(row)

    def get_artifact(self, project_id: str, artifact_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM artifacts WHERE id=? AND project_id=?",
                (artifact_id, project_id),
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown artifact: {artifact_id}")
        return _artifact_dict(row)

    def transition_artifact_status(
        self,
        project_id: str,
        artifact_id: str,
        status: str,
        *,
        actor: str = "user",
    ) -> dict[str, Any]:
        next_status = str(status or "").strip()
        now = utc_now()
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM artifacts WHERE id=? AND project_id=?",
                (artifact_id, project_id),
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown artifact: {artifact_id}")
            current_status = str(row["status"])
            if next_status not in ARTIFACT_TRANSITIONS.get(current_status, set()):
                raise ValueError(
                    f"Invalid artifact transition: {current_status} -> {next_status}"
                )
            connection.execute(
                "UPDATE artifacts SET status=?, updated_at=? WHERE id=?",
                (next_status, now, artifact_id),
            )
            self.ledger.append(
                project_id,
                "artifact.transitioned",
                {"artifact_id": artifact_id, "from": current_status, "to": next_status},
                actor,
                connection=connection,
            )
            updated = connection.execute(
                "SELECT * FROM artifacts WHERE id=?", (artifact_id,)
            ).fetchone()
        return _artifact_dict(updated)

    def save_artifact_version(
        self,
        project_id: str,
        artifact_id: str,
        content: str,
        *,
        expected_current_version_id: str | None,
        change_summary: str,
        source_kind: str = "user",
        actor: str = "user",
        metadata: dict[str, Any] | None = None,
        connection: Any | None = None,
    ) -> dict[str, Any]:
        clean_summary = str(change_summary or "").strip()
        if not clean_summary:
            raise ValueError("Change summary is required")
        now = utc_now()
        version_id = new_id("ver")
        impact_ids: list[str] = []
        stale_review_ids: list[str] = []
        owns_connection = connection is None
        with (self.database.connect() if owns_connection else nullcontext(connection)) as connection:
            # Serialize the optimistic-version check with the write. A deferred
            # transaction lets two writers both observe the same current
            # version and then collide on version_number instead of returning
            # the domain-level conflict promised by the API.
            if owns_connection:
                connection.execute("BEGIN IMMEDIATE")
            artifact = connection.execute(
                "SELECT * FROM artifacts WHERE id=? AND project_id=?",
                (artifact_id, project_id),
            ).fetchone()
            if artifact is None:
                raise KeyError(f"Unknown artifact: {artifact_id}")
            if artifact["status"] == "locked":
                raise ValueError("Cannot save a locked artifact")
            current_version_id = artifact["current_version_id"]
            if current_version_id != expected_current_version_id:
                raise VersionConflictError(
                    "Artifact version conflict: "
                    f"expected {expected_current_version_id!r}, current {current_version_id!r}"
                )
            version_number = int(
                connection.execute(
                    "SELECT COALESCE(MAX(version_number), 0) + 1 AS n "
                    "FROM artifact_versions WHERE artifact_id=?",
                    (artifact_id,),
                ).fetchone()["n"]
            )
            connection.execute(
                """
                INSERT INTO artifact_versions(
                    id, artifact_id, version_number, parent_version_id,
                    content, content_format, source_kind, change_summary,
                    actor, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, 'text/plain', ?, ?, ?, ?, ?)
                """,
                (
                    version_id,
                    artifact_id,
                    version_number,
                    current_version_id,
                    str(content),
                    str(source_kind or "user"),
                    clean_summary,
                    actor,
                    json_dumps(metadata or {}),
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE artifacts
                SET current_version_id=?, status='draft', updated_at=?
                WHERE id=?
                """,
                (version_id, now, artifact_id),
            )

            path_rows = connection.execute(
                """
                WITH RECURSIVE paths(artifact_id, path_text, depth) AS (
                    SELECT downstream_artifact_id,
                           upstream_artifact_id || '|' || downstream_artifact_id,
                           1
                    FROM artifact_dependencies
                    WHERE project_id=? AND upstream_artifact_id=?
                    UNION ALL
                    SELECT edge.downstream_artifact_id,
                           paths.path_text || '|' || edge.downstream_artifact_id,
                           paths.depth + 1
                    FROM paths
                    JOIN artifact_dependencies edge
                      ON edge.project_id=?
                     AND edge.upstream_artifact_id=paths.artifact_id
                    WHERE instr('|' || paths.path_text || '|',
                                '|' || edge.downstream_artifact_id || '|')=0
                )
                SELECT artifact_id, path_text, depth
                FROM paths ORDER BY depth, path_text
                """,
                (project_id, artifact_id, project_id),
            ).fetchall()
            affected: dict[str, tuple[list[str], int]] = {}
            for path_row in path_rows:
                target_id = str(path_row["artifact_id"])
                if target_id not in affected:
                    affected[target_id] = (
                        str(path_row["path_text"]).split("|"),
                        int(path_row["depth"]),
                    )

            review_targets = [artifact_id, *affected.keys()]
            placeholders = ",".join("?" for _ in review_targets)
            review_rows = connection.execute(
                f"SELECT id FROM reviews WHERE project_id=? "
                f"AND artifact_id IN ({placeholders}) AND status='valid'",
                [project_id, *review_targets],
            ).fetchall()
            stale_review_ids = [str(row["id"]) for row in review_rows]
            if stale_review_ids:
                review_placeholders = ",".join("?" for _ in stale_review_ids)
                connection.execute(
                    f"UPDATE reviews SET status='stale', stale_at=?, updated_at=? "
                    f"WHERE id IN ({review_placeholders})",
                    [now, now, *stale_review_ids],
                )
            if affected:
                affected_ids = list(affected)
                affected_placeholders = ",".join("?" for _ in affected_ids)
                connection.execute(
                    f"UPDATE artifacts SET status='stale', updated_at=? "
                    f"WHERE id IN ({affected_placeholders}) "
                    "AND status IN ('draft', 'ready_for_review', 'approved', 'needs_revision')",
                    [now, *affected_ids],
                )
                for affected_id, (path, depth) in affected.items():
                    impact_id = new_id("impact")
                    impact_ids.append(impact_id)
                    connection.execute(
                        """
                        INSERT INTO impact_records(
                            id, project_id, change_type, source_artifact_id,
                            source_version_id, affected_artifact_id,
                            dependency_path_json, risk_level, status,
                            created_at, updated_at
                        ) VALUES (?, ?, 'artifact.version_created', ?, ?, ?, ?, ?, 'open', ?, ?)
                        """,
                        (
                            impact_id,
                            project_id,
                            artifact_id,
                            version_id,
                            affected_id,
                            json_dumps(path),
                            "high" if depth == 1 else "medium",
                            now,
                            now,
                        ),
                    )
            sync = {
                "stale_review_ids": stale_review_ids,
                "impact_ids": impact_ids,
                "affected_artifact_ids": list(affected),
            }
            self.ledger.append(
                project_id,
                "artifact.version_created",
                {
                    "artifact_id": artifact_id,
                    "version_id": version_id,
                    "version_number": version_number,
                    "parent_version_id": current_version_id,
                    "change_summary": clean_summary,
                    "source_kind": str(source_kind or "user"),
                    "sync": sync,
                },
                actor,
                connection=connection,
            )
            version = connection.execute(
                "SELECT * FROM artifact_versions WHERE id=?", (version_id,)
            ).fetchone()
            updated_artifact = connection.execute(
                "SELECT * FROM artifacts WHERE id=?", (artifact_id,)
            ).fetchone()
        return {
            "artifact": _artifact_dict(updated_artifact),
            "version": _version_dict(version),
            "sync": sync,
        }

    def list_artifact_versions(
        self, project_id: str, artifact_id: str
    ) -> list[dict[str, Any]]:
        self.get_artifact(project_id, artifact_id)
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM artifact_versions WHERE artifact_id=? ORDER BY version_number",
                (artifact_id,),
            ).fetchall()
        return [_version_dict(row) for row in rows]

    def add_dependency(
        self,
        project_id: str,
        upstream_artifact_id: str,
        downstream_artifact_id: str,
        dependency_type: str,
        *,
        actor: str = "user",
    ) -> dict[str, Any]:
        clean_type = str(dependency_type or "").strip()
        if clean_type not in DEPENDENCY_TYPES:
            raise ValueError(f"Unsupported dependency type: {clean_type}")
        if upstream_artifact_id == downstream_artifact_id:
            raise ValueError("An artifact cannot depend on itself")
        dependency_id = new_id("dep")
        now = utc_now()
        with self.database.connect() as connection:
            artifacts = connection.execute(
                "SELECT id, project_id FROM artifacts WHERE id IN (?, ?)",
                (upstream_artifact_id, downstream_artifact_id),
            ).fetchall()
            by_id = {row["id"]: row for row in artifacts}
            missing = [
                artifact_id
                for artifact_id in (upstream_artifact_id, downstream_artifact_id)
                if artifact_id not in by_id
            ]
            if missing:
                raise KeyError(f"Unknown artifact: {missing[0]}")
            if (
                by_id[upstream_artifact_id]["project_id"] != project_id
                or by_id[downstream_artifact_id]["project_id"] != project_id
            ):
                raise ValueError("Both artifacts must belong to the same project")
            cycle = connection.execute(
                """
                WITH RECURSIVE reachable(id) AS (
                    SELECT ?
                    UNION
                    SELECT edge.downstream_artifact_id
                    FROM artifact_dependencies edge
                    JOIN reachable ON edge.upstream_artifact_id=reachable.id
                    WHERE edge.project_id=?
                )
                SELECT 1 FROM reachable WHERE id=? LIMIT 1
                """,
                (downstream_artifact_id, project_id, upstream_artifact_id),
            ).fetchone()
            if cycle is not None:
                raise ValueError("Artifact dependency would create a cycle")
            duplicate = connection.execute(
                """
                SELECT id FROM artifact_dependencies
                WHERE project_id=? AND upstream_artifact_id=?
                  AND downstream_artifact_id=? AND dependency_type=?
                """,
                (
                    project_id,
                    upstream_artifact_id,
                    downstream_artifact_id,
                    clean_type,
                ),
            ).fetchone()
            if duplicate is not None:
                raise ValueError("Artifact dependency already exists")
            connection.execute(
                """
                INSERT INTO artifact_dependencies(
                    id, project_id, upstream_artifact_id,
                    downstream_artifact_id, dependency_type, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    dependency_id,
                    project_id,
                    upstream_artifact_id,
                    downstream_artifact_id,
                    clean_type,
                    now,
                ),
            )
            self.ledger.append(
                project_id,
                "artifact_dependency.created",
                {
                    "dependency_id": dependency_id,
                    "upstream_artifact_id": upstream_artifact_id,
                    "downstream_artifact_id": downstream_artifact_id,
                    "dependency_type": clean_type,
                },
                actor,
                connection=connection,
            )
        return {
            "id": dependency_id,
            "project_id": project_id,
            "upstream_artifact_id": upstream_artifact_id,
            "downstream_artifact_id": downstream_artifact_id,
            "dependency_type": clean_type,
            "created_at": now,
        }

    def create_review(
        self,
        project_id: str,
        artifact_id: str,
        review_type: str,
        input_version_id: str,
        *,
        summary: str = "",
        actor: str = "user",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        clean_type = str(review_type or "").strip()
        if not clean_type:
            raise ValueError("Review type is required")
        review_id = new_id("review")
        now = utc_now()
        with self.database.connect() as connection:
            artifact = connection.execute(
                "SELECT * FROM artifacts WHERE id=? AND project_id=?",
                (artifact_id, project_id),
            ).fetchone()
            if artifact is None:
                raise KeyError(f"Unknown artifact: {artifact_id}")
            if artifact["current_version_id"] != input_version_id:
                raise VersionConflictError(
                    "Review input version conflict: review must use the current artifact version"
                )
            version = connection.execute(
                "SELECT id FROM artifact_versions WHERE id=? AND artifact_id=?",
                (input_version_id, artifact_id),
            ).fetchone()
            if version is None:
                raise ValueError("Review input version must belong to the artifact")
            connection.execute(
                """
                INSERT INTO reviews(
                    id, project_id, artifact_id, review_type,
                    input_version_id, status, summary, metadata_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'valid', ?, ?, ?, ?)
                """,
                (
                    review_id,
                    project_id,
                    artifact_id,
                    clean_type,
                    input_version_id,
                    str(summary or ""),
                    json_dumps(metadata or {}),
                    now,
                    now,
                ),
            )
            self.ledger.append(
                project_id,
                "review.created",
                {
                    "review_id": review_id,
                    "artifact_id": artifact_id,
                    "review_type": clean_type,
                    "input_version_id": input_version_id,
                },
                actor,
                connection=connection,
            )
            row = connection.execute(
                "SELECT * FROM reviews WHERE id=?", (review_id,)
            ).fetchone()
        return _review_dict(row)

    def list_impacts(
        self, project_id: str, *, status: str | None = None
    ) -> list[dict[str, Any]]:
        self._require_project(project_id)
        where = ["project_id=?"]
        params: list[Any] = [project_id]
        if status is not None:
            where.append("status=?")
            params.append(str(status))
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT ir.*, source.title AS source_title, "
                "affected.title AS affected_title, av.change_summary "
                "FROM impact_records ir "
                "JOIN artifacts source ON source.id=ir.source_artifact_id "
                "JOIN artifacts affected ON affected.id=ir.affected_artifact_id "
                "JOIN artifact_versions av ON av.id=ir.source_version_id "
                f"WHERE {' AND '.join('ir.' + clause for clause in where)} "
                "ORDER BY ir.created_at, ir.id",
                params,
            ).fetchall()
        return [_impact_dict(row) for row in rows]
