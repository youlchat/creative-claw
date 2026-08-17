(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.CreativeClawColdStart = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const CONTENT_KEYS = [
    "documents",
    "entities",
    "relations",
    "timeline",
    "ohlc",
    "production_units",
    "artifacts",
  ];

  function isProjectEmpty(snapshot) {
    const source = snapshot && typeof snapshot === "object" ? snapshot : {};
    return CONTENT_KEYS.every((key) => Array.isArray(source[key]) && source[key].length === 0);
  }

  function modeView(mode, snapshot) {
    const empty = isProjectEmpty(snapshot);
    if (mode === "cold_start") {
      return {
        coldStart: true,
        empty,
        placeholder: "例如：帮我写一个类阿凡提的幽默故事",
        primaryLabel: "生成框架预览",
        status: empty
          ? "输入一句创作意图，先预览再采用"
          : "冷启动仅适用于空项目，请先新建项目",
      };
    }
    return {
      coldStart: false,
      empty,
      placeholder: "例如：当前人物在这个场景知道了什么？",
      primaryLabel: "运行模型",
      status: null,
    };
  }

  function summarizePreview(preview) {
    const source = preview && typeof preview === "object" ? preview : {};
    const entities = Array.isArray(source.entities) ? source.entities : [];
    const relations = Array.isArray(source.relations) ? source.relations : [];
    const scenes = Array.isArray(source.scenes) ? source.scenes : [];
    const protagonist = entities.find((item) => item.key === source.protagonist_key);
    return {
      entityCount: entities.length,
      relationCount: relations.length,
      sceneCount: scenes.length,
      protagonistName: protagonist?.name || "未命名主人公",
      dimension: String(source.kline_dimension || "未命名维度"),
    };
  }

  return { isProjectEmpty, modeView, summarizePreview };
});
