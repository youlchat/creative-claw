from __future__ import annotations

import json
import math
import re
import sqlite3
from typing import Any, Protocol

from .db import Database
from .ledger import Ledger
from .repository import Repository
from .util import json_dumps, new_id, utc_now


ALLOWED_ENTITY_TYPES = frozenset(
    {"character", "location", "object", "organization", "canon_fact"}
)
PROJECT_CONTENT_TABLES = (
    "documents",
    "entities",
    "relations",
    "timeline_events",
    "ohlc_points",
    "production_units",
    "artifacts",
)


class ColdStartWriter(Protocol):
    model: str

    def generate(
        self,
        prompt: str,
        *,
        repair: dict[str, str] | None = None,
    ) -> str: ...


class ColdStartConflictError(ValueError):
    """Raised when a project gains content before cold-start adoption."""


def _required_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} is required")
    return text


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def parse_preview_text(text: str) -> dict[str, Any]:
    content = str(text or "").strip()
    fenced = re.fullmatch(
        r"```(?:json)?\s*(.*?)\s*```",
        content,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if fenced:
        content = fenced.group(1)
    value = json.loads(content)
    if not isinstance(value, dict):
        raise ValueError("Cold-start preview must be a JSON object")
    return value


def _normalize_entities(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list) or not 3 <= len(value) <= 5:
        raise ValueError("entities must contain between 3 and 5 items")
    result: list[dict[str, str]] = []
    seen_keys: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"entities[{index}] must be an object")
        key = _required_text(item.get("key"), f"entities[{index}].key")
        if key in seen_keys:
            raise ValueError(f"duplicate entity key: {key}")
        entity_type = _required_text(
            item.get("entity_type"), f"entities[{index}].entity_type"
        ).lower()
        if entity_type not in ALLOWED_ENTITY_TYPES:
            raise ValueError(f"unsupported entity type: {entity_type}")
        seen_keys.add(key)
        result.append(
            {
                "key": key,
                "name": _required_text(item.get("name"), f"entities[{index}].name"),
                "entity_type": entity_type,
                "description": _required_text(
                    item.get("description"), f"entities[{index}].description"
                ),
            }
        )
    return result


def _normalize_relations(
    value: Any,
    entity_keys: set[str],
) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise ValueError("relations must be an array")
    result: list[dict[str, str]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"relations[{index}] must be an object")
        source_key = _required_text(
            item.get("source_key"), f"relations[{index}].source_key"
        )
        target_key = _required_text(
            item.get("target_key"), f"relations[{index}].target_key"
        )
        if source_key not in entity_keys or target_key not in entity_keys:
            raise ValueError(f"relations[{index}] references an unknown entity")
        result.append(
            {
                "source_key": source_key,
                "predicate": _required_text(
                    item.get("predicate"), f"relations[{index}].predicate"
                ),
                "target_key": target_key,
            }
        )
    return result


def _number(value: Any, field: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field} must be numeric") from error
    if not math.isfinite(number) or not 0 <= number <= 100:
        raise ValueError(f"{field} must be between 0 and 100")
    return round(number, 1)


def _normalize_scenes(
    value: Any,
    entity_keys: set[str],
) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not 6 <= len(value) <= 8:
        raise ValueError("scenes must contain between 6 and 8 items")
    result: list[dict[str, Any]] = []
    previous_close: float | None = None
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"scenes[{index}] must be an object")
        raw_entity_keys = item.get("entity_keys")
        if not isinstance(raw_entity_keys, list):
            raise ValueError(f"scenes[{index}].entity_keys must be an array")
        scene_entity_keys: list[str] = []
        for raw_key in raw_entity_keys:
            key = _required_text(raw_key, f"scenes[{index}].entity_keys")
            if key not in entity_keys:
                raise ValueError(f"scenes[{index}] references an unknown entity")
            if key not in scene_entity_keys:
                scene_entity_keys.append(key)
        raw_ohlc = item.get("ohlc")
        if not isinstance(raw_ohlc, dict):
            raise ValueError(f"scenes[{index}].ohlc must be an object")
        missing = [field for field in ("open", "high", "low", "close") if field not in raw_ohlc]
        if missing:
            raise ValueError(f"scenes[{index}].ohlc is missing: {', '.join(missing)}")
        open_value = _number(raw_ohlc["open"], f"scenes[{index}].ohlc.open")
        high = _number(raw_ohlc["high"], f"scenes[{index}].ohlc.high")
        low = _number(raw_ohlc["low"], f"scenes[{index}].ohlc.low")
        close = _number(raw_ohlc["close"], f"scenes[{index}].ohlc.close")
        if previous_close is not None:
            open_value = previous_close
        high = max(high, open_value, close)
        low = min(low, open_value, close)
        previous_close = close
        result.append(
            {
                "title": _required_text(item.get("title"), f"scenes[{index}].title"),
                "summary": _required_text(
                    item.get("summary"), f"scenes[{index}].summary"
                ),
                "story_time": _optional_text(item.get("story_time")),
                "entity_keys": scene_entity_keys,
                "ohlc": {
                    "open": open_value,
                    "high": high,
                    "low": low,
                    "close": close,
                },
            }
        )
    return result


def normalize_preview(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("Cold-start preview must be an object")
    title = _required_text(value.get("title"), "title")
    premise = _required_text(value.get("premise"), "premise")
    protagonist_key = _required_text(value.get("protagonist_key"), "protagonist_key")
    dimension = _required_text(value.get("kline_dimension"), "kline_dimension")
    entities = _normalize_entities(value.get("entities"))
    entity_by_key = {item["key"]: item for item in entities}
    if (
        protagonist_key not in entity_by_key
        or entity_by_key[protagonist_key]["entity_type"] != "character"
    ):
        raise ValueError("protagonist_key must reference a character")
    entity_keys = set(entity_by_key)
    relations = _normalize_relations(value.get("relations", []), entity_keys)
    scenes = _normalize_scenes(value.get("scenes"), entity_keys)
    return {
        "title": title,
        "premise": premise,
        "protagonist_key": protagonist_key,
        "kline_dimension": dimension,
        "entities": entities,
        "relations": relations,
        "scenes": scenes,
    }


class ColdStartService:
    def __init__(self, database: Database):
        self.database = database
        self.ledger = Ledger(database)

    def is_empty(
        self,
        project_id: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> bool:
        if connection is None:
            with self.database.connect() as own_connection:
                return self.is_empty(project_id, connection=own_connection)
        project = connection.execute(
            "SELECT 1 FROM projects WHERE id=?", (project_id,)
        ).fetchone()
        if project is None:
            raise KeyError(f"Unknown project: {project_id}")
        for table in PROJECT_CONTENT_TABLES:
            exists = connection.execute(
                f"SELECT 1 FROM {table} WHERE project_id=? LIMIT 1", (project_id,)
            ).fetchone()
            if exists is not None:
                return False
        return True

    def preview(
        self,
        project_id: str,
        prompt: str,
        writer: ColdStartWriter,
    ) -> dict[str, Any]:
        clean_prompt = _required_text(prompt, "prompt")
        if not self.is_empty(project_id):
            raise ColdStartConflictError("冷启动仅适用于空项目，请先新建项目")
        first_response = writer.generate(clean_prompt)
        try:
            normalized = normalize_preview(parse_preview_text(first_response))
        except (TypeError, ValueError) as first_error:
            repaired_response = writer.generate(
                clean_prompt,
                repair={
                    "response": str(first_response)[:28_000],
                    "error": str(first_error),
                },
            )
            normalized = normalize_preview(parse_preview_text(repaired_response))
        return {
            "preview": normalized,
            "generation": {"prompt": clean_prompt, "model": writer.model},
        }

    def apply(
        self,
        project_id: str,
        preview: Any,
        generation: Any,
    ) -> dict[str, Any]:
        normalized = normalize_preview(preview)
        if not isinstance(generation, dict):
            raise ValueError("generation must be an object")
        prompt = _required_text(generation.get("prompt"), "generation.prompt")
        model = _required_text(generation.get("model"), "generation.model")
        created_at = utc_now()
        entity_ids: dict[str, str] = {}
        relation_ids: list[str] = []
        scene_ids: list[str] = []
        ohlc_ids: list[str] = []

        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if not self.is_empty(project_id, connection=connection):
                raise ColdStartConflictError("冷启动仅适用于空项目，请先新建项目")
            connection.execute(
                "UPDATE projects SET name=? WHERE id=?",
                (normalized["title"], project_id),
            )

            for entity in normalized["entities"]:
                entity_id = new_id("ent")
                entity_ids[entity["key"]] = entity_id
                connection.execute(
                    """
                    INSERT INTO entities(
                        id, project_id, name, entity_type, aliases_json, attrs_json,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        entity_id,
                        project_id,
                        entity["name"],
                        entity["entity_type"],
                        "[]",
                        json_dumps(
                            {
                                "description": entity["description"],
                                "source": "cold_start",
                            }
                        ),
                        created_at,
                        created_at,
                    ),
                )

            for relation in normalized["relations"]:
                relation_id = new_id("rel")
                relation_ids.append(relation_id)
                connection.execute(
                    """
                    INSERT INTO relations(
                        id, project_id, source_id, predicate, target_id,
                        evidence_chunk_id, valid_from, valid_to, branch,
                        attrs_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, 'main', ?, ?)
                    """,
                    (
                        relation_id,
                        project_id,
                        entity_ids[relation["source_key"]],
                        relation["predicate"],
                        entity_ids[relation["target_key"]],
                        json_dumps({"source": "cold_start"}),
                        created_at,
                    ),
                )

            protagonist = next(
                entity
                for entity in normalized["entities"]
                if entity["key"] == normalized["protagonist_key"]
            )
            for position, scene in enumerate(normalized["scenes"], start=1):
                scene_id = new_id("time")
                scene_ids.append(scene_id)
                scene_attrs: dict[str, Any] = {
                    "status": "outline",
                    "format": "scene_card",
                    "source": "cold_start",
                    "entity_ids": [entity_ids[key] for key in scene["entity_keys"]],
                }
                if position == 1:
                    scene_attrs["premise"] = normalized["premise"]
                connection.execute(
                    """
                    INSERT INTO timeline_events(
                        id, project_id, label, story_time, episode, scene,
                        description, evidence_chunk_id, branch, attrs_json, created_at
                    ) VALUES (?, ?, ?, ?, 1, ?, ?, NULL, 'main', ?, ?)
                    """,
                    (
                        scene_id,
                        project_id,
                        scene["title"],
                        scene["story_time"],
                        position,
                        scene["summary"],
                        json_dumps(scene_attrs),
                        created_at,
                    ),
                )

                ohlc_id = new_id("ohlc")
                ohlc_ids.append(ohlc_id)
                period_id = f"E1-S{position:02d}"
                values = scene["ohlc"]
                connection.execute(
                    """
                    INSERT INTO ohlc_points(
                        id, project_id, character_name, dimension, period_type,
                        period_id, parent_period_id, sort_key, open, high, low, close,
                        evidence_chunk_id, timeline_event_id, branch, attrs_json,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'scene', ?, NULL, ?, ?, ?, ?, ?, NULL, ?, 'main', ?, ?, ?)
                    """,
                    (
                        ohlc_id,
                        project_id,
                        protagonist["name"],
                        normalized["kline_dimension"],
                        period_id,
                        1 + position / 100,
                        values["open"],
                        values["high"],
                        values["low"],
                        values["close"],
                        scene_id,
                        json_dumps({"source": "cold_start"}),
                        created_at,
                        created_at,
                    ),
                )

            summary = {
                "entities": len(entity_ids),
                "relations": len(relation_ids),
                "scenes": len(scene_ids),
                "ohlc": len(ohlc_ids),
            }
            self.ledger.append(
                project_id,
                "cold_start.applied",
                {
                    "prompt": prompt,
                    "model": model,
                    "title": normalized["title"],
                    "premise": normalized["premise"],
                    "entity_ids": list(entity_ids.values()),
                    "relation_ids": relation_ids,
                    "scene_ids": scene_ids,
                    "ohlc_ids": ohlc_ids,
                    "counts": summary,
                },
                actor="ai",
                connection=connection,
            )

        return {
            "summary": summary,
            "snapshot": Repository(self.database).canvas_snapshot(project_id),
        }
