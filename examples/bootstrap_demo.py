from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from creative_claw.db import Database  # noqa: E402
from creative_claw.indexer import Indexer  # noqa: E402
from creative_claw.repository import Repository  # noqa: E402


CHARACTER = "沈霜"
DIMENSION = "知情度"
ACTOR = "bootstrap-demo"

SCENE_7 = """偏殿的风从旧窗纸缝里钻进来。沈霜隔着屏风，只听清一句：先帝遗孤仍在人世。说话的人随即离开，没有留下姓名，也没有提到齐尧。她把这条线索记入案卷，决定先核对文书，不提前确认任何人的身份。"""

SCENE_8 = """沈霜进文书房时，先把偏殿里带回来的疑心压进袖中。

“我只查书，不查人。”她对主簿说。

话音刚落，一个传令小吏撞开门，扶着门框喘道：“沈大人，齐尧是新来的小主——簿！”

沈霜抬眼。小吏努力补完最后一个字，屋里却先安静了半拍。与此同时，书吏从旧档里抽出一张破损登记，纸上只剩两个隔得很远的字：“遗……孤……”；齐尧恰好抱着一摞册页走进来。

人、称谓、残页，在同一刻撞到一起。沈霜的手按住剑柄：“齐主簿，解释一下。”

齐尧看了看小吏，又看了看残页：“他跑得太快，纸也烂得太慢。”

书吏终于从桌脚下找到了后半张登记。两片一拼，原文赫然是：“遗失孤本一册，移至西架避潮。”

小吏松了口气：“所以没有小主？”

“有小主簿。”齐尧纠正，“也有孤本。西架第三层，蓝布函套。”

沈霜看向西架。书果然在那里。齐尧答得太快，却也可能只是整理过书架；证据不足以确认更多。

齐尧问：“沈大人查的是遗孤，还是遗失的孤本？”

沈霜抽出登记册，面无表情地写下一行：“先把两样都登记上。”

她合上册子：“我说过，我只查书——至于人，是书自己带进来的。”"""


def _has_project(repository: Repository, project_id: str) -> bool:
    return any(project["id"] == project_id for project in repository.list_projects())


def _ensure_timeline(
    repository: Repository,
    project_id: str,
    *,
    label: str,
    description: str,
    episode: int,
    scene: int,
    evidence_chunk_id: str | None,
) -> bool:
    with repository.database.connect() as connection:
        exists = connection.execute(
            "SELECT 1 FROM timeline_events WHERE project_id=? AND branch='main' AND episode=? AND scene=? LIMIT 1",
            (project_id, episode, scene),
        ).fetchone()
    if exists:
        return False
    repository.add_timeline_event(
        project_id,
        label,
        description,
        story_time="景曜十三年冬",
        episode=episode,
        scene=scene,
        evidence_chunk_id=evidence_chunk_id,
        attrs={"demo": True, "editable_manuscript": True},
        actor=ACTOR,
    )
    return True


def _ensure_relation(
    repository: Repository,
    project_id: str,
    source_id: str,
    predicate: str,
    target_id: str,
    evidence_chunk_id: str | None,
) -> bool:
    with repository.database.connect() as connection:
        exists = connection.execute(
            "SELECT 1 FROM relations WHERE project_id=? AND branch='main' AND source_id=? AND predicate=? AND target_id=? LIMIT 1",
            (project_id, source_id, predicate, target_id),
        ).fetchone()
    if exists:
        return False
    repository.add_relation(
        project_id,
        source_id,
        predicate,
        target_id,
        evidence_chunk_id=evidence_chunk_id,
        attrs={"demo": True},
        actor=ACTOR,
    )
    return True


def _ensure_ohlc(repository: Repository, project_id: str) -> int:
    rows = [
        ("E18-S01", 18.01, 42, 50, 38, 47),
        ("E18-S02", 18.02, 47, 58, 45, 55),
        ("E18-S03", 18.03, 55, 81, 52, 76),
        ("E18-S08", 18.08, 76, 88, 61, 79),
    ]
    changed = 0
    for period_id, sort_key, open_value, high, low, close in rows:
        with repository.database.connect() as connection:
            existing = connection.execute(
                "SELECT open, high, low, close, parent_period_id, sort_key FROM ohlc_points WHERE project_id=? AND character_name=? AND dimension=? AND period_id=? AND branch='main'",
                (project_id, CHARACTER, DIMENSION, period_id),
            ).fetchone()
        desired = (float(open_value), float(high), float(low), float(close), "E18", float(sort_key))
        current = (
            tuple(existing[key] for key in ("open", "high", "low", "close", "parent_period_id", "sort_key"))
            if existing
            else None
        )
        if current == desired:
            continue
        repository.upsert_ohlc(
            project_id,
            CHARACTER,
            DIMENSION,
            "scene",
            period_id,
            sort_key,
            open_value,
            high,
            low,
            close,
            parent_period_id="E18",
            attrs={"demo": True, "scale": "0-100"},
            actor=ACTOR,
        )
        changed += 1
    return changed


def bootstrap(database_path: Path, project_root: Path, project_id: str) -> dict[str, Any]:
    database = Database(database_path)
    database.initialize()
    repository = Repository(database)
    project_root.mkdir(parents=True, exist_ok=True)

    created_project = False
    if not _has_project(repository, project_id):
        repository.create_project("Creative Claw 演示项目", project_root, project_id)
        created_project = True

    source = REPOSITORY_ROOT / "story-sources" / "E18-S08-喜剧续写正典.md"
    imported = Indexer(database).import_file(
        project_id,
        source,
        canon_status="canon",
        metadata={"episode": 18, "scene": 8, "purpose": "demo-canon"},
        actor=ACTOR,
    )
    with database.connect() as connection:
        evidence = connection.execute(
            "SELECT id FROM chunks WHERE document_id=? ORDER BY ordinal LIMIT 1",
            (imported.document_id,),
        ).fetchone()
    evidence_chunk_id = evidence["id"] if evidence else None

    entities_by_key = {
        (entity["name"], entity["entity_type"]): entity
        for entity in repository.list_entities(project_id)
    }
    entity_specs = [
        ("沈霜", "character", ["沈大人"], {"style": "克制、敏锐", "demo": True}),
        ("齐尧", "character", ["齐主簿"], {"style": "谨慎、字面化", "demo": True}),
        ("人物 K 线", "method", ["OHLC 叙事曲线"], {"typed": "OHLC", "demo": True}),
        ("连续叙事账本", "system", ["Canon Ledger"], {"append_only": True, "demo": True}),
    ]
    entities_added = 0
    for name, entity_type, aliases, attrs in entity_specs:
        key = (name, entity_type)
        if key not in entities_by_key:
            entities_by_key[key] = repository.upsert_entity(
                project_id, name, entity_type, aliases=aliases, attrs=attrs, actor=ACTOR
            )
            entities_added += 1

    relations_added = 0
    relations_added += _ensure_relation(
        repository, project_id, entities_by_key[("沈霜", "character")]["id"],
        "怀疑但未确认身份", entities_by_key[("齐尧", "character")]["id"], evidence_chunk_id,
    )
    relations_added += _ensure_relation(
        repository, project_id, entities_by_key[("人物 K 线", "method")]["id"],
        "变更写入", entities_by_key[("连续叙事账本", "system")]["id"], evidence_chunk_id,
    )

    timeline_added = int(_ensure_timeline(
        repository, project_id, label="E18-S07 偏殿偷听", description=SCENE_7,
        episode=18, scene=7, evidence_chunk_id=evidence_chunk_id,
    ))
    timeline_added += int(_ensure_timeline(
        repository, project_id, label="E18-S08 遗孤与孤本", description=SCENE_8,
        episode=18, scene=8, evidence_chunk_id=evidence_chunk_id,
    ))
    ohlc_changed = _ensure_ohlc(repository, project_id)

    stats = repository.knowledge_stats(project_id)
    return {
        "database": str(database.path),
        "project_id": project_id,
        "project_created": created_project,
        "document": {"path": str(source), "id": imported.document_id, "skipped": imported.skipped, "version": imported.version},
        "changes": {
            "entities_added": entities_added,
            "relations_added": relations_added,
            "timeline_events_added": timeline_added,
            "ohlc_rows_changed": ohlc_changed,
        },
        "totals": {"documents": stats["documents"], "chunks": stats["chunks"], **stats["structured"]},
        "ledger": stats["ledger"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Create an idempotent, repository-contained demo")
    parser.add_argument("--db", default=str(REPOSITORY_ROOT / ".creative-claw" / "demo.db"))
    parser.add_argument("--project-root", default=str(REPOSITORY_ROOT / ".creative-claw" / "projects" / "demo"))
    parser.add_argument("--project-id", default="demo")
    args = parser.parse_args()
    print(json.dumps(bootstrap(Path(args.db), Path(args.project_root), args.project_id), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
