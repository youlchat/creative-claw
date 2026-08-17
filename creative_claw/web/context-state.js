(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.CreativeClawContext = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  function clean(value) {
    if (value === undefined || value === null) return null;
    const text = String(value).trim();
    return text || null;
  }

  function buildContextScope(state) {
    const selected = String(state.selectedNodeId || "");
    const selectedSceneId = selected.startsWith("scene:") ? selected.slice(6) : clean(state.selectedSceneId || state.activeSceneId);
    const event = (state.timeline || []).find((row) => row.id === selectedSceneId);
    return {
      branch: clean(state.branch) || "main",
      episode: event?.episode ?? state.selectedEpisode ?? null,
      scene_id: selectedSceneId,
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

  function indexEvidenceRefs(refs) {
    return Object.fromEntries((refs || []).map((item) => [item.ref, item]));
  }

  return { buildContextScope, summarizeContext, parseCitationTokens, indexEvidenceRefs };
});
