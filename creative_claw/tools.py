from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Callable, Literal

from .db import Database
from .indexer import Indexer
from .office import OfficeArtifactService
from .repository import Repository
from .retrieval import HybridRetriever


ToolRisk = Literal["read", "write", "external"]
ToolHandler = Callable[[str, dict[str, Any]], Any]


@dataclass(slots=True)
class ToolSpec:
    name: str
    description: str
    risk: ToolRisk
    handler: ToolHandler
    input_schema: dict[str, Any]

    def public_spec(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "risk": self.risk,
            "input_schema": self.input_schema,
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"Duplicate tool: {spec.name}")
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise KeyError(f"Unknown tool: {name}") from exc

    def list(self) -> list[dict[str, Any]]:
        return [tool.public_spec() for tool in self._tools.values()]

    def validate_call(self, name: str, args: dict[str, Any]) -> ToolSpec:
        tool = self.get(name)
        if not isinstance(args, dict):
            raise ValueError(f"Tool arguments for {name} must be an object")
        schema = tool.input_schema
        missing = [key for key in schema.get("required", []) if key not in args]
        if missing:
            raise ValueError(f"Tool {name} is missing required arguments: {', '.join(missing)}")
        expected_types = {
            "string": str,
            "integer": int,
            "number": (int, float),
            "boolean": bool,
            "array": list,
            "object": dict,
        }
        for key, value in args.items():
            expected_name = schema.get("properties", {}).get(key, {}).get("type")
            expected = expected_types.get(expected_name)
            if value is None and key not in schema.get("required", []):
                continue
            if expected and not isinstance(value, expected):
                raise ValueError(f"Tool {name} argument {key} must be {expected_name}")
        return tool


class CreativeToolset:
    def __init__(self, database: Database):
        self.database = database
        self.repository = Repository(database)
        self.indexer = Indexer(database)
        self.retriever = HybridRetriever(database)

    def build_registry(self) -> ToolRegistry:
        registry = ToolRegistry()
        registry.register(
            ToolSpec(
                "search_knowledge",
                "Hybrid search over indexed narrative sources with citations.",
                "read",
                self.search_knowledge,
                {"type": "object", "required": ["query"], "properties": {"query": {"type": "string"}, "top_k": {"type": "integer"}, "filters": {"type": "object"}}},
            )
        )
        registry.register(
            ToolSpec(
                "get_narrative_context",
                "Build grounded text, graph, timeline and OHLC context for a model call.",
                "read",
                self.get_narrative_context,
                {"type": "object", "required": ["query"], "properties": {"query": {"type": "string"}, "top_k": {"type": "integer"}, "filters": {"type": "object"}, "character_name": {"type": "string"}, "dimension": {"type": "string"}}},
            )
        )
        registry.register(ToolSpec("list_documents", "List indexed sources and versions.", "read", self.list_documents, {"type": "object", "properties": {}}))
        registry.register(ToolSpec("knowledge_stats", "Report index, embedding, graph, timeline, OHLC and ledger coverage.", "read", self.knowledge_stats, {"type": "object", "properties": {}}))
        registry.register(ToolSpec("verify_ledger", "Verify the append-only event hash chain.", "read", self.verify_ledger, {"type": "object", "properties": {}}))
        registry.register(
            ToolSpec(
                "import_document",
                "Import a supported file or directory into the knowledge base.",
                "write",
                self.import_document,
                {"type": "object", "required": ["path"], "properties": {"path": {"type": "string"}, "recursive": {"type": "boolean"}, "branch": {"type": "string"}, "canon_status": {"type": "string"}}},
            )
        )
        registry.register(
            ToolSpec(
                "delete_document",
                "Remove a source from the index without deleting the source file.",
                "write",
                self.delete_document,
                {"type": "object", "required": ["document_id"], "properties": {"document_id": {"type": "string"}}},
            )
        )
        registry.register(
            ToolSpec(
                "reindex_document",
                "Re-extract and re-embed a physical indexed source.",
                "write",
                self.reindex_document,
                {"type": "object", "required": ["document_id"], "properties": {"document_id": {"type": "string"}}},
            )
        )
        registry.register(
            ToolSpec(
                "backfill_embeddings",
                "Fill missing vectors or replace all project vectors with the configured provider.",
                "write",
                self.backfill_embeddings,
                {"type": "object", "properties": {"replace": {"type": "boolean"}, "batch_size": {"type": "integer"}}},
            )
        )
        registry.register(
            ToolSpec(
                "upsert_entity",
                "Create or update a narrative entity.",
                "write",
                self.upsert_entity,
                {"type": "object", "required": ["name", "entity_type"], "properties": {"name": {"type": "string"}, "entity_type": {"type": "string"}, "aliases": {"type": "array"}, "attrs": {"type": "object"}}},
            )
        )
        registry.register(
            ToolSpec(
                "add_relation",
                "Add an evidence-linked relation between narrative entities.",
                "write",
                self.add_relation,
                {"type": "object", "required": ["source_id", "predicate", "target_id"], "properties": {"source_id": {"type": "string"}, "predicate": {"type": "string"}, "target_id": {"type": "string"}, "evidence_chunk_id": {"type": "string"}, "valid_from": {"type": "string"}, "valid_to": {"type": "string"}, "branch": {"type": "string"}, "attrs": {"type": "object"}}},
            )
        )
        registry.register(
            ToolSpec(
                "add_timeline_event",
                "Add a structured story-time event with evidence.",
                "write",
                self.add_timeline_event,
                {"type": "object", "required": ["label", "description"], "properties": {"label": {"type": "string"}, "description": {"type": "string"}, "story_time": {"type": "string"}, "episode": {"type": "integer"}, "scene": {"type": "integer"}, "evidence_chunk_id": {"type": "string"}, "branch": {"type": "string"}, "attrs": {"type": "object"}}},
            )
        )
        registry.register(
            ToolSpec(
                "update_timeline_event",
                "Update a scene manuscript while preserving the previous and new text in the ledger.",
                "write",
                self.update_timeline_event,
                {"type": "object", "required": ["event_id", "description"], "properties": {"event_id": {"type": "string"}, "description": {"type": "string"}, "label": {"type": "string"}, "story_time": {"type": "string"}, "patches": {"type": "array"}}},
            )
        )
        registry.register(
            ToolSpec(
                "upsert_ohlc",
                "Write a typed character OHLC row. Higher periods are aggregated from child rows.",
                "write",
                self.upsert_ohlc,
                {"type": "object", "required": ["character_name", "dimension", "period_type", "period_id", "sort_key", "open", "high", "low", "close"], "properties": {"character_name": {"type": "string"}, "dimension": {"type": "string"}, "period_type": {"type": "string"}, "period_id": {"type": "string"}, "parent_period_id": {"type": "string"}, "sort_key": {"type": "number"}, "open": {"type": "number"}, "high": {"type": "number"}, "low": {"type": "number"}, "close": {"type": "number"}, "evidence_chunk_id": {"type": "string"}, "timeline_event_id": {"type": "string"}, "branch": {"type": "string"}, "attrs": {"type": "object"}}},
            )
        )
        registry.register(
            ToolSpec(
                "aggregate_ohlc",
                "Aggregate ordered scene OHLC rows into their parent period.",
                "read",
                self.aggregate_ohlc,
                {"type": "object", "required": ["character_name", "dimension", "parent_period_id"], "properties": {"character_name": {"type": "string"}, "dimension": {"type": "string"}, "parent_period_id": {"type": "string"}, "branch": {"type": "string"}}},
            )
        )
        registry.register(ToolSpec("export_word", "Create a Word document inside the project root.", "write", self.export_word, {"type": "object", "required": ["output_path", "title", "sections"], "properties": {"output_path": {"type": "string"}, "title": {"type": "string"}, "sections": {"type": "array"}}}))
        registry.register(ToolSpec("export_powerpoint", "Create a PowerPoint deck inside the project root.", "write", self.export_powerpoint, {"type": "object", "required": ["output_path", "title", "slides"], "properties": {"output_path": {"type": "string"}, "title": {"type": "string"}, "subtitle": {"type": "string"}, "slides": {"type": "array"}}}))
        registry.register(ToolSpec("export_excel", "Create an Excel workbook inside the project root.", "write", self.export_excel, {"type": "object", "required": ["output_path", "sheets"], "properties": {"output_path": {"type": "string"}, "sheets": {"type": "array"}}}))
        registry.register(ToolSpec("edit_word", "Replace text or append sections in an existing Word document.", "write", self.edit_word, {"type": "object", "required": ["path"], "properties": {"path": {"type": "string"}, "replacements": {"type": "object"}, "append_sections": {"type": "array"}}}))
        registry.register(ToolSpec("edit_powerpoint", "Replace text or append slides in an existing PowerPoint deck.", "write", self.edit_powerpoint, {"type": "object", "required": ["path"], "properties": {"path": {"type": "string"}, "replacements": {"type": "object"}, "append_slides": {"type": "array"}}}))
        registry.register(ToolSpec("edit_excel", "Set explicit cells or formulas in an existing Excel workbook.", "write", self.edit_excel, {"type": "object", "required": ["path", "edits"], "properties": {"path": {"type": "string"}, "edits": {"type": "array"}, "create_sheets": {"type": "boolean"}}}))
        return registry

    def search_knowledge(self, project_id: str, args: dict[str, Any]) -> dict[str, Any]:
        hits = self.retriever.search(project_id, args["query"], top_k=int(args.get("top_k", 8)), filters=args.get("filters"))
        return {"query": args["query"], "results": [hit.citation() for hit in hits]}

    def get_narrative_context(self, project_id: str, args: dict[str, Any]) -> dict[str, Any]:
        return self.retriever.build_context(
            project_id,
            args["query"],
            top_k=int(args.get("top_k", 8)),
            filters=args.get("filters"),
            character_name=args.get("character_name"),
            dimension=args.get("dimension", "知情度"),
        )

    def list_documents(self, project_id: str, args: dict[str, Any]) -> dict[str, Any]:
        return {"documents": self.repository.list_documents(project_id)}

    def knowledge_stats(self, project_id: str, args: dict[str, Any]) -> dict[str, Any]:
        return self.repository.knowledge_stats(project_id)

    def verify_ledger(self, project_id: str, args: dict[str, Any]) -> dict[str, Any]:
        return self.repository.ledger.verify(project_id)

    def import_document(self, project_id: str, args: dict[str, Any]) -> dict[str, Any]:
        results = self.indexer.import_path(
            project_id,
            args["path"],
            recursive=bool(args.get("recursive", True)),
            branch=args.get("branch", "main"),
            canon_status=args.get("canon_status", "reference"),
            actor=args.get("actor", "agent"),
        )
        return {"imports": [asdict(result) | {"path": str(result.path)} for result in results]}

    def delete_document(self, project_id: str, args: dict[str, Any]) -> dict[str, Any]:
        return self.indexer.delete_document(project_id, args["document_id"], actor="agent")

    def reindex_document(self, project_id: str, args: dict[str, Any]) -> dict[str, Any]:
        result = self.indexer.reindex_document(project_id, args["document_id"], actor="agent")
        return asdict(result) | {"path": str(result.path)}

    def backfill_embeddings(self, project_id: str, args: dict[str, Any]) -> dict[str, Any]:
        return self.indexer.backfill_embeddings(
            project_id,
            replace=bool(args.get("replace", False)),
            batch_size=int(args.get("batch_size", 64)),
            actor="agent",
        )

    def upsert_entity(self, project_id: str, args: dict[str, Any]) -> dict[str, Any]:
        return self.repository.upsert_entity(project_id, actor="agent", **args)

    def add_relation(self, project_id: str, args: dict[str, Any]) -> dict[str, Any]:
        return self.repository.add_relation(project_id, actor="agent", **args)

    def add_timeline_event(self, project_id: str, args: dict[str, Any]) -> dict[str, Any]:
        return self.repository.add_timeline_event(project_id, actor="agent", **args)

    def update_timeline_event(self, project_id: str, args: dict[str, Any]) -> dict[str, Any]:
        return self.repository.update_timeline_event(project_id, actor="agent", **args)

    def upsert_ohlc(self, project_id: str, args: dict[str, Any]) -> dict[str, Any]:
        mapped = dict(args)
        mapped["open_value"] = mapped.pop("open")
        return self.repository.upsert_ohlc(project_id, actor="agent", **mapped)

    def aggregate_ohlc(self, project_id: str, args: dict[str, Any]) -> dict[str, Any]:
        return self.repository.aggregate_ohlc(project_id, **args)

    def _office(self, project_id: str) -> OfficeArtifactService:
        project = self.repository.get_project(project_id)
        return OfficeArtifactService(project["root_path"])

    def export_word(self, project_id: str, args: dict[str, Any]) -> dict[str, Any]:
        result = self._office(project_id).export_word(**args)
        self.repository.ledger.append(project_id, "office.word.exported", result, "agent")
        return result

    def export_powerpoint(self, project_id: str, args: dict[str, Any]) -> dict[str, Any]:
        result = self._office(project_id).export_powerpoint(**args)
        self.repository.ledger.append(project_id, "office.powerpoint.exported", result, "agent")
        return result

    def export_excel(self, project_id: str, args: dict[str, Any]) -> dict[str, Any]:
        result = self._office(project_id).export_excel(**args)
        self.repository.ledger.append(project_id, "office.excel.exported", result, "agent")
        return result

    def edit_word(self, project_id: str, args: dict[str, Any]) -> dict[str, Any]:
        result = self._office(project_id).edit_word(**args)
        self.repository.ledger.append(project_id, "office.word.edited", result, "agent")
        return result

    def edit_powerpoint(self, project_id: str, args: dict[str, Any]) -> dict[str, Any]:
        result = self._office(project_id).edit_powerpoint(**args)
        self.repository.ledger.append(project_id, "office.powerpoint.edited", result, "agent")
        return result

    def edit_excel(self, project_id: str, args: dict[str, Any]) -> dict[str, Any]:
        result = self._office(project_id).edit_excel(**args)
        self.repository.ledger.append(project_id, "office.excel.edited", result, "agent")
        return result
