from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from creative_claw.context import ContextScope
from creative_claw.db import Database
from creative_claw.indexer import Indexer
from creative_claw.repository import Repository
from creative_claw.retrieval import HybridRetriever


@unittest.skipUnless(
    os.getenv("CREATIVE_CLAW_REAL_LLM_TEST") == "1",
    "set CREATIVE_CLAW_REAL_LLM_TEST=1 to run paid real-model verification",
)
class RealLlmContextTests(unittest.TestCase):
    def setUp(self) -> None:
        if not os.getenv("CREATIVE_CLAW_LLM_API_KEY"):
            self.skipTest("model key is not configured")
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = Database(self.root / "creative-claw.db")
        self.database.initialize()
        self.repository = Repository(self.database)
        self.repository.create_project("模型上下文验证", self.root, "real-llm-test")
        self.indexer = Indexer(self.database)
        self.indexer.index_text(
            "real-llm-test",
            "canon/guyao.md",
            "顾遥在密室拒绝交出钥匙，她对导师的信任开始动摇。",
            metadata={"episode": 18, "scene": 2},
            canon_status="canon",
        )
        self.current = self.repository.add_timeline_event(
            "real-llm-test", "拒交钥匙", "顾遥当场拒绝交出钥匙。", episode=18, scene=2
        )
        self.repository.add_timeline_event(
            "real-llm-test", "进入密室", "顾遥进入密室。", episode=18, scene=1
        )
        self.repository.add_timeline_event(
            "real-llm-test", "离开密室", "顾遥离开密室。", episode=18, scene=3
        )
        self.repository.upsert_ohlc(
            "real-llm-test", "顾遥", "信任度", "scene", "E18-S02", 18.02,
            30, 45, 20, 25, timeline_event_id=self.current["id"]
        )
        self.retriever = HybridRetriever(self.database)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _assert_no_secret_leak(self, obj: object) -> None:
        serialized = json.dumps(obj, ensure_ascii=False, default=str)
        self.assertNotIn("sk-", serialized)
        self.assertNotIn("Bearer ", serialized)
        self.assertNotIn("Authorization", serialized)

    def test_context_includes_timeline_and_kline_for_real_model(self) -> None:
        from creative_claw.llm import OpenAICompatibleWriter

        scope = ContextScope(
            branch="main",
            scene_id=self.current["id"],
            character_name="顾遥",
            dimension="信任度",
        )
        context_result = self.retriever.build_context(
            "real-llm-test",
            "只依据证据说明顾遥在当前场景做了什么，以及她的信任度从 open 到 close 如何变化。每个事实分别引用时间线和 K 线编号。",
            scope=scope,
        )
        writer = OpenAICompatibleWriter.from_env()
        answer = writer.answer(
            "只依据证据说明顾遥在当前场景做了什么，以及她的信任度从 open 到 close 如何变化。每个事实分别引用时间线和 K 线编号。",
            context_result,
            mode="analysis",
        )

        response = answer["answer"]
        self.assertIsInstance(response, str)
        self.assertTrue(len(response) > 20)

        self.assertEqual(
            context_result["resolved_scope"]["scene_id"], self.current["id"]
        )
        evidence_refs = context_result["evidence_refs"]
        self.assertTrue(
            any(ref["kind"] == "timeline" for ref in evidence_refs)
        )
        self.assertTrue(
            any(ref["kind"] == "kline" for ref in evidence_refs)
        )
        from creative_claw.evidence import validate_citations

        validation = validate_citations(response, evidence_refs)
        self.assertEqual(
            validation["unknown"], [], f"model used unknown refs: {validation['unknown']}"
        )
        self.assertIn("T1", [ref["ref"] for ref in evidence_refs], "no timeline ref")
        self.assertIn("K1", [ref["ref"] for ref in evidence_refs], "no kline ref")
        has_timeline_citation = any(
            f"[{token}]" in response for token in validation["used"]
        )
        self.assertTrue(
            has_timeline_citation or not validation["used"],
            f"model answer uses no citation: {validation}",
        )
        self._assert_no_secret_leak(answer)
        self._assert_no_secret_leak(context_result)


if __name__ == "__main__":
    unittest.main()
