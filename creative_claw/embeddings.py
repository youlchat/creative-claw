from __future__ import annotations

import hashlib
import math
import os
import sys
from array import array
from dataclasses import dataclass
from typing import Protocol

import requests

from .text import lexical_tokens


class EmbeddingProvider(Protocol):
    name: str
    dimension: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


@dataclass(slots=True)
class HashEmbeddingProvider:
    """Deterministic offline embedding for zero-configuration local search.

    This is a signed hashing vector, not a neural model. It makes the complete
    retrieval pipeline usable offline while allowing a semantic provider to be
    swapped in without reworking storage or citations.
    """

    dimension: int = 384
    name: str = "hash-v1"

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        vector = [0.0] * self.dimension
        tokens = lexical_tokens(text)
        if not tokens:
            return vector
        for token in tokens:
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=16).digest()
            index = int.from_bytes(digest[:8], "little") % self.dimension
            sign = 1.0 if digest[8] & 1 else -1.0
            weight = 1.0 + min(len(token), 8) / 16.0
            vector[index] += sign * weight
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [value / norm for value in vector]


@dataclass(slots=True)
class OpenAICompatibleEmbeddingProvider:
    base_url: str
    api_key: str
    model: str
    dimension: int = 0
    timeout: float = 60.0
    name: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            self.name = f"openai-compatible:{self.model}"

    def _url(self) -> str:
        base = self.base_url.rstrip("/")
        if base.endswith("/embeddings"):
            return base
        if base.endswith("/v1"):
            return base + "/embeddings"
        return base + "/v1/embeddings"

    def embed(self, texts: list[str]) -> list[list[float]]:
        response = requests.post(
            self._url(),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json={"model": self.model, "input": texts},
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        ordered = sorted(payload["data"], key=lambda item: item.get("index", 0))
        vectors = [list(map(float, item["embedding"])) for item in ordered]
        if vectors:
            self.dimension = len(vectors[0])
        return vectors


def provider_from_env() -> EmbeddingProvider:
    base_url = os.getenv("CREATIVE_CLAW_EMBEDDING_BASE_URL")
    api_key = os.getenv("CREATIVE_CLAW_EMBEDDING_API_KEY")
    model = os.getenv("CREATIVE_CLAW_EMBEDDING_MODEL")
    if base_url and api_key and model:
        return OpenAICompatibleEmbeddingProvider(base_url=base_url, api_key=api_key, model=model)
    return HashEmbeddingProvider(dimension=int(os.getenv("CREATIVE_CLAW_HASH_DIM", "384")))


def pack_vector(values: list[float]) -> bytes:
    packed = array("f", values)
    if sys.byteorder != "little":
        packed.byteswap()
    return packed.tobytes()


def unpack_vector(blob: bytes | None, dimension: int | None) -> list[float]:
    if not blob:
        return []
    values = array("f")
    values.frombytes(blob)
    if sys.byteorder != "little":
        values.byteswap()
    result = values.tolist()
    if dimension and len(result) != dimension:
        return []
    return result


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        return 0.0
    return dot / (left_norm * right_norm)
