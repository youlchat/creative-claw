# Phase 2 生产工作流内核 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立媒介无关的后端生产模型，使阶段、生产单元、交付物、不可覆盖版本、依赖、审阅和影响成为可迁移、可审计、可通过 API 操作的一等对象。

**Architecture:** 在现有 Flask + SQLite 架构上增加 schema v4、内置模板定义和独立 `WorkflowService`。所有状态变更与正式版本写入只经过服务层；版本写入、依赖传播、审阅过期、影响记录和账本事件在同一 SQLite 事务内完成，现有 `documents`、`timeline_events`、`ohlc_points` 与 `ledger_events` 保持原表和原语义。

**Tech Stack:** Python 3.11+、Flask 3、SQLite、Python `unittest`。

## Global Constraints

- 首版只内置 `novel`（长篇小说）和 `vertical_short_drama`（竖屏短剧）模板，底层对象保持媒介无关。
- 面向单人创作者；无模型配置时，全部工作流、版本、审阅和影响操作仍可手工完成。
- AI 或人工候选不得直接覆盖正式稿；每次正式保存必须新增 `artifact_versions` 行。
- 上游正式版本变化必须生成影响记录，并将源交付物及可达下游的有效审阅标记为 `stale`。
- 阶段状态只能通过 `WorkflowService.transition_stage()` 修改；跳过阶段必须记录非空理由。
- 保留现有 `projects`、`documents`、`chunks`、`entities`、`relations`、`timeline_events`、`ohlc_points`、`ledger_events`、`tasks` 和 `tool_runs`。
- schema v4 迁移必须幂等：旧 `document` 映射为 `source` artifact，旧 `timeline_event` 映射为 `scene` production unit 与 `manuscript` artifact；旧记录和账本哈希链不得删除或重写。
- 自动测试不得调用真实模型，不得把 API Key 写入源码、数据库、浏览器存储、报告或日志。
- 项目不是 Git 仓库；不得自动初始化 Git。每项“提交”改为记录建议提交信息。

---

## File Structure

### 新建

- `creative_claw/workflow_templates.py`：两个内置媒介模板的不可变定义与模板种子写入。
- `creative_claw/workflow.py`：工作流、阶段、生产单元、交付物、版本、依赖、审阅和影响的唯一服务层。
- `tests/test_workflow_migration.py`：schema v4、模板种子和旧项目幂等迁移。
- `tests/test_workflow.py`：领域规则、版本冲突、依赖传播、审阅过期、影响与账本事务。
- `tests/test_workflow_api.py`：Phase 2 HTTP 契约与 UTF-8 行为。

### 修改

- `creative_claw/db.py`：schema version 4、新表、索引、种子与 legacy backfill。
- `creative_claw/ledger.py`：允许调用者把账本事件写入已有事务，同时保持现有调用兼容。
- `creative_claw/repository.py`：统计结果加入 Phase 2 对象计数，不改现有返回字段。
- `creative_claw/api.py`：挂接 `WorkflowService` 并公开模板、工作流、阶段、单元、交付物、版本、依赖、审阅与影响端点。
- `README.md`：记录 Phase 2 稳定契约、验证证据、已知边界和建议提交信息。
- `未完成的任务.txt`：最终追加 Phase 2 完成状态与复现命令。

## Public Interfaces Locked by This Plan

```python
class WorkflowService:
    def list_templates(self) -> list[dict]: ...
    def instantiate_workflow(self, project_id: str, template_key: str, *, version: int | None = None, name: str | None = None) -> dict: ...
    def get_project_workflow(self, project_id: str) -> dict: ...
    def create_production_unit(self, project_id: str, unit_type: str, title: str, *, parent_id: str | None = None, position: int = 0, branch: str = "main", attrs: dict | None = None) -> dict: ...
    def transition_stage(self, project_id: str, stage_id: str, status: str, *, exception_reason: str | None = None, actor: str = "user") -> dict: ...
    def create_artifact(self, project_id: str, artifact_type: str, title: str, *, stage_id: str | None = None, unit_id: str | None = None, branch: str = "main", attrs: dict | None = None, actor: str = "user") -> dict: ...
    def get_artifact(self, project_id: str, artifact_id: str) -> dict: ...
    def transition_artifact_status(self, project_id: str, artifact_id: str, status: str, *, actor: str = "user") -> dict: ...
    def save_artifact_version(self, project_id: str, artifact_id: str, content: str, *, expected_current_version_id: str | None, change_summary: str, source_kind: str = "user", actor: str = "user", metadata: dict | None = None) -> dict: ...
    def list_artifact_versions(self, project_id: str, artifact_id: str) -> list[dict]: ...
    def add_dependency(self, project_id: str, upstream_artifact_id: str, downstream_artifact_id: str, dependency_type: str, *, actor: str = "user") -> dict: ...
    def create_review(self, project_id: str, artifact_id: str, review_type: str, input_version_id: str, *, summary: str = "", actor: str = "user", metadata: dict | None = None) -> dict: ...
    def list_impacts(self, project_id: str, *, status: str | None = None) -> list[dict]: ...

class VersionConflictError(ValueError): ...
```

阶段状态为 `not_started`、`in_progress`、`pending_review`、`passed`、`needs_revision`、`stale`、`locked`、`skipped`。交付物状态为 `empty`、`draft`、`ready_for_review`、`approved`、`needs_revision`、`stale`、`locked`。依赖类型采用设计规格中的 `contains`、`requires`、`constrains`、`derives_from`、`measures`、`affects`、`evidence_for`、`adapts_from`。

---

### Task 1: schema v4、内置模板与旧数据迁移

**Files:**
- Create: `creative_claw/workflow_templates.py`
- Create: `tests/test_workflow_migration.py`
- Modify: `creative_claw/db.py`

**Interfaces:**
- Produces: `SCHEMA_VERSION == 4`；`install_builtin_templates(connection)`；Phase 2 十张表及其索引。

- [ ] **Step 1: 写失败测试，锁定模板、表、legacy 映射与幂等性**

```python
def test_schema_v4_seeds_templates_and_migrates_legacy_rows_idempotently(self):
    self.database.initialize()
    # 初始化后写入一个旧 document/chunk、一个 timeline_event、一个 OHLC 和一个 ledger event，
    # 把 user_version 降为 3 后再次 initialize，模拟旧项目升级。
    self.assertEqual(self.database.schema_version(), 4)
    self.assertEqual([row["template_key"] for row in templates], ["novel", "vertical_short_drama"])
    self.assertEqual(stage_counts, {"novel": 13, "vertical_short_drama": 16})
    self.assertEqual(source_artifact["source_document_id"], document_id)
    self.assertEqual(scene_unit["source_timeline_event_id"], timeline_id)
    self.assertEqual(scene_artifact["source_timeline_event_id"], timeline_id)
    self.assertTrue(Ledger(self.database).verify(project_id)["valid"])
    self.database.initialize()
    self.assertEqual(mapped_counts_after_second_run, mapped_counts_after_first_run)
```

- [ ] **Step 2: 运行迁移测试并确认失败原因是 schema 仍为 3 且新表不存在**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_workflow_migration -v`

Expected: FAIL，指出 `SCHEMA_VERSION` 或 `workflow_templates` 不存在。

- [ ] **Step 3: 定义两个精确模板**

`novel` 使用规格第 14 节的 13 个阶段；`vertical_short_drama` 使用规格第 15 节的 16 个阶段。每个阶段定义稳定 `key`、中文 `name`、`description`、`completion_criteria` 和至少一个 `required_artifact_type`。模板以 `template_key + version` 唯一，内置版本均为 `1`。

- [ ] **Step 4: 增加 schema v4 表和索引**

表为 `workflow_templates`、`project_workflows`、`workflow_stages`、`production_units`、`artifacts`、`artifact_versions`、`artifact_dependencies`、`reviews`、`review_issues`、`impact_records`。所有 project-scoped 行必须带外键并在 project 删除时级联；历史版本只能删除其 artifact 时级联。

- [ ] **Step 5: 实现 `_migrate_v4()`**

迁移通过 `source_document_id` 与 `source_timeline_event_id` 唯一索引保证幂等。document 内容按 chunk ordinal 用两个换行连接；timeline_event 的 description 作为 scene manuscript 初始版本。迁移不追加或改写旧 `ledger_events`。

- [ ] **Step 6: 运行目标测试与原有迁移回归**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_workflow_migration tests.test_maintenance -v`

Expected: PASS。

- [ ] **Step 7: 记录建议提交信息**

`feat(workflow): add schema v4 and legacy production migration`

---

### Task 2: 工作流实例、阶段规则与生产单元层级

**Files:**
- Create: `creative_claw/workflow.py`
- Create: `tests/test_workflow.py`

**Interfaces:**
- Consumes: Task 1 新表和模板 definition JSON。
- Produces: `list_templates()`、`instantiate_workflow()`、`get_project_workflow()`、`transition_stage()`、`create_production_unit()`。

- [ ] **Step 1: 写失败测试，锁定两模板实例化和生产单元归属**

```python
def test_instantiates_both_media_templates_and_validates_unit_parent(self):
    novel = service.instantiate_workflow(project_a, "novel")
    drama = service.instantiate_workflow(project_b, "vertical_short_drama")
    self.assertEqual(len(novel["stages"]), 13)
    self.assertEqual(len(drama["stages"]), 16)
    volume = service.create_production_unit(project_a, "volume", "第一卷")
    chapter = service.create_production_unit(project_a, "chapter", "第一章", parent_id=volume["id"])
    self.assertEqual(chapter["parent_id"], volume["id"])
    with self.assertRaisesRegex(ValueError, "same project"):
        service.create_production_unit(project_b, "scene", "越界场景", parent_id=volume["id"])
```

- [ ] **Step 2: 写失败测试，锁定阶段转换矩阵、完成条件和跳过理由**

允许转换为：`not_started -> in_progress|skipped`、`in_progress -> pending_review|needs_revision|skipped`、`pending_review -> passed|needs_revision`、`needs_revision|stale -> in_progress`、`passed -> stale|locked`。`skipped` 必须有理由，`locked` 不可再转移；`pending_review -> passed` 时模板声明的 required artifact types 必须在该阶段存在且状态为 `approved` 或 `locked`。

- [ ] **Step 3: 运行测试并确认 `WorkflowService` 尚不存在**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_workflow -v`

Expected: FAIL with import error。

- [ ] **Step 4: 实现只读模板和工作流实例化**

一个项目只允许一个 `project_workflows` 实例；实例化在一个事务内复制模板阶段，保留稳定位置和完成条件。返回对象包含 `template_key`、`media_type`、按 position 排序的 `stages` 与状态计数。

- [ ] **Step 5: 实现生产单元与阶段服务规则**

允许的底层 unit types 为 `work`、`volume`、`chapter`、`episode`、`act`、`scene`、`sequence`、`beat`、`quest`、`branch`。父级必须属于同一 project 与 branch。任何阶段状态写入都由服务层完成，并追加同事务账本事件。

- [ ] **Step 6: 运行目标测试和账本回归**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_workflow tests.test_end_to_end -v`

Expected: PASS，账本校验仍有效。

- [ ] **Step 7: 记录建议提交信息**

`feat(workflow): instantiate stages and production units`

---

### Task 3: 交付物版本、依赖、审阅过期和影响传播

**Files:**
- Modify: `creative_claw/workflow.py`
- Modify: `creative_claw/ledger.py`
- Modify: `tests/test_workflow.py`

**Interfaces:**
- Consumes: Task 2 的工作流、阶段和单元查询。
- Produces: `create_artifact()`、`get_artifact()`、`transition_artifact_status()`、`save_artifact_version()`、`list_artifact_versions()`、`add_dependency()`、`create_review()`、`list_impacts()`。

- [ ] **Step 1: 写失败测试，锁定正式版本永不覆盖与乐观冲突**

```python
first = service.save_artifact_version(project_id, artifact_id, "v1", expected_current_version_id=None, change_summary="初稿")
second = service.save_artifact_version(project_id, artifact_id, "v2", expected_current_version_id=first["version"]["id"], change_summary="修订")
self.assertEqual([row["content"] for row in service.list_artifact_versions(project_id, artifact_id)], ["v1", "v2"])
with self.assertRaisesRegex(ValueError, "version conflict"):
    service.save_artifact_version(project_id, artifact_id, "lost", expected_current_version_id=first["version"]["id"], change_summary="过期保存")
self.assertEqual(service.get_artifact(project_id, artifact_id)["current_version_id"], second["version"]["id"])
```

- [ ] **Step 2: 写失败测试，锁定递归影响和审阅过期**

建立 `bible -> outline -> manuscript` 两级依赖；在 outline/manuscript 当前版本上创建 valid review；保存 bible v2 后断言两个 review 均为 stale、两个 impact path 分别为 2/3 个 artifact id、下游 approved artifact 变为 stale、正式内容未被改写、ledger 新事件有效。

- [ ] **Step 3: 写失败测试，锁定依赖跨项目、self-edge 和环路拒绝**

添加 `manuscript -> bible` 必须抛出包含 `cycle` 的 ValueError，且数据库没有残留边或影响记录。

- [ ] **Step 4: 运行测试并确认缺少交付物服务方法**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_workflow -v`

Expected: FAIL with missing method。

- [ ] **Step 5: 让账本支持调用者事务**

给 `Ledger.append()` 增加仅关键字参数 `connection: sqlite3.Connection | None = None`。有 connection 时复用当前事务；无 connection 时保持现有行为。事件哈希算法和返回结构不得变化。

- [ ] **Step 6: 实现版本、依赖、审阅和递归传播**

使用 SQLite recursive CTE 获取每个可达下游及完整最短路径。版本插入、artifact current_version 更新、review stale、downstream artifact stale、impact insert 和 ledger append 在单一 `Database.connect()` 上完成。失败必须整笔回滚。

交付物状态转换矩阵为：`empty -> draft`（首次版本自动完成）、`draft -> ready_for_review`、`ready_for_review -> approved|needs_revision`、`needs_revision|stale -> draft`、`approved -> stale|locked`；`locked` 不可再写版本或改变状态。每次显式状态转换追加同事务账本事件。

- [ ] **Step 7: 运行领域测试和完整 Python 回归**

Run: `.\.venv\Scripts\python.exe -m unittest discover -s tests -v`

Expected: PASS；真实模型测试仍默认 skipped。

- [ ] **Step 8: 记录建议提交信息**

`feat(workflow): add immutable versions reviews and impact propagation`

---

### Task 4: Phase 2 HTTP 契约

**Files:**
- Create: `tests/test_workflow_api.py`
- Modify: `creative_claw/api.py`

**Interfaces:**
- Consumes: Task 2/3 的 `WorkflowService`。
- Produces: 以下 JSON API：
  - `GET /v1/workflow-templates`
  - `POST|GET /v1/projects/<project_id>/workflow`
  - `POST /v1/projects/<project_id>/production-units`
  - `POST /v1/projects/<project_id>/workflow-stages/<stage_id>/transition`
  - `POST /v1/projects/<project_id>/artifacts`
  - `GET /v1/projects/<project_id>/artifacts/<artifact_id>`
  - `POST /v1/projects/<project_id>/artifacts/<artifact_id>/transition`
  - `POST|GET /v1/projects/<project_id>/artifacts/<artifact_id>/versions`
  - `POST /v1/projects/<project_id>/artifact-dependencies`
  - `POST /v1/projects/<project_id>/reviews`
  - `GET /v1/projects/<project_id>/impacts`

- [ ] **Step 1: 写失败 API 测试，完成一条无模型生产链**

测试用中文项目名创建 novel workflow、volume/chapter、故事圣经和正文 artifacts、两个初始版本、依赖与正文 review；再保存故事圣经 v2，断言响应 `sync.stale_review_ids` 和 `sync.impact_ids` 非空，`GET impacts` 返回 UTF-8 中文摘要，ledger verify 为 valid。

- [ ] **Step 2: 写失败 API 测试，锁定 409 版本冲突**

连续两次用同一旧 `expected_current_version_id` 保存，第二次返回 HTTP 409，响应 `error` 含 `version conflict`，当前版本与 ledger event_count 不变。

- [ ] **Step 3: 运行测试并确认路由为 404**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_workflow_api -v`

Expected: FAIL with 404。

- [ ] **Step 4: 注册服务和端点**

端点只做 payload 清洗和 HTTP 状态映射，不复制领域规则。创建返回 201；未知对象复用 404；验证错误返回 400；版本冲突用专用 `VersionConflictError` 返回 409。

- [ ] **Step 5: 运行 API 测试和原有上下文 API 回归**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_workflow_api tests.test_context_api tests.test_end_to_end -v`

Expected: PASS。

- [ ] **Step 6: 记录建议提交信息**

`feat(api): expose production workflow contracts`

---

### Task 5: 统计、文档与 Phase 2 全量验收

**Files:**
- Modify: `creative_claw/repository.py`
- Modify: `README.md`
- Modify: `未完成的任务.txt`

**Interfaces:**
- Produces: `knowledge_stats()["structured"]` 新增 workflow、unit、artifact、version、dependency、review、impact 计数；可复现的 Phase 2 验收记录。

- [ ] **Step 1: 扩展统计行为测试**

在现有 workflow API 或领域测试中创建对象后，断言 `structured` 包含 `workflow_stages`、`production_units`、`artifacts`、`artifact_versions`、`artifact_dependencies`、`reviews`、`impact_records`，且旧计数字段仍存在。

- [ ] **Step 2: 运行测试并确认新计数字段缺失**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_workflow_api -v`

Expected: FAIL on missing structured count。

- [ ] **Step 3: 最小扩展统计并更新 README**

README 记录 schema v4、两个模板、服务/API 契约、legacy 映射、无模型路径、影响传播、测试命令、已知边界（Phase 3 才做三模式 UI，Phase 4 才做向导/流程编辑器，Phase 5 才做锁稿/导出工业验收）。

- [ ] **Step 4: Python 编译和完整测试**

```powershell
.\.venv\Scripts\python.exe -m compileall -q creative_claw tests
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Expected: 全部 PASS，真实模型仅默认 skipped。

- [ ] **Step 5: Node 与浏览器静态回归**

```powershell
node --test tests/js/*.test.cjs
node --check creative_claw/web/context-state.js
node --check creative_claw/web/app.js
```

Expected: 全部 PASS。

- [ ] **Step 6: 安全与兼容扫描**

扫描源码、测试、README 和 Phase 2 报告中的 `sk-`、`Bearer `、`api_key` 值；只允许配置字段名和环境变量说明，不允许看似真实的凭据。再次运行 legacy migration 测试和 ledger verify。

- [ ] **Step 7: 更新续作记录**

在根目录 `未完成的任务.txt` 追加 UTF-8 Phase 2 完成报告：修改文件、测试数量、命令、结果、已知边界和建议提交信息。

- [ ] **Step 8: 记录建议提交信息**

`feat: complete phase 2 production workflow kernel`

---

## Phase 2 Definition of Done

- schema v4 可从 v3 幂等升级，旧项目、来源、场景、K 线和账本均保留。
- novel 13 阶段与 vertical_short_drama 16 阶段都能实例化。
- 生产单元可形成项目内层级，跨项目父级被拒绝。
- 正式稿每次保存均产生新版本；过期 expected version 被 409 拒绝且无部分写入。
- 依赖图拒绝环路；上游新版本产生递归影响并使相关有效审阅过期。
- 阶段转换由服务规则保护，required artifacts 不满足时不能 passed，跳过必须有理由。
- 全部能力可在无模型配置下通过 Python API 和 HTTP API 完成。
- 原有 Python、Node、语法、账本和 Phase 1 上下文测试无回归。
