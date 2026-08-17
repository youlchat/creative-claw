const test = require("node:test");
const assert = require("node:assert/strict");
const {
  isProjectEmpty,
  modeView,
  summarizePreview,
} = require("../../creative_claw/web/cold-start-state.js");

const emptySnapshot = {
  documents: [],
  entities: [],
  relations: [],
  timeline: [],
  ohlc: [],
  production_units: [],
  artifacts: [],
};

test("cold-start mode exposes one-sentence generation copy only for an empty project", () => {
  assert.equal(isProjectEmpty(emptySnapshot), true);
  assert.deepEqual(modeView("cold_start", emptySnapshot), {
    coldStart: true,
    empty: true,
    placeholder: "例如：帮我写一个类阿凡提的幽默故事",
    primaryLabel: "生成框架预览",
    status: "输入一句创作意图，先预览再采用",
  });
});

test("any structured project content blocks cold start", () => {
  for (const key of [
    "documents",
    "entities",
    "relations",
    "timeline",
    "ohlc",
    "production_units",
    "artifacts",
  ]) {
    assert.equal(
      isProjectEmpty({ ...emptySnapshot, [key]: [{ id: key }] }),
      false,
      key,
    );
  }
  assert.equal(
    modeView("cold_start", {
      ...emptySnapshot,
      timeline: [{ id: "scene" }],
    }).status,
    "冷启动仅适用于空项目，请先新建项目",
  );
});

test("ordinary modes preserve the existing assistant controls", () => {
  assert.deepEqual(modeView("analysis", emptySnapshot), {
    coldStart: false,
    empty: true,
    placeholder: "例如：当前人物在这个场景知道了什么？",
    primaryLabel: "运行模型",
    status: null,
  });
});

test("preview summary resolves protagonist without recomputing backend validation", () => {
  assert.deepEqual(
    summarizePreview({
      protagonist_key: "hero",
      kline_dimension: "解局主动权",
      entities: [
        { key: "hero", name: "艾山" },
        { key: "collector", name: "罗班" },
      ],
      relations: [{ source_key: "hero", target_key: "collector" }],
      scenes: [{ title: "一" }, { title: "二" }],
    }),
    {
      entityCount: 2,
      relationCount: 1,
      sceneCount: 2,
      protagonistName: "艾山",
      dimension: "解局主动权",
    },
  );
});
