(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.CreativeClawBlueprint = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const BLUEPRINT_DIMENSIONS = [
    "narrative_function", "characters", "relationships", "goals", "obstacles",
    "stakes", "events", "causality", "conflict", "turns", "reveals", "suspense",
    "setup_payoff", "pov", "story_time", "discourse_time", "location",
    "emotion_kline", "pacing", "themes", "motifs", "imagery", "style_statistics",
  ];
  const STATES = new Set(["observed", "not_observed", "uncertain"]);
  const TARGET_SETTING_FIELDS = [
    "genre", "audience", "media_type", "scale", "world_rules", "characters",
    "character_goals", "core_conflict", "stakes", "themes", "narrative_preferences",
    "must_include", "must_avoid", "ending_direction",
  ];

  function calculateProgress(job) {
    const progress = job?.progress || {};
    const total = Number(progress.total_batches || progress.total_agents || 0);
    const completed = Number(progress.completed_batches || progress.completed_agents || 0);
    const percent = job?.status === "completed" ? 100 : (total > 0 ? Math.round((completed / total) * 100) : 0);
    return { completed, total, percent, status: String(job?.status || "pending") };
  }

  function hasCompleteDimensions(node) {
    const dimensions = node?.dimensions;
    return Boolean(dimensions) && BLUEPRINT_DIMENSIONS.every((name) => STATES.has(dimensions[name]?.state));
  }

  function filterConflicts(conflicts, status = "pending_author") {
    return (conflicts || []).filter((item) => !status || item.status === status);
  }

  function groupRisks(candidates) {
    const result = { blocked: [], review_required: [], passed: [], other: [] };
    (candidates || []).forEach((item) => {
      const bucket = Object.prototype.hasOwnProperty.call(result, item.status) ? item.status : "other";
      result[bucket].push(item);
    });
    return result;
  }

  function canAcceptCandidate(candidate) {
    return candidate?.status === "passed" && candidate?.similarity?.gate_status === "passed";
  }

  function buildDraftRequest(value) {
    return {
      target_blueprint_id: String(value?.target_blueprint_id || ""),
      unit_id: String(value?.unit_id || ""),
      artifact_id: String(value?.artifact_id || ""),
    };
  }

  function buildInterpretationDecisions(items) {
    return Object.fromEntries((items || [])
      .filter((item) => item?.id && ["pending", "confirmed", "rejected"].includes(item?.decision))
      .map((item) => [String(item.id), String(item.decision)]));
  }

  function normalizeStructuredSetting(value) {
    const source = value && typeof value === "object" ? value : {};
    const missing = TARGET_SETTING_FIELDS.filter((field) => !Object.hasOwn(source, field));
    if (missing.length) throw new Error(`Missing target setting fields: ${missing.join(", ")}`);
    return Object.fromEntries(TARGET_SETTING_FIELDS.map((field) => [field, source[field]]));
  }

  function canMigrateSetting(setting) {
    return setting?.artifact?.attrs?.confirmation_status === "confirmed";
  }

  return {
    BLUEPRINT_DIMENSIONS,
    calculateProgress,
    hasCompleteDimensions,
    filterConflicts,
    groupRisks,
    canAcceptCandidate,
    buildDraftRequest,
    buildInterpretationDecisions,
    normalizeStructuredSetting,
    canMigrateSetting,
  };
});
