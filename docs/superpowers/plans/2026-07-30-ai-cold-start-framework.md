# AI 冷启动框架搭建 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在右侧 AI 助手新增“冷启动框架搭建”模式，让空项目从一句创作意图生成可预览、可整包采用的原创轻量故事骨架。

**Architecture:** 新建 `cold_start.py` 作为独立领域边界，负责空项目判断、结构校验、模型格式修复和单事务采用；普通 RAG `/chat` 保持不变。浏览器新增可由 Node 测试的 `cold-start-state.js`，`app.js` 只负责模式切换、请求、DOM 渲染与刷新；两个专用 API 分别承担“只预览不写入”和“再次校验后原子写入”。

**Tech Stack:** Python 3.11+、Flask 3、SQLite、`requests`、原生 JavaScript、Node `node:test`、Playwright。

## Global Constraints

- 入口必须位于右侧 AI 助手工作模式下拉框，与“分析”“一致性检查”“续写”“改写”并列。
- 只允许空项目；非空项目的预览和采用都返回 HTTP 409，且不得追加或覆盖现有数据。
- 默认结构为 3–5 个实体、必要关系、6–8 个场景卡、1 名主人公和每场一根关联 OHLC K 线。
- 预览阶段不得写数据库；采用阶段必须在一个 SQLite 事务中写入项目标题、实体、关系、场景、K 线和 `cold_start.applied` 账本事件。
- 场景只保存卡片级摘要，属性包含 `status=outline` 与 `format=scene_card`，不生成完整正文。
- 参照已有作品或人物时只能使用抽象叙事机制，禁止复刻专有名称、完整情节、标志性台词或连续表达。
- 模型响应可自动修复一次；第二次仍无效则失败且不写入。
- 当前 `creative-claw` 目录不是 Git 仓库：不要初始化仓库。每个任务保留建议提交信息，但在当前环境跳过 `git commit`。

---

## File Structure

- Create `creative_claw/cold_start.py`：冷启动结构规范化、空项目检查、模型预览编排和原子采用。
- Modify `creative_claw/llm.py`：增加只返回 JSON 文本的 OpenAI-compatible 冷启动模型适配器。
- Modify `creative_claw/api.py`：注册冷启动服务、409 错误映射和两个专用端点。
- Create `tests/test_cold_start.py`：领域校验、模型修复、空项目判断与事务测试。
- Create `tests/test_cold_start_api.py`：HTTP 契约、预览零写入、采用成功与冲突测试。
- Create `creative_claw/web/cold-start-state.js`：空项目判断、模式文案和预览摘要的纯函数。
- Create `tests/js/cold-start-state.test.cjs`：浏览器纯状态行为测试。
- Modify `creative_claw/web/index.html`：增加模式选项并加载纯状态模块。
- Modify `creative_claw/web/app.js`：冷启动状态、预览卡、重新生成、采用和画布刷新。
- Modify `creative_claw/web/app.css`：结构化预览卡样式和响应式布局。
- Create `scripts/e2e_cold_start.py`：本地假模型 + 真实 Flask/SQLite + Playwright 验收。
- Modify `README.md`：记录用户操作、API、原创性边界和验证命令。

---

### Task 1: 结构化预览、确定性规范化与一次修复

**Files:**
- Create: `creative_claw/cold_start.py`
- Modify: `creative_claw/llm.py`
- Create: `tests/test_cold_start.py`

**Interfaces:**
- Produces: `ColdStartWriter` protocol with `model: str` and `generate(prompt: str, *, repair: dict[str, str] | None = None) -> str`
- Produces: `normalize_preview(value: Any) -> dict[str, Any]`
- Produces: `parse_preview_text(text: str) -> dict[str, Any]`
- Produces: `ColdStartService.preview(project_id: str, prompt: str, writer: ColdStartWriter) -> dict[str, Any]`
- Produces: `OpenAICompatibleColdStartWriter.from_env()` and `generate(prompt: str, *, repair: dict[str, str] | None = None) -> str`
- Consumes: `Database`, existing runtime model environment, `_chat_url()` and `_strip_reasoning_blocks()` from `llm.py`

- [ ] **Step 1: Write failing normalization tests**

Before writing each body, name the mutation caught: removing the entity/scene bounds, accepting a missing reference, or leaving a broken K-line opening must fail at least one test. Add a literal six-scene fixture to `tests/test_cold_start.py` and assertions with hand-derived values:

```python
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from creative_claw.cold_start import normalize_preview, parse_preview_text


VALID_PREVIEW = {
    "title": "铜铃镇的聪明账单",
    "premise": "机智小贩用六次反转让贪心税吏为自己的规则买单。",
    "protagonist_key": "hero",
    "kline_dimension": "解局主动权",
    "entities": [
        {"key": "hero", "name": "艾山", "entity_type": "character", "description": "冷静机智的小贩"},
        {"key": "collector", "name": "罗班", "entity_type": "character", "description": "贪心的税吏"},
        {"key": "bell", "name": "铜铃市集", "entity_type": "location", "description": "交易发生的公共市集"},
    ],
    "relations": [
        {"source_key": "hero", "predicate": "智斗", "target_key": "collector"}
    ],
    "scenes": [
        {"title": "怪税告示", "summary": "罗班宣布影子也要纳税。", "story_time": "清晨", "entity_keys": ["hero", "collector", "bell"], "ohlc": {"open": 30.04, "high": 42.06, "low": 24.04, "close": 38.04}},
        {"title": "主动交账", "summary": "艾山带来一张没有数字的账单。", "story_time": "上午", "entity_keys": ["hero", "collector"], "ohlc": {"open": 12, "high": 52, "low": 35, "close": 48}},
        {"title": "规则套索", "summary": "罗班亲口确认声音也能抵税。", "story_time": "正午", "entity_keys": ["hero", "collector"], "ohlc": {"open": 48, "high": 64, "low": 44, "close": 60}},
        {"title": "铜钱回声", "summary": "艾山摇响钱袋，以声音支付影子税。", "story_time": "午后", "entity_keys": ["hero", "collector", "bell"], "ohlc": {"open": 60, "high": 78, "low": 56, "close": 73}},
        {"title": "众人作证", "summary": "市民复述罗班刚刚确认的规则。", "story_time": "傍晚", "entity_keys": ["hero", "collector", "bell"], "ohlc": {"open": 73, "high": 88, "low": 70, "close": 84}},
        {"title": "税吏买单", "summary": "罗班撤下告示并退还错收的钱。", "story_time": "日落", "entity_keys": ["hero", "collector", "bell"], "ohlc": {"open": 84, "high": 94, "low": 80, "close": 90}},
    ],
}


class ColdStartNormalizationTests(unittest.TestCase):
    def test_normalizes_rounding_and_continuous_ohlc_without_missing_references(self) -> None:
        normalized = normalize_preview(VALID_PREVIEW)
        self.assertEqual(normalized["scenes"][0]["ohlc"], {"open": 30.0, "high": 42.1, "low": 24.0, "close": 38.0})
        self.assertEqual(normalized["scenes"][1]["ohlc"], {"open": 38.0, "high": 52.0, "low": 35.0, "close": 48.0})

    def test_rejects_entity_scene_and_reference_contract_breaks(self) -> None:
        cases = []
        too_few_entities = json.loads(json.dumps(VALID_PREVIEW, ensure_ascii=False))
        too_few_entities["entities"] = too_few_entities["entities"][:2]
        cases.append(too_few_entities)
        too_few_scenes = json.loads(json.dumps(VALID_PREVIEW, ensure_ascii=False))
        too_few_scenes["scenes"] = too_few_scenes["scenes"][:5]
        cases.append(too_few_scenes)
        unknown_reference = json.loads(json.dumps(VALID_PREVIEW, ensure_ascii=False))
        unknown_reference["scenes"][0]["entity_keys"] = ["missing"]
        cases.append(unknown_reference)
        for value in cases:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    normalize_preview(value)

    def test_parses_json_code_fence_and_rejects_missing_ohlc_field(self) -> None:
        parsed = parse_preview_text("```json\n" + json.dumps(VALID_PREVIEW, ensure_ascii=False) + "\n```")
        self.assertEqual(parsed["title"], "铜铃镇的聪明账单")
        invalid = json.loads(json.dumps(VALID_PREVIEW, ensure_ascii=False))
        del invalid["scenes"][0]["ohlc"]["low"]
        with self.assertRaises(ValueError):
            normalize_preview(invalid)
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```powershell
python -m unittest tests.test_cold_start.ColdStartNormalizationTests -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'creative_claw.cold_start'`.

- [ ] **Step 3: Implement the minimal parser and normalizer**

In `creative_claw/cold_start.py`, implement focused helpers. Do not add database writes in this step:

```python
ALLOWED_ENTITY_TYPES = frozenset({"character", "location", "object", "organization", "canon_fact"})


def parse_preview_text(text: str) -> dict[str, Any]:
    content = str(text or "").strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", content, re.DOTALL | re.IGNORECASE)
    if fenced:
        content = fenced.group(1)
    value = json.loads(content)
    if not isinstance(value, dict):
        raise ValueError("Cold-start preview must be a JSON object")
    return value


def normalize_preview(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("Cold-start preview must be an object")
    title = _required_text(value.get("title"), "title")
    premise = _required_text(value.get("premise"), "premise")
    protagonist_key = _required_text(value.get("protagonist_key"), "protagonist_key")
    dimension = _required_text(value.get("kline_dimension"), "kline_dimension")
    entities = _normalize_entities(value.get("entities"))
    by_key = {item["key"]: item for item in entities}
    if protagonist_key not in by_key or by_key[protagonist_key]["entity_type"] != "character":
        raise ValueError("protagonist_key must reference a character")
    relations = _normalize_relations(value.get("relations", []), by_key)
    scenes = _normalize_scenes(value.get("scenes"), by_key)
    return {
        "title": title, "premise": premise, "protagonist_key": protagonist_key,
        "kline_dimension": dimension, "entities": entities,
        "relations": relations, "scenes": scenes,
    }
```

Implement `_normalize_scenes` with exactly these rules: 6–8 items; all four numeric fields present and finite; round to one decimal; clamp only values already within `0..100` (out-of-range values raise); set scene `N>1` open to prior normalized close; expand high/low only enough to contain normalized open and close.

- [ ] **Step 4: Verify GREEN for normalization**

Run the same unittest command. Expected: 3 tests PASS.

- [ ] **Step 5: Write the failing one-repair service test**

Add a real temporary SQLite database and a small external-boundary fake. Assert observable response and call count, not the fake itself as the product behavior:

```python
from creative_claw.cold_start import ColdStartService
from creative_claw.db import Database
from creative_claw.repository import Repository


class SequenceWriter:
    model = "fake-cold-start"
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.calls: list[dict[str, object] | None] = []
    def generate(self, prompt: str, *, repair=None) -> str:
        self.calls.append(repair)
        return self.responses[len(self.calls) - 1]


class ColdStartPreviewServiceTests(unittest.TestCase):
    def test_invalid_first_response_is_repaired_once_without_writing_project(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "test.db")
            database.initialize()
            Repository(database).create_project("空项目", temp_dir, "empty")
            writer = SequenceWriter(["not-json", json.dumps(VALID_PREVIEW, ensure_ascii=False)])
            result = ColdStartService(database).preview("empty", "写一个原创民间幽默故事", writer)
            self.assertEqual(result["preview"]["title"], "铜铃镇的聪明账单")
            self.assertEqual(result["generation"], {"prompt": "写一个原创民间幽默故事", "model": "fake-cold-start"})
            self.assertIsNone(writer.calls[0])
            self.assertIn("not-json", writer.calls[1]["response"])
            snapshot = Repository(database).canvas_snapshot("empty")
            self.assertEqual(snapshot["entities"], [])
            self.assertEqual(snapshot["timeline"], [])
            self.assertEqual(snapshot["ohlc"], [])
```

- [ ] **Step 6: Run the service test and verify RED**

Run:

```powershell
python -m unittest tests.test_cold_start.ColdStartPreviewServiceTests -v
```

Expected: FAIL because `ColdStartService` does not exist.

- [ ] **Step 7: Implement the service preview and model adapter**

Implement `ColdStartService.preview()` so it checks prompt and emptiness, calls `writer.generate()`, parses/normalizes, and retries exactly once with `repair={"response": raw[:28000], "error": str(error)}`. In `llm.py`, add `OpenAICompatibleColdStartWriter` using the same endpoint and secret handling as `OpenAICompatibleWriter`; its system message must demand only the exact JSON contract, 3–5 entities, 6–8 scenes, original names/plots, and no reasoning text. Use temperature `0.65` for the first request and `0.0` for repair.

- [ ] **Step 8: Verify GREEN and run the focused module**

```powershell
python -m unittest tests.test_cold_start -v
python -m compileall -q creative_claw
```

Expected: all cold-start tests PASS; compile command exits 0.

- [ ] **Step 9: Record checkpoint**

Recommended commit if the directory later becomes a repository:

```powershell
git add creative_claw/cold_start.py creative_claw/llm.py tests/test_cold_start.py
git commit -m "feat: add validated cold-start previews"
```

Current environment: skip commit without initializing Git.

---

### Task 2: 原子采用与专用 HTTP API

**Files:**
- Modify: `creative_claw/cold_start.py`
- Modify: `creative_claw/api.py`
- Modify: `tests/test_cold_start.py`
- Create: `tests/test_cold_start_api.py`

**Interfaces:**
- Consumes: `normalize_preview`, preview response `{preview, generation}` from Task 1
- Produces: `ColdStartService.is_empty(project_id: str, connection=None) -> bool`
- Produces: `ColdStartService.apply(project_id: str, preview: dict, generation: dict) -> dict`
- Produces: `ColdStartConflictError`
- Produces: `POST /v1/projects/<id>/cold-start/preview`
- Produces: `POST /v1/projects/<id>/cold-start/apply`

- [ ] **Step 1: Write failing atomic-apply tests**

Add tests whose mutations are: forgetting a table in emptiness detection, committing before ledger append, or creating unlinked K lines.

```python
from unittest.mock import patch
from creative_claw.cold_start import ColdStartConflictError, ColdStartService
from creative_claw.ledger import Ledger


class ColdStartApplyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = Database(self.root / "test.db")
        self.database.initialize()
        self.repository = Repository(self.database)
        self.repository.create_project("空项目", self.root, "empty")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_apply_creates_connected_framework_and_one_audit_event(self) -> None:
        result = ColdStartService(self.database).apply(
            "empty", VALID_PREVIEW,
            {"prompt": "写一个原创民间幽默故事", "model": "fake-cold-start"},
        )
        snapshot = result["snapshot"]
        self.assertEqual(snapshot["project"]["name"], "铜铃镇的聪明账单")
        self.assertEqual(len(snapshot["entities"]), 3)
        self.assertEqual(len(snapshot["relations"]), 1)
        self.assertEqual(len(snapshot["timeline"]), 6)
        self.assertEqual(len(snapshot["ohlc"]), 6)
        self.assertEqual(
            {row["timeline_event_id"] for row in snapshot["ohlc"]},
            {row["id"] for row in snapshot["timeline"]},
        )
        self.assertTrue(all(row["attrs"]["status"] == "outline" for row in snapshot["timeline"]))
        applied = [event for event in Ledger(self.database).list("empty") if event["event_type"] == "cold_start.applied"]
        self.assertEqual(len(applied), 1)
        self.assertTrue(Ledger(self.database).verify("empty")["valid"])

    def test_apply_rolls_back_every_write_when_ledger_append_fails(self) -> None:
        before = self.repository.canvas_snapshot("empty")
        with patch.object(Ledger, "append", side_effect=RuntimeError("forced ledger failure")):
            with self.assertRaises(RuntimeError):
                ColdStartService(self.database).apply(
                    "empty", VALID_PREVIEW,
                    {"prompt": "写一个原创民间幽默故事", "model": "fake-cold-start"},
                )
        after = self.repository.canvas_snapshot("empty")
        self.assertEqual(after["project"]["name"], before["project"]["name"])
        self.assertEqual(after["entities"], [])
        self.assertEqual(after["timeline"], [])
        self.assertEqual(after["ohlc"], [])

    def test_existing_content_blocks_apply(self) -> None:
        self.repository.upsert_entity("empty", "已有角色", "character")
        with self.assertRaises(ColdStartConflictError):
            ColdStartService(self.database).apply(
                "empty", VALID_PREVIEW,
                {"prompt": "另一个故事", "model": "fake-cold-start"},
            )
```

- [ ] **Step 2: Run atomic tests and verify RED**

```powershell
python -m unittest tests.test_cold_start.ColdStartApplyTests -v
```

Expected: FAIL because `apply` and `ColdStartConflictError` are absent.

- [ ] **Step 3: Implement one-connection apply**

In `cold_start.py`, use `with database.connect() as connection`, immediately execute `BEGIN IMMEDIATE`, recheck emptiness across `documents`, `entities`, `relations`, `timeline_events`, `ohlc_points`, `production_units`, and `artifacts`, then write in dependency order. Generate all IDs with `new_id`, timestamps with `utc_now`, JSON with `json_dumps`, and append the ledger through:

```python
self.ledger.append(
    project_id,
    "cold_start.applied",
    {
        "prompt": generation["prompt"],
        "model": generation["model"],
        "title": normalized["title"],
        "entity_ids": list(entity_ids.values()),
        "relation_ids": relation_ids,
        "scene_ids": scene_ids,
        "ohlc_ids": ohlc_ids,
        "counts": {"entities": len(entity_ids), "relations": len(relation_ids), "scenes": len(scene_ids), "ohlc": len(ohlc_ids)},
    },
    actor="ai",
    connection=connection,
)
```

Do not call `Repository.upsert_entity`, `add_relation`, `add_timeline_event`, or `upsert_ohlc` inside this transaction because each opens its own connection. Return `{"summary": counts, "snapshot": Repository(database).canvas_snapshot(project_id)}` only after the transaction exits successfully.

- [ ] **Step 4: Verify atomic tests GREEN**

```powershell
python -m unittest tests.test_cold_start.ColdStartApplyTests -v
```

Expected: 3 tests PASS.

- [ ] **Step 5: Write failing API contract tests**

Create `tests/test_cold_start_api.py` using a temporary database and Flask test client. Patch only the external model adapter:

```python
class ColdStartApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = Database(self.root / "api.db")
        self.database.initialize()
        self.repository = Repository(self.database)
        self.repository.create_project("空项目", self.root, "empty")
        self.client = create_app(self.database.path, run_blueprint_jobs_inline=True).test_client()

    def test_preview_is_read_only_and_apply_returns_full_snapshot(self) -> None:
        writer = SequenceWriter([json.dumps(VALID_PREVIEW, ensure_ascii=False)])
        with patch("creative_claw.api.OpenAICompatibleColdStartWriter.from_env", return_value=writer):
            preview_response = self.client.post(
                "/v1/projects/empty/cold-start/preview",
                json={"prompt": "写一个原创民间幽默故事"},
            )
        self.assertEqual(preview_response.status_code, 200)
        self.assertEqual(self.repository.canvas_snapshot("empty")["timeline"], [])
        apply_response = self.client.post(
            "/v1/projects/empty/cold-start/apply",
            json=preview_response.get_json(),
        )
        self.assertEqual(apply_response.status_code, 201)
        self.assertEqual(len(apply_response.get_json()["snapshot"]["timeline"]), 6)

    def test_nonempty_project_returns_409_for_preview_and_apply(self) -> None:
        self.repository.upsert_entity("empty", "已有角色", "character")
        for path, body in (
            ("preview", {"prompt": "另一个故事"}),
            ("apply", {"preview": VALID_PREVIEW, "generation": {"prompt": "另一个故事", "model": "fake"}}),
        ):
            response = self.client.post(f"/v1/projects/empty/cold-start/{path}", json=body)
            self.assertEqual(response.status_code, 409)
            self.assertIn("冷启动仅适用于空项目", response.get_data(as_text=True))
```

- [ ] **Step 6: Run API tests and verify RED**

```powershell
python -m unittest tests.test_cold_start_api -v
```

Expected: both routes return 404.

- [ ] **Step 7: Register service, error handler, and routes**

In `api.py`, instantiate `ColdStartService(database)`, add a `ColdStartConflictError` handler returning `{"error": str(error)}` with 409, and register:

```python
@app.post("/v1/projects/<project_id>/cold-start/preview")
def cold_start_preview(project_id: str):
    payload = json_object()
    writer = OpenAICompatibleColdStartWriter.from_env()
    return jsonify(cold_start_service.preview(project_id, str(payload.get("prompt") or ""), writer))


@app.post("/v1/projects/<project_id>/cold-start/apply")
def cold_start_apply(project_id: str):
    payload = json_object()
    result = cold_start_service.apply(project_id, payload.get("preview"), payload.get("generation"))
    return jsonify(result), 201
```

- [ ] **Step 8: Verify all backend behavior**

```powershell
python -m unittest tests.test_cold_start tests.test_cold_start_api -v
python -m unittest discover -s tests -p "test_*.py" -v
```

Expected: focused tests PASS; existing suite has no new failures and the paid real-model test remains skipped by default.

- [ ] **Step 9: Record checkpoint**

Recommended commit:

```powershell
git add creative_claw/cold_start.py creative_claw/api.py tests/test_cold_start.py tests/test_cold_start_api.py
git commit -m "feat: apply cold-start frameworks atomically"
```

Current environment: skip commit without initializing Git.

---

### Task 3: AI 助手模式、结构化预览卡与一键采用

**Files:**
- Create: `creative_claw/web/cold-start-state.js`
- Create: `tests/js/cold-start-state.test.cjs`
- Modify: `creative_claw/web/index.html`
- Modify: `creative_claw/web/app.js`
- Modify: `creative_claw/web/app.css`
- Create: `scripts/e2e_cold_start.py`

**Interfaces:**
- Produces: `CreativeClawColdStart.isProjectEmpty(snapshot) -> boolean`
- Produces: `CreativeClawColdStart.modeView(mode, snapshot) -> {coldStart, empty, placeholder, primaryLabel, status}`
- Produces: `CreativeClawColdStart.summarizePreview(preview) -> {entityCount, relationCount, sceneCount, protagonistName, dimension}`
- Consumes: Task 2 preview/apply endpoints and the existing `loadSnapshot(true)` refresh path

- [ ] **Step 1: Write failing Node behavior tests**

Create `tests/js/cold-start-state.test.cjs`:

```javascript
const test = require("node:test");
const assert = require("node:assert/strict");
const {
  isProjectEmpty,
  modeView,
  summarizePreview,
} = require("../../creative_claw/web/cold-start-state.js");

const emptySnapshot = {
  documents: [], entities: [], relations: [], timeline: [], ohlc: [],
  production_units: [], artifacts: [],
};

test("cold-start mode exposes one-sentence generation copy only for an empty project", () => {
  assert.equal(isProjectEmpty(emptySnapshot), true);
  assert.deepEqual(modeView("cold_start", emptySnapshot), {
    coldStart: true,
    empty: true,
    placeholder: "例如：帮我写一个类阿凡提的幽默故事",
    primaryLabel: "生成框架预览",
    status: "输入一句创作意图，先预览再采用",
  });
});

test("any structured project content blocks cold start", () => {
  for (const key of ["documents", "entities", "relations", "timeline", "ohlc", "production_units", "artifacts"]) {
    assert.equal(isProjectEmpty({ ...emptySnapshot, [key]: [{ id: key }] }), false, key);
  }
  assert.equal(modeView("cold_start", { ...emptySnapshot, timeline: [{ id: "scene" }] }).status, "冷启动仅适用于空项目，请先新建项目");
});

test("preview summary resolves protagonist without recomputing backend validation", () => {
  assert.deepEqual(summarizePreview({
    protagonist_key: "hero", kline_dimension: "解局主动权",
    entities: [{ key: "hero", name: "艾山" }, { key: "collector", name: "罗班" }],
    relations: [{ source_key: "hero", target_key: "collector" }],
    scenes: [{ title: "一" }, { title: "二" }],
  }), {
    entityCount: 2, relationCount: 1, sceneCount: 2,
    protagonistName: "艾山", dimension: "解局主动权",
  });
});
```

- [ ] **Step 2: Run Node test and verify RED**

```powershell
node --test tests/js/cold-start-state.test.cjs
```

Expected: FAIL with missing module.

- [ ] **Step 3: Implement the pure browser state module**

Use the same UMD shape as `context-state.js` and export only the three tested functions. `isProjectEmpty` must inspect exactly the seven arrays in the test; `modeView` must preserve the ordinary-chat placeholder and label when mode is not `cold_start`.

- [ ] **Step 4: Verify Node test GREEN**

```powershell
node --test tests/js/cold-start-state.test.cjs
node --check creative_claw/web/cold-start-state.js
```

Expected: 3 tests PASS; syntax exits 0.

- [ ] **Step 5: Write and run a failing real-browser acceptance test**

Create `scripts/e2e_cold_start.py` before editing the HTML or app integration. Follow `scripts/e2e_context_preview.py` process cleanup and port checks. The script must:

1. Create an empty project in a temporary SQLite database.
2. Start a `ThreadingHTTPServer` on localhost whose `/v1/chat/completions` response contains the literal `VALID_PREVIEW` JSON as assistant content.
3. Start Creative Claw with temporary environment values `CREATIVE_CLAW_LLM_API_KEY=e2e-key`, `CREATIVE_CLAW_LLM_BASE_URL=http://127.0.0.1:<fake-port>/v1`, and `CREATIVE_CLAW_LLM_MODEL=e2e-cold-start`.
4. Open the page with Playwright/Edge and select `#chatMode` value `cold_start`.
5. Assert the placeholder, “生成框架预览” button, preview title, 3 entity items, and 6 scene items.
6. Assert `#canvasMeta` still reports zero scenes before applying.
7. Click “采用全部”; wait until `#canvasMeta` reports 6 scenes and the K-line chart has 6 candles/period groups.
8. Query the real SQLite database after the UI action and assert 3 entities, 1 relation, 6 timeline events, 6 linked OHLC rows, and one `cold_start.applied` event.
9. Always stop both HTTP servers and write `cold-start-report.json` plus `cold-start.png`.

Run:

```powershell
python scripts/e2e_cold_start.py --port 8769 --fake-model-port 8770 --output-dir demo-output/cold-start-red
```

Expected: FAIL because `#chatMode` has no `cold_start` option. Confirm the report names that missing behavior and both servers stop cleanly.

- [ ] **Step 6: Add the mode option and script loading**

In `index.html`, add this option after consistency:

```html
<option value="cold_start">冷启动框架搭建</option>
```

Load `/assets/cold-start-state.js` before `/assets/app.js`. Keep the feature inside the existing assistant panel; do not add a tab, modal wizard, or Blueprint Lab dependency.

- [ ] **Step 7: Implement mode synchronization and preview state**

Add to `state`:

```javascript
coldStart: { preview: null, generation: null, prompt: "", busy: false },
```

Implement `syncChatMode()` using `CreativeClawColdStart.modeView`. In cold-start mode it must update `#chatInput.placeholder`, `#sendChat.textContent`, `#chatStatus`, hide `#previewContext`, and replace the workflow reminder with `生成预览 → 审阅骨架 → 采用全部`。In ordinary modes restore the current copy and context controls. Call it after every `loadSnapshot()`, project reset, and `#chatMode` change.

Change `sendChat()` to branch:

```javascript
async function sendChat() {
  if ($("#chatMode").value === "cold_start") {
    await generateColdStartPreview();
    return;
  }
  await prepareChatRequest();
}
```

- [ ] **Step 8: Render the preview card with safe DOM APIs**

Implement `renderColdStartPreview(payload)` with `document.createElement` and `textContent` only. Render title, premise, entity chips with types/descriptions, relation sentences, protagonist/dimension, and an ordered 6–8 scene list showing `O/H/L/C`. Append buttons with IDs or listeners for “重新生成” and “采用全部”; never use `innerHTML` with model output.

Add CSS classes `.cold-start-preview`, `.cold-start-summary`, `.cold-start-entities`, `.cold-start-scenes`, `.cold-start-scene`, `.cold-start-ohlc`, and `.cold-start-actions`. At right-panel widths, use one column and allow scene summaries to wrap without horizontal overflow.

- [ ] **Step 9: Implement preview, regenerate, and apply requests**

`generateColdStartPreview()` must:

- reject blank input;
- block when the pure state says nonempty;
- open model config if not configured;
- disable buttons while requesting;
- POST `{prompt}` to `/cold-start/preview`;
- store both `preview` and `generation`, then render the card;
- keep prompt and previous preview visible on network error.

`applyColdStartPreview()` must POST the exact stored `{preview, generation}` to `/cold-start/apply`; on 201, update the current project `<option>` from `result.snapshot.project.name`, clear cold-start state, call `await loadSnapshot(true)`, and append a success message containing the returned entity/scene/K-line counts. On 409 or other errors, keep the preview visible.

- [ ] **Step 10: Run frontend checks and verify E2E GREEN**

```powershell
node --test tests/js/*.test.cjs
node --check creative_claw/web/context-state.js
node --check creative_claw/web/blueprint-state.js
node --check creative_claw/web/cold-start-state.js
node --check creative_claw/web/app.js
python scripts/e2e_cold_start.py --port 8769 --fake-model-port 8770 --output-dir demo-output/cold-start
```

Expected: all Node tests PASS; all four syntax checks exit 0; E2E report has `"passed": true`, no page errors, and both server processes are stopped.

- [ ] **Step 11: Record checkpoint**

Recommended commit:

```powershell
git add creative_claw/web/cold-start-state.js creative_claw/web/index.html creative_claw/web/app.js creative_claw/web/app.css tests/js/cold-start-state.test.cjs scripts/e2e_cold_start.py
git commit -m "feat: add cold-start mode to AI assistant"
```

Current environment: skip commit without initializing Git.

---

### Task 4: 使用说明与全量回归验证

**Files:**
- Modify: `README.md`

**Interfaces:**
- Consumes: cold-start UI and API from Tasks 2–3
- Produces: user/API documentation and complete verification evidence

- [ ] **Step 1: Document the user flow and API**

Add a concise README section titled `AI 冷启动框架搭建` containing:

- 空项目限定；
- AI 助手 → 冷启动框架搭建 → 输入一句话 → 生成预览 → 采用全部；
- 3–5 entities / 6–8 scenes / one protagonist K-line contract;
- two endpoint payloads;
- originality constraint;
- the focused backend, Node, and E2E commands from this plan.

- [ ] **Step 2: Run the complete verification matrix**

```powershell
python -m compileall -q creative_claw
python -m unittest discover -s tests -p "test_*.py" -v
node --test tests/js/*.test.cjs
node --check creative_claw/web/context-state.js
node --check creative_claw/web/blueprint-state.js
node --check creative_claw/web/cold-start-state.js
node --check creative_claw/web/app.js
python scripts/e2e_context_preview.py --port 8767 --output-dir demo-output/context-regression
python scripts/e2e_blueprint_lab.py --port 8768 --output-dir demo-output/blueprint-regression
python scripts/e2e_cold_start.py --port 8769 --fake-model-port 8770 --output-dir demo-output/cold-start
```

Expected: compile exits 0; Python tests all pass except the existing paid real-model test skipped by design; all Node tests and syntax checks pass; all three E2E reports contain `"passed": true` and no functional page errors.

- [ ] **Step 3: Review mutations and scope**

Confirm these realistic regressions are caught by tests: wrong mode branch, nonempty project accepted, preview writing early, entity/scene bounds removed, unknown reference accepted, unlinked K-line insert, ledger failure partially committed, apply repeated after concurrent content, and model text inserted through unsafe HTML. Confirm no Blueprint Lab or ordinary chat behavior was refactored beyond the cold-start branch.

- [ ] **Step 4: Record final checkpoint**

Recommended commit:

```powershell
git add README.md
git commit -m "test: verify AI cold-start workflow"
```

Current environment: skip commit without initializing Git.

---

## Plan Self-Review

- Spec coverage: entry location, empty-only boundary, preview-before-apply, 3–5 entities, 6–8 scenes, original mechanisms, OHLC normalization, single transaction, 409 concurrency behavior, UI refresh, and all verification layers each map to a task above.
- Completeness scan: every step contains concrete inputs, outputs, commands, expected results, and defined interfaces; no deferred implementation markers remain.
- Type consistency: both endpoints and frontend use the exact `{preview, generation}` envelope; `generation` always contains `{prompt, model}`; scene K lines use `timeline_event_id`; browser mode value is consistently `cold_start`.
