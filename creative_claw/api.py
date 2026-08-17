from __future__ import annotations

from dataclasses import asdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import re
from typing import Any

import requests
from flask import Flask, jsonify, request, send_from_directory
from werkzeug.exceptions import BadRequest

from .context import ContextScope
from .cold_start import ColdStartConflictError, ColdStartService
from .blueprint_agents import (
    AgentRegistry as BlueprintAgentRegistry,
    MIGRATION_AGENT_DAG,
    OpenAICompatibleBlueprintAgent,
    REFERENCE_AGENT_DAG,
)
from .blueprint_service import BlueprintService
from .db import Database
from .evidence import validate_citations
from .indexer import Indexer
from .llm import (
    OpenAICompatibleColdStartWriter,
    OpenAICompatiblePlanner,
    OpenAICompatibleWriter,
    configure_runtime_model,
    load_persisted_runtime_model,
    public_model_config,
    runtime_model_config_path,
)
from .repository import Repository
from .retrieval import HybridRetriever
from .runtime import AgentRuntime
from .tools import CreativeToolset
from .util import new_id, safe_output_path
from .workflow import VersionConflictError, WorkflowService


def create_app(
    database_path: str | Path,
    *,
    blueprint_registry: BlueprintAgentRegistry | None = None,
    run_blueprint_jobs_inline: bool = False,
) -> Flask:
    database = Database(database_path)
    database.initialize()
    model_config_path = runtime_model_config_path(database.path)
    load_persisted_runtime_model(model_config_path)
    repository = Repository(database)
    indexer = Indexer(database)
    retriever = HybridRetriever(database)
    registry = CreativeToolset(database).build_registry()
    runtime = AgentRuntime(database, registry)
    workflow_service = WorkflowService(database)
    cold_start_service = ColdStartService(database)
    runtime_managed_blueprint_registry = blueprint_registry is None
    if blueprint_registry is None:
        blueprint_registry = BlueprintAgentRegistry()

    def register_runtime_blueprint_agents() -> None:
        for name in (
            *REFERENCE_AGENT_DAG,
            "target_setting_agent",
            *MIGRATION_AGENT_DAG,
            "unit_planner_agent",
            "draft_writer_agent",
            "continuity_review_agent",
            "similarity_safety_agent",
        ):
            if blueprint_registry.get(name) is None:
                blueprint_registry.register(OpenAICompatibleBlueprintAgent(name))

    if runtime_managed_blueprint_registry and public_model_config()["configured"]:
        register_runtime_blueprint_agents()
    blueprint_service = BlueprintService(database, blueprint_registry)
    blueprint_executor = None if run_blueprint_jobs_inline else ThreadPoolExecutor(
        max_workers=2, thread_name_prefix="creative-claw-blueprint"
    )
    web_root = Path(__file__).resolve().parent / "web"

    app = Flask(__name__)
    app.config["JSON_AS_ASCII"] = False
    app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024
    app.json.ensure_ascii = False
    app.extensions["blueprint_executor"] = blueprint_executor

    def public_blueprint_job(job: dict[str, Any]) -> dict[str, Any]:
        raw_input = dict(job.get("input") or {})
        input_allowlist = {
            "title", "source_hash", "text_hash", "source_length", "character_count",
            "reference_blueprint_id", "reference_version_id", "target_setting_id",
            "target_setting_version_id", "target_blueprint_id", "target_version_id",
            "unit_id", "artifact_id",
        }
        safe_input = {key: raw_input[key] for key in input_allowlist if key in raw_input}
        if "text" in raw_input:
            safe_input["text_hash"] = raw_input.get("source_hash")
            safe_input["character_count"] = len(str(raw_input["text"]))
        raw_error = dict(job.get("error") or {})
        error_allowlist = {"category", "batch_id", "missing_agents", "code"}
        safe_error = {key: raw_error[key] for key in error_allowlist if key in raw_error}
        return {
            "id": job["id"], "project_id": job["project_id"], "job_type": job["job_type"],
            "status": job["status"], "desired_state": job["desired_state"], "input": safe_input,
            "rights_basis": job.get("rights_basis"), "source_document_id": job.get("source_document_id"),
            "source_version_id": job.get("source_version_id"),
            "output_artifact_id": job.get("output_artifact_id"),
            "progress": dict(job.get("progress") or {}), "error": safe_error,
            "created_at": job["created_at"], "updated_at": job["updated_at"],
        }

    def submit_blueprint_job(project_id: str, job_id: str) -> None:
        if blueprint_executor is None:
            return

        def execute() -> None:
            worker_database = Database(database.path)
            worker_service = BlueprintService(worker_database, blueprint_registry)
            worker_service.execute_job(project_id, job_id)

        blueprint_executor.submit(execute)

    def json_object() -> dict[str, Any]:
        payload = request.get_json(force=True)
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        return payload

    @app.errorhandler(requests.RequestException)
    def upstream_error(error: requests.RequestException):  # noqa: ANN202
        response = getattr(error, "response", None)
        if response is not None:
            status = response.status_code
            messages = {
                400: ("模型拒绝了请求", "请检查模型名称、请求格式和区域端点。"),
                401: ("模型认证失败", "请检查 API Key 是否有效，以及账号是否对应当前区域端点。"),
                403: ("模型访问被拒绝", "当前 API Key 没有该模型的访问权限。"),
                404: ("模型或接口不存在", "请检查模型名称和 Base URL。"),
                429: ("模型调用过于频繁", "请稍后重试，或检查账号额度。"),
            }
            title, advice = messages.get(status, ("模型服务返回错误", "请稍后重试。"))
            return jsonify({"error": title, "detail": f"HTTP {status}。{advice}", "upstream_status": status}), 502
        if isinstance(error, requests.Timeout):
            detail = "连接模型服务超时，请检查网络或稍后重试。"
        elif isinstance(error, requests.ConnectionError):
            detail = "无法连接模型服务，请检查 Base URL、网络和代理设置。"
        else:
            detail = "模型服务请求未完成，请检查网络后重试。"
        return jsonify({"error": "模型服务请求失败", "detail": detail}), 502

    @app.errorhandler(BadRequest)
    def bad_request(error: BadRequest):  # noqa: ANN202
        return jsonify({"error": "Invalid JSON body", "detail": error.description}), 400

    @app.errorhandler(KeyError)
    def key_error(error: KeyError):  # noqa: ANN202
        return jsonify({"error": str(error)}), 404

    @app.errorhandler(VersionConflictError)
    def version_conflict(error: VersionConflictError):  # noqa: ANN202
        return jsonify({"error": str(error)}), 409

    @app.errorhandler(ColdStartConflictError)
    def cold_start_conflict(error: ColdStartConflictError):  # noqa: ANN202
        return jsonify({"error": str(error)}), 409

    @app.errorhandler(ValueError)
    def value_error(error: ValueError):  # noqa: ANN202
        return jsonify({"error": str(error)}), 400

    @app.errorhandler(FileNotFoundError)
    def file_error(error: FileNotFoundError):  # noqa: ANN202
        return jsonify({"error": str(error)}), 404

    @app.get("/health")
    def health():  # noqa: ANN202
        return jsonify({"status": "ok", "service": "creative-claw"})

    @app.get("/")
    def canvas_app():  # noqa: ANN202
        return send_from_directory(web_root, "index.html")

    @app.get("/assets/<path:filename>")
    def canvas_asset(filename: str):  # noqa: ANN202
        return send_from_directory(web_root, filename)

    @app.get("/v1/config")
    def configuration():  # noqa: ANN202
        return jsonify({"llm": public_model_config(), "database": str(database.path)})

    @app.post("/v1/config/llm")
    def configure_llm():  # noqa: ANN202
        payload: dict[str, Any] = request.get_json(force=True)
        llm = configure_runtime_model(
            api_key=str(payload.get("api_key") or ""),
            base_url=str(payload.get("base_url") or ""),
            model=str(payload.get("model") or ""),
            persist_path=model_config_path,
        )
        if runtime_managed_blueprint_registry:
            register_runtime_blueprint_agents()
        return jsonify({"llm": llm, "message": "模型配置已明文保存到当前数据库同目录的 *.llm.json"})

    @app.get("/v1/tools")
    def tools():  # noqa: ANN202
        return jsonify({"tools": registry.list()})

    @app.get("/v1/workflow-templates")
    def workflow_templates():  # noqa: ANN202
        return jsonify({"templates": workflow_service.list_templates()})

    @app.get("/v1/projects")
    def list_projects():  # noqa: ANN202
        return jsonify({"projects": repository.list_projects()})

    @app.post("/v1/projects")
    def create_project():  # noqa: ANN202
        payload: dict[str, Any] = request.get_json(force=True)
        name = str(payload.get("name") or "").strip()
        if not name:
            raise ValueError("Project name is required")
        project_id = str(payload.get("id") or new_id("prj"))
        requested_root = str(payload.get("root_path") or "").strip()
        root_path = Path(requested_root).expanduser() if requested_root else database.path.parent / "projects" / project_id
        root_path.mkdir(parents=True, exist_ok=True)
        project = repository.create_project(name, root_path, project_id)
        return jsonify(project), 201

    @app.patch("/v1/projects/<project_id>")
    def update_project(project_id: str):  # noqa: ANN202
        payload: dict[str, Any] = request.get_json(force=True)
        return jsonify(repository.update_project(project_id, name=str(payload.get("name") or "")))

    @app.post("/v1/projects/<project_id>/cold-start/preview")
    def cold_start_preview(project_id: str):  # noqa: ANN202
        payload = json_object()
        if not cold_start_service.is_empty(project_id):
            raise ColdStartConflictError("冷启动仅适用于空项目，请先新建项目")
        writer = OpenAICompatibleColdStartWriter.from_env()
        return jsonify(
            cold_start_service.preview(
                project_id,
                str(payload.get("prompt") or ""),
                writer,
            )
        )

    @app.post("/v1/projects/<project_id>/cold-start/apply")
    def cold_start_apply(project_id: str):  # noqa: ANN202
        payload = json_object()
        result = cold_start_service.apply(
            project_id,
            payload.get("preview"),
            payload.get("generation"),
        )
        return jsonify(result), 201

    @app.post("/v1/projects/<project_id>/workflow")
    def instantiate_workflow(project_id: str):  # noqa: ANN202
        payload = json_object()
        workflow = workflow_service.instantiate_workflow(
            project_id,
            str(payload.get("template_key") or ""),
            version=(int(payload["version"]) if payload.get("version") is not None else None),
            name=(str(payload["name"]) if payload.get("name") is not None else None),
        )
        return jsonify(workflow), 201

    @app.get("/v1/projects/<project_id>/workflow")
    def get_workflow(project_id: str):  # noqa: ANN202
        return jsonify(workflow_service.get_project_workflow(project_id))

    @app.post("/v1/projects/<project_id>/production-units")
    def create_production_unit(project_id: str):  # noqa: ANN202
        payload = json_object()
        unit = workflow_service.create_production_unit(
            project_id,
            str(payload.get("unit_type") or ""),
            str(payload.get("title") or ""),
            parent_id=(str(payload["parent_id"]) if payload.get("parent_id") else None),
            position=int(payload.get("position", 0)),
            branch=str(payload.get("branch") or "main"),
            attrs=payload.get("attrs"),
        )
        return jsonify(unit), 201

    @app.post(
        "/v1/projects/<project_id>/workflow-stages/<stage_id>/transition"
    )
    def transition_workflow_stage(project_id: str, stage_id: str):  # noqa: ANN202
        payload = json_object()
        return jsonify(
            workflow_service.transition_stage(
                project_id,
                stage_id,
                str(payload.get("status") or ""),
                exception_reason=(
                    str(payload["exception_reason"])
                    if payload.get("exception_reason") is not None
                    else None
                ),
                actor="api",
            )
        )

    @app.post("/v1/projects/<project_id>/artifacts")
    def create_artifact(project_id: str):  # noqa: ANN202
        payload = json_object()
        artifact = workflow_service.create_artifact(
            project_id,
            str(payload.get("artifact_type") or ""),
            str(payload.get("title") or ""),
            stage_id=(str(payload["stage_id"]) if payload.get("stage_id") else None),
            unit_id=(str(payload["unit_id"]) if payload.get("unit_id") else None),
            branch=str(payload.get("branch") or "main"),
            attrs=payload.get("attrs"),
            actor="api",
        )
        return jsonify(artifact), 201

    @app.get("/v1/projects/<project_id>/artifacts/<artifact_id>")
    def get_artifact(project_id: str, artifact_id: str):  # noqa: ANN202
        return jsonify(workflow_service.get_artifact(project_id, artifact_id))

    @app.post("/v1/projects/<project_id>/artifacts/<artifact_id>/transition")
    def transition_artifact(project_id: str, artifact_id: str):  # noqa: ANN202
        payload = json_object()
        return jsonify(
            workflow_service.transition_artifact_status(
                project_id,
                artifact_id,
                str(payload.get("status") or ""),
                actor="api",
            )
        )

    @app.post("/v1/projects/<project_id>/artifacts/<artifact_id>/versions")
    def save_artifact_version(project_id: str, artifact_id: str):  # noqa: ANN202
        payload = json_object()
        result = workflow_service.save_artifact_version(
            project_id,
            artifact_id,
            str(payload.get("content") or ""),
            expected_current_version_id=(
                str(payload["expected_current_version_id"])
                if payload.get("expected_current_version_id") is not None
                else None
            ),
            change_summary=str(payload.get("change_summary") or ""),
            source_kind=str(payload.get("source_kind") or "user"),
            actor="api",
            metadata=payload.get("metadata"),
        )
        return jsonify(result), 201

    @app.get("/v1/projects/<project_id>/artifacts/<artifact_id>/versions")
    def list_artifact_versions(project_id: str, artifact_id: str):  # noqa: ANN202
        return jsonify(
            {
                "versions": workflow_service.list_artifact_versions(
                    project_id, artifact_id
                )
            }
        )

    @app.post("/v1/projects/<project_id>/artifact-dependencies")
    def create_artifact_dependency(project_id: str):  # noqa: ANN202
        payload = json_object()
        dependency = workflow_service.add_dependency(
            project_id,
            str(payload.get("upstream_artifact_id") or ""),
            str(payload.get("downstream_artifact_id") or ""),
            str(payload.get("dependency_type") or ""),
            actor="api",
        )
        return jsonify(dependency), 201

    @app.post("/v1/projects/<project_id>/reviews")
    def create_review(project_id: str):  # noqa: ANN202
        payload = json_object()
        review = workflow_service.create_review(
            project_id,
            str(payload.get("artifact_id") or ""),
            str(payload.get("review_type") or ""),
            str(payload.get("input_version_id") or ""),
            summary=str(payload.get("summary") or ""),
            actor="api",
            metadata=payload.get("metadata"),
        )
        return jsonify(review), 201

    @app.get("/v1/projects/<project_id>/impacts")
    def list_impacts(project_id: str):  # noqa: ANN202
        return jsonify(
            {
                "impacts": workflow_service.list_impacts(
                    project_id, status=request.args.get("status")
                )
            }
        )

    @app.get("/v1/projects/<project_id>/documents")
    def list_documents(project_id: str):  # noqa: ANN202
        return jsonify({"documents": repository.list_documents(project_id)})

    @app.get("/v1/projects/<project_id>/stats")
    def knowledge_stats(project_id: str):  # noqa: ANN202
        return jsonify(repository.knowledge_stats(project_id))

    @app.post("/v1/projects/<project_id>/blueprint-jobs/reference")
    def create_reference_blueprint_job(project_id: str):  # noqa: ANN202
        payload = json_object()
        requested_async = payload.get("run_async")
        job = blueprint_service.create_reference_job(
            project_id,
            title=str(payload.get("title") or ""),
            text=str(payload.get("text") or ""),
            rights_basis=str(payload.get("rights_basis") or ""),
            run_async=(bool(requested_async) if requested_async is not None else None),
        )
        if job["status"] != "completed" and blueprint_executor is not None:
            submit_blueprint_job(project_id, job["id"])
        status = 201 if job["status"] == "completed" else 202
        return jsonify(public_blueprint_job(job)), status

    @app.get("/v1/projects/<project_id>/blueprint-jobs/<job_id>")
    def get_blueprint_job(project_id: str, job_id: str):  # noqa: ANN202
        return jsonify(public_blueprint_job(blueprint_service.get_job(project_id, job_id)))

    @app.post("/v1/projects/<project_id>/blueprint-jobs/<job_id>/pause")
    def pause_blueprint_job(project_id: str, job_id: str):  # noqa: ANN202
        json_object()
        return jsonify(public_blueprint_job(blueprint_service.pause_job(project_id, job_id)))

    @app.post("/v1/projects/<project_id>/blueprint-jobs/<job_id>/resume")
    def resume_blueprint_job(project_id: str, job_id: str):  # noqa: ANN202
        json_object()
        if blueprint_executor is None:
            result = blueprint_service.resume_job(project_id, job_id)
            return jsonify(public_blueprint_job(result))
        blueprint_service.repository.set_job_desired_state(project_id, job_id, "running")
        result = blueprint_service.repository.update_job(project_id, job_id, status="resumable")
        submit_blueprint_job(project_id, job_id)
        return jsonify(public_blueprint_job(result)), 202

    @app.post("/v1/projects/<project_id>/blueprint-jobs/<job_id>/cancel")
    def cancel_blueprint_job(project_id: str, job_id: str):  # noqa: ANN202
        json_object()
        return jsonify(public_blueprint_job(blueprint_service.cancel_job(project_id, job_id)))

    def blueprint_response(project_id: str, artifact_id: str):  # noqa: ANN202
        include_evidence = request.args.get("include_evidence") in {"1", "true", "yes"}
        include_quotes = request.args.get("include_quotes") in {"1", "true", "yes"}
        result = blueprint_service.get_blueprint(
            project_id, artifact_id, include_quotes=include_quotes
        )
        if not include_evidence:
            result["evidence"] = []
        return jsonify(result)

    @app.get("/v1/projects/<project_id>/reference-blueprints/<artifact_id>")
    def get_reference_blueprint(project_id: str, artifact_id: str):  # noqa: ANN202
        return blueprint_response(project_id, artifact_id)

    @app.post("/v1/projects/<project_id>/reference-blueprints/manual")
    def create_manual_reference_blueprint(project_id: str):  # noqa: ANN202
        payload = json_object()
        return jsonify(blueprint_service.create_manual_reference_blueprint(
            project_id, title=str(payload.get("title") or ""), nodes=list(payload.get("nodes") or [])
        )), 201

    @app.post("/v1/projects/<project_id>/reference-blueprints/<artifact_id>/versions")
    def save_reference_blueprint_version(project_id: str, artifact_id: str):  # noqa: ANN202
        payload = json_object()
        result = blueprint_service.save_blueprint_version(
            project_id,
            artifact_id,
            list(payload.get("nodes") or []),
            expected_current_version_id=(
                str(payload["expected_current_version_id"])
                if payload.get("expected_current_version_id") is not None
                else None
            ),
            change_summary=str(payload.get("change_summary") or ""),
            interpretation_decisions=dict(payload.get("interpretation_decisions") or {}),
            conflict_resolutions=dict(payload.get("conflict_resolutions") or {}),
        )
        return jsonify(result), 201

    @app.post("/v1/projects/<project_id>/target-settings")
    def create_blueprint_target_setting(project_id: str):  # noqa: ANN202
        payload = json_object()
        result = blueprint_service.create_target_setting(
            project_id, str(payload.get("text") or ""), overrides=payload.get("overrides")
        )
        return jsonify(result), 201

    @app.post("/v1/projects/<project_id>/target-settings/<artifact_id>/confirm")
    def confirm_blueprint_target_setting(project_id: str, artifact_id: str):  # noqa: ANN202
        payload = json_object()
        return jsonify(blueprint_service.confirm_target_setting(
            project_id, artifact_id,
            expected_current_version_id=str(payload.get("expected_current_version_id") or ""),
            structured=dict(payload.get("structured") or {}),
        ))

    @app.post("/v1/projects/<project_id>/blueprint-jobs/migration")
    def create_blueprint_migration(project_id: str):  # noqa: ANN202
        payload = json_object()
        result = blueprint_service.create_migration_job(
            project_id,
            str(payload.get("reference_blueprint_id") or ""),
            str(payload.get("target_setting_id") or ""),
        )
        return jsonify(public_blueprint_job(result)), 201

    @app.get("/v1/projects/<project_id>/target-blueprints/<artifact_id>")
    def get_target_blueprint(project_id: str, artifact_id: str):  # noqa: ANN202
        return blueprint_response(project_id, artifact_id)

    @app.post("/v1/projects/<project_id>/target-blueprints/manual")
    def create_manual_target_blueprint(project_id: str):  # noqa: ANN202
        payload = json_object()
        return jsonify(blueprint_service.create_manual_target_blueprint(
            project_id, title=str(payload.get("title") or ""), nodes=list(payload.get("nodes") or []),
            target_setting_id=str(payload.get("target_setting_id") or ""),
            reference_blueprint_id=(str(payload["reference_blueprint_id"])
                                    if payload.get("reference_blueprint_id") else None),
        )), 201

    @app.post("/v1/projects/<project_id>/target-blueprints/<artifact_id>/confirm")
    def confirm_target_blueprint(project_id: str, artifact_id: str):  # noqa: ANN202
        payload = json_object()
        return jsonify(
            blueprint_service.confirm_target_blueprint(
                project_id,
                artifact_id,
                expected_current_version_id=str(payload.get("expected_current_version_id") or ""),
            )
        )

    @app.post("/v1/projects/<project_id>/draft-candidates")
    def create_blueprint_draft_candidate(project_id: str):  # noqa: ANN202
        payload = json_object()
        result = blueprint_service.create_draft_candidate(
            project_id,
            str(payload.get("target_blueprint_id") or ""),
            str(payload.get("unit_id") or ""),
            str(payload.get("artifact_id") or ""),
        )
        return jsonify(result), 201

    @app.get("/v1/projects/<project_id>/draft-candidates/<candidate_id>")
    def get_blueprint_draft_candidate(project_id: str, candidate_id: str):  # noqa: ANN202
        return jsonify(blueprint_service.get_candidate(project_id, candidate_id))

    @app.post("/v1/projects/<project_id>/draft-candidates/<candidate_id>/accept")
    def accept_blueprint_draft_candidate(project_id: str, candidate_id: str):  # noqa: ANN202
        payload = json_object()
        result = blueprint_service.accept_candidate(
            project_id,
            candidate_id,
            expected_current_version_id=(
                str(payload["expected_current_version_id"])
                if payload.get("expected_current_version_id") is not None
                else None
            ),
        )
        return jsonify(result)

    @app.post("/v1/projects/<project_id>/draft-candidates/<candidate_id>/reject")
    def reject_blueprint_draft_candidate(project_id: str, candidate_id: str):  # noqa: ANN202
        payload = json_object()
        return jsonify(
            blueprint_service.reject_candidate(
                project_id, candidate_id, reason=str(payload.get("reason") or "")
            )
        )

    @app.get("/v1/projects/<project_id>/canvas")
    def canvas_snapshot(project_id: str):  # noqa: ANN202
        return jsonify(repository.canvas_snapshot(project_id, branch=request.args.get("branch", "main")))

    @app.post("/v1/projects/<project_id>/documents/import")
    def import_documents(project_id: str):  # noqa: ANN202
        payload = request.get_json(force=True)
        results = indexer.import_path(
            project_id,
            payload["path"],
            recursive=payload.get("recursive", True),
            branch=payload.get("branch", "main"),
            canon_status=payload.get("canon_status", "reference"),
            metadata=payload.get("metadata"),
            actor="api",
        )
        return jsonify({"imports": [{**asdict(item), "path": str(item.path)} for item in results]}), 201

    @app.post("/v1/projects/<project_id>/documents/upload")
    def upload_document(project_id: str):  # noqa: ANN202
        upload = request.files.get("file")
        if upload is None or not upload.filename:
            raise ValueError("file is required")
        filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", Path(upload.filename).name)
        project = repository.get_project(project_id)
        destination = safe_output_path(
            Path(project["root_path"]),
            Path(".creative-claw") / "uploads" / f"{new_id('upload')}-{filename}",
        )
        upload.save(destination)
        result = indexer.import_file(
            project_id,
            destination,
            branch=request.form.get("branch", "main"),
            canon_status=request.form.get("canon_status", "reference"),
            actor="canvas-upload",
        )
        return jsonify({**asdict(result), "path": str(result.path)}), 201

    @app.post("/v1/projects/<project_id>/sources/text")
    def create_text_source(project_id: str):  # noqa: ANN202
        payload: dict[str, Any] = request.get_json(force=True)
        title = str(payload.get("title") or "").strip()
        text = str(payload.get("text") or "")
        if not title:
            raise ValueError("Source title is required")
        if not text.strip():
            raise ValueError("Source text is required")
        branch = str(payload.get("branch") or "main").strip() or "main"
        canon_status = str(payload.get("canon_status") or "reference").strip() or "reference"
        if canon_status not in {"reference", "canon", "draft"}:
            raise ValueError("canon_status must be reference, canon, or draft")
        repository.get_project(project_id)
        safe_title = re.sub(r'[^\w\u4e00-\u9fff-]+', "-", title, flags=re.UNICODE).strip("-")[:80] or "source"
        virtual_path = f"manual-sources/{new_id('source')}-{safe_title}.md"
        result = indexer.index_text(
            project_id,
            virtual_path,
            text,
            title=title,
            metadata={"source_type": "manual", "created_from": "canvas"},
            branch=branch,
            canon_status=canon_status,
            actor="canvas-source",
        )
        return jsonify({**asdict(result), "path": str(result.path), "title": title}), 201

    @app.delete("/v1/projects/<project_id>/documents/<document_id>")
    def delete_document(project_id: str, document_id: str):  # noqa: ANN202
        return jsonify(indexer.delete_document(project_id, document_id, actor="api"))

    @app.post("/v1/projects/<project_id>/documents/<document_id>/reindex")
    def reindex_document(project_id: str, document_id: str):  # noqa: ANN202
        result = indexer.reindex_document(project_id, document_id, actor="api")
        return jsonify({**asdict(result), "path": str(result.path)})

    @app.post("/v1/projects/<project_id>/embeddings/backfill")
    def backfill_embeddings(project_id: str):  # noqa: ANN202
        payload: dict[str, Any] = request.get_json(silent=True) or {}
        return jsonify(
            indexer.backfill_embeddings(
                project_id,
                replace=bool(payload.get("replace", False)),
                batch_size=int(payload.get("batch_size", 64)),
                actor="api",
            )
        )

    @app.post("/v1/projects/<project_id>/search")
    def search(project_id: str):  # noqa: ANN202
        payload = request.get_json(force=True)
        hits = retriever.search(project_id, payload["query"], top_k=int(payload.get("top_k", 8)), filters=payload.get("filters"))
        return jsonify({"query": payload["query"], "results": [hit.citation() for hit in hits]})

    @app.post("/v1/projects/<project_id>/context")
    def context(project_id: str):  # noqa: ANN202
        payload = request.get_json(force=True)
        scope = ContextScope.from_payload(payload)
        result = retriever.build_context(
            project_id,
            payload["query"],
            top_k=int(payload.get("top_k", 8)),
            scope=scope,
        )
        result["citation_validation"] = validate_citations("", result["evidence_refs"])
        return jsonify(result)

    @app.post("/v1/projects/<project_id>/chat")
    def grounded_chat(project_id: str):  # noqa: ANN202
        payload = request.get_json(force=True)
        message = str(payload.get("message") or "").strip()
        if not message:
            raise ValueError("message is required")
        scope = ContextScope.from_payload(payload)
        context_result = retriever.build_context(
            project_id,
            message,
            top_k=int(payload.get("top_k", 8)),
            scope=scope,
        )
        answer = OpenAICompatibleWriter.from_env().answer(
            message,
            context_result,
            mode=str(payload.get("mode") or "analysis"),
        )
        citation_validation = validate_citations(
            answer.get("answer", ""), context_result["evidence_refs"]
        )
        return jsonify(
            {
                **answer,
                "resolved_scope": context_result["resolved_scope"],
                "evidence_refs": context_result["evidence_refs"],
                "citation_validation": citation_validation,
                "citations": context_result["citations"],
                "graph": context_result["graph"],
                "timeline": context_result["timeline"],
                "ohlc": context_result["ohlc"],
                "retrieval_policy": context_result["retrieval_policy"],
            }
        )

    @app.get("/v1/projects/<project_id>/ledger/verify")
    def verify_ledger(project_id: str):  # noqa: ANN202
        return jsonify(repository.ledger.verify(project_id))

    @app.get("/v1/projects/<project_id>/ledger/events")
    def ledger_events(project_id: str):  # noqa: ANN202
        limit = min(max(int(request.args.get("limit", 8)), 1), 50)
        events = []
        for event in repository.ledger.list(project_id, limit=limit):
            public_event = dict(event)
            public_event.pop("payload_json", None)
            events.append(public_event)
        return jsonify({"verification": repository.ledger.verify(project_id), "events": events})

    @app.post("/v1/tasks")
    def create_task():  # noqa: ANN202
        payload = request.get_json(force=True)
        return jsonify(runtime.create_task(payload["project_id"], payload["goal"], payload["plan"])), 201

    @app.post("/v1/tasks/auto")
    def create_automatic_task():  # noqa: ANN202
        payload = request.get_json(force=True)
        plan = OpenAICompatiblePlanner.from_env().plan(payload["goal"], registry)
        return jsonify(runtime.create_task(payload["project_id"], payload["goal"], plan)), 201

    @app.get("/v1/tasks/<task_id>")
    def get_task(task_id: str):  # noqa: ANN202
        return jsonify(runtime.get_task(task_id))

    @app.post("/v1/tasks/<task_id>/step")
    def step_task(task_id: str):  # noqa: ANN202
        payload: dict[str, Any] = request.get_json(silent=True) or {}
        if payload.get("reject"):
            return jsonify(runtime.reject(task_id, reason=str(payload.get("reason") or "Rejected by user")))
        if payload.get("run_until_blocked"):
            return jsonify(runtime.run_until_blocked(task_id))
        return jsonify(runtime.step(task_id, approve=bool(payload.get("approve", False))))

    return app
