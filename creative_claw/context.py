from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


def _clean_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return int(value)


@dataclass(frozen=True, slots=True)
class ContextScope:
    branch: str = "main"
    episode: int | None = None
    scene_id: str | None = None
    character_name: str | None = None
    dimension: str | None = None

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ContextScope":
        scope = dict(payload.get("scope") or {})
        filters = dict(payload.get("filters") or {})
        return cls(
            branch=_clean_text(scope.get("branch")) or _clean_text(filters.get("branch")) or "main",
            episode=_clean_int(scope.get("episode") if "episode" in scope else filters.get("episode")),
            scene_id=_clean_text(scope.get("scene_id")),
            character_name=_clean_text(
                scope.get("character_name") if "character_name" in scope else payload.get("character_name")
            ),
            dimension=_clean_text(scope.get("dimension") if "dimension" in scope else payload.get("dimension")),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
