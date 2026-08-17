from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .api import create_app
from .db import Database
from .indexer import Indexer
from .llm import OpenAICompatiblePlanner
from .repository import Repository
from .retrieval import HybridRetriever
from .runtime import AgentRuntime
from .tools import CreativeToolset


def _json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def _load_json(value: str) -> Any:
    candidate = Path(value)
    if candidate.is_file():
        return json.loads(candidate.read_text(encoding="utf-8"))
    return json.loads(value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="creative-claw")
    parser.add_argument("--db", default=".creative-claw/knowledge.db", help="SQLite database path")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init", help="Initialize storage and create a project")
    init.add_argument("--name", required=True)
    init.add_argument("--root", default=".")
    init.add_argument("--id")

    subparsers.add_parser("projects", help="List projects")

    documents = subparsers.add_parser("documents", help="List indexed documents")
    documents.add_argument("--project", required=True)

    stats = subparsers.add_parser("stats", help="Show knowledge-base coverage and integrity")
    stats.add_argument("--project", required=True)

    import_parser = subparsers.add_parser("import", help="Import a file or directory")
    import_parser.add_argument("--project", required=True)
    import_parser.add_argument("path")
    import_parser.add_argument("--no-recursive", action="store_true")
    import_parser.add_argument("--branch", default="main")
    import_parser.add_argument("--canon-status", default="reference")

    delete = subparsers.add_parser("delete-document", help="Delete a document from the index only")
    delete.add_argument("--project", required=True)
    delete.add_argument("document_id")

    reindex = subparsers.add_parser("reindex", help="Re-extract a physical indexed document")
    reindex.add_argument("--project", required=True)
    reindex.add_argument("document_id")

    embeddings = subparsers.add_parser("embeddings", help="Backfill or replace stored embeddings")
    embeddings.add_argument("--project", required=True)
    embeddings.add_argument("--replace", action="store_true")
    embeddings.add_argument("--batch-size", type=int, default=64)

    search = subparsers.add_parser("search", help="Search the knowledge base")
    search.add_argument("--project", required=True)
    search.add_argument("query")
    search.add_argument("--top-k", type=int, default=8)
    search.add_argument("--episode", type=int)
    search.add_argument("--branch", default="main")
    search.add_argument("--context", action="store_true")
    search.add_argument("--character")
    search.add_argument("--dimension", default="知情度")

    entity = subparsers.add_parser("entity", help="Create a narrative entity")
    entity.add_argument("--project", required=True)
    entity.add_argument("name")
    entity.add_argument("entity_type")
    entity.add_argument("--aliases", default="[]")
    entity.add_argument("--attrs", default="{}")

    relation = subparsers.add_parser("relation", help="Create a narrative relation")
    relation.add_argument("--project", required=True)
    relation.add_argument("source_id")
    relation.add_argument("predicate")
    relation.add_argument("target_id")
    relation.add_argument("--evidence-chunk-id")
    relation.add_argument("--valid-from")
    relation.add_argument("--valid-to")
    relation.add_argument("--branch", default="main")

    ohlc = subparsers.add_parser("ohlc", help="Write or aggregate an OHLC row")
    ohlc.add_argument("--project", required=True)
    ohlc.add_argument("--aggregate", action="store_true")
    ohlc.add_argument("--character", required=True)
    ohlc.add_argument("--dimension", default="知情度")
    ohlc.add_argument("--period-type", default="scene")
    ohlc.add_argument("--period-id", required=True)
    ohlc.add_argument("--parent-period-id")
    ohlc.add_argument("--sort-key", type=float, default=0)
    ohlc.add_argument("--open", type=float)
    ohlc.add_argument("--high", type=float)
    ohlc.add_argument("--low", type=float)
    ohlc.add_argument("--close", type=float)
    ohlc.add_argument("--branch", default="main")

    ledger = subparsers.add_parser("ledger", help="Verify ledger integrity")
    ledger.add_argument("--project", required=True)

    subparsers.add_parser("tools", help="List agent tools")

    task = subparsers.add_parser("task", help="Create or advance an agent task")
    task.add_argument("action", choices=["create", "get", "step", "run", "auto", "reject"])
    task.add_argument("--project")
    task.add_argument("--goal")
    task.add_argument("--plan")
    task.add_argument("--task-id")
    task.add_argument("--approve", action="store_true")
    task.add_argument("--reason", default="Rejected by user")

    serve = subparsers.add_parser("serve", help="Run the local JSON API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--debug", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    database = Database(args.db)
    database.initialize()
    repository = Repository(database)
    indexer = Indexer(database)
    retriever = HybridRetriever(database)
    registry = CreativeToolset(database).build_registry()
    runtime = AgentRuntime(database, registry)

    if args.command == "init":
        _json(repository.create_project(args.name, args.root, args.id))
    elif args.command == "projects":
        _json(repository.list_projects())
    elif args.command == "documents":
        _json(repository.list_documents(args.project))
    elif args.command == "stats":
        _json(repository.knowledge_stats(args.project))
    elif args.command == "import":
        results = indexer.import_path(
            args.project,
            args.path,
            recursive=not args.no_recursive,
            branch=args.branch,
            canon_status=args.canon_status,
        )
        _json([{**asdict(item), "path": str(item.path)} for item in results])
    elif args.command == "delete-document":
        _json(indexer.delete_document(args.project, args.document_id))
    elif args.command == "reindex":
        result = indexer.reindex_document(args.project, args.document_id)
        _json({**asdict(result), "path": str(result.path)})
    elif args.command == "embeddings":
        _json(indexer.backfill_embeddings(args.project, replace=args.replace, batch_size=args.batch_size))
    elif args.command == "search":
        filters = {"branch": args.branch}
        if args.episode is not None:
            filters["episode"] = args.episode
        if args.context:
            _json(
                retriever.build_context(
                    args.project,
                    args.query,
                    top_k=args.top_k,
                    filters=filters,
                    character_name=args.character,
                    dimension=args.dimension,
                )
            )
        else:
            _json([hit.citation() for hit in retriever.search(args.project, args.query, top_k=args.top_k, filters=filters)])
    elif args.command == "entity":
        _json(repository.upsert_entity(args.project, args.name, args.entity_type, aliases=_load_json(args.aliases), attrs=_load_json(args.attrs)))
    elif args.command == "relation":
        _json(
            repository.add_relation(
                args.project,
                args.source_id,
                args.predicate,
                args.target_id,
                evidence_chunk_id=args.evidence_chunk_id,
                valid_from=args.valid_from,
                valid_to=args.valid_to,
                branch=args.branch,
            )
        )
    elif args.command == "ohlc":
        if args.aggregate:
            _json(repository.aggregate_ohlc(args.project, args.character, args.dimension, args.period_id, branch=args.branch))
        else:
            missing = [name for name in ("open", "high", "low", "close") if getattr(args, name) is None]
            if missing:
                raise SystemExit("Missing OHLC values: " + ", ".join(missing))
            _json(
                repository.upsert_ohlc(
                    args.project,
                    args.character,
                    args.dimension,
                    args.period_type,
                    args.period_id,
                    args.sort_key,
                    args.open,
                    args.high,
                    args.low,
                    args.close,
                    parent_period_id=args.parent_period_id,
                    branch=args.branch,
                )
            )
    elif args.command == "ledger":
        _json(repository.ledger.verify(args.project))
    elif args.command == "tools":
        _json(registry.list())
    elif args.command == "task":
        if args.action in {"create", "auto"}:
            if not args.project or not args.goal:
                raise SystemExit("task create/auto requires --project and --goal")
            plan = OpenAICompatiblePlanner.from_env().plan(args.goal, registry) if args.action == "auto" else _load_json(args.plan or "[]")
            _json(runtime.create_task(args.project, args.goal, plan))
        elif args.action == "get":
            _json(runtime.get_task(args.task_id))
        elif args.action == "step":
            _json(runtime.step(args.task_id, approve=args.approve))
        elif args.action == "run":
            _json(runtime.run_until_blocked(args.task_id))
        elif args.action == "reject":
            _json(runtime.reject(args.task_id, reason=args.reason))
    elif args.command == "serve":
        create_app(args.db).run(host=args.host, port=args.port, debug=args.debug)
    return 0


if __name__ == "__main__":
    sys.exit(main())
