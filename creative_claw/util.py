from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def json_loads(value: str | None, default: Any = None) -> Any:
    if not value:
        return {} if default is None else default
    return json.loads(value)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def safe_output_path(project_root: Path, requested: str | Path) -> Path:
    root = project_root.resolve()
    candidate = Path(requested)
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = candidate.resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Output path must stay inside project root: {candidate}") from exc
    candidate.parent.mkdir(parents=True, exist_ok=True)
    return candidate


EPISODE_RE = re.compile(r"(?:第\s*(\d+)\s*[集章]|\bE(\d+)\b)", re.IGNORECASE)
SCENE_RE = re.compile(r"\bE(\d+)[-_]?S(\d+)\b", re.IGNORECASE)


def infer_story_locator(text: str, path: str = "") -> dict[str, Any]:
    sample = f"{path}\n{text[:800]}"
    scene_match = SCENE_RE.search(sample)
    episode_match = EPISODE_RE.search(sample)
    result: dict[str, Any] = {}
    if scene_match:
        episode = int(scene_match.group(1))
        scene = int(scene_match.group(2))
        result.update({"episode": episode, "scene": scene, "period_id": f"E{episode}-S{scene:02d}"})
    elif episode_match:
        episode = int(episode_match.group(1) or episode_match.group(2))
        result.update({"episode": episode, "period_id": f"E{episode}"})
    return result

