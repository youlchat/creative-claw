from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from collections.abc import Iterator
from pathlib import Path


from .workflow_templates import install_builtin_templates


SCHEMA_VERSION = 5


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    root_path TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(project_id, path)
);

CREATE TABLE IF NOT EXISTS chunks (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    text TEXT NOT NULL,
    search_text TEXT NOT NULL,
    embedding BLOB,
    embedding_dim INTEGER,
    embedding_provider TEXT NOT NULL DEFAULT 'hash-v1',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    branch TEXT NOT NULL DEFAULT 'main',
    canon_status TEXT NOT NULL DEFAULT 'reference',
    episode INTEGER,
    scene INTEGER,
    story_time TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(document_id, ordinal)
);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    chunk_id UNINDEXED,
    project_id UNINDEXED,
    search_text,
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TABLE IF NOT EXISTS entities (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    aliases_json TEXT NOT NULL DEFAULT '[]',
    attrs_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(project_id, name, entity_type)
);

CREATE TABLE IF NOT EXISTS relations (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    source_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    predicate TEXT NOT NULL,
    target_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    evidence_chunk_id TEXT REFERENCES chunks(id) ON DELETE SET NULL,
    valid_from TEXT,
    valid_to TEXT,
    branch TEXT NOT NULL DEFAULT 'main',
    attrs_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS timeline_events (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    label TEXT NOT NULL,
    story_time TEXT,
    episode INTEGER,
    scene INTEGER,
    description TEXT NOT NULL,
    evidence_chunk_id TEXT REFERENCES chunks(id) ON DELETE SET NULL,
    branch TEXT NOT NULL DEFAULT 'main',
    attrs_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ohlc_points (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    character_name TEXT NOT NULL,
    dimension TEXT NOT NULL,
    period_type TEXT NOT NULL,
    period_id TEXT NOT NULL,
    parent_period_id TEXT,
    sort_key REAL NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    evidence_chunk_id TEXT REFERENCES chunks(id) ON DELETE SET NULL,
    timeline_event_id TEXT REFERENCES timeline_events(id) ON DELETE SET NULL,
    branch TEXT NOT NULL DEFAULT 'main',
    attrs_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(project_id, character_name, dimension, period_id, branch),
    CHECK(high >= open AND high >= close AND low <= open AND low <= close)
);

CREATE TABLE IF NOT EXISTS ledger_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    parent_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    goal TEXT NOT NULL,
    status TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    cursor INTEGER NOT NULL DEFAULT 0,
    checkpoint_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tool_runs (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    step_index INTEGER NOT NULL,
    tool_name TEXT NOT NULL,
    risk TEXT NOT NULL,
    approval_status TEXT NOT NULL,
    input_json TEXT NOT NULL,
    output_json TEXT,
    error_text TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS workflow_templates (
    id TEXT PRIMARY KEY,
    template_key TEXT NOT NULL,
    version INTEGER NOT NULL,
    media_type TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    definition_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(template_key, version)
);

CREATE TABLE IF NOT EXISTS project_workflows (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    template_id TEXT NOT NULL REFERENCES workflow_templates(id),
    media_type TEXT NOT NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(project_id)
);

CREATE TABLE IF NOT EXISTS workflow_stages (
    id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL REFERENCES project_workflows(id) ON DELETE CASCADE,
    template_stage_key TEXT NOT NULL,
    position INTEGER NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    entry_criteria_json TEXT NOT NULL DEFAULT '[]',
    completion_criteria_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'not_started',
    exception_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(workflow_id, template_stage_key),
    UNIQUE(workflow_id, position)
);

CREATE TABLE IF NOT EXISTS production_units (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    workflow_id TEXT REFERENCES project_workflows(id) ON DELETE SET NULL,
    parent_id TEXT REFERENCES production_units(id) ON DELETE CASCADE,
    unit_type TEXT NOT NULL,
    title TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0,
    branch TEXT NOT NULL DEFAULT 'main',
    source_timeline_event_id TEXT REFERENCES timeline_events(id) ON DELETE SET NULL,
    attrs_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    workflow_stage_id TEXT REFERENCES workflow_stages(id) ON DELETE SET NULL,
    production_unit_id TEXT REFERENCES production_units(id) ON DELETE SET NULL,
    artifact_type TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'empty',
    current_version_id TEXT,
    source_document_id TEXT REFERENCES documents(id) ON DELETE SET NULL,
    source_timeline_event_id TEXT REFERENCES timeline_events(id) ON DELETE SET NULL,
    branch TEXT NOT NULL DEFAULT 'main',
    attrs_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS artifact_versions (
    id TEXT PRIMARY KEY,
    artifact_id TEXT NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE,
    version_number INTEGER NOT NULL,
    parent_version_id TEXT REFERENCES artifact_versions(id) ON DELETE SET NULL,
    content TEXT NOT NULL,
    content_format TEXT NOT NULL DEFAULT 'text/plain',
    source_kind TEXT NOT NULL,
    change_summary TEXT NOT NULL,
    actor TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(artifact_id, version_number)
);

CREATE TABLE IF NOT EXISTS artifact_dependencies (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    upstream_artifact_id TEXT NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE,
    downstream_artifact_id TEXT NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE,
    dependency_type TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(project_id, upstream_artifact_id, downstream_artifact_id, dependency_type),
    CHECK(upstream_artifact_id <> downstream_artifact_id)
);

CREATE TABLE IF NOT EXISTS reviews (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    artifact_id TEXT NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE,
    review_type TEXT NOT NULL,
    input_version_id TEXT NOT NULL REFERENCES artifact_versions(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'valid',
    summary TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    stale_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS review_issues (
    id TEXT PRIMARY KEY,
    review_id TEXT NOT NULL REFERENCES reviews(id) ON DELETE CASCADE,
    severity TEXT NOT NULL,
    category TEXT NOT NULL,
    description TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS impact_records (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    change_type TEXT NOT NULL,
    source_artifact_id TEXT NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE,
    source_version_id TEXT NOT NULL REFERENCES artifact_versions(id) ON DELETE CASCADE,
    affected_artifact_id TEXT NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE,
    dependency_path_json TEXT NOT NULL,
    risk_level TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS blueprint_jobs (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    job_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    desired_state TEXT NOT NULL DEFAULT 'running',
    input_json TEXT NOT NULL DEFAULT '{}',
    input_hash TEXT,
    rights_basis TEXT,
    source_document_id TEXT REFERENCES documents(id) ON DELETE SET NULL,
    source_version_id TEXT REFERENCES artifact_versions(id) ON DELETE SET NULL,
    output_artifact_id TEXT REFERENCES artifacts(id) ON DELETE SET NULL,
    progress_json TEXT NOT NULL DEFAULT '{}',
    checkpoint_json TEXT NOT NULL DEFAULT '{}',
    error_json TEXT NOT NULL DEFAULT '{}',
    idempotency_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(project_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS blueprint_batches (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    job_id TEXT NOT NULL REFERENCES blueprint_jobs(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    start_offset INTEGER NOT NULL,
    end_offset INTEGER NOT NULL,
    overlap_start INTEGER NOT NULL DEFAULT 0,
    source_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    checkpoint_json TEXT NOT NULL DEFAULT '{}',
    idempotency_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(job_id, ordinal),
    UNIQUE(job_id, idempotency_key),
    CHECK(start_offset >= 0 AND end_offset > start_offset AND overlap_start >= 0)
);

CREATE TABLE IF NOT EXISTS blueprint_agent_runs (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    job_id TEXT NOT NULL REFERENCES blueprint_jobs(id) ON DELETE CASCADE,
    batch_id TEXT REFERENCES blueprint_batches(id) ON DELETE CASCADE,
    agent_name TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    model_json TEXT NOT NULL DEFAULT '{}',
    input_hash TEXT NOT NULL,
    output_hash TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    result_json TEXT NOT NULL DEFAULT '{}',
    warnings_json TEXT NOT NULL DEFAULT '[]',
    diagnostic_hash TEXT,
    error_category TEXT,
    idempotency_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(job_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS blueprint_nodes (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    artifact_version_id TEXT REFERENCES artifact_versions(id) ON DELETE CASCADE,
    job_id TEXT REFERENCES blueprint_jobs(id) ON DELETE CASCADE,
    parent_id TEXT REFERENCES blueprint_nodes(id) ON DELETE CASCADE,
    stable_key TEXT NOT NULL,
    node_type TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    source_locator_json TEXT NOT NULL DEFAULT '{}',
    dimensions_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'candidate',
    confidence REAL NOT NULL DEFAULT 1,
    agent_run_ids_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(artifact_version_id, stable_key),
    UNIQUE(job_id, stable_key)
);

CREATE TABLE IF NOT EXISTS blueprint_evidence (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    node_id TEXT NOT NULL REFERENCES blueprint_nodes(id) ON DELETE CASCADE,
    interpretation_id TEXT REFERENCES blueprint_interpretations(id) ON DELETE CASCADE,
    source_document_id TEXT REFERENCES documents(id) ON DELETE SET NULL,
    chunk_id TEXT REFERENCES chunks(id) ON DELETE SET NULL,
    start_offset INTEGER NOT NULL,
    end_offset INTEGER NOT NULL,
    source_length INTEGER NOT NULL,
    quote TEXT NOT NULL DEFAULT '',
    locator_json TEXT NOT NULL DEFAULT '{}',
    agent_run_id TEXT REFERENCES blueprint_agent_runs(id) ON DELETE SET NULL,
    confidence REAL NOT NULL,
    created_at TEXT NOT NULL,
    CHECK(start_offset >= 0 AND end_offset > start_offset AND end_offset <= source_length)
);

CREATE TABLE IF NOT EXISTS blueprint_interpretations (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    node_id TEXT NOT NULL REFERENCES blueprint_nodes(id) ON DELETE CASCADE,
    dimension TEXT NOT NULL,
    value_json TEXT NOT NULL,
    confidence REAL NOT NULL,
    author_status TEXT NOT NULL DEFAULT 'pending',
    conflict_group_id TEXT,
    agent_run_id TEXT REFERENCES blueprint_agent_runs(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS blueprint_conflicts (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    artifact_version_id TEXT REFERENCES artifact_versions(id) ON DELETE CASCADE,
    conflict_group_id TEXT NOT NULL,
    relation_type TEXT NOT NULL,
    interpretation_ids_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending_author',
    resolution_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS blueprint_edges (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    artifact_version_id TEXT REFERENCES artifact_versions(id) ON DELETE CASCADE,
    job_id TEXT REFERENCES blueprint_jobs(id) ON DELETE CASCADE,
    source_node_id TEXT NOT NULL REFERENCES blueprint_nodes(id) ON DELETE CASCADE,
    target_node_id TEXT NOT NULL REFERENCES blueprint_nodes(id) ON DELETE CASCADE,
    edge_type TEXT NOT NULL,
    attrs_json TEXT NOT NULL DEFAULT '{}',
    confidence REAL NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    UNIQUE(artifact_version_id, source_node_id, target_node_id, edge_type)
);

CREATE TABLE IF NOT EXISTS target_settings (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    artifact_id TEXT NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE,
    artifact_version_id TEXT NOT NULL REFERENCES artifact_versions(id) ON DELETE CASCADE,
    source_text TEXT NOT NULL,
    structured_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'confirmed',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(artifact_version_id)
);

CREATE TABLE IF NOT EXISTS blueprint_mappings (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    job_id TEXT REFERENCES blueprint_jobs(id) ON DELETE CASCADE,
    reference_version_id TEXT NOT NULL REFERENCES artifact_versions(id) ON DELETE CASCADE,
    target_version_id TEXT REFERENCES artifact_versions(id) ON DELETE CASCADE,
    reference_node_id TEXT REFERENCES blueprint_nodes(id) ON DELETE CASCADE,
    target_node_id TEXT REFERENCES blueprint_nodes(id) ON DELETE CASCADE,
    action TEXT NOT NULL,
    rationale TEXT NOT NULL DEFAULT '',
    risk_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'proposed',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS draft_candidates (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    target_blueprint_version_id TEXT REFERENCES artifact_versions(id) ON DELETE SET NULL,
    unit_id TEXT REFERENCES production_units(id) ON DELETE SET NULL,
    artifact_id TEXT REFERENCES artifacts(id) ON DELETE SET NULL,
    unit_plan_json TEXT NOT NULL DEFAULT '{}',
    candidate_text TEXT NOT NULL,
    base_version_id TEXT REFERENCES artifact_versions(id) ON DELETE SET NULL,
    status TEXT NOT NULL DEFAULT 'pending_review',
    generation_metadata_json TEXT NOT NULL DEFAULT '{}',
    exception_json TEXT NOT NULL DEFAULT '{}',
    rejection_reason TEXT,
    accepted_version_id TEXT REFERENCES artifact_versions(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS similarity_assessments (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    candidate_id TEXT NOT NULL REFERENCES draft_candidates(id) ON DELETE CASCADE,
    expression_json TEXT NOT NULL DEFAULT '{}',
    structure_json TEXT NOT NULL DEFAULT '{}',
    mechanism_json TEXT NOT NULL DEFAULT '{}',
    gate_status TEXT NOT NULL,
    findings_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    UNIQUE(candidate_id)
);

CREATE INDEX IF NOT EXISTS idx_chunks_project ON chunks(project_id);
CREATE INDEX IF NOT EXISTS idx_chunks_story ON chunks(project_id, branch, episode, scene);
CREATE INDEX IF NOT EXISTS idx_relations_source ON relations(project_id, source_id);
CREATE INDEX IF NOT EXISTS idx_relations_target ON relations(project_id, target_id);
CREATE INDEX IF NOT EXISTS idx_timeline_story ON timeline_events(project_id, branch, episode, scene);
CREATE INDEX IF NOT EXISTS idx_ohlc_series ON ohlc_points(project_id, character_name, dimension, branch, sort_key);
CREATE INDEX IF NOT EXISTS idx_ledger_project ON ledger_events(project_id, seq);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tool_run_step ON tool_runs(task_id, step_index);
CREATE INDEX IF NOT EXISTS idx_workflow_stages_order ON workflow_stages(workflow_id, position);
CREATE INDEX IF NOT EXISTS idx_production_units_project ON production_units(project_id, branch, parent_id, position);
CREATE UNIQUE INDEX IF NOT EXISTS idx_production_units_source_timeline
    ON production_units(source_timeline_event_id) WHERE source_timeline_event_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_artifacts_project ON artifacts(project_id, branch, artifact_type);
CREATE UNIQUE INDEX IF NOT EXISTS idx_artifacts_source_document
    ON artifacts(source_document_id) WHERE source_document_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_artifacts_source_timeline
    ON artifacts(source_timeline_event_id) WHERE source_timeline_event_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_artifact_versions_artifact ON artifact_versions(artifact_id, version_number);
CREATE INDEX IF NOT EXISTS idx_artifact_dependencies_upstream ON artifact_dependencies(project_id, upstream_artifact_id);
CREATE INDEX IF NOT EXISTS idx_artifact_dependencies_downstream ON artifact_dependencies(project_id, downstream_artifact_id);
CREATE INDEX IF NOT EXISTS idx_reviews_artifact ON reviews(project_id, artifact_id, status);
CREATE INDEX IF NOT EXISTS idx_impacts_project ON impact_records(project_id, status, created_at);
CREATE INDEX IF NOT EXISTS idx_blueprint_jobs_project ON blueprint_jobs(project_id, status, created_at);
CREATE INDEX IF NOT EXISTS idx_blueprint_batches_job ON blueprint_batches(project_id, job_id, ordinal);
CREATE INDEX IF NOT EXISTS idx_blueprint_runs_job ON blueprint_agent_runs(project_id, job_id, batch_id, agent_name);
CREATE INDEX IF NOT EXISTS idx_blueprint_nodes_version ON blueprint_nodes(project_id, artifact_version_id, node_type);
CREATE INDEX IF NOT EXISTS idx_blueprint_nodes_job ON blueprint_nodes(project_id, job_id, node_type);
CREATE INDEX IF NOT EXISTS idx_blueprint_evidence_node ON blueprint_evidence(project_id, node_id);
CREATE INDEX IF NOT EXISTS idx_blueprint_interpretations_node ON blueprint_interpretations(project_id, node_id, author_status);
CREATE INDEX IF NOT EXISTS idx_blueprint_conflicts_version ON blueprint_conflicts(project_id, artifact_version_id, status);
CREATE INDEX IF NOT EXISTS idx_blueprint_edges_version ON blueprint_edges(project_id, artifact_version_id, edge_type);
CREATE INDEX IF NOT EXISTS idx_target_settings_project ON target_settings(project_id, artifact_id);
CREATE INDEX IF NOT EXISTS idx_blueprint_mappings_project ON blueprint_mappings(project_id, reference_version_id, target_version_id);
CREATE INDEX IF NOT EXISTS idx_draft_candidates_project ON draft_candidates(project_id, status, created_at);
CREATE INDEX IF NOT EXISTS idx_similarity_candidate ON similarity_assessments(project_id, candidate_id);
"""


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as connection:
            current_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if current_version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"Database schema version {current_version} is newer than supported version {SCHEMA_VERSION}"
                )
            connection.executescript(SCHEMA)
            if current_version < 2:
                self._migrate_v2(connection)
            if current_version < 3:
                self._migrate_v3(connection)
            if current_version < 4:
                self._migrate_v4(connection)
            # Recovery is a normal-startup invariant, not a one-time schema migration.
            # It never dispatches work or invokes a model.
            self._migrate_v5(connection)
            install_builtin_templates(connection)
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_ohlc_timeline_event "
                "ON ohlc_points(project_id, branch, timeline_event_id)"
            )
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    @staticmethod
    def _migrate_v5(connection: sqlite3.Connection) -> None:
        """Make interrupted blueprint work explicitly resumable without running it."""

        connection.execute(
            "UPDATE blueprint_jobs SET status='resumable', desired_state='paused' "
            "WHERE status='running'"
        )
        connection.execute(
            "UPDATE blueprint_batches SET status='resumable' WHERE status='running'"
        )

    @staticmethod
    def _migrate_v2(connection: sqlite3.Connection) -> None:
        """Record the embedding source per chunk for mixed-provider indexes.

        Version 1 stored the provider only in document JSON. The migration is
        deliberately idempotent so it also upgrades unversioned MVP databases.
        """

        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(chunks)").fetchall()
        }
        if "embedding_provider" not in columns:
            connection.execute(
                "ALTER TABLE chunks ADD COLUMN embedding_provider TEXT NOT NULL DEFAULT 'hash-v1'"
            )
        rows = connection.execute(
            "SELECT id, metadata_json FROM documents"
        ).fetchall()
        for row in rows:
            provider = "hash-v1"
            try:
                import json

                metadata = json.loads(row["metadata_json"] or "{}")
                provider = str(metadata.get("embedding_provider") or provider)
            except (TypeError, ValueError):
                pass
            connection.execute(
                "UPDATE chunks SET embedding_provider=? WHERE document_id=?",
                (provider, row["id"]),
            )

    @staticmethod
    def _migrate_v3(connection: sqlite3.Connection) -> None:
        """Link scene-level character state directly to its timeline scene.

        Older databases encoded this link only in strings such as ``E18-S07``.
        Backfill only unambiguous matches; aggregate rows intentionally remain
        unlinked because they summarize multiple scenes.
        """

        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(ohlc_points)").fetchall()
        }
        if "timeline_event_id" not in columns:
            connection.execute("ALTER TABLE ohlc_points ADD COLUMN timeline_event_id TEXT")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_ohlc_timeline_event "
            "ON ohlc_points(project_id, branch, timeline_event_id)"
        )
        points = connection.execute(
            """
            SELECT id, project_id, branch, period_id
            FROM ohlc_points
            WHERE timeline_event_id IS NULL AND period_type='scene'
            """
        ).fetchall()
        for point in points:
            matches = connection.execute(
                """
                SELECT id
                FROM timeline_events
                WHERE project_id=? AND branch=?
                  AND ('E' || episode || '-S' || printf('%02d', scene))=?
                """,
                (point["project_id"], point["branch"], point["period_id"]),
            ).fetchall()
            if len(matches) == 1:
                connection.execute(
                    "UPDATE ohlc_points SET timeline_event_id=? WHERE id=?",
                    (matches[0]["id"], point["id"]),
                )

    @staticmethod
    def _migrate_v4(connection: sqlite3.Connection) -> None:
        """Map legacy sources and scenes into the production model.

        Stable IDs plus partial unique indexes make the backfill safe to run
        repeatedly. Existing ledger rows are deliberately left untouched so
        their immutable hash chain remains byte-for-byte valid.
        """

        install_builtin_templates(connection)
        documents = connection.execute("SELECT * FROM documents ORDER BY created_at, id").fetchall()
        for document in documents:
            artifact_id = f"legacy_source_{document['id']}"
            version_id = f"legacy_version_{document['id']}"
            chunk_rows = connection.execute(
                "SELECT text, branch FROM chunks WHERE document_id=? ORDER BY ordinal",
                (document["id"],),
            ).fetchall()
            content = "\n\n".join(str(row["text"]) for row in chunk_rows)
            branch = str(chunk_rows[0]["branch"]) if chunk_rows else "main"
            attrs_json = json.dumps(
                {"migrated_from": "documents", "legacy_kind": document["kind"]},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO artifacts(
                    id, project_id, artifact_type, title, status,
                    source_document_id, branch, attrs_json, created_at, updated_at
                ) VALUES (?, ?, 'source', ?, 'approved', ?, ?, ?, ?, ?)
                """,
                (
                    artifact_id,
                    document["project_id"],
                    document["title"],
                    document["id"],
                    branch,
                    attrs_json,
                    document["created_at"],
                    document["updated_at"],
                ),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO artifact_versions(
                    id, artifact_id, version_number, content, content_format,
                    source_kind, change_summary, actor, metadata_json, created_at
                ) VALUES (?, ?, 1, ?, 'text/plain', 'migration', ?, 'migration', ?, ?)
                """,
                (
                    version_id,
                    artifact_id,
                    content,
                    "Legacy document migration",
                    document["metadata_json"] or "{}",
                    document["created_at"],
                ),
            )
            connection.execute(
                "UPDATE artifacts SET current_version_id=? WHERE id=? AND current_version_id IS NULL",
                (version_id, artifact_id),
            )

        timeline_rows = connection.execute(
            "SELECT * FROM timeline_events ORDER BY created_at, id"
        ).fetchall()
        for event in timeline_rows:
            unit_id = f"legacy_unit_{event['id']}"
            artifact_id = f"legacy_scene_{event['id']}"
            version_id = f"legacy_scene_version_{event['id']}"
            attrs = json.loads(event["attrs_json"] or "{}")
            attrs["migrated_from"] = "timeline_events"
            attrs_json = json.dumps(
                attrs, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO production_units(
                    id, project_id, unit_type, title, position, branch,
                    source_timeline_event_id, attrs_json, created_at, updated_at
                ) VALUES (?, ?, 'scene', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    unit_id,
                    event["project_id"],
                    event["label"],
                    int(event["scene"] or 0),
                    event["branch"],
                    event["id"],
                    attrs_json,
                    event["created_at"],
                    event["created_at"],
                ),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO artifacts(
                    id, project_id, production_unit_id, artifact_type, title,
                    status, source_timeline_event_id, branch, attrs_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, 'manuscript', ?, 'draft', ?, ?, ?, ?, ?)
                """,
                (
                    artifact_id,
                    event["project_id"],
                    unit_id,
                    event["label"],
                    event["id"],
                    event["branch"],
                    attrs_json,
                    event["created_at"],
                    event["created_at"],
                ),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO artifact_versions(
                    id, artifact_id, version_number, content, content_format,
                    source_kind, change_summary, actor, metadata_json, created_at
                ) VALUES (?, ?, 1, ?, 'text/plain', 'migration', ?, 'migration', '{}', ?)
                """,
                (
                    version_id,
                    artifact_id,
                    event["description"],
                    "Legacy timeline scene migration",
                    event["created_at"],
                ),
            )
            connection.execute(
                "UPDATE artifacts SET current_version_id=? WHERE id=? AND current_version_id IS NULL",
                (version_id, artifact_id),
            )

    def schema_version(self) -> int:
        with self.connect() as connection:
            return int(connection.execute("PRAGMA user_version").fetchone()[0])

    def vacuum(self) -> None:
        with self.connect() as connection:
            connection.execute("VACUUM")
