from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from creative_claw.api import create_app
from creative_claw.context import ContextScope
from creative_claw.db import Database
from creative_claw.indexer import Indexer
from creative_claw.repository import Repository
from creative_claw.retrieval import HybridRetriever


class RetrieverContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = Database(self.root / "creative-claw.db")
        self.database.initialize()
        self.repository = Repository(self.database)
        self.repository.create_project("上下文项目", self.root, "context-project")
        self.indexer = Indexer(self.database)
        self.indexer.index_text(
            "context-project",
            "canon/character.md",
            "顾遥在第十八集拒绝交出钥匙，她对导师的信任下降。",
            metadata={"episode": 18, "scene": 2},
            canon_status="canon",
        )
        self.before = self.repository.add_timeline_event(
            "context-project", "进入密室", "顾遥进入密室。", episode=18, scene=1
        )
        self.current = self.repository.add_timeline_event(
            "context-project", "拒交钥匙", "顾遥当场拒绝交出钥匙。", episode=18, scene=2
        )
        self.after = self.repository.add_timeline_event(
            "context-project", "离开密室", "顾遥离开密室。", episode=18, scene=3
        )
        self.alternate = self.repository.add_timeline_event(
            "context-project", "分支场景", "分支中的另一决定。", episode=18, scene=2, branch="alternate"
        )
        self.repository.upsert_ohlc(
            "context-project", "顾遥", "信任度", "scene", "E18-S02", 18.02,
            30, 45, 20, 25, timeline_event_id=self.current["id"]
        )
        self.retriever = HybridRetriever(self.database)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_scene_scope_loads_timeline_and_linked_ohlc(self) -> None:
        result = self.retriever.build_context(
            "context-project",
            "顾遥此时是否已经信任导师？",
            scope=ContextScope(
                branch="main",
                scene_id=self.current["id"],
                character_name="顾遥",
                dimension="信任度",
            ),
        )
        self.assertEqual(result["resolved_scope"]["episode"], 18)
        self.assertEqual(result["resolved_scope"]["scene_id"], self.current["id"])
        self.assertEqual(result["timeline"][1]["id"], self.current["id"])
        self.assertEqual(len(result["ohlc"]), 1)
        self.assertIn(self.current["label"], result["context_text"])
        self.assertIn("信任度", result["context_text"])
        self.assertTrue(any(ref["kind"] == "source" for ref in result["evidence_refs"]))
        self.assertTrue(any(ref["kind"] == "timeline" for ref in result["evidence_refs"]))
        self.assertTrue(any(ref["kind"] == "kline" for ref in result["evidence_refs"]))
        self.assertIn("[T", result["context_text"])
        self.assertIn("[K", result["context_text"])

    def test_scene_id_cannot_cross_branch(self) -> None:
        result = self.retriever.build_context(
            "context-project",
            "分支上下文",
            scope=ContextScope(branch="main", scene_id=self.alternate["id"]),
        )
        self.assertEqual(result["timeline"], [])
        self.assertEqual(result["ohlc"], [])

    def test_legacy_episode_character_and_dimension_remain_supported(self) -> None:
        result = self.retriever.build_context(
            "context-project",
            "顾遥信任度",
            filters={"episode": 18, "branch": "main"},
            character_name="顾遥",
            dimension="信任度",
        )
        self.assertTrue(result["timeline"])
        self.assertEqual(result["resolved_scope"]["episode"], 18)

    def test_context_api_returns_utf8_resolved_scope_and_typed_evidence(self) -> None:
        app = create_app(self.database.path)
        client = app.test_client()
        response = client.post(
            "/v1/projects/context-project/context",
            json={
                "query": "顾遥此时的信任状态",
                "scope": {
                    "branch": "main",
                    "scene_id": self.current["id"],
                    "character_name": "顾遥",
                    "dimension": "信任度",
                },
            },
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["resolved_scope"]["scene_id"], self.current["id"])
        self.assertTrue(any(ref["kind"] == "timeline" for ref in payload["evidence_refs"]))
        self.assertTrue(any(ref["kind"] == "kline" for ref in payload["evidence_refs"]))
        self.assertIn("顾遥", response.get_data(as_text=True))

    def test_chat_api_returns_citation_validation(self) -> None:
        app = create_app(self.database.path)
        client = app.test_client()
        fake_writer = unittest.mock.Mock()
        fake_writer.answer.return_value = {
            "answer": "顾遥拒绝交出钥匙。[T1] 她的信任度收低于开盘。[K1]",
            "model": "fake-model",
            "usage": {},
        }
        with patch("creative_claw.api.OpenAICompatibleWriter.from_env", return_value=fake_writer):
            response = client.post(
                "/v1/projects/context-project/chat",
                json={
                    "message": "顾遥此时做了什么？",
                    "scope": {
                        "branch": "main",
                        "scene_id": self.current["id"],
                        "character_name": "顾遥",
                        "dimension": "信任度",
                    },
                },
            )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["resolved_scope"]["scene_id"], self.current["id"])
        self.assertEqual(payload["citation_validation"]["unknown"], [])
        self.assertTrue(payload["citation_validation"]["valid"])
        self.assertIn("evidence_refs", payload)
        self.assertIn("citations", payload)
        self.assertIn("graph", payload)
        self.assertIn("timeline", payload)
        self.assertIn("ohlc", payload)
        self.assertIn("retrieval_policy", payload)


if __name__ == "__main__":
    unittest.main()
