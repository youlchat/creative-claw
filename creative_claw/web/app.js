(() => {
  "use strict";

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
  const clamp = (value, min, max) => Math.max(min, Math.min(max, value));
  const escapeText = (value) => String(value ?? "");
  const CANVAS_WIDTH = 1600;
  const CANVAS_HEIGHT = 1100;

  const state = {
    projectId: "demo",
    branch: "main",
    snapshot: null,
    ledger: { verification: null, events: [] },
    config: null,
    nodes: [],
    positions: {},
    zoom: 0.82,
    pan: { x: 20, y: 20 },
    canvasMode: "select",
    drag: null,
    panDrag: null,
    selectedNodeId: null,
    activeSceneId: null,
    manuscriptDrafts: {},
    manuscriptPatches: {},
    textSelection: null,
    activeSelectionPatch: null,
    pendingManuscriptSceneId: null,
    pendingManuscriptTaskId: null,
    selectedOhlc: null,
    selectedCharacterName: null,
    selectedDimension: null,
    lastContextPreview: null,
    candleDraft: null,
    candleDrag: null,
    evidence: [],
    activeTask: null,
    tasks: [],
    sceneDraftCitations: [],
    lastAssistantDraft: null,
    coldStart: { preview: null, generation: null, prompt: "", busy: false },
    blueprint: {
      job: null,
      reference: null,
      setting: null,
      migration: null,
      target: null,
      candidate: null,
      pollTimer: null,
    },
    toastTimer: null,
  };

  async function api(path, options = {}) {
    let response;
    try {
      response = await fetch(path, options);
    } catch (cause) {
      const error = new Error("本地服务已断开，请重新启动 Creative Claw");
      error.code = "service_unreachable";
      error.cause = cause;
      throw error;
    }
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      const message = payload.detail ? `${payload.error || "请求失败"}：${payload.detail}` : (payload.error || `HTTP ${response.status}`);
      const error = new Error(message);
      error.status = response.status;
      error.payload = payload;
      throw error;
    }
    return payload;
  }

  function jsonOptions(value) {
    return { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(value) };
  }

  function toast(message) {
    const node = $("#toast");
    node.textContent = message;
    node.classList.add("show");
    clearTimeout(state.toastTimer);
    state.toastTimer = setTimeout(() => node.classList.remove("show"), 2600);
  }

  function formatNumber(value) {
    return new Intl.NumberFormat("zh-CN", { notation: value > 9999 ? "compact" : "standard", maximumFractionDigits: 1 }).format(value || 0);
  }

  function fileLabel(kind) {
    const names = { docx: "DOC", pptx: "PPT", xlsx: "XLS", md: "MD", pdf: "PDF", txt: "TXT", csv: "CSV" };
    return names[String(kind).toLowerCase()] || String(kind).slice(0, 4).toUpperCase();
  }

  function displayTitle(value) {
    return String(value || "").replace(/^upload_[a-f0-9]+-/i, "");
  }

  function locatorFor(item) {
    if (item.episode != null) return `E${item.episode}${item.scene != null ? `-S${String(item.scene).padStart(2, "0")}` : ""}`;
    return item.entity_type || item.kind || "项目";
  }

  const EVIDENCE_LABELS = {
    source: "来源", graph: "图谱", timeline: "时间线", kline: "K 线",
    version: "版本", rule: "规则", issue: "问题",
  };

  function currentContextScope() {
    return CreativeClawContext.buildContextScope({
      ...state,
      timeline: state.snapshot?.timeline || [],
    });
  }

  function sourceCitations(evidenceRefs) {
    return (evidenceRefs || []).filter((item) => item.kind === "source").map((item) => item.payload || item);
  }

  function focusEvidence(item, switchPanel = true) {
    if (!item) return;
    const locator = item.locator || {};
    if (item.kind === "source" && locator.document_id) {
      selectNode(`source:${locator.document_id}`, switchPanel);
      return;
    }
    if (item.kind === "graph" && locator.entity_id) {
      selectNode(`entity:${locator.entity_id}`, switchPanel);
      return;
    }
    const sceneId = locator.event_id || locator.timeline_event_id;
    if (sceneId) {
      selectNode(`scene:${sceneId}`, switchPanel);
      if (item.kind === "kline") {
        const row = (state.snapshot?.ohlc || []).find((candidate) => candidate.id === locator.ohlc_id);
        if (row) selectCandle(row);
      }
      return;
    }
    state.evidence = [item];
    renderEvidence();
    if (switchPanel) switchTab("inspector");
  }

  async function initialize() {
    bindControls();
    try {
      const [config, projectsResult] = await Promise.all([api("/v1/config"), api("/v1/projects")]);
      state.config = config;
      renderModelStatus();
      const projects = projectsResult.projects || [];
      const select = $("#projectSelect");
      select.replaceChildren(...projects.map((project) => {
        const option = document.createElement("option");
        option.value = project.id;
        option.textContent = project.name;
        return option;
      }));
      if (projects.length) {
        const remembered = localStorage.getItem("creative-claw:active-project");
        state.projectId = projects.some((project) => project.id === remembered)
          ? remembered
          : (projects.some((project) => project.id === "demo") ? "demo" : projects[0].id);
        select.value = state.projectId;
        select.disabled = false;
        $("#renameProjectButton").disabled = false;
        await loadSnapshot(true);
      } else {
        select.disabled = true;
        $("#renameProjectButton").disabled = true;
        $("#newProjectDialog").showModal();
        toast("创建第一个写作项目后即可开始");
      }
    } catch (error) {
      renderConnectionError(error);
    }
  }

  function renderModelStatus() {
    const status = $("#modelStatus");
    const llm = state.config?.llm || {};
    status.classList.toggle("configured", Boolean(llm.configured));
    status.classList.toggle("unconfigured", !llm.configured);
    status.classList.remove("offline");
    $("span", status).textContent = llm.configured ? `${llm.model} 已连接` : `${llm.model || "MiniMax-M3"} 待配置`;
    $("#assistantModel").textContent = llm.model || "MiniMax-M3";
  }

  async function renameProject() {
    const name = $("#projectName").value.trim();
    if (!name) return;
    try {
      const project = await api(`/v1/projects/${encodeURIComponent(state.projectId)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      });
      state.snapshot.project = project;
      const option = $(`#projectSelect option[value="${CSS.escape(state.projectId)}"]`);
      if (option) option.textContent = project.name;
      $("#projectDialog").close();
      toast(`项目已命名为：${project.name}`);
    } catch (error) { toast(error.message); }
  }

  function resetProjectState(projectId) {
    state.projectId = projectId;
    localStorage.setItem("creative-claw:active-project", projectId);
    state.selectedNodeId = null;
    state.activeSceneId = null;
    state.selectedOhlc = null;
    state.selectedCharacterName = null;
    state.selectedDimension = null;
    state.lastContextPreview = null;
    state.candleDraft = null;
    state.manuscriptDrafts = {};
    state.manuscriptPatches = {};
    state.evidence = [];
    state.activeTask = null;
    state.tasks = [];
    state.lastAssistantDraft = null;
    state.coldStart = { preview: null, generation: null, prompt: "", busy: false };
    if (state.blueprint.pollTimer) clearTimeout(state.blueprint.pollTimer);
    state.blueprint = { job: null, reference: null, setting: null, migration: null, target: null, candidate: null, pollTimer: null };
    closeSelectionTools();
    renderEvidence();
    renderTasks();
    $("#inspectorEmpty").hidden = false;
    $("#nodeInspector").hidden = true;
    $("#selectionHint").textContent = "未选择节点";
  }

  function openNewProjectDialog() {
    $("#newProjectName").value = "";
    $("#newProjectError").hidden = true;
    $("#newProjectDialog").showModal();
  }

  async function createProject() {
    const name = $("#newProjectName").value.trim();
    if (!name) return;
    const submit = $("#createProjectButton");
    const errorNode = $("#newProjectError");
    submit.disabled = true;
    errorNode.hidden = true;
    try {
      const project = await api("/v1/projects", jsonOptions({ name }));
      const option = document.createElement("option");
      option.value = project.id;
      option.textContent = project.name;
      $("#projectSelect").append(option);
      $("#projectSelect").disabled = false;
      $("#renameProjectButton").disabled = false;
      $("#projectSelect").value = project.id;
      resetProjectState(project.id);
      $("#newProjectDialog").close();
      await loadSnapshot(true);
      toast(`已创建项目：${project.name}`);
    } catch (error) {
      errorNode.textContent = error.message;
      errorNode.hidden = false;
    } finally {
      submit.disabled = false;
    }
  }

  function renderConnectionError(error) {
    const status = $("#modelStatus");
    status.classList.remove("configured", "unconfigured");
    status.classList.add("offline");
    $("span", status).textContent = "服务已断开";
    $("#canvasMeta").textContent = "请重新启动本地服务并刷新页面";
    $("#chatStatus").textContent = error.message;
    toast(error.message);
  }

  function openModelDialog() {
    const llm = state.config?.llm || {};
    $("#modelBaseUrl").value = llm.base_url || "https://api.minimaxi.com/v1";
    $("#modelName").value = llm.model || "MiniMax-M3";
    $("#modelApiKey").value = "";
    $("#modelConfigError").hidden = true;
    $("#modelDialog").showModal();
  }

  async function saveModelConfig() {
    const submit = $("#saveModelConfig");
    const errorNode = $("#modelConfigError");
    submit.disabled = true;
    errorNode.hidden = true;
    try {
      const result = await api("/v1/config/llm", jsonOptions({
        base_url: $("#modelBaseUrl").value,
        model: $("#modelName").value,
        api_key: $("#modelApiKey").value,
      }));
      state.config = { ...(state.config || {}), llm: result.llm };
      $("#modelApiKey").value = "";
      $("#modelDialog").close();
      renderModelStatus();
      $("#chatStatus").textContent = "模型已连接，可以运行";
      toast(result.message || "MiniMax 已连接；配置已明文保存");
    } catch (error) {
      errorNode.textContent = error.message;
      errorNode.hidden = false;
    } finally {
      submit.disabled = false;
    }
  }

  async function loadSnapshot(fit = false) {
    const project = encodeURIComponent(state.projectId);
    const [snapshot, ledger] = await Promise.all([
      api(`/v1/projects/${project}/canvas?branch=${encodeURIComponent(state.branch)}`),
      api(`/v1/projects/${project}/ledger/events?limit=50`),
    ]);
    state.snapshot = snapshot;
    state.ledger = ledger;
    if (!snapshot.timeline.some((event) => event.id === state.activeSceneId)) {
      state.activeSceneId = snapshot.timeline.at(-1)?.id || null;
    }
    state.selectedOhlc = null;
    state.candleDraft = null;
    state.positions = loadPositions();
    buildNodes();
    renderSidebar();
    renderCanvas();
    renderManuscript();
    renderKline();
    renderLedgerBoard();
    syncChatMode();
    if (fit) requestAnimationFrame(fitCanvas);
  }

  function renderSidebar() {
    const { documents = [], entities = [], stats = {} } = state.snapshot || {};
    $("#documentCount").textContent = documents.length;
    $("#chunkCount").textContent = formatNumber(stats.chunks);
    $("#entityCount").textContent = formatNumber(entities.length);
    $("#ledgerCount").textContent = formatNumber(stats.ledger?.event_count);
    $("#entitySummary").textContent = `${entities.length} 项`;
    $("#canvasMeta").textContent = `${state.snapshot.timeline.length} 个场景 · ${entities.length} 个实体 · ${documents.length} 个来源`;

    const documentList = $("#documentList");
    documentList.replaceChildren(...documents.slice(0, 14).map((source) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "document-item";
      button.dataset.documentId = source.id;
      const icon = document.createElement("span");
      icon.className = "file-icon";
      icon.textContent = fileLabel(source.kind);
      const copy = document.createElement("span");
      copy.className = "file-copy";
      const title = document.createElement("strong");
      title.textContent = displayTitle(source.title);
      const detail = document.createElement("small");
      detail.textContent = `${source.chunk_count} 个引用块 · v${source.version}`;
      copy.append(title, detail);
      const count = document.createElement("small");
      count.textContent = source.kind.toUpperCase();
      button.append(icon, copy, count);
      button.addEventListener("click", () => selectNode(`source:${source.id}`, true));
      return button;
    }));

    const entityList = $("#entityList");
    entityList.replaceChildren(...entities.map((entity) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "entity-chip";
      button.textContent = entity.name;
      button.addEventListener("click", () => selectNode(`entity:${entity.id}`, true));
      return button;
    }));
  }

  function buildNodes() {
    const snapshot = state.snapshot;
    const nodes = [];
    snapshot.entities.forEach((entity, index) => nodes.push({
      id: `entity:${entity.id}`,
      rawId: entity.id,
      type: "character",
      typeLabel: entity.entity_type === "character" ? "人物" : "实体",
      title: entity.name,
      description: entity.attrs?.description || (entity.aliases?.length ? `别名：${entity.aliases.join("、")}` : `类型：${entity.entity_type}`),
      locator: entity.entity_type,
      raw: entity,
      fallback: { x: 35, y: 370 + index * 132 },
    }));
    snapshot.timeline.slice(0, 8).forEach((event, index) => nodes.push({
      id: `scene:${event.id}`,
      rawId: event.id,
      type: "scene",
      typeLabel: "场景",
      title: event.label,
      description: event.description,
      locator: locatorFor(event),
      raw: event,
      fallback: { x: 35, y: 40 + index * 140 },
    }));
    snapshot.documents.slice(0, 6).forEach((document, index) => nodes.push({
      id: `source:${document.id}`,
      rawId: document.id,
      type: "source",
      typeLabel: "来源",
      title: displayTitle(document.title),
      description: `${document.kind.toUpperCase()} · ${document.chunk_count} 个可引用片段`,
      locator: document.kind.toUpperCase(),
      raw: document,
      fallback: { x: 280 + (index % 3) * 230, y: 750 + Math.floor(index / 3) * 125 },
    }));
    state.nodes = nodes;
    nodes.forEach((node) => {
      if (!state.positions[node.id]) state.positions[node.id] = node.fallback;
    });
    if (!state.positions["board:manuscript"]) state.positions["board:manuscript"] = { x: 280, y: 40 };
    if (!state.positions["board:kline"]) state.positions["board:kline"] = { x: 960, y: 40 };
    if (!state.positions["board:ledger"]) state.positions["board:ledger"] = { x: 960, y: 495 };
  }

  function positionKey() {
    return `creative-claw:canvas:v3:${state.projectId}:${state.branch}`;
  }

  function loadPositions() {
    try { return JSON.parse(localStorage.getItem(positionKey()) || "{}"); } catch { return {}; }
  }

  function savePositions() {
    localStorage.setItem(positionKey(), JSON.stringify(state.positions));
  }

  function renderCanvas() {
    const layer = $("#nodeLayer");
    layer.replaceChildren(...state.nodes.map(createNodeElement));
    applyBoardPositions();
    applyTransform();
    requestAnimationFrame(renderEdges);
  }

  function applyBoardPositions() {
    $$(".canvas-board").forEach((board) => {
      const position = state.positions[board.dataset.boardId];
      if (!position) return;
      board.style.left = `${position.x}px`;
      board.style.top = `${position.y}px`;
    });
  }

  function createNodeElement(node) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `canvas-node ${node.type}${state.selectedNodeId === node.id ? " selected" : ""}`;
    button.dataset.nodeId = node.id;
    const position = state.positions[node.id];
    button.style.left = `${position.x}px`;
    button.style.top = `${position.y}px`;

    const top = document.createElement("div");
    top.className = "node-top";
    const type = document.createElement("span");
    type.className = "node-type";
    type.textContent = node.typeLabel;
    const locator = document.createElement("span");
    locator.className = "node-locator";
    locator.textContent = node.locator;
    top.append(type, locator);
    const title = document.createElement("h3");
    title.textContent = node.title;
    const description = document.createElement("p");
    description.textContent = node.description;
    const footer = document.createElement("div");
    footer.className = "node-footer";
    const left = document.createElement("span");
    left.textContent = node.type === "source" ? `v${node.raw.version}` : node.type === "scene" ? `${String(node.raw.description || "").length} 字符正文` : "已写入账本";
    const value = document.createElement("span");
    value.className = "node-mini-value";
    value.textContent = node.type === "scene" ? (ohlcCloseForScene(node.raw) ?? "未关联 K 线") : "打开";
    footer.append(left, value);
    button.append(top, title, description, footer);
    button.addEventListener("pointerdown", startNodeDrag);
    button.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") selectNode(node.id, true);
    });
    return button;
  }

  function ohlcCloseForScene(scene) {
    if (!scene) return null;
    const row = (state.snapshot?.ohlc || []).find((item) => item.timeline_event_id === scene.id);
    return row ? `C ${row.close}` : null;
  }

  function renderEdges() {
    const svg = $("#edgeLayer");
    svg.replaceChildren();
    const nodeMap = new Map(state.nodes.map((node) => [node.id, node]));
    (state.snapshot?.relations || []).forEach((relation) => {
      const sourceId = `entity:${relation.source_id}`;
      const targetId = `entity:${relation.target_id}`;
      if (!nodeMap.has(sourceId) || !nodeMap.has(targetId)) return;
      const source = state.positions[sourceId];
      const target = state.positions[targetId];
      const x1 = source.x + 210;
      const y1 = source.y + 52;
      const x2 = target.x;
      const y2 = target.y + 52;
      const bend = Math.max(60, Math.abs(x2 - x1) * .45);
      const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("class", "edge-path");
      path.setAttribute("d", `M ${x1} ${y1} C ${x1 + bend} ${y1}, ${x2 - bend} ${y2}, ${x2} ${y2}`);
      const label = document.createElementNS("http://www.w3.org/2000/svg", "text");
      label.setAttribute("class", "edge-label");
      label.setAttribute("x", String((x1 + x2) / 2));
      label.setAttribute("y", String((y1 + y2) / 2 - 6));
      label.setAttribute("text-anchor", "middle");
      label.textContent = relation.predicate;
      svg.append(path, label);
    });
    const activeSceneNode = nodeMap.get(`scene:${state.activeSceneId}`);
    const linkedRows = (state.snapshot?.ohlc || []).filter((row) => row.timeline_event_id === state.activeSceneId);
    const klinePosition = state.positions["board:kline"];
    if (activeSceneNode && linkedRows.length && klinePosition) {
      const source = state.positions[activeSceneNode.id];
      const x1 = source.x + 210;
      const y1 = source.y + 52;
      const x2 = klinePosition.x;
      const y2 = klinePosition.y + 88;
      const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("class", "edge-path kline-link");
      path.setAttribute("d", `M ${x1} ${y1} C ${x1 + 100} ${y1}, ${x2 - 100} ${y2}, ${x2} ${y2}`);
      const label = document.createElementNS("http://www.w3.org/2000/svg", "text");
      label.setAttribute("class", "edge-label");
      label.setAttribute("x", String((x1 + x2) / 2));
      label.setAttribute("y", String((y1 + y2) / 2 - 7));
      label.setAttribute("text-anchor", "middle");
      label.textContent = `${linkedRows.length} 条场景 K 线`;
      svg.append(path, label);
    }
  }

  function applyTransform() {
    $("#canvasWorld").style.transform = `translate(${state.pan.x}px, ${state.pan.y}px) scale(${state.zoom})`;
    $("#zoomLabel").textContent = `${Math.round(state.zoom * 100)}%`;
  }

  function fitCanvas() {
    const viewport = $("#canvasViewport");
    const width = viewport.clientWidth;
    const height = viewport.clientHeight;
    const canvasWidth = 1430;
    const canvasHeight = 1080;
    const scale = clamp(Math.min((width - 26) / canvasWidth, (height - 30) / canvasHeight), .42, 1);
    state.zoom = scale;
    state.pan = { x: Math.max(12, (width - canvasWidth * scale) / 2), y: Math.max(12, (height - canvasHeight * scale) / 2) };
    applyTransform();
  }

  function startNodeDrag(event) {
    if (state.canvasMode !== "select" || event.button !== 0) return;
    const id = event.currentTarget.dataset.nodeId;
    const position = state.positions[id];
    state.drag = { id, pointerId: event.pointerId, startX: event.clientX, startY: event.clientY, x: position.x, y: position.y, moved: false, target: event.currentTarget, openOnClick: true };
    event.currentTarget.setPointerCapture(event.pointerId);
  }

  function startBoardDrag(event) {
    if (state.canvasMode !== "select" || event.button !== 0 || event.target.closest("button, input, select, textarea")) return;
    const board = event.currentTarget.closest(".canvas-board");
    const id = board.dataset.boardId;
    const position = state.positions[id];
    state.drag = { id, pointerId: event.pointerId, startX: event.clientX, startY: event.clientY, x: position.x, y: position.y, moved: false, target: board, openOnClick: false };
    event.currentTarget.setPointerCapture(event.pointerId);
    board.classList.add("is-dragging");
  }

  function movePointer(event) {
    if (state.drag && state.drag.pointerId === event.pointerId) {
      const dx = (event.clientX - state.drag.startX) / state.zoom;
      const dy = (event.clientY - state.drag.startY) / state.zoom;
      state.drag.moved ||= Math.abs(dx) + Math.abs(dy) > 3;
      const maxX = CANVAS_WIDTH - state.drag.target.offsetWidth;
      const maxY = CANVAS_HEIGHT - state.drag.target.offsetHeight;
      const position = { x: clamp(state.drag.x + dx, 0, maxX), y: clamp(state.drag.y + dy, 0, maxY) };
      state.positions[state.drag.id] = position;
      state.drag.target.style.left = `${position.x}px`;
      state.drag.target.style.top = `${position.y}px`;
      renderEdges();
    }
    if (state.panDrag && state.panDrag.pointerId === event.pointerId) {
      state.pan.x = state.panDrag.x + event.clientX - state.panDrag.startX;
      state.pan.y = state.panDrag.y + event.clientY - state.panDrag.startY;
      applyTransform();
    }
    if (state.candleDrag) moveCandleHandle(event);
  }

  function endPointer(event) {
    if (state.drag && state.drag.pointerId === event.pointerId) {
      const { id, moved, openOnClick, target } = state.drag;
      state.drag = null;
      target.classList.remove("is-dragging");
      savePositions();
      if (!moved && openOnClick) selectNode(id, true);
    }
    if (state.panDrag && state.panDrag.pointerId === event.pointerId) {
      state.panDrag = null;
      $("#canvasViewport").classList.remove("is-panning");
    }
    if (state.candleDrag) {
      state.candleDrag = null;
      $$(".k-handle.active").forEach((node) => node.classList.remove("active"));
    }
  }

  function selectNode(id, switchPanel = false) {
    const node = state.nodes.find((item) => item.id === id);
    if (!node) return;
    state.selectedNodeId = id;
    if (node.type === "entity" && String(node.raw.entity_type || "").toLowerCase() === "character") {
      state.selectedCharacterName = node.raw.name;
    }
    if (node.type === "scene") {
      state.activeSceneId = node.raw.id;
      renderManuscript();
      const linkedCandle = (state.snapshot?.ohlc || []).find((row) => row.timeline_event_id === node.raw.id);
      if (linkedCandle) selectCandle(linkedCandle, false);
      renderKline();
      renderLedgerBoard();
      requestAnimationFrame(renderEdges);
    }
    $$(".canvas-node").forEach((element) => element.classList.toggle("selected", element.dataset.nodeId === id));
    $$(".document-item").forEach((element) => element.classList.toggle("selected", id === `source:${element.dataset.documentId}`));
    $("#inspectorEmpty").hidden = true;
    $("#nodeInspector").hidden = false;
    $("#selectedKind").textContent = node.typeLabel;
    $("#selectedTitle").textContent = node.title;
    $("#selectedDescription").textContent = node.description;
    const details = $("#selectedDetails");
    const linkedOhlc = node.type === "scene"
      ? (state.snapshot?.ohlc || []).filter((row) => row.timeline_event_id === node.raw.id)
      : [];
    const pairs = node.type === "scene"
      ? [
          ["定位", node.locator],
          ["故事时间", node.raw.story_time || "未填写"],
          ["分支", node.raw.branch],
          ["人物 K 线", linkedOhlc.length ? linkedOhlc.map((row) => `${row.character_name}/${row.dimension} C${row.close}`).join("；") : "未关联"],
          ["生成证据", node.raw.attrs?.citations?.length ? `${node.raw.attrs.citations.length} 条` : "—"],
        ]
      : node.type === "source"
        ? [["格式", node.raw.kind.toUpperCase()], ["版本", `v${node.raw.version}`], ["引用块", node.raw.chunk_count], ["路径", node.raw.path]]
        : [["类型", node.raw.entity_type], ["别名", node.raw.aliases?.join("、") || "—"], ["标识", node.raw.id]];
    details.replaceChildren(...pairs.flatMap(([term, value]) => {
      const dt = document.createElement("dt"); dt.textContent = term;
      const dd = document.createElement("dd"); dd.textContent = value;
      return [dt, dd];
    }));
    $("#selectionHint").textContent = `${node.typeLabel}：${node.title}`;
    if (node.type === "scene" && node.raw.attrs?.citations?.length) {
      state.evidence = node.raw.attrs.citations.map((citation) => ({
        ...citation,
        snippet: `生成该场景时使用的 ${citation.citation || "项目"} 证据`,
      }));
      renderEvidence();
    }
    if (switchPanel) switchTab("inspector");
  }

  async function searchKnowledge(query) {
    const cleaned = String(query || "").trim();
    if (!cleaned) return;
    $("#selectionHint").textContent = `正在检索：${cleaned}`;
    try {
      const result = await api(`/v1/projects/${encodeURIComponent(state.projectId)}/context`, jsonOptions({
        query: cleaned,
        top_k: 8,
        scope: currentContextScope(),
      }));
      state.evidence = result.evidence_refs || [];
      renderEvidence();
      switchTab("inspector");
      $("#selectionHint").textContent = `找到 ${state.evidence.length} 条证据`;
      return result;
    } catch (error) {
      toast(error.message);
      return null;
    }
  }

  function renderEvidence() {
    $("#evidenceCount").textContent = `${state.evidence.length} 条`;
    const list = $("#evidenceList");
    list.replaceChildren(...state.evidence.map((item) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "evidence-item";
      button.dataset.kind = item.kind || "source";
      const title = document.createElement("strong");
      const locator = Object.entries(item.locator || {}).filter(([, value]) => value != null).map(([key, value]) => `${key}:${value}`).join(" · ");
      const ref = item.ref || "S?";
      title.textContent = `[${ref}] ${EVIDENCE_LABELS[item.kind] || "来源"} · ${displayTitle(item.title)}${locator ? ` · ${locator}` : ""}`;
      const body = document.createElement("p");
      body.textContent = item.summary || item.snippet || "";
      button.append(title, body);
      button.addEventListener("click", () => focusEvidence(item, false));
      return button;
    }));
  }

  function countCharacters(value) {
    return Array.from(String(value || "").replace(/\s/g, "")).length;
  }

  function activeScene() {
    return (state.snapshot?.timeline || []).find((event) => event.id === state.activeSceneId) || null;
  }

  function manuscriptDraft(scene) {
    if (!scene) return "";
    return Object.prototype.hasOwnProperty.call(state.manuscriptDrafts, scene.id)
      ? state.manuscriptDrafts[scene.id]
      : scene.description;
  }

  function updateManuscriptState(scene) {
    const draft = manuscriptDraft(scene);
    const dirty = Boolean(scene) && draft !== scene.description;
    $("#manuscriptCount").textContent = `${countCharacters(draft)} 字 · ${String(draft).length} 字符`;
    $("#manuscriptState").textContent = dirty ? "未提交" : "已同步";
    $("#manuscriptState").classList.toggle("dirty", dirty);
    $("#saveManuscript").disabled = !dirty;
    $("#revertManuscript").disabled = !dirty;
  }

  function renderManuscript() {
    const timeline = state.snapshot?.timeline || [];
    const picker = $("#manuscriptSceneSelect");
    picker.replaceChildren(...timeline.map((event) => {
      const option = document.createElement("option");
      option.value = event.id;
      option.textContent = `${locatorFor(event)} · ${event.label}`;
      return option;
    }));
    const scene = activeScene();
    const editor = $("#manuscriptText");
    if (!scene) {
      $("#manuscriptTitle").textContent = "场景正文";
      $("#manuscriptLocator").textContent = "暂无场景";
      editor.value = "";
      editor.disabled = true;
      editor.hidden = true;
      $("#projectStart").hidden = false;
      picker.disabled = true;
      updateManuscriptState(null);
      return;
    }
    $("#projectStart").hidden = true;
    editor.hidden = false;
    picker.disabled = false;
    picker.value = scene.id;
    $("#manuscriptTitle").textContent = scene.label;
    $("#manuscriptLocator").textContent = `${locatorFor(scene)} · ${scene.story_time || "故事时间未填写"}`;
    editor.disabled = false;
    editor.value = manuscriptDraft(scene);
    closeSelectionTools();
    updateManuscriptState(scene);
  }

  function closeSelectionTools() {
    state.textSelection = null;
    state.activeSelectionPatch = null;
    $("#selectionToolbar").hidden = true;
    $("#selectionReview").hidden = true;
  }

  function captureManuscriptSelection() {
    const scene = activeScene();
    const editor = $("#manuscriptText");
    const start = editor.selectionStart;
    const end = editor.selectionEnd;
    const selected = editor.value.slice(start, end);
    if (!scene || start === end || !selected) {
      state.textSelection = null;
      $("#selectionToolbar").hidden = true;
      return;
    }
    state.textSelection = { sceneId: scene.id, start, end, original: selected };
    state.activeSelectionPatch = null;
    $("#selectionReview").hidden = true;
    $("#selectionCount").textContent = `已选 ${selected.length} 字符 · ${start}–${end}`;
    $("#selectionPreview").textContent = selected.replace(/\s+/g, " ").slice(0, 100);
    $("#selectionToolbar").hidden = false;
  }

  function selectCurrentParagraph() {
    const editor = $("#manuscriptText");
    if (editor.disabled || !editor.value) return;
    const text = editor.value;
    const caret = editor.selectionStart;
    let start = text.lastIndexOf("\n\n", Math.max(0, caret - 1));
    start = start < 0 ? 0 : start + 2;
    let end = text.indexOf("\n\n", caret);
    if (end < 0) end = text.length;
    while (start < end && text[start] === "\n") start += 1;
    while (end > start && text[end - 1] === "\n") end -= 1;
    if (start === end) {
      toast("当前光标位置没有可选择的段落");
      return;
    }
    editor.focus();
    editor.setSelectionRange(start, end);
    captureManuscriptSelection();
  }

  function selectionIsCurrent(selection = state.textSelection) {
    const scene = activeScene();
    const editor = $("#manuscriptText");
    return Boolean(selection && scene?.id === selection.sceneId && editor.value.slice(selection.start, selection.end) === selection.original);
  }

  function updateSelectionApplyState() {
    const patch = state.activeSelectionPatch;
    const replacement = $("#selectionReplacement").value;
    $("#applySelectionPatch").disabled = !patch || replacement === patch.original;
  }

  function openSelectionReview(replacement, source, citations = []) {
    if (!selectionIsCurrent()) {
      toast("正文已变化，请重新选择要修改的段落");
      return;
    }
    const instruction = $("#selectionInstruction").value.trim();
    state.activeSelectionPatch = {
      ...state.textSelection,
      replacement: String(replacement ?? ""),
      source,
      instruction,
      citations,
    };
    $("#selectionOriginal").textContent = state.textSelection.original;
    $("#selectionReplacement").value = String(replacement ?? "");
    $("#selectionPatchMeta").textContent = `${state.textSelection.start}–${state.textSelection.end} · 其余正文不变`;
    $("#selectionEvidence").textContent = source === "ai" ? `${citations.length} 条项目证据` : "手工局部补丁";
    $("#selectionToolbar").hidden = true;
    $("#selectionReview").hidden = false;
    updateSelectionApplyState();
    $("#selectionReplacement").focus();
  }

  function startManualSelectionPatch() {
    if (!selectionIsCurrent()) {
      toast("请先选中要替换的文字");
      return;
    }
    openSelectionReview(state.textSelection.original, "manual");
  }

  function cleanSelectionRewrite(value) {
    return String(value || "")
      .trim()
      .replace(/^```(?:text|markdown)?\s*/i, "")
      .replace(/\s*```$/, "")
      .trim();
  }

  async function runSelectionRewrite() {
    if (!selectionIsCurrent()) {
      toast("请先选中要改写的句子或段落");
      return;
    }
    const instruction = $("#selectionInstruction").value.trim();
    if (!instruction) {
      toast("请填写这段文字要怎么修改");
      $("#selectionInstruction").focus();
      return;
    }
    if (!state.config?.llm?.configured) {
      openModelDialog();
      toast("连接模型后即可只改写当前选区");
      return;
    }
    const scene = activeScene();
    const button = $("#runSelectionRewrite");
    button.disabled = true;
    button.textContent = "生成补丁…";
    try {
      const result = await api(`/v1/projects/${encodeURIComponent(state.projectId)}/chat`, jsonOptions({
        message: [
          "你是 IDE 风格的选区改写器。",
          "只输出用于替换选区的正文，不要解释，不要标题，不要 Markdown 代码围栏。",
          `场景：${locatorFor(scene)}《${scene.label}》`,
          `修改要求：${instruction}`,
          "待替换选区：",
          state.textSelection.original,
        ].join("\n"),
        mode: "rewrite",
        top_k: 8,
        scope: currentContextScope(),
      }));
      const replacement = cleanSelectionRewrite(result.answer);
      if (!replacement) throw new Error("模型没有返回可用的替换文本");
      state.evidence = result.evidence_refs || [];
      renderEvidence();
      openSelectionReview(replacement, "ai", sourceCitations(result.evidence_refs));
    } catch (error) {
      toast(`选区改写失败：${error.message}`);
    } finally {
      button.disabled = false;
      button.textContent = "AI 改写选区";
    }
  }

  function cancelSelectionPatch() {
    state.activeSelectionPatch = null;
    $("#selectionReview").hidden = true;
    if (selectionIsCurrent()) $("#selectionToolbar").hidden = false;
  }

  function applySelectionPatch() {
    const patch = state.activeSelectionPatch;
    if (!patch || !selectionIsCurrent(patch)) {
      toast("选区已变化，未应用补丁");
      closeSelectionTools();
      return;
    }
    const scene = activeScene();
    const editor = $("#manuscriptText");
    const replacement = $("#selectionReplacement").value;
    const nextText = `${editor.value.slice(0, patch.start)}${replacement}${editor.value.slice(patch.end)}`;
    state.manuscriptDrafts[scene.id] = nextText;
    if (!state.manuscriptPatches[scene.id]) state.manuscriptPatches[scene.id] = [];
    state.manuscriptPatches[scene.id].push({
      start: patch.start,
      end: patch.end,
      removed: patch.original,
      inserted: replacement,
      source: patch.source,
      instruction: patch.instruction,
      applied_at: new Date().toISOString(),
    });
    editor.value = nextText;
    const nextEnd = patch.start + replacement.length;
    editor.focus();
    editor.setSelectionRange(patch.start, nextEnd);
    closeSelectionTools();
    updateManuscriptState(scene);
    toast(`已应用局部补丁；其余 ${nextText.length - replacement.length} 个字符未改动`);
  }

  function updateManuscriptDraft() {
    const scene = activeScene();
    if (!scene) return;
    state.manuscriptDrafts[scene.id] = $("#manuscriptText").value;
    closeSelectionTools();
    updateManuscriptState(scene);
  }

  function revertManuscript() {
    const scene = activeScene();
    if (!scene) return;
    delete state.manuscriptDrafts[scene.id];
    delete state.manuscriptPatches[scene.id];
    closeSelectionTools();
    renderManuscript();
    toast("已还原为账本中的当前正文");
  }

  async function saveManuscript() {
    const scene = activeScene();
    if (!scene) return;
    const description = manuscriptDraft(scene);
    if (!description.trim()) {
      toast("正文不能为空");
      return;
    }
    if (description === scene.description) return;
    try {
      let task = await createTask(
        `修改 ${locatorFor(scene)}《${scene.label}》正文`,
        [{ tool: "update_timeline_event", args: { event_id: scene.id, description, patches: state.manuscriptPatches[scene.id] || [] } }],
      );
      task = await approveFormTask(task);
      if (task.status === "completed") {
        delete state.manuscriptDrafts[scene.id];
        delete state.manuscriptPatches[scene.id];
        await loadSnapshot(false);
        toast("正文已保存，原文与新文均已写入账本");
      } else {
        switchTab("tasks");
        toast(`正文任务状态：${task.status}`);
      }
    } catch (error) { toast(error.message); }
  }

  function renderKline() {
    const allRows = state.snapshot?.ohlc || [];
    const selectedRows = allRows.filter((row) =>
      (!state.selectedCharacterName || row.character_name === state.selectedCharacterName)
      && (!state.selectedDimension || row.dimension === state.selectedDimension)
    );
    const sceneRows = state.activeSceneId
      ? allRows.filter((row) => row.timeline_event_id === state.activeSceneId)
      : [];
    const rows = (selectedRows.length ? selectedRows : (sceneRows.length ? sceneRows : allRows)).slice(0, 10);
    const firstRow = rows[0];
    const oneSeries = firstRow && rows.every((row) =>
      row.character_name === firstRow.character_name && row.dimension === firstRow.dimension
    );
    $("#klineBoardTitle").textContent = oneSeries
      ? `${firstRow.character_name} · ${firstRow.dimension} K 线`
      : "人物 K 线";
    const svg = $("#klineChart");
    svg.replaceChildren();
    const ns = "http://www.w3.org/2000/svg";
    const xStart = 55;
    const xEnd = 392;
    const plotTop = 15;
    const plotHeight = 150;
    const yFor = (value) => plotTop + (100 - Number(value)) / 100 * plotHeight;
    [0, 25, 50, 75, 100].forEach((tick) => {
      const y = yFor(tick);
      const line = document.createElementNS(ns, "line");
      line.setAttribute("class", "k-grid"); line.setAttribute("x1", "36"); line.setAttribute("x2", String(xEnd)); line.setAttribute("y1", y); line.setAttribute("y2", y);
      const label = document.createElementNS(ns, "text");
      label.setAttribute("class", "k-axis-label"); label.setAttribute("x", "27"); label.setAttribute("y", y + 3); label.setAttribute("text-anchor", "end"); label.textContent = tick;
      svg.append(line, label);
    });
    if (!rows.length) {
      const empty = document.createElementNS(ns, "text");
      empty.setAttribute("x", "210"); empty.setAttribute("y", "105"); empty.setAttribute("text-anchor", "middle"); empty.setAttribute("class", "k-axis-label"); empty.textContent = "暂无 OHLC 数据";
      svg.append(empty);
      $("#ohlcEditor").hidden = true;
      return;
    }
    const gap = rows.length > 1 ? Math.min(68, (xEnd - xStart) / (rows.length - 1)) : 0;
    rows.forEach((row, index) => {
      const active = state.selectedOhlc === row.id;
      const values = active && state.candleDraft ? state.candleDraft : row;
      const x = xStart + index * gap;
      const wick = document.createElementNS(ns, "line");
      wick.setAttribute("class", "k-wick"); wick.setAttribute("x1", x); wick.setAttribute("x2", x); wick.setAttribute("y1", yFor(values.high)); wick.setAttribute("y2", yFor(values.low));
      const body = document.createElementNS(ns, "rect");
      const yOpen = yFor(values.open); const yClose = yFor(values.close);
      body.setAttribute("class", `k-body ${Number(values.close) >= Number(values.open) ? "up" : "down"}`);
      body.setAttribute("x", x - 7); body.setAttribute("y", Math.min(yOpen, yClose)); body.setAttribute("width", "14"); body.setAttribute("height", Math.max(3, Math.abs(yOpen - yClose))); body.setAttribute("rx", "1");
      body.style.cursor = "pointer";
      body.addEventListener("click", () => selectCandle(row));
      const label = document.createElementNS(ns, "text");
      label.setAttribute("class", "k-candle-label"); label.setAttribute("x", x); label.setAttribute("y", "190"); label.textContent = String(row.period_id).replace(/^E\d+-/, "");
      const valueLabel = document.createElementNS(ns, "text");
      valueLabel.setAttribute("class", "k-value-label"); valueLabel.setAttribute("x", x); valueLabel.setAttribute("y", "203"); valueLabel.textContent = `C${values.close}`;
      const title = document.createElementNS(ns, "title");
      const linkedScene = (state.snapshot?.timeline || []).find((scene) => scene.id === row.timeline_event_id);
      title.textContent = `${row.period_id}  开 ${values.open}  高 ${values.high}  低 ${values.low}  收 ${values.close}${linkedScene ? `  场景 ${locatorFor(linkedScene)}《${linkedScene.label}》` : "  未关联场景"}`;
      body.append(title);
      svg.append(wick, body, label, valueLabel);
      if (active) {
        const handles = [
          ["high", x, yFor(values.high)], ["low", x, yFor(values.low)],
          ["open", x - 8, yFor(values.open)], ["close", x + 8, yFor(values.close)],
        ];
        handles.forEach(([field, hx, hy]) => {
          const handle = document.createElementNS(ns, "circle");
          handle.setAttribute("class", "k-handle"); handle.setAttribute("cx", hx); handle.setAttribute("cy", hy); handle.setAttribute("r", "4"); handle.dataset.field = field;
          handle.addEventListener("pointerdown", (event) => {
            event.preventDefault(); event.stopPropagation();
            state.candleDrag = { field, pointerId: event.pointerId };
            handle.classList.add("active");
          });
          svg.append(handle);
        });
      }
    });
    if (!state.selectedOhlc || !rows.some((row) => row.id === state.selectedOhlc)) {
      const linked = rows.find((row) => row.timeline_event_id === state.activeSceneId);
      selectCandle(linked || rows[0], true);
    }
  }

  function shortHash(value) {
    const hash = String(value || "");
    return hash.length > 14 ? `${hash.slice(0, 7)}…${hash.slice(-7)}` : (hash || "—");
  }

  function ledgerEventSubject(event) {
    const payload = event.payload || {};
    return payload.label || payload.title || payload.name || payload.goal || payload.period_id || payload.task_id || "已记录完整事件载荷";
  }

  function renderLedgerTextVersion() {
    const scene = activeScene();
    const history = $("#ledgerTextHistory");
    const patches = $("#ledgerTextPatches");
    patches.replaceChildren();
    if (!scene) {
      $("#ledgerTextTitle").textContent = "选择一个场景";
      $("#ledgerTextLocator").textContent = "正文版本会在这里展开";
      $("#ledgerTextVersionMeta").textContent = "—";
      $("#ledgerTextCurrent").textContent = "选择场景节点后查看当前完整正文。";
      history.hidden = true;
      history.open = false;
      return;
    }
    const versions = (state.ledger?.events || []).filter((event) => {
      const payload = event.payload || {};
      return (event.event_type === "timeline.added" || event.event_type === "timeline.updated") && payload.id === scene.id;
    });
    const latest = versions[0] || null;
    const previousText = latest?.event_type === "timeline.updated" ? latest.payload?.before?.description : null;
    const latestPatches = latest?.event_type === "timeline.updated" ? (latest.payload?.patches || []) : [];
    $("#ledgerTextTitle").textContent = scene.label;
    $("#ledgerTextLocator").textContent = `${locatorFor(scene)} · 当前完整正文 · ${String(scene.description || "").length} 字符`;
    $("#ledgerTextVersionMeta").textContent = latest
      ? `v${Math.max(versions.length, 1)} · #${latest.seq} · ${shortHash(latest.event_hash)}`
      : "当前快照 · 未找到历史事件";
    $("#ledgerTextCurrent").textContent = scene.description || "（正文为空）";
    history.hidden = previousText == null;
    if (previousText == null) {
      history.open = false;
      return;
    }
    $("#ledgerTextPrevious").textContent = previousText;
    if (!latestPatches.length) {
      const item = document.createElement("div");
      item.className = "ledger-patch";
      item.textContent = "本次保存记录了修改前后全文，没有单独的区间补丁。";
      patches.append(item);
    } else {
      latestPatches.forEach((patch) => {
        const item = document.createElement("div");
        item.className = "ledger-patch";
        item.textContent = `${patch.start ?? "?"}–${patch.end ?? "?"} · ${patch.source === "ai" ? "AI" : "手工"} · “${String(patch.removed || "").slice(0, 36)}” → “${String(patch.inserted || "").slice(0, 36)}”`;
        patches.append(item);
      });
    }
  }

  function renderLedgerBoard() {
    const verification = state.ledger?.verification || {};
    const validity = $("#ledgerValidity");
    validity.textContent = verification.valid ? "有效" : "异常";
    validity.classList.toggle("invalid", verification.valid === false);
    $("#ledgerEventCount").textContent = String(verification.event_count ?? state.ledger?.events?.length ?? 0);
    $("#ledgerHead").textContent = shortHash(verification.head);
    renderLedgerTextVersion();
    const list = $("#ledgerEvents");
    const events = (state.ledger?.events || []).slice(0, 8);
    if (!events.length) {
      const empty = document.createElement("p");
      empty.className = "ledger-empty";
      empty.textContent = "账本还没有事件。";
      list.replaceChildren(empty);
      return;
    }
    list.replaceChildren(...events.map((event) => {
      const details = document.createElement("details");
      details.className = "ledger-event";
      const summary = document.createElement("summary");
      const seq = document.createElement("span"); seq.className = "ledger-seq"; seq.textContent = `#${event.seq}`;
      const copy = document.createElement("span"); copy.className = "ledger-event-copy";
      const type = document.createElement("strong"); type.textContent = event.event_type;
      const subject = document.createElement("small"); subject.textContent = ledgerEventSubject(event);
      copy.append(type, subject);
      const meta = document.createElement("span"); meta.className = "ledger-event-meta";
      const actor = document.createElement("small"); actor.textContent = event.actor;
      const hash = document.createElement("code"); hash.textContent = shortHash(event.event_hash);
      meta.append(actor, hash);
      summary.append(seq, copy, meta);
      const payload = document.createElement("pre");
      payload.textContent = JSON.stringify(event.payload || {}, null, 2);
      const footer = document.createElement("div"); footer.className = "ledger-event-footer";
      const parent = document.createElement("code"); parent.textContent = `parent ${shortHash(event.parent_hash)}`;
      const time = document.createElement("time");
      time.dateTime = event.created_at;
      time.textContent = new Date(event.created_at).toLocaleString("zh-CN", { hour12: false });
      footer.append(parent, time);
      details.append(summary, payload, footer);
      return details;
    }));
  }

  async function refreshLedger() {
    try {
      state.ledger = await api(`/v1/projects/${encodeURIComponent(state.projectId)}/ledger/events?limit=50`);
      renderLedgerBoard();
      toast("连续性账本已重新校验");
    } catch (error) { toast(error.message); }
  }

  function selectCandle(row, rerender = true) {
    state.selectedOhlc = row.id;
    state.selectedCharacterName = row.character_name || null;
    state.selectedDimension = row.dimension || null;
    state.candleDraft = { open: Number(row.open), high: Number(row.high), low: Number(row.low), close: Number(row.close), row };
    $("#ohlcEditor").hidden = false;
    $("#ohlcPeriod").textContent = `${row.character_name} · ${row.period_id}`;
    const scene = (state.snapshot?.timeline || []).find((item) => item.id === row.timeline_event_id);
    $("#ohlcSceneLink").textContent = scene
      ? `关联场景：${locatorFor(scene)} · ${scene.label}`
      : (row.attrs?.aggregated ? "父周期：由多个场景自动聚合" : "关联场景：未设置");
    syncOhlcInputs();
    if (rerender) renderKline();
  }

  function syncOhlcInputs() {
    if (!state.candleDraft) return;
    $$("[data-ohlc]").forEach((input) => { input.value = state.candleDraft[input.dataset.ohlc]; });
  }

  function updateDraft(field, value) {
    const draft = state.candleDraft;
    if (!draft) return;
    value = clamp(Math.round(Number(value)), 0, 100);
    if (field === "high") value = Math.max(value, draft.open, draft.close);
    if (field === "low") value = Math.min(value, draft.open, draft.close);
    if (field === "open" || field === "close") value = clamp(value, draft.low, draft.high);
    draft[field] = value;
    syncOhlcInputs();
    renderKline();
  }

  function moveCandleHandle(event) {
    if (!state.candleDrag || event.pointerId !== state.candleDrag.pointerId) return;
    const svg = $("#klineChart");
    const point = svg.createSVGPoint();
    point.x = event.clientX; point.y = event.clientY;
    const local = point.matrixTransform(svg.getScreenCTM().inverse());
    const value = 100 - (local.y - 15) / 150 * 100;
    updateDraft(state.candleDrag.field, value);
  }

  function switchTab(name) {
    $$(".right-tab").forEach((button) => {
      const active = button.dataset.tab === name;
      button.classList.toggle("active", active);
      button.setAttribute("aria-selected", String(active));
    });
    $$(".tab-panel").forEach((panel) => panel.classList.toggle("active", panel.dataset.panel === name));
  }

  function syncChatMode() {
    const view = CreativeClawColdStart.modeView($("#chatMode").value, state.snapshot);
    $("#chatInput").placeholder = view.placeholder;
    $("#sendChat").textContent = view.primaryLabel;
    $("#sendChat").disabled = view.coldStart && (!view.empty || state.coldStart.busy);
    $("#previewContext").hidden = view.coldStart;
    $("#previewContext").disabled = view.coldStart;
    $("#contextBadge").hidden = view.coldStart;
    $(".workflow-reminder").textContent = view.coldStart
      ? "当前工序：生成预览 → 审阅骨架 → 采用全部"
      : "当前工序：确认上下文 → 运行模型 → 审阅候选 → 接受或拒绝";
    $("#chatStatus").textContent = view.coldStart
      ? view.status
      : (state.config?.llm?.configured ? "本地知识库已就绪" : "请先连接模型");
  }

  function coldStartSection(title) {
    const section = document.createElement("section");
    section.className = "cold-start-section";
    const heading = document.createElement("strong");
    heading.textContent = title;
    section.append(heading);
    return section;
  }

  function renderColdStartPreview(payload) {
    $(".cold-start-preview")?.remove();
    const preview = payload.preview;
    const summary = CreativeClawColdStart.summarizePreview(preview);
    const entityByKey = Object.fromEntries(preview.entities.map((entity) => [entity.key, entity]));
    const typeLabels = {
      character: "人物",
      location: "地点",
      object: "物件",
      organization: "组织",
      canon_fact: "关键事实",
    };
    const article = document.createElement("article");
    article.className = "message assistant-message cold-start-preview";
    const title = document.createElement("h3");
    title.textContent = preview.title;
    const premise = document.createElement("p");
    premise.className = "cold-start-premise";
    premise.textContent = preview.premise;
    const countRow = document.createElement("div");
    countRow.className = "cold-start-summary";
    [
      `${summary.entityCount} 个实体`,
      `${summary.relationCount} 条关系`,
      `${summary.sceneCount} 个场景`,
    ].forEach((label) => {
      const badge = document.createElement("span");
      badge.textContent = label;
      countRow.append(badge);
    });
    article.append(title, premise, countRow);

    const entitySection = coldStartSection("实体与定位");
    const entityGrid = document.createElement("div");
    entityGrid.className = "cold-start-entities";
    preview.entities.forEach((entity) => {
      const card = document.createElement("article");
      card.className = "cold-start-entity";
      const header = document.createElement("header");
      const name = document.createElement("strong");
      name.textContent = entity.name;
      const type = document.createElement("span");
      type.className = "cold-start-type";
      type.textContent = typeLabels[entity.entity_type] || entity.entity_type;
      const description = document.createElement("p");
      description.textContent = entity.description;
      header.append(name, type);
      card.append(header, description);
      entityGrid.append(card);
    });
    entitySection.append(entityGrid);
    article.append(entitySection);

    if (preview.relations.length) {
      const relationSection = coldStartSection("关键关系");
      const list = document.createElement("ul");
      list.className = "cold-start-relations";
      preview.relations.forEach((relation) => {
        const item = document.createElement("li");
        item.textContent = `${entityByKey[relation.source_key]?.name || relation.source_key} · ${relation.predicate} · ${entityByKey[relation.target_key]?.name || relation.target_key}`;
        list.append(item);
      });
      relationSection.append(list);
      article.append(relationSection);
    }

    const klineSection = coldStartSection("主人公 K 线");
    const klineSummary = document.createElement("div");
    klineSummary.className = "cold-start-summary";
    [summary.protagonistName, summary.dimension, `${summary.sceneCount} 根 K 线`].forEach((label) => {
      const badge = document.createElement("span");
      badge.textContent = label;
      klineSummary.append(badge);
    });
    klineSection.append(klineSummary);
    article.append(klineSection);

    const sceneSection = coldStartSection("场景卡");
    const sceneList = document.createElement("ol");
    sceneList.className = "cold-start-scenes";
    preview.scenes.forEach((scene, index) => {
      const item = document.createElement("li");
      item.className = "cold-start-scene";
      const header = document.createElement("header");
      const name = document.createElement("strong");
      name.textContent = `${index + 1}. ${scene.title}`;
      const values = document.createElement("span");
      values.className = "cold-start-ohlc";
      values.textContent = `O${scene.ohlc.open} H${scene.ohlc.high} L${scene.ohlc.low} C${scene.ohlc.close}`;
      const description = document.createElement("p");
      description.textContent = scene.story_time
        ? `${scene.story_time} · ${scene.summary}`
        : scene.summary;
      header.append(name, values);
      item.append(header, description);
      sceneList.append(item);
    });
    sceneSection.append(sceneList);
    article.append(sceneSection);

    const actions = document.createElement("div");
    actions.className = "cold-start-actions";
    const regenerate = document.createElement("button");
    regenerate.type = "button";
    regenerate.id = "coldStartRegenerate";
    regenerate.className = "btn secondary";
    regenerate.textContent = "重新生成";
    regenerate.addEventListener("click", generateColdStartPreview);
    const apply = document.createElement("button");
    apply.type = "button";
    apply.id = "coldStartApply";
    apply.className = "btn primary";
    apply.textContent = "采用全部";
    apply.addEventListener("click", applyColdStartPreview);
    actions.append(regenerate, apply);
    article.append(actions);
    $("#chatStream").append(article);
    $("#chatStream").scrollTop = $("#chatStream").scrollHeight;
  }

  async function generateColdStartPreview() {
    const prompt = $("#chatInput").value.trim();
    if (!prompt) {
      toast("请先输入一句创作意图");
      $("#chatInput").focus();
      return;
    }
    const view = CreativeClawColdStart.modeView("cold_start", state.snapshot);
    if (!view.empty) {
      $("#chatStatus").textContent = view.status;
      toast(view.status);
      return;
    }
    if (!state.config?.llm?.configured) {
      $("#chatStatus").textContent = "请先连接模型";
      openModelDialog();
      return;
    }
    state.coldStart.busy = true;
    state.coldStart.prompt = prompt;
    syncChatMode();
    $("#chatStatus").textContent = "正在生成轻量骨架";
    addMessage("user", prompt);
    const loading = addMessage("assistant", "正在生成标题、实体、场景卡与主人公 K 线…");
    loading.classList.add("loading");
    let finalStatus = "";
    try {
      const result = await api(
        `/v1/projects/${encodeURIComponent(state.projectId)}/cold-start/preview`,
        jsonOptions({ prompt }),
      );
      state.coldStart.preview = result.preview;
      state.coldStart.generation = result.generation;
      loading.remove();
      renderColdStartPreview(result);
      finalStatus = `${result.generation.model} · 框架预览已生成，尚未写入项目`;
    } catch (error) {
      loading.remove();
      addMessage("assistant", `框架生成失败：${error.message}`);
      finalStatus = "生成失败；创作意图已保留，可重试";
    } finally {
      state.coldStart.busy = false;
      syncChatMode();
      if (finalStatus) $("#chatStatus").textContent = finalStatus;
    }
  }

  async function applyColdStartPreview() {
    if (!state.coldStart.preview || !state.coldStart.generation || state.coldStart.busy) return;
    state.coldStart.busy = true;
    const card = $(".cold-start-preview");
    const applyButton = $("#coldStartApply");
    const regenerateButton = $("#coldStartRegenerate");
    if (applyButton) { applyButton.disabled = true; applyButton.textContent = "正在采用…"; }
    if (regenerateButton) regenerateButton.disabled = true;
    $("#sendChat").disabled = true;
    $("#chatStatus").textContent = "正在一次性写入实体、关系、场景与 K 线";
    let finalStatus = "";
    let succeeded = false;
    try {
      const result = await api(
        `/v1/projects/${encodeURIComponent(state.projectId)}/cold-start/apply`,
        jsonOptions({
          preview: state.coldStart.preview,
          generation: state.coldStart.generation,
        }),
      );
      const option = $(`#projectSelect option[value="${CSS.escape(state.projectId)}"]`);
      if (option) option.textContent = result.snapshot.project.name;
      if (card) card.classList.add("is-applied");
      if (applyButton) applyButton.textContent = "已采用";
      state.coldStart.preview = null;
      state.coldStart.generation = null;
      await loadSnapshot(true);
      addMessage(
        "assistant",
        `框架已采用：${result.summary.entities} 个实体、${result.summary.relations} 条关系、${result.summary.scenes} 个场景、${result.summary.ohlc} 根 K 线。`,
      );
      finalStatus = "冷启动框架已写入项目";
      succeeded = true;
    } catch (error) {
      addMessage("assistant", `采用失败：${error.message}`);
      finalStatus = "采用失败；预览仍保留，项目未写入半成品";
    } finally {
      state.coldStart.busy = false;
      syncChatMode();
      if (!succeeded) {
        if (applyButton) { applyButton.disabled = false; applyButton.textContent = "采用全部"; }
        if (regenerateButton) regenerateButton.disabled = false;
      }
      if (finalStatus) $("#chatStatus").textContent = finalStatus;
    }
  }

  function openSceneFromStory(text, citations) {
    const heading = String(text).match(/^\s*#{1,3}\s+(.+)$/m)?.[1]
      || String(text).match(/《([^》]+)》/)?.[1]
      || "AI 续写场景";
    const episodes = (state.snapshot?.timeline || []).map((item) => Number(item.episode)).filter(Number.isFinite);
    const episode = episodes.length ? Math.max(...episodes) : 1;
    const scenes = (state.snapshot?.timeline || []).filter((item) => Number(item.episode) === episode).map((item) => Number(item.scene)).filter(Number.isFinite);
    $("#sceneTitle").value = heading.replace(/[*_]/g, "").trim();
    $("#sceneEpisode").value = episode;
    $("#sceneNumber").value = scenes.length ? Math.max(...scenes) + 1 : 1;
    $("#sceneDescription").value = text;
    $("#sceneStoryTime").value = `E${episode} 上一场之后`;
    state.sceneDraftCitations = citations.map((item, index) => ({
      citation: `C${index + 1}`,
      document_id: item.document_id,
      title: item.title,
      locator: item.locator || {},
    }));
    $("#sceneDialog").showModal();
  }

  function openBlankSceneDialog() {
    const timeline = state.snapshot?.timeline || [];
    const episodes = timeline.map((item) => Number(item.episode)).filter(Number.isFinite);
    const episode = episodes.length ? Math.max(...episodes) : 1;
    const scenes = timeline
      .filter((item) => Number(item.episode) === episode)
      .map((item) => Number(item.scene))
      .filter(Number.isFinite);
    $("#sceneTitle").value = "";
    $("#sceneEpisode").value = episode;
    $("#sceneNumber").value = scenes.length ? Math.max(...scenes) + 1 : 1;
    $("#sceneDescription").value = "";
    $("#sceneStoryTime").value = "";
    state.sceneDraftCitations = [];
    $("#sceneDialog").showModal();
  }

  function addMessage(role, text, evidenceRefs = [], options = {}) {
    const article = document.createElement("article");
    article.className = `message ${role === "user" ? "user-message" : "assistant-message"}`;
    const paragraph = document.createElement("p");
    paragraph.textContent = text;
    article.append(paragraph);
    const evidenceIndex = CreativeClawContext.indexEvidenceRefs(evidenceRefs);
    const tokens = CreativeClawContext.parseCitationTokens(text);
    if (tokens.length) {
      const row = document.createElement("div");
      row.className = "citation-row";
      tokens.forEach((token) => {
        const item = evidenceIndex[token];
        if (!item) return;
        const button = document.createElement("button");
        button.type = "button";
        button.className = "citation-link";
        button.dataset.kind = item.kind;
        button.textContent = `[${token}] ${EVIDENCE_LABELS[item.kind] || "证据"} · ${displayTitle(item.title)}`;
        button.addEventListener("click", () => {
          state.evidence = evidenceRefs;
          renderEvidence();
          focusEvidence(item, true);
        });
        row.append(button);
      });
      if (row.childElementCount) article.append(row);
    }
    const unknown = options.citationValidation?.unknown || [];
    if (unknown.length) {
      const warning = document.createElement("p");
      warning.className = "citation-warning";
      warning.textContent = `引用警告：回答包含当前上下文中不存在的编号 ${unknown.map((ref) => `[${ref}]`).join("、")}。请勿将该引用视为已验证事实。`;
      article.append(warning);
    }
    if (role === "assistant" && options.saveable) {
      const citations = sourceCitations(evidenceRefs);
      state.lastAssistantDraft = { text, citations };
      const actions = document.createElement("div");
      actions.className = "message-actions";
      const save = document.createElement("button");
      save.type = "button";
      save.className = "message-action";
      save.textContent = "保存为场景";
      save.addEventListener("click", () => openSceneFromStory(text, citations));
      actions.append(save);
      article.append(actions);
    }
    $("#chatStream").append(article);
    $("#chatStream").scrollTop = $("#chatStream").scrollHeight;
    return article;
  }

  function renderContextPreview(result) {
    const summary = CreativeClawContext.summarizeContext(result);
    const scope = summary.scope;
    const pairs = [
      ["分支", scope.branch || "main"],
      ["集/章", scope.episode ?? "未限定"],
      ["场景", scope.scene_id || "未选择"],
      ["人物", scope.character_name || "未限定"],
      ["维度", scope.dimension || "未限定"],
      ["证据", `${summary.evidenceCount} 条`],
    ];
    $("#contextScopeSummary").replaceChildren(...pairs.flatMap(([term, value]) => {
      const dt = document.createElement("dt");
      dt.textContent = term;
      const dd = document.createElement("dd");
      dd.textContent = String(value);
      return [dt, dd];
    }));
    const current = (result.timeline || []).find((item) => item.id === scope.scene_id);
    $("#contextTimelineSummary").textContent = `时间线：${summary.timelineCount} 条${current ? ` · 当前 ${locatorFor(current)}《${current.label}》` : ""}`;
    $("#contextKlineSummary").textContent = summary.klineCount
      ? `K 线：${(result.ohlc || []).map((row) => `${row.character_name}/${row.dimension} O${row.open} H${row.high} L${row.low} C${row.close}`).join("；")}`
      : "K 线：0 条";
    const byKind = {};
    (result.evidence_refs || []).forEach((item) => {
      if (!byKind[item.kind]) byKind[item.kind] = [];
      byKind[item.kind].push(item.ref);
    });
    $("#contextEvidenceSummary").textContent = `证据：${Object.entries(byKind).map(([kind, refs]) => `${EVIDENCE_LABELS[kind] || kind} ${refs.length} [${refs.join("][")}]`).join(" · ") || "0 条"}`;
    const warnings = [];
    if (!summary.timelineCount) warnings.push("当前选择没有解析到时间线，请先选择场景或检查分支。");
    if (!summary.klineCount) warnings.push("当前选择没有关联 K 线；模型不会获得人物状态曲线。");
    const warningNode = $("#contextWarnings");
    warningNode.hidden = !warnings.length;
    warningNode.textContent = warnings.join(" ");
    $("#contextBadge").textContent = `上下文 ${summary.evidenceCount} 条 · 时间线 ${summary.timelineCount} · K 线 ${summary.klineCount}`;
  }

  async function loadContextPreview(message) {
    const scope = currentContextScope();
    const result = await api(`/v1/projects/${encodeURIComponent(state.projectId)}/context`, jsonOptions({
      query: message,
      top_k: 8,
      scope,
    }));
    state.lastContextPreview = { message, scope, result };
    renderContextPreview(result);
    return result;
  }

  async function prepareChatRequest() {
    const message = $("#chatInput").value.trim();
    if (!message) {
      toast("请先填写要分析或创作的问题");
      $("#chatInput").focus();
      return;
    }
    if (/^(请)?保存(为|成)?(场景|场景卡|正文)(吧)?[。！!]?$/u.test(message) && state.lastAssistantDraft) {
      $("#chatInput").value = "";
      addMessage("user", message);
      openSceneFromStory(state.lastAssistantDraft.text, state.lastAssistantDraft.citations || []);
      toast("已把上一条 AI 回复放入场景表单，请检查后保存");
      return;
    }
    $("#chatStatus").textContent = "正在生成上下文预览";
    try {
      await loadContextPreview(message);
      $("#contextDialog").showModal();
      $("#chatStatus").textContent = "请确认上下文后运行模型";
    } catch (error) {
      $("#chatStatus").textContent = "上下文获取失败";
      toast(error.message);
    }
  }

  async function runChatRequest(message, scope) {
    if (!state.config?.llm?.configured) {
      $("#chatStatus").textContent = "请先连接模型";
      openModelDialog();
      return;
    }
    $("#contextDialog").close();
    $("#chatInput").value = "";
    addMessage("user", message);
    const loading = addMessage("assistant", "正在使用已确认的上下文调用模型…");
    loading.classList.add("loading");
    $("#sendChat").disabled = true;
    $("#previewContext").disabled = true;
    $("#chatStatus").textContent = "正在运行";
    try {
      const result = await api(`/v1/projects/${encodeURIComponent(state.projectId)}/chat`, jsonOptions({
        message,
        mode: $("#chatMode").value,
        top_k: 8,
        scope,
      }));
      loading.remove();
      addMessage("assistant", result.answer || "模型没有返回正文。", result.evidence_refs || [], {
        saveable: true,
        citationValidation: result.citation_validation,
      });
      state.evidence = result.evidence_refs || [];
      renderEvidence();
      const needsReview = result.citation_validation?.unknown?.length ? " · 引用需复核" : "";
      $("#chatStatus").textContent = `${result.model} · ${state.evidence.length} 条证据 · 待审阅候选${needsReview}`;
    } catch (error) {
      loading.remove();
      const hint = error.code === "service_unreachable"
        ? "本地服务已断开。请重新启动 Creative Claw，刷新页面后再试。"
        : error.message.includes("CREATIVE_CLAW_LLM_API_KEY")
          ? "模型尚未连接。点击顶部模型状态，输入 API Key 后再试。"
          : `模型调用失败：${error.message}`;
      addMessage("assistant", hint);
      $("#chatStatus").textContent = "调用失败";
    } finally {
      $("#sendChat").disabled = false;
      $("#previewContext").disabled = false;
    }
  }

  async function runChatFromPreview() {
    const preview = state.lastContextPreview;
    if (!preview) {
      toast("上下文预览已失效，请重新预览");
      return;
    }
    await runChatRequest(preview.message, preview.scope);
  }

  async function sendChat() {
    if ($("#chatMode").value === "cold_start") {
      await generateColdStartPreview();
      return;
    }
    await prepareChatRequest();
  }

  async function createTask(goal, plan) {
    const created = await api("/v1/tasks", jsonOptions({ project_id: state.projectId, goal, plan }));
    let task = await api(`/v1/tasks/${created.id}/step`, jsonOptions({ run_until_blocked: true }));
    updateTask(task);
    return task;
  }

  async function approveFormTask(task) {
    if (task.status !== "awaiting_approval") return task;
    const approved = await api(`/v1/tasks/${encodeURIComponent(task.id)}/step`, jsonOptions({ approve: true }));
    updateTask(approved);
    return approved;
  }

  function updateTask(task) {
    const index = state.tasks.findIndex((item) => item.id === task.id);
    if (index >= 0) state.tasks[index] = task; else state.tasks.unshift(task);
    state.activeTask = task.status === "awaiting_approval" ? task : (state.activeTask?.id === task.id ? null : state.activeTask);
    renderTasks();
  }

  function renderTasks() {
    $("#taskCount").textContent = `${state.tasks.length} 项`;
    const list = $("#taskList");
    list.replaceChildren(...state.tasks.map((task) => {
      const item = document.createElement("div");
      item.className = `task-item ${task.status}`;
      const dot = document.createElement("i");
      const copy = document.createElement("div");
      const title = document.createElement("strong"); title.textContent = task.goal;
      const detail = document.createElement("span"); detail.textContent = task.status === "awaiting_approval" ? "等待批准" : task.status;
      copy.append(title, detail);
      const count = document.createElement("span"); count.textContent = `${task.cursor}/${task.plan?.length || 0}`;
      item.append(dot, copy, count);
      return item;
    }));
    const card = $("#approvalCard");
    card.hidden = !state.activeTask;
    if (state.activeTask) {
      const checkpoint = state.activeTask.checkpoint || {};
      $("#approvalDescription").textContent = `${checkpoint.tool || "写入工具"} 将修改项目文件或结构化知识。`;
    }
  }

  async function approveActiveTask() {
    if (!state.activeTask) return;
    const id = state.activeTask.id;
    try {
      let task = await api(`/v1/tasks/${id}/step`, jsonOptions({ approve: true }));
      if (!["completed", "failed", "rejected", "awaiting_approval"].includes(task.status)) {
        task = await api(`/v1/tasks/${id}/step`, jsonOptions({ run_until_blocked: true }));
      }
      updateTask(task);
      if (task.status === "completed") {
        if (id === state.pendingManuscriptTaskId && state.pendingManuscriptSceneId) {
          delete state.manuscriptDrafts[state.pendingManuscriptSceneId];
          delete state.manuscriptPatches[state.pendingManuscriptSceneId];
          state.pendingManuscriptSceneId = null;
          state.pendingManuscriptTaskId = null;
        }
        const path = task.result?.path;
        toast(path ? `已生成：${path}` : "写入完成，画布正在刷新");
        await loadSnapshot(false);
      } else if (task.status === "failed") toast(`任务失败：${task.checkpoint?.error || "未知错误"}`);
    } catch (error) { toast(error.message); }
  }

  async function rejectActiveTask() {
    if (!state.activeTask) return;
    const id = state.activeTask.id;
    try {
      const task = await api(`/v1/tasks/${id}/step`, jsonOptions({ reject: true, reason: "用户在叙事画布中拒绝" }));
      updateTask(task);
      if (id === state.pendingManuscriptTaskId) {
        state.pendingManuscriptSceneId = null;
        state.pendingManuscriptTaskId = null;
      }
      toast("已拒绝，未执行写入");
    } catch (error) { toast(error.message); }
  }

  function officePlan(type) {
    const timeline = state.snapshot.timeline.slice(0, 8);
    const ohlc = state.snapshot.ohlc;
    const stamp = new Date().toISOString().slice(0, 10);
    if (type === "word") return {
      goal: "根据项目知识生成 Word 创作圣经",
      plan: [
        { tool: "search_knowledge", args: { query: "项目正典 人物关系 时间线", top_k: 8, filters: { branch: state.branch } } },
        { tool: "export_word", args: { output_path: `canvas-output/创作圣经-${stamp}.docx`, title: `${state.snapshot.project.name} · 创作圣经`, sections: [
          { heading: "项目连续性规则", paragraphs: ["本文件由 Creative Claw 根据本地知识库生成。所有写入已通过用户审批。"] },
          { heading: "时间线", table: [["定位", "场景", "内容"], ...timeline.map((event) => [locatorFor(event), event.label, event.description])] },
          { heading: "人物状态", table: [["人物", "维度", "周期", "开", "高", "低", "收"], ...ohlc.map((row) => [row.character_name, row.dimension, row.period_id, row.open, row.high, row.low, row.close])] },
        ] } },
      ],
    };
    if (type === "powerpoint") return {
      goal: "根据项目知识生成 PowerPoint 提案",
      plan: [
        { tool: "search_knowledge", args: { query: "产品方法 人物K线 叙事连续性", top_k: 8, filters: { branch: state.branch } } },
        { tool: "export_powerpoint", args: { output_path: `canvas-output/叙事提案-${stamp}.pptx`, title: state.snapshot.project.name, subtitle: "Creative Claw 叙事画布导出", slides: [
          { title: "可引用的项目知识", bullets: [`${state.snapshot.stats.documents} 个来源`, `${state.snapshot.stats.chunks} 个引用块`, "正典与支线隔离"] },
          { title: "场景时间线", bullets: timeline.map((event) => `${locatorFor(event)} · ${event.label}`) },
          { title: "人物 K 线", bullets: ["场景级 OHLC 可手动调整", "集级使用首开、最高、最低、末收聚合", "数值与原文证据并存"] },
        ] } },
      ],
    };
    return {
      goal: "导出 Excel 人物 K 线与时间线",
      plan: [
        { tool: "search_knowledge", args: { query: "人物状态 OHLC 时间线", top_k: 6, filters: { branch: state.branch } } },
        { tool: "export_excel", args: { output_path: `canvas-output/人物K线-${stamp}.xlsx`, sheets: [
          { name: "人物OHLC", rows: [["人物", "维度", "周期", "类型", "上级周期", "开", "高", "低", "收"], ...ohlc.map((row) => [row.character_name, row.dimension, row.period_id, row.period_type, row.parent_period_id || "", row.open, row.high, row.low, row.close])] },
          { name: "时间线", rows: [["集", "场", "故事时间", "标题", "内容"], ...timeline.map((event) => [event.episode, event.scene, event.story_time || "", event.label, event.description])] },
        ] } },
      ],
    };
  }

  async function startOfficeTask(type) {
    switchTab("tasks");
    try {
      const spec = officePlan(type);
      const task = await createTask(spec.goal, spec.plan);
      if (task.status === "awaiting_approval") toast("任务已准备，等待你批准写入");
    } catch (error) { toast(error.message); }
  }

  async function saveOhlc() {
    if (!state.candleDraft) return;
    const { row, open, high, low, close } = state.candleDraft;
    try {
      let task = await createTask(
        `修改 ${row.character_name} ${row.period_id} 的 OHLC`,
        [{ tool: "upsert_ohlc", args: {
          character_name: row.character_name, dimension: row.dimension, period_type: row.period_type,
          period_id: row.period_id, parent_period_id: row.parent_period_id, sort_key: row.sort_key,
          open, high, low, close, timeline_event_id: row.timeline_event_id || null,
          branch: row.branch, attrs: row.attrs || {},
        } }],
      );
      task = await approveFormTask(task);
      if (task.status === "completed") {
        await loadSnapshot(false);
        toast("K 线修改已保存并重新聚合父周期");
      } else {
        switchTab("tasks");
        toast(`K 线任务状态：${task.status}`);
      }
    } catch (error) { toast(error.message); }
  }

  async function submitScene() {
    const episode = Number($("#sceneEpisode").value);
    const scene = Number($("#sceneNumber").value);
    const label = $("#sceneTitle").value.trim();
    const description = $("#sceneDescription").value.trim();
    if (!label || !description) return;
    const submit = $("#submitScene");
    submit.disabled = true;
    submit.textContent = "正在保存…";
    try {
      let task = await createTask(
        `添加场景 E${episode}-S${String(scene).padStart(2, "0")} ${label}`,
        [{ tool: "add_timeline_event", args: {
          label, description, episode, scene, story_time: $("#sceneStoryTime").value.trim(), branch: state.branch,
          attrs: { source: "ai-canvas", citations: state.sceneDraftCitations },
        } }],
      );
      task = await approveFormTask(task);
      state.sceneDraftCitations = [];
      if (task.status === "completed") {
        $("#sceneDialog").close();
        state.activeSceneId = null;
        await loadSnapshot(false);
        toast(`已保存场景：${label}`);
      } else {
        switchTab("tasks");
        toast(`场景任务状态：${task.status}`);
      }
    } catch (error) { toast(error.message); }
    finally {
      submit.disabled = false;
      submit.textContent = "保存到画布";
    }
  }

  function fillNewOhlcSceneDefaults(scene) {
    if (!scene) return;
    const episode = Number(scene?.episode) || 1;
    const sceneNumber = Number(scene?.scene) || 1;
    $("#newOhlcPeriod").value = `E${episode}-S${String(sceneNumber).padStart(2, "0")}`;
    $("#newOhlcParent").value = `E${episode}`;
    $("#newOhlcType").value = "scene";
    $("#newOhlcSort").value = String(episode + sceneNumber / 100);
  }

  function openNewOhlcDialog() {
    const timeline = state.snapshot?.timeline || [];
    if (!timeline.length) {
      toast("请先创建场景；场景级 K 线必须关联正文场景");
      openBlankSceneDialog();
      return;
    }
    const scene = activeScene() || timeline.at(-1);
    const character = (state.snapshot?.entities || []).find((item) => item.entity_type === "character");
    const picker = $("#newOhlcScene");
    picker.replaceChildren(...timeline.map((item) => {
      const option = document.createElement("option");
      option.value = item.id;
      option.textContent = `${locatorFor(item)} · ${item.label}`;
      return option;
    }));
    picker.value = scene.id;
    $("#newOhlcCharacter").value = character?.name || "主角";
    $("#newOhlcDimension").value = "知情度";
    fillNewOhlcSceneDefaults(scene);
    ["Open", "High", "Low", "Close"].forEach((field) => { $(`#newOhlc${field}`).value = "50"; });
    $("#ohlcDialog").showModal();
  }

  async function submitNewOhlc() {
    const values = {
      open: Number($("#newOhlcOpen").value), high: Number($("#newOhlcHigh").value),
      low: Number($("#newOhlcLow").value), close: Number($("#newOhlcClose").value),
    };
    if (values.high < Math.max(values.open, values.close) || values.low > Math.min(values.open, values.close)) {
      toast("OHLC 无效：最高/最低必须包住开盘和收盘");
      return;
    }
    try {
      let task = await createTask(
        `添加 ${$("#newOhlcCharacter").value.trim()} ${$("#newOhlcPeriod").value.trim()} K 线`,
        [{ tool: "upsert_ohlc", args: {
          character_name: $("#newOhlcCharacter").value.trim(), dimension: $("#newOhlcDimension").value.trim(),
          period_type: $("#newOhlcType").value.trim(), period_id: $("#newOhlcPeriod").value.trim(),
          parent_period_id: $("#newOhlcParent").value.trim() || null, sort_key: Number($("#newOhlcSort").value),
          ...values, timeline_event_id: $("#newOhlcScene").value,
          branch: state.branch, attrs: { source: "canvas-manual" },
        } }],
      );
      task = await approveFormTask(task);
      if (task.status === "completed") {
        $("#ohlcDialog").close();
        await loadSnapshot(false);
        toast("新 K 线已保存到画布");
      } else {
        switchTab("tasks");
        toast(`K 线任务状态：${task.status}`);
      }
    } catch (error) { toast(error.message); }
  }

  function openEntityDialog() {
    $("#newEntityName").value = "";
    $("#newEntityType").value = "character";
    $("#newEntityAliases").value = "";
    $("#newEntityDescription").value = "";
    $("#entityDialog").showModal();
  }

  async function submitEntity() {
    const name = $("#newEntityName").value.trim();
    if (!name) return;
    const entityType = $("#newEntityType").value;
    const aliases = $("#newEntityAliases").value
      .split(/[,，]/u)
      .map((item) => item.trim())
      .filter(Boolean);
    const description = $("#newEntityDescription").value.trim();
    const submit = $("#saveEntityButton");
    submit.disabled = true;
    submit.textContent = "正在保存…";
    try {
      let task = await createTask(
        `添加${entityType === "character" ? "人物" : "实体"} ${name}`,
        [{ tool: "upsert_entity", args: {
          name, entity_type: entityType, aliases,
          attrs: { description, source: "canvas-manual" },
        } }],
      );
      task = await approveFormTask(task);
      if (task.status === "completed") {
        $("#entityDialog").close();
        await loadSnapshot(false);
        toast(`已添加：${name}`);
      } else {
        switchTab("tasks");
        toast(`实体任务状态：${task.status}`);
      }
    } catch (error) { toast(error.message); }
    finally {
      submit.disabled = false;
      submit.textContent = "保存到项目";
    }
  }

  function openSourceDialog() {
    $("#newSourceTitle").value = "";
    $("#newSourceText").value = "";
    $("#newSourceCanon").value = "reference";
    $("#newSourceError").hidden = true;
    $("#sourceDialog").showModal();
  }

  async function submitSource() {
    const title = $("#newSourceTitle").value.trim();
    const text = $("#newSourceText").value;
    if (!title || !text.trim()) return;
    const button = $("#saveSourceButton");
    const errorNode = $("#newSourceError");
    button.disabled = true;
    button.textContent = "正在索引…";
    errorNode.hidden = true;
    try {
      const result = await api(`/v1/projects/${encodeURIComponent(state.projectId)}/sources/text`, jsonOptions({
        title,
        text,
        branch: state.branch,
        canon_status: $("#newSourceCanon").value,
      }));
      $("#sourceDialog").close();
      await loadSnapshot(false);
      selectNode(`source:${result.document_id}`, true);
      toast(`来源已创建 · ${result.chunk_count} 个可引用片段`);
    } catch (error) {
      errorNode.textContent = error.message;
      errorNode.hidden = false;
    } finally {
      button.disabled = false;
      button.textContent = "保存来源";
    }
  }

  async function uploadFile(file) {
    if (!file) return;
    const form = new FormData();
    form.append("file", file);
    form.append("branch", state.branch);
    form.append("canon_status", "reference");
    $("#uploadButton").disabled = true;
    try {
      const result = await api(`/v1/projects/${encodeURIComponent(state.projectId)}/documents/upload`, { method: "POST", body: form });
      toast(`已导入 ${file.name} · ${result.chunk_count} 个引用块`);
      await loadSnapshot(false);
    } catch (error) { toast(error.message); }
    finally { $("#uploadButton").disabled = false; $("#fileUpload").value = ""; }
  }

  function blueprintProjectPath(suffix) {
    return `/v1/projects/${encodeURIComponent(state.projectId)}${suffix}`;
  }

  function populateBlueprintDraftOptions() {
    const units = state.snapshot?.production_units || [];
    const artifacts = (state.snapshot?.artifacts || []).filter((item) => item.artifact_type === "manuscript");
    const unitSelect = $("#blueprintUnitSelect");
    const artifactSelect = $("#blueprintArtifactSelect");
    unitSelect.replaceChildren(...units.map((unit) => {
      const option = document.createElement("option");
      option.value = unit.id;
      option.textContent = `${unit.unit_type} · ${unit.title}`;
      return option;
    }));
    artifactSelect.replaceChildren(...artifacts.map((artifact) => {
      const option = document.createElement("option");
      option.value = artifact.id;
      option.dataset.currentVersionId = artifact.current_version_id || "";
      option.textContent = artifact.title;
      return option;
    }));
  }

  function openBlueprintLab() {
    populateBlueprintDraftOptions();
    $("#blueprintLabDialog").showModal();
  }

  function renderBlueprintProgress(job) {
    const progress = CreativeClawBlueprint.calculateProgress(job || {});
    const node = $("#blueprintJobProgress");
    $("i", node).style.width = `${progress.percent}%`;
    $("span", node).textContent = `${progress.status} · ${progress.completed}/${progress.total || "?"} · ${progress.percent}%`;
    $("#pauseBlueprintJob").disabled = !["pending", "running", "resumable"].includes(progress.status);
    $("#resumeBlueprintJob").disabled = !["paused", "resumable"].includes(progress.status);
    $("#cancelBlueprintJob").disabled = ["completed", "cancelled"].includes(progress.status);
  }

  function renderBlueprintNodes(container, nodes, editable) {
    container.replaceChildren();
    container.classList.toggle("empty", !(nodes || []).length);
    if (!(nodes || []).length) {
      container.textContent = "暂无蓝图节点";
      return;
    }
    (nodes || []).forEach((node) => {
      const row = document.createElement("div");
      row.className = "blueprint-node";
      row.dataset.nodeId = node.id;
      const kind = document.createElement("code");
      kind.textContent = node.node_type;
      const fields = document.createElement("div");
      const title = document.createElement("input");
      title.value = node.title || node.stable_key;
      title.readOnly = !editable;
      title.setAttribute("aria-label", `${node.stable_key} 标题`);
      const summary = document.createElement("textarea");
      summary.value = node.summary || node.title || node.stable_key;
      summary.readOnly = !editable;
      summary.setAttribute("aria-label", `${node.stable_key} 摘要`);
      const complete = document.createElement("small");
      complete.textContent = CreativeClawBlueprint.hasCompleteDimensions(node) ? "全维度完整" : "维度缺失";
      fields.append(title, summary);
      row.append(kind, fields, complete);
      container.append(row);
    });
  }

  function renderReferenceBlueprint(blueprint) {
    renderBlueprintNodes($("#referenceBlueprintTree"), blueprint?.nodes || [], true);
    const queue = $("#blueprintConflictQueue");
    const conflicts = CreativeClawBlueprint.filterConflicts(blueprint?.conflicts || []);
    const interpretations = blueprint?.interpretations || [];
    queue.replaceChildren();
    queue.classList.toggle("empty", !conflicts.length && !interpretations.length);
    if (!conflicts.length && !interpretations.length) queue.textContent = "暂无待确认冲突";
    interpretations.forEach((item) => {
      const row = document.createElement("div");
      row.className = "blueprint-interpretation";
      row.dataset.interpretationId = item.id;
      const value = document.createElement("span");
      value.textContent = `${item.dimension} · ${JSON.stringify(item.value)}`;
      const decision = document.createElement("select");
      decision.setAttribute("aria-label", `${item.dimension} 解释裁决`);
      [["pending", "待裁决"], ["confirmed", "确认"], ["rejected", "拒绝"]].forEach(([key, label]) => {
        const option = document.createElement("option"); option.value = key; option.textContent = label;
        decision.append(option);
      });
      decision.value = item.author_status || "pending";
      row.append(value, decision);
      queue.append(row);
    });
    conflicts.forEach((item) => {
      const row = document.createElement("div");
      row.className = "blueprint-conflict";
      row.dataset.conflictId = item.id;
      const label = document.createElement("span");
      label.textContent = `${item.relation_type} · ${item.interpretation_ids.length} 个解释 · ${item.status}`;
      const resolution = document.createElement("select");
      resolution.setAttribute("aria-label", "冲突裁决");
      const unresolved = document.createElement("option"); unresolved.value = ""; unresolved.textContent = "暂不裁决";
      resolution.append(unresolved);
      item.interpretation_ids.forEach((id, index) => {
        const option = document.createElement("option"); option.value = id; option.textContent = `选择解释 ${index + 1}`;
        resolution.append(option);
      });
      row.append(label, resolution);
      queue.append(row);
    });
    $("#saveReferenceBlueprint").disabled = !blueprint;
    $("#createTargetBlueprint").disabled = !blueprint;
  }

  function renderTargetBlueprint(blueprint) {
    renderBlueprintNodes($("#targetBlueprintTree"), blueprint?.nodes || [], false);
    const confirmed = blueprint?.artifact?.attrs?.confirmation_status === "confirmed";
    $("#confirmTargetBlueprint").disabled = !blueprint || confirmed;
    $("#generateUnitDraft").disabled = !confirmed || !$("#blueprintUnitSelect").value || !$("#blueprintArtifactSelect").value;
  }

  async function loadReferenceBlueprint(artifactId) {
    const blueprint = await api(blueprintProjectPath(`/reference-blueprints/${encodeURIComponent(artifactId)}?include_evidence=1`));
    state.blueprint.reference = blueprint;
    renderReferenceBlueprint(blueprint);
  }

  async function pollReferenceBlueprintJob() {
    const current = state.blueprint.job;
    if (!current) return;
    const job = await api(blueprintProjectPath(`/blueprint-jobs/${encodeURIComponent(current.id)}`));
    state.blueprint.job = job;
    renderBlueprintProgress(job);
    if (job.output_artifact_id) {
      await loadReferenceBlueprint(job.output_artifact_id);
      return;
    }
    if (["pending", "running", "resumable"].includes(job.status)) {
      state.blueprint.pollTimer = setTimeout(() => pollReferenceBlueprintJob().catch((error) => toast(error.message)), 600);
    }
  }

  async function startReferenceBlueprint() {
    const title = $("#referenceTitleInput").value.trim();
    const text = $("#referenceTextInput").value;
    if (!title || !text.trim()) { toast("请填写参考标题和正文"); return; }
    const button = $("#startReferenceBlueprint");
    button.disabled = true;
    try {
      const job = await api(blueprintProjectPath("/blueprint-jobs/reference"), jsonOptions({
        title,
        text,
        rights_basis: $("#referenceRightsBasis").value,
        run_async: $("#forceBackgroundBlueprint").checked || undefined,
      }));
      state.blueprint.job = job;
      renderBlueprintProgress(job);
      if (job.output_artifact_id) await loadReferenceBlueprint(job.output_artifact_id);
      else await pollReferenceBlueprintJob();
      toast("参考机制抽取任务已启动");
    } catch (error) { toast(error.message); }
    finally { button.disabled = false; }
  }

  async function controlBlueprintJob(action) {
    const job = state.blueprint.job;
    if (!job) return;
    const result = await api(blueprintProjectPath(`/blueprint-jobs/${encodeURIComponent(job.id)}/${action}`), jsonOptions({}));
    state.blueprint.job = result;
    renderBlueprintProgress(result);
    if (action === "resume") pollReferenceBlueprintJob().catch((error) => toast(error.message));
  }

  async function saveReferenceBlueprint() {
    const blueprint = state.blueprint.reference;
    if (!blueprint) return;
    const edits = Object.fromEntries($$(".blueprint-node", $("#referenceBlueprintTree")).map((row) => [
      row.dataset.nodeId, { title: $("input", row).value, summary: $("textarea", row).value },
    ]));
    const nodes = blueprint.nodes.map((node) => ({ ...node, ...(edits[node.id] || {}) }));
    const interpretationDecisions = CreativeClawBlueprint.buildInterpretationDecisions(
      $$(".blueprint-interpretation", $("#blueprintConflictQueue")).map((row) => ({
        id: row.dataset.interpretationId, decision: $("select", row).value,
      })),
    );
    const conflictResolutions = Object.fromEntries(
      $$(".blueprint-conflict", $("#blueprintConflictQueue")).map((row) => {
        const selected = $("select", row).value;
        return [row.dataset.conflictId, selected
          ? { status: "resolved", resolution: { selected_interpretation_id: selected } }
          : { status: "pending_author", resolution: {} }];
      }),
    );
    const result = await api(blueprintProjectPath(`/reference-blueprints/${encodeURIComponent(blueprint.artifact.id)}/versions`), jsonOptions({
      nodes,
      expected_current_version_id: blueprint.version.id,
      change_summary: "作者在蓝图实验室确认并编辑机制",
      interpretation_decisions: interpretationDecisions,
      conflict_resolutions: conflictResolutions,
    }));
    await loadReferenceBlueprint(result.artifact.id);
    toast("参考蓝图新版本已保存");
  }

  async function createTargetBlueprint() {
    const reference = state.blueprint.reference;
    const text = $("#targetSettingInput").value.trim();
    if (!reference || !text) { toast("请先完成参考蓝图并填写新作品设定"); return; }
    const button = $("#createTargetBlueprint");
    button.disabled = true;
    try {
      const setting = await api(blueprintProjectPath("/target-settings"), jsonOptions({ text }));
      state.blueprint.setting = setting;
      renderTargetSetting(setting);
      toast("结构化设定已生成，请编辑并显式确认");
    } catch (error) { toast(error.message); }
    finally { button.disabled = false; }
  }

  function renderTargetSetting(setting) {
    const container = $("#targetSettingFields");
    container.replaceChildren();
    const structured = setting?.structured;
    container.classList.toggle("empty", !structured);
    if (!structured) { container.textContent = "等待结构化设定"; return; }
    Object.entries(CreativeClawBlueprint.normalizeStructuredSetting(structured)).forEach(([field, value]) => {
      const label = document.createElement("label"); label.textContent = field;
      const input = document.createElement("textarea");
      input.dataset.settingField = field;
      input.dataset.jsonValue = typeof value === "string" ? "0" : "1";
      input.value = typeof value === "string" ? value : JSON.stringify(value, null, 2);
      input.readOnly = CreativeClawBlueprint.canMigrateSetting(setting);
      label.append(input); container.append(label);
    });
    const confirmed = CreativeClawBlueprint.canMigrateSetting(setting);
    $("#confirmTargetSetting").disabled = confirmed;
    $("#migrateTargetBlueprint").disabled = !confirmed;
    $("#createManualTargetBlueprint").disabled = !confirmed || !state.blueprint.reference;
  }

  function readTargetSettingFields() {
    return CreativeClawBlueprint.normalizeStructuredSetting(Object.fromEntries(
      $$('[data-setting-field]', $("#targetSettingFields")).map((input) => {
        if (input.dataset.jsonValue === "0") return [input.dataset.settingField, input.value];
        try { return [input.dataset.settingField, JSON.parse(input.value)]; }
        catch (_error) { throw new Error(`${input.dataset.settingField} 必须是有效 JSON`); }
      }),
    ));
  }

  async function confirmTargetSetting() {
    const setting = state.blueprint.setting;
    if (!setting) return;
    const confirmed = await api(blueprintProjectPath(`/target-settings/${encodeURIComponent(setting.artifact.id)}/confirm`), jsonOptions({
      expected_current_version_id: setting.version.id,
      structured: readTargetSettingFields(),
    }));
    state.blueprint.setting = confirmed;
    renderTargetSetting(confirmed);
    toast("结构化设定已由作者确认");
  }

  async function migrateTargetBlueprint() {
    const reference = state.blueprint.reference;
    const setting = state.blueprint.setting;
    if (!reference || !CreativeClawBlueprint.canMigrateSetting(setting)) return;
    const migration = await api(blueprintProjectPath("/blueprint-jobs/migration"), jsonOptions({
      reference_blueprint_id: reference.artifact.id,
      target_setting_id: setting.artifact.id,
    }));
    const target = await api(blueprintProjectPath(`/target-blueprints/${encodeURIComponent(migration.output_artifact_id)}`));
    state.blueprint.migration = migration;
    state.blueprint.target = target;
    renderTargetBlueprint(target);
    toast("新作品生产蓝图已生成，等待确认");
  }

  function emptyManualNode(title) {
    const dimensions = Object.fromEntries(CreativeClawBlueprint.BLUEPRINT_DIMENSIONS.map((name) => [
      name, { state: "not_observed", value: null, confidence: 1, evidence_refs: [] },
    ]));
    return { stable_key: "work", node_type: "work", title, summary: "作者手工维护", dimensions };
  }

  async function createManualReferenceBlueprint() {
    const title = $("#referenceTitleInput").value.trim() || "手工参考蓝图";
    const blueprint = await api(blueprintProjectPath("/reference-blueprints/manual"), jsonOptions({
      title, nodes: [emptyManualNode(title)],
    }));
    state.blueprint.reference = blueprint;
    renderReferenceBlueprint(blueprint);
    toast("已创建无模型手工参考蓝图");
  }

  async function createManualTargetBlueprint() {
    const reference = state.blueprint.reference;
    const setting = state.blueprint.setting;
    if (!reference || !CreativeClawBlueprint.canMigrateSetting(setting)) return;
    const target = await api(blueprintProjectPath("/target-blueprints/manual"), jsonOptions({
      title: "手工目标蓝图", target_setting_id: setting.artifact.id,
      reference_blueprint_id: reference.artifact.id,
      nodes: [emptyManualNode("手工目标蓝图")],
    }));
    state.blueprint.target = target;
    renderTargetBlueprint(target);
    toast("已创建无模型手工目标蓝图");
  }

  async function confirmTargetBlueprint() {
    const target = state.blueprint.target;
    if (!target) return;
    const confirmed = await api(blueprintProjectPath(`/target-blueprints/${encodeURIComponent(target.artifact.id)}/confirm`), jsonOptions({
      expected_current_version_id: target.version.id,
    }));
    state.blueprint.target = confirmed;
    renderTargetBlueprint(confirmed);
    toast("目标蓝图已确认，可以逐单元生成草稿");
  }

  function renderSimilarity(candidate) {
    const report = $("#similarityReport");
    const similarity = candidate?.similarity;
    report.replaceChildren();
    report.classList.toggle("empty", !similarity);
    if (!similarity) { report.textContent = "尚无相似度报告"; return; }
    const badge = document.createElement("span");
    badge.className = `risk-badge ${similarity.gate_status}`;
    badge.textContent = similarity.gate_status;
    const details = document.createElement("dl");
    const rows = [
      ["表达复制", similarity.expression?.blocked ? "硬阻止" : "未命中"],
      ["结构一一对应", similarity.structure?.high_structural_risk ? "需要整改" : "未命中"],
      ["抽象机制", similarity.mechanism?.allowed ? "允许" : "待复核"],
      ["发现", `${(similarity.findings || []).length} 项`],
    ];
    rows.forEach(([label, value]) => {
      const dt = document.createElement("dt"); dt.textContent = label;
      const dd = document.createElement("dd"); dd.textContent = value;
      details.append(dt, dd);
    });
    report.append(badge, details);
  }

  async function generateUnitDraft() {
    const target = state.blueprint.target;
    if (!target) return;
    const request = CreativeClawBlueprint.buildDraftRequest({
      target_blueprint_id: target.artifact.id,
      unit_id: $("#blueprintUnitSelect").value,
      artifact_id: $("#blueprintArtifactSelect").value,
    });
    const candidate = await api(blueprintProjectPath("/draft-candidates"), jsonOptions(request));
    state.blueprint.candidate = candidate;
    $("#draftCandidateText").value = candidate.candidate_text;
    renderSimilarity(candidate);
    $("#acceptDraftCandidate").disabled = !CreativeClawBlueprint.canAcceptCandidate(candidate);
    $("#rejectDraftCandidate").disabled = false;
    toast(candidate.status === "passed" ? "候选已通过门禁" : `候选状态：${candidate.status}`);
  }

  async function acceptDraftCandidate() {
    const candidate = state.blueprint.candidate;
    if (!CreativeClawBlueprint.canAcceptCandidate(candidate)) return;
    const selected = $("#blueprintArtifactSelect").selectedOptions[0];
    const result = await api(blueprintProjectPath(`/draft-candidates/${encodeURIComponent(candidate.id)}/accept`), jsonOptions({
      expected_current_version_id: selected?.dataset.currentVersionId || null,
    }));
    state.blueprint.candidate = result;
    $("#acceptDraftCandidate").disabled = true;
    $("#rejectDraftCandidate").disabled = true;
    await loadSnapshot(false);
    populateBlueprintDraftOptions();
    toast("候选已接受并创建正式稿版本");
  }

  async function rejectDraftCandidate() {
    const candidate = state.blueprint.candidate;
    if (!candidate) return;
    const reason = window.prompt("拒绝原因", "不符合当前单元目标") || "作者拒绝";
    const result = await api(blueprintProjectPath(`/draft-candidates/${encodeURIComponent(candidate.id)}/reject`), jsonOptions({ reason }));
    state.blueprint.candidate = result;
    $("#acceptDraftCandidate").disabled = true;
    $("#rejectDraftCandidate").disabled = true;
    toast("候选已拒绝，正式稿未改变");
  }

  function bindControls() {
    $("#projectSelect").addEventListener("change", async (event) => {
      resetProjectState(event.target.value);
      await loadSnapshot(true);
    });
    $("#refreshButton").addEventListener("click", () => loadSnapshot(false).catch((error) => toast(error.message)));
    $("#searchForm").addEventListener("submit", (event) => { event.preventDefault(); searchKnowledge($("#knowledgeQuery").value); });
    $("#inspectEvidence").addEventListener("click", () => {
      const node = state.nodes.find((item) => item.id === state.selectedNodeId);
      if (node) searchKnowledge(`${node.title} ${node.description}`);
    });
    $$(".right-tab").forEach((button) => button.addEventListener("click", () => switchTab(button.dataset.tab)));
    $$("[data-mode]").forEach((button) => button.addEventListener("click", () => {
      state.canvasMode = button.dataset.mode;
      $$("[data-mode]").forEach((item) => { const active = item === button; item.classList.toggle("active", active); item.setAttribute("aria-pressed", String(active)); });
      $("#canvasViewport").classList.toggle("pan-mode", state.canvasMode === "pan");
    }));
    $("#zoomIn").addEventListener("click", () => { state.zoom = clamp(state.zoom + .1, .35, 1.6); applyTransform(); });
    $("#zoomOut").addEventListener("click", () => { state.zoom = clamp(state.zoom - .1, .35, 1.6); applyTransform(); });
    $("#resetView").addEventListener("click", fitCanvas);
    $("#canvasViewport").addEventListener("pointerdown", (event) => {
      if (state.canvasMode !== "pan" || event.button !== 0 || event.target.closest(".canvas-node, .canvas-board")) return;
      state.panDrag = { pointerId: event.pointerId, startX: event.clientX, startY: event.clientY, x: state.pan.x, y: state.pan.y };
      $("#canvasViewport").classList.add("is-panning");
    });
    $("#canvasViewport").addEventListener("wheel", (event) => {
      event.preventDefault();
      const rect = $("#canvasViewport").getBoundingClientRect();
      const px = event.clientX - rect.left; const py = event.clientY - rect.top;
      const worldX = (px - state.pan.x) / state.zoom; const worldY = (py - state.pan.y) / state.zoom;
      const next = clamp(state.zoom * (event.deltaY > 0 ? .9 : 1.1), .35, 1.6);
      state.pan.x = px - worldX * next; state.pan.y = py - worldY * next; state.zoom = next; applyTransform();
    }, { passive: false });
    window.addEventListener("pointermove", movePointer);
    window.addEventListener("pointerup", endPointer);
    window.addEventListener("pointercancel", endPointer);
    $$("[data-board-handle]").forEach((handle) => handle.addEventListener("pointerdown", startBoardDrag));
    $("#sendChat").addEventListener("click", sendChat);
    $("#chatMode").addEventListener("change", syncChatMode);
    $("#previewContext").addEventListener("click", prepareChatRequest);
    $("#runChatFromPreview").addEventListener("click", runChatFromPreview);
    $("#renameProjectButton").addEventListener("click", () => {
      $("#projectName").value = state.snapshot?.project?.name || "";
      $("#projectDialog").showModal();
    });
    $("#newProjectButton").addEventListener("click", openNewProjectDialog);
    $("#newProjectForm").addEventListener("submit", (event) => {
      event.preventDefault();
      if (event.submitter?.value === "cancel") { $("#newProjectDialog").close(); return; }
      createProject();
    });
    $("#projectForm").addEventListener("submit", (event) => {
      event.preventDefault();
      if (event.submitter?.value === "cancel") { $("#projectDialog").close(); return; }
      renameProject();
    });
    $("#modelStatus").addEventListener("click", () => {
      if (state.config) openModelDialog();
      else toast("本地服务已断开，请重新启动后刷新页面");
    });
    $("#modelForm").addEventListener("submit", (event) => {
      event.preventDefault();
      if (event.submitter?.value === "cancel") { $("#modelDialog").close(); return; }
      saveModelConfig();
    });
    $("#chatInput").addEventListener("keydown", (event) => { if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) sendChat(); });
    $$(".office-action").forEach((button) => button.addEventListener("click", () => startOfficeTask(button.dataset.office)));
    $("#approveTask").addEventListener("click", approveActiveTask);
    $("#rejectTask").addEventListener("click", rejectActiveTask);
    $("#saveOhlc").addEventListener("click", saveOhlc);
    $("#addOhlcButton").addEventListener("click", openNewOhlcDialog);
    $("#newOhlcScene").addEventListener("change", (event) => {
      const scene = (state.snapshot?.timeline || []).find((item) => item.id === event.target.value);
      fillNewOhlcSceneDefaults(scene);
    });
    $("#refreshLedgerButton").addEventListener("click", refreshLedger);
    $("#manuscriptSceneSelect").addEventListener("change", (event) => selectNode(`scene:${event.target.value}`, false));
    $("#manuscriptText").addEventListener("input", updateManuscriptDraft);
    $("#manuscriptText").addEventListener("select", captureManuscriptSelection);
    $("#selectParagraph").addEventListener("click", selectCurrentParagraph);
    $("#manualSelectionPatch").addEventListener("click", startManualSelectionPatch);
    $("#runSelectionRewrite").addEventListener("click", runSelectionRewrite);
    $("#selectionInstruction").addEventListener("keydown", (event) => { if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) runSelectionRewrite(); });
    $("#selectionReplacement").addEventListener("input", updateSelectionApplyState);
    $("#cancelSelectionPatch").addEventListener("click", cancelSelectionPatch);
    $("#applySelectionPatch").addEventListener("click", applySelectionPatch);
    $("#revertManuscript").addEventListener("click", revertManuscript);
    $("#saveManuscript").addEventListener("click", saveManuscript);
    $("#ohlcForm").addEventListener("submit", (event) => {
      event.preventDefault();
      if (event.submitter?.value === "cancel") { $("#ohlcDialog").close(); return; }
      submitNewOhlc();
    });
    $$("[data-ohlc]").forEach((input) => input.addEventListener("change", () => updateDraft(input.dataset.ohlc, input.value)));
    $("#newSceneButton").addEventListener("click", openBlankSceneDialog);
    $("#canvasNewScene").addEventListener("click", openBlankSceneDialog);
    $("#startFirstScene").addEventListener("click", openBlankSceneDialog);
    $("#addEntityButton").addEventListener("click", openEntityDialog);
    $("#canvasNewEntity").addEventListener("click", openEntityDialog);
    $("#startEntity").addEventListener("click", openEntityDialog);
    $("#canvasNewSource").addEventListener("click", openSourceDialog);
    $("#startImport").addEventListener("click", () => $("#fileUpload").click());
    $("#sceneForm").addEventListener("submit", (event) => {
      event.preventDefault();
      if (event.submitter?.value === "cancel") { $("#sceneDialog").close(); return; }
      submitScene();
    });
    $("#entityForm").addEventListener("submit", (event) => {
      event.preventDefault();
      if (event.submitter?.value === "cancel") { $("#entityDialog").close(); return; }
      submitEntity();
    });
    $("#sourceForm").addEventListener("submit", (event) => {
      event.preventDefault();
      if (event.submitter?.value === "cancel") { $("#sourceDialog").close(); return; }
      submitSource();
    });
    $("#sourceUploadInstead").addEventListener("click", () => {
      $("#sourceDialog").close();
      $("#fileUpload").click();
    });
    $("#uploadButton").addEventListener("click", () => $("#fileUpload").click());
    $("#fileUpload").addEventListener("change", (event) => uploadFile(event.target.files[0]));
    $("#blueprintLabButton").addEventListener("click", openBlueprintLab);
    $("#closeBlueprintLab").addEventListener("click", () => $("#blueprintLabDialog").close());
    $("#startReferenceBlueprint").addEventListener("click", startReferenceBlueprint);
    $("#createManualReferenceBlueprint").addEventListener("click", () => createManualReferenceBlueprint().catch((error) => toast(error.message)));
    $("#pauseBlueprintJob").addEventListener("click", () => controlBlueprintJob("pause").catch((error) => toast(error.message)));
    $("#resumeBlueprintJob").addEventListener("click", () => controlBlueprintJob("resume").catch((error) => toast(error.message)));
    $("#cancelBlueprintJob").addEventListener("click", () => controlBlueprintJob("cancel").catch((error) => toast(error.message)));
    $("#saveReferenceBlueprint").addEventListener("click", () => saveReferenceBlueprint().catch((error) => toast(error.message)));
    $("#createTargetBlueprint").addEventListener("click", createTargetBlueprint);
    $("#confirmTargetSetting").addEventListener("click", () => confirmTargetSetting().catch((error) => toast(error.message)));
    $("#migrateTargetBlueprint").addEventListener("click", () => migrateTargetBlueprint().catch((error) => toast(error.message)));
    $("#createManualTargetBlueprint").addEventListener("click", () => createManualTargetBlueprint().catch((error) => toast(error.message)));
    $("#confirmTargetBlueprint").addEventListener("click", () => confirmTargetBlueprint().catch((error) => toast(error.message)));
    $("#generateUnitDraft").addEventListener("click", () => generateUnitDraft().catch((error) => toast(error.message)));
    $("#acceptDraftCandidate").addEventListener("click", () => acceptDraftCandidate().catch((error) => toast(error.message)));
    $("#rejectDraftCandidate").addEventListener("click", () => rejectDraftCandidate().catch((error) => toast(error.message)));
    $("#blueprintUnitSelect").addEventListener("change", () => renderTargetBlueprint(state.blueprint.target));
    $("#blueprintArtifactSelect").addEventListener("change", () => renderTargetBlueprint(state.blueprint.target));
    window.addEventListener("resize", () => { if (window.innerWidth < 900) applyTransform(); });
  }

  document.addEventListener("DOMContentLoaded", initialize);
})();
