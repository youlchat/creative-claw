from __future__ import annotations

import unittest

from creative_claw.blueprint_similarity import assess_similarity


class BlueprintSimilarityTests(unittest.TestCase):
    def test_expression_boundary_blocks_24_chinese_and_80_latin_characters(self) -> None:
        chinese_24 = "春夏秋冬风雨雷电山川湖海星月云霞花鸟鱼虫天地人和"
        self.assertEqual(len(chinese_24), 24)
        blocked = assess_similarity(
            f"候选开头{chinese_24}候选结尾",
            f"参考开头{chinese_24}参考结尾",
            candidate_beats=[],
            reference_beats=[],
            mappings=[],
        )
        allowed = assess_similarity(
            f"候选讲述机械师穿越沙丘并校准钟阵随后返回城邦{chinese_24[:-1]}甲又转向地下河记录陌生回声",
            f"参考描写药师沿冰川寻找种子并修复温室最后离开村落{chinese_24[:-1]}乙再登上海岛观察候鸟迁徙",
            candidate_beats=[],
            reference_beats=[],
            mappings=[],
        )
        latin_80 = "abcdefghijklmnopqrstuvwxyz" * 3 + "ab"
        self.assertEqual(len(latin_80), 80)
        latin = assess_similarity(
            f"candidate-{latin_80}-tail",
            f"reference-{latin_80}-end",
            candidate_beats=[],
            reference_beats=[],
            mappings=[],
        )
        self.assertEqual(blocked.gate_status, "blocked")
        self.assertEqual(latin.gate_status, "blocked")
        self.assertNotEqual(allowed.gate_status, "blocked")

    def test_expression_blocks_rare_phrase_and_combined_ngram_lcs(self) -> None:
        rare = assess_similarity(
            "他终于找到赤铜月桂机关并将其关闭。",
            "赤铜月桂机关藏在旧宫深处。",
            candidate_beats=[],
            reference_beats=[],
            mappings=[],
            rare_phrases=["赤铜月桂机关"],
        )
        copied = "a1b2c3d4e5" * 9
        ngram = assess_similarity(
            copied + "候选",
            copied + "参考",
            candidate_beats=[],
            reference_beats=[],
            mappings=[],
        )
        self.assertEqual(rare.gate_status, "blocked")
        self.assertTrue(any(item["rule"] == "rare_phrase" for item in rare.findings))
        self.assertEqual(ngram.gate_status, "blocked")

    def test_structure_requires_remediation_but_abstract_mechanism_is_allowed(self) -> None:
        reference = [
            {"role_function": f"r{i}", "event_function": f"e{i}", "outcome": f"o{i}"}
            for i in range(10)
        ]
        candidate = [dict(item) if i < 7 else {"role_function": "x", "event_function": "y", "outcome": "z"}
                     for i, item in enumerate(reference)]
        risky = assess_similarity(
            "全新的目标表达",
            "完全不同的参考表达",
            candidate_beats=candidate,
            reference_beats=reference,
            mappings=[{"action": "transform"} for _ in range(2)] + [{"action": "preserve"} for _ in range(8)],
        )
        remediated = assess_similarity(
            "全新的目标表达",
            "完全不同的参考表达",
            candidate_beats=candidate,
            reference_beats=reference,
            mappings=[{"action": "transform"} for _ in range(3)] + [{"action": "preserve"} for _ in range(7)],
        )
        abstract = assess_similarity(
            "主角在雪原修复信标，决定承担陌生人的损失。",
            "学徒在海港修补灯塔，选择承受家族的代价。",
            candidate_beats=[{"mechanism": "goal-obstacle-result"}],
            reference_beats=[{"mechanism": "goal-obstacle-result"}],
            mappings=[{"action": "transform"}],
        )
        self.assertEqual(risky.gate_status, "review_required")
        self.assertTrue(risky.structure["high_structural_risk"])
        self.assertEqual(remediated.gate_status, "passed")
        self.assertEqual(abstract.gate_status, "passed")
        self.assertTrue(abstract.mechanism["allowed"])

    def test_expression_uses_sliding_windows_and_reports_candidate_and_reference_locations(self) -> None:
        shared = "abcdefghij" * 6
        candidate = ("c" * 500) + (shared + "k" * 50) + ("d" * 500)
        reference = ("r" * 500) + (shared + "z" * 50) + ("s" * 500)

        result = assess_similarity(
            candidate, reference, candidate_beats=[], reference_beats=[], mappings=[]
        )

        finding = next(item for item in result.findings if item["rule"] == "ngram_lcs")
        self.assertEqual(result.gate_status, "blocked")
        self.assertEqual(set(finding["candidate_range"]), {"start", "end"})
        self.assertEqual(set(finding["reference_range"]), {"start", "end"})
        self.assertGreaterEqual(finding["candidate_range"]["start"], 400)
        self.assertGreaterEqual(finding["reference_range"]["start"], 400)

    def test_structural_match_ratio_uses_reference_denominator(self) -> None:
        reference = [
            {"role_function": f"r{i}", "event_function": f"e{i}", "outcome": f"o{i}"}
            for i in range(10)
        ]
        result = assess_similarity(
            "全新候选", "不同参考",
            candidate_beats=[dict(reference[0])], reference_beats=reference,
            mappings=[{"action": "preserve"} for _ in range(10)],
        )
        self.assertEqual(result.structure["match_ratio"], 0.1)

    def test_style_fingerprint_hits_are_counted_separately_with_locations(self) -> None:
        fingerprint = "断弦回声折返"
        candidate = f"全新开场，随后出现{fingerprint}，再转入另一条叙事线。"
        reference = f"旧作在中段使用{fingerprint}作为句法指纹。"
        result = assess_similarity(
            candidate, reference, candidate_beats=[], reference_beats=[], mappings=[],
            rare_phrases=[], style_fingerprints=[fingerprint],
        )
        self.assertEqual(result.gate_status, "blocked")
        self.assertEqual(result.expression["rare_phrase_hit_count"], 0)
        self.assertEqual(result.expression["style_fingerprint_hit_count"], 1)
        finding = next(item for item in result.findings if item["rule"] == "style_fingerprint")
        self.assertGreater(finding["candidate_range"]["end"], finding["candidate_range"]["start"])
        self.assertGreater(finding["reference_range"]["end"], finding["reference_range"]["start"])


if __name__ == "__main__":
    unittest.main()
