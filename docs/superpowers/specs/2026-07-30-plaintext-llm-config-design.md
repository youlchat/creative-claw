# Plaintext LLM Configuration Persistence Design

## Goal

Persist the user-supplied MiniMax-compatible API configuration in plaintext so Creative Claw can restart without losing the key, while continuing to keep the key out of API responses, browser storage, and logs.

## Decision

Use a database-local JSON file named from the active database: `.creative-claw/demo.llm.json` for `.creative-claw/demo.db`. This is preferred over a schema migration or a machine-wide environment variable because it is project-local, visible, easy to back up, and requires the smallest change. The runtime directory is already excluded by `.gitignore`.

The JSON document contains exactly `api_key`, `base_url`, and `model` in plaintext. A successful `POST /v1/config/llm` validates the same URL/model rules as today, atomically replaces the JSON file, applies the values to the process environment, and registers the real blueprint agents in the already-running app. On startup, Creative Claw loads the JSON before constructing the blueprint registry. An explicitly supplied process environment key takes precedence over the file.

## Data Flow

1. The browser sends the model configuration to the local API.
2. `configure_runtime_model` validates and normalizes the fields.
3. The normalized fields are written to a sibling `*.llm.json.tmp` file and atomically moved to `*.llm.json`.
4. The same values are installed in the process environment.
5. The API registers any missing OpenAI-compatible blueprint agents, so blueprint extraction works without another restart.
6. On later startup, `load_persisted_runtime_model` restores the values before blueprint-agent selection.

## Security and UX Contract

- Plaintext persistence is intentional and explicitly requested by the user.
- Neither `/v1/config` nor `POST /v1/config/llm` returns the key.
- The key is never written to application logs or committed files.
- The model dialog states the exact persistence behavior and file naming rule.
- A missing file leaves the app unconfigured; malformed or incomplete persisted configuration raises a clear startup error instead of silently falling back.

## Verification

- An integration test posts a test key, verifies the plaintext JSON contents, clears the process environment, creates a fresh app against the same database, and verifies that configuration is restored without exposing the key.
- The test also verifies that reference-blueprint creation is no longer blocked by missing agents immediately after configuration.
- The full automated suite, JavaScript syntax checks, local API restart, `/v1/config`, and the real paid blueprint job are verified before completion.

