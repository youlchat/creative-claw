from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from creative_claw.context import ContextScope
from creative_claw.db import Database
from creative_claw.repository import Repository


class ContextScopeTests(unittest.TestCase):
    def test_defaults_to_main_branch(self) -> None:
        self.assertEqual(
            ContextScope.from_payload({}).to_dict(),
            {
                "branch": "main",
                "episode": None,
                "scene_id": None,
                "character_name": None,
                "dimension": None,
            },
        )

    def test_scope_wins_over_legacy_fields(self) -> None:
        scope = ContextScope.from_payload(
            {
                "scope": {
                    "branch": "rewrite-a",
                    "episode": "18",
                    "scene_id": "time-current",
                    "character_name": "顾遥",
                    "dimension": "信任度",
                },
                "filters": {"branch": "main", "episode": 3},
                "character_name": "沈霜",
                "dimension": "知情度",
            }
        )
        self.assertEqual(scope.branch, "rewrite-a")
        self.assertEqual(scope.episode, 18)
        self.assertEqual(scope.scene_id, "time-current")
        self.assertEqual(scope.character_name, "顾遥")
        self.assertEqual(scope.dimension, "信任度")

    def test_legacy_request_remains_supported(self) -> None:
        scope = ContextScope.from_payload(
            {
                "filters": {"branch": "main", "episode": 7},
                "character_name": "林川",
                "dimension": "决心",
            }
        )
        self.assertEqual(scope.episode, 7)
        self.assertEqual(scope.character_name, "林川")
        self.assertEqual(scope.dimension, "决心")

    def test_blank_values_become_none(self) -> None:
        scope = ContextScope.from_payload(
            {"scope": {"scene_id": "  ", "character_name": "", "dimension": None}}
        )
        self.assertIsNone(scope.scene_id)
        self.assertIsNone(scope.character_name)
        self.assertIsNone(scope.dimension)


class RepositorySceneContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = Database(self.root / "creative-claw.db")
        self.database.initialize()
        self.repository = Repository(self.database)
        self.repository.create_project("上下文项目", self.root, "context-project")
        self.before = self.repository.add_timeline_event(
            "context-project", "进入密室", "顾遥进入密室。", episode=18, scene=1
        )
        self.current = self.repository.add_timeline_event(
            "context-project", "拒交钥匙", "顾遥当场拒绝交出钥匙。", episode=18, scene=2
        )
        self.after = self.repository.add_timeline_event(
            "context-project", "离开密室", "顾遥离开密室。", episode=18, scene=3
        )
        self.repository.add_timeline_event(
            "context-project", "分支场景", "只属于另一个分支。", episode=18, scene=2, branch="alternate"
        )
        self.repository.upsert_ohlc(
            "context-project", "顾遥", "信任度", "scene", "E18-S02", 18.02,
            30, 45, 20, 25, timeline_event_id=self.current["id"]
        )
        self.repository.upsert_ohlc(
            "context-project", "林川", "决心", "scene", "E18-S02-B", 18.021,
            40, 60, 35, 55, timeline_event_id=self.current["id"]
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_get_timeline_event_is_project_and_branch_scoped(self) -> None:
        current = self.repository.get_timeline_event(
            "context-project", self.current["id"], branch="main"
        )
        self.assertEqual(current["id"], self.current["id"])
        self.assertIsNone(
            self.repository.get_timeline_event(
                "context-project", self.current["id"], branch="alternate"
            )
        )

    def test_timeline_context_returns_ordered_neighborhood(self) -> None:
        context = self.repository.timeline_context(
            "context-project", event_id=self.current["id"], branch="main", radius=1
        )
        self.assertEqual(context["current"]["id"], self.current["id"])
        self.assertEqual(
            [row["id"] for row in context["events"]],
            [self.before["id"], self.current["id"], self.after["id"]],
        )
        legacy = self.repository.timeline_context(
            "context-project", episode=18, scene=2, branch="main", radius=1
        )
        self.assertEqual(legacy["current"]["id"], self.current["id"])

    def test_ohlc_for_timeline_events_filters_character_and_dimension(self) -> None:
        rows = self.repository.ohlc_for_timeline_events(
            "context-project",
            [self.current["id"]],
            character_name="顾遥",
            dimension="信任度",
            branch="main",
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["timeline_event_id"], self.current["id"])
        self.assertEqual(rows[0]["character_name"], "顾遥")


if __name__ == "__main__":
    unittest.main()
