from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from creative_claw.api import create_app


class PlaintextLlmConfigTests(unittest.TestCase):
    def test_config_is_saved_plaintext_and_restored_by_new_app(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "creative-claw.db"
            config_path = database_path.with_suffix(".llm.json")
            environment = {
                "CREATIVE_CLAW_LLM_API_KEY": "",
                "CREATIVE_CLAW_LLM_BASE_URL": "https://api.minimaxi.com/v1",
                "CREATIVE_CLAW_LLM_MODEL": "MiniMax-M3",
            }
            with patch.dict(os.environ, environment, clear=False):
                client = create_app(
                    database_path,
                    run_blueprint_jobs_inline=True,
                ).test_client()
                response = client.post(
                    "/v1/config/llm",
                    json={
                        "api_key": "plain-text-test-key",
                        "base_url": "https://api.minimaxi.com/v1",
                        "model": "MiniMax-M3",
                    },
                )

                self.assertEqual(response.status_code, 200)
                self.assertTrue(config_path.is_file())
                self.assertEqual(
                    json.loads(config_path.read_text(encoding="utf-8")),
                    {
                        "api_key": "plain-text-test-key",
                        "base_url": "https://api.minimaxi.com/v1",
                        "model": "MiniMax-M3",
                    },
                )
                self.assertNotIn("plain-text-test-key", response.get_data(as_text=True))

                for name in environment:
                    os.environ.pop(name, None)
                restarted_client = create_app(
                    database_path,
                    run_blueprint_jobs_inline=True,
                ).test_client()
                restored = restarted_client.get("/v1/config")

                self.assertEqual(restored.status_code, 200)
                self.assertTrue(restored.get_json()["llm"]["configured"])
                self.assertNotIn("plain-text-test-key", restored.get_data(as_text=True))

    def test_live_config_registers_blueprint_agents(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "creative-claw.db"
            environment = {
                "CREATIVE_CLAW_LLM_API_KEY": "",
                "CREATIVE_CLAW_LLM_BASE_URL": "https://api.minimaxi.com/v1",
                "CREATIVE_CLAW_LLM_MODEL": "MiniMax-M3",
            }
            with patch.dict(os.environ, environment, clear=False):
                client = create_app(
                    database_path,
                    run_blueprint_jobs_inline=True,
                ).test_client()
                project = client.post(
                    "/v1/projects",
                    json={
                        "id": "demo",
                        "name": "Blueprint Test",
                        "root_path": str(Path(temp_dir) / "project"),
                    },
                )
                self.assertEqual(project.status_code, 201)
                configured = client.post(
                    "/v1/config/llm",
                    json={
                        "api_key": "plain-text-test-key",
                        "base_url": "https://api.minimaxi.com/v1",
                        "model": "MiniMax-M3",
                    },
                )
                self.assertEqual(configured.status_code, 200)

                reference = client.post(
                    "/v1/projects/demo/blueprint-jobs/reference",
                    json={
                        "title": "Public-domain reference",
                        "text": "A social reversal exposes how status changes every relationship.",
                        "rights_basis": "public_domain",
                        "run_async": True,
                    },
                )

                self.assertEqual(reference.status_code, 202)
                self.assertEqual(reference.get_json()["status"], "pending")


if __name__ == "__main__":
    unittest.main()
