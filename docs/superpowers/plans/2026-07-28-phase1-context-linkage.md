# Phase 1 上下文与真实联动修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 移除前端演示人物硬编码，使当前分支、章/集、场景、人物和 K 线维度从浏览器选择一路传递到 API、检索上下文、模型提示、类型化证据和验收测试。

**Architecture:** 新增纯领域 `ContextScope` 负责请求兼容和上下文解析，Repository 负责精确场景及其关联时间线/K 线查询，Retriever 只编排搜索、图谱、时间线、K 线和证据。浏览器通过独立纯 JavaScript 模块从当前 UI 状态生成 scope，并在调用模型前使用同一请求先预览上下文，保证“预览内容”和“实际调用内容”采用同一后端契约。

**Tech Stack:** Python 3、Flask、SQLite、原生 JavaScript、HTML `<dialog>`、Python `unittest`、Node.js `node:test`、Playwright、OpenAI-compatible Chat Completions API。

## Global Constraints

- AI 是纯副驾驶，只在用户明确点击运行时调用，不自动推进工序。
- AI 只返回分析、候选稿或提案，不直接覆盖正式稿。
- K 线修改不自动改正文；本阶段只确保父周期既有逻辑不回归，并让场景 K 线进入上下文、证据和预览。
- 来源、图谱、时间线、K 线、版本、规则和问题分别使用 `[S#]`、`[G#]`、`[T#]`、`[K#]`、`[V#]`、`[R#]`、`[I#]`。
- `/context` 与 `/chat` 必须返回 UTF-8 JSON，并暴露 `resolved_scope`、`evidence_refs`、`citation_validation`。
- 保留旧请求的 `filters`、顶层 `character_name` 和顶层 `dimension` 参数兼容；新前端统一发送 `scope`。
- 当前场景可由 `scene_id` 精确定位；只有旧客户端没有 `scene_id` 时才使用 `episode + scene`。
- 自动测试默认不调用真实模型；只有 `CREATIVE_CLAW_REAL_LLM_TEST=1` 时运行真实模型测试。
- 模型密钥只从进程环境读取，不写入源码、测试夹具、SQLite、浏览器存储、截图、报告或日志。
- 项目当前不是 Git 仓库。每个任务的 Commit 步骤只有在 `git rev-parse --is-inside-work-tree` 成功时执行；否则记录建议提交信息，不得执行 `git init`。

---

## File Structure

### 新建

- `creative_claw/context.py`：定义 `ContextScope`、兼容请求解析和序列化，不访问数据库。
- `creative_claw/evidence.py`：将来源、图谱、时间线、K 线等上下文转换为稳定的类型化证据，并校验回答中的引用。
- `creative_claw/web/context-state.js`：纯函数模块，从浏览器状态生成 scope、生成预览摘要、解析引用标记；可由 Node 直接测试。
- `tests/test_context_scope.py`：`ContextScope` 正常化、优先级和兼容性单元测试。
- `tests/test_evidence.py`：类型化证据编号和引用校验单元测试。
- `tests/test_context_api.py`：隔离数据库下 `/context` 与 `/chat` 契约测试。
- `tests/js/context-state.test.cjs`：浏览器上下文纯函数测试。
- `scripts/e2e_context_preview.py`：启动隔离服务并用 Playwright 验收选择、预览、请求和证据联动。
- `tests/test_real_llm.py`：显式启用的真实模型上下文验证；默认跳过。

### 修改

- `creative_claw/repository.py`：新增精确时间线事件、场景邻域和事件关联 K 线查询。
- `creative_claw/retrieval.py`：接受 `ContextScope`，解析当前场景，自动加载相邻时间线与关联 K 线，返回类型化证据。
- `creative_claw/llm.py`：提示词改用类型化证据规则，并返回引用校验结果。
- `creative_claw/api.py`：统一解析 scope；扩展 `/context`、`/chat` 响应契约和 UTF-8 行为。
- `creative_claw/web/index.html`：加载 `context-state.js`，增加“预览模型上下文”入口与对话框。
- `creative_claw/web/app.js`：移除全部演示人物硬编码，维护当前上下文选择，先预览后调用，渲染类型化证据。
- `creative_claw/web/app.css`：上下文预览、证据徽标、警告和响应布局样式。
- `README.md`：记录新上下文契约、证据编号和测试开关。

## Public Interfaces Locked by This Plan

```python
@dataclass(frozen=True, slots=True)
class ContextScope:
    branch: str = "main"
    episode: int | None = None
    scene_id: str | None = None
    character_name: str | None = None
    dimension: str | None = None

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ContextScope": ...
    def to_dict(self) -> dict[str, Any]: ...
```

```python
Repository.get_timeline_event(project_id: str, event_id: str, *, branch: str = "main") -> dict[str, Any] | None
Repository.timeline_context(project_id: str, *, event_id: str | None = None, episode: int | None = None, scene: int | None = None, branch: str = "main", radius: int = 1, limit: int = 20) -> dict[str, Any]
Repository.ohlc_for_timeline_events(project_id: str, event_ids: list[str], *, character_name: str | None = None, dimension: str | None = None, branch: str = "main") -> list[dict[str, Any]]
```

```python
HybridRetriever.build_context(project_id: str, query: str, *, top_k: int = 8, scope: ContextScope | None = None, filters: dict[str, Any] | None = None, character_name: str | None = None, dimension: str | None = None) -> dict[str, Any]
```

```python
build_evidence_refs(*, sources: list[dict[str, Any]], graph: dict[str, Any], timeline: list[dict[str, Any]], ohlc: list[dict[str, Any]], versions: list[dict[str, Any]] | None = None, rules: list[dict[str, Any]] | None = None, issues: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]
validate_citations(text: str, evidence_refs: list[dict[str, Any]]) -> dict[str, Any]
```

浏览器模块对外暴露：

```javascript
globalThis.CreativeClawContext = {
  buildContextScope,
  summarizeContext,
  parseCitationTokens,
};
```

---

### Task 1: `ContextScope` 类型与兼容请求解析

**Files:**
- Create: `creative_claw/context.py`
- Create: `tests/test_context_scope.py`

**Interfaces:**
- Consumes: Flask payload 中的新 `scope`、旧 `filters`、旧顶层 `character_name`、旧顶层 `dimension`。
- Produces: `ContextScope.from_payload(payload)` 和 `ContextScope.to_dict()`，供 Task 3、5 使用。

- [ ] **Step 1: 写入失败测试，锁定默认值、类型清洗和新旧参数优先级**

```python
import unittest

from creative_claw.context import ContextScope


class ContextScopeTests(unittest.TestCase):
    def test_defaults_to_main_branch(self):
        self.assertEqual(
            ContextScope.from_payload({}).to_dict(),
            {
                "branch": "main",
                "episode": None,
                "scene_id": None,
                "character_name": None,
                "dimension": None,
            },
        )

    def test_scope_wins_over_legacy_fields(self):
        scope = ContextScope.from_payload({
            "scope": {
                "branch": "rewrite-a",
                "episode": "18",
                "scene_id": "time-current",
                "character_name": "顾遥",
                "dimension": "信任度",
            },
            "filters": {"branch": "main", "episode": 3},
            "character_name": "沈霜",
            "dimension": "知情度",
        })
        self.assertEqual(scope.branch, "rewrite-a")
        self.assertEqual(scope.episode, 18)
        self.assertEqual(scope.scene_id, "time-current")
        self.assertEqual(scope.character_name, "顾遥")
        self.assertEqual(scope.dimension, "信任度")

    def test_legacy_request_remains_supported(self):
        scope = ContextScope.from_payload({
            "filters": {"branch": "main", "episode": 7},
            "character_name": "林川",
            "dimension": "决心",
        })
        self.assertEqual(scope.episode, 7)
        self.assertEqual(scope.character_name, "林川")
        self.assertEqual(scope.dimension, "决心")

    def test_blank_values_become_none(self):
        scope = ContextScope.from_payload({
            "scope": {"scene_id": "  ", "character_name": "", "dimension": None}
        })
        self.assertIsNone(scope.scene_id)
        self.assertIsNone(scope.character_name)
        self.assertIsNone(scope.dimension)
```

- [ ] **Step 2: 运行测试并确认因模块不存在而失败**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_context_scope -v`

Expected: FAIL，错误包含 `No module named 'creative_claw.context'`。

- [ ] **Step 3: 实现最小不可变上下文类型**

```python
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


def _clean_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return int(value)


@dataclass(frozen=True, slots=True)
class ContextScope:
    branch: str = "main"
    episode: int | None = None
    scene_id: str | None = None
    character_name: str | None = None
    dimension: str | None = None

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ContextScope":
        scope = dict(payload.get("scope") or {})
        filters = dict(payload.get("filters") or {})
        return cls(
            branch=_clean_text(scope.get("branch")) or _clean_text(filters.get("branch")) or "main",
            episode=_clean_int(scope.get("episode") if "episode" in scope else filters.get("episode")),
            scene_id=_clean_text(scope.get("scene_id")),
            character_name=_clean_text(
                scope.get("character_name") if "character_name" in scope else payload.get("character_name")
            ),
            dimension=_clean_text(scope.get("dimension") if "dimension" in scope else payload.get("dimension")),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
```

- [ ] **Step 4: 运行单元测试并确认通过**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_context_scope -v`

Expected: 4 tests PASS。

- [ ] **Step 5: 执行条件提交或记录建议提交信息**

```powershell
if (git rev-parse --is-inside-work-tree 2>$null) {
  git add creative_claw/context.py tests/test_context_scope.py
  git commit -m "feat: add normalized context scope"
} else {
  Write-Output "Not a Git work tree; suggested commit: feat: add normalized context scope"
}
```

### Task 2: Repository 场景上下文与关联 K 线查询

**Files:**
- Modify: `creative_claw/repository.py:312-520`
- Modify: `tests/test_context_scope.py`

**Interfaces:**
- Consumes: Task 1 的标准化 `branch`、`episode`、`scene_id`。
- Produces: `get_timeline_event()`、`timeline_context()`、`ohlc_for_timeline_events()`，供 Task 3 调用。

- [ ] **Step 1: 在现有测试夹具风格基础上加入精确场景、邻域和 K 线关联测试**

测试必须创建三个同分支连续场景和一个其他分支场景，并为当前场景写入两个人物或两个维度的场景级 OHLC。核心断言如下：

```python
current = repository.get_timeline_event(project_id, current_event["id"], branch="main")
self.assertEqual(current["id"], current_event["id"])
self.assertIsNone(repository.get_timeline_event(project_id, current_event["id"], branch="alternate"))

context = repository.timeline_context(
    project_id,
    event_id=current_event["id"],
    branch="main",
    radius=1,
)
self.assertEqual(context["current"]["id"], current_event["id"])
self.assertEqual([row["id"] for row in context["events"]], [before["id"], current_event["id"], after["id"]])

ohlc = repository.ohlc_for_timeline_events(
    project_id,
    [current_event["id"]],
    character_name="顾遥",
    dimension="信任度",
    branch="main",
)
self.assertEqual(len(ohlc), 1)
self.assertEqual(ohlc[0]["timeline_event_id"], current_event["id"])
```

同时增加旧客户端回退断言：没有 `event_id` 时，`timeline_context(..., episode=18, scene=2)` 能找到相同 `current`。

- [ ] **Step 2: 运行新增测试并确认三个方法尚不存在**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_context_scope -v`

Expected: FAIL，错误包含 `Repository has no attribute 'get_timeline_event'`。

- [ ] **Step 3: 在 `Repository` 中实现精确事件读取**

```python
def get_timeline_event(
    self,
    project_id: str,
    event_id: str,
    *,
    branch: str = "main",
) -> dict[str, Any] | None:
    with self.database.connect() as connection:
        row = connection.execute(
            "SELECT * FROM timeline_events WHERE id=? AND project_id=? AND branch=?",
            (event_id, project_id, branch),
        ).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["attrs"] = json_loads(result.pop("attrs_json"))
    return result
```

- [ ] **Step 4: 实现场景邻域查询，保证 current 单独返回且 events 按集、场、创建时间稳定排序**

`timeline_context()` 先按 `event_id` 查当前事件；若没有 `event_id`，使用 `project_id + branch + episode + scene` 查第一条匹配事件。找到当前事件后复用或收敛现有 `nearby_timeline()`，返回：

```python
{
    "current": current_or_none,
    "events": ordered_events,
}
```

若无法解析当前事件，不得偷偷返回整个项目时间线，应返回 `{"current": None, "events": []}`。

- [ ] **Step 5: 实现批量关联 K 线查询并参数化可选人物、维度过滤**

SQL 必须使用参数占位符构造 `timeline_event_id IN (...)`，同时限制 `project_id`、`branch` 和 `period_type='scene'`。返回顺序为 `sort_key, character_name, dimension`，并解析 `attrs_json`。

- [ ] **Step 6: 运行新增测试和原有测试**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_context_scope -v`

Expected: PASS。

Run: `.\.venv\Scripts\python.exe -m unittest discover -s tests -v`

Expected: 当前全部测试 PASS，父周期聚合和账本相关原测试不回归。

- [ ] **Step 7: 执行条件提交或记录建议提交信息**

```powershell
if (git rev-parse --is-inside-work-tree 2>$null) {
  git add creative_claw/repository.py tests/test_context_scope.py
  git commit -m "feat: query scene-linked timeline and ohlc context"
} else {
  Write-Output "Not a Git work tree; suggested commit: feat: query scene-linked timeline and ohlc context"
}
```

### Task 3: Retriever 自动解析当前场景、相邻时间线和关联 K 线

**Files:**
- Modify: `creative_claw/retrieval.py:232-282`
- Create: `tests/test_context_api.py`

**Interfaces:**
- Consumes: Task 1 `ContextScope`；Task 2 Repository 三个查询方法。
- Produces: 扩展后的 `HybridRetriever.build_context()`，其返回值包含 `resolved_scope`、`sources`、`graph`、`timeline`、`ohlc`、`context_text` 和 `retrieval_policy`。

- [ ] **Step 1: 创建隔离数据库测试，证明只给 `scene_id` 也会自动加载时间线和场景 K 线**

使用现有 `Database` 初始化和 Repository 写入方式构造：一条正典来源、三条时间线事件、当前事件关联的“顾遥 / 信任度”OHLC。断言：

```python
result = retriever.build_context(
    project_id,
    "顾遥此时是否已经信任导师？",
    scope=ContextScope(
        branch="main",
        scene_id=current_event["id"],
        character_name="顾遥",
        dimension="信任度",
    ),
)
self.assertEqual(result["resolved_scope"]["episode"], 18)
self.assertEqual(result["resolved_scope"]["scene_id"], current_event["id"])
self.assertEqual(result["timeline"][1]["id"], current_event["id"])
self.assertEqual(len(result["ohlc"]), 1)
self.assertIn(current_event["label"], result["context_text"])
self.assertIn("信任度", result["context_text"])
```

再增加两项边界断言：

- `scene_id` 属于其他分支时，`timeline == []` 且 `ohlc == []`。
- 旧参数 `filters={"episode": 18, "branch": "main"}, character_name="顾遥", dimension="信任度"` 仍可解析当前集的时间线。

- [ ] **Step 2: 运行测试并确认当前实现不能从 `scene_id` 自动加载上下文**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_context_api -v`

Expected: FAIL，至少出现 `build_context() got an unexpected keyword argument 'scope'` 或 `timeline` 为空。

- [ ] **Step 3: 修改签名并集中解析兼容参数**

在 `retrieval.py` 导入 `ContextScope`，将签名改为：

```python
def build_context(
    self,
    project_id: str,
    query: str,
    *,
    top_k: int = 8,
    scope: ContextScope | None = None,
    filters: dict[str, Any] | None = None,
    character_name: str | None = None,
    dimension: str | None = None,
) -> dict[str, Any]:
```

若 `scope is None`，通过下面的兼容 payload 构造：

```python
scope = ContextScope.from_payload({
    "filters": filters or {},
    "character_name": character_name,
    "dimension": dimension,
})
```

`_search()` 继续接收 filters，但必须由 scope 生成，避免新旧两套 branch/episode 分叉：

```python
search_filters = {
    **dict(filters or {}),
    "branch": scope.branch,
    "episode": scope.episode,
}
```

- [ ] **Step 4: 解析当前事件并补全 `resolved_scope`**

调用：

```python
timeline_context = self.repository.timeline_context(
    project_id,
    event_id=scope.scene_id,
    episode=scope.episode,
    branch=scope.branch,
    radius=1,
)
```

若找到 `current`，使用当前事件的 `episode` 补全返回 scope，但不改写用户显式人物和维度。返回 `resolved_scope` 必须始终包含 Task 1 的五个键。

- [ ] **Step 5: 只加载当前邻域关联的场景 K 线**

调用 `ohlc_for_timeline_events()`，传入 `timeline_context["events"]` 的 id。人物或维度为空时允许返回邻域内全部场景 K 线，供前端选择；两者都有值时精确过滤。不得继续用 `ohlc_series()` 把整个人物全项目曲线无差别塞入模型上下文。

- [ ] **Step 6: 构建清晰的上下文文本分区**

在类型化证据接入前，先按以下标题组织 `context_text`，保证调试和真实模型测试可定位：

```text
## Sources
...
## Graph
...
## Timeline
...
## Character state
...
```

空分区可以省略，但不能把 OHLC high/low 描述成先后事件；文字必须说明 open/close 是起止状态，high/low 是区间极值。

- [ ] **Step 7: 运行目标测试与完整回归**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_context_api -v`

Expected: PASS。

Run: `.\.venv\Scripts\python.exe -m unittest discover -s tests -v`

Expected: 全部 PASS。

- [ ] **Step 8: 执行条件提交或记录建议提交信息**

```powershell
if (git rev-parse --is-inside-work-tree 2>$null) {
  git add creative_claw/retrieval.py tests/test_context_api.py
  git commit -m "fix: resolve active scene context for retrieval"
} else {
  Write-Output "Not a Git work tree; suggested commit: fix: resolve active scene context for retrieval"
}
```

### Task 4: 类型化证据编号与引用校验

**Files:**
- Create: `creative_claw/evidence.py`
- Create: `tests/test_evidence.py`
- Modify: `creative_claw/retrieval.py:259-282`

**Interfaces:**
- Consumes: Retriever 中的来源 citation、图谱、时间线、K 线，以及未来可传入的版本、规则、问题。
- Produces: `build_evidence_refs()`、`validate_citations()` 和统一证据对象，供 Task 5、8、9 使用。

- [ ] **Step 1: 写入证据编号和引用校验失败测试**

```python
import unittest

from creative_claw.evidence import build_evidence_refs, validate_citations


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.refs = build_evidence_refs(
            sources=[{"document_id": "doc-1", "chunk_id": "chunk-1", "title": "人物圣经", "text": "顾遥拒绝交出钥匙。"}],
            graph={"entities": [{"id": "ent-1", "name": "顾遥", "entity_type": "character"}], "relations": []},
            timeline=[{"id": "time-1", "label": "拒交钥匙", "description": "顾遥当场拒绝。", "episode": 18, "scene": 2}],
            ohlc=[{"id": "ohlc-1", "character_name": "顾遥", "dimension": "信任度", "open": 30, "high": 45, "low": 20, "close": 25, "timeline_event_id": "time-1"}],
        )

    def test_assigns_type_specific_ids(self):
        self.assertEqual([row["ref"] for row in self.refs], ["S1", "G1", "T1", "K1"])
        self.assertEqual([row["kind"] for row in self.refs], ["source", "graph", "timeline", "kline"])

    def test_validation_reports_unknown_and_unused_refs(self):
        result = validate_citations("顾遥拒绝交钥匙。[S1][T1] 状态下降。[K9]", self.refs)
        self.assertEqual(result["used"], ["S1", "T1", "K9"])
        self.assertEqual(result["unknown"], ["K9"])
        self.assertIn("G1", result["unused"])
        self.assertFalse(result["valid"])

    def test_validation_accepts_known_refs(self):
        result = validate_citations("顾遥拒绝交钥匙。[S1][T1]", self.refs)
        self.assertTrue(result["valid"])
        self.assertEqual(result["unknown"], [])
```

- [ ] **Step 2: 运行测试并确认模块不存在**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_evidence -v`

Expected: FAIL，错误包含 `No module named 'creative_claw.evidence'`。

- [ ] **Step 3: 实现稳定编号构建器**

每类证据独立从 1 递增；输入顺序必须稳定。每个证据对象至少包含：

```python
{
    "ref": "T1",
    "kind": "timeline",
    "title": "拒交钥匙",
    "summary": "顾遥当场拒绝。",
    "locator": {"event_id": "time-1", "episode": 18, "scene": 2},
    "payload": original_row,
}
```

前缀映射固定为：

```python
PREFIXES = {
    "source": "S",
    "graph": "G",
    "timeline": "T",
    "kline": "K",
    "version": "V",
    "rule": "R",
    "issue": "I",
}
```

图谱第一版按实体和关系分别生成可读摘要，但共享 `G` 序号；不要把 `evidence_chunk_ids` 再伪装成来源证据。

- [ ] **Step 4: 实现引用标记扫描与校验**

使用严格正则 `r"\[([SGTKVRI]\d+)\]"`，按正文首次出现顺序去重。返回：

```python
{
    "valid": not unknown,
    "used": used_refs,
    "unknown": unknown_refs,
    "unused": unused_known_refs,
}
```

校验只判断引用编号是否存在，不在本阶段自动判定语义是否真实支持陈述。

- [ ] **Step 5: 让 Retriever 用 `evidence_refs` 生成模型上下文**

`build_context()` 调用 `build_evidence_refs()`，并把每条证据格式化为：

```text
[T1] 拒交钥匙
定位：episode=18, scene=2, event_id=time-1
顾遥当场拒绝。
```

返回对象同时保留旧 `citations` 字段，值仅为来源类证据对应的旧 citation payload；新增 `evidence_refs` 作为新前端和新提示词的权威字段。

- [ ] **Step 6: 运行证据测试、上下文测试与完整回归**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_evidence tests.test_context_api -v`

Expected: PASS。

Run: `.\.venv\Scripts\python.exe -m unittest discover -s tests -v`

Expected: 全部 PASS。

- [ ] **Step 7: 执行条件提交或记录建议提交信息**

```powershell
if (git rev-parse --is-inside-work-tree 2>$null) {
  git add creative_claw/evidence.py creative_claw/retrieval.py tests/test_evidence.py
  git commit -m "feat: add typed evidence references"
} else {
  Write-Output "Not a Git work tree; suggested commit: feat: add typed evidence references"
}
```

### Task 5: API 与 LLM 上下文契约

**Files:**
- Modify: `creative_claw/api.py:223-269`
- Modify: `creative_claw/llm.py:130-202`
- Modify: `tests/test_context_api.py`

**Interfaces:**
- Consumes: Task 1 `ContextScope.from_payload()`、Task 3 上下文结果、Task 4 `validate_citations()`。
- Produces: `/context`、`/chat` 的统一响应；模型回答中的 `citation_validation`。

- [ ] **Step 1: 扩展 API 测试，锁定 UTF-8 和新字段**

使用 Flask test client 请求：

```python
response = client.post(
    f"/v1/projects/{project_id}/context",
    json={
        "query": "顾遥此时的信任状态",
        "scope": {
            "branch": "main",
            "scene_id": current_event["id"],
            "character_name": "顾遥",
            "dimension": "信任度",
        },
    },
)
self.assertEqual(response.status_code, 200)
self.assertIn("application/json", response.content_type)
payload = response.get_json()
self.assertEqual(payload["resolved_scope"]["scene_id"], current_event["id"])
self.assertTrue(any(ref["kind"] == "timeline" for ref in payload["evidence_refs"]))
self.assertTrue(any(ref["kind"] == "kline" for ref in payload["evidence_refs"]))
self.assertIn("顾遥", response.get_data(as_text=True))
```

`/chat` 测试不得访问网络。使用 `unittest.mock.patch` 替换 `OpenAICompatibleWriter.from_env`，让 fake writer 返回含 `[S1][T1][K1]` 的固定中文回答，并断言响应同时包含：

- `resolved_scope`
- `evidence_refs`
- `citation_validation`
- 旧字段 `citations`、`graph`、`timeline`、`ohlc`、`retrieval_policy`

- [ ] **Step 2: 运行 API 测试并确认新契约尚未满足**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_context_api -v`

Expected: FAIL，缺少 `resolved_scope` 或 `evidence_refs`。

- [ ] **Step 3: API 统一通过 `ContextScope.from_payload(payload)` 调用 Retriever**

`/context` 和 `/chat` 都执行：

```python
scope = ContextScope.from_payload(payload)
context_result = retriever.build_context(
    project_id,
    query_or_message,
    top_k=int(payload.get("top_k", 8)),
    scope=scope,
)
```

不得在两个路由中分别复制 branch、episode、人物和维度的提取逻辑。

- [ ] **Step 4: 更新模型系统提示的引用规则**

将只允许 `[C#]` 的旧说明替换为：

```text
证据编号按类型区分：来源 [S#]、图谱 [G#]、时间线 [T#]、人物 K 线 [K#]、版本 [V#]、规则 [R#]、问题 [I#]。
引用必须紧跟在被支持的事实之后，只能使用上下文中真实存在的编号。
没有证据支持时明确说明；创作性补充必须标明为非正典且不能附加引用。
人物 OHLC 的 open/close 是周期起止状态，high/low 是区间极值，不代表事件先后。
```

模型 user message 继续使用 `json.dumps(..., ensure_ascii=False)`；发送给模型的 payload 应排除不必要的大型重复字段，但保留 `resolved_scope`、`evidence_refs` 和 `context_text`。

- [ ] **Step 5: 在 `/chat` 返回前执行引用校验**

```python
citation_validation = validate_citations(
    answer.get("answer", ""),
    context_result["evidence_refs"],
)
```

响应结构使用：

```python
{
    **answer,
    "resolved_scope": context_result["resolved_scope"],
    "evidence_refs": context_result["evidence_refs"],
    "citation_validation": citation_validation,
    "citations": context_result["citations"],
    "graph": context_result["graph"],
    "timeline": context_result["timeline"],
    "ohlc": context_result["ohlc"],
    "retrieval_policy": context_result["retrieval_policy"],
}
```

`/context` 没有模型回答时返回空校验：`validate_citations("", evidence_refs)`。

- [ ] **Step 6: 明确 Flask UTF-8 JSON 配置**

在 app 创建处设置：

```python
app.json.ensure_ascii = False
```

不得通过手工字符串拼接 JSON。

- [ ] **Step 7: 运行 API、证据和完整回归测试**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_context_api tests.test_evidence -v`

Expected: PASS。

Run: `.\.venv\Scripts\python.exe -m unittest discover -s tests -v`

Expected: 全部 PASS。

- [ ] **Step 8: 执行条件提交或记录建议提交信息**

```powershell
if (git rev-parse --is-inside-work-tree 2>$null) {
  git add creative_claw/api.py creative_claw/llm.py tests/test_context_api.py
  git commit -m "feat: expose resolved model context contract"
} else {
  Write-Output "Not a Git work tree; suggested commit: feat: expose resolved model context contract"
}
```

### Task 6: 浏览器上下文选择纯函数模块

**Files:**
- Create: `creative_claw/web/context-state.js`
- Create: `tests/js/context-state.test.cjs`
- Modify: `creative_claw/web/index.html`
- Modify: `creative_claw/web/app.js:1-170, 600-860, 1242-1272`

**Interfaces:**
- Consumes: 浏览器 `state.branch`、当前选中节点、时间线事件、K 线行及用户显式选择。
- Produces: `CreativeClawContext.buildContextScope(state)`，供 Task 7 的预览和实际聊天请求共同使用。

- [ ] **Step 1: 写 Node 失败测试，覆盖场景选择、人物/维度选择和无选择状态**

```javascript
const test = require("node:test");
const assert = require("node:assert/strict");
const {
  buildContextScope,
  summarizeContext,
  parseCitationTokens,
} = require("../../creative_claw/web/context-state.js");

test("builds scope from selected scene and selected kline", () => {
  const scope = buildContextScope({
    branch: "main",
    selectedNodeId: "scene:time-18-2",
    selectedCharacterName: "顾遥",
    selectedDimension: "信任度",
    timeline: [{ id: "time-18-2", episode: 18, scene: 2 }],
  });
  assert.deepEqual(scope, {
    branch: "main",
    episode: 18,
    scene_id: "time-18-2",
    character_name: "顾遥",
    dimension: "信任度",
  });
});

test("does not invent demo character when no character is selected", () => {
  const scope = buildContextScope({ branch: "main", timeline: [] });
  assert.equal(scope.character_name, null);
  assert.equal(scope.dimension, null);
});

test("summarizes resolved counts for preview", () => {
  const summary = summarizeContext({
    resolved_scope: { episode: 18, scene_id: "time-18-2", character_name: "顾遥", dimension: "信任度" },
    timeline: [{ id: "time-18-2" }],
    ohlc: [{ id: "ohlc-1" }],
    evidence_refs: [{ ref: "T1" }, { ref: "K1" }],
  });
  assert.equal(summary.timelineCount, 1);
  assert.equal(summary.klineCount, 1);
  assert.equal(summary.evidenceCount, 2);
});

test("extracts typed citation tokens in first-use order", () => {
  assert.deepEqual(parseCitationTokens("依据[T1][K1]，并参考[T1]。"), ["T1", "K1"]);
});
```

- [ ] **Step 2: 运行 Node 测试并确认模块不存在**

Run: `node --test tests/js/context-state.test.cjs`

Expected: FAIL，错误包含 `Cannot find module '../../creative_claw/web/context-state.js'`。

- [ ] **Step 3: 创建 UMD 风格纯函数模块，使浏览器和 Node 共用同一实现**

```javascript
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.CreativeClawContext = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  function clean(value) {
    if (value === undefined || value === null) return null;
    const text = String(value).trim();
    return text || null;
  }

  function buildContextScope(state) {
    const selected = String(state.selectedNodeId || "");
    const sceneId = selected.startsWith("scene:") ? selected.slice(6) : clean(state.selectedSceneId);
    const event = (state.timeline || []).find((row) => row.id === sceneId);
    return {
      branch: clean(state.branch) || "main",
      episode: event?.episode ?? state.selectedEpisode ?? null,
      scene_id: sceneId,
      character_name: clean(state.selectedCharacterName),
      dimension: clean(state.selectedDimension),
    };
  }

  function summarizeContext(payload) {
    return {
      scope: payload.resolved_scope || {},
      timelineCount: (payload.timeline || []).length,
      klineCount: (payload.ohlc || []).length,
      evidenceCount: (payload.evidence_refs || []).length,
    };
  }

  function parseCitationTokens(text) {
    const found = String(text || "").match(/\[([SGTKVRI]\d+)\]/g) || [];
    return [...new Set(found.map((token) => token.slice(1, -1)))];
  }

  return { buildContextScope, summarizeContext, parseCitationTokens };
});
```

- [ ] **Step 4: 在 HTML 中先加载模块，再加载 `app.js`**

在现有 `app.js` script 标签前加入：

```html
<script src="/context-state.js"></script>
```

若静态路由不是根路径，按现有 Flask 静态文件规则使用与 `app.js` 相同的路径前缀。

- [ ] **Step 5: 扩展浏览器 state 并在选择变化时维护人物和维度**

在初始 state 增加：

```javascript
selectedCharacterName: null,
selectedDimension: null,
lastContextPreview: null,
```

当用户选择人物节点或 K 线编辑项时更新 `selectedCharacterName`；选择 K 线维度时更新 `selectedDimension`。选择普通场景不得把人物重置为演示值。

- [ ] **Step 6: 删除 `app.js` 中所有演示人物请求硬编码**

运行搜索：

Run: `Select-String -Path creative_claw/web/app.js -Pattern 'character_name: "沈霜"|dimension: "知情度"'`

修改第 620、841、1270 附近的请求，统一通过：

```javascript
const scope = CreativeClawContext.buildContextScope(state);
```

发送 `{ scope }`。如果某处操作明确针对一条 K 线，可在调用前先用该行的 `character_name`、`dimension` 更新 state，再生成 scope；不得保留任何人物名或维度名常量作为请求默认值。

- [ ] **Step 7: 运行 Node、语法和硬编码检查**

Run: `node --test tests/js/context-state.test.cjs`

Expected: PASS。

Run: `node --check creative_claw/web/context-state.js`

Expected: exit 0。

Run: `node --check creative_claw/web/app.js`

Expected: exit 0。

Run: `Select-String -Path creative_claw/web/app.js -Pattern 'character_name: "沈霜"|dimension: "知情度"'`

Expected: 无输出。

- [ ] **Step 8: 执行条件提交或记录建议提交信息**

```powershell
if (git rev-parse --is-inside-work-tree 2>$null) {
  git add creative_claw/web/context-state.js creative_claw/web/index.html creative_claw/web/app.js tests/js/context-state.test.cjs
  git commit -m "fix: derive browser model scope from active selection"
} else {
  Write-Output "Not a Git work tree; suggested commit: fix: derive browser model scope from active selection"
}
```

### Task 7: 模型上下文预览对话框与“先预览、后调用”工序提醒

**Files:**
- Modify: `creative_claw/web/index.html:190-215, 255-end`
- Modify: `creative_claw/web/app.js:1242-1290`
- Modify: `creative_claw/web/app.css:335-347`
- Modify: `tests/js/context-state.test.cjs`

**Interfaces:**
- Consumes: Task 5 `/context`；Task 6 `buildContextScope()` 和 `summarizeContext()`。
- Produces: `#previewContext` 按钮、`#contextDialog` 对话框、可见工序提醒，以及与实际 `/chat` 共用的 scope。

- [ ] **Step 1: 增加 HTML 结构，所有关键节点使用稳定 id**

在 prompt tools 中增加：

```html
<button class="text-button" id="previewContext" type="button">预览模型上下文</button>
```

在页面底部 dialogs 区增加：

```html
<dialog id="contextDialog">
  <form method="dialog">
    <div class="dialog-head">
      <div><span class="eyebrow">CONTEXT</span><h2>模型上下文预览</h2></div>
      <button class="icon-button" value="cancel" aria-label="关闭">×</button>
    </div>
    <p class="dialog-note">工序提醒：先确认当前场景、人物与 K 线维度，再运行模型。AI 只生成建议，不会自动覆盖正式稿。</p>
    <dl id="contextScopeSummary" class="context-scope-summary"></dl>
    <div id="contextTimelineSummary" class="context-preview-section"></div>
    <div id="contextKlineSummary" class="context-preview-section"></div>
    <div id="contextEvidenceSummary" class="context-preview-section"></div>
    <div id="contextWarnings" class="context-warning" hidden></div>
    <div class="dialog-actions">
      <button class="btn secondary" value="cancel">返回调整</button>
      <button class="btn primary" id="runChatFromPreview" value="default">确认并运行模型</button>
    </div>
  </form>
</dialog>
```

- [ ] **Step 2: 新增预览加载函数，只调用 `/context` 不调用模型**

```javascript
async function loadContextPreview(message) {
  const scope = CreativeClawContext.buildContextScope(state);
  const result = await api(
    `/v1/projects/${encodeURIComponent(state.projectId)}/context`,
    jsonOptions({ query: message, top_k: 8, scope })
  );
  state.lastContextPreview = { message, scope, result };
  renderContextPreview(result);
  return result;
}
```

`renderContextPreview()` 必须显示：

- 当前分支、episode、scene_id。
- 当前人物和维度；为空时显示“未限定”，不能显示演示值。
- 时间线条数与当前场景标题。
- K 线条数、人物、维度、open/high/low/close。
- 每类证据数量和编号范围。
- `timeline` 或 `ohlc` 为空时显示明确警告。

- [ ] **Step 3: 将发送动作拆成准备、预览、确认执行三个函数**

建议固定接口：

```javascript
async function prepareChatRequest() { /* 读取并校验 message，加载预览，打开 dialog */ }
async function runChatRequest(message, scope) { /* 调用 /chat 并渲染回答 */ }
```

`#sendChat` 和 `#previewContext` 都进入 `prepareChatRequest()`；只有 `#runChatFromPreview` 调用 `runChatRequest()`。实际 `/chat` 必须使用 `state.lastContextPreview.scope`，不得重新读取已变化的 UI 状态，确保用户确认的预览与实际请求一致。

- [ ] **Step 4: 保留无需模型的“保存上一条回复为场景”快捷命令**

`sendChat()` 现有正则分支必须继续在模型配置检查和预览前执行。该动作只打开场景表单，不调用 `/context` 或 `/chat`。

- [ ] **Step 5: 增加工序状态提示**

聊天区常驻提示文案至少包含：

```text
当前工序：确认上下文 → 运行模型 → 审阅候选 → 接受或拒绝
```

模型返回后，`chatStatus` 显示“待审阅候选”，而不是暗示已写入正式稿。只有用户执行已有保存/审批动作后才能显示已写入。

- [ ] **Step 6: 添加预览样式并限制桌面宽度**

CSS 至少包含：

```css
#contextDialog { width: min(760px, calc(100vw - 48px)); }
.context-scope-summary { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px 16px; }
.context-preview-section { margin-top: 14px; padding-top: 12px; border-top: 1px solid var(--line); }
.context-warning { margin-top: 12px; padding: 10px; border-radius: 8px; background: var(--warning-soft, #fff4d6); color: var(--warning, #7a5200); }
```

首版不增加手机断点或手机专用交互。

- [ ] **Step 7: 运行语法、Node 和完整 Python 回归**

Run: `node --check creative_claw/web/app.js`

Expected: exit 0。

Run: `node --test tests/js/context-state.test.cjs`

Expected: PASS。

Run: `.\.venv\Scripts\python.exe -m unittest discover -s tests -v`

Expected: 全部 PASS。

- [ ] **Step 8: 执行条件提交或记录建议提交信息**

```powershell
if (git rev-parse --is-inside-work-tree 2>$null) {
  git add creative_claw/web/index.html creative_claw/web/app.js creative_claw/web/app.css tests/js/context-state.test.cjs
  git commit -m "feat: preview model context before generation"
} else {
  Write-Output "Not a Git work tree; suggested commit: feat: preview model context before generation"
}
```

### Task 8: 前端类型化证据徽标、定位和未知引用警告

**Files:**
- Modify: `creative_claw/web/app.js:1210-1236` 及现有 `renderEvidence()`
- Modify: `creative_claw/web/app.css:285-307`
- Modify: `tests/js/context-state.test.cjs`

**Interfaces:**
- Consumes: Task 4/5 `evidence_refs`、`citation_validation`；Task 6 `parseCitationTokens()`。
- Produces: 回答中的类型化引用按钮、证据面板分类渲染和未知编号警告。

- [ ] **Step 1: 扩展纯函数测试，锁定编号到证据对象的映射行为**

在 `context-state.js` 增加并导出：

```javascript
function indexEvidenceRefs(refs) {
  return Object.fromEntries((refs || []).map((item) => [item.ref, item]));
}
```

测试：

```javascript
test("indexes typed evidence by stable ref", () => {
  const index = indexEvidenceRefs([{ ref: "T1", kind: "timeline" }, { ref: "K1", kind: "kline" }]);
  assert.equal(index.T1.kind, "timeline");
  assert.equal(index.K1.kind, "kline");
});
```

- [ ] **Step 2: 改造 `addMessage` 输入，停止按 citation 数组位置重新生成 `[C#]`**

推荐签名：

```javascript
function addMessage(role, text, evidenceRefs = [], options = {})
```

对 assistant 回答执行 `parseCitationTokens(text)`；每个已知 token 创建 `.citation-link[data-kind]` 按钮，按钮文本直接使用 `[T1] 时间线标题` 等真实编号。未知 token 不创建可点击伪证据，并把编号交给警告区。

- [ ] **Step 3: 为不同证据类型实现明确定位行为**

固定映射：

- `source`：设置 `state.evidence` 并选择 `source:{document_id}`。
- `graph`：选择实体 `entity:{id}`；关系若没有画布节点则在 inspector 显示 payload。
- `timeline`：选择 `scene:{event_id}`。
- `kline`：选择关联 `scene:{timeline_event_id}` 并打开或聚焦对应人物/维度 K 线编辑区。
- `version`、`rule`、`issue`：本阶段只在 inspector 显示详情，不伪造画布节点。

- [ ] **Step 4: 改造证据面板按 kind 显示徽标**

使用中文显示名：

```javascript
const EVIDENCE_LABELS = {
  source: "来源",
  graph: "图谱",
  timeline: "时间线",
  kline: "K 线",
  version: "版本",
  rule: "规则",
  issue: "问题",
};
```

每项显示真实 `ref`、类型、标题、定位摘要，不再把所有证据都当作文档 citation。

- [ ] **Step 5: 显示 API 引用校验警告**

当 `citation_validation.unknown.length > 0` 时，在回答下追加：

```text
引用警告：回答包含当前上下文中不存在的编号 [K9]。请勿将该引用视为已验证事实。
```

未知引用不阻止用户阅读候选稿，但候选状态必须标记为“需复核”。

- [ ] **Step 6: 增加类型徽标与警告样式**

CSS 使用 `data-kind` 区分颜色，但不能只靠颜色传达类型；按钮文字必须保留“来源/图谱/时间线/K 线”等标签。添加键盘 focus 样式，确保按钮可操作。

- [ ] **Step 7: 运行 Node、语法和 Python 回归**

Run: `node --test tests/js/context-state.test.cjs`

Expected: PASS。

Run: `node --check creative_claw/web/app.js`

Expected: exit 0。

Run: `.\.venv\Scripts\python.exe -m unittest discover -s tests -v`

Expected: 全部 PASS。

- [ ] **Step 8: 执行条件提交或记录建议提交信息**

```powershell
if (git rev-parse --is-inside-work-tree 2>$null) {
  git add creative_claw/web/context-state.js creative_claw/web/app.js creative_claw/web/app.css tests/js/context-state.test.cjs
  git commit -m "feat: render typed evidence and citation warnings"
} else {
  Write-Output "Not a Git work tree; suggested commit: feat: render typed evidence and citation warnings"
}
```

### Task 9: Playwright 浏览器验收与显式真实模型验证

**Files:**
- Create: `scripts/e2e_context_preview.py`
- Create: `tests/test_real_llm.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: Task 5 API、Task 7 预览对话框、Task 8 类型化证据。
- Produces: 可重复的隔离浏览器报告；默认跳过、显式启用的真实模型测试。

- [ ] **Step 1: 创建浏览器验收脚本的隔离运行骨架**

脚本必须：

1. 在临时目录创建 SQLite 数据库和项目素材。
2. 使用测试专用环境变量启动 `127.0.0.1:8767`，不得占用主服务 `8766`。
3. 等待 `/v1/config` 或项目列表接口健康。
4. 用 Playwright 打开页面。
5. 在 `finally` 中关闭浏览器和服务进程。
6. 报告只记录 scope、计数、证据编号、通过/失败和截图路径，不记录模型密钥或完整请求头。

建议报告路径由 `--output-dir` 参数控制，默认使用系统临时目录；只有调用者明确传入项目外目录时才持久保存。

- [ ] **Step 2: 编写无真实模型的浏览器联动场景**

脚本通过 API 夹具创建至少：

- 人物“顾遥”和“林川”。
- E18S1、E18S2、E18S3 三个时间线事件。
- E18S2 的“顾遥 / 信任度”场景 K 线。
- E18S3 的“林川 / 决心”场景 K 线。

浏览器断言：

1. 选择 E18S2 与顾遥/信任度后点击“预览模型上下文”。
2. 对话框显示 E18S2、顾遥、信任度、时间线数大于 0、K 线数大于 0，并出现 `[T#]` 与 `[K#]`。
3. 关闭预览，切换到 E18S3 与林川/决心，再次预览。
4. 第二次预览不得残留顾遥/信任度，且 `resolved_scope.scene_id` 已改变。
5. 捕获两次 `/context` 请求体，确认请求内没有“沈霜”，且 scope 与 UI 选择一致。
6. 页面显示“确认上下文 → 运行模型 → 审阅候选 → 接受或拒绝”的工序提醒。

- [ ] **Step 3: 运行浏览器脚本并保存最小验收产物**

Run: `.\.venv\Scripts\python.exe scripts/e2e_context_preview.py --port 8767 --output-dir "$env:TEMP\creative-claw-phase1-e2e"`

Expected: exit 0；报告中两个预览场景全部 PASS；截图可打开；服务进程已退出。

- [ ] **Step 4: 创建默认跳过的真实模型测试**

测试类使用：

```python
@unittest.skipUnless(
    os.getenv("CREATIVE_CLAW_REAL_LLM_TEST") == "1",
    "set CREATIVE_CLAW_REAL_LLM_TEST=1 to run paid real-model verification",
)
class RealLlmContextTests(unittest.TestCase):
    ...
```

测试前检查 `CREATIVE_CLAW_LLM_API_KEY` 存在，否则使用 `self.skipTest("model key is not configured")`。测试数据必须使用随机项目名和非演示人物“顾遥”。问题要让答案必须依赖时间线与 K 线，例如：

```text
只依据证据说明顾遥在当前场景做了什么，以及她的信任度从 open 到 close 如何变化。每个事实分别引用时间线和 K 线编号。
```

断言：

- 回答非空。
- `resolved_scope.scene_id` 等于当前事件。
- `evidence_refs` 同时包含 `timeline` 和 `kline`。
- 回答至少使用一个 `[T#]` 和一个 `[K#]`。
- `citation_validation.unknown == []`。
- 测试输出不打印环境变量、Authorization header 或完整客户端配置。

- [ ] **Step 5: 先验证默认测试不会产生模型调用**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_real_llm -v`

Expected: SKIPPED，原因提示需要 `CREATIVE_CLAW_REAL_LLM_TEST=1`。

- [ ] **Step 6: 仅在调用者显式注入临时环境变量后运行一次真实模型验证**

```powershell
$env:CREATIVE_CLAW_REAL_LLM_TEST='1'
.\.venv\Scripts\python.exe -m unittest tests.test_real_llm -v
Remove-Item Env:CREATIVE_CLAW_REAL_LLM_TEST -ErrorAction SilentlyContinue
```

Expected: PASS。若服务暂不可用，记录为外部依赖未完成验证，不得把密钥写进重试命令、文件或报告。

- [ ] **Step 7: 更新 README 的上下文契约和安全说明**

README 增加：

- 新请求推荐使用 `scope`。
- 旧 `filters/character_name/dimension` 暂时兼容。
- 七类证据编号说明。
- “预览模型上下文”操作顺序。
- Python、Node、浏览器和真实模型测试命令。
- 密钥只通过进程环境或已有安全配置流程提供，不粘贴到源码和命令历史。

同时删除 README 中把“沈霜 / 知情度”描述成系统默认值的表述；可保留为明确标注的示例数据，但不能让用户误以为其他人物无法工作。

- [ ] **Step 8: 执行条件提交或记录建议提交信息**

```powershell
if (git rev-parse --is-inside-work-tree 2>$null) {
  git add scripts/e2e_context_preview.py tests/test_real_llm.py README.md
  git commit -m "test: verify browser and real-model context linkage"
} else {
  Write-Output "Not a Git work tree; suggested commit: test: verify browser and real-model context linkage"
}
```

### Task 10: Phase 1 总验收与交付记录

**Files:**
- Modify: `README.md`
- Verify: `creative_claw/context.py`
- Verify: `creative_claw/evidence.py`
- Verify: `creative_claw/repository.py`
- Verify: `creative_claw/retrieval.py`
- Verify: `creative_claw/llm.py`
- Verify: `creative_claw/api.py`
- Verify: `creative_claw/web/context-state.js`
- Verify: `creative_claw/web/index.html`
- Verify: `creative_claw/web/app.js`
- Verify: `creative_claw/web/app.css`
- Verify: `tests/`
- Verify: `scripts/e2e_context_preview.py`

**Interfaces:**
- Consumes: Tasks 1-9 全部交付物。
- Produces: Phase 1 可复现验收结果、变更清单和进入 Phase 2 的稳定上下文契约。

- [ ] **Step 1: 编译 Python 并检查语法**

Run: `.\.venv\Scripts\python.exe -m compileall creative_claw tests scripts/e2e_context_preview.py`

Expected: exit 0，无 SyntaxError。

- [ ] **Step 2: 运行全部 Python 自动化测试**

Run: `.\.venv\Scripts\python.exe -m unittest discover -s tests -v`

Expected: 全部非真实模型测试 PASS；`tests.test_real_llm` 默认 SKIPPED；原有 8 个测试继续 PASS。

- [ ] **Step 3: 运行全部 Node 测试与脚本语法检查**

Run: `node --test tests/js/context-state.test.cjs`

Expected: 全部 PASS。

Run: `node --check creative_claw/web/context-state.js`

Expected: exit 0。

Run: `node --check creative_claw/web/app.js`

Expected: exit 0。

- [ ] **Step 4: 扫描演示人物和旧引用协议残留**

Run: `Select-String -Path creative_claw/web/app.js -Pattern 'character_name: "沈霜"|dimension: "知情度"'`

Expected: 无输出。

Run: `Select-String -Path creative_claw/llm.py,creative_claw/retrieval.py,creative_claw/web/app.js -Pattern '\[C\d|\[C#\]|\[C1\]'`

Expected: 业务逻辑无旧 `[C#]` 协议；若只出现在迁移说明或兼容测试中，逐项确认不会发送给模型或显示为新回答引用。

- [ ] **Step 5: 运行浏览器端到端验收**

Run: `.\.venv\Scripts\python.exe scripts/e2e_context_preview.py --port 8767 --output-dir "$env:TEMP\creative-claw-phase1-e2e"`

Expected: 两组人物/场景/维度切换均 PASS；预览与请求 scope 一致；时间线和 K 线计数均大于 0；工序提醒可见。

- [ ] **Step 6: 校验服务和账本原能力不回归**

使用现有端到端测试确认：来源→索引→搜索、实体→画布、关系→图谱、场景→时间线、正文→版本→补丁→账本、场景 K 线→父周期聚合、拒绝任务→正式稿不变、账本校验仍通过。

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_end_to_end tests.test_maintenance -v`

Expected: 全部 PASS。

- [ ] **Step 7: 检查密钥未进入项目和验收产物**

不得在命令中写出密钥字面值。使用通用模式检查常见密钥前缀和 Authorization header：

Run: `Get-ChildItem creative_claw,tests,scripts,docs -Recurse -File | Select-String -Pattern 'Bearer\s+[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9_-]{20,}'`

Expected: 无真实密钥匹配。测试中若存在用于解析的短假值，必须长度不足以被误认为真实凭据。

- [ ] **Step 8: 在 README 记录最终稳定契约和已知边界**

记录以下事实：

- 新前端只发送 `scope`。
- 后端仍兼容旧请求，但兼容参数不再是前端主路径。
- 当前上下文按 `scene_id` 精确定位，并自动加载相邻时间线和关联场景 K 线。
- 预览和实际聊天共享同一 scope 快照。
- 类型化引用只保证编号存在性；语义蕴含校验属于后续审阅能力。
- Phase 1 不实现工作流数据库、三模式重构、首次建项向导和完整影响中心，这些按路线图进入 Phase 2-5。

- [ ] **Step 9: 生成交付清单**

交付记录至少列出：

```text
变更文件：逐项完整路径
Python 测试：命令、通过数、跳过数
Node 测试：命令、通过数
浏览器验收：报告与截图完整路径
真实模型验证：未启用 / 通过 / 当前数据暂不可获取
密钥检查：通过
已知边界：类型化引用尚未做语义蕴含判断
建议提交：feat: complete phase 1 context linkage
```

不得写入模型密钥、Authorization header、完整模型请求或含密钥的环境转储。

- [ ] **Step 10: 执行条件最终提交或记录建议提交信息**

```powershell
if (git rev-parse --is-inside-work-tree 2>$null) {
  git add creative_claw tests scripts README.md docs/superpowers/plans
  git commit -m "feat: complete phase 1 context linkage"
} else {
  Write-Output "Not a Git work tree; suggested commit: feat: complete phase 1 context linkage"
}
```

---

## Phase 1 Definition of Done

只有同时满足以下条件，Phase 1 才可关闭：

- 前端请求中不存在写死的演示人物或维度。
- 选择不同场景、人物、维度后，`scope`、预览、实际聊天和证据同步变化。
- 当前场景及相邻时间线自动进入上下文。
- 当前邻域关联的场景 K 线自动进入上下文，且不会被解释为 high/low 的时间顺序。
- `/context` 和 `/chat` 都返回 `resolved_scope`、`evidence_refs`、`citation_validation`。
- `[S#] [G#] [T#] [K#] [V#] [R#] [I#]` 编号互不混用，未知编号有可见警告。
- 模型调用前必须经过可见上下文预览；预览与实际请求使用同一 scope 快照。
- 页面明确提示“确认上下文 → 运行模型 → 审阅候选 → 接受或拒绝”，且 AI 返回不等于正式稿已修改。
- Python、Node、浏览器验收通过，原有测试不回归。
- 真实模型测试默认跳过，显式启用时不泄露密钥。
- 项目、报告和日志中不存在用户提供的真实密钥。

## Handoff to Phase 2

Phase 2 可以依赖以下稳定接口：

- `ContextScope` 作为全局创作上下文快照。
- `resolved_scope` 作为服务端实际采用的上下文。
- `evidence_refs` 作为审阅、影响分析和工序助手的统一证据入口。
- `scene_id` 与 `timeline_event_id` 作为当前生产场景和 K 线关联的桥梁。

Phase 2 不得重新引入另一套人物、场景或证据状态；工作流阶段和生产单元应扩展 `ContextScope`，而不是绕开它。
