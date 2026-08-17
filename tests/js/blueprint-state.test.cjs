const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const {
  calculateProgress,
  hasCompleteDimensions,
  filterConflicts,
  groupRisks,
  canAcceptCandidate,
  buildDraftRequest,
  buildInterpretationDecisions,
  normalizeStructuredSetting,
  canMigrateSetting,
  BLUEPRINT_DIMENSIONS,
} = require("../../creative_claw/web/blueprint-state.js");

test("calculates persisted job progress", () => {
  assert.deepEqual(calculateProgress({ status: "running", progress: { completed_batches: 2, total_batches: 5 } }), {
    completed: 2,
    total: 5,
    percent: 40,
    status: "running",
  });
  assert.equal(calculateProgress({ status: "completed", progress: {} }).percent, 100);
});

test("requires all typed dimensions and valid tri-state values", () => {
  const dimensions = Object.fromEntries(BLUEPRINT_DIMENSIONS.map((name) => [name, { state: "not_observed" }]));
  assert.equal(hasCompleteDimensions({ dimensions }), true);
  delete dimensions.causality;
  assert.equal(hasCompleteDimensions({ dimensions }), false);
  dimensions.causality = { state: "invented" };
  assert.equal(hasCompleteDimensions({ dimensions }), false);
});

test("filters conflict queue and groups layered candidate risks", () => {
  const conflicts = [
    { id: "a", status: "pending_author" },
    { id: "b", status: "resolved" },
    { id: "c", status: "pending_author" },
  ];
  assert.deepEqual(filterConflicts(conflicts, "pending_author").map((item) => item.id), ["a", "c"]);
  const groups = groupRisks([
    { id: "x", status: "blocked" },
    { id: "y", status: "review_required" },
    { id: "z", status: "passed" },
  ]);
  assert.equal(groups.blocked.length, 1);
  assert.equal(groups.review_required.length, 1);
  assert.equal(groups.passed.length, 1);
});

test("candidate acceptance follows similarity gate", () => {
  assert.equal(canAcceptCandidate({ status: "passed", similarity: { gate_status: "passed" } }), true);
  assert.equal(canAcceptCandidate({ status: "blocked", similarity: { gate_status: "blocked" } }), false);
  assert.equal(canAcceptCandidate({ status: "review_required", similarity: { gate_status: "review_required" } }), false);
});

test("draft request allowlist excludes reference fields", () => {
  assert.deepEqual(buildDraftRequest({
    target_blueprint_id: "target-1",
    unit_id: "unit-1",
    artifact_id: "artifact-1",
    reference_text: "must-not-leak",
    quote: "must-not-leak",
    rare_phrases: ["must-not-leak"],
  }), {
    target_blueprint_id: "target-1",
    unit_id: "unit-1",
    artifact_id: "artifact-1",
  });
});

test("author decisions and editable structured setting require explicit confirmation", () => {
  assert.deepEqual(buildInterpretationDecisions([
    { id: "i1", decision: "confirmed" },
    { id: "i2", decision: "rejected" },
    { id: "i3", decision: "pending" },
  ]), { i1: "confirmed", i2: "rejected", i3: "pending" });
  const structured = normalizeStructuredSetting({
    genre: "fantasy", audience: "adult", media_type: "novel", scale: "long",
    world_rules: ["rule"], characters: [{ name: "A" }], character_goals: ["goal"],
    core_conflict: "conflict", stakes: "stakes", themes: ["theme"],
    narrative_preferences: { pov: "first" }, must_include: [], must_avoid: ["avoid"],
    ending_direction: "ending", ignored_reference_text: "must-not-survive",
  });
  assert.equal(structured.genre, "fantasy");
  assert.equal(Object.hasOwn(structured, "ignored_reference_text"), false);
  assert.equal(canMigrateSetting({ artifact: { attrs: { confirmation_status: "proposed" } } }), false);
  assert.equal(canMigrateSetting({ artifact: { attrs: { confirmation_status: "confirmed" } } }), true);
});

test("blueprint lab exposes stable controls and loads state before app", () => {
  const html = fs.readFileSync(path.join(__dirname, "../../creative_claw/web/index.html"), "utf8");
  const js = fs.readFileSync(path.join(__dirname, "../../creative_claw/web/app.js"), "utf8");
  const css = fs.readFileSync(path.join(__dirname, "../../creative_claw/web/app.css"), "utf8");
  const ids = [
    "blueprintLabButton", "referenceTextInput", "startReferenceBlueprint",
    "blueprintJobProgress", "referenceBlueprintTree", "blueprintConflictQueue",
    "targetSettingInput", "createTargetBlueprint", "targetBlueprintTree",
    "targetSettingFields", "confirmTargetSetting", "migrateTargetBlueprint",
    "generateUnitDraft", "similarityReport", "acceptDraftCandidate", "rejectDraftCandidate",
  ];
  ids.forEach((id) => assert.ok(html.includes(`id="${id}"`), id));
  assert.ok(html.indexOf("blueprint-state.js") < html.indexOf("app.js"));
  assert.ok(js.includes("startReferenceBlueprint"));
  assert.ok(js.includes("createTargetBlueprint"));
  assert.ok(js.includes("generateUnitDraft"));
  assert.ok(css.includes("#blueprintLabDialog"));
  assert.equal(js.includes("localStorage.setItem(\"reference"), false);
  assert.equal(js.includes("localStorage.setItem(\"api"), false);
});
