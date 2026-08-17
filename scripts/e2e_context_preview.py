from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.request import urlopen

from playwright.sync_api import sync_playwright

from creative_claw.db import Database
from creative_claw.indexer import Indexer
from creative_claw.repository import Repository


def wait_for_service(url: str, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urlopen(url, timeout=1.0) as response:  # noqa: S310 - fixed localhost URL
                if response.status == 200:
                    return
        except Exception:
            time.sleep(0.2)
    raise RuntimeError(f"service did not become ready: {url}")


def seed(database_path: Path, root: Path) -> dict[str, Any]:
    database = Database(database_path)
    database.initialize()
    repository = Repository(database)
    project = repository.create_project("Phase 1 浏览器验收", root, "phase1-e2e")
    Indexer(database).index_text(
        project["id"],
        "canon/context.md",
        "顾遥在密室拒绝交出钥匙。林川随后决定独自追查。",
        metadata={"episode": 18},
        canon_status="canon",
    )
    first = repository.add_timeline_event(
        project["id"], "进入密室", "顾遥与林川进入密室。", episode=18, scene=1
    )
    second = repository.add_timeline_event(
        project["id"], "拒交钥匙", "顾遥拒绝交出钥匙。", episode=18, scene=2
    )
    third = repository.add_timeline_event(
        project["id"], "独自追查", "林川决定独自追查。", episode=18, scene=3
    )
    repository.upsert_ohlc(
        project["id"], "顾遥", "信任度", "scene", "E18-S02", 18.02,
        30, 45, 20, 25, timeline_event_id=second["id"]
    )
    repository.upsert_ohlc(
        project["id"], "林川", "决心", "scene", "E18-S03", 18.03,
        40, 60, 35, 55, timeline_event_id=third["id"]
    )
    return {"project": project, "first": first, "second": second, "third": third}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    output_dir = args.output_dir or Path(tempfile.mkdtemp(prefix="creative-claw-phase1-e2e-"))
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "context-preview-report.json"
    screenshot_path = output_dir / "context-preview.png"

    with tempfile.TemporaryDirectory(prefix="creative-claw-e2e-db-") as temp_name:
        temp_root = Path(temp_name)
        database_path = temp_root / "creative-claw.db"
        fixture = seed(database_path, temp_root)
        command = [
            sys.executable,
            "-m",
            "creative_claw",
            "--db",
            str(database_path),
            "serve",
            "--host",
            "127.0.0.1",
            "--port",
            str(args.port),
        ]
        creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            if sock.connect_ex(("127.0.0.1", args.port)) == 0:
                raise RuntimeError(f"port {args.port} is already in use")
        process = subprocess.Popen(
            command,
            cwd=Path(__file__).resolve().parents[1],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        report: dict[str, Any] = {"passed": False, "previews": [], "requests": [], "console": [], "page_errors": []}
        page = None
        try:
            base_url = f"http://127.0.0.1:{args.port}"
            wait_for_service(f"{base_url}/v1/config")
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(channel="msedge", headless=True)
                page = browser.new_page(viewport={"width": 1600, "height": 1000})
                page.on("console", lambda message: report["console"].append({"type": message.type, "text": message.text}))
                page.on("pageerror", lambda error: report["page_errors"].append(str(error)))

                def capture_request(request: Any) -> None:
                    if request.url.endswith("/context") and request.method == "POST":
                        payload = request.post_data_json
                        report["requests"].append({"scope": payload.get("scope"), "query": payload.get("query")})

                page.on("request", capture_request)
                page.goto(base_url, wait_until="networkidle")
                report["project_value"] = page.locator("#projectSelect").input_value()
                report["canvas_node_count"] = page.locator(".canvas-node").count()
                report["body_excerpt"] = page.locator("body").inner_text()[:1200]
                page.locator("#chatInput").fill("当前人物在这个场景做了什么，状态如何变化？")

                expected = [
                    (fixture["second"], "顾遥", "信任度"),
                    (fixture["third"], "林川", "决心"),
                ]
                for index, (event, character, dimension) in enumerate(expected, start=1):
                    page.locator(f'[data-node-id="scene:{event["id"]}"]').click(force=True)
                    page.locator('[data-tab="assistant"]').click()
                    page.wait_for_timeout(250)
                    page.locator("#previewContext").click()
                    page.locator("#contextDialog").wait_for(state="visible")
                    dialog_text = page.locator("#contextDialog").inner_text()
                    evidence_text = page.locator("#contextEvidenceSummary").inner_text()
                    assertions = {
                        "scene": event["id"] in dialog_text,
                        "character": character in dialog_text,
                        "dimension": dimension in dialog_text,
                        "timeline_ref": "[T1]" in evidence_text,
                        "kline_ref": "[K1]" in evidence_text,
                        "workflow": "确认上下文 → 运行模型 → 审阅候选 → 接受或拒绝" in page.locator("body").inner_text(),
                    }
                    if not all(assertions.values()):
                        raise AssertionError(f"preview {index} failed: {assertions}\n{dialog_text}")
                    report["previews"].append(
                        {
                            "scene_id": event["id"],
                            "character_name": character,
                            "dimension": dimension,
                            "assertions": assertions,
                        }
                    )
                    page.locator('#contextDialog button[value="cancel"]').first.click()
                    page.locator("#contextDialog").wait_for(state="hidden")

                page.screenshot(path=str(screenshot_path), full_page=True)
                browser.close()

            if len(report["requests"]) != 2:
                raise AssertionError(f"expected 2 context requests, got {len(report['requests'])}")
            first_scope, second_scope = [row["scope"] for row in report["requests"]]
            if first_scope["scene_id"] != fixture["second"]["id"]:
                raise AssertionError("first request did not use selected second scene")
            if second_scope["scene_id"] != fixture["third"]["id"]:
                raise AssertionError("second request did not use selected third scene")
            serialized = json.dumps(report["requests"], ensure_ascii=False)
            if "沈霜" in serialized:
                raise AssertionError("demo character leaked into context request")
            report["passed"] = True
            return_code = 0
        except Exception as exc:
            report["error"] = str(exc)
            if page is not None:
                try:
                    report["canvas_node_count_at_failure"] = page.locator(".canvas-node").count()
                    report["body_excerpt_at_failure"] = page.locator("body").inner_text()[:2000]
                    page.screenshot(path=str(screenshot_path), full_page=True)
                except Exception as diagnostic_error:
                    report["diagnostic_error"] = str(diagnostic_error)
            return_code = 1
        finally:
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            report["service_stopped"] = process.poll() is not None
            report["screenshot"] = str(screenshot_path.resolve())
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(str(report_path.resolve()))
        return return_code


if __name__ == "__main__":
    raise SystemExit(main())
