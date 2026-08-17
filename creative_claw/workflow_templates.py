from __future__ import annotations

import json
import sqlite3
from typing import Any


def _stage(
    key: str,
    name: str,
    description: str,
    artifact_type: str,
) -> dict[str, Any]:
    return {
        "key": key,
        "name": name,
        "description": description,
        "entry_criteria": [],
        "completion_criteria": {
            "required_artifact_types": [artifact_type],
        },
    }


NOVEL_STAGES = [
    _stage("project_positioning", "立项与定位", "明确题材、受众、规模与创作边界。", "project_brief"),
    _stage("research_materials", "研究与素材", "整理研究来源、事实边界与待核问题。", "source_collection"),
    _stage("story_bible", "故事圣经", "确定核心冲突、正典事实与开放问题。", "story_bible"),
    _stage("book_structure", "全书结构", "建立全书主线、阶段转折与结局方向。", "book_outline"),
    _stage("volume_structure", "卷级结构", "拆分卷目标、冲突升级与卷末状态。", "volume_outline"),
    _stage("chapter_structure", "章节结构", "明确章节目标、信息增量和前后依赖。", "chapter_card"),
    _stage("scene_design", "场景设计", "设计视角、目标、阻力、转折与结果。", "scene_card"),
    _stage("manuscript_writing", "正文写作", "完成可进入编辑流程的章节正文。", "manuscript"),
    _stage("developmental_editing", "发展编辑", "检查结构、人物弧光和叙事推进。", "developmental_review"),
    _stage("continuity_review", "连续性审阅", "核对正典、时间线、人物状态与伏笔。", "continuity_review"),
    _stage("style_editing", "文风编辑", "统一叙述声音、节奏和语言质量。", "style_review"),
    _stage("proofreading_final", "校对与终审", "完成文字校对、终审问题处理与版本确认。", "proofreading_report"),
    _stage("locking_export", "锁稿与导出", "锁定通过审阅的版本并生成交付包。", "locked_package"),
]


VERTICAL_SHORT_DRAMA_STAGES = [
    _stage("commercial_positioning", "项目立项与商业定位", "明确题材、受众、平台和商业目标。", "project_brief"),
    _stage("rights_compliance_precheck", "版权、改编与合规预检", "记录权利来源、改编边界和适用规则。", "rights_record"),
    _stage("story_bible", "短剧故事圣经", "确定核心冲突、人物关系与正典事实。", "story_bible"),
    _stage("overall_structure", "总体故事和阶段结构", "建立全剧阶段目标、升级路径与结局。", "series_structure"),
    _stage("series_outline", "总集纲", "分配各集功能、主要事件和悬念推进。", "series_outline"),
    _stage("episode_outline", "分集大纲", "明确每集目标、冲突、钩子与结尾。", "episode_card"),
    _stage("scene_beat_design", "场次和节拍设计", "拆分场次、节拍、信息释放和情绪变化。", "beat_sheet"),
    _stage("literary_script", "剧本文学稿", "完成可审阅的分集文学剧本。", "literary_script"),
    _stage("hook_retention_review", "钩子、节奏和留存审阅", "依据项目目标审阅开场、转折和集尾钩子。", "hook_review"),
    _stage("continuity_character_review", "连续性与人物状态审阅", "核对时间线、人物状态和跨集连续性。", "continuity_review"),
    _stage("content_compliance_review", "内容合规审阅", "按地区、平台和规则版本记录合规问题。", "compliance_review"),
    _stage("production_feasibility_review", "制作可行性和成本审阅", "评估场景、人物、道具和拍摄成本风险。", "production_review"),
    _stage("shooting_handoff", "拍摄稿与生产交接", "形成拍摄稿和制作部门需要的清单。", "shooting_script"),
    _stage("final_cut_feedback", "成片变化回写与终审资料", "记录成片变化并同步终审资料。", "final_cut_record"),
    _stage("filing_release_lock", "锁稿、备案资料与发行版本", "锁定发行版本并整理备案与发行资料。", "release_package"),
    _stage("release_retrospective", "上线复盘", "记录上线表现、创作复盘和后续提案。", "retrospective"),
]


BUILTIN_WORKFLOW_TEMPLATES = (
    {
        "id": "wft_novel_v1",
        "template_key": "novel",
        "version": 1,
        "media_type": "novel",
        "name": "长篇小说标准流程",
        "description": "从立项、结构和正文写作到编辑、终审与锁稿的标准长篇小说流程。",
        "stages": NOVEL_STAGES,
    },
    {
        "id": "wft_vertical_short_drama_v1",
        "template_key": "vertical_short_drama",
        "version": 1,
        "media_type": "vertical_short_drama",
        "name": "竖屏短剧标准流程",
        "description": "从商业定位和合规预检到文学稿、制作交接、发行与复盘的标准短剧流程。",
        "stages": VERTICAL_SHORT_DRAMA_STAGES,
    },
)


def install_builtin_templates(connection: sqlite3.Connection) -> None:
    for template in BUILTIN_WORKFLOW_TEMPLATES:
        definition = {
            "template_key": template["template_key"],
            "version": template["version"],
            "media_type": template["media_type"],
            "name": template["name"],
            "description": template["description"],
            "stages": template["stages"],
        }
        connection.execute(
            """
            INSERT OR IGNORE INTO workflow_templates(
                id, template_key, version, media_type, name, description,
                definition_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                template["id"],
                template["template_key"],
                template["version"],
                template["media_type"],
                template["name"],
                template["description"],
                json.dumps(definition, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                "2026-07-29T00:00:00+00:00",
            ),
        )
