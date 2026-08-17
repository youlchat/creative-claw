from __future__ import annotations

from copy import deepcopy
from typing import Any


BLUEPRINT_DIMENSIONS = (
    "narrative_function",
    "characters",
    "relationships",
    "goals",
    "obstacles",
    "stakes",
    "events",
    "causality",
    "conflict",
    "turns",
    "reveals",
    "suspense",
    "setup_payoff",
    "pov",
    "story_time",
    "discourse_time",
    "location",
    "emotion_kline",
    "pacing",
    "themes",
    "motifs",
    "imagery",
    "style_statistics",
)

DIMENSION_STATES = frozenset({"observed", "not_observed", "uncertain"})
NODE_TYPES = frozenset({"work", "volume", "phase", "chapter", "episode", "scene", "beat"})
EDGE_TYPES = frozenset({"contains", "causes", "reveals", "sets_up", "pays_off", "changes", "mirrors"})
MAPPING_ACTIONS = frozenset({"preserve", "transform", "drop", "add"})
RIGHTS_BASES = frozenset({"owned", "licensed", "public_domain", "research_reference"})


def _confidence(value: Any, *, field: str = "confidence") -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be between 0 and 1") from exc
    if not 0 <= result <= 1:
        raise ValueError(f"{field} must be between 0 and 1")
    return result


def validate_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(evidence, dict):
        raise ValueError("evidence must be an object")
    try:
        start = int(evidence["start"])
        end = int(evidence["end"])
        source_length = int(evidence["source_length"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("evidence range requires integer start, end and source_length") from exc
    if start < 0 or end <= start or source_length < 0 or end > source_length:
        raise ValueError(
            f"evidence range must satisfy 0 <= start < end <= source_length; got {start}:{end}/{source_length}"
        )
    result = deepcopy(evidence)
    result.update({"start": start, "end": end, "source_length": source_length})
    if "confidence" in result:
        result["confidence"] = _confidence(result["confidence"])
    return result


def validate_node(node: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(node, dict):
        raise ValueError("blueprint node must be an object")
    dimensions = node.get("dimensions")
    if not isinstance(dimensions, dict):
        raise ValueError("missing blueprint dimensions: " + ", ".join(BLUEPRINT_DIMENSIONS))
    missing = [name for name in BLUEPRINT_DIMENSIONS if name not in dimensions]
    extra = sorted(set(dimensions).difference(BLUEPRINT_DIMENSIONS))
    if missing:
        raise ValueError("missing blueprint dimensions: " + ", ".join(missing))
    if extra:
        raise ValueError("unknown blueprint dimensions: " + ", ".join(extra))

    normalized = deepcopy(node)
    normalized_dimensions: dict[str, dict[str, Any]] = {}
    for name in BLUEPRINT_DIMENSIONS:
        item = dimensions[name]
        if not isinstance(item, dict):
            raise ValueError(f"blueprint dimension {name} must be an object")
        state = str(item.get("state") or "")
        if state not in DIMENSION_STATES:
            raise ValueError(f"invalid blueprint dimension state for {name}: {state}")
        refs = item.get("evidence_refs", [])
        if not isinstance(refs, list) or any(not isinstance(ref, str) or not ref for ref in refs):
            raise ValueError(f"evidence_refs for {name} must be a list of ids")
        if state in {"observed", "uncertain"} and not refs:
            raise ValueError(f"observed or uncertain dimension {name} requires evidence_refs")
        normalized_dimensions[name] = {
            **deepcopy(item),
            "state": state,
            "value": deepcopy(item.get("value")),
            "confidence": _confidence(item.get("confidence", 0.0)),
            "evidence_refs": list(refs),
        }
    node_type = str(node.get("node_type") or "")
    if node_type and node_type not in NODE_TYPES:
        raise ValueError(f"unsupported blueprint node type: {node_type}")
    stable_key = str(node.get("stable_key") or "").strip()
    if not stable_key:
        raise ValueError("blueprint node stable_key is required")
    normalized["stable_key"] = stable_key
    normalized["node_type"] = node_type or "scene"
    normalized["dimensions"] = normalized_dimensions
    return normalized


def empty_dimensions(*, confidence: float = 1.0) -> dict[str, dict[str, Any]]:
    score = _confidence(confidence)
    return {
        name: {
            "state": "not_observed",
            "value": None,
            "confidence": score,
            "evidence_refs": [],
        }
        for name in BLUEPRINT_DIMENSIONS
    }
