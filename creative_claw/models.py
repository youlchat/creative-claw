from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class ExtractedUnit:
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ChunkDraft:
    text: str
    metadata: dict[str, Any]
    ordinal: int


@dataclass(slots=True)
class SearchHit:
    chunk_id: str
    document_id: str
    path: str
    title: str
    kind: str
    text: str
    metadata: dict[str, Any]
    score: float
    score_breakdown: dict[str, float]

    def citation(self) -> dict[str, Any]:
        locator = {
            key: self.metadata[key]
            for key in ("page", "slide", "sheet", "row", "paragraph", "table", "episode", "scene")
            if self.metadata.get(key) is not None
        }
        return {
            "chunk_id": self.chunk_id,
            "document_id": self.document_id,
            "path": self.path,
            "title": self.title,
            "kind": self.kind,
            "locator": locator,
            "score": round(self.score, 6),
            "score_breakdown": {k: round(v, 6) for k, v in self.score_breakdown.items()},
            "snippet": self.text[:420],
        }


@dataclass(slots=True)
class ImportResult:
    document_id: str
    path: Path
    kind: str
    version: int
    unit_count: int
    chunk_count: int
    skipped: bool = False

