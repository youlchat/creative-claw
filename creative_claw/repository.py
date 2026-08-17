from __future__ import annotations

from pathlib import Path
from typing import Any

from .db import Database
from .ledger import Ledger
from .util import json_dumps, json_loads, new_id, utc_now


class Repository:
    def __init__(self, database: Database):
        self.database = database
        self.ledger = Ledger(database)

    def create_project(self, name: str, root_path: str | Path, project_id: str | None = None) -> dict[str, Any]:
        identifier = project_id or new_id("prj")
        root = str(Path(root_path).resolve())
        created_at = utc_now()
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO projects(id, name, root_path, created_at) VALUES (?, ?, ?, ?)",
                (identifier, name, root, created_at),
            )
        self.ledger.append(identifier, "project.created", {"name": name, "root_path": root})
        return {"id": identifier, "name": name, "root_path": root, "created_at": created_at}

    def get_project(self, project_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            row = connection.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if not row:
            raise KeyError(f"Unknown project: {project_id}")
        return dict(row)

    def list_projects(self) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute("SELECT * FROM projects ORDER BY created_at").fetchall()
        return [dict(row) for row in rows]

    def update_project(self, project_id: str, *, name: str) -> dict[str, Any]:
        clean_name = str(name or "").strip()
        if not clean_name:
            raise ValueError("Project name is required")
        self.get_project(project_id)
        with self.database.connect() as connection:
            connection.execute("UPDATE projects SET name=? WHERE id=?", (clean_name, project_id))
        self.ledger.append(project_id, "project.updated", {"name": clean_name}, "canvas")
        return self.get_project(project_id)

    def list_documents(self, project_id: str) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT d.*, COUNT(c.id) AS chunk_count
                FROM documents d LEFT JOIN chunks c ON c.document_id=d.id
                WHERE d.project_id=? GROUP BY d.id ORDER BY d.updated_at DESC
                """,
                (project_id,),
            ).fetchall()
        return [
            {**dict(row), "metadata": json_loads(row["metadata_json"])}
            for row in rows
        ]

    def knowledge_stats(self, project_id: str) -> dict[str, Any]:
        """Return explainable storage/index coverage, grouped by meaningful dimensions."""

        self.get_project(project_id)
        with self.database.connect() as connection:
            totals = connection.execute(
                """
                SELECT COUNT(DISTINCT d.id) AS documents,
                       COUNT(c.id) AS chunks,
                       COALESCE(SUM(LENGTH(c.text)), 0) AS indexed_characters,
                       SUM(CASE WHEN c.embedding IS NOT NULL THEN 1 ELSE 0 END) AS embedded_chunks
                FROM documents d LEFT JOIN chunks c ON c.document_id=d.id
                WHERE d.project_id=?
                """,
                (project_id,),
            ).fetchone()

            def grouped(sql: str) -> list[dict[str, Any]]:
                return [dict(row) for row in connection.execute(sql, (project_id,)).fetchall()]

            document_kinds = grouped(
                "SELECT kind AS name, COUNT(*) AS count FROM documents WHERE project_id=? GROUP BY kind ORDER BY count DESC"
            )
            embedding_providers = grouped(
                "SELECT embedding_provider AS name, embedding_dim AS dimension, COUNT(*) AS count "
                "FROM chunks WHERE project_id=? GROUP BY embedding_provider, embedding_dim ORDER BY count DESC"
            )
            branches = grouped(
                "SELECT branch AS name, COUNT(*) AS count FROM chunks WHERE project_id=? GROUP BY branch ORDER BY count DESC"
            )
            canon_statuses = grouped(
                "SELECT canon_status AS name, COUNT(*) AS count FROM chunks WHERE project_id=? GROUP BY canon_status ORDER BY count DESC"
            )
            structured: dict[str, int] = {}
            for table, key in (
                ("entities", "entities"),
                ("relations", "relations"),
                ("timeline_events", "timeline_events"),
                ("ohlc_points", "ohlc_points"),
                ("ledger_events", "ledger_events"),
                ("tasks", "agent_tasks"),
                ("tool_runs", "tool_runs"),
                ("project_workflows", "project_workflows"),
                ("production_units", "production_units"),
                ("artifacts", "artifacts"),
                ("artifact_dependencies", "artifact_dependencies"),
                ("reviews", "reviews"),
                ("impact_records", "impact_records"),
                ("blueprint_jobs", "blueprint_jobs"),
                ("blueprint_batches", "blueprint_batches"),
                ("blueprint_agent_runs", "blueprint_agent_runs"),
                ("blueprint_nodes", "blueprint_nodes"),
                ("blueprint_evidence", "blueprint_evidence"),
                ("blueprint_interpretations", "blueprint_interpretations"),
                ("blueprint_conflicts", "blueprint_conflicts"),
                ("blueprint_edges", "blueprint_edges"),
                ("target_settings", "target_settings"),
                ("blueprint_mappings", "blueprint_mappings"),
                ("draft_candidates", "draft_candidates"),
                ("similarity_assessments", "similarity_assessments"),
            ):
                structured[key] = int(
                    connection.execute(
                        f"SELECT COUNT(*) AS n FROM {table} WHERE project_id=?", (project_id,)
                    ).fetchone()["n"]
                )
            structured["workflow_stages"] = int(
                connection.execute(
                    """
                    SELECT COUNT(*) AS n FROM workflow_stages ws
                    JOIN project_workflows pw ON pw.id=ws.workflow_id
                    WHERE pw.project_id=?
                    """,
                    (project_id,),
                ).fetchone()["n"]
            )
            structured["artifact_versions"] = int(
                connection.execute(
                    """
                    SELECT COUNT(*) AS n FROM artifact_versions av
                    JOIN artifacts a ON a.id=av.artifact_id
                    WHERE a.project_id=?
                    """,
                    (project_id,),
                ).fetchone()["n"]
            )
        return {
            "project_id": project_id,
            "schema_version": self.database.schema_version(),
            "documents": int(totals["documents"] or 0),
            "chunks": int(totals["chunks"] or 0),
            "indexed_characters": int(totals["indexed_characters"] or 0),
            "embedded_chunks": int(totals["embedded_chunks"] or 0),
            "document_kinds": document_kinds,
            "embedding_providers": embedding_providers,
            "branches": branches,
            "canon_statuses": canon_statuses,
            "structured": structured,
            "ledger": self.ledger.verify(project_id),
        }

    def canvas_snapshot(self, project_id: str, *, branch: str = "main") -> dict[str, Any]:
        """Return the structured project state needed by the narrative canvas."""

        project = self.get_project(project_id)
        with self.database.connect() as connection:
            relation_rows = connection.execute(
                """
                SELECT r.*, s.name AS source_name, s.entity_type AS source_type,
                       t.name AS target_name, t.entity_type AS target_type
                FROM relations r
                JOIN entities s ON s.id=r.source_id
                JOIN entities t ON t.id=r.target_id
                WHERE r.project_id=? AND r.branch=? ORDER BY r.created_at
                """,
                (project_id, branch),
            ).fetchall()
            timeline_rows = connection.execute(
                """
                SELECT * FROM timeline_events
                WHERE project_id=? AND branch=?
                ORDER BY COALESCE(episode, 999999), COALESCE(scene, 999999), created_at
                """,
                (project_id, branch),
            ).fetchall()
            ohlc_rows = connection.execute(
                """
                SELECT * FROM ohlc_points
                WHERE project_id=? AND branch=? ORDER BY character_name, dimension, sort_key
                """,
                (project_id, branch),
            ).fetchall()
            production_unit_rows = connection.execute(
                "SELECT * FROM production_units WHERE project_id=? AND branch=? ORDER BY position, created_at",
                (project_id, branch),
            ).fetchall()
            artifact_rows = connection.execute(
                "SELECT * FROM artifacts WHERE project_id=? AND branch=? ORDER BY created_at",
                (project_id, branch),
            ).fetchall()
        return {
            "project": project,
            "branch": branch,
            "stats": self.knowledge_stats(project_id),
            "documents": self.list_documents(project_id),
            "entities": self.list_entities(project_id),
            "relations": [
                {**dict(row), "attrs": json_loads(row["attrs_json"])} for row in relation_rows
            ],
            "timeline": [
                {**dict(row), "attrs": json_loads(row["attrs_json"])} for row in timeline_rows
            ],
            "ohlc": [
                {**dict(row), "attrs": json_loads(row["attrs_json"])} for row in ohlc_rows
            ],
            "production_units": [
                {**dict(row), "attrs": json_loads(row["attrs_json"])}
                for row in production_unit_rows
            ],
            "artifacts": [
                {**dict(row), "attrs": json_loads(row["attrs_json"])}
                for row in artifact_rows
            ],
        }

    def upsert_entity(
        self,
        project_id: str,
        name: str,
        entity_type: str,
        *,
        aliases: list[str] | None = None,
        attrs: dict[str, Any] | None = None,
        actor: str = "user",
    ) -> dict[str, Any]:
        now = utc_now()
        aliases_json = json_dumps(aliases or [])
        attrs_json = json_dumps(attrs or {})
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT id FROM entities WHERE project_id=? AND name=? AND entity_type=?",
                (project_id, name, entity_type),
            ).fetchone()
            identifier = row["id"] if row else new_id("ent")
            if row:
                connection.execute(
                    "UPDATE entities SET aliases_json=?, attrs_json=?, updated_at=? WHERE id=?",
                    (aliases_json, attrs_json, now, identifier),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO entities(id, project_id, name, entity_type, aliases_json, attrs_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (identifier, project_id, name, entity_type, aliases_json, attrs_json, now, now),
                )
        self.ledger.append(
            project_id,
            "entity.upserted",
            {"entity_id": identifier, "name": name, "entity_type": entity_type, "aliases": aliases or [], "attrs": attrs or {}},
            actor,
        )
        return {"id": identifier, "name": name, "entity_type": entity_type, "aliases": aliases or [], "attrs": attrs or {}}

    def list_entities(self, project_id: str) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute("SELECT * FROM entities WHERE project_id=? ORDER BY entity_type, name", (project_id,)).fetchall()
        return [
            {**dict(row), "aliases": json_loads(row["aliases_json"], []), "attrs": json_loads(row["attrs_json"])}
            for row in rows
        ]

    def add_relation(
        self,
        project_id: str,
        source_id: str,
        predicate: str,
        target_id: str,
        *,
        evidence_chunk_id: str | None = None,
        valid_from: str | None = None,
        valid_to: str | None = None,
        branch: str = "main",
        attrs: dict[str, Any] | None = None,
        actor: str = "user",
    ) -> dict[str, Any]:
        identifier = new_id("rel")
        created_at = utc_now()
        with self.database.connect() as connection:
            entity_count = connection.execute(
                "SELECT COUNT(*) AS n FROM entities WHERE project_id=? AND id IN (?, ?)",
                (project_id, source_id, target_id),
            ).fetchone()["n"]
            if entity_count != (1 if source_id == target_id else 2):
                raise ValueError("Relation endpoints must exist in the same project")
            connection.execute(
                """
                INSERT INTO relations(id, project_id, source_id, predicate, target_id, evidence_chunk_id,
                                      valid_from, valid_to, branch, attrs_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identifier,
                    project_id,
                    source_id,
                    predicate,
                    target_id,
                    evidence_chunk_id,
                    valid_from,
                    valid_to,
                    branch,
                    json_dumps(attrs or {}),
                    created_at,
                ),
            )
        payload = {
            "id": identifier,
            "source_id": source_id,
            "predicate": predicate,
            "target_id": target_id,
            "evidence_chunk_id": evidence_chunk_id,
            "valid_from": valid_from,
            "valid_to": valid_to,
            "branch": branch,
            "attrs": attrs or {},
        }
        self.ledger.append(project_id, "relation.added", payload, actor)
        return payload

    def graph_context(self, project_id: str, query: str, branch: str = "main", limit: int = 30) -> dict[str, Any]:
        lowered = query.lower()
        entities = self.list_entities(project_id)
        matched = [
            entity
            for entity in entities
            if entity["name"].lower() in lowered or any(alias.lower() in lowered for alias in entity["aliases"])
        ]
        if not matched:
            return {"entities": [], "relations": [], "evidence_chunk_ids": []}
        ids = [entity["id"] for entity in matched]
        placeholders = ",".join("?" for _ in ids)
        params: list[Any] = [project_id, branch, *ids, *ids, limit]
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT r.*, s.name AS source_name, s.entity_type AS source_type,
                       t.name AS target_name, t.entity_type AS target_type
                FROM relations r
                JOIN entities s ON s.id=r.source_id
                JOIN entities t ON t.id=r.target_id
                WHERE r.project_id=? AND r.branch=?
                  AND (r.source_id IN ({placeholders}) OR r.target_id IN ({placeholders}))
                LIMIT ?
                """,
                params,
            ).fetchall()
        relations = [{**dict(row), "attrs": json_loads(row["attrs_json"])} for row in rows]
        evidence = [row["evidence_chunk_id"] for row in rows if row["evidence_chunk_id"]]
        return {"entities": matched, "relations": relations, "evidence_chunk_ids": evidence}

    def add_timeline_event(
        self,
        project_id: str,
        label: str,
        description: str,
        *,
        story_time: str | None = None,
        episode: int | None = None,
        scene: int | None = None,
        evidence_chunk_id: str | None = None,
        branch: str = "main",
        attrs: dict[str, Any] | None = None,
        actor: str = "user",
    ) -> dict[str, Any]:
        identifier = new_id("time")
        created_at = utc_now()
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO timeline_events(id, project_id, label, story_time, episode, scene, description,
                                            evidence_chunk_id, branch, attrs_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identifier,
                    project_id,
                    label,
                    story_time,
                    episode,
                    scene,
                    description,
                    evidence_chunk_id,
                    branch,
                    json_dumps(attrs or {}),
                    created_at,
                ),
            )
        payload = {
            "id": identifier,
            "label": label,
            "description": description,
            "story_time": story_time,
            "episode": episode,
            "scene": scene,
            "evidence_chunk_id": evidence_chunk_id,
            "branch": branch,
            "attrs": attrs or {},
        }
        self.ledger.append(project_id, "timeline.added", payload, actor)
        return payload

    def update_timeline_event(
        self,
        project_id: str,
        event_id: str,
        description: str,
        *,
        label: str | None = None,
        story_time: str | None = None,
        patches: list[dict[str, Any]] | None = None,
        actor: str = "user",
    ) -> dict[str, Any]:
        clean_description = str(description)
        if not clean_description.strip():
            raise ValueError("timeline description cannot be empty")
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM timeline_events WHERE id=? AND project_id=?",
                (event_id, project_id),
            ).fetchone()
            if row is None:
                raise KeyError(event_id)
            before = {**dict(row), "attrs": json_loads(row["attrs_json"])}
            next_label = str(label).strip() if label is not None else row["label"]
            if not next_label:
                raise ValueError("timeline label cannot be empty")
            next_story_time = story_time if story_time is not None else row["story_time"]
            connection.execute(
                "UPDATE timeline_events SET label=?, description=?, story_time=? WHERE id=? AND project_id=?",
                (next_label, clean_description, next_story_time, event_id, project_id),
            )
        after = {
            "id": event_id,
            "project_id": project_id,
            "label": next_label,
            "description": clean_description,
            "story_time": next_story_time,
            "episode": row["episode"],
            "scene": row["scene"],
            "evidence_chunk_id": row["evidence_chunk_id"],
            "branch": row["branch"],
            "attrs": before["attrs"],
            "created_at": row["created_at"],
        }
        before.pop("attrs_json", None)
        self.ledger.append(
            project_id,
            "timeline.updated",
            {"id": event_id, "patches": patches or [], "before": before, "after": after},
            actor,
        )
        return after

    def get_timeline_event(
        self,
        project_id: str,
        event_id: str,
        *,
        branch: str = "main",
    ) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM timeline_events WHERE id=? AND project_id=? AND branch=?",
                (event_id, project_id, branch),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["attrs"] = json_loads(result.pop("attrs_json"))
        return result

    def timeline_context(
        self,
        project_id: str,
        *,
        event_id: str | None = None,
        episode: int | None = None,
        scene: int | None = None,
        branch: str = "main",
        radius: int = 1,
        limit: int = 20,
    ) -> dict[str, Any]:
        current = self.get_timeline_event(project_id, event_id, branch=branch) if event_id else None
        if current is None and episode is not None:
            where = ["project_id=?", "branch=?", "episode=?"]
            params: list[Any] = [project_id, branch, episode]
            if scene is not None:
                where.append("scene=?")
                params.append(scene)
            with self.database.connect() as connection:
                row = connection.execute(
                    f"SELECT * FROM timeline_events WHERE {' AND '.join(where)} "
                    "ORDER BY scene, created_at, id LIMIT 1",
                    params,
                ).fetchone()
            if row is not None:
                current = dict(row)
                current["attrs"] = json_loads(current.pop("attrs_json"))
        if current is None:
            return {"current": None, "events": []}

        current_episode = current.get("episode")
        current_scene = current.get("scene")
        if current_episode is None or current_scene is None:
            return {"current": current, "events": [current]}
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM timeline_events
                WHERE project_id=? AND branch=? AND episode=? AND scene BETWEEN ? AND ?
                ORDER BY episode, scene, created_at, id LIMIT ?
                """,
                (
                    project_id,
                    branch,
                    current_episode,
                    int(current_scene) - radius,
                    int(current_scene) + radius,
                    limit,
                ),
            ).fetchall()
        events = []
        for row in rows:
            item = dict(row)
            item["attrs"] = json_loads(item.pop("attrs_json"))
            events.append(item)
        return {"current": current, "events": events}

    def ohlc_for_timeline_events(
        self,
        project_id: str,
        event_ids: list[str],
        *,
        character_name: str | None = None,
        dimension: str | None = None,
        branch: str = "main",
    ) -> list[dict[str, Any]]:
        clean_ids = [str(event_id) for event_id in event_ids if event_id]
        if not clean_ids:
            return []
        where = [
            "project_id=?",
            "branch=?",
            "period_type='scene'",
            "timeline_event_id IN (" + ",".join("?" for _ in clean_ids) + ")",
        ]
        params: list[Any] = [project_id, branch, *clean_ids]
        if character_name:
            where.append("character_name=?")
            params.append(character_name)
        if dimension:
            where.append("dimension=?")
            params.append(dimension)
        with self.database.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM ohlc_points WHERE {' AND '.join(where)} "
                "ORDER BY sort_key, character_name, dimension",
                params,
            ).fetchall()
        results = []
        for row in rows:
            item = dict(row)
            item["attrs"] = json_loads(item.pop("attrs_json"))
            results.append(item)
        return results

    def nearby_timeline(
        self,
        project_id: str,
        *,
        episode: int | None = None,
        scene: int | None = None,
        branch: str = "main",
        radius: int = 1,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        where = ["project_id=?", "branch=?"]
        params: list[Any] = [project_id, branch]
        if episode is not None:
            where.append("episode BETWEEN ? AND ?")
            params.extend([episode - radius, episode + radius])
        params.append(limit)
        with self.database.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM timeline_events WHERE {' AND '.join(where)} ORDER BY episode, scene LIMIT ?",
                params,
            ).fetchall()
        return [{**dict(row), "attrs": json_loads(row["attrs_json"])} for row in rows]

    def upsert_ohlc(
        self,
        project_id: str,
        character_name: str,
        dimension: str,
        period_type: str,
        period_id: str,
        sort_key: float,
        open_value: float,
        high: float,
        low: float,
        close: float,
        *,
        parent_period_id: str | None = None,
        evidence_chunk_id: str | None = None,
        timeline_event_id: str | None = None,
        branch: str = "main",
        attrs: dict[str, Any] | None = None,
        actor: str = "user",
    ) -> dict[str, Any]:
        values = list(map(float, (open_value, high, low, close)))
        open_value, high, low, close = values
        if high < max(open_value, close) or low > min(open_value, close):
            raise ValueError("Invalid OHLC: high/low must contain open and close")
        if timeline_event_id and period_type != "scene":
            raise ValueError("Only scene-level OHLC rows can link to a timeline scene")
        now = utc_now()
        with self.database.connect() as connection:
            if timeline_event_id:
                scene_row = connection.execute(
                    """
                    SELECT id FROM timeline_events
                    WHERE id=? AND project_id=? AND branch=?
                    """,
                    (timeline_event_id, project_id, branch),
                ).fetchone()
                if scene_row is None:
                    raise ValueError("Linked scene must exist in the same project and branch")
            row = connection.execute(
                """
                SELECT id FROM ohlc_points
                WHERE project_id=? AND character_name=? AND dimension=? AND period_id=? AND branch=?
                """,
                (project_id, character_name, dimension, period_id, branch),
            ).fetchone()
            identifier = row["id"] if row else new_id("ohlc")
            if row:
                connection.execute(
                    """
                    UPDATE ohlc_points SET period_type=?, parent_period_id=?, sort_key=?, open=?, high=?, low=?, close=?,
                                           evidence_chunk_id=?, timeline_event_id=?, attrs_json=?, updated_at=? WHERE id=?
                    """,
                    (
                        period_type,
                        parent_period_id,
                        sort_key,
                        open_value,
                        high,
                        low,
                        close,
                        evidence_chunk_id,
                        timeline_event_id,
                        json_dumps(attrs or {}),
                        now,
                        identifier,
                    ),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO ohlc_points(id, project_id, character_name, dimension, period_type, period_id,
                                            parent_period_id, sort_key, open, high, low, close, evidence_chunk_id,
                                            timeline_event_id, branch, attrs_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        identifier,
                        project_id,
                        character_name,
                        dimension,
                        period_type,
                        period_id,
                        parent_period_id,
                        sort_key,
                        open_value,
                        high,
                        low,
                        close,
                        evidence_chunk_id,
                        timeline_event_id,
                        branch,
                        json_dumps(attrs or {}),
                        now,
                        now,
                    ),
                )
        payload = {
            "id": identifier,
            "character_name": character_name,
            "dimension": dimension,
            "period_type": period_type,
            "period_id": period_id,
            "parent_period_id": parent_period_id,
            "sort_key": sort_key,
            "open": open_value,
            "high": high,
            "low": low,
            "close": close,
            "evidence_chunk_id": evidence_chunk_id,
            "timeline_event_id": timeline_event_id,
            "branch": branch,
            "attrs": attrs or {},
        }
        self.ledger.append(project_id, "ohlc.upserted", payload, actor)
        if parent_period_id:
            aggregate = self.aggregate_ohlc(
                project_id,
                character_name,
                dimension,
                parent_period_id,
                branch=branch,
            )
            with self.database.connect() as connection:
                parent_row = connection.execute(
                    """
                    SELECT period_type, parent_period_id, sort_key, attrs_json
                    FROM ohlc_points
                    WHERE project_id=? AND character_name=? AND dimension=? AND period_id=? AND branch=?
                    """,
                    (project_id, character_name, dimension, parent_period_id, branch),
                ).fetchone()
            parent_sort_key = float(parent_row["sort_key"]) if parent_row else float(sort_key // 1)
            parent_type = str(parent_row["period_type"]) if parent_row else "episode"
            parent_parent = parent_row["parent_period_id"] if parent_row else None
            parent_attrs = json_loads(parent_row["attrs_json"]) if parent_row else {}
            parent_attrs.update({"aggregated": True, "child_period_ids": aggregate["child_period_ids"]})
            payload["parent_aggregate"] = self.upsert_ohlc(
                project_id,
                character_name,
                dimension,
                parent_type,
                parent_period_id,
                parent_sort_key,
                aggregate["open"],
                aggregate["high"],
                aggregate["low"],
                aggregate["close"],
                parent_period_id=parent_parent,
                timeline_event_id=None,
                branch=branch,
                attrs=parent_attrs,
                actor="aggregate",
            )
        return payload

    def ohlc_series(
        self,
        project_id: str,
        character_name: str,
        dimension: str,
        *,
        branch: str = "main",
        period_type: str | None = None,
    ) -> list[dict[str, Any]]:
        where = ["project_id=?", "character_name=?", "dimension=?", "branch=?"]
        params: list[Any] = [project_id, character_name, dimension, branch]
        if period_type:
            where.append("period_type=?")
            params.append(period_type)
        with self.database.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM ohlc_points WHERE {' AND '.join(where)} ORDER BY sort_key",
                params,
            ).fetchall()
        return [{**dict(row), "attrs": json_loads(row["attrs_json"])} for row in rows]

    def aggregate_ohlc(
        self,
        project_id: str,
        character_name: str,
        dimension: str,
        parent_period_id: str,
        *,
        branch: str = "main",
    ) -> dict[str, Any]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM ohlc_points
                WHERE project_id=? AND character_name=? AND dimension=? AND parent_period_id=? AND branch=?
                ORDER BY sort_key
                """,
                (project_id, character_name, dimension, parent_period_id, branch),
            ).fetchall()
        if not rows:
            raise KeyError(f"No child OHLC rows for {parent_period_id}")
        return {
            "period_id": parent_period_id,
            "character_name": character_name,
            "dimension": dimension,
            "open": rows[0]["open"],
            "high": max(row["high"] for row in rows),
            "low": min(row["low"] for row in rows),
            "close": rows[-1]["close"],
            "child_period_ids": [row["period_id"] for row in rows],
            "branch": branch,
        }
