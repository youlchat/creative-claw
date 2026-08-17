# Batch-safe Keys and Staged Synthesis Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve all batch-local blueprint nodes and references while enforcing legal phase/episode hierarchy and real chapter→volume→work→conflict synthesis barriers.

**Architecture:** Normalize raw agent keys into deterministic canonical keys at the orchestrator boundary using explicit `key_scope`, then merge only canonical structures. Keep chapter synthesis batch-local and add resumable job-global volume/work/conflict stage runs with typed, reference-free contexts.

**Tech Stack:** Python 3.12, SQLite, `unittest`, existing BlueprintAgent/BlueprintRepository abstractions.

## Global Constraints

- Do not initialize or use Git.
- Do not call a real model or incur API cost.
- Every production change follows a separately observed RED and GREEN.
- Preserve public API, context firewall, idempotency, pause/cancel, and retry semantics.

---

### Task 1: Canonical batch/global key normalization

**Files:**
- Modify: `tests/test_blueprint_orchestrator.py`
- Modify: `tests/test_blueprint_service.py`
- Modify: `creative_claw/blueprint_agents.py`
- Modify: `creative_claw/blueprint_orchestrator.py`

**Interfaces:**
- Consumes: node payload field `key_scope` with values `batch` or `global`.
- Produces: canonical node keys (`batch:<ordinal>:<raw>` for batch scope, unchanged for global scope) and remapped parents, edges, interpretations, conflict groups, publication keys, and migration mapping inputs.

- [ ] Add an orchestrator integration test whose three batches all return raw `chapter:1`, `scene:1`, and `beat:1`, while `volume:shared` is global. Assert one volume, three chapters/scenes/beats, canonical parents and edge endpoints, canonical interpretation keys, and non-colliding conflict groups.
- [ ] Run only that test and confirm RED shows batch nodes collapsed or references still raw.
- [ ] Add a service integration test that publishes the canonical reference keys and asserts mechanism mapping input covers exactly those canonical keys.
- [ ] Run only that service test and confirm RED shows raw/collapsed keys downstream.
- [ ] Extend the machine-readable node contract with `key_scope`, enforce `work` global, and keep runtime fallback `work→global`, non-work→batch for historical runs.
- [ ] Implement a two-pass canonicalization helper that rejects mixed scope/type declarations and rewrites every related reference before merge.
- [ ] Run both tests and confirm GREEN.
- [ ] Do not commit; Git operations are prohibited for this workspace.

### Task 2: Phase and episode hierarchy support

**Files:**
- Modify: `tests/test_blueprint_orchestrator.py`
- Modify: `creative_claw/blueprint_orchestrator.py`

**Interfaces:**
- Consumes: `NODE_TYPES = {work, volume, phase, chapter, episode, scene, beat}`.
- Produces: global validation and deterministic ordering for both volume/chapter and phase/episode branches.

- [ ] Add one real orchestrator test with `work→phase→episode→scene→beat` and one with short-form `work→episode→scene→beat`.
- [ ] Run the tests and confirm RED reports `hierarchy_invalid` for legal phase/episode output.
- [ ] Expand allowed parent sets and type order so volume/phase share level 1 and chapter/episode share level 2.
- [ ] Run both tests and confirm GREEN, then run existing orphan/cycle/illegal-parent tests.
- [ ] Do not commit; Git operations are prohibited for this workspace.

### Task 3: Typed chapter, volume, work, and conflict barriers

**Files:**
- Modify: `tests/test_blueprint_orchestrator.py`
- Modify: `tests/test_blueprint_agents.py`
- Modify: `creative_claw/blueprint_agents.py`
- Modify: `creative_claw/blueprint_orchestrator.py`

**Interfaces:**
- Consumes: completed batch-local typed results.
- Produces: `synthesis_stage` contexts and independent runs keyed by `prompt-v1:synthesis:<stage>:attempt:<n>`.

- [ ] Add an integration test recording agent task contexts. Assert every batch chapter stage completes before one volume stage, then one work stage, then one conflict stage; assert global contexts contain typed canonical summaries and no text/quote/fingerprint/raw response fields.
- [ ] Run it and confirm RED shows conflict per batch and no volume/work stages.
- [ ] Add tests that `max_batches=1` does not call global stages, pause after volume prevents work/conflict, and a fail-once work stage records `retryable_failed` then resumes without repeating completed volume.
- [ ] Run each test separately and confirm its expected RED.
- [ ] Split the batch DAG from final conflict processing, add a reusable job-global stage runner with completed-only reuse and existing error categories, and check desired state before and after every stage.
- [ ] Build global stage contexts from canonical typed summaries and locator/evidence metadata only; never include source text, quotes, style fingerprints, rare phrases, or raw response.
- [ ] Update deterministic agents to emit scoped raw keys and stage-specific typed hierarchy/conflict outputs.
- [ ] Run all new stage tests and confirm GREEN.
- [ ] Do not commit; Git operations are prohibited for this workspace.

### Task 4: Regression verification and review record

**Files:**
- Modify: `docs/superpowers/reviews/2026-07-29-phase2.5-fix-report.md`

**Interfaces:**
- Consumes: targeted RED/GREEN outputs and focused suite results.
- Produces: auditable review addendum with residual risks.

- [ ] Run targeted canonicalization, phase/episode, staged barrier, pause/cancel, retry, publication, and migration tests.
- [ ] Run focused Python modules `test_blueprint_agents`, `test_blueprint_orchestrator`, `test_blueprint_service`, `test_blueprint_similarity`, `test_blueprint_schema`, and `test_blueprint_api`.
- [ ] Run Node tests, three JS syntax checks, and Python compileall.
- [ ] Append exact RED failures, root causes, changed behavior, GREEN counts, command evidence, and residual risks to the fix report.
- [ ] Do not commit; Git operations are prohibited for this workspace.
