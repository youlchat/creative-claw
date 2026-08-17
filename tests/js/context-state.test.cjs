const test = require("node:test");
const assert = require("node:assert/strict");
const {
  buildContextScope,
  summarizeContext,
  parseCitationTokens,
  indexEvidenceRefs,
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

test("indexes typed evidence by stable ref", () => {
  const index = indexEvidenceRefs([{ ref: "T1", kind: "timeline" }, { ref: "K1", kind: "kline" }]);
  assert.equal(index.T1.kind, "timeline");
  assert.equal(index.K1.kind, "kline");
});

const fs = require("node:fs");
const path = require("node:path");

test("loads context module and exposes preview workflow controls", () => {
  const html = fs.readFileSync(path.join(__dirname, "../../creative_claw/web/index.html"), "utf8");
  assert.ok(html.includes('src="/assets/context-state.js"'));
  assert.ok(html.indexOf('context-state.js') < html.indexOf('app.js'));
  assert.ok(html.includes('id="previewContext"'));
  assert.ok(html.includes('id="contextDialog"'));
  assert.ok(html.includes('id="runChatFromPreview"'));
  assert.ok(html.includes("确认上下文 → 运行模型 → 审阅候选 → 接受或拒绝"));
});

test("app requests derive scope and contain no demo-character defaults", () => {
  const js = fs.readFileSync(path.join(__dirname, "../../creative_claw/web/app.js"), "utf8");
  assert.equal(js.includes('character_name: "沈霜"'), false);
  assert.equal(js.includes('dimension: "知情度"'), false);
  assert.ok(js.includes("currentContextScope()"));
  assert.ok(js.includes("loadContextPreview"));
  assert.ok(js.includes("runChatRequest"));
  assert.ok(js.includes("citation_validation"));
});

test("preview controls are bound and styled", () => {
  const js = fs.readFileSync(path.join(__dirname, "../../creative_claw/web/app.js"), "utf8");
  const css = fs.readFileSync(path.join(__dirname, "../../creative_claw/web/app.css"), "utf8");
  assert.ok(js.includes('$("#previewContext").addEventListener("click", prepareChatRequest)'));
  assert.ok(js.includes('$("#runChatFromPreview").addEventListener("click", runChatFromPreview)'));
  assert.ok(css.includes("#contextDialog"));
  assert.ok(css.includes(".citation-warning"));
  assert.ok(css.includes('.evidence-item[data-kind="timeline"]'));
});

test("kline rendering follows selected character and dimension", () => {
  const js = fs.readFileSync(path.join(__dirname, "../../creative_claw/web/app.js"), "utf8");
  assert.equal(js.includes('row.character_name === "沈霜"'), false);
  assert.equal(js.includes('row.dimension === "知情度"'), false);
  assert.ok(js.includes("state.selectedCharacterName"));
  assert.ok(js.includes("state.selectedDimension"));
});
