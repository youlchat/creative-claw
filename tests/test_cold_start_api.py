from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from creative_claw.api import create_app
from creative_claw.db import Database
from creative_claw.repository import Repository
from tests.test_cold_start import SequenceWriter, VALID_PREVIEW


class ColdStartApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = Database(self.root / "api.db")
        self.database.initialize()
        self.repository = Repository(self.database)
        self.repository.create_project("空项目", self.root, "empty")
        self.client = create_app(self.database.path).test_client()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_preview_is_read_only_and_apply_returns_full_snapshot(self) -> None:
        writer = SequenceWriter([json.dumps(VALID_PREVIEW, ensure_ascii=False)])
        with patch(
            "creative_claw.api.OpenAICompatibleColdStartWriter.from_env",
            return_value=writer,
        ):
            preview_response = self.client.post(
                "/v1/projects/empty/cold-start/preview",
                json={"prompt": "写一个原创民间幽默故事"},
            )

        self.assertEqual(preview_response.status_code, 200)
        snapshot_before_apply = self.repository.canvas_snapshot("empty")
        self.assertEqual(snapshot_before_apply["entities"], [])
        self.assertEqual(snapshot_before_apply["timeline"], [])
        self.assertEqual(snapshot_before_apply["ohlc"], [])

        apply_response = self.client.post(
            "/v1/projects/empty/cold-start/apply",
            json=preview_response.get_json(),
        )

        self.assertEqual(apply_response.status_code, 201)
        payload = apply_response.get_json()
        self.assertEqual(payload["summary"]["entities"], 3)
        self.assertEqual(payload["summary"]["scenes"], 6)
        self.assertEqual(payload["summary"]["ohlc"], 6)
        self.assertEqual(len(payload["snapshot"]["timeline"]), 6)

    def test_nonempty_project_returns_409_for_preview_and_apply(self) -> None:
        self.repository.upsert_entity("empty", "已有角色", "character")
        requests = (
            ("preview", {"prompt": "另一个故事"}),
            (
                "apply",
                {
                    "preview": VALID_PREVIEW,
                    "generation": {"prompt": "另一个故事", "model": "fake"},
                },
            ),
        )

        for action, body in requests:
            with self.subTest(action=action):
                response = self.client.post(
                    f"/v1/projects/empty/cold-start/{action}", json=body
                )
                self.assertEqual(response.status_code, 409)
                self.assertIn("冷启动仅适用于空项目", response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
