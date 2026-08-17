from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any
from urllib.request import urlopen

from playwright.sync_api import sync_playwright

from creative_claw.db import Database
from creative_claw.repository import Repository


PREVIEW = {
    "title": "铜铃镇的聪明账单",
    "premise": "机智小贩让贪心税吏为自己的荒唐规则买单。",
    "protagonist_key": "hero",
    "kline_dimension": "解局主动权",
    "entities": [
        {"key": "hero", "name": "艾山", "entity_type": "character", "description": "冷静机智的小贩"},
        {"key": "collector", "name": "罗班", "entity_type": "character", "description": "贪心的税吏"},
        {"key": "market", "name": "铜铃市集", "entity_type": "location", "description": "公开交易的市集"},
    ],
    "relations": [
        {"source_key": "hero", "predicate": "智斗", "target_key": "collector"}
    ],
    "scenes": [
        {"title": "怪税告示", "summary": "罗班宣布影子也要纳税。", "story_time": "清晨", "entity_keys": ["hero", "collector", "market"], "ohlc": {"open": 30, "high": 42, "low": 24, "close": 38}},
        {"title": "主动交账", "summary": "艾山带来一张没有数字的账单。", "story_time": "上午", "entity_keys": ["hero", "collector"], "ohlc": {"open": 38, "high": 52, "low": 35, "close": 48}},
        {"title": "规则套索", "summary": "罗班亲口确认声音也能抵税。", "story_time": "正午", "entity_keys": ["hero", "collector"], "ohlc": {"open": 48, "high": 64, "low": 44, "close": 60}},
        {"title": "铜钱回声", "summary": "艾山以钱袋声音支付影子税。", "story_time": "午后", "entity_keys": ["hero", "collector", "market"], "ohlc": {"open": 60, "high": 78, "low": 56, "close": 73}},
        {"title": "众人作证", "summary": "市民复述罗班刚确认的规则。", "story_time": "傍晚", "entity_keys": ["hero", "collector", "market"], "ohlc": {"open": 73, "high": 88, "low": 70, "close": 84}},
        {"title": "税吏买单", "summary": "罗班撤下告示并退还错收的钱。", "story_time": "日落", "entity_keys": ["hero", "collector", "market"], "ohlc": {"open": 84, "high": 94, "low": 80, "close": 90}},
    ],
}


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


def ensure_port_free(port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        if sock.connect_ex(("127.0.0.1", port)) == 0:
            raise RuntimeError(f"port {port} is already in use")


class FakeModelHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - stdlib handler contract
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        payload = {
            "id": "cold-start-e2e",
            "model": "e2e-cold-start",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": json.dumps(PREVIEW, ensure_ascii=False),
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 20, "completion_tokens": 300, "total_tokens": 320},
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8769)
    parser.add_argument("--fake-model-port", type=int, default=8770)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    ensure_port_free(args.port)
    ensure_port_free(args.fake_model_port)
    output_dir = args.output_dir or Path(tempfile.mkdtemp(prefix="creative-claw-cold-start-e2e-"))
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "cold-start-report.json"
    screenshot_path = output_dir / "cold-start.png"
    report: dict[str, Any] = {"passed": False, "console": [], "page_errors": []}

    fake_server = ThreadingHTTPServer(("127.0.0.1", args.fake_model_port), FakeModelHandler)
    fake_thread = threading.Thread(target=fake_server.serve_forever, name="fake-cold-start-model", daemon=True)
    fake_thread.start()
    process: subprocess.Popen[bytes] | None = None
    page = None
    try:
        with tempfile.TemporaryDirectory(prefix="creative-claw-cold-start-db-") as temp_name:
            temp_root = Path(temp_name)
            database_path = temp_root / "creative-claw.db"
            database = Database(database_path)
            database.initialize()
            Repository(database).create_project("空白故事", temp_root, "cold-start-e2e")
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
            environment = os.environ.copy()
            environment.update(
                {
                    "CREATIVE_CLAW_LLM_API_KEY": "e2e-key",
                    "CREATIVE_CLAW_LLM_BASE_URL": f"http://127.0.0.1:{args.fake_model_port}/v1",
                    "CREATIVE_CLAW_LLM_MODEL": "e2e-cold-start",
                }
            )
            creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            process = subprocess.Popen(
                command,
                cwd=Path(__file__).resolve().parents[1],
                env=environment,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
            )
            base_url = f"http://127.0.0.1:{args.port}"
            wait_for_service(f"{base_url}/v1/config")

            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(channel="msedge", headless=True)
                page = browser.new_page(viewport={"width": 1600, "height": 1000})
                page.set_default_timeout(8_000)
                page.on("console", lambda message: report["console"].append({"type": message.type, "text": message.text}))
                page.on("pageerror", lambda error: report["page_errors"].append(str(error)))
                page.goto(base_url, wait_until="networkidle")
                page.locator("#chatMode").select_option("cold_start")
                report["placeholder"] = page.locator("#chatInput").get_attribute("placeholder")
                report["button_before"] = page.locator("#sendChat").inner_text()
                if report["placeholder"] != "例如：帮我写一个类阿凡提的幽默故事":
                    raise AssertionError(f"wrong cold-start placeholder: {report['placeholder']}")
                if report["button_before"] != "生成框架预览":
                    raise AssertionError(f"wrong cold-start button: {report['button_before']}")
                page.locator("#chatInput").fill("帮我写一个类阿凡提的幽默故事")
                page.locator("#sendChat").click()
                page.locator(".cold-start-preview").wait_for(state="visible")
                report["preview_title"] = page.locator(".cold-start-preview h3").inner_text()
                report["preview_entities"] = page.locator(".cold-start-entity").count()
                report["preview_scenes"] = page.locator(".cold-start-scene").count()
                report["canvas_before"] = page.locator("#canvasMeta").inner_text()
                if report["preview_title"] != PREVIEW["title"]:
                    raise AssertionError("preview title mismatch")
                if report["preview_entities"] != 3 or report["preview_scenes"] != 6:
                    raise AssertionError("preview item counts mismatch")
                if "0 个场景" not in report["canvas_before"]:
                    raise AssertionError("preview wrote project data before adoption")

                page.locator("#coldStartApply").click()
                page.wait_for_function("document.querySelector('#canvasMeta')?.textContent.includes('6 个场景')")
                page.wait_for_function("document.querySelectorAll('#klineChart .k-body').length === 6")
                report["canvas_after"] = page.locator("#canvasMeta").inner_text()
                report["candles"] = page.locator("#klineChart .k-body").count()
                report["kline_title"] = page.locator("#klineBoardTitle").inner_text()
                if report["kline_title"] != "艾山 · 解局主动权 K 线":
                    raise AssertionError(f"K-line title did not follow adopted framework: {report['kline_title']}")
                page.screenshot(path=str(screenshot_path), full_page=True)
                browser.close()

            with database.connect() as connection:
                counts = {}
                for table in ("entities", "relations", "timeline_events", "ohlc_points"):
                    counts[table] = connection.execute(
                        f"SELECT COUNT(*) AS n FROM {table} WHERE project_id=?",
                        ("cold-start-e2e",),
                    ).fetchone()["n"]
                counts["linked_ohlc"] = connection.execute(
                    "SELECT COUNT(*) AS n FROM ohlc_points WHERE project_id=? AND timeline_event_id IS NOT NULL",
                    ("cold-start-e2e",),
                ).fetchone()["n"]
                counts["audit"] = connection.execute(
                    "SELECT COUNT(*) AS n FROM ledger_events WHERE project_id=? AND event_type='cold_start.applied'",
                    ("cold-start-e2e",),
                ).fetchone()["n"]
            report["database_counts"] = counts
            expected_counts = {
                "entities": 3,
                "relations": 1,
                "timeline_events": 6,
                "ohlc_points": 6,
                "linked_ohlc": 6,
                "audit": 1,
            }
            if counts != expected_counts:
                raise AssertionError(f"database counts mismatch: {counts}")
            if report["page_errors"]:
                raise AssertionError(f"page errors: {report['page_errors']}")
            report["passed"] = True
            return_code = 0
    except Exception as error:
        report["error"] = str(error)
        if page is not None:
            try:
                report["body_excerpt_at_failure"] = page.locator("body").inner_text()[:2000]
                page.screenshot(path=str(screenshot_path), full_page=True)
            except Exception as diagnostic_error:
                report["diagnostic_error"] = str(diagnostic_error)
        return_code = 1
    finally:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        fake_server.shutdown()
        fake_server.server_close()
        fake_thread.join(timeout=5)
        report["service_stopped"] = process is None or process.poll() is not None
        report["fake_model_stopped"] = not fake_thread.is_alive()
        report["screenshot"] = str(screenshot_path.resolve())
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(str(report_path.resolve()))
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
