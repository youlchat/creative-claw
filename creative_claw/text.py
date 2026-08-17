from __future__ import annotations

import re
from collections.abc import Iterable

from .models import ChunkDraft, ExtractedUnit
from .util import infer_story_locator


CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
WORD_RE = re.compile(r"[a-zA-Z0-9_]+")
SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？!?；;\.])\s*|\n+")


def lexical_tokens(text: str) -> list[str]:
    lowered = text.lower()
    tokens = WORD_RE.findall(lowered)
    for run in CJK_RE.findall(lowered):
        if len(run) == 1:
            tokens.append(run)
            continue
        tokens.extend(run[index : index + 2] for index in range(len(run) - 1))
        tokens.extend(run[index : index + 3] for index in range(len(run) - 2))
    return tokens


def fts_document(text: str) -> str:
    return " ".join(lexical_tokens(text))


def fts_query(text: str, max_terms: int = 24) -> str:
    seen: set[str] = set()
    terms: list[str] = []
    for token in lexical_tokens(text):
        if token in seen:
            continue
        seen.add(token)
        terms.append('"' + token.replace('"', '""') + '"')
        if len(terms) >= max_terms:
            break
    return " OR ".join(terms)


def _split_long_text(text: str, max_chars: int, overlap_chars: int) -> list[str]:
    clean = re.sub(r"[ \t]+", " ", text).strip()
    if not clean:
        return []
    if len(clean) <= max_chars:
        return [clean]

    sentences = [part.strip() for part in SENTENCE_SPLIT_RE.split(clean) if part.strip()]
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        if len(sentence) > max_chars:
            if current:
                chunks.append(current.strip())
                current = ""
            start = 0
            while start < len(sentence):
                end = min(len(sentence), start + max_chars)
                chunks.append(sentence[start:end].strip())
                if end == len(sentence):
                    break
                start = max(start + 1, end - overlap_chars)
            continue
        candidate = sentence if not current else current + "\n" + sentence
        if len(candidate) <= max_chars:
            current = candidate
            continue
        chunks.append(current.strip())
        overlap = current[-overlap_chars:] if overlap_chars else ""
        current = (overlap + "\n" + sentence).strip()
    if current:
        chunks.append(current.strip())
    return chunks


def chunk_units(
    units: Iterable[ExtractedUnit],
    *,
    source_path: str,
    max_chars: int = 900,
    overlap_chars: int = 120,
) -> list[ChunkDraft]:
    drafts: list[ChunkDraft] = []
    ordinal = 0
    for unit_index, unit in enumerate(units):
        pieces = _split_long_text(unit.text, max_chars, overlap_chars)
        for piece_index, piece in enumerate(pieces):
            metadata = dict(unit.metadata)
            metadata.setdefault("unit", unit_index)
            metadata.setdefault("piece", piece_index)
            for key, value in infer_story_locator(piece, source_path).items():
                metadata.setdefault(key, value)
            drafts.append(ChunkDraft(text=piece, metadata=metadata, ordinal=ordinal))
            ordinal += 1
    return drafts

