from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any


_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_LATIN_RE = re.compile(r"[a-z0-9]")


def _normalize(text: str) -> str:
    return "".join(character.lower() for character in str(text) if character.isalnum())


def _normalize_with_positions(text: str) -> tuple[str, list[int]]:
    characters: list[str] = []
    positions: list[int] = []
    for index, character in enumerate(str(text)):
        if character.isalnum():
            characters.append(character.lower())
            positions.append(index)
    return "".join(characters), positions


def _source_range(positions: list[int], start: int, end: int) -> dict[str, int]:
    if not positions or end <= start:
        return {"start": 0, "end": 0}
    return {"start": positions[start], "end": positions[min(end - 1, len(positions) - 1)] + 1}


def _window_metrics(candidate: str, reference: str, *, size: int = 120, step: int = 40) -> dict[str, Any]:
    best = {"jaccard": 0.0, "lcs_ratio": 0.0, "candidate_start": 0, "candidate_end": len(candidate),
            "reference_start": 0, "reference_end": len(reference)}
    candidate_starts = list(range(0, max(1, len(candidate) - size + 1), step)) or [0]
    reference_starts = list(range(0, max(1, len(reference) - size + 1), step)) or [0]
    if candidate_starts[-1] != max(0, len(candidate) - size):
        candidate_starts.append(max(0, len(candidate) - size))
    if reference_starts[-1] != max(0, len(reference) - size):
        reference_starts.append(max(0, len(reference) - size))
    for candidate_start in candidate_starts:
        candidate_window = candidate[candidate_start : candidate_start + size]
        candidate_grams = _ngrams(candidate_window)
        for reference_start in reference_starts:
            reference_window = reference[reference_start : reference_start + size]
            reference_grams = _ngrams(reference_window)
            union = candidate_grams | reference_grams
            jaccard = len(candidate_grams & reference_grams) / len(union) if union else 0.0
            match = SequenceMatcher(None, candidate_window, reference_window, autojunk=False).find_longest_match()
            lcs_ratio = match.size / max(1, min(len(candidate_window), len(reference_window)))
            score = min(jaccard / 0.32, lcs_ratio / 0.45)
            best_score = min(best["jaccard"] / 0.32, best["lcs_ratio"] / 0.45)
            if score > best_score:
                best = {
                    "jaccard": jaccard, "lcs_ratio": lcs_ratio,
                    "candidate_start": candidate_start, "candidate_end": candidate_start + len(candidate_window),
                    "reference_start": reference_start, "reference_end": reference_start + len(reference_window),
                }
    return best


def _ngrams(text: str, size: int = 5) -> set[str]:
    if len(text) < size:
        return set()
    return {text[index : index + size] for index in range(len(text) - size + 1)}


@dataclass(frozen=True, slots=True)
class SimilarityAssessment:
    expression: dict[str, Any]
    structure: dict[str, Any]
    mechanism: dict[str, Any]
    gate_status: str
    findings: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "expression": self.expression,
            "structure": self.structure,
            "mechanism": self.mechanism,
            "gate_status": self.gate_status,
            "findings": self.findings,
        }


def assess_similarity(
    candidate: str,
    reference: str,
    *,
    candidate_beats: list[dict],
    reference_beats: list[dict],
    mappings: list[dict],
    rare_phrases: list[str] | None = None,
    style_fingerprints: list[str] | None = None,
) -> SimilarityAssessment:
    normalized_candidate, candidate_positions = _normalize_with_positions(candidate)
    normalized_reference, reference_positions = _normalize_with_positions(reference)
    matcher = SequenceMatcher(None, normalized_candidate, normalized_reference, autojunk=False)
    longest = matcher.find_longest_match()
    shared = normalized_candidate[longest.a : longest.a + longest.size]
    shared_chinese = len(_CJK_RE.findall(shared))
    shared_latin = len(_LATIN_RE.findall(shared)) if shared_chinese == 0 else 0
    window = _window_metrics(normalized_candidate, normalized_reference)
    jaccard = float(window["jaccard"])
    lcs_ratio = float(window["lcs_ratio"])
    findings: list[dict[str, Any]] = []
    expression_blocked = False
    if shared_chinese >= 24 or shared_latin >= 80:
        expression_blocked = True
        findings.append(
            {
                "layer": "expression",
                "rule": "longest_common_substring",
                "shared_chinese": shared_chinese,
                "shared_latin": shared_latin,
                "candidate_range": _source_range(candidate_positions, longest.a, longest.a + longest.size),
                "reference_range": _source_range(reference_positions, longest.b, longest.b + longest.size),
            }
        )
    if jaccard >= 0.32 and lcs_ratio >= 0.45:
        expression_blocked = True
        findings.append(
            {"layer": "expression", "rule": "ngram_lcs", "jaccard_5gram": jaccard,
             "lcs_ratio": lcs_ratio,
             "candidate_range": _source_range(candidate_positions, int(window["candidate_start"]), int(window["candidate_end"])),
             "reference_range": _source_range(reference_positions, int(window["reference_start"]), int(window["reference_end"]))}
        )
    normalized_phrases = [phrase for phrase in (rare_phrases or []) if str(phrase).strip()]
    hits = [phrase for phrase in normalized_phrases if _normalize(phrase) in normalized_candidate]
    if hits:
        expression_blocked = True
        findings.append(
            {"layer": "expression", "rule": "rare_phrase", "hit_count": len(hits)}
        )
    fingerprint_hits: list[str] = []
    for fingerprint in style_fingerprints or []:
        normalized_fingerprint = _normalize(fingerprint)
        if not normalized_fingerprint:
            continue
        candidate_start = normalized_candidate.find(normalized_fingerprint)
        reference_start = normalized_reference.find(normalized_fingerprint)
        if candidate_start < 0 or reference_start < 0:
            continue
        fingerprint_hits.append(str(fingerprint))
        expression_blocked = True
        findings.append({
            "layer": "expression",
            "rule": "style_fingerprint",
            "hit_count": 1,
            "candidate_range": _source_range(
                candidate_positions, candidate_start, candidate_start + len(normalized_fingerprint)
            ),
            "reference_range": _source_range(
                reference_positions, reference_start, reference_start + len(normalized_fingerprint)
            ),
        })

    comparable = len(reference_beats)
    matches = 0
    for candidate_beat, reference_beat in zip(candidate_beats, reference_beats):
        values = [
            (candidate_beat.get(key), reference_beat.get(key))
            for key in ("role_function", "event_function", "outcome")
        ]
        if all(left not in (None, "") and left == right for left, right in values):
            matches += 1
    match_ratio = matches / max(1, len(reference_beats))
    mapped = [item for item in mappings if item.get("action") in {"preserve", "transform"}]
    transform_ratio = (
        sum(item.get("action") == "transform" for item in mapped) / len(mapped) if mapped else 1.0
    )
    structural_risk = comparable > 0 and match_ratio >= 0.70 and transform_ratio < 0.30
    if structural_risk:
        findings.append(
            {
                "layer": "structure",
                "rule": "ordered_beat_mapping",
                "match_ratio": match_ratio,
                "transform_ratio": transform_ratio,
            }
        )
    mechanism_overlap = bool(candidate_beats and reference_beats) and any(
        left.get("mechanism") and left.get("mechanism") == right.get("mechanism")
        for left, right in zip(candidate_beats, reference_beats)
    )
    expression = {
        "blocked": expression_blocked,
        "longest_common_length": longest.size,
        "shared_chinese": shared_chinese,
        "shared_latin": shared_latin,
        "jaccard_5gram": jaccard,
        "lcs_ratio": lcs_ratio,
        "rare_phrase_hit_count": len(hits),
        "style_fingerprint_hit_count": len(fingerprint_hits),
    }
    structure = {
        "high_structural_risk": structural_risk,
        "comparable_beats": comparable,
        "matched_beats": matches,
        "match_ratio": match_ratio,
        "transform_ratio": transform_ratio,
    }
    mechanism = {
        "allowed": True,
        "abstract_overlap": mechanism_overlap,
        "policy": "abstract narrative mechanisms do not constitute expression copying",
    }
    gate_status = "blocked" if expression_blocked else "review_required" if structural_risk else "passed"
    return SimilarityAssessment(expression, structure, mechanism, gate_status, findings)
