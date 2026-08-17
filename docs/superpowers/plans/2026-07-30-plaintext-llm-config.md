# Plaintext LLM Configuration Persistence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist the local MiniMax API configuration in plaintext across Creative Claw restarts and make paid blueprint agents available immediately after configuration.

**Architecture:** `creative_claw.llm` owns validation, JSON persistence, startup loading, and public redaction. `creative_claw.api.create_app` derives the configuration path from the database, loads it before building the blueprint registry, and registers runtime agents after a live configuration change.

**Tech Stack:** Python 3.12, Flask, `pathlib`, JSON, standard-library `unittest`, vanilla HTML/JavaScript.

## Global Constraints

- Store `api_key`, `base_url`, and `model` as plaintext in `.creative-claw/demo.llm.json`.
- Never return or log the API key.
- Process environment configuration takes precedence over the persisted file.
- Do not initialize Git; this workspace is not a Git repository.
- Do not call the real model from automated tests.

---

### Task 1: Persist and restore model configuration

**Files:**
- Modify: `creative_claw/llm.py`
- Modify: `creative_claw/api.py`
- Test: `tests/test_llm_config.py`

**Interfaces:**
- Produces: `runtime_model_config_path(database_path: str | Path) -> Path`
- Produces: `load_persisted_runtime_model(path: str | Path) -> dict[str, Any]`
- Extends: `configure_runtime_model(..., persist_path: str | Path | None = None) -> dict[str, Any]`

- [ ] **Step 1: Write a failing integration test**

Create a temporary database, post a test key to `/v1/config/llm`, assert `<database>.llm.json` contains the literal test key, clear the three runtime environment variables, create a new app on the same database, and assert `/v1/config` reports configured without containing the key.

- [ ] **Step 2: Run the focused test and verify RED**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_llm_config -v`

Expected: FAIL because the configuration JSON is not created and a fresh app is unconfigured.

- [ ] **Step 3: Implement minimal persistence and startup loading**

Normalize and validate once, atomically write the three fields when `persist_path` is present, and load the file before `public_model_config()` controls blueprint-registry construction. Keep public responses redacted.

- [ ] **Step 4: Run the focused test and verify GREEN**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_llm_config -v`

Expected: PASS with no network calls.

### Task 2: Activate blueprint agents without another restart

**Files:**
- Modify: `creative_claw/api.py`
- Test: `tests/test_llm_config.py`

**Interfaces:**
- Produces: a local `register_runtime_blueprint_agents()` helper that idempotently calls `BlueprintAgentRegistry.register` for every production agent name.

- [ ] **Step 1: Extend the test and verify RED**

After live configuration, post a public-domain reference job with `run_async: true` against an app created with `run_blueprint_jobs_inline=True`; assert the job is pending rather than blocked with `automation_unavailable`.

- [ ] **Step 2: Implement idempotent live registration**

Call the helper during app construction when configured and after `POST /v1/config/llm` succeeds. Do not alter an explicitly injected test registry.

- [ ] **Step 3: Run the focused test and verify GREEN**

Run: `.\.venv\Scripts\python.exe -m unittest tests.test_llm_config -v`

Expected: PASS and no paid request.

### Task 3: Update user-facing persistence copy and verify the application

**Files:**
- Modify: `creative_claw/web/index.html`
- Modify: `creative_claw/api.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: the persistence behavior implemented by Tasks 1 and 2.
- Produces: accurate UI/API/docs copy that states configuration is stored in plaintext next to the active database.

- [ ] **Step 1: Update the model-dialog paragraph, API success message, and README**

Remove claims that the key is memory-only. State that local plaintext persistence is enabled and public API responses still redact the key.

- [ ] **Step 2: Run complete verification**

Run: `.\.venv\Scripts\python.exe -m unittest discover -s tests -v`

Run: `node --check creative_claw/web/app.js`

Expected: all tests pass and JavaScript syntax exits 0.

- [ ] **Step 3: Restart and perform the authorized real workflow**

Restart port 8766, post the provided MiniMax configuration through the visible model dialog, verify `/v1/config` reports `configured: true`, restore the 4,801-character `范进中举` reference text, start background extraction, and poll the job until completion or a concrete provider error.
