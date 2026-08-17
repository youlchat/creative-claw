from __future__ import annotations

from collections import defaultdict
from typing import Any

from .context import ContextScope
from .db import Database
from .evidence import build_evidence_refs
from .embeddings import (
    EmbeddingProvider,
    HashEmbeddingProvider,
    cosine_similarity,
    provider_from_env,
    unpack_vector,
)
from .models import SearchHit
from .repository import Repository
from .text import fts_query
from .util import json_loads


class HybridRetriever:
    def __init__(self, database: Database, embedding_provider: EmbeddingProvider | None = None):
        self.database = database
        self.embedding_provider = embedding_provider or provider_from_env()
        self.repository = Repository(database)

    def _where(self, project_id: str, filters: dict[str, Any]) -> tuple[str, list[Any]]:
        clauses = ["c.project_id=?", "c.branch=?"]
        params: list[Any] = [project_id, filters.get("branch", "main")]
        if filters.get("canon_status"):
            values = filters["canon_status"]
            if isinstance(values, str):
                values = [values]
            clauses.append("c.canon_status IN (" + ",".join("?" for _ in values) + ")")
            params.extend(values)
        if filters.get("episode") is not None and filters.get("strict_time"):
            clauses.append("c.episode=?")
            params.append(int(filters["episode"]))
        if filters.get("kind"):
            clauses.append("d.kind=?")
            params.append(filters["kind"])
        return " AND ".join(clauses), params

    def _lexical(
        self,
        project_id: str,
        query: str,
        filters: dict[str, Any],
        limit: int,
    ) -> list[tuple[str, float]]:
        match = fts_query(query)
        if not match:
            return []
        where, params = self._where(project_id, filters)
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT c.id, bm25(chunks_fts) AS lexical_rank
                FROM chunks_fts
                JOIN chunks c ON c.id=chunks_fts.chunk_id
                JOIN documents d ON d.id=c.document_id
                WHERE chunks_fts MATCH ? AND {where}
                ORDER BY lexical_rank LIMIT ?
                """,
                [match, *params, limit],
            ).fetchall()
        return [(row["id"], float(row["lexical_rank"])) for row in rows]

    def _vector(
        self,
        project_id: str,
        query: str,
        filters: dict[str, Any],
        limit: int,
    ) -> tuple[list[tuple[str, float]], dict[str, Any]]:
        where, params = self._where(project_id, filters)
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT c.id, c.embedding, c.embedding_dim, c.embedding_provider
                FROM chunks c JOIN documents d ON d.id=c.document_id
                WHERE {where} AND c.embedding IS NOT NULL
                LIMIT 50000
                """,
                params,
            ).fetchall()
        status: dict[str, Any] = {
            "requested_provider": self.embedding_provider.name,
            "used_providers": [],
            "degraded": False,
        }
        query_vectors: dict[tuple[str, int], list[float]] = {}
        remote_vector: list[float] | None = None
        if any(row["embedding_provider"] == self.embedding_provider.name for row in rows):
            try:
                remote_vector = self.embedding_provider.embed([query])[0]
            except Exception as exc:  # lexical and local-hash retrieval remain available
                status["degraded"] = True
                status["warning"] = f"Embedding query failed; lexical/local-hash retrieval used: {exc}"

        scored: list[tuple[str, float]] = []
        used: set[str] = set()
        for row in rows:
            dimension = int(row["embedding_dim"] or 0)
            provider_name = str(row["embedding_provider"] or "hash-v1")
            key = (provider_name, dimension)
            if provider_name.startswith("hash-v1") and dimension > 0:
                if key not in query_vectors:
                    query_vectors[key] = HashEmbeddingProvider(dimension=dimension).embed([query])[0]
            elif provider_name == self.embedding_provider.name and remote_vector and len(remote_vector) == dimension:
                query_vectors[key] = remote_vector
            query_vector = query_vectors.get(key)
            if query_vector is None:
                continue
            used.add(provider_name)
            scored.append(
                (
                    row["id"],
                    cosine_similarity(query_vector, unpack_vector(row["embedding"], dimension)),
                )
            )
        scored.sort(key=lambda item: item[1], reverse=True)
        status["used_providers"] = sorted(used)
        if rows and not scored:
            status["degraded"] = True
            status.setdefault(
                "warning",
                "No compatible stored vector provider/dimension; lexical retrieval used",
            )
        return scored[:limit], status

    def search(
        self,
        project_id: str,
        query: str,
        *,
        top_k: int = 8,
        filters: dict[str, Any] | None = None,
    ) -> list[SearchHit]:
        hits, _policy = self._search(project_id, query, top_k=top_k, filters=filters)
        return hits

    def _search(
        self,
        project_id: str,
        query: str,
        *,
        top_k: int = 8,
        filters: dict[str, Any] | None = None,
    ) -> tuple[list[SearchHit], dict[str, Any]]:
        filters = dict(filters or {})
        branch = filters.get("branch", "main")
        graph = self.repository.graph_context(project_id, query, branch=branch)
        expansion_names: list[str] = []
        for relation in graph["relations"]:
            expansion_names.extend([relation["source_name"], relation["target_name"], relation["predicate"]])
        expanded_query = query + (" " + " ".join(dict.fromkeys(expansion_names)) if expansion_names else "")

        candidate_limit = max(top_k * 8, 32)
        lexical = self._lexical(project_id, expanded_query, filters, candidate_limit)
        vector, vector_policy = self._vector(project_id, query, filters, candidate_limit)

        scores: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        for rank, (chunk_id, bm25_value) in enumerate(lexical, start=1):
            scores[chunk_id]["lexical"] = 1.0 / (50.0 + rank)
            scores[chunk_id]["bm25"] = bm25_value
        for rank, (chunk_id, similarity) in enumerate(vector, start=1):
            scores[chunk_id]["vector"] = max(0.0, similarity) / (40.0 + rank)
            scores[chunk_id]["cosine"] = similarity
        for chunk_id in graph["evidence_chunk_ids"]:
            scores[chunk_id]["graph"] = 0.024

        if not scores:
            return [], vector_policy
        ids = list(scores)
        placeholders = ",".join("?" for _ in ids)
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT c.*, d.path, d.title, d.kind
                FROM chunks c JOIN documents d ON d.id=c.document_id
                WHERE c.id IN ({placeholders})
                """,
                ids,
            ).fetchall()

        requested_episode = filters.get("episode")
        ranked: list[tuple[float, SearchHit]] = []
        canon_boosts = {"canon": 0.015, "approved": 0.012, "draft": 0.004, "reference": 0.002}
        for row in rows:
            chunk_scores = scores[row["id"]]
            if requested_episode is not None and row["episode"] is not None:
                distance = abs(int(row["episode"]) - int(requested_episode))
                chunk_scores["time"] = 0.012 / (1.0 + distance)
            chunk_scores["canon"] = canon_boosts.get(row["canon_status"], 0.0)
            combined = (
                chunk_scores.get("lexical", 0.0) * 0.48
                + chunk_scores.get("vector", 0.0) * 0.38
                + chunk_scores.get("graph", 0.0)
                + chunk_scores.get("time", 0.0)
                + chunk_scores.get("canon", 0.0)
            )
            metadata = json_loads(row["metadata_json"])
            hit = SearchHit(
                chunk_id=row["id"],
                document_id=row["document_id"],
                path=row["path"],
                title=row["title"],
                kind=row["kind"],
                text=row["text"],
                metadata=metadata,
                score=combined,
                score_breakdown={
                    "lexical_rrf": chunk_scores.get("lexical", 0.0),
                    "vector_rrf": chunk_scores.get("vector", 0.0),
                    "cosine": chunk_scores.get("cosine", 0.0),
                    "graph": chunk_scores.get("graph", 0.0),
                    "time": chunk_scores.get("time", 0.0),
                    "canon": chunk_scores.get("canon", 0.0),
                },
            )
            ranked.append((combined, hit))
        ranked.sort(key=lambda item: item[0], reverse=True)
        if not ranked:
            return [], vector_policy
        maximum = ranked[0][0] or 1.0
        results: list[SearchHit] = []
        for combined, hit in ranked[:top_k]:
            hit.score = combined / maximum
            results.append(hit)
        return results, vector_policy

    def build_context(
        self,
        project_id: str,
        query: str,
        *,
        top_k: int = 8,
        scope: ContextScope | None = None,
        filters: dict[str, Any] | None = None,
        character_name: str | None = None,
        dimension: str | None = None,
    ) -> dict[str, Any]:
        legacy_filters = dict(filters or {})
        if scope is None:
            scope = ContextScope.from_payload(
                {
                    "filters": legacy_filters,
                    "character_name": character_name,
                    "dimension": dimension,
                }
            )
        search_filters = {
            **legacy_filters,
            "branch": scope.branch,
            "episode": scope.episode,
        }
        hits, vector_policy = self._search(project_id, query, top_k=top_k, filters=search_filters)
        graph = self.repository.graph_context(project_id, query, branch=scope.branch)
        timeline_result = self.repository.timeline_context(
            project_id,
            event_id=scope.scene_id,
            episode=scope.episode,
            branch=scope.branch,
            radius=1,
        )
        current_event = timeline_result["current"]
        timeline = timeline_result["events"]

        selected_character = scope.character_name
        if not selected_character:
            for entity in graph["entities"]:
                if entity["entity_type"].lower() in {"character", "person", "人物"}:
                    selected_character = entity["name"]
                    break
        event_ids = [event["id"] for event in timeline]
        ohlc = self.repository.ohlc_for_timeline_events(
            project_id,
            event_ids,
            character_name=selected_character,
            dimension=scope.dimension,
            branch=scope.branch,
        )

        resolved_scope = {
            "branch": scope.branch,
            "episode": current_event.get("episode") if current_event else scope.episode,
            "scene_id": current_event.get("id") if current_event else scope.scene_id,
            "character_name": selected_character,
            "dimension": scope.dimension,
        }

        citations = [hit.citation() for hit in hits]
        evidence_refs = build_evidence_refs(
            sources=citations,
            graph=graph,
            timeline=timeline,
            ohlc=ohlc,
        )
        section_titles = {
            "source": "Sources",
            "graph": "Graph",
            "timeline": "Timeline",
            "kline": "Character state",
            "version": "Versions",
            "rule": "Rules",
            "issue": "Issues",
        }
        grouped: dict[str, list[str]] = defaultdict(list)
        for ref in evidence_refs:
            locator = ", ".join(
                f"{key}={value}" for key, value in ref["locator"].items() if value is not None
            )
            detail = f"[{ref['ref']}] {ref['title']}"
            if locator:
                detail += f"\n定位：{locator}"
            if ref["summary"]:
                detail += f"\n{ref['summary']}"
            grouped[ref["kind"]].append(detail)
        sections = [
            f"## {section_titles[kind]}\n" + "\n\n".join(grouped[kind])
            for kind in section_titles
            if grouped.get(kind)
        ]
        context_text = "\n\n".join(sections)

        return {
            "query": query,
            "context": context_text,
            "context_text": context_text,
            "resolved_scope": resolved_scope,
            "citations": citations,
            "evidence_refs": evidence_refs,
            "graph": graph,
            "timeline": timeline,
            "ohlc": ohlc,
            "retrieval_policy": {
                "lexical": "FTS5 with Chinese bigram/trigram tokens",
                "vector": vector_policy,
                "graph_expansion": True,
                "time_filter": resolved_scope["episode"],
                "branch": scope.branch,
            },
        }

