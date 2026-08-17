# Creative Claw Phase 2.5：参考文本逆向与差异化草稿设计规格

- 日期：2026-07-29
- 状态：用户已确认核心方向，并授权后续设计决策直接推进
- 定位：Phase 2 生产工作流内核与 Phase 3 三模式前端之间的前提能力
- 产品模式：受编排的多代理基础架构 + 类型化流水线 + 作者确认门禁

## 1. 目标

用户提供一篇短文本或整部长篇参考文本。系统不直接仿写，而是完整逆向其可观察的创作机制，形成带证据、置信度、冲突和版本的可编辑 `ReferenceBlueprint`。用户再粘贴一份新的自然语言创作设定，系统将其整理成结构化设定，并把抽象机制迁移为新的 `TargetBlueprint`。作者确认目标蓝图后，系统才允许按卷、章、场景逐生产单元生成草稿候选。

草稿代理不得读取参考原文。参考原文只允许被抽取代理、证据定位器和相似度安全代理读取。任何 AI 输出都是 candidate/proposal，作者接受后才创建正式 artifact version。

## 2. 已确认的产品决策

1. 默认完整抽取全部可观察维度，不让用户预先挑选分析维度。
2. 同时支持短文本即时分析与长篇后台分批分析；长篇任务可暂停、恢复和只重跑失败批次。
3. 先生成可编辑的参考文本创作机制蓝图，再处理新设定。
4. 新设定使用自然语言输入，系统自动整理成可编辑结构化设定表。
5. 系统先生成新作品生产蓝图；作者确认后，才按卷、章、场景逐单元生成草稿。
6. 每项抽取结论保留原文证据定位、置信度、代理信息和解释状态。
7. 歧义保留多个解释，并记录冲突关系，进入待作者确认队列。
8. 草稿生成上下文与参考原文严格隔离。
9. 候选保存前使用分层相似度门禁：表达复制硬阻止；独特事件链一一对应需要整改；抽象母题和功能相似允许。
10. 多代理是基础架构，但代理受确定性 DAG 编排，不自由协商、不自行推进生产阶段。

## 3. 非目标

- 不承诺还原作者真实心理过程，只输出“由文本证据支持的可解释创作机制模型”。
- 不自动生成或接受整部正式稿。
- 不让草稿代理模仿在世作者的标志性语言表达。
- 不把参考文本中的专名、稀有短语、标志性句式作为生成素材。
- 不以单一相似度总分代替分层风险解释。
- 不把模型输出视为事实；低置信度和冲突解释必须可见。
- 不在此阶段实现云端队列、多用户协作或分布式执行。

## 4. 用户流程

### 4.1 参考文本入口

在桌面端增加“蓝图实验室”。用户可以粘贴文本或选择已导入的 document，填写标题和权利基础：`owned`、`licensed`、`public_domain` 或 `research_reference`。界面明确提示：若使用外部模型 API，参考文本将按批次发送给所配置的模型服务。

字符数不超过 20,000 的输入默认即时运行；更长文本创建后台任务。用户也可以强制把短文本放入后台。

### 4.2 参考蓝图审阅

界面显示层级树、跨层图谱、证据、置信度和冲突队列。作者可以：

- 修改节点字段；
- 确认或拒绝某个解释；
- 合并重复节点；
- 标记“不应迁移”的机制；
- 补充人工机制；
- 创建新的蓝图版本。

修改蓝图创建新 artifact version，并通过 Phase 2 依赖图让旧迁移方案、目标蓝图和相关审阅变为 stale。

### 4.3 新设定与目标蓝图

用户粘贴自然语言新设定。设定解析代理输出结构化字段：题材、受众、媒介、规模、世界规则、主要人物、人物目标、核心冲突、失败代价、主题方向、叙述偏好、必须包含、必须避免和预期结局。

迁移代理只读取确认后的抽象参考蓝图和结构化新设定，输出：

- 参考机制到目标机制的映射；
- 明确保留的抽象功能；
- 已改变的人物、世界、冲突、因果和结果；
- 被删除或新加入的机制；
- 独特事件链一一对应风险；
- 新作品的卷、章、场景与节拍生产蓝图。

目标蓝图必须由作者确认版本后才能进入草稿生成。

### 4.4 单元草稿

作者选择一个目标 production unit 并显式点击生成。单元规划代理先生成 unit plan，草稿代理再生成 candidate；连续性代理检查目标正典、前后单元和 K 线；相似度代理最后检查参考文本。通过门禁后，候选进入审阅区。作者接受候选时才调用 Phase 2 `save_artifact_version()`。

## 5. 受编排的多代理基础架构

### 5.1 编排器

`BlueprintOrchestrator` 是确定性状态机，负责 DAG、批次、输入版本、重试、检查点、取消和合并屏障。编排器不使用模型自行决定下一步。

所有代理实现统一契约：

```python
class BlueprintAgent(Protocol):
    name: str
    output_schema: str

    def run(self, task: AgentTask) -> AgentResult: ...
```

`AgentTask` 必须包含 job、batch、project、source/blueprint version、允许的上下文类型、提示版本和幂等键。`AgentResult` 必须包含类型化 JSON、证据引用、置信度、警告、模型公开配置和输入/输出哈希。

### 5.2 代理组

解析代理：

- `segmentation_agent`：作品、卷、章、场景、节拍边界；
- `evidence_locator_agent`：稳定字符范围、段落和章节定位；
- `entity_world_agent`：人物、地点、组织、规则和专名。

叙事代理：

- `character_function_agent`：人物功能、欲望、目标、阻力、代价和弧光；
- `relationship_agent`：关系、权力和关系变化；
- `event_causality_agent`：事件、行动、结果、因果和依赖；
- `turning_point_agent`：转折、揭示、悬念和信息差；
- `setup_payoff_agent`：伏笔、回收和未回收线索。

表现代理：

- `pov_time_agent`：视角、叙述距离、故事时间、叙述时间和顺序；
- `emotion_kline_agent`：情绪、人物状态与 K 线；
- `pacing_agent`：篇幅比例、节奏、对话密度和场景强度；
- `theme_motif_agent`：主题、母题和意象演化；
- `style_fingerprint_agent`：语言统计、稀有短语和标志性表达指纹，仅供安全检查。

综合与迁移代理：

- `hierarchy_synthesis_agent`：从节拍到作品逐级汇总；
- `interpretation_conflict_agent`：保留多解释并建立冲突组；
- `target_setting_agent`：自然语言新设定结构化；
- `mechanism_mapping_agent`：参考机制到目标机制映射；
- `target_blueprint_agent`：生成新作品生产蓝图。

草稿与审阅代理：

- `unit_planner_agent`；
- `draft_writer_agent`；
- `continuity_review_agent`；
- `similarity_safety_agent`。

### 5.3 并行与合并

同一批次的独立专业代理可并行；同一层级的综合代理等待全部必需代理完成。章节综合完成后才能进入卷级综合，卷级完成后才能进入作品级综合。任何必需代理失败时，该合并节点保持 blocked，不用空结果冒充完成。

## 6. 完整蓝图结构

蓝图层级为 `work → volume/phase → chapter/episode → scene → beat`。每个 `BlueprintNode` 包含：

- `node_type`、`source_locator`、`title`、`summary`、`narrative_function`；
- participants、角色功能、目标、阻力、代价、行动和结果；
- incoming/outgoing causal edges；
- conflict、turn、reveal、suspense、setup/payoff；
- POV、叙述距离、story/discourse time、location；
- emotion/K-line changes、pacing、length ratio、dialogue density；
- themes、motifs、imagery；
- `evidence_refs`、`confidence`、`status`、`agent_runs`；
- 多个 `interpretations` 和 `conflict_group_id`。

跨层结构包括人物功能图、关系图、因果图、信息揭示图、伏笔回收图、时间线、K 线和主题意象图。

“全部抽取”表示代理必须对所有适用字段给出三态结果：`observed`、`not_observed` 或 `uncertain`，不得通过缺少字段假装已经分析。

## 7. 数据模型

在 schema v5 新增：

- `blueprint_jobs`：job type、输入版本、状态、进度、取消、错误和检查点；
- `blueprint_batches`：长篇批次边界、依赖和状态；
- `blueprint_agent_runs`：代理、模型、提示版本、输入/输出哈希、状态和结果；
- `blueprint_nodes`：绑定 blueprint artifact version 的层级节点；
- `blueprint_evidence`：node/interpretation 到 document chunk/字符范围；
- `blueprint_interpretations`：解释、置信度和作者状态；
- `blueprint_conflicts`：互斥、兼容或待裁决关系；
- `blueprint_edges`：contains、causes、reveals、sets_up、pays_off、changes、mirrors；
- `target_settings`：自然语言输入、结构化字段和版本；
- `blueprint_mappings`：reference node 到 target node 的 preserve/transform/drop/add 映射；
- `draft_candidates`：unit plan、候选文本、基础版本、状态和生成元数据；
- `similarity_assessments`：分层指标、命中范围、风险和门禁结果。

`reference_blueprint`、`target_setting` 和 `target_blueprint` 同时作为 Phase 2 artifact type 存在，正式编辑沿用不可覆盖 artifact version。专用表只保存版本化结构和任务运行状态。

## 8. 长短文本执行策略

### 8.1 短文本

20,000 字符以内使用单 job。仍按段落、场景和节拍拆分，但 API 可以同步等待；超过服务同步超时后自动降级为后台轮询，不丢任务。

### 8.2 长篇

先做本地确定性分段，优先尊重卷章标题。无明确标题时按约 12,000 字符分批，保留最多 800 字符重叠；证据定位使用原文绝对字符范围，重叠内容通过稳定哈希去重。

执行顺序：局部抽取 → 章级综合 → 卷级综合 → 全文综合 → 冲突归并 → 蓝图版本。每个批次提交后保存检查点。服务重启后 pending/running 批次恢复为 resumable，不自动调用模型，直到用户点击恢复。

## 9. 生成上下文防火墙

`DraftContextBuilder` 使用显式 allowlist，只允许：

- 已确认的 target setting version；
- 已确认的 target blueprint version；
- 当前目标 unit 和依赖；
- 当前项目正典、人物、关系、时间线和 K 线；
- 已接受的前序目标文本；
- 目标项目的有效审阅和开放问题。

禁止进入草稿上下文：

- 参考 document 的正文或 chunk text；
- reference evidence 的 quote；
- style fingerprint 中的稀有短语；
- 任何 source locator 附带的文本；
- 抽取代理的原始模型回复。

防火墙在调用模型前扫描 provenance；发现 reference source provenance 时拒绝调用并写入安全事件。相似度代理是唯一可在生成后同时读取 candidate 与 reference fingerprint/text 的代理，但其输出只包含指标、定位和整改建议，不把参考片段传回草稿代理。

## 10. 分层相似度门禁

### 10.1 表达层：硬阻止

默认阻止条件任一成立：

- 归一化最长公共连续片段达到 24 个中文字符或 80 个拉丁字符；
- candidate 窗口与 reference 窗口的字符 5-gram Jaccard ≥ 0.32，且归一化 LCS 比例 ≥ 0.45；
- 命中 style fingerprint 标记的稀有短语或独特专名组合。

阻止结果不能进入待接受状态，只能重新生成或人工重写。阈值保存在项目安全配置中，允许调严，不允许关闭硬阻止。

### 10.2 独特结构层：高风险整改

当 ordered beats 中至少 70% 同时满足角色功能、事件功能和结果的一一对应，且目标蓝图对这些节拍的 transform 比例低于 30%，标记 `high_structural_risk`。候选保持 review_required，不能直接接受；作者必须修改目标蓝图或记录明确例外并再次运行安全审阅。

### 10.3 抽象机制层：允许

母题、三幕/阶段比例、目标—阻碍—结果机制、人物功能类别、一般性情绪曲线相似只展示迁移关系，不计为表达复制。系统不得用一个总分掩盖三层差异。

## 11. 事务、版本与影响

- job、batch 和 agent run 使用幂等键，重复请求不重复创建结果；
- 蓝图发布版本在一个事务内写 artifact version、结构节点、账本和依赖；
- reference blueprint 新版本使 mappings、target blueprint 和其审阅 stale；
- target setting 新版本使 target blueprint 和未接受 candidates stale；
- target blueprint 新版本使基于旧版本的 unit plans、candidates 和审阅 obsolete/stale；
- 接受 candidate 时验证 artifact current version 和 candidate base version；冲突返回 409；
- 拒绝 candidate 不修改正式稿。

## 12. API

```text
POST /v1/projects/{project_id}/blueprint-jobs/reference
GET  /v1/projects/{project_id}/blueprint-jobs/{job_id}
POST /v1/projects/{project_id}/blueprint-jobs/{job_id}/pause
POST /v1/projects/{project_id}/blueprint-jobs/{job_id}/resume
POST /v1/projects/{project_id}/blueprint-jobs/{job_id}/cancel
GET  /v1/projects/{project_id}/reference-blueprints/{artifact_id}
POST /v1/projects/{project_id}/reference-blueprints/{artifact_id}/versions
POST /v1/projects/{project_id}/target-settings
POST /v1/projects/{project_id}/blueprint-jobs/migration
GET  /v1/projects/{project_id}/target-blueprints/{artifact_id}
POST /v1/projects/{project_id}/target-blueprints/{artifact_id}/confirm
POST /v1/projects/{project_id}/draft-candidates
GET  /v1/projects/{project_id}/draft-candidates/{candidate_id}
POST /v1/projects/{project_id}/draft-candidates/{candidate_id}/accept
POST /v1/projects/{project_id}/draft-candidates/{candidate_id}/reject
```

自动运行需要已配置模型。未配置模型时返回明确的 `automation_unavailable`，但用户仍可手工创建、编辑和版本化参考蓝图、结构化新设定与目标蓝图。

## 13. 错误处理

- 模型超时：当前 agent run 标记 retryable_failed；已完成批次保留；
- 非法 JSON：保留原始模型文本到受限诊断字段，run 标记 schema_failed，不发布节点；
- 证据越界：拒绝该结论并记录 evidence_invalid；
- 合并缺输入：合并 batch 保持 blocked 并列出缺失代理；
- 服务重启：running job 变为 resumable；不自动重新产生费用；
- 用户取消：不删除已完成 run，仅阻止新 run 调度；
- 相似度硬阻止：candidate 保留为 blocked 供查看，但不能接受；
- 原文上下文泄漏：模型调用前拒绝，记录 context_firewall_blocked；
- 版本冲突：返回 409，无部分写入。

## 14. 安全与隐私

- 参考文本、蓝图、模型回复和指纹保存在本地 SQLite/项目目录；
- API Key 只保存在进程内存，沿用现有安全规则；
- 日志和报告不写正文、Authorization 或 API Key；
- 外部模型调用前界面展示模型服务与将发送的范围；
- 参考文本视为数据，系统提示明确忽略其中的指令，防止文内 prompt injection；
- agent output 必须通过 schema 和 evidence range 校验后才能进入蓝图；
- 权利基础作为审计元数据，不等同于系统作出法律判断。

## 15. 测试策略

### 单元测试

- 全维度字段三态完整性；
- 层级节点、证据范围、解释冲突和边类型；
- DAG 顺序、并行屏障、幂等、暂停、恢复、取消和失败重跑；
- target setting 结构化和人工覆盖；
- mapping preserve/transform/drop/add；
- DraftContextBuilder provenance allowlist；
- 表达、独特结构和抽象机制三层门禁；
- artifact/version/impact/review stale 传播。

### 契约测试

使用 deterministic fake agents 返回固定类型化结果，证明短文本完整闭环和长篇多批合并。测试 candidate 只包含 target context sentinel，不包含 reference sentinel。

### API 测试

覆盖 UTF-8、JSON 错误、未知对象 404、版本冲突 409、任务状态、暂停恢复、相似度阻止和接受/拒绝。

### 浏览器 E2E

覆盖粘贴短文本 → 蓝图 → 冲突确认 → 新设定 → 目标蓝图 → 单元草稿 → 相似度报告 → 接受；以及长篇后台进度、暂停和恢复。

### 真实模型测试

默认跳过。只有 `CREATIVE_CLAW_REAL_LLM_TEST=1` 时运行一次小型参考文本闭环；测试报告只保存哈希、计数和断言，不保存 API Key 或完整参考文本。

## 16. 首版验收标准

- 短文本和长篇两种入口可用；长篇任务可暂停、恢复和重跑失败批次；
- 蓝图覆盖全部定义维度，任何维度都有 observed/not_observed/uncertain；
- 每个 observed/uncertain 结论有合法证据范围；
- 多代理输出受 DAG 和 schema 约束，失败不会被空结果吞掉；
- 多解释和冲突队列可编辑、可确认、可版本化；
- 自然语言新设定可转为结构化设定并生成 target blueprint；
- 草稿代理的实际请求中没有参考原文、证据 quote 或稀有表达；
- 草稿逐 production unit 生成，用户接受前不修改正式稿；
- 表达复制被硬阻止，独特结构一一对应被要求整改，抽象机制相似可解释地允许；
- 接受产生新 artifact version、账本和影响，拒绝后正式稿不变；
- 无模型时仍可手工维护蓝图；自动测试默认不消费模型费用；
- Phase 1/2 的 38 项 Python、9 项 Node 和浏览器上下文验收无回归。

## 17. 实施顺序

1. schema v5、类型定义和蓝图 repository；
2. 受编排多代理任务内核与 deterministic fake agents；
3. 分层抽取、证据和长篇批次合并；
4. 新设定结构化、mapping 和 target blueprint；
5. 草稿上下文防火墙、candidate 生命周期和相似度门禁；
6. API、蓝图实验室 UI、后台进度与端到端验收；
7. README、续作记录、安全扫描和可选真实模型验证。
