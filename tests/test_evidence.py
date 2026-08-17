from __future__ import annotations

import unittest

from creative_claw.evidence import build_evidence_refs, validate_citations


class EvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.refs = build_evidence_refs(
            sources=[
                {
                    "document_id": "doc-1",
                    "chunk_id": "chunk-1",
                    "title": "人物圣经",
                    "snippet": "顾遥拒绝交出钥匙。",
                    "locator": {"episode": 18, "scene": 2},
                }
            ],
            graph={
                "entities": [{"id": "ent-1", "name": "顾遥", "entity_type": "character"}],
                "relations": [],
            },
            timeline=[
                {
                    "id": "time-1",
                    "label": "拒交钥匙",
                    "description": "顾遥当场拒绝。",
                    "episode": 18,
                    "scene": 2,
                }
            ],
            ohlc=[
                {
                    "id": "ohlc-1",
                    "character_name": "顾遥",
                    "dimension": "信任度",
                    "open": 30,
                    "high": 45,
                    "low": 20,
                    "close": 25,
                    "timeline_event_id": "time-1",
                }
            ],
        )

    def test_assigns_type_specific_ids(self) -> None:
        self.assertEqual([row["ref"] for row in self.refs], ["S1", "G1", "T1", "K1"])
        self.assertEqual(
            [row["kind"] for row in self.refs],
            ["source", "graph", "timeline", "kline"],
        )

    def test_validation_reports_unknown_and_unused_refs(self) -> None:
        result = validate_citations("顾遥拒绝交钥匙。[S1][T1] 状态下降。[K9]", self.refs)
        self.assertEqual(result["used"], ["S1", "T1", "K9"])
        self.assertEqual(result["unknown"], ["K9"])
        self.assertIn("G1", result["unused"])
        self.assertFalse(result["valid"])

    def test_validation_accepts_known_refs(self) -> None:
        result = validate_citations("顾遥拒绝交钥匙。[S1][T1]", self.refs)
        self.assertTrue(result["valid"])
        self.assertEqual(result["unknown"], [])

    def test_optional_version_rule_and_issue_prefixes(self) -> None:
        refs = build_evidence_refs(
            sources=[], graph={"entities": [], "relations": []}, timeline=[], ohlc=[],
            versions=[{"id": "version-1", "title": "正文第二版"}],
            rules=[{"id": "rule-1", "title": "锁稿规则"}],
            issues=[{"id": "issue-1", "title": "人物动机冲突"}],
        )
        self.assertEqual([row["ref"] for row in refs], ["V1", "R1", "I1"])


if __name__ == "__main__":
    unittest.main()
