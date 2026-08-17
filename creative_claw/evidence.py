from __future__ import annotations

import re
from typing import Any

PREFIXES = {
    "source": "S",
    "graph": "G",
    "timeline": "T",
    "kline": "K",
    "version": "V",
    "rule": "R",
    "issue": "I",
}


def _ref(kind: str, index: int) -> str:
    return f"{PREFIXES[kind]}{index}"


def _generic_ref(kind: str, index: int, row: dict[str, Any]) -> dict[str, Any]:
    title = str(row.get("title") or row.get("name") or row.get("label") or row.get("id") or kind)
    summary = str(row.get("summary") or row.get("description") or row.get("text") or title)
    return {
        "ref": _ref(kind, index),
        "kind": kind,
        "title": title,
        "summary": summary,
        "locator": {"id": row.get("id")},
        "payload": row,
    }


def build_evidence_refs(
    *,
    sources: list[dict[str, Any]],
    graph: dict[str, Any],
    timeline: list[dict[str, Any]],
    ohlc: list[dict[str, Any]],
    versions: list[dict[str, Any]] | None = None,
    rules: list[dict[str, Any]] | None = None,
    issues: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for index, row in enumerate(sources, start=1):
        refs.append(
            {
                "ref": _ref("source", index),
                "kind": "source",
                "title": str(row.get("title") or row.get("path") or row.get("document_id") or "来源"),
                "summary": str(row.get("snippet") or row.get("text") or ""),
                "locator": {
                    "document_id": row.get("document_id"),
                    "chunk_id": row.get("chunk_id"),
                    **dict(row.get("locator") or {}),
                },
                "payload": row,
            }
        )

    graph_index = 0
    for row in graph.get("entities", []):
        graph_index += 1
        refs.append(
            {
                "ref": _ref("graph", graph_index),
                "kind": "graph",
                "title": str(row.get("name") or row.get("id") or "实体"),
                "summary": f"{row.get('name', '')}（{row.get('entity_type', 'entity')}）",
                "locator": {"entity_id": row.get("id")},
                "payload": row,
            }
        )
    for row in graph.get("relations", []):
        graph_index += 1
        source = row.get("source_name") or row.get("source_id") or ""
        target = row.get("target_name") or row.get("target_id") or ""
        predicate = row.get("predicate") or "关联"
        refs.append(
            {
                "ref": _ref("graph", graph_index),
                "kind": "graph",
                "title": f"{source} {predicate} {target}".strip(),
                "summary": f"{source} {predicate} {target}".strip(),
                "locator": {
                    "relation_id": row.get("id"),
                    "source_id": row.get("source_id"),
                    "target_id": row.get("target_id"),
                },
                "payload": row,
            }
        )

    for index, row in enumerate(timeline, start=1):
        refs.append(
            {
                "ref": _ref("timeline", index),
                "kind": "timeline",
                "title": str(row.get("label") or row.get("id") or "时间线事件"),
                "summary": str(row.get("description") or ""),
                "locator": {
                    "event_id": row.get("id"),
                    "episode": row.get("episode"),
                    "scene": row.get("scene"),
                    "story_time": row.get("story_time"),
                },
                "payload": row,
            }
        )

    for index, row in enumerate(ohlc, start=1):
        refs.append(
            {
                "ref": _ref("kline", index),
                "kind": "kline",
                "title": f"{row.get('character_name', '')} / {row.get('dimension', '')} / {row.get('period_id', '')}".strip(" /"),
                "summary": (
                    f"open={row.get('open')}, high={row.get('high')}, "
                    f"low={row.get('low')}, close={row.get('close')}；"
                    "open/close 为周期起止状态，high/low 为区间极值，不表示事件先后。"
                ),
                "locator": {
                    "ohlc_id": row.get("id"),
                    "timeline_event_id": row.get("timeline_event_id"),
                    "period_id": row.get("period_id"),
                    "character_name": row.get("character_name"),
                    "dimension": row.get("dimension"),
                },
                "payload": row,
            }
        )

    for kind, rows in (("version", versions or []), ("rule", rules or []), ("issue", issues or [])):
        refs.extend(_generic_ref(kind, index, row) for index, row in enumerate(rows, start=1))
    return refs


def validate_citations(text: str, evidence_refs: list[dict[str, Any]]) -> dict[str, Any]:
    used: list[str] = []
    for match in re.finditer(r"\[([SGTKVRI]\d+)\]", str(text or "")):
        ref = match.group(1)
        if ref not in used:
            used.append(ref)
    known = [str(row["ref"]) for row in evidence_refs]
    unknown = [ref for ref in used if ref not in known]
    return {
        "valid": not unknown,
        "used": used,
        "unknown": unknown,
        "unused": [ref for ref in known if ref not in used],
    }
