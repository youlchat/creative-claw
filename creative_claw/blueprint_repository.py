from __future__ import annotations

from contextlib import nullcontext
from typing import Any

from .blueprint_models import validate_node
from .db import Database
from .util import json_dumps, json_loads, new_id, utc_now


def _decode(row: Any, *json_fields: str) -> dict[str, Any]:
    result = dict(row)
    for field in json_fields:
        raw_name = f"{field}_json"
        if raw_name in result:
            result[field] = json_loads(result.pop(raw_name))
    return result


class BlueprintRepository:
    """Project-scoped persistence for blueprint runtime objects."""

    def __init__(self, database: Database):
        self.database = database

    def _require_project(self, connection: Any, project_id: str) -> None:
        if connection.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone() is None:
            raise KeyError(f"Unknown project: {project_id}")

    def create_job(
        self,
        project_id: str,
        *,
        job_type: str,
        input_json: dict[str, Any],
        idempotency_key: str,
        status: str = "pending",
        desired_state: str = "running",
        rights_basis: str | None = None,
        source_document_id: str | None = None,
        source_version_id: str | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        identifier = new_id("bpjob")
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_project(connection, project_id)
            if source_document_id is not None and connection.execute(
                "SELECT 1 FROM documents WHERE id=? AND project_id=?", (source_document_id, project_id)
            ).fetchone() is None:
                raise KeyError(f"Unknown document: {source_document_id}")
            if source_version_id is not None and connection.execute(
                """SELECT 1 FROM artifact_versions av JOIN artifacts a ON a.id=av.artifact_id
                   WHERE av.id=? AND a.project_id=?""", (source_version_id, project_id)
            ).fetchone() is None:
                raise KeyError(f"Unknown artifact version: {source_version_id}")
            existing = connection.execute(
                "SELECT * FROM blueprint_jobs WHERE project_id=? AND idempotency_key=?",
                (project_id, idempotency_key),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO blueprint_jobs(
                        id, project_id, job_type, status, desired_state, input_json,
                        rights_basis, source_document_id, source_version_id, progress_json, checkpoint_json,
                        error_json, idempotency_key, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', '{}', '{}', ?, ?, ?)
                    """,
                    (
                        identifier,
                        project_id,
                        job_type,
                        status,
                        desired_state,
                        json_dumps(input_json),
                        rights_basis,
                        source_document_id,
                        source_version_id,
                        idempotency_key,
                        now,
                        now,
                    ),
                )
                existing = connection.execute(
                    "SELECT * FROM blueprint_jobs WHERE id=?", (identifier,)
                ).fetchone()
        return _decode(existing, "input", "progress", "checkpoint", "error")

    def get_job(self, project_id: str, job_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM blueprint_jobs WHERE id=? AND project_id=?",
                (job_id, project_id),
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown blueprint job: {job_id}")
        return _decode(row, "input", "progress", "checkpoint", "error")

    def update_job(
        self,
        project_id: str,
        job_id: str,
        *,
        status: str | None = None,
        desired_state: str | None = None,
        progress: dict[str, Any] | None = None,
        checkpoint: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
        output_artifact_id: str | None = None,
    ) -> dict[str, Any]:
        self.get_job(project_id, job_id)
        now = utc_now()
        assignments: list[str] = []
        values: list[Any] = []
        for column, value in (
            ("status", status),
            ("desired_state", desired_state),
            ("progress_json", json_dumps(progress) if progress is not None else None),
            ("checkpoint_json", json_dumps(checkpoint) if checkpoint is not None else None),
            ("error_json", json_dumps(error) if error is not None else None),
            ("output_artifact_id", output_artifact_id),
        ):
            if value is not None:
                assignments.append(f"{column}=?")
                values.append(value)
        assignments.append("updated_at=?")
        values.append(now)
        values.extend([job_id, project_id])
        with self.database.connect() as connection:
            connection.execute(
                f"UPDATE blueprint_jobs SET {', '.join(assignments)} WHERE id=? AND project_id=?",
                values,
            )
        return self.get_job(project_id, job_id)

    def complete_job_if_running(
        self,
        project_id: str,
        job_id: str,
        *,
        progress: dict[str, Any],
        checkpoint: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Atomically complete a job only while the user's desired state is running."""
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE blueprint_jobs
                SET status='completed', progress_json=?, checkpoint_json=?, error_json='{}', updated_at=?
                WHERE id=? AND project_id=? AND status='running' AND desired_state='running'
                  AND NOT EXISTS (
                    SELECT 1 FROM blueprint_batches
                    WHERE job_id=blueprint_jobs.id AND status!='completed'
                  )
                """,
                (json_dumps(progress), json_dumps(checkpoint), now, job_id, project_id),
            )
            if cursor.rowcount != 1:
                return None
            row = connection.execute(
                "SELECT * FROM blueprint_jobs WHERE id=? AND project_id=?", (job_id, project_id)
            ).fetchone()
        return _decode(row, "input", "progress", "checkpoint", "error")

    def set_job_desired_state(self, project_id: str, job_id: str, desired_state: str) -> dict[str, Any]:
        if desired_state not in {"running", "paused", "cancelled"}:
            raise ValueError(f"unsupported blueprint job desired state: {desired_state}")
        return self.update_job(project_id, job_id, desired_state=desired_state)

    def create_batch(
        self,
        project_id: str,
        job_id: str,
        *,
        ordinal: int,
        start_offset: int,
        end_offset: int,
        overlap_start: int,
        source_hash: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        self.get_job(project_id, job_id)
        identifier = new_id("bpbatch")
        now = utc_now()
        with self.database.connect() as connection:
            existing = connection.execute(
                "SELECT * FROM blueprint_batches WHERE job_id=? AND ordinal=?",
                (job_id, ordinal),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO blueprint_batches(
                        id, project_id, job_id, ordinal, start_offset, end_offset,
                        overlap_start, source_hash, status, checkpoint_json,
                        idempotency_key, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', '{}', ?, ?, ?)
                    """,
                    (
                        identifier,
                        project_id,
                        job_id,
                        ordinal,
                        start_offset,
                        end_offset,
                        overlap_start,
                        source_hash,
                        idempotency_key,
                        now,
                        now,
                    ),
                )
                existing = connection.execute(
                    "SELECT * FROM blueprint_batches WHERE id=?", (identifier,)
                ).fetchone()
        return _decode(existing, "checkpoint")

    def list_batches(self, project_id: str, job_id: str) -> list[dict[str, Any]]:
        self.get_job(project_id, job_id)
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM blueprint_batches WHERE project_id=? AND job_id=? ORDER BY ordinal",
                (project_id, job_id),
            ).fetchall()
        return [_decode(row, "checkpoint") for row in rows]

    def update_batch(
        self,
        project_id: str,
        batch_id: str,
        *,
        status: str,
        checkpoint: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM blueprint_batches WHERE id=? AND project_id=?",
                (batch_id, project_id),
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown blueprint batch: {batch_id}")
            connection.execute(
                "UPDATE blueprint_batches SET status=?, checkpoint_json=?, updated_at=? WHERE id=?",
                (status, json_dumps(checkpoint if checkpoint is not None else json_loads(row["checkpoint_json"])), now, batch_id),
            )
            updated = connection.execute("SELECT * FROM blueprint_batches WHERE id=?", (batch_id,)).fetchone()
        return _decode(updated, "checkpoint")

    def complete_batch_if_job_running(self, project_id: str, batch_id: str) -> bool:
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE blueprint_batches
                SET status='completed', updated_at=?
                WHERE id=? AND project_id=? AND status='running'
                  AND EXISTS (
                    SELECT 1 FROM blueprint_jobs j
                    WHERE j.id=blueprint_batches.job_id
                      AND j.project_id=? AND j.desired_state='running' AND j.status='running'
                  )
                """,
                (now, batch_id, project_id, project_id),
            )
        return cursor.rowcount == 1

    def get_agent_run_by_key(
        self, project_id: str, job_id: str, idempotency_key: str
    ) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM blueprint_agent_runs WHERE project_id=? AND job_id=? AND idempotency_key=?",
                (project_id, job_id, idempotency_key),
            ).fetchone()
        return None if row is None else _decode(row, "model", "result", "warnings")

    def create_agent_run(
        self,
        project_id: str,
        job_id: str,
        *,
        batch_id: str | None,
        agent_name: str,
        prompt_version: str,
        model: dict[str, Any],
        input_hash: str,
        output_hash: str | None,
        status: str,
        result: dict[str, Any],
        warnings: list[str],
        idempotency_key: str,
        diagnostic_hash: str | None = None,
        error_category: str | None = None,
    ) -> dict[str, Any]:
        existing = self.get_agent_run_by_key(project_id, job_id, idempotency_key)
        if existing is not None:
            return existing
        identifier = new_id("bprun")
        now = utc_now()
        with self.database.connect() as connection:
            if batch_id is not None and connection.execute(
                "SELECT 1 FROM blueprint_batches WHERE id=? AND project_id=? AND job_id=?",
                (batch_id, project_id, job_id),
            ).fetchone() is None:
                raise KeyError(f"Unknown blueprint batch: {batch_id}")
            connection.execute(
                """
                INSERT INTO blueprint_agent_runs(
                    id, project_id, job_id, batch_id, agent_name, prompt_version,
                    model_json, input_hash, output_hash, status, result_json,
                    warnings_json, diagnostic_hash, error_category, idempotency_key,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identifier,
                    project_id,
                    job_id,
                    batch_id,
                    agent_name,
                    prompt_version,
                    json_dumps(model),
                    input_hash,
                    output_hash,
                    status,
                    json_dumps(result),
                    json_dumps(warnings),
                    diagnostic_hash,
                    error_category,
                    idempotency_key,
                    now,
                    now,
                ),
            )
            row = connection.execute("SELECT * FROM blueprint_agent_runs WHERE id=?", (identifier,)).fetchone()
        return _decode(row, "model", "result", "warnings")

    def list_agent_runs(self, project_id: str, job_id: str) -> list[dict[str, Any]]:
        self.get_job(project_id, job_id)
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM blueprint_agent_runs WHERE project_id=? AND job_id=? ORDER BY created_at, id",
                (project_id, job_id),
            ).fetchall()
        return [_decode(row, "model", "result", "warnings") for row in rows]

    def create_node(
        self,
        project_id: str,
        *,
        artifact_version_id: str | None,
        job_id: str | None,
        stable_key: str,
        node_type: str,
        dimensions: dict[str, Any],
        parent_id: str | None = None,
        title: str = "",
        summary: str = "",
        source_locator: dict[str, Any] | None = None,
        status: str = "candidate",
        confidence: float = 1.0,
        agent_run_ids: list[str] | None = None,
        connection: Any | None = None,
    ) -> dict[str, Any]:
        validated = validate_node(
            {"stable_key": stable_key, "node_type": node_type, "dimensions": dimensions}
        )
        identifier = new_id("bpnode")
        now = utc_now()
        with (nullcontext(connection) if connection is not None else self.database.connect()) as active:
            self._require_project(active, project_id)
            if artifact_version_id is not None and active.execute(
                """
                SELECT 1 FROM artifact_versions av JOIN artifacts a ON a.id=av.artifact_id
                WHERE av.id=? AND a.project_id=?
                """, (artifact_version_id, project_id)
            ).fetchone() is None:
                raise KeyError(f"Unknown artifact version: {artifact_version_id}")
            if job_id is not None and active.execute(
                "SELECT 1 FROM blueprint_jobs WHERE id=? AND project_id=?", (job_id, project_id)
            ).fetchone() is None:
                raise KeyError(f"Unknown blueprint job: {job_id}")
            if parent_id is not None and active.execute(
                "SELECT 1 FROM blueprint_nodes WHERE id=? AND project_id=?", (parent_id, project_id)
            ).fetchone() is None:
                raise KeyError(f"Unknown blueprint node: {parent_id}")
            for run_id in agent_run_ids or []:
                if active.execute(
                    "SELECT 1 FROM blueprint_agent_runs WHERE id=? AND project_id=?", (run_id, project_id)
                ).fetchone() is None:
                    raise KeyError(f"Unknown blueprint agent run: {run_id}")
            active.execute(
                """
                INSERT INTO blueprint_nodes(
                    id, project_id, artifact_version_id, job_id, parent_id,
                    stable_key, node_type, title, summary, source_locator_json,
                    dimensions_json, status, confidence, agent_run_ids_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identifier,
                    project_id,
                    artifact_version_id,
                    job_id,
                    parent_id,
                    validated["stable_key"],
                    validated["node_type"],
                    title,
                    summary,
                    json_dumps(source_locator or {}),
                    json_dumps(validated["dimensions"]),
                    status,
                    float(confidence),
                    json_dumps(agent_run_ids or []),
                    now,
                    now,
                ),
            )
            row = active.execute("SELECT * FROM blueprint_nodes WHERE id=?", (identifier,)).fetchone()
        return _decode(row, "source_locator", "dimensions", "agent_run_ids")

    def get_node(self, project_id: str, node_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM blueprint_nodes WHERE id=? AND project_id=?", (node_id, project_id)
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown blueprint node: {node_id}")
        return _decode(row, "source_locator", "dimensions", "agent_run_ids")

    def list_nodes_for_version(
        self, project_id: str, artifact_version_id: str
    ) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            version = connection.execute(
                """
                SELECT av.id FROM artifact_versions av
                JOIN artifacts a ON a.id=av.artifact_id
                WHERE av.id=? AND a.project_id=?
                """,
                (artifact_version_id, project_id),
            ).fetchone()
            if version is None:
                raise KeyError(f"Unknown artifact version: {artifact_version_id}")
            rows = connection.execute(
                """SELECT * FROM blueprint_nodes WHERE project_id=? AND artifact_version_id=?
                   ORDER BY CASE node_type
                     WHEN 'work' THEN 0 WHEN 'volume' THEN 1 WHEN 'phase' THEN 1
                     WHEN 'chapter' THEN 2 WHEN 'episode' THEN 2 WHEN 'scene' THEN 3
                     WHEN 'beat' THEN 4 ELSE 5 END, created_at, stable_key, id""",
                (project_id, artifact_version_id),
            ).fetchall()
        return [_decode(row, "source_locator", "dimensions", "agent_run_ids") for row in rows]

    def create_evidence(
        self,
        project_id: str,
        node_id: str,
        *,
        start: int,
        end: int,
        source_length: int,
        quote: str,
        confidence: float,
        agent_run_id: str | None,
        source_document_id: str | None = None,
        chunk_id: str | None = None,
        locator: dict[str, Any] | None = None,
        interpretation_id: str | None = None,
        evidence_id: str | None = None,
        connection: Any | None = None,
    ) -> dict[str, Any]:
        from .blueprint_models import validate_evidence

        checked = validate_evidence(
            {"start": start, "end": end, "source_length": source_length, "confidence": confidence}
        )
        identifier = evidence_id or new_id("bpev")
        now = utc_now()
        with (nullcontext(connection) if connection is not None else self.database.connect()) as active:
            if active.execute(
                "SELECT 1 FROM blueprint_nodes WHERE id=? AND project_id=?", (node_id, project_id)
            ).fetchone() is None:
                raise KeyError(f"Unknown blueprint node: {node_id}")
            if interpretation_id is not None and active.execute(
                "SELECT 1 FROM blueprint_interpretations WHERE id=? AND project_id=?",
                (interpretation_id, project_id),
            ).fetchone() is None:
                raise KeyError(f"Unknown blueprint interpretation: {interpretation_id}")
            if agent_run_id is not None and active.execute(
                "SELECT 1 FROM blueprint_agent_runs WHERE id=? AND project_id=?", (agent_run_id, project_id)
            ).fetchone() is None:
                raise KeyError(f"Unknown blueprint agent run: {agent_run_id}")
            if source_document_id is not None and active.execute(
                "SELECT 1 FROM documents WHERE id=? AND project_id=?", (source_document_id, project_id)
            ).fetchone() is None:
                raise KeyError(f"Unknown document: {source_document_id}")
            if chunk_id is not None and active.execute(
                "SELECT 1 FROM chunks WHERE id=? AND project_id=?", (chunk_id, project_id)
            ).fetchone() is None:
                raise KeyError(f"Unknown chunk: {chunk_id}")
            active.execute(
                """
                INSERT INTO blueprint_evidence(
                    id, project_id, node_id, interpretation_id, source_document_id,
                    chunk_id, start_offset, end_offset, source_length, quote,
                    locator_json, agent_run_id, confidence, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identifier,
                    project_id,
                    node_id,
                    interpretation_id,
                    source_document_id,
                    chunk_id,
                    checked["start"],
                    checked["end"],
                    checked["source_length"],
                    str(quote),
                    json_dumps(locator or {}),
                    agent_run_id,
                    checked.get("confidence", confidence),
                    now,
                ),
            )
            row = active.execute("SELECT * FROM blueprint_evidence WHERE id=?", (identifier,)).fetchone()
        result = _decode(row, "locator")
        result["start"] = result.pop("start_offset")
        result["end"] = result.pop("end_offset")
        return result

    def list_evidence_for_version(
        self, project_id: str, artifact_version_id: str, *, include_quotes: bool = True
    ) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT e.* FROM blueprint_evidence e
                JOIN blueprint_nodes n ON n.id=e.node_id
                WHERE e.project_id=? AND n.artifact_version_id=?
                ORDER BY e.created_at, e.id
                """,
                (project_id, artifact_version_id),
            ).fetchall()
        result = []
        for row in rows:
            item = _decode(row, "locator")
            item["start"] = item.pop("start_offset")
            item["end"] = item.pop("end_offset")
            if not include_quotes:
                item.pop("quote", None)
            result.append(item)
        return result

    def create_interpretation(
        self,
        project_id: str,
        node_id: str,
        *,
        dimension: str,
        value: Any,
        confidence: float,
        conflict_group_id: str | None,
        agent_run_id: str | None,
        author_status: str = "pending",
        connection: Any | None = None,
    ) -> dict[str, Any]:
        identifier = new_id("bpint")
        now = utc_now()
        with (nullcontext(connection) if connection is not None else self.database.connect()) as active:
            if active.execute(
                "SELECT 1 FROM blueprint_nodes WHERE id=? AND project_id=?", (node_id, project_id)
            ).fetchone() is None:
                raise KeyError(f"Unknown blueprint node: {node_id}")
            if agent_run_id is not None and active.execute(
                "SELECT 1 FROM blueprint_agent_runs WHERE id=? AND project_id=?", (agent_run_id, project_id)
            ).fetchone() is None:
                raise KeyError(f"Unknown blueprint agent run: {agent_run_id}")
            active.execute(
                """
                INSERT INTO blueprint_interpretations(
                    id, project_id, node_id, dimension, value_json, confidence,
                    author_status, conflict_group_id, agent_run_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identifier,
                    project_id,
                    node_id,
                    dimension,
                    json_dumps(value),
                    float(confidence),
                    author_status,
                    conflict_group_id,
                    agent_run_id,
                    now,
                    now,
                ),
            )
            row = active.execute("SELECT * FROM blueprint_interpretations WHERE id=?", (identifier,)).fetchone()
        return _decode(row, "value")

    def list_interpretations_for_version(
        self, project_id: str, artifact_version_id: str
    ) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT i.* FROM blueprint_interpretations i
                JOIN blueprint_nodes n ON n.id=i.node_id
                WHERE i.project_id=? AND n.artifact_version_id=?
                ORDER BY i.created_at, i.rowid
                """,
                (project_id, artifact_version_id),
            ).fetchall()
        return [_decode(row, "value") for row in rows]

    def create_conflict(
        self,
        project_id: str,
        artifact_version_id: str,
        *,
        conflict_group_id: str,
        relation_type: str,
        interpretation_ids: list[str],
        status: str = "pending_author",
        connection: Any | None = None,
    ) -> dict[str, Any]:
        identifier = new_id("bpconf")
        now = utc_now()
        with (nullcontext(connection) if connection is not None else self.database.connect()) as active:
            if active.execute(
                """SELECT 1 FROM artifact_versions av JOIN artifacts a ON a.id=av.artifact_id
                   WHERE av.id=? AND a.project_id=?""", (artifact_version_id, project_id)
            ).fetchone() is None:
                raise KeyError(f"Unknown artifact version: {artifact_version_id}")
            for interpretation_id in interpretation_ids:
                if active.execute(
                    "SELECT 1 FROM blueprint_interpretations WHERE id=? AND project_id=?",
                    (interpretation_id, project_id),
                ).fetchone() is None:
                    raise KeyError(f"Unknown blueprint interpretation: {interpretation_id}")
            active.execute(
                """
                INSERT INTO blueprint_conflicts(
                    id, project_id, artifact_version_id, conflict_group_id,
                    relation_type, interpretation_ids_json, status,
                    resolution_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, '{}', ?, ?)
                """,
                (
                    identifier,
                    project_id,
                    artifact_version_id,
                    conflict_group_id,
                    relation_type,
                    json_dumps(interpretation_ids),
                    status,
                    now,
                    now,
                ),
            )
            row = active.execute("SELECT * FROM blueprint_conflicts WHERE id=?", (identifier,)).fetchone()
        return _decode(row, "interpretation_ids", "resolution")

    def create_edge(
        self,
        project_id: str,
        *,
        artifact_version_id: str,
        source_node_id: str,
        target_node_id: str,
        edge_type: str,
        attrs: dict[str, Any] | None = None,
        confidence: float = 1.0,
        job_id: str | None = None,
        connection: Any | None = None,
    ) -> dict[str, Any]:
        if edge_type not in {"contains", "causes", "reveals", "sets_up", "pays_off", "changes", "mirrors"}:
            raise ValueError(f"unsupported blueprint edge type: {edge_type}")
        identifier = new_id("bpedge")
        now = utc_now()
        with (nullcontext(connection) if connection is not None else self.database.connect()) as active:
            for node_id in (source_node_id, target_node_id):
                row = active.execute(
                    "SELECT artifact_version_id FROM blueprint_nodes WHERE id=? AND project_id=?",
                    (node_id, project_id),
                ).fetchone()
                if row is None or row["artifact_version_id"] != artifact_version_id:
                    raise KeyError(f"Unknown blueprint node: {node_id}")
            active.execute(
                """INSERT INTO blueprint_edges(
                       id, project_id, artifact_version_id, job_id, source_node_id, target_node_id,
                       edge_type, attrs_json, confidence, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (identifier, project_id, artifact_version_id, job_id, source_node_id, target_node_id,
                 edge_type, json_dumps(attrs or {}), float(confidence), now),
            )
            row = active.execute("SELECT * FROM blueprint_edges WHERE id=?", (identifier,)).fetchone()
        return _decode(row, "attrs")

    def list_edges_for_version(self, project_id: str, artifact_version_id: str) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM blueprint_edges WHERE project_id=? AND artifact_version_id=? ORDER BY created_at, id",
                (project_id, artifact_version_id),
            ).fetchall()
        return [_decode(row, "attrs") for row in rows]

    def list_conflicts_for_version(
        self, project_id: str, artifact_version_id: str
    ) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM blueprint_conflicts WHERE project_id=? AND artifact_version_id=? ORDER BY created_at, id",
                (project_id, artifact_version_id),
            ).fetchall()
        return [_decode(row, "interpretation_ids", "resolution") for row in rows]

    def create_target_setting_record(
        self,
        project_id: str,
        *,
        artifact_id: str,
        artifact_version_id: str,
        source_text: str,
        structured: dict[str, Any],
        status: str = "confirmed",
        connection: Any | None = None,
    ) -> dict[str, Any]:
        identifier = new_id("setting")
        now = utc_now()
        with (nullcontext(connection) if connection is not None else self.database.connect()) as active:
            self._require_project(active, project_id)
            artifact = active.execute(
                "SELECT current_version_id FROM artifacts WHERE id=? AND project_id=?",
                (artifact_id, project_id),
            ).fetchone()
            if artifact is None:
                raise KeyError(f"Unknown artifact: {artifact_id}")
            if active.execute(
                "SELECT 1 FROM artifact_versions WHERE id=? AND artifact_id=?",
                (artifact_version_id, artifact_id),
            ).fetchone() is None:
                raise KeyError(f"Unknown artifact version: {artifact_version_id}")
            active.execute(
                """
                INSERT INTO target_settings(
                    id, project_id, artifact_id, artifact_version_id, source_text,
                    structured_json, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (identifier, project_id, artifact_id, artifact_version_id, source_text, json_dumps(structured), status, now, now),
            )
            row = active.execute("SELECT * FROM target_settings WHERE id=?", (identifier,)).fetchone()
        return _decode(row, "structured")

    def get_target_setting(self, project_id: str, artifact_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT ts.* FROM target_settings ts
                JOIN artifacts a ON a.id=ts.artifact_id
                WHERE ts.project_id=? AND ts.artifact_id=? AND a.current_version_id=ts.artifact_version_id
                """,
                (project_id, artifact_id),
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown target setting: {artifact_id}")
        return _decode(row, "structured")

    def create_mapping(
        self,
        project_id: str,
        *,
        job_id: str,
        reference_version_id: str,
        target_version_id: str,
        reference_node_id: str | None,
        target_node_id: str | None,
        action: str,
        rationale: str,
        risk: dict[str, Any] | None = None,
        connection: Any | None = None,
    ) -> dict[str, Any]:
        identifier = new_id("bpmap")
        now = utc_now()
        with (nullcontext(connection) if connection is not None else self.database.connect()) as active:
            self._require_project(active, project_id)
            if active.execute(
                "SELECT 1 FROM blueprint_jobs WHERE id=? AND project_id=?", (job_id, project_id)
            ).fetchone() is None:
                raise KeyError(f"Unknown blueprint job: {job_id}")
            for version_id in (reference_version_id, target_version_id):
                if version_id is not None and active.execute(
                    """SELECT 1 FROM artifact_versions av JOIN artifacts a ON a.id=av.artifact_id
                       WHERE av.id=? AND a.project_id=?""", (version_id, project_id)
                ).fetchone() is None:
                    raise KeyError(f"Unknown artifact version: {version_id}")
            for node_id in (reference_node_id, target_node_id):
                if node_id is not None and active.execute(
                    "SELECT 1 FROM blueprint_nodes WHERE id=? AND project_id=?", (node_id, project_id)
                ).fetchone() is None:
                    raise KeyError(f"Unknown blueprint node: {node_id}")
            active.execute(
                """
                INSERT INTO blueprint_mappings(
                    id, project_id, job_id, reference_version_id, target_version_id,
                    reference_node_id, target_node_id, action, rationale, risk_json,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'proposed', ?, ?)
                """,
                (
                    identifier,
                    project_id,
                    job_id,
                    reference_version_id,
                    target_version_id,
                    reference_node_id,
                    target_node_id,
                    action,
                    rationale,
                    json_dumps(risk or {}),
                    now,
                    now,
                ),
            )
            row = active.execute(
                """
                SELECT m.*, rn.stable_key AS reference_stable_key,
                       tn.stable_key AS target_stable_key
                FROM blueprint_mappings m
                LEFT JOIN blueprint_nodes rn ON rn.id=m.reference_node_id
                LEFT JOIN blueprint_nodes tn ON tn.id=m.target_node_id
                WHERE m.id=?
                """,
                (identifier,),
            ).fetchone()
        return _decode(row, "risk")

    def list_mappings(self, project_id: str, job_id: str) -> list[dict[str, Any]]:
        self.get_job(project_id, job_id)
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT m.*, rn.stable_key AS reference_stable_key,
                       tn.stable_key AS target_stable_key
                FROM blueprint_mappings m
                LEFT JOIN blueprint_nodes rn ON rn.id=m.reference_node_id
                LEFT JOIN blueprint_nodes tn ON tn.id=m.target_node_id
                WHERE m.project_id=? AND m.job_id=? ORDER BY m.created_at, m.rowid
                """,
                (project_id, job_id),
            ).fetchall()
        return [_decode(row, "risk") for row in rows]

    def create_candidate(
        self,
        project_id: str,
        *,
        target_blueprint_version_id: str | None,
        unit_id: str | None,
        artifact_id: str | None,
        unit_plan: dict[str, Any],
        text: str,
        base_version_id: str | None,
        generation_metadata: dict[str, Any],
        status: str = "pending_review",
    ) -> dict[str, Any]:
        identifier = new_id("cand")
        now = utc_now()
        with self.database.connect() as connection:
            self._require_project(connection, project_id)
            for version_id in (target_blueprint_version_id, base_version_id):
                if version_id is not None and connection.execute(
                    """SELECT 1 FROM artifact_versions av JOIN artifacts a ON a.id=av.artifact_id
                       WHERE av.id=? AND a.project_id=?""", (version_id, project_id)
                ).fetchone() is None:
                    raise KeyError(f"Unknown artifact version: {version_id}")
            if unit_id is not None and connection.execute(
                "SELECT 1 FROM production_units WHERE id=? AND project_id=?", (unit_id, project_id)
            ).fetchone() is None:
                raise KeyError(f"Unknown production unit: {unit_id}")
            if artifact_id is not None and connection.execute(
                "SELECT 1 FROM artifacts WHERE id=? AND project_id=?", (artifact_id, project_id)
            ).fetchone() is None:
                raise KeyError(f"Unknown artifact: {artifact_id}")
            connection.execute(
                """
                INSERT INTO draft_candidates(
                    id, project_id, target_blueprint_version_id, unit_id, artifact_id,
                    unit_plan_json, candidate_text, base_version_id, status,
                    generation_metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identifier,
                    project_id,
                    target_blueprint_version_id,
                    unit_id,
                    artifact_id,
                    json_dumps(unit_plan),
                    str(text),
                    base_version_id,
                    status,
                    json_dumps(generation_metadata),
                    now,
                    now,
                ),
            )
            row = connection.execute("SELECT * FROM draft_candidates WHERE id=?", (identifier,)).fetchone()
        return _decode(row, "unit_plan", "generation_metadata", "exception")

    def get_candidate(self, project_id: str, candidate_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM draft_candidates WHERE id=? AND project_id=?",
                (candidate_id, project_id),
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown draft candidate: {candidate_id}")
        return _decode(row, "unit_plan", "generation_metadata", "exception")

    def update_candidate(
        self,
        project_id: str,
        candidate_id: str,
        *,
        status: str | None = None,
        rejection_reason: str | None = None,
        accepted_version_id: str | None = None,
        exception: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        current = self.get_candidate(project_id, candidate_id)
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE draft_candidates
                SET status=?, rejection_reason=?, accepted_version_id=?,
                    exception_json=?, updated_at=?
                WHERE id=? AND project_id=?
                """,
                (
                    status if status is not None else current["status"],
                    rejection_reason if rejection_reason is not None else current.get("rejection_reason"),
                    accepted_version_id if accepted_version_id is not None else current.get("accepted_version_id"),
                    json_dumps(exception if exception is not None else current.get("exception", {})),
                    now,
                    candidate_id,
                    project_id,
                ),
            )
        return self.get_candidate(project_id, candidate_id)

    def create_similarity_assessment(
        self,
        project_id: str,
        candidate_id: str,
        *,
        expression: dict[str, Any],
        structure: dict[str, Any],
        mechanism: dict[str, Any],
        gate_status: str,
        findings: list[dict[str, Any]],
    ) -> dict[str, Any]:
        self.get_candidate(project_id, candidate_id)
        identifier = new_id("sim")
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO similarity_assessments(
                    id, project_id, candidate_id, expression_json, structure_json,
                    mechanism_json, gate_status, findings_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(candidate_id) DO UPDATE SET
                    expression_json=excluded.expression_json,
                    structure_json=excluded.structure_json,
                    mechanism_json=excluded.mechanism_json,
                    gate_status=excluded.gate_status,
                    findings_json=excluded.findings_json,
                    created_at=excluded.created_at
                """,
                (
                    identifier,
                    project_id,
                    candidate_id,
                    json_dumps(expression),
                    json_dumps(structure),
                    json_dumps(mechanism),
                    gate_status,
                    json_dumps(findings),
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM similarity_assessments WHERE candidate_id=?", (candidate_id,)
            ).fetchone()
        return _decode(row, "expression", "structure", "mechanism", "findings")

    def get_similarity_assessment(
        self, project_id: str, candidate_id: str
    ) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM similarity_assessments WHERE project_id=? AND candidate_id=?",
                (project_id, candidate_id),
            ).fetchone()
        return None if row is None else _decode(row, "expression", "structure", "mechanism", "findings")
