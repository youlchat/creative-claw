from __future__ import annotations

from pathlib import Path
from typing import Any

from .db import Database
from .embeddings import EmbeddingProvider, HashEmbeddingProvider, pack_vector, provider_from_env
from .extractors import extract, kind_for_path, supported_suffixes
from .ledger import Ledger
from .models import ExtractedUnit, ImportResult
from .text import chunk_units, fts_document
from .util import json_dumps, json_loads, new_id, sha256_file, sha256_text, utc_now


class Indexer:
    def __init__(self, database: Database, embedding_provider: EmbeddingProvider | None = None):
        self.database = database
        self.embedding_provider = embedding_provider or provider_from_env()
        self.ledger = Ledger(database)

    def _embed(self, texts: list[str], batch_size: int = 64) -> tuple[list[list[float]], str]:
        vectors: list[list[float]] = []
        try:
            for start in range(0, len(texts), batch_size):
                vectors.extend(self.embedding_provider.embed(texts[start : start + batch_size]))
            return vectors, self.embedding_provider.name
        except Exception:
            fallback = HashEmbeddingProvider()
            return fallback.embed(texts), fallback.name

    def import_file(
        self,
        project_id: str,
        path: str | Path,
        *,
        metadata: dict[str, Any] | None = None,
        branch: str = "main",
        canon_status: str = "reference",
        actor: str = "user",
        force: bool = False,
    ) -> ImportResult:
        source = Path(path).resolve()
        if not source.is_file():
            raise FileNotFoundError(source)
        units = extract(source)
        return self._index_units(
            project_id,
            source,
            units,
            sha256_file(source),
            kind_for_path(source),
            metadata=metadata,
            branch=branch,
            canon_status=canon_status,
            actor=actor,
            force=force,
        )

    def index_text(
        self,
        project_id: str,
        virtual_path: str,
        text: str,
        *,
        title: str | None = None,
        metadata: dict[str, Any] | None = None,
        branch: str = "main",
        canon_status: str = "reference",
        actor: str = "user",
        force: bool = False,
    ) -> ImportResult:
        path = Path(virtual_path)
        unit = ExtractedUnit(text, {"source_type": "text", **(metadata or {})})
        return self._index_units(
            project_id,
            path,
            [unit],
            sha256_text(text),
            "text",
            title=title,
            metadata=metadata,
            branch=branch,
            canon_status=canon_status,
            actor=actor,
            force=force,
        )

    def _index_units(
        self,
        project_id: str,
        source: Path,
        units: list[ExtractedUnit],
        digest: str,
        kind: str,
        *,
        title: str | None = None,
        metadata: dict[str, Any] | None = None,
        branch: str,
        canon_status: str,
        actor: str,
        force: bool,
    ) -> ImportResult:
        source_key = str(source.resolve()) if source.is_absolute() else source.as_posix()
        drafts = chunk_units(units, source_path=source_key)
        if not drafts:
            raise ValueError(f"No indexable text extracted from {source}")
        vectors, provider_name = self._embed([draft.text for draft in drafts])
        if len(vectors) != len(drafts):
            raise RuntimeError("Embedding provider returned a mismatched vector count")
        now = utc_now()
        with self.database.connect() as connection:
            existing = connection.execute(
                "SELECT * FROM documents WHERE project_id=? AND path=?",
                (project_id, source_key),
            ).fetchone()
            if existing and existing["sha256"] == digest and not force:
                count = connection.execute(
                    "SELECT COUNT(*) AS n FROM chunks WHERE document_id=?", (existing["id"],)
                ).fetchone()["n"]
                return ImportResult(existing["id"], source, existing["kind"], existing["version"], len(units), count, True)

            document_id = existing["id"] if existing else new_id("doc")
            version = (existing["version"] + 1) if existing else 1
            document_metadata = {**(metadata or {}), "embedding_provider": provider_name}
            if existing:
                old_ids = [
                    row["id"]
                    for row in connection.execute("SELECT id FROM chunks WHERE document_id=?", (document_id,)).fetchall()
                ]
                if old_ids:
                    placeholders = ",".join("?" for _ in old_ids)
                    connection.execute(f"DELETE FROM chunks_fts WHERE chunk_id IN ({placeholders})", old_ids)
                connection.execute("DELETE FROM chunks WHERE document_id=?", (document_id,))
                connection.execute(
                    """
                    UPDATE documents SET kind=?, title=?, sha256=?, version=?, metadata_json=?, updated_at=? WHERE id=?
                    """,
                    (kind, title or source.stem, digest, version, json_dumps(document_metadata), now, document_id),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO documents(id, project_id, path, kind, title, sha256, version, metadata_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        document_id,
                        project_id,
                        source_key,
                        kind,
                        title or source.stem,
                        digest,
                        version,
                        json_dumps(document_metadata),
                        now,
                        now,
                    ),
                )

            for draft, vector in zip(drafts, vectors):
                chunk_id = new_id("chk")
                chunk_metadata = {**(metadata or {}), **draft.metadata, "document_version": version}
                search_text = fts_document(draft.text)
                connection.execute(
                    """
                    INSERT INTO chunks(id, project_id, document_id, ordinal, text, search_text, embedding, embedding_dim,
                                       embedding_provider, metadata_json, branch, canon_status, episode, scene, story_time,
                                       created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        chunk_id,
                        project_id,
                        document_id,
                        draft.ordinal,
                        draft.text,
                        search_text,
                        pack_vector(vector),
                        len(vector),
                        provider_name,
                        json_dumps(chunk_metadata),
                        branch,
                        canon_status,
                        chunk_metadata.get("episode"),
                        chunk_metadata.get("scene"),
                        chunk_metadata.get("story_time"),
                        now,
                    ),
                )
                connection.execute(
                    "INSERT INTO chunks_fts(chunk_id, project_id, search_text) VALUES (?, ?, ?)",
                    (chunk_id, project_id, search_text),
                )

        self.ledger.append(
            project_id,
            "document.indexed",
            {
                "document_id": document_id,
                "path": source_key,
                "kind": kind,
                "version": version,
                "unit_count": len(units),
                "chunk_count": len(drafts),
                "embedding_provider": provider_name,
                "branch": branch,
                "canon_status": canon_status,
            },
            actor,
        )
        return ImportResult(document_id, source, kind, version, len(units), len(drafts), False)

    def delete_document(
        self,
        project_id: str,
        document_id: str,
        *,
        actor: str = "user",
    ) -> dict[str, Any]:
        """Delete an indexed source and its derived chunks, never the source file."""

        with self.database.connect() as connection:
            document = connection.execute(
                "SELECT * FROM documents WHERE project_id=? AND id=?",
                (project_id, document_id),
            ).fetchone()
            if not document:
                raise KeyError(f"Unknown document: {document_id}")
            chunk_ids = [
                row["id"]
                for row in connection.execute(
                    "SELECT id FROM chunks WHERE document_id=?", (document_id,)
                ).fetchall()
            ]
            if chunk_ids:
                placeholders = ",".join("?" for _ in chunk_ids)
                connection.execute(
                    f"DELETE FROM chunks_fts WHERE chunk_id IN ({placeholders})",
                    chunk_ids,
                )
            connection.execute("DELETE FROM documents WHERE id=?", (document_id,))
        result = {
            "document_id": document_id,
            "path": document["path"],
            "deleted_chunks": len(chunk_ids),
            "source_file_deleted": False,
        }
        self.ledger.append(project_id, "document.deleted", result, actor)
        return result

    def reindex_document(
        self,
        project_id: str,
        document_id: str,
        *,
        actor: str = "user",
    ) -> ImportResult:
        """Re-extract a physical source even when its content hash is unchanged."""

        with self.database.connect() as connection:
            document = connection.execute(
                "SELECT * FROM documents WHERE project_id=? AND id=?",
                (project_id, document_id),
            ).fetchone()
            first_chunk = connection.execute(
                "SELECT branch, canon_status FROM chunks WHERE document_id=? ORDER BY ordinal LIMIT 1",
                (document_id,),
            ).fetchone()
        if not document:
            raise KeyError(f"Unknown document: {document_id}")
        source = Path(document["path"])
        if not source.is_absolute() or not source.is_file():
            raise ValueError(
                "This source is virtual or missing; import/index its current text again instead"
            )
        metadata = json_loads(document["metadata_json"])
        metadata.pop("embedding_provider", None)
        return self.import_file(
            project_id,
            source,
            metadata=metadata,
            branch=first_chunk["branch"] if first_chunk else "main",
            canon_status=first_chunk["canon_status"] if first_chunk else "reference",
            actor=actor,
            force=True,
        )

    def backfill_embeddings(
        self,
        project_id: str,
        *,
        replace: bool = False,
        batch_size: int = 64,
        actor: str = "user",
    ) -> dict[str, Any]:
        """Fill missing vectors or deliberately replace all vectors for a project."""

        where = "c.project_id=?" if replace else "c.project_id=? AND c.embedding IS NULL"
        with self.database.connect() as connection:
            rows = connection.execute(
                f"SELECT c.id, c.document_id, c.text FROM chunks c WHERE {where} ORDER BY c.document_id, c.ordinal",
                (project_id,),
            ).fetchall()
        if not rows:
            return {"updated_chunks": 0, "embedding_provider": self.embedding_provider.name, "replace": replace}

        vectors, provider_name = self._embed([row["text"] for row in rows], batch_size=batch_size)
        if len(vectors) != len(rows):
            raise RuntimeError("Embedding provider returned a mismatched vector count")
        document_ids = sorted({row["document_id"] for row in rows})
        with self.database.connect() as connection:
            for row, vector in zip(rows, vectors):
                connection.execute(
                    "UPDATE chunks SET embedding=?, embedding_dim=?, embedding_provider=? WHERE id=?",
                    (pack_vector(vector), len(vector), provider_name, row["id"]),
                )
            for document_id in document_ids:
                providers = connection.execute(
                    "SELECT DISTINCT embedding_provider FROM chunks WHERE document_id=?",
                    (document_id,),
                ).fetchall()
                if len(providers) == 1:
                    document = connection.execute(
                        "SELECT metadata_json FROM documents WHERE id=?", (document_id,)
                    ).fetchone()
                    metadata = json_loads(document["metadata_json"]) if document else {}
                    metadata["embedding_provider"] = providers[0]["embedding_provider"]
                    connection.execute(
                        "UPDATE documents SET metadata_json=?, updated_at=? WHERE id=?",
                        (json_dumps(metadata), utc_now(), document_id),
                    )
        result = {
            "updated_chunks": len(rows),
            "updated_documents": len(document_ids),
            "embedding_provider": provider_name,
            "replace": replace,
        }
        self.ledger.append(project_id, "embeddings.backfilled", result, actor)
        return result

    def import_path(
        self,
        project_id: str,
        path: str | Path,
        *,
        recursive: bool = True,
        max_files: int = 10_000,
        metadata: dict[str, Any] | None = None,
        branch: str = "main",
        canon_status: str = "reference",
        actor: str = "user",
    ) -> list[ImportResult]:
        source = Path(path).resolve()
        if source.is_file():
            return [
                self.import_file(
                    project_id,
                    source,
                    metadata=metadata,
                    branch=branch,
                    canon_status=canon_status,
                    actor=actor,
                )
            ]
        if not source.is_dir():
            raise FileNotFoundError(source)
        iterator = source.rglob("*") if recursive else source.glob("*")
        paths = [
            candidate
            for candidate in iterator
            if candidate.is_file()
            and candidate.suffix.lower() in supported_suffixes()
            and not any(part.startswith(".") for part in candidate.relative_to(source).parts)
        ]
        paths.sort(key=lambda item: item.as_posix().lower())
        if len(paths) > max_files:
            raise ValueError(f"Import contains {len(paths)} supported files, above max_files={max_files}")
        results: list[ImportResult] = []
        for candidate in paths:
            results.append(
                self.import_file(
                    project_id,
                    candidate,
                    metadata=metadata,
                    branch=branch,
                    canon_status=canon_status,
                    actor=actor,
                )
            )
        return results
