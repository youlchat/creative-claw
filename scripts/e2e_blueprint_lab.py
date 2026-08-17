from __future__ import annotations

import argparse
import hashlib
import json
import socket
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

from creative_claw.api import create_app
from creative_claw.blueprint_agents import DeterministicAgentRegistry
from creative_claw.db import Database
from creative_claw.repository import Repository
from creative_claw.workflow import WorkflowService


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def seed(database_path: Path, root: Path) -> dict[str, Any]:
    database = Database(database_path)
    database.initialize()
    project = Repository(database).create_project("Phase 2.5 蓝图验收", root, "phase25-e2e")
    workflow = WorkflowService(database)
    unit = workflow.create_production_unit(project["id"], "scene", "第一场", position=1)
    artifact = workflow.create_artifact(
        project["id"], "manuscript", "第一场正式稿", unit_id=unit["id"]
    )
    return {"project": project, "unit": unit, "artifact": artifact}


def wait_for_port(port: int, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.1)
    raise RuntimeError(f"service did not become ready on port {port}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8768)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    output_dir = args.output_dir or Path(tempfile.mkdtemp(prefix="creative-claw-phase25-e2e-"))
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "blueprint-lab-report.json"
    screenshot_path = output_dir / "blueprint-lab.png"

    with tempfile.TemporaryDirectory(prefix="creative-claw-blueprint-db-") as temp_name:
        temp_root = Path(temp_name)
        database_path = temp_root / "creative-claw.db"
        fixture = seed(database_path, temp_root)
        registry = DeterministicAgentRegistry(delay_seconds=0.01)
        app = create_app(
            database_path,
            blueprint_registry=registry,
            run_blueprint_jobs_inline=False,
        )
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            if sock.connect_ex(("127.0.0.1", args.port)) == 0:
                raise RuntimeError(f"port {args.port} is already in use")
        server = make_server("127.0.0.1", args.port, app, threaded=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        report: dict[str, Any] = {
            "passed": False,
            "project_id": fixture["project"]["id"],
            "requests": [],
            "console": [],
            "page_errors": [],
        }
        page = None
        try:
            wait_for_port(args.port)
            base_url = f"http://127.0.0.1:{args.port}"
            reference_text = "第一章\n守门人沿冰河寻找旧塔，却决定焚毁唯一地图。"
            safe_draft = "声学师拆下铜铃，在无风广场重建全新的共振顺序。"
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(channel="msedge", headless=True)
                page = browser.new_page(viewport={"width": 1600, "height": 1000})
                page.on("console", lambda message: report["console"].append({"type": message.type, "text": message.text[:240]}))
                page.on("pageerror", lambda error: report["page_errors"].append(str(error)))
                page.on("dialog", lambda dialog: dialog.accept("E2E 拒绝复制候选"))

                def capture_request(request: Any) -> None:
                    if request.url.endswith("/draft-candidates") and request.method == "POST":
                        payload = request.post_data_json
                        report["requests"].append({"path": "/draft-candidates", "keys": sorted(payload)})

                page.on("request", capture_request)
                page.goto(base_url, wait_until="networkidle")
                page.locator("#blueprintLabButton").click()
                page.locator("#blueprintLabDialog").wait_for(state="visible")
                page.locator("#referenceTitleInput").fill("机制参考")
                page.locator("#referenceRightsBasis").select_option("research_reference")
                page.locator("#referenceTextInput").fill(reference_text)
                page.locator("#startReferenceBlueprint").click()
                page.locator("#referenceBlueprintTree .blueprint-node").first.wait_for(timeout=30_000)
                report["reference_node_count"] = page.locator("#referenceBlueprintTree .blueprint-node").count()
                report["conflict_count"] = page.locator("#blueprintConflictQueue .blueprint-conflict").count()
                report["interpretation_count"] = page.locator("#blueprintConflictQueue .blueprint-interpretation").count()
                first_interpretation = page.locator("#blueprintConflictQueue .blueprint-interpretation select").first
                if first_interpretation.count():
                    first_interpretation.select_option("confirmed")
                    page.locator("#saveReferenceBlueprint").click()
                    page.locator("#referenceBlueprintTree .blueprint-node").first.wait_for(timeout=30_000)

                page.locator("#targetSettingInput").fill("沙漠声学师寻找失落钟阵，失败会让城市永远没有清晨。")
                page.locator("#createTargetBlueprint").click()
                page.locator("#targetSettingFields [data-setting-field]").first.wait_for(timeout=30_000)
                page.locator("#targetSettingFields [data-setting-field='genre']").fill("author-confirmed-fantasy")
                page.locator("#confirmTargetSetting").click()
                page.wait_for_function("!document.querySelector('#migrateTargetBlueprint').disabled")
                page.locator("#migrateTargetBlueprint").click()
                page.locator("#targetBlueprintTree .blueprint-node").first.wait_for(timeout=30_000)
                page.locator("#confirmTargetBlueprint").click()
                page.locator("#generateUnitDraft").wait_for(state="visible")
                page.wait_for_function("!document.querySelector('#generateUnitDraft').disabled")

                registry.set_draft_text(reference_text)
                page.locator("#generateUnitDraft").click()
                page.wait_for_function("document.querySelector('#similarityReport .risk-badge')?.textContent === 'blocked'")
                report["blocked_accept_disabled"] = page.locator("#acceptDraftCandidate").is_disabled()
                page.locator("#rejectDraftCandidate").click()

                registry.set_draft_text(safe_draft)
                page.locator("#generateUnitDraft").click()
                page.wait_for_function("document.querySelector('#similarityReport .risk-badge')?.textContent === 'passed'")
                report["passed_accept_enabled"] = page.locator("#acceptDraftCandidate").is_enabled()
                page.locator("#acceptDraftCandidate").click()
                page.wait_for_function("document.querySelector('#acceptDraftCandidate').disabled")

                long_text = "\n".join(f"第{i}章\n" + "潮汐迫使人物改变目标。" * 900 for i in range(1, 5))
                page.locator("#referenceTitleInput").fill("长篇参考")
                page.locator("#referenceTextInput").fill(long_text)
                page.locator("#forceBackgroundBlueprint").check()
                page.locator("#startReferenceBlueprint").click()
                page.wait_for_function("!document.querySelector('#pauseBlueprintJob').disabled")
                page.locator("#pauseBlueprintJob").click()
                page.wait_for_function("document.querySelector('#blueprintJobProgress span').textContent.includes('paused')")
                report["long_pause_visible"] = True
                page.locator("#resumeBlueprintJob").click()
                page.wait_for_function(
                    "document.querySelector('#blueprintJobProgress span').textContent.includes('completed')",
                    timeout=60_000,
                )
                report["long_resume_completed"] = True
                page.screenshot(path=str(screenshot_path), full_page=True)
                browser.close()

            expected_request_keys = ["artifact_id", "target_blueprint_id", "unit_id"]
            if not report["requests"] or any(row["keys"] != expected_request_keys for row in report["requests"]):
                raise AssertionError(f"draft request allowlist failed: {report['requests']}")
            if report["reference_node_count"] < 2 or report["conflict_count"] < 1 or report["interpretation_count"] < 2:
                raise AssertionError("reference blueprint hierarchy or conflict queue was not rendered")
            if not report["blocked_accept_disabled"] or not report["passed_accept_enabled"]:
                raise AssertionError("candidate gate controls did not follow similarity status")
            if report["page_errors"]:
                raise AssertionError(f"page errors: {report['page_errors']}")
            report["reference_hash"] = sha256(reference_text)
            report["safe_draft_hash"] = sha256(safe_draft)
            report["passed"] = True
            return_code = 0
        except Exception as exc:
            report["error"] = str(exc)
            if page is not None:
                try:
                    report["dialog_visible_at_failure"] = page.locator("#blueprintLabDialog").is_visible()
                    report["progress_at_failure"] = page.locator("#blueprintJobProgress").inner_text()
                    page.screenshot(path=str(screenshot_path), full_page=True)
                except Exception as diagnostic_error:
                    report["diagnostic_error"] = str(diagnostic_error)
            return_code = 1
        finally:
            server.shutdown()
            thread.join(timeout=8)
            executor = app.extensions.get("blueprint_executor")
            if executor is not None:
                executor.shutdown(wait=True, cancel_futures=True)
            report["service_stopped"] = not thread.is_alive()
            report["screenshot"] = str(screenshot_path.resolve())
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(str(report_path.resolve()))
        return return_code


if __name__ == "__main__":
    raise SystemExit(main())
