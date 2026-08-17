from __future__ import annotations

import tempfile
import unittest
from io import BytesIO
from unittest.mock import patch
from pathlib import Path

from creative_claw.api import create_app
from creative_claw.db import Database
from creative_claw.indexer import Indexer
from creative_claw.office import OfficeArtifactService
from creative_claw.repository import Repository
from creative_claw.retrieval import HybridRetriever
from creative_claw.runtime import AgentRuntime
from creative_claw.tools import CreativeToolset


class CreativeClawEndToEndTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = Database(self.root / "creative-claw.db")
        self.database.initialize()
        self.repository = Repository(self.database)
        self.project = self.repository.create_project("雁门雪", self.root, "demo")
        self.indexer = Indexer(self.database)
        self.retriever = HybridRetriever(self.database)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_knowledge_office_graph_ohlc_agent_and_api(self) -> None:
        story = (
            "# E18-S07 偏殿偷听\n\n"
            "沈霜在偏殿听见先帝遗孤仍在人世。她只产生怀疑，并不知道齐尧就是遗孤。\n"
            "身份确认必须等到 E25-S08 密道密诏出现后。"
        )
        indexed = self.indexer.index_text(
            "demo",
            "story/E18-S07.md",
            story,
            metadata={"episode": 18, "scene": 7, "story_time": "景曜十三年冬夜"},
            canon_status="canon",
        )
        self.assertEqual(indexed.chunk_count, 1)
        hits = self.retriever.search("demo", "沈霜何时确认齐尧身份", top_k=4, filters={"episode": 18})
        self.assertTrue(hits)
        self.assertIn("E25-S08", hits[0].text)
        evidence_chunk = hits[0].chunk_id

        shen = self.repository.upsert_entity("demo", "沈霜", "character", aliases=["皇后"])
        qiyao = self.repository.upsert_entity("demo", "齐尧", "character", aliases=["遗孤"])
        fact = self.repository.upsert_entity("demo", "齐尧身份", "canon_fact")
        self.repository.add_relation(
            "demo",
            shen["id"],
            "在E25确认",
            fact["id"],
            evidence_chunk_id=evidence_chunk,
            valid_from="E25-S08",
        )
        self.repository.add_relation("demo", fact["id"], "身份属于", qiyao["id"], evidence_chunk_id=evidence_chunk)
        graph = self.repository.graph_context("demo", "沈霜与齐尧身份")
        self.assertGreaterEqual(len(graph["relations"]), 2)

        timeline_event = self.repository.add_timeline_event(
            "demo",
            "偏殿偷听",
            "沈霜获知遗孤存在，但未确认身份",
            story_time="景曜十三年冬夜",
            episode=18,
            scene=7,
            evidence_chunk_id=evidence_chunk,
        )
        timeline_added = next(
            event
            for event in self.repository.ledger.list("demo")
            if event["event_type"] == "timeline.added" and event["payload"]["id"] == timeline_event["id"]
        )
        self.assertEqual(timeline_added["payload"]["description"], timeline_event["description"])
        self.repository.upsert_ohlc("demo", "沈霜", "知情度", "scene", "E18-S01", 18.01, 42, 50, 38, 47, parent_period_id="E18")
        self.repository.upsert_ohlc("demo", "沈霜", "知情度", "scene", "E18-S02", 18.02, 47, 58, 45, 55, parent_period_id="E18")
        self.repository.upsert_ohlc("demo", "沈霜", "知情度", "scene", "E18-S03", 18.03, 55, 81, 52, 76, parent_period_id="E18")
        aggregated = self.repository.aggregate_ohlc("demo", "沈霜", "知情度", "E18")
        self.assertEqual((aggregated["open"], aggregated["high"], aggregated["low"], aggregated["close"]), (42, 81, 38, 76))
        linked_ohlc = self.repository.upsert_ohlc(
            "demo",
            "沈霜",
            "知情度",
            "scene",
            "E18-S07",
            18.07,
            76,
            86,
            70,
            80,
            parent_period_id="E18",
            timeline_event_id=timeline_event["id"],
        )
        self.assertEqual(linked_ohlc["timeline_event_id"], timeline_event["id"])
        parent = next(row for row in self.repository.ohlc_series("demo", "沈霜", "知情度") if row["period_id"] == "E18")
        self.assertIsNone(parent["timeline_event_id"])

        office = OfficeArtifactService(self.root)
        word = office.export_word(
            "artifacts/story-bible.docx",
            "雁门雪 Story Bible",
            [{"heading": "人物", "paragraphs": ["沈霜：皇后，追查先帝遗孤。"]}],
        )
        powerpoint = office.export_powerpoint(
            "artifacts/pitch.pptx",
            "雁门雪",
            [{"title": "人物弧线", "bullets": ["E18 怀疑", "E25 确认"]}],
        )
        excel = office.export_excel(
            "artifacts/kline.xlsx",
            [{"name": "OHLC", "rows": [["时间", "开", "高", "低", "收"], ["E18", 42, 81, 38, 76]]}],
        )
        word_edit = office.edit_word(
            "artifacts/story-bible.docx",
            replacements={"追查": "调查"},
            append_sections=[{"heading": "审计", "paragraphs": ["改动已记录。"]}],
        )
        powerpoint_edit = office.edit_powerpoint(
            "artifacts/pitch.pptx",
            replacements={"人物弧线": "人物知情弧线"},
            append_slides=[{"title": "正典", "bullets": ["所有结论附带来源"]}],
        )
        excel_edit = office.edit_excel(
            "artifacts/kline.xlsx",
            edits=[{"sheet": "OHLC", "cell": "F1", "value": "审计"}, {"sheet": "OHLC", "cell": "F2", "value": "通过"}],
        )
        self.assertEqual(word_edit["replacements"], 1)
        self.assertEqual(powerpoint_edit["appended_slides"], 1)
        self.assertEqual(excel_edit["edited_cells"], 2)
        for artifact in (word, powerpoint, excel):
            self.assertTrue(Path(artifact["path"]).is_file())
            result = self.indexer.import_file("demo", artifact["path"])
            self.assertGreater(result.chunk_count, 0)

        registry = CreativeToolset(self.database).build_registry()
        runtime = AgentRuntime(self.database, registry)
        task = runtime.create_task(
            "demo",
            "检索身份事实并导出审计表",
            [
                {"tool": "search_knowledge", "args": {"query": "沈霜确认身份", "top_k": 3}},
                {
                    "tool": "export_excel",
                    "args": {
                        "output_path": "artifacts/agent-audit.xlsx",
                        "sheets": [{"name": "审计", "rows": [["检查", "状态"], ["身份时间", "E25"]]}],
                    },
                },
            ],
        )
        task = runtime.run_until_blocked(task["id"])
        self.assertEqual(task["status"], "awaiting_approval")
        self.assertEqual(task["cursor"], 1)
        task = runtime.step(task["id"], approve=True)
        self.assertEqual(task["status"], "completed")
        self.assertTrue((self.root / "artifacts" / "agent-audit.xlsx").is_file())

        manuscript_task = runtime.create_task(
            "demo",
            "修改场景正文",
            [
                {
                    "tool": "update_timeline_event",
                    "args": {
                        "event_id": timeline_event["id"],
                        "description": "沈霜获知遗孤存在，但尚未确认齐尧身份。正文编辑已写入连续性账本。",
                        "patches": [
                            {
                                "start": 10,
                                "end": 14,
                                "removed": "未确认",
                                "inserted": "尚未确认",
                                "source": "manual",
                            }
                        ],
                    },
                }
            ],
        )
        manuscript_task = runtime.run_until_blocked(manuscript_task["id"])
        self.assertEqual(manuscript_task["status"], "awaiting_approval")
        manuscript_task = runtime.step(manuscript_task["id"], approve=True)
        self.assertEqual(manuscript_task["status"], "completed")
        edited_scene = next(item for item in self.repository.canvas_snapshot("demo")["timeline"] if item["id"] == timeline_event["id"])
        self.assertIn("正文编辑已写入", edited_scene["description"])
        ledger_update = next(event for event in self.repository.ledger.list("demo") if event["event_type"] == "timeline.updated")
        self.assertIn("before", ledger_update["payload"])
        self.assertIn("after", ledger_update["payload"])
        self.assertEqual(ledger_update["payload"]["patches"][0]["inserted"], "尚未确认")

        context = self.retriever.build_context(
            "demo",
            "沈霜为什么在 E18 只有怀疑",
            filters={"episode": 18},
            character_name="沈霜",
        )
        self.assertTrue(context["citations"])
        self.assertTrue(context["ohlc"])
        self.assertTrue(self.repository.ledger.verify("demo")["valid"])

        app = create_app(self.database.path)
        client = app.test_client()
        self.assertEqual(client.get("/health").status_code, 200)
        response = client.post("/v1/projects/demo/search", json={"query": "密道密诏", "top_k": 3})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["results"])
        root_response = client.get("/")
        asset_response = client.get("/assets/app.js")
        self.assertEqual(root_response.status_code, 200)
        self.assertEqual(asset_response.status_code, 200)
        root_response.close()
        asset_response.close()
        canvas = client.get("/v1/projects/demo/canvas")
        self.assertEqual(canvas.status_code, 200)
        self.assertEqual(canvas.get_json()["ohlc"][0]["character_name"], "沈霜")
        canvas_link = next(row for row in canvas.get_json()["ohlc"] if row["period_id"] == "E18-S07")
        self.assertEqual(canvas_link["timeline_event_id"], timeline_event["id"])
        ledger_events = client.get("/v1/projects/demo/ledger/events?limit=3")
        self.assertEqual(ledger_events.status_code, 200)
        ledger_payload = ledger_events.get_json()
        self.assertTrue(ledger_payload["verification"]["valid"])
        self.assertLessEqual(len(ledger_payload["events"]), 3)
        self.assertTrue(ledger_payload["events"])
        self.assertIn("payload", ledger_payload["events"][0])
        self.assertNotIn("payload_json", ledger_payload["events"][0])
        config = client.get("/v1/config").get_json()
        self.assertEqual(config["llm"]["model"], "MiniMax-M3")
        self.assertFalse(config["llm"]["api_key_exposed"])
        renamed = client.patch("/v1/projects/demo", json={"name": "雁门雪 · 喜剧实验"})
        self.assertEqual(renamed.status_code, 200)
        self.assertEqual(renamed.get_json()["name"], "雁门雪 · 喜剧实验")
        created = client.post("/v1/projects", json={"name": "长安夜行录"})
        self.assertEqual(created.status_code, 201)
        created_project = created.get_json()
        self.assertEqual(created_project["name"], "长安夜行录")
        self.assertTrue(Path(created_project["root_path"]).is_dir())
        empty_canvas = client.get(f"/v1/projects/{created_project['id']}/canvas")
        self.assertEqual(empty_canvas.status_code, 200)
        self.assertEqual(empty_canvas.get_json()["timeline"], [])
        invalid_project = client.post("/v1/projects", json={"name": "  "})
        self.assertEqual(invalid_project.status_code, 400)
        with patch.dict(
            "os.environ",
            {
                "CREATIVE_CLAW_LLM_API_KEY": "",
                "CREATIVE_CLAW_LLM_BASE_URL": "https://api.minimaxi.com/v1",
                "CREATIVE_CLAW_LLM_MODEL": "MiniMax-M3",
            },
        ):
            configured = client.post(
                "/v1/config/llm",
                json={
                    "api_key": "memory-only-test-key",
                    "base_url": "https://api.minimaxi.com/v1",
                    "model": "MiniMax-M3",
                },
            )
            self.assertEqual(configured.status_code, 200)
            configured_payload = configured.get_json()
            self.assertTrue(configured_payload["llm"]["configured"])
            self.assertNotIn("memory-only-test-key", configured.get_data(as_text=True))
        upload = client.post(
            "/v1/projects/demo/documents/upload",
            data={"file": (BytesIO("E19：沈霜继续追查。".encode("utf-8")), "E19.md")},
            content_type="multipart/form-data",
        )
        self.assertEqual(upload.status_code, 201)
        self.assertGreater(upload.get_json()["chunk_count"], 0)
        manual_source = client.post(
            "/v1/projects/demo/sources/text",
            json={
                "title": "沈霜人物设定",
                "text": "沈霜说话克制，不会在 E18 提前确认齐尧身份。",
                "branch": "main",
                "canon_status": "canon",
            },
        )
        self.assertEqual(manual_source.status_code, 201)
        self.assertGreater(manual_source.get_json()["chunk_count"], 0)
        self.assertTrue(manual_source.get_json()["path"].startswith("manual-sources"))
        with patch.dict("os.environ", {"CREATIVE_CLAW_LLM_API_KEY": ""}):
            chat = client.post("/v1/projects/demo/chat", json={"message": "检查 E18"})
        self.assertEqual(chat.status_code, 400)
        self.assertIn("CREATIVE_CLAW_LLM_API_KEY", chat.get_json()["error"])


if __name__ == "__main__":
    unittest.main()
