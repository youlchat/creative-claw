from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from creative_claw.cold_start import (
    ColdStartConflictError,
    ColdStartService,
    normalize_preview,
    parse_preview_text,
)
from creative_claw.db import Database
from creative_claw.ledger import Ledger
from creative_claw.repository import Repository


VALID_PREVIEW = {
    "title": "铜铃镇的聪明账单",
    "premise": "机智小贩用六次反转让贪心税吏为自己的规则买单。",
    "protagonist_key": "hero",
    "kline_dimension": "解局主动权",
    "entities": [
        {
            "key": "hero",
            "name": "艾山",
            "entity_type": "character",
            "description": "冷静机智的小贩",
        },
        {
            "key": "collector",
            "name": "罗班",
            "entity_type": "character",
            "description": "贪心的税吏",
        },
        {
            "key": "market",
            "name": "铜铃市集",
            "entity_type": "location",
            "description": "交易发生的公共市集",
        },
    ],
    "relations": [
        {"source_key": "hero", "predicate": "智斗", "target_key": "collector"}
    ],
    "scenes": [
        {
            "title": "怪税告示",
            "summary": "罗班宣布影子也要纳税。",
            "story_time": "清晨",
            "entity_keys": ["hero", "collector", "market"],
            "ohlc": {"open": 30.04, "high": 42.06, "low": 24.04, "close": 38.04},
        },
        {
            "title": "主动交账",
            "summary": "艾山带来一张没有数字的账单。",
            "story_time": "上午",
            "entity_keys": ["hero", "collector"],
            "ohlc": {"open": 12, "high": 52, "low": 35, "close": 48},
        },
        {
            "title": "规则套索",
            "summary": "罗班亲口确认声音也能抵税。",
            "story_time": "正午",
            "entity_keys": ["hero", "collector"],
            "ohlc": {"open": 48, "high": 64, "low": 44, "close": 60},
        },
        {
            "title": "铜钱回声",
            "summary": "艾山摇响钱袋，以声音支付影子税。",
            "story_time": "午后",
            "entity_keys": ["hero", "collector", "market"],
            "ohlc": {"open": 60, "high": 78, "low": 56, "close": 73},
        },
        {
            "title": "众人作证",
            "summary": "市民复述罗班刚刚确认的规则。",
            "story_time": "傍晚",
            "entity_keys": ["hero", "collector", "market"],
            "ohlc": {"open": 73, "high": 88, "low": 70, "close": 84},
        },
        {
            "title": "税吏买单",
            "summary": "罗班撤下告示并退还错收的钱。",
            "story_time": "日落",
            "entity_keys": ["hero", "collector", "market"],
            "ohlc": {"open": 84, "high": 94, "low": 80, "close": 90},
        },
    ],
}


class SequenceWriter:
    model = "fake-cold-start"

    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.calls: list[dict[str, str] | None] = []

    def generate(self, prompt: str, *, repair: dict[str, str] | None = None) -> str:
        self.calls.append(repair)
        return self.responses[len(self.calls) - 1]


class ColdStartNormalizationTests(unittest.TestCase):
    def test_normalizes_rounding_and_continuous_ohlc_without_missing_references(self) -> None:
        normalized = normalize_preview(VALID_PREVIEW)

        self.assertEqual(
            normalized["scenes"][0]["ohlc"],
            {"open": 30.0, "high": 42.1, "low": 24.0, "close": 38.0},
        )
        self.assertEqual(
            normalized["scenes"][1]["ohlc"],
            {"open": 38.0, "high": 52.0, "low": 35.0, "close": 48.0},
        )

    def test_rejects_entity_scene_and_reference_contract_breaks(self) -> None:
        too_few_entities = copy.deepcopy(VALID_PREVIEW)
        too_few_entities["entities"] = too_few_entities["entities"][:2]
        too_few_scenes = copy.deepcopy(VALID_PREVIEW)
        too_few_scenes["scenes"] = too_few_scenes["scenes"][:5]
        unknown_reference = copy.deepcopy(VALID_PREVIEW)
        unknown_reference["scenes"][0]["entity_keys"] = ["missing"]

        for value in (too_few_entities, too_few_scenes, unknown_reference):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    normalize_preview(value)

    def test_parses_json_code_fence_and_rejects_missing_ohlc_field(self) -> None:
        parsed = parse_preview_text(
            "```json\n" + json.dumps(VALID_PREVIEW, ensure_ascii=False) + "\n```"
        )
        self.assertEqual(parsed["title"], "铜铃镇的聪明账单")

        invalid = copy.deepcopy(VALID_PREVIEW)
        del invalid["scenes"][0]["ohlc"]["low"]
        with self.assertRaises(ValueError):
            normalize_preview(invalid)


class ColdStartPreviewServiceTests(unittest.TestCase):
    def test_invalid_first_response_is_repaired_once_without_writing_project(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "test.db")
            database.initialize()
            repository = Repository(database)
            repository.create_project("空项目", temp_dir, "empty")
            writer = SequenceWriter(
                ["not-json", json.dumps(VALID_PREVIEW, ensure_ascii=False)]
            )

            result = ColdStartService(database).preview(
                "empty", "写一个原创民间幽默故事", writer
            )

            self.assertEqual(result["preview"]["title"], "铜铃镇的聪明账单")
            self.assertEqual(
                result["generation"],
                {"prompt": "写一个原创民间幽默故事", "model": "fake-cold-start"},
            )
            self.assertIsNone(writer.calls[0])
            self.assertIn("not-json", writer.calls[1]["response"])
            snapshot = repository.canvas_snapshot("empty")
            self.assertEqual(snapshot["entities"], [])
            self.assertEqual(snapshot["timeline"], [])
            self.assertEqual(snapshot["ohlc"], [])


class ColdStartApplyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = Database(self.root / "test.db")
        self.database.initialize()
        self.repository = Repository(self.database)
        self.repository.create_project("空项目", self.root, "empty")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_apply_creates_connected_framework_and_one_audit_event(self) -> None:
        result = ColdStartService(self.database).apply(
            "empty",
            VALID_PREVIEW,
            {"prompt": "写一个原创民间幽默故事", "model": "fake-cold-start"},
        )

        snapshot = result["snapshot"]
        self.assertEqual(snapshot["project"]["name"], "铜铃镇的聪明账单")
        self.assertEqual(len(snapshot["entities"]), 3)
        self.assertEqual(len(snapshot["relations"]), 1)
        self.assertEqual(len(snapshot["timeline"]), 6)
        self.assertEqual(len(snapshot["ohlc"]), 6)
        self.assertEqual(
            {row["timeline_event_id"] for row in snapshot["ohlc"]},
            {row["id"] for row in snapshot["timeline"]},
        )
        self.assertTrue(
            all(row["attrs"]["status"] == "outline" for row in snapshot["timeline"])
        )
        self.assertTrue(
            all(row["attrs"]["format"] == "scene_card" for row in snapshot["timeline"])
        )
        applied = [
            event
            for event in Ledger(self.database).list("empty")
            if event["event_type"] == "cold_start.applied"
        ]
        self.assertEqual(len(applied), 1)
        self.assertEqual(result["summary"]["scenes"], 6)
        self.assertTrue(Ledger(self.database).verify("empty")["valid"])

    def test_apply_rolls_back_every_write_when_ledger_append_fails(self) -> None:
        before = self.repository.canvas_snapshot("empty")

        with patch.object(Ledger, "append", side_effect=RuntimeError("forced ledger failure")):
            with self.assertRaisesRegex(RuntimeError, "forced ledger failure"):
                ColdStartService(self.database).apply(
                    "empty",
                    VALID_PREVIEW,
                    {"prompt": "写一个原创民间幽默故事", "model": "fake-cold-start"},
                )

        after = self.repository.canvas_snapshot("empty")
        self.assertEqual(after["project"]["name"], before["project"]["name"])
        self.assertEqual(after["entities"], [])
        self.assertEqual(after["relations"], [])
        self.assertEqual(after["timeline"], [])
        self.assertEqual(after["ohlc"], [])

    def test_existing_content_blocks_preview_and_apply(self) -> None:
        self.repository.upsert_entity("empty", "已有角色", "character")
        service = ColdStartService(self.database)

        with self.assertRaises(ColdStartConflictError):
            service.preview(
                "empty",
                "另一个故事",
                SequenceWriter([json.dumps(VALID_PREVIEW, ensure_ascii=False)]),
            )
        with self.assertRaises(ColdStartConflictError):
            service.apply(
                "empty",
                VALID_PREVIEW,
                {"prompt": "另一个故事", "model": "fake-cold-start"},
            )


if __name__ == "__main__":
    unittest.main()
