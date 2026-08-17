# Batch-safe keys and staged hierarchy synthesis design

## Goal

Prevent independent batch-local nodes from collapsing when agents reuse raw keys, preserve explicitly shared upper hierarchy nodes, accept every hierarchy shape allowed by `NODE_TYPES`, and enforce the long-form synthesis barriers defined by the Phase 2.5 specification.

## Canonical key model

Agent node payloads carry `key_scope: "batch" | "global"`. The output contract and prompt require it for new calls. `work` must be global. For backward compatibility with already persisted runs and occasional model omission, missing scope is interpreted as global only for `work` and batch for every other node.

Before merge, the orchestrator scans every completed node-producing run. A raw key declared global in one run and batch in another is invalid; a raw key whose node type changes across runs is invalid. Canonical keys are deterministic:

- global: the raw stable key is retained;
- batch: `batch:<ordinal>:<raw-stable-key>`.

The same resolver rewrites node `stable_key` and `parent_key`, edge endpoints, interpretation node keys, and conflict group ids. Exact canonical references remain unchanged. An unscoped raw reference may resolve only when it has one unambiguous canonical target; ambiguity is rejected instead of guessed. Published reference nodes and later mechanism mappings therefore consume canonical keys.

## Legal hierarchy

The global validator accepts the specification's alternatives:

- `work → volume | phase`;
- `volume | phase → chapter | episode`;
- optional upper level: `work → chapter | episode`;
- `chapter | episode → scene`;
- `scene → beat`.

Ordering places volume/phase together and chapter/episode together. Orphans, cycles, mixed scope/type declarations, and illegal parent types block the job.

## Staged synthesis

Batch execution contains specialist extraction followed by `hierarchy_synthesis_agent` with `synthesis_stage="chapter"`. `interpretation_conflict_agent` is not run inside a batch.

Only after every batch is completed does the orchestrator run these job-global stages in order:

1. `hierarchy_synthesis_agent`, `synthesis_stage="volume"`;
2. `hierarchy_synthesis_agent`, `synthesis_stage="work"`;
3. `interpretation_conflict_agent`, `synthesis_stage="conflict"`.

Each stage has an independent prompt/idempotency key and attempt sequence; only completed runs are reused. Every stage checks desired state before and after invocation. A partial `max_batches` run never starts a global stage.

Global-stage context is an allowlisted typed summary: canonical nodes, hierarchy relations, dimension states/confidences, canonical evidence ids and numeric locator metadata. It excludes reference text, evidence quotes, fingerprints, rare phrases, and raw agent responses. Stage failures retain existing `schema_failed` / `retryable_failed` run semantics and do not let a later stage start.

## Compatibility and safety

Public APIs and persisted schemas do not change. Existing completed runs without `key_scope` are safely namespaced as batch-local. Draft context and reference-expression firewalls remain unchanged. No real-model call is required for tests.

## Verification

Tests must prove: three batches returning identical local keys yield three independent chapter/scene/beat sets; explicitly global upper nodes merge once; every related reference is canonical; published nodes and migration mappings use canonical keys; phase/episode trees complete; global stage ordering and typed contexts are exact; pause/cancel and partial runs suppress later stages; retry reuses only completed stage attempts.
