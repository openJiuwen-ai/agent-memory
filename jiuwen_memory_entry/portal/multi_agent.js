/* Multi-Agent role memory workspace embedded in the existing Portal shell. */
(() => {
  "use strict";

  let section = document.getElementById("multi-agent");
  if (!section) {
    section = document.createElement("section");
    section.id = "multi-agent";
    section.hidden = true;
    section.setAttribute("role", "tabpanel");
    section.setAttribute("aria-labelledby", "tab-multiAgent");
    const settings = document.getElementById("settings");
    settings?.parentElement?.insertBefore(section, settings);
  }

  const iconPaths = {
    search: '<circle cx="10" cy="10" r="6"/><path d="m15 15 5 5"/>',
    folder: '<path d="M3 6h7l2 2h9v11H3z"/>',
    hash: '<path d="M5 9h14M4 15h14M9 4 7 20M17 4l-2 16"/>',
    history: '<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5M12 7v5l3 2"/>',
    network: '<circle cx="5" cy="12" r="2"/><circle cx="19" cy="6" r="2"/><circle cx="19" cy="18" r="2"/><path d="M7 11.2 17 6.8M7 12.8l10 4.4"/>',
    file: '<path d="M6 3h8l4 4v14H6z"/><path d="M14 3v5h5M9 13h6M9 17h6"/>',
    alert: '<path d="M12 3 2.5 20h19z"/><path d="M12 9v5M12 17h.01"/>',
    x: '<path d="m6 6 12 12M18 6 6 18"/>',
    branch: '<circle cx="6" cy="5" r="2"/><circle cx="18" cy="7" r="2"/><circle cx="18" cy="18" r="2"/><path d="M8 5h4a4 4 0 0 1 4 4v7M6 7v11h9"/>',
    copy: '<rect x="8" y="8" width="11" height="12" rx="1"/><path d="M16 8V4H5v12h3"/>',
    filter: '<path d="M3 5h18l-7 8v6l-4 2v-8z"/>',
    close: '<path d="m6 6 12 12M18 6 6 18"/>',
  };
  const icon = name => `<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">${iconPaths[name] || iconPaths.file}</svg>`;
  const esc = value => String(value ?? "").replace(/[&<>"']/g, char => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[char]));
  const tr = (zh, en) => language === "en" ? en : zh;
  const nowDate = value => {
    if (!value) return tr("时间未记录", "Time not recorded");
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return tr("时间未记录", "Time not recorded");
    return new Intl.DateTimeFormat(language === "en" ? "en" : "zh-CN", {
      timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", hour12: false,
    }).format(date);
  };
  const blank = value => typeof value === "string" && value.trim() ? value.trim() : "";
  const scopeKey = scope => [scope?.org, scope?.space, scope?.user, scope?.agent, scope?.session]
    .map(value => value || "").join("\u0000");
  const recordKey = (scope, id) => scopeKey(scope) + "\u0000" + id;
  const safeScope = scope => ({
    org: scope?.org || "", space: scope?.space || "", user: scope?.user || "",
    agent: scope?.agent || "", session: scope?.session || "",
  });
  const unique = values => [...new Set(values.filter(Boolean))];
  const textOf = item => blank(item.abstract) || blank(item.overview) || blank(item.content)
    || tr("正文未随结果返回", "Body was not returned");
  const firstLine = value => blank(String(value || "").split(/\r?\n/).find(line => line.trim()))
    || tr("无正文记忆", "Memory without text");
  const recordBodyHtml = item => {
    const text = textOf(item).trim();
    const title = firstLine(text);
    const body = text.slice(title.length).trim();
    return `<strong class="ma-record-title">${esc(title)}</strong>${body ? `<span class="ma-record-text">${esc(body)}</span>` : ""}`;
  };

  const state = {
    ready: false, capability: null, page: "search", duty: "general", compare: false,
    dutyMap: new Map(), relationMap: new Map(), compareDuties: [], policyVersion: null,
    catalogError: "",
    task: null, taskResult: null, taskError: "", taskTab: "background",
    taskSourceSelection: null, taskSourceResult: null,
    anchor: null, roleScope: "project", roleResults: {}, roleError: "",
    roleErrors: {}, rolePending: new Set(), catalogStale: false,
    units: new Map(), readFailures: new Set(), staleRefs: new Set(), refLoading: new Set(),
    hydrationQueue: [], hydrationPending: new Map(), hydrationActive: 0,
    records: new Map(), bindingSeq: 0, searchToken: 0, taskToken: 0,
    roleRequests: createRoleRequestLedger(),
    taskSourceToken: 0, detailToken: 0, traces: [], pending: 0, searchResult: null,
    lookupToken: 0, taskPlatform: "", taskQuery: "",
    searchSelection: null, detailUnit: null, detailItem: null, detailReference: null,
    detailPaths: [], detailError: "", gaps: [], toastTimer: null,
  };

  function createRoleRequestLedger() {
    let generation = 0;
    const tokens = new Map();
    return {
      invalidate() {
        generation++;
        tokens.clear();
      },
      begin(duties) {
        const requestGeneration = generation;
        const requestTokens = new Map();
        duties.forEach(duty => {
          const token = (tokens.get(duty) || 0) + 1;
          tokens.set(duty, token);
          requestTokens.set(duty, token);
        });
        return duty => requestGeneration === generation
          && requestTokens.get(duty) === tokens.get(duty);
      },
    };
  }

  function invalidateRoleRequests() {
    state.roleRequests.invalidate();
  }

  section.innerHTML = `
    <div class="ma-heading">
      <div>
        <div class="ma-eyebrow" id="ma-project"></div>
        <h1 id="ma-title"></h1>
        <p id="ma-description"></p>
      </div>
      <div class="ma-heading-actions">
        <button type="button" class="quiet small" id="ma-refresh-capability"></button>
        <details class="ma-diagnostics"><summary id="ma-diagnostics-label"></summary>
          <button type="button" class="quiet small" id="ma-traces-open"></button>
          <details class="ma-policy" id="ma-policy-details" hidden><summary id="ma-policy-summary"></summary><div id="ma-policy-body"></div></details>
        </details>
      </div>
    </div>
    <div class="ma-tabs" role="group" aria-label="多 Agent 角色记忆视图">
      <button type="button" data-ma-page="search" aria-pressed="true"></button>
      <button type="button" data-ma-page="task" aria-pressed="false"></button>
      <button type="button" data-ma-page="role" aria-pressed="false"></button>
    </div>
    <div id="ma-unavailable" hidden></div>
    <div id="ma-search-page">
      <form class="ma-panel" id="ma-search-form">
        <div class="ma-form-line ma-search-line">
          <select id="ma-search-platform" aria-label="来源平台" class="ma-platform-select"></select>
          <input id="ma-search-query" type="search" autocomplete="off">
          <button class="primary" type="submit"></button>
        </div>
        <div class="ma-controls">
          <label class="ma-check"><input id="ma-known-platforms" type="checkbox"><span></span></label>
          <span class="ma-page-note" id="ma-search-note"></span>
        </div>
      </form>
      <div id="ma-search-error"></div>
      <div id="ma-search-results"></div>
    </div>
    <div id="ma-task-page" hidden></div>
    <div id="ma-role-page" hidden>
      <form class="ma-panel" id="ma-role-form">
        <div class="ma-scope">
          <div><label id="ma-role-scope-label" for="ma-role-scope"></label><select id="ma-role-scope"><option value="project"></option><option value="task"></option></select></div>
          <div><label id="ma-duty-label" for="ma-duty"></label><select id="ma-duty"></select></div>
          <div><label id="ma-role-query-label" for="ma-role-query"></label><input id="ma-role-query" type="search" autocomplete="off"></div>
          <button class="primary" type="submit"></button>
        </div>
        <div class="ma-controls">
          <label class="ma-check"><input id="ma-compare" type="checkbox"><span></span></label>
          <label class="ma-result-limit" id="ma-limit-label"><span></span><select id="ma-role-limit"><option value="10">10</option><option value="20" selected>20</option><option value="50">50</option></select></label>
        </div>
        <div class="ma-compare-picker" id="ma-compare-duties" hidden></div>
        <div class="ma-catalog-note" id="ma-catalog-note" role="status"></div>
      </form>
      <div id="ma-role-error"></div>
      <div id="ma-role-results"></div>
    </div>
    <div class="ma-toast" id="ma-toast" hidden role="status"></div>
  `;

  const dialogs = document.createElement("div");
  dialogs.id = "multi-agent-dialogs";
  dialogs.innerHTML = `
    <dialog id="ma-lookup-dialog" class="ma-dialog" aria-labelledby="ma-lookup-title">
      <div class="ma-dialog-head"><div><h2 id="ma-lookup-title"></h2><p class="ma-page-note" id="ma-lookup-hint"></p></div><button type="button" class="quiet" data-ma-close="ma-lookup-dialog" aria-label="关闭">${icon("x")}</button></div>
      <div class="ma-dialog-body">
        <div class="ma-lookup-tabs" role="tablist"><button type="button" role="tab" data-ma-lookup="query" aria-selected="true"></button><button type="button" role="tab" data-ma-lookup="id" aria-selected="false"></button></div>
        <form id="ma-lookup-form"><div class="ma-form-line"><input id="ma-lookup-query" type="search" autocomplete="off"><button class="primary" type="submit"></button></div></form>
        <div id="ma-lookup-results"></div>
      </div>
    </dialog>
    <dialog id="multi-agent-detail-dialog" class="detail-drawer" aria-labelledby="ma-detail-title-label">
      <div class="dialog-header"><div><h2 id="ma-detail-title-label"></h2><p class="muted" id="ma-detail-context"></p></div><button type="button" class="quiet" data-ma-close="multi-agent-detail-dialog" aria-label="关闭">×</button></div>
      <div class="detail-pane" id="ma-detail-body" aria-live="polite"></div>
    </dialog>
    <dialog id="ma-gaps-dialog" class="ma-dialog" aria-labelledby="ma-gaps-title"><div class="ma-dialog-head"><div><h2 id="ma-gaps-title"></h2></div><button type="button" class="quiet" data-ma-close="ma-gaps-dialog" aria-label="关闭">${icon("x")}</button></div><div class="ma-dialog-body" id="ma-gaps-body"></div></dialog>
    <dialog id="ma-traces-dialog" class="ma-dialog" aria-labelledby="ma-traces-title"><div class="ma-dialog-head"><div><h2 id="ma-traces-title"></h2><p class="ma-page-note" id="ma-traces-hint"></p></div><button type="button" class="quiet" data-ma-close="ma-traces-dialog" aria-label="关闭">${icon("x")}</button></div><div class="ma-dialog-body" id="ma-traces-body"></div></dialog>
  `;
  section.append(dialogs);

  const $ma = id => document.getElementById(id);
  const activePage = () => state.page;
  const connectorLabel = connector => {
    if (!connector) return "";
    const configured = state.capability?.connectors?.find(item => item.id === connector);
    return configured?.label || connector;
  };
  const connectorIdForText = value => {
    const normalized = value.trim().toLowerCase();
    if (!normalized) return "";
    const choices = (state.capability?.connectors || []).filter(item =>
      item.id.toLowerCase() === normalized || item.label.toLowerCase() === normalized);
    const ids = new Set(choices.map(item => item.id));
    return ids.size === 1 ? [...ids][0] : "";
  };
  const referenceForItem = (item, scope) => {
    const evidence = item.view?.evidence_refs?.find(ref => ref.unit_id === item.unit_id);
    return evidence || {scope: safeScope(scope), unit_id: item.unit_id};
  };
  const sourceLabelForRef = (ref, fallbackMetadata = null, pending = false) => {
    const unit = state.units.get(recordKey(ref.scope, ref.unit_id));
    const metadata = unit?.system_metadata || fallbackMetadata || {};
    const connector = metadata.continuity_connector;
    if (connector) return connectorLabel(connector);
    const key = recordKey(ref.scope, ref.unit_id);
    if (state.readFailures.has(key)) return tr("来源读取失败", "Source lookup failed");
    if (state.refLoading.has(key)) return tr("来源读取中", "Reading source");
    if (pending) return tr("待读取", "Pending");
    return tr("未登记来源平台", "Source platform not recorded");
  };
  const sourceLabel = (item, scope) => sourceLabelForRef(
    referenceForItem(item, scope), item.system_metadata || {},
  );
  const sourceMeta = (item, scope) => {
    const ref = referenceForItem(item, scope);
    return state.units.get(recordKey(ref.scope, item.unit_id))?.system_metadata
      || item.system_metadata || {};
  };
  const unitFor = (item, scope) => state.units.get(recordKey(referenceForItem(item, scope).scope, item.unit_id));
  const isRisk = item => item.view?.kind === "risk";
  const sourceHtml = (item, scope) => `<span class="ma-source">${icon("network")}${tr("来源", "Source")}：${esc(sourceLabel(item, scope))}</span>`;

  function notice(message, error = false) {
    const target = activePage() === "task" ? $ma("ma-task-page") : activePage() === "role" ? $ma("ma-role-error") : $ma("ma-search-error");
    if (!target) return;
    target.innerHTML = message ? `<div class="${error ? "ma-error" : "ma-page-note"}">${esc(message)}</div>` : "";
  }
  function toast(message) {
    const node = $ma("ma-toast");
    node.textContent = message;
    node.hidden = false;
    clearTimeout(state.toastTimer);
    state.toastTimer = setTimeout(() => { node.hidden = true; }, 3200);
  }
  function errorMessage(error) {
    return error?.localizedMessage ? messageText(error.localizedMessage) : (error?.message || tr("请求失败", "Request failed"));
  }
  function messageText(message) {
    if (typeof message === "function") return message();
    return String(message ?? "");
  }
  function payloadSummary(payload) {
    const result = { fields: Object.keys(payload || {}).sort() };
    if (payload?.request?.operation) result.operation = payload.request.operation;
    if (payload?.duty) result.duty = payload.duty;
    if (payload?.filters) result.filter_fields = Object.keys(payload.filters).sort();
    if (payload?.offset !== undefined) result.offset = payload.offset;
    if (payload?.limit !== undefined) result.limit = payload.limit;
    return result;
  }
  async function api(verb, payload = {}) {
    await browserReady;
    if (sessionExpired) throw portalError(sessionExpiredMessage);
    const headers = {
      "Content-Type": "application/json", "X-Memory-Portal-Browser": "1",
      "X-Memory-Portal-View": "multi-agent",
    };
    if (portalSession) headers["X-Memory-Portal-Session"] = portalSession;
    const started = performance.now();
    const entry = {verb, method: "POST", status: 0, ms: 0, requestId: "", summary: payloadSummary(payload), error: ""};
    state.traces.unshift(entry);
    state.traces = state.traces.slice(0, 50);
    state.pending++;
    try {
      const response = await fetch(`/v1/${verb}`, {
        method: "POST", headers, body: JSON.stringify(payload), credentials: "same-origin",
      });
      entry.status = response.status;
      entry.requestId = response.headers.get("X-Request-ID") || "";
      if (response.status === 401) expireSession();
      let data = {};
      try { data = await response.json(); } catch { data = {}; }
      if (!response.ok) {
        entry.error = data.error || String(response.status);
        const error = portalError(data.message || data.error || tr("请求失败", "Request failed"), response.status);
        error.requestId = entry.requestId;
        throw error;
      }
      return {data, requestId: entry.requestId, status: response.status};
    } catch (error) {
      if (!entry.status) entry.error = error?.name || "NetworkError";
      throw error;
    } finally {
      entry.ms = Math.round(performance.now() - started);
      state.pending--;
    }
  }

  function navButton() {
    const nav = document.querySelector("nav[role=tablist]");
    if (!nav) return null;
    let button = $ma("tab-multiAgent");
    if (!button) {
      button = document.createElement("button");
      button.id = "tab-multiAgent";
      button.dataset.tab = "multi-agent";
      button.setAttribute("role", "tab");
      button.setAttribute("aria-controls", "multi-agent");
      button.setAttribute("aria-selected", "false");
      button.tabIndex = -1;
      button.innerHTML = `${icon("network")}<span></span>`;
      const settings = $ma("tab-settings");
      if (settings?.parentElement === nav) nav.insertBefore(button, settings);
      else nav.append(button);
    }
    return button;
  }
  function installNavigation() {
    const button = navButton();
    if (!button) return;
    messages.memoryGroup = ["记忆", "Memories"];
    messages.multiAgent = ["多 Agent 角色记忆", "Multi-Agent Role Memory"];
    navigationLabels.multiAgent = "multiAgent";
    const group = document.querySelector(".nav-label");
    if (group) {
      group.setAttribute("data-i18n", "memoryGroup");
      setText(group, msg("memoryGroup"));
    }
    setText(button.querySelector("span"), msg("multiAgent"));
    button.onclick = () => switchTab("multi-agent");
    button.onkeydown = event => {
      if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const tabs = [...document.querySelectorAll("nav button[data-tab]")];
      const index = tabs.indexOf(button);
      const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1
        : (index + (["ArrowRight", "ArrowDown"].includes(event.key) ? 1 : -1) + tabs.length) % tabs.length;
      tabs[next].focus();
      switchTab(tabs[next].dataset.tab);
    };
    const originalRender = renderNavigation;
    renderNavigation = function (name) {
      const renderName = name === "multi-agent" ? "multiAgent" : name;
      originalRender(renderName);
      section.hidden = name !== "multi-agent";
      if (name === "multi-agent") setText($ma("breadcrumb-current"), msg("multiAgent"));
    };
    const originalSwitch = switchTab;
    switchTab = async function (name, load = true) {
      if (name !== "multi-agent") return originalSwitch(name, load);
      activeTab = name;
      renderNavigation(name);
      closeDetail();
      await activate();
    };
    const originalLanguage = applyLanguage;
    applyLanguage = function () {
      originalLanguage();
      setText(button.querySelector("span"), msg("multiAgent"));
      if (group) setText(group, msg("memoryGroup"));
      localize();
      renderCurrent();
    };
  }

  function localize() {
    setText($ma("ma-refresh-capability"), tr("刷新", "Refresh"));
    setText($ma("ma-diagnostics-label"), tr("开发者信息", "Developer details"));
    setText($ma("ma-traces-open"), tr("接口记录", "Request Log"));
    const pageLabels = {search: [tr("共享搜索", "Shared Search"), tr("共享记忆搜索", "Shared Memory Search")],
      task: [tr("任务接续", "Task Continuation"), tr("任务接续", "Task Continuation")],
      role: [tr("职责视角", "Duty View"), tr("职责记忆视图", "Duty Memory View")]};
    document.querySelectorAll("#multi-agent [data-ma-page]").forEach(button => {
      setText(button, pageLabels[button.dataset.maPage][0]);
    });
    setText($ma("ma-search-query"), tr("搜索共享记忆，或输入完整平台名", "Search shared memory or type a full platform name"), "placeholder");
    setText($ma("ma-search-platform"), tr("全部来源", "All sources"), "aria-label");
    setText($ma("ma-search-form").querySelector("button[type=submit]"), tr("搜索", "Search"));
    setText($ma("ma-known-platforms").parentElement.querySelector("span"), tr("仅已配置平台来源", "Configured platforms only"));
    setText($ma("ma-search-note"), "");
    setText($ma("ma-role-scope-label"), tr("检索范围", "Scope"));
    setText($ma("ma-role-query-label"), tr("查找内容", "Search memories"));
    setText($ma("ma-role-form").querySelector("button[type=submit]"), tr("检索", "Search"));
    setText($ma("ma-role-query"), tr("任务范围可留空读取背景", "Task scope may be empty to read background"), "placeholder");
    setText($ma("ma-duty-label"), tr("查看视角", "Duty perspective"));
    setText($ma("ma-compare").parentElement.querySelector("span"), tr("职责对照（2～3 项）", "Compare duties (2-3)"));
    setText($ma("ma-limit-label").querySelector("span"), tr("返回上限", "Result limit"));
    setText($ma("ma-lookup-title"), tr("查找任务", "Find Task"));
    setText($ma("ma-lookup-hint"), tr("查找只返回匹配的开放任务；选择候选后才会读取任务。", "Find returns matching open tasks. A task is read only after you select a candidate."));
    setText(document.querySelector('[data-ma-lookup="query"]'), tr("按目标查找", "By goal"));
    setText(document.querySelector('[data-ma-lookup="id"]'), tr("按任务 ID 打开", "Open by task ID"));
    setText($ma("ma-lookup-form").querySelector("button[type=submit]"), tr("查找", "Find"));
    setText($ma("ma-detail-title-label"), tr("记忆详情", "Memory Details"));
    setText($ma("ma-detail-context"), tr("完整内容、来源与职责依据", "Full content, sources and duty evidence"));
    setText($ma("ma-gaps-title"), tr("本次检索缺口", "Retrieval Gaps"));
    setText($ma("ma-traces-title"), tr("接口记录", "Request Log"));
    setText($ma("ma-traces-hint"), tr("只保留字段摘要、状态和耗时，不记录查询正文或凭据。", "Only field summaries, status, and duration are kept. Query text and credentials are not logged."));
    document.querySelectorAll("#multi-agent-dialogs [data-ma-close]").forEach(button =>
      setText(button, tr("关闭", "Close"), "aria-label"));
    renderDuties();
    renderCatalogNote();
    renderTask();
    renderRole();
    if (state.searchResult) renderSearchResult();
  }
  const dutyName = duty => state.dutyMap.get(duty)?.display_name || duty;
  const relationName = relation => state.relationMap.get(relation)?.display_name || relation;
  const relationDescription = relation => state.relationMap.get(relation)?.description || "";
  const dutyNames = values => unique((Array.isArray(values) ? values : []).map(dutyName)).join(language === "en" ? ", " : "、");

  function renderRoleScope() {
    const select = $ma("ma-role-scope");
    const task = state.task?.title || tr("未命名任务", "Untitled task");
    if (!state.task && state.roleScope === "task") state.roleScope = "project";
    select.innerHTML = `<option value="project">${esc(tr("当前共享空间", "Current shared space"))}</option><option value="task" ${!state.task || !roleCatalogUsable() ? "disabled" : ""}>${esc(state.task ? tr(`任务：${task}`, `Task: ${task}`) : tr("请先选择任务", "Choose a task first"))}</option><option value="choose-task">${esc(tr("查找其他任务…", "Find another task…"))}</option>`;
    select.value = state.roleScope;
    select.title = select.selectedOptions[0]?.textContent || "";
  }

  function renderDuties() {
    const root = $ma("ma-duty");
    const entries = [...state.dutyMap.values()];
    root.innerHTML = entries.length
      ? entries.map(duty => `<option value="${esc(duty.id)}">${esc(duty.display_name || duty.id)}</option>`).join("")
      : `<option value="">${esc(tr("目录不可用", "Catalog unavailable"))}</option>`;
    if (!state.dutyMap.has(state.duty)) state.duty = entries[0]?.id || "";
    root.value = state.duty;
    root.disabled = !entries.length;
    renderCompareDuties();
    renderPolicy();
  }

  function renderCompareDuties() {
    const root = $ma("ma-compare-duties");
    root.hidden = !state.compare;
    if (!state.compare) return;
    const entries = [...state.dutyMap.values()];
    root.innerHTML = `<div class="ma-page-note">${esc(tr("选择 2～3 个职责；提交后每个职责独立检索。", "Choose 2-3 duties; each duty runs its own search on submit."))}</div><div class="ma-compare-options">${entries.map((duty, index) => `<label class="ma-check"><input type="checkbox" data-ma-compare-index="${index}" ${state.compareDuties.includes(duty.id) ? "checked" : ""}><span>${esc(duty.display_name || duty.id)}</span></label>`).join("")}</div>`;
    root.querySelectorAll("[data-ma-compare-index]").forEach(input => {
      input.onchange = () => {
        const duty = entries[Number(input.dataset.maCompareIndex)]?.id;
        if (!duty) return;
        if (input.checked && !state.compareDuties.includes(duty)) {
          if (state.compareDuties.length >= 3) {
            input.checked = false;
            toast(tr("对照最多选择 3 个职责。", "Choose at most three duties."));
            return;
          }
          state.compareDuties.push(duty);
        } else if (!input.checked) {
          state.compareDuties = state.compareDuties.filter(item => item !== duty);
        }
        invalidateRoleRequests();
        state.roleResults = {};
        state.roleErrors = {};
        state.rolePending.clear();
        renderPolicy();
        renderRole();
      };
    });
  }

  function renderPolicy() {
    const details = $ma("ma-policy-details");
    if (!state.dutyMap.size) {
      details.hidden = true;
      details.open = false;
      return;
    }
    const duties = state.compare ? state.compareDuties : [state.duty];
    details.hidden = false;
    $ma("ma-policy-summary").textContent = tr("职责配置", "Duty configuration");
    $ma("ma-policy-body").innerHTML = `<p class="ma-page-note">${esc(tr("配置版本", "Policy version"))}：${esc(state.policyVersion || tr("未提供", "Not provided"))}</p>` + duties.map(id => {
      const duty = state.dutyMap.get(id);
      if (!duty) return "";
      const rules = (duty.traversal || []).map(rule => `<li><code>${esc(relationName(rule.edge_type))} · ${esc(rule.direction)}</code><span>${esc(tr("优先级", "priority"))} ${esc(rule.priority)} · ${esc(tr("深度", "depth"))} ${esc(rule.max_depth)}</span></li>`).join("");
      const budget = Object.entries(duty.budget || {}).map(([key, value]) => `${key}=${value}`).join(" · ");
      return `<section class="ma-policy-duty"><h3>${esc(duty.display_name || duty.id)} <code>${esc(duty.id)}</code></h3>${duty.description ? `<p>${esc(duty.description)}</p>` : ""}<ul>${rules}</ul><small>${esc(budget)}</small></section>`;
    }).join("");
  }

  async function activate() {
    section.hidden = false;
    if (!state.ready) {
      state.ready = true;
      await loadCapability();
    }
    localize();
    renderCurrent();
  }
  async function loadCapability({refresh = false} = {}) {
    const previousVersion = state.policyVersion;
    try {
      const {data} = await api("portal_context", {});
      state.capability = data;
      const catalog = data.role_catalog || {};
      const duties = Array.isArray(catalog.duties) ? catalog.duties : [];
      const relations = Array.isArray(catalog.relation_types) ? catalog.relation_types : [];
      const version = catalog.policy_version || null;
      const dutyIds = duties.map(item => item?.id).filter(Boolean);
      const relationIds = relations.map(item => item?.id).filter(Boolean);
      if (catalog.available && (dutyIds.length !== duties.length || new Set(dutyIds).size !== dutyIds.length)) {
        state.catalogError = "duty_ids_invalid";
      } else if (catalog.available && (relationIds.length !== relations.length || new Set(relationIds).size !== relationIds.length)) {
        state.catalogError = "relation_ids_invalid";
      } else if (catalog.available && !dutyIds.includes(catalog.default_duty)) {
        state.catalogError = "default_duty_invalid";
      } else {
        state.catalogError = "";
      }
      const changed = !!(previousVersion && version && previousVersion !== version);
      state.dutyMap = new Map(duties.filter(item => item?.id).map(item => [item.id, item]));
      state.relationMap = new Map(relations.filter(item => item?.id).map(item => [item.id, item]));
      state.policyVersion = version;
      state.compareDuties = state.compareDuties.filter(id => state.dutyMap.has(id));
      if (data.mode !== "role" || !catalog.available || state.catalogError) {
        state.compare = false;
        state.compareDuties = [];
        $ma("ma-compare").checked = false;
      }
      const preferred = catalog.default_duty || state.duty;
      if ((!previousVersion && state.dutyMap.has(preferred)) || !state.dutyMap.has(state.duty)) {
        state.duty = state.dutyMap.has(preferred) ? preferred : (duties[0]?.id || "");
      }
      if (refresh || changed) {
        invalidateRoleRequests();
        state.roleResults = {};
        state.roleErrors = {};
        state.rolePending.clear();
        state.catalogStale = false;
        state.roleError = "";
        state.catalogStale = changed;
      }
      renderCapability();
    } catch (error) {
      state.dutyMap = new Map();
      state.relationMap = new Map();
      state.policyVersion = null;
      state.catalogError = "";
      state.capability = {available: false, reason: errorMessage(error), connectors: []};
      renderCapability(error);
    }
  }
  function hasRestrictedContinuityScope() {
    const scope = state.capability?.scope || {};
    return Boolean(blank(scope.user) || blank(scope.agent) || blank(scope.session));
  }
  function continuityTaskUsable() {
    return state.capability?.available === true
      && ["continuity", "role"].includes(state.capability?.mode);
  }
  function taskBackgroundUsable() {
    return continuityTaskUsable() && !hasRestrictedContinuityScope();
  }
  function continuityScopeRestrictionText() {
    return tr(
      "当前共享范围通过 user、agent 或 session 收窄。共享搜索与任务信息仍可用，任务背景和职责检索需要未收窄的组织/空间范围。",
      "This shared scope is narrowed by user, agent, or session. Shared search and task information remain available; task background and duty search require an unrestricted organization/space scope.",
    );
  }
  function roleCatalogUsable() {
    return !hasRestrictedContinuityScope() && state.capability?.mode === "role"
      && state.capability?.role_catalog?.available === true
      && state.dutyMap.size > 0 && !state.catalogError;
  }
  function catalogErrorText() {
    return {
      duty_ids_invalid: tr("职责目录包含空 ID 或重复 ID。", "The duty catalog contains missing or duplicate IDs."),
      relation_ids_invalid: tr("关系目录包含空 ID 或重复 ID。", "The relation catalog contains missing or duplicate IDs."),
      default_duty_invalid: tr("目录默认职责不在有效职责中。", "The catalog default duty is not in the effective duty list."),
    }[state.catalogError] || "";
  }
  function renderCatalogNote() {
    const capability = state.capability || {};
    $ma("ma-catalog-note").hidden = roleCatalogUsable();
    $ma("ma-catalog-note").textContent = roleCatalogUsable()
      ? ""
      : hasRestrictedContinuityScope()
        ? continuityScopeRestrictionText()
        : capability.mode === "continuity"
          ? tr("当前服务仅支持任务接续，暂不支持职责视角。", "This service supports task continuation only.")
          : catalogErrorText() || tr("职责信息暂不可用，请刷新后重试。", "Duty information is unavailable. Refresh and try again.");
  }
  function renderCapability(error = null) {
    const capability = state.capability;
    $ma("ma-unavailable").hidden = !!capability?.available;
    $ma("ma-unavailable").innerHTML = capability?.available ? "" :
      `<div class="ma-empty"><strong>${esc(tr("多 Agent 角色记忆不可用", "Multi-Agent memory is unavailable"))}</strong><span>${esc(capability?.reason || errorMessage(error))}</span></div>`;
    if (!capability?.available) {
      $ma("ma-compare").disabled = true;
      $ma("ma-role-form").querySelector("button[type=submit]").disabled = true;
      renderCatalogNote();
      renderDuties();
      return;
    }
    $ma("ma-project").textContent = capability.project_name;
    const root = $ma("ma-search-platform");
    root.innerHTML = `<option value="">${esc(tr("全部来源", "All sources"))}</option>` +
      (capability.connectors || []).map(item => `<option value="${esc(item.id)}">${esc(item.label)}</option>`).join("");
    const usable = roleCatalogUsable();
    $ma("ma-duty").disabled = !usable;
    $ma("ma-compare").disabled = !usable;
    $ma("ma-role-form").querySelector("button[type=submit]").disabled = !usable;
    renderRoleScope();
    renderCatalogNote();
    renderDuties();
    renderCurrent();
  }
  function pageCopy() {
    const title = activePage() === "search" ? tr("共享记忆搜索", "Shared Memory Search")
      : activePage() === "task" ? tr("任务接续", "Task Continuation")
      : tr("职责记忆视图", "Duty Memory View");
    const description = activePage() === "search"
      ? tr("查找不同 Agent 留下的共享记录与经验。", "Find shared records and experiences from different Agents.")
      : activePage() === "task"
        ? tr("了解已有进展、关键背景和待验证事项，继续完成任务。", "Review progress, context, and pending checks before continuing.")
        : tr("从不同职责查看证据、判断和待核验事项。", "Review evidence, judgments, and pending checks from different perspectives.");
    return {title, description};
  }
  function renderCurrent() {
    if (!state.capability?.available) return;
    const copy = pageCopy();
    $ma("ma-title").textContent = copy.title;
    $ma("ma-description").textContent = copy.description;
    document.querySelectorAll("#multi-agent [data-ma-page]").forEach(button =>
      button.setAttribute("aria-pressed", String(button.dataset.maPage === activePage())));
    $ma("ma-search-page").hidden = activePage() !== "search";
    $ma("ma-task-page").hidden = activePage() !== "task";
    $ma("ma-role-page").hidden = activePage() !== "role";
    renderRoleScope();
    if (activePage() === "task") renderTask();
    if (activePage() === "role") renderRole();
  }
  function setPage(page) {
    state.page = page;
    renderCurrent();
  }

  function renderRecords(container, items, scope, paths = []) {
    const id = `ma-bind-${++state.bindingSeq}`;
    state.records.set(id, {items, scope, paths});
    container.dataset.maBind = id;
    container.innerHTML = items.length ? `<div class="ma-results">` + items.map((item, index) =>
      `<article class="ma-record ${isRisk(item) ? "ma-risk" : ""}">
        <div class="ma-record-head"><button type="button" class="ma-record-main" data-ma-index="${index}">
          <span class="ma-record-origin">${sourceHtml(item, scope)}${unitFor(item, scope)?.temporal?.t_event ? `<span>${esc(nowDate(unitFor(item, scope).temporal.t_event))}</span>` : ""}</span>
          ${recordBodyHtml(item)}
          <span class="ma-meta">
            ${item.view ? `<span class="ma-pill ${isRisk(item) ? "red" : ""}">${esc(viewKind(item.view.kind))}</span>` : `<span class="ma-pill">${esc(tr("记忆记录", "Memory record"))}</span>`}
            ${item.view?.status ? `<span class="ma-pill">${esc(viewStatus(item.view.status))}</span>` : ""}
            ${item.view?.required ? `<span class="ma-pill">${esc(tr("必需背景", "Required background"))}</span>` : ""}
          </span>
          ${item.view?.conditions?.length ? `<span class="ma-conditions">${esc(tr("适用条件：", "Conditions: "))}${esc(item.view.conditions.join(" · "))}</span>` : ""}
        </button><span>${icon("file")}</span></div>
        <div class="ma-row-actions">
          <button type="button" class="ma-link" data-ma-open="${index}">${icon("file")}${tr("查看正文与来源", "View body and source")}</button>
          ${sourceMeta(item, scope).continuity_task ? `<button type="button" class="ma-link" data-ma-task="${esc(sourceMeta(item, scope).continuity_task)}">${icon("folder")}${tr("查看所属任务", "Open source task")}</button>` : ""}
        </div>
      </article>`).join("") + `</div>` : `<div class="ma-empty"><strong>${esc(tr("本次未返回匹配记忆", "No matching memories returned"))}</strong><span>${esc(tr("这不代表共享空间中没有任何记忆。", "This does not mean the shared space has no memories."))}</span></div>`;
    container.querySelectorAll("[data-ma-open]").forEach(button =>
      button.onclick = () => openDetail(items[Number(button.dataset.maOpen)], null, scope, paths));
    container.querySelectorAll("[data-ma-index]").forEach(button =>
      button.onclick = () => openDetail(items[Number(button.dataset.maIndex)], null, scope, paths));
    container.querySelectorAll("[data-ma-task]").forEach(button =>
      button.onclick = () => openTask(button.dataset.maTask));
  }
  function renderRecordsAndHydrate(
    container, items, scope, paths = [], isCurrent = () => true,
  ) {
    renderRecords(container, items, scope, paths);
    const hydration = hydrate(items, scope);
    renderRecords(container, items, scope, paths);
    hydration.then(() => {
      if (isCurrent()) renderRecords(container, items, scope, paths);
    });
  }
  function viewKind(kind) {
    return ({observation: "观察", statement: "陈述", inference: "判断", suggestion: "建议", decision: "决策", experience: "经验", risk: "风险", open_issue: "待解决事项"})[kind] || kind || tr("记录", "Record");
  }
  function viewStatus(status) {
    return ({reported: "来源陈述", observed: "已观察", candidate: "待验证", supported_in_conditions: "条件内有依据", needs_review: "待复核", refuted: "已反驳", unknown: "未明确"})[status] || status || tr("未明确", "Unspecified");
  }
  function relationLabel(value) {
    return relationName(value) || value;
  }
  function gapLabel(value) {
    return ({unresolved_conflict: "存在未决反证", unchecked_dependencies: "部分依赖仍待核验", environment_unconfirmed: "当前环境尚未核验", relation_coverage_unknown: "关联依据可能不完整", budget_exhausted: "本次仅展示部分结果", reference_unavailable: "部分关联记忆暂不可读", relation_expansion_disabled: "未启用职责关系扩展", retrieval_partial: "部分检索通道不可用"})[value] || tr("部分信息需要进一步核验", "Some information needs further review");
  }

  function canonical(value) {
    if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
    if (value && typeof value === "object") {
      return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${canonical(value[key])}`).join(",")}}`;
    }
    return JSON.stringify(value);
  }
  async function memoryRefOf(unit) {
    const segments = (unit.segments || []).map(segment => ({
      content: segment.content, assets: segment.assets || [], source: segment.source,
    }));
    const bytes = new TextEncoder().encode(canonical({segments}));
    const digest = await crypto.subtle.digest("SHA-256", bytes);
    const contentDigest = [...new Uint8Array(digest)].map(byte => byte.toString(16).padStart(2, "0")).join("");
    return {scope: safeScope(unit.scope), unit_id: unit.id, content_digest: contentDigest};
  }
  function pumpHydration() {
    while (state.hydrationActive < 4 && state.hydrationQueue.length) {
      const task = state.hydrationQueue.shift();
      state.hydrationActive++;
      (async () => {
        try {
          const {data: unit} = await api("get", {unit_id: task.ref.unit_id, scope: task.ref.scope});
          const current = await memoryRefOf(unit);
          if (task.ref.content_digest && current.content_digest !== task.ref.content_digest) {
            state.staleRefs.add(task.key);
          }
          state.units.set(task.key, unit);
          state.readFailures.delete(task.key);
        } catch {
          state.readFailures.add(task.key);
        } finally {
          state.refLoading.delete(task.key);
          state.hydrationPending.delete(task.key);
          state.hydrationActive--;
          task.resolve();
          pumpHydration();
        }
      })();
    }
  }
  function hydrateRef(ref) {
    const key = recordKey(ref.scope, ref.unit_id);
    if (state.hydrationPending.has(key)) return state.hydrationPending.get(key);
    state.refLoading.add(key);
    const pending = new Promise(resolve => {
      state.hydrationQueue.push({ref, key, resolve});
      pumpHydration();
    });
    state.hydrationPending.set(key, pending);
    return pending;
  }
  async function hydrate(items, fallbackScope, force = false) {
    const refs = [];
    for (const item of items) {
      const ref = referenceForItem(item, fallbackScope);
      const key = recordKey(ref.scope, ref.unit_id);
      if (!force && state.units.has(key)) continue;
      const metadata = item.system_metadata || {};
      if (force || !blank(metadata.continuity_connector)) refs.push(ref);
    }
    const deduped = [...new Map(refs.map(ref => [recordKey(ref.scope, ref.unit_id), ref])).values()];
    await Promise.all(deduped.map(hydrateRef));
  }
  function sourceFilters(platform, knownOnly, taskId = "") {
    const filters = {};
    if (platform) filters["system_metadata.continuity_connector"] = {eq: platform};
    else if (knownOnly) filters["system_metadata.continuity_connector"] = {in: (state.capability?.connectors || []).map(item => item.id)};
    if (taskId) filters["system_metadata.continuity_task"] = {eq: taskId};
    return filters;
  }
  function parseSelection(rawQuery, selectedPlatform, knownOnly) {
    const exact = connectorIdForText(rawQuery);
    if (exact) return {query: "", platform: exact, knownOnly};
    return {query: rawQuery.trim(), platform: selectedPlatform, knownOnly};
  }
  async function sourceRequest(selection, offset = 0, limit = 20, taskId = "") {
    const scope = safeScope(state.capability.scope);
    const filters = sourceFilters(selection.platform, selection.knownOnly, taskId);
    if (selection.query) {
      const payload = {query: selection.query, context: {scope}, top_k: 20, disclosure: "l2"};
      if (Object.keys(filters).length) payload.filters = filters;
      const {data} = await api("search", payload);
      return { ...data, method: "search", offset: 0, limit: 20 };
    }
    const payload = {scope, offset, limit};
    if (Object.keys(filters).length) payload.filters = filters;
    const {data} = await api("list", payload);
    return {
      items: (data.items || []).map(unit => ({
        unit_id: unit.id, abstract: unit.segments?.map(segment => segment.content).join("\n\n") || "",
        content: unit.segments?.map(segment => segment.content).join("\n\n") || "", system_metadata: unit.system_metadata || {}, view: null,
      })),
      count: data.count || 0, method: "list", offset, limit,
    };
  }
  function renderSearchResult() {
    const root = $ma("ma-search-results");
    if (!state.searchResult) {
      root.innerHTML = `<div class="ma-empty"><strong>${esc(tr("尚未检索", "No search yet"))}</strong><span>${esc(tr("进入页面不会自动读取记忆或任务。", "Opening this page does not load memories or tasks."))}</span></div>`;
      return;
    }
    const result = state.searchResult;
    const scope = safeScope(state.capability.scope);
    let html = `<div class="ma-gapline">${gapBanner(result, "ma-search-gaps")}</div>`;
    html += result.method === "list"
      ? `<div class="ma-page-note">${esc(tr(`按来源浏览：本页 ${result.items.length} 条，匹配 ${result.count} 条`, `Source browse: ${result.items.length} on this page, ${result.count} matching`))}</div>`
      : `<div class="ma-page-note">${esc(tr(`语义检索返回 ${result.items.length} 条，结果不是全量清单。`, `Semantic search returned ${result.items.length} item(s); this is not a full inventory.`))}</div>`;
    html += `<div id="ma-search-record-root"></div>`;
    if (result.method === "list") {
      html += `<div class="ma-pager"><span>${esc(tr("上一页 / 下一页按当前筛选快照读取", "Previous/next use the current filter snapshot"))}</span><div class="row"><button type="button" id="ma-search-prev" ${result.offset === 0 ? "disabled" : ""}>${esc(tr("上一页", "Previous"))}</button><button type="button" id="ma-search-next" ${result.offset + result.limit >= result.count ? "disabled" : ""}>${esc(tr("下一页", "Next"))}</button></div></div>`;
    }
    root.innerHTML = html;
    renderRecordsAndHydrate(
      $ma("ma-search-record-root"), result.items, scope, result.paths || [],
      () => state.searchResult === result,
    );
    if ($ma("ma-search-gaps")) $ma("ma-search-gaps").onclick = () => openGaps([result]);
    if ($ma("ma-search-prev")) $ma("ma-search-prev").onclick = () => search(state.searchSelection, Math.max(0, result.offset - result.limit));
    if ($ma("ma-search-next")) $ma("ma-search-next").onclick = () => search(state.searchSelection, result.offset + result.limit);
  }
  function gapBanner(result, id) {
    const gaps = result?.gaps || [];
    const errors = result?.errors || [];
    if (!gaps.length && !errors.length) return "";
    const blocking = gaps.filter(gap => gap.blocking).length;
    return `<div class="ma-gap-banner">${icon("alert")}<span>${esc(tr(`${blocking} 类阻塞缺口`, `${blocking} blocking gap(s)`))}${errors.length ? esc(tr(` · ${errors.length} 个通道失败`, ` · ${errors.length} channel error(s)`)) : ""}</span><button type="button" class="ma-link" id="${id}">${esc(tr("查看缺口", "View gaps"))}</button></div>`;
  }
  async function search(selection, offset = 0) {
    const token = ++state.searchToken;
    state.searchSelection = selection;
    const root = $ma("ma-search-results");
    root.innerHTML = `<div class="ma-loading">${esc(tr("正在检索共享记忆…", "Searching shared memory…"))}</div>`;
    $ma("ma-search-error").innerHTML = "";
    try {
      const result = await sourceRequest(selection, offset, 20);
      if (token !== state.searchToken) return;
      state.searchResult = result;
      renderSearchResult();
    } catch (error) {
      if (token !== state.searchToken) return;
      $ma("ma-search-error").innerHTML = `<div class="ma-error">${esc(errorMessage(error))}</div>`;
      root.innerHTML = `<div class="ma-empty"><strong>${esc(tr("检索失败", "Search failed"))}</strong><span>${esc(errorMessage(error))}</span></div>`;
    }
  }
  $ma("ma-search-form").onsubmit = event => {
    event.preventDefault();
    const selection = parseSelection($ma("ma-search-query").value, $ma("ma-search-platform").value, $ma("ma-known-platforms").checked);
    if (!selection.query && !selection.platform && !selection.knownOnly) {
      $ma("ma-search-error").innerHTML = `<div class="ma-error">${esc(tr("请输入关键词或选择来源平台。", "Enter a keyword or choose a source platform."))}</div>`;
      return;
    }
    $ma("ma-search-query").value = selection.query;
    $ma("ma-search-platform").value = selection.platform;
    search(selection);
  };
  $ma("ma-search-platform").onchange = () => {
    if (state.searchSelection) {
      const selection = parseSelection($ma("ma-search-query").value, $ma("ma-search-platform").value, $ma("ma-known-platforms").checked);
      if (selection.query || selection.platform || selection.knownOnly) search(selection);
    }
  };
  $ma("ma-known-platforms").onchange = () => {
    if (!state.searchSelection && !$ma("ma-search-query").value.trim()) return;
    const selection = parseSelection($ma("ma-search-query").value, $ma("ma-search-platform").value, $ma("ma-known-platforms").checked);
    if (selection.query || selection.platform || selection.knownOnly) search(selection);
  };

  async function openTask(id) {
    if (!id) return;
    if (!continuityTaskUsable()) {
      toast(tr("当前 Runtime 未提供任务接续能力。", "This Runtime does not provide task continuation."));
      return;
    }
    const token = ++state.taskToken;
    state.taskError = "";
    $ma("ma-lookup-dialog").close();
    setPage("task");
    $ma("ma-task-page").innerHTML = `<div class="ma-loading">${esc(tr("正在读取任务…", "Reading task…"))}</div>`;
    try {
      const {data: task} = await api("continuity_task", {scope: safeScope(state.capability.scope), request: {operation: "get", task_id: id}});
      if (token !== state.taskToken) return;
      state.task = task;
      state.taskResult = null;
      state.taskSourceSelection = null;
      state.taskSourceResult = null;
      state.taskTab = "background";
      state.roleScope = "task";
      $ma("ma-role-scope").value = "task";
      state.anchor = null;
      invalidateRoleRequests();
      state.roleResults = {};
      state.roleErrors = {};
      state.rolePending.clear();
      renderTask();
      if (!taskBackgroundUsable()) {
        state.taskResult = null;
        state.taskError = continuityScopeRestrictionText();
        renderTask();
        return;
      }
      try {
        const {data: background} = await api("search", {
          query: "", context: {scope: safeScope(state.capability.scope)},
          task_id: id, duty: "general", top_k: 20, disclosure: "l2",
        });
        if (token !== state.taskToken) return;
        state.taskResult = background;
        if (token === state.taskToken) {
          await hydrate(background.items || [], state.capability.scope, true);
        }
      } catch (error) {
        if (token !== state.taskToken) return;
        state.taskError = errorMessage(error);
      }
      if (token === state.taskToken) renderTask();
    } catch (error) {
      if (token !== state.taskToken) return;
      state.task = null;
      state.taskError = errorMessage(error);
      renderTask();
    }
  }
  function renderTask() {
    const root = $ma("ma-task-page");
    if (!root) return;
    if (!continuityTaskUsable()) {
      root.innerHTML = `<div class="ma-empty"><strong>${esc(tr("任务接续不可用", "Task continuation is unavailable"))}</strong><span>${esc(tr("当前 Runtime 未提供任务接续能力。", "This Runtime does not provide task continuation."))}</span></div>`;
      return;
    }
    if (!state.task) {
      root.innerHTML = `<div class="ma-empty"><strong>${esc(tr("未选择任务", "No task selected"))}</strong><span>${esc(tr("按目标查找开放任务，或输入已知任务 ID。", "Find an open task or enter a known task ID."))}</span><div class="ma-row-actions"><button type="button" class="primary" id="ma-empty-find">${esc(tr("查找任务", "Find task"))}</button><button type="button" id="ma-empty-id">${esc(tr("按 ID 打开", "Open by ID"))}</button></div><div class="ma-error" id="ma-task-error">${esc(state.taskError)}</div></div>`;
      $ma("ma-empty-find").onclick = () => showLookup("query");
      $ma("ma-empty-id").onclick = () => showLookup("id");
      return;
    }
    const task = state.task;
    const result = state.taskResult;
    const background = (result?.items || []).filter(item => item.view?.discovery === "background");
    const required = background.filter(item => item.view?.required);
    const bindings = (task.bindings || []).filter(binding => binding.active);
    const sorted = [...background].sort((a, b) => {
      const left = unitFor(a, state.capability.scope)?.temporal?.t_event || "";
      const right = unitFor(b, state.capability.scope)?.temporal?.t_event || "";
      return right.localeCompare(left);
    });
    root.innerHTML = `
      <div class="ma-panel">
        <div class="row"><div class="ma-eyebrow">${esc(tr("当前任务", "Current task"))}</div><span class="ma-pill ${task.state === "closed" ? "red" : "green"}">${esc(task.state === "closed" ? tr("已关闭", "Closed") : tr("进行中", "Open"))}</span><span class="spacer"></span><button type="button" class="quiet" id="ma-clear-task">${esc(tr("切换任务", "Switch task"))}</button></div>
        <h2 class="ma-task-title">${esc(task.title || tr("未命名任务", "Untitled task"))}</h2>
        <p class="ma-task-desc">${esc(task.description || tr("暂无任务说明", "No task description"))}</p>
        <div class="ma-row-actions"><button type="button" id="ma-task-background-tab" aria-pressed="${state.taskTab === "background"}">${icon("history")}${esc(tr("接续背景", "Continuation background"))}</button><button type="button" id="ma-task-sources-tab" aria-pressed="${state.taskTab === "sources"}">${icon("filter")}${esc(tr("按来源查看", "Browse by source"))}</button><button type="button" class="ma-link" id="ma-task-role" ${roleCatalogUsable() ? "" : "disabled"}>${icon("network")}${esc(tr("按职责展开", "Open duty view"))}</button></div>
      </div>
      <div id="ma-task-background" ${state.taskTab === "background" ? "" : "hidden"}>
        <div class="ma-task"><div class="ma-task-main"><div class="ma-section"><h2>${esc(tr("本次返回的任务背景", "Background returned in this request"))}</h2>${result ? "" : `<div class="ma-error">${esc(state.taskError || tr("背景加载失败", "Background failed to load"))}</div>`}${result ? `<div class="ma-timeline">${sorted.length ? "" : `<div class="ma-empty">${esc(tr("本次未返回可展示背景。", "No displayable background was returned."))}</div>`}</div>` : ""}<div id="ma-task-timeline"></div><div class="ma-page-note">${esc(tr("仅代表本次检索返回，不是完整历史，也不是执行所有权。", "This request is not a complete history or execution ownership."))}</div></div></div>
        <aside class="ma-task-side"><section class="ma-section"><h3>${esc(tr("已接入平台", "Connected platforms"))}</h3>${task.background_partial ? `<p class="ma-error">${esc(tr("部分任务背景暂不可读。", "Some task context is unavailable."))}</p>` : ""}<div>${bindings.length ? unique(bindings.map(binding => binding.connector_id)).map(connector => `<div class="ma-binding"><span>${icon("network")}</span><strong>${esc(connectorLabel(connector))}</strong></div>`).join("") : `<p class="ma-page-note">${esc(tr("暂无平台接入记录。", "No platform connections recorded."))}</p>`}</div><details class="ma-tech"><summary>${esc(tr("任务技术信息", "Task technical details"))}</summary><pre>${esc(JSON.stringify({task_id: task.task_id, revision: task.revision, bindings: task.bindings}, null, 2))}</pre></details></section>
        <section class="ma-section"><h3>${esc(tr("必需背景", "Required background"))}</h3>${required.length ? required.map((item, i) => `<p>${sourceHtml(item, state.capability.scope)} ${esc(textOf(item))} <button type="button" class="ma-link" data-ma-required="${i}">${esc(tr("查看正文", "View body"))}</button></p>`).join("") : `<p class="ma-page-note">${esc(tr("本次未返回必需背景。", "No required background returned."))}</p>`}</section>
        <section class="ma-section"><h3>${esc(tr("本次检索缺口", "Retrieval gaps"))}</h3>${gapBanner(result || {}, "ma-task-gaps")}<p class="ma-page-note">${esc(result ? tr("缺口不代表任务安全或历史完整。", "Gaps do not imply a safe or complete history.") : tr("背景读取失败时，任务说明仍保留。", "The task description remains visible when background loading fails."))}</p></section></aside></div>
      </div>
      <div id="ma-task-sources" ${state.taskTab === "sources" ? "" : "hidden"}><div class="ma-panel"><div class="ma-form-line ma-search-line"><select id="ma-task-platform" class="ma-platform-select" aria-label="${esc(tr("来源平台", "Source platform"))}"><option value="">${esc(tr("全部来源", "All sources"))}</option>${(state.capability.connectors || []).map(item => `<option value="${esc(item.id)}">${esc(item.label)}</option>`).join("")}</select><input id="ma-task-query" type="search" aria-label="${esc(tr("搜索任务记录", "Search task records"))}" placeholder="${esc(tr("搜索当前任务的记录", "Search this task's records"))}"><button type="button" class="primary" id="ma-task-source-search">${esc(tr("查询", "Search"))}</button></div></div><div id="ma-task-source-result"></div></div>`;
    $ma("ma-task-query").value = state.taskSourceSelection?.query || "";
    $ma("ma-task-platform").value = state.taskSourceSelection?.platform || "";
    $ma("ma-clear-task").onclick = clearTask;
    $ma("ma-task-background-tab").onclick = () => { state.taskTab = "background"; renderTask(); };
    $ma("ma-task-sources-tab").onclick = () => { state.taskTab = "sources"; renderTask(); loadTaskSources(); };
    $ma("ma-task-role").onclick = () => {
      if (!roleCatalogUsable()) { toast(tr("当前 Runtime 不提供职责目录。", "This Runtime does not expose a duty catalog.")); return; }
      state.roleScope = "task"; $ma("ma-role-scope").value = "task";
      setPage("role"); runRoles();
    };
    $ma("ma-task-source-search").onclick = () => loadTaskSources();
    $ma("ma-task-platform").onchange = () => loadTaskSources();
    $ma("ma-task-query").onchange = () => loadTaskSources();
    if ($ma("ma-empty-find")) $ma("ma-empty-find").onclick = () => showLookup("query");
    if (result) {
      const timelineRoot = $ma("ma-task-timeline");
      renderRecords(timelineRoot, sorted, state.capability.scope, result.paths || []);
      root.querySelectorAll("[data-ma-required]").forEach(button => {
        button.onclick = () => openDetail(required[Number(button.dataset.maRequired)], null, state.capability.scope, result.paths || []);
      });
    }
    if ($ma("ma-task-gaps")) $ma("ma-task-gaps").onclick = () => openGaps(result ? [result] : []);
    if (state.taskSourceResult) renderTaskSources();
  }
  async function loadTaskSources() {
    if (!state.task) return;
    const token = ++state.taskSourceToken;
    const selection = parseSelection($ma("ma-task-query")?.value || "", $ma("ma-task-platform")?.value || "", false);
    state.taskSourceSelection = selection;
    if ($ma("ma-task-query")) $ma("ma-task-query").value = selection.query;
    if ($ma("ma-task-platform")) $ma("ma-task-platform").value = selection.platform;
    const root = $ma("ma-task-source-result");
    if (!root) return;
    root.innerHTML = `<div class="ma-loading">${esc(tr("正在读取任务来源记录…", "Reading task source records…"))}</div>`;
    try {
      const result = await sourceRequest(selection, 0, 20, state.task.task_id);
      if (token !== state.taskSourceToken) return;
      state.taskSourceResult = result;
      renderTaskSources();
    } catch (error) {
      if (token === state.taskSourceToken) root.innerHTML = `<div class="ma-error">${esc(errorMessage(error))}</div>`;
    }
  }
  function renderTaskSources() {
    const root = $ma("ma-task-source-result");
    const result = state.taskSourceResult;
    if (!root || !result) return;
    const sources = unique(result.items.map(item => sourceMeta(item, state.capability.scope).continuity_connector));
    root.innerHTML = `<div class="ma-panel"></div>`;
    const panel = root.firstElementChild;
    panel.innerHTML = `<div class="row"><h2>${esc(tr("任务记录", "Task records"))}</h2><span class="spacer"></span><span class="ma-page-note">${esc(tr(`${result.items.length} 条`, `${result.items.length} item(s)`))}</span></div><div class="ma-page-note">${sources.map(source => `<span class="ma-pill">${esc(connectorLabel(source))}</span>`).join(" ")}</div><div id="ma-task-source-records"></div><div class="ma-pager"><span>${esc(result.method === "list" ? tr(`第 ${Math.floor(result.offset / result.limit) + 1} 页 · 共 ${result.count} 条`, `Page ${Math.floor(result.offset / result.limit) + 1} · ${result.count} records`) : tr("本次检索结果", "Search result"))}</span><div class="row"><button type="button" id="ma-task-source-prev" ${result.offset === 0 ? "disabled" : ""}>${esc(tr("上一页", "Previous"))}</button><button type="button" id="ma-task-source-next" ${result.method !== "list" || result.offset + result.limit >= result.count ? "disabled" : ""}>${esc(tr("下一页", "Next"))}</button></div></div>`;
    renderRecordsAndHydrate(
      $ma("ma-task-source-records"), result.items, state.capability.scope, result.paths || [],
      () => state.taskSourceResult === result,
    );
    $ma("ma-task-source-prev").onclick = () => loadTaskSourcesAt(Math.max(0, result.offset - result.limit));
    $ma("ma-task-source-next").onclick = () => loadTaskSourcesAt(result.offset + result.limit);
  }
  async function loadTaskSourcesAt(offset) {
    if (!state.task || !state.taskSourceSelection) return;
    const token = ++state.taskSourceToken;
    const root = $ma("ma-task-source-result");
    root.innerHTML = `<div class="ma-loading">${esc(tr("正在读取任务来源记录…", "Reading task source records…"))}</div>`;
    try {
      const result = await sourceRequest(state.taskSourceSelection, offset, 20, state.task.task_id);
      if (token === state.taskSourceToken) { state.taskSourceResult = result; renderTaskSources(); }
    } catch (error) {
      if (token === state.taskSourceToken) root.innerHTML = `<div class="ma-error">${esc(errorMessage(error))}</div>`;
    }
  }
  function clearTask() {
    ++state.taskToken;
    invalidateRoleRequests();
    ++state.taskSourceToken;
    state.task = null;
    state.taskResult = null;
    state.taskError = "";
    state.taskTab = "background";
    state.taskSourceResult = null;
    state.taskSourceSelection = null;
    state.anchor = null;
    state.roleResults = {};
    state.roleErrors = {};
    state.rolePending.clear();
    state.roleScope = "project";
    $ma("ma-role-scope").value = "project";
    state.page = "task";
    renderTask();
    renderRole();
  }

  function showLookup(mode = "query", returnPage = state.page) {
    if (!continuityTaskUsable()) {
      toast(tr("当前 Runtime 未提供任务接续能力。", "This Runtime does not provide task continuation."));
      return;
    }
    state.lookupMode = mode;
    state.lookupReturnPage = returnPage;
    const dialog = $ma("ma-lookup-dialog");
    $ma("ma-lookup-query").value = "";
    $ma("ma-lookup-results").innerHTML = `<div class="ma-page-note">${esc(tr("尚未提交查找", "No lookup submitted"))}</div>`;
    document.querySelectorAll("[data-ma-lookup]").forEach(button =>
      button.setAttribute("aria-selected", String(button.dataset.maLookup === mode)));
    $ma("ma-lookup-query").placeholder = mode === "id"
      ? tr("输入已知任务 ID", "Enter a known task ID")
      : tr("任务标题或描述中的关键词", "A keyword from the task title or description");
    $ma("ma-lookup-hint").textContent = mode === "id"
      ? tr("读取任务详情后，再读取本次任务背景。", "Read task details, then this task's background.")
      : tr("查找只返回开放任务；候选不会自动选中。", "Find returns open tasks only; candidates are not auto-selected.");
    if (!dialog.open) dialog.showModal();
    $ma("ma-lookup-query").focus();
  }
  async function lookup() {
    if (!continuityTaskUsable()) {
      toast(tr("当前 Runtime 未提供任务接续能力。", "This Runtime does not provide task continuation."));
      return;
    }
    const query = $ma("ma-lookup-query").value.trim();
    if (!query) {
      $ma("ma-lookup-results").innerHTML = `<div class="ma-error">${esc(tr("请输入任务目标或任务 ID。", "Enter a task goal or task ID."))}</div>`;
      return;
    }
    if (state.lookupMode === "id") {
      await selectLookupTask(query);
      return;
    }
    const token = ++state.lookupToken;
    const root = $ma("ma-lookup-results");
    root.innerHTML = `<div class="ma-loading">${esc(tr("正在查找开放任务…", "Finding open tasks…"))}</div>`;
    try {
      const {data} = await api("continuity_task", {
        scope: safeScope(state.capability.scope),
        request: {operation: "find", query, top_k: 5},
      });
      if (token !== state.lookupToken) return;
      const tasks = data.tasks || [];
      root.innerHTML = tasks.length ? `<div class="ma-results">${tasks.map((task, index) => `<article class="ma-record"><div class="ma-record-head"><div class="ma-record-main"><strong>${esc(task.title || tr("未命名任务", "Untitled task"))}</strong><span class="ma-meta"><span>${esc(task.description || tr("暂无任务说明", "No task description"))}</span><span class="ma-pill green">${esc(task.state === "open" ? tr("开放", "Open") : task.state)}</span><span class="ma-pill">${esc(task.match_type || tr("匹配", "match"))}</span></span></div><button type="button" class="primary" data-ma-select-task="${index}">${esc(tr("查看任务", "Open task"))}</button></div></article>`).join("")}</div>` : `<div class="ma-empty"><strong>${esc(tr("未找到匹配的开放任务", "No matching open task"))}</strong><span>${esc(tr("已关闭任务可按已知 ID 打开。", "Closed tasks can be opened by known ID."))}</span></div>`;
      root.querySelectorAll("[data-ma-select-task]").forEach(button =>
        button.onclick = () => selectLookupTask(tasks[Number(button.dataset.maSelectTask)].task_id));
    } catch (error) {
      if (token === state.lookupToken) root.innerHTML = `<div class="ma-error">${esc(errorMessage(error))}</div>`;
    }
  }
  async function selectLookupTask(id) {
    const returnToRole = state.lookupReturnPage === "role";
    await openTask(id);
    if (returnToRole && state.task?.task_id === id) {
      setPage("role");
      await runRoles();
    }
  }
  $ma("ma-lookup-form").onsubmit = event => { event.preventDefault(); lookup(); };
  document.querySelectorAll("[data-ma-lookup]").forEach(button =>
    button.onclick = () => showLookup(button.dataset.maLookup, state.lookupReturnPage));

  function selectedRoleDuties() {
    if (!state.compare) return state.dutyMap.has(state.duty) ? [state.duty] : [];
    return unique(state.compareDuties.filter(duty => state.dutyMap.has(duty))).slice(0, 3);
  }

  function renderRole() {
    renderRoleScope();
    const root = $ma("ma-role-results");
    if (!root) return;
    if (!roleCatalogUsable()) {
      const reason = hasRestrictedContinuityScope()
        ? continuityScopeRestrictionText()
        : state.capability?.mode === "continuity"
          ? tr("当前 Runtime 为接续模式，职责检索不可用。", "This Runtime uses continuity mode; duty search is unavailable.")
          : catalogErrorText() || tr(`职责目录不可用：${state.capability?.role_catalog?.reason || "catalog_unavailable"}`, `Duty catalog unavailable: ${state.capability?.role_catalog?.reason || "catalog_unavailable"}`);
      root.innerHTML = `<div class="ma-empty"><strong>${esc(tr("职责检索已禁用", "Duty search is disabled"))}</strong><span>${esc(reason)}</span></div>`;
      return;
    }
    const scope = state.capability?.scope;
    const query = $ma("ma-role-query").value.trim();
    const hasInput = !!query || !!state.anchor || (state.roleScope === "task" && !!state.task);
    if (!hasInput) {
      root.innerHTML = `<div class="ma-empty"><strong>${esc(tr("尚未检索", "No search yet"))}</strong><span>${esc(state.roleScope === "task" ? tr("已选择任务背景", "Task context selected") : tr("输入想了解的问题，或从记忆详情继续查找。", "Enter a question or continue from a memory's details."))}</span></div>`;
      return;
    }
    const duties = selectedRoleDuties();
    const stale = (state.roleError ? `<div class="ma-error">${esc(state.roleError)}</div>` : "") + (state.catalogStale ? `<div class="ma-error">${esc(tr("职责规则已更新，请刷新并重新检索；当前结果保留供参考。", "Duty rules changed. Refresh and search again; current results remain for reference."))}</div>` : "");
    root.innerHTML = `${stale}<div class="ma-page-note">${esc(state.roleScope === "task" ? (state.task?.title || tr("当前任务", "Current task")) : state.capability.project_name)}${state.anchor ? ` · ${esc(tr("从所选记忆展开", "Expanded from selected memory"))} <button type="button" class="ma-link" id="ma-clear-anchor">${esc(tr("清除起点", "Clear anchor"))}</button>` : ""}</div><div class="${state.compare ? "ma-role-columns" : "ma-role-layout"}">${duties.map((duty, index) => renderRoleColumn(duty, index)).join("")}</div>`;
    if ($ma("ma-clear-anchor")) {
      $ma("ma-clear-anchor").onclick = () => {
        state.anchor = null;
        invalidateRoleRequests();
        state.roleResults = {};
        state.roleErrors = {};
        state.rolePending.clear();
        renderRole();
        if ($ma("ma-role-query").value.trim() || state.roleScope === "task") runRoles();
      };
    }
    duties.forEach((duty, index) => {
      const result = state.roleResults[duty];
      const column = root.querySelector(`[data-ma-duty-index="${index}"]`);
      const retry = column?.querySelector("[data-ma-retry-duty]");
      if (retry) retry.onclick = () => runRoles(duty);
      if (!result || !column) return;
      renderRecords(column.querySelector(".ma-role-records"), result.items || [], scope, result.paths || []);
      column.querySelectorAll('[id^="ma-gap-"]').forEach(button => button.onclick = () => openGaps([result]));
      bindPaths(column, result.items || [], scope, result.paths || []);
    });
  }
  function renderRoleColumn(duty, index) {
    const result = state.roleResults[duty];
    const error = state.roleErrors[duty];
    const pending = state.rolePending.has(duty);
    const title = dutyName(duty);
    if (!result) {
      return `<section class="ma-role-column" data-ma-duty-index="${index}" data-ma-duty-id="${esc(duty)}"><div class="ma-role-title"><h2>${esc(title)}</h2></div>${pending ? `<div class="ma-loading">${esc(tr("正在按此职责检索…", "Searching this duty…"))}</div>` : `<div class="ma-error">${esc(error || tr("本次检索失败，其他列仍可保留。", "This request failed; other columns can remain."))}</div><button type="button" class="ma-link" data-ma-retry-duty>${esc(tr("只重试此列", "Retry this column"))}</button>`}</section>`;
    }
    const supported = result.view_meta?.mode === "role" && result.view_meta?.schema_version === "role-view/2";
    return `<section class="ma-role-column" data-ma-duty-index="${index}" data-ma-duty-id="${esc(duty)}"><div class="ma-role-title"><h2>${esc(title)} ${esc(tr("视角", "view"))}</h2><small>${esc(tr(`${result.items.length} 条`, `${result.items.length} items`))}</small></div>${gapBanner(result, `ma-gap-${index}`)}${result.view_meta?.mode === "continuity" ? `<p class="ma-page-note">${esc(tr("当前服务只提供接续能力，未展开职责关系。", "This service exposes continuation without duty relation expansion."))}</p>` : !supported ? `<p class="ma-page-note">${esc(tr("当前结果暂不支持关系展示，已保留记忆正文。", "Relations are unavailable for this result; memory text is preserved."))}</p>` : ""}<div class="ma-role-records"></div><div class="ma-paths"></div><details class="ma-tech"><summary>${esc(tr("检索技术信息", "Retrieval technical details"))}</summary><pre>${esc(JSON.stringify(result.view_meta || {}, null, 2))}</pre></details></section>`;
  }
  function bindPaths(root, items, scope, paths) {
    const holder = root.querySelector(".ma-paths") || root;
    if (!paths?.length) { holder.innerHTML = `<p class="ma-page-note">${esc(tr("本次未返回关系路径。", "No relation paths returned."))}</p>`; return; }
    const pathNode = ref => `<button type="button" class="ma-path-node" data-ma-ref="${encodeURIComponent(JSON.stringify(ref))}">${esc(pathSourceText(ref))}</button>`;
    holder.innerHTML = `<details class="ma-path"><summary>${icon("branch")}${esc(tr(`关联依据 · ${paths.length} 条`, `Related evidence · ${paths.length}`))}</summary>${paths.map((path, index) => `<div class="ma-path-record"><div class="ma-timeline-meta"><strong>${esc(tr(`依据 ${index + 1}`, `Evidence ${index + 1}`))}</strong><span>${esc(dutyNames(path.valid_duties) || tr("跨职责", "Cross-duty"))}</span></div>${(path.steps || []).map(step => `${pathNode(step.source)}<div class="ma-path-edge"><strong>${esc(relationLabel(step.relation_type))}</strong>${relationDescription(step.relation_type) ? `<small>${esc(relationDescription(step.relation_type))}</small>` : ""}</div>${pathNode(step.target)}<div class="ma-conditions">${esc(viewStatus(step.status))}${step.conditions?.length ? ` · ${esc(step.conditions.join(" · "))}` : ""}</div>`).join("")}<details class="ma-tech"><summary>${esc(tr("关系技术信息", "Relation technical details"))}</summary><pre>${esc(JSON.stringify(path, null, 2))}</pre></details></div>`).join("")}</details>`;
    holder.querySelectorAll("[data-ma-ref]").forEach(button => button.onclick = () => {
      const ref = JSON.parse(decodeURIComponent(button.dataset.maRef));
      openReference(ref, items, scope, paths);
    });
  }
  const pathSourceText = (ref, pending = true) => {
    const unit = state.units.get(recordKey(ref.scope, ref.unit_id));
    const title = unit ? firstLine(unit.segments?.map(segment => segment.content).join("\n"))
      : tr("查看关联记忆", "View related memory");
    return `${title} · ${tr("来源：", "Source: ")}${sourceLabelForRef(ref, null, pending)}`;
  };
  function syncPathSources(reference) {
    const key = recordKey(reference.scope, reference.unit_id);
    document.querySelectorAll("#multi-agent [data-ma-ref]").forEach(button => {
      let ref;
      try { ref = JSON.parse(decodeURIComponent(button.dataset.maRef)); } catch { return; }
      if (recordKey(ref.scope, ref.unit_id) === key) button.textContent = pathSourceText(ref, false);
    });
  }
  async function runRoles(retryDuty = null) {
    if (!state.capability?.available || !roleCatalogUsable()) return;
    const query = $ma("ma-role-query").value.trim();
    state.roleScope = $ma("ma-role-scope").value;
    const duties = retryDuty ? [retryDuty] : selectedRoleDuties();
    if (!duties.length) { toast(tr("请选择有效职责。", "Choose a valid duty.")); return; }
    if (state.compare && !retryDuty && (duties.length < 2 || duties.length > 3)) {
      toast(tr("职责对照必须选择 2～3 项。", "Duty comparison requires 2-3 duties."));
      return;
    }
    if (state.roleScope === "task" && !state.task) { toast(tr("请先打开任务。", "Open a task first.")); return; }
    if (state.roleScope === "project" && !query && !state.anchor) { toast(tr("共享空间检索需要关键词或记忆起点。", "Shared space search needs a question or anchor.")); renderRole(); return; }
    if (!retryDuty) {
      invalidateRoleRequests();
      state.roleResults = {};
      state.roleErrors = {};
      state.rolePending.clear();
      state.roleError = "";
      state.catalogStale = false;
    }
    const isCurrentRequest = state.roleRequests.begin(duties);
    duties.forEach(duty => {
      state.rolePending.add(duty);
      delete state.roleErrors[duty];
    });
    renderRole();
    const payloadBase = {
      query, context: {scope: safeScope(state.capability.scope)},
      top_k: Number($ma("ma-role-limit").value), disclosure: "l2",
    };
    if (state.roleScope === "task") payloadBase.task_id = state.task.task_id;
    if (state.anchor) payloadBase.anchor = state.anchor;
    await Promise.all(duties.map(async duty => {
      const current = () => isCurrentRequest(duty);
      try {
        const {data} = await api("search", {...payloadBase, duty});
        if (!current()) return;
        if (data.view_meta?.policy_version && data.view_meta.policy_version !== state.policyVersion) {
          state.catalogStale = true;
        }
        state.roleResults[duty] = data;
        state.rolePending.delete(duty);
        renderRole();
        const hydration = hydrate(data.items || [], state.capability.scope);
        renderRole();
        await hydration;
      } catch (error) {
        if (!current()) return;
        state.roleErrors[duty] = errorMessage(error);
        state.rolePending.delete(duty);
      }
      if (current()) renderRole();
    }));
  }
  function openReference(reference, items, scope, paths) {
    const returned = items.find(item => item.unit_id === reference.unit_id && recordKey(referenceForItem(item, scope).scope, item.unit_id) === recordKey(reference.scope, reference.unit_id));
    openDetail(returned || {unit_id: reference.unit_id, abstract: "", content: "", view: null}, reference, scope, paths);
  }

  async function openDetail(item, reference = null, scope = null, paths = []) {
    const token = ++state.detailToken;
    state.detailItem = item;
    state.detailReference = reference || referenceForItem(item, scope || state.capability.scope);
    state.detailPaths = paths;
    state.detailUnit = null;
    state.detailError = "";
    const dialog = $ma("multi-agent-detail-dialog");
    if (!dialog.open) dialog.showModal();
    renderDetail();
    try {
      const {data: unit} = await api("get", {unit_id: state.detailReference.unit_id, scope: state.detailReference.scope});
      if (token !== state.detailToken) return;
      state.detailUnit = unit;
      state.units.set(recordKey(unit.scope, unit.id), unit);
      syncPathSources(state.detailReference);
      renderDetail();
    } catch (error) {
      if (token !== state.detailToken) return;
      state.detailError = errorMessage(error);
      renderDetail();
    }
  }
  function renderDetail() {
    const root = $ma("ma-detail-body");
    const item = state.detailItem;
    if (!root || !item) return;
    const unit = state.detailUnit;
    const metadata = unit?.system_metadata || item.system_metadata || {};
    const view = item.view;
    const original = unit ? (unit.segments || []).map(segment => segment.content).join("\n\n") : "";
    const paths = state.detailPaths || [];
    root.innerHTML = `
      <div class="row"><span class="badge">${esc(view ? viewKind(view.kind) : tr("原始记忆", "Original memory"))}</span>${view ? `<span class="badge">${esc(viewStatus(view.status))}</span>` : ""}</div>
      ${isRisk(item) ? `<section class="ma-projection"><strong>${esc(tr("本次视图风险提示", "View risk projection"))}</strong><p>${esc(item.content || item.abstract || "")}</p></section>` : ""}
      <h2 class="detail-heading">${esc(isRisk(item) ? tr("原始记忆正文", "Original memory body") : firstLine(original || item.content))}</h2>
      <p class="detail-body">${esc(original || (state.detailError ? "" : tr("正在读取原始记录…", "Reading original record…")))}</p>
      ${state.detailError ? `<div class="ma-error">${esc(tr(`完整记录读取失败：${state.detailError}`, `Could not read the original record: ${state.detailError}`))}</div>` : ""}
      ${view?.conditions?.length ? `<div class="ma-section"><h3>${esc(tr("本次视图适用条件", "View conditions"))}</h3>${view.conditions.map(value => `<span class="ma-pill">${esc(value)}</span>`).join(" ")}</div>` : ""}
      <div class="detail-actions"><button type="button" class="quiet small" id="ma-copy-detail" ${unit ? "" : "disabled"}>${esc(tr("复制内容", "Copy content"))}</button>${unit && metadata.continuity_task ? `<button type="button" id="ma-detail-task">${icon("folder")}${esc(tr("查看所属任务", "Open source task"))}</button>` : ""}${unit ? `<button type="button" id="ma-detail-anchor">${icon("branch")}${esc(tr("以此记忆展开职责依据", "Expand duty evidence from this memory"))}</button>` : ""}</div>
      ${unit ? `<hr class="detail-divider"><h3>${esc(tr("来源与记录", "Sources and record"))}</h3><dl class="ma-data"><dt>${esc(tr("来源平台", "Source platform"))}</dt><dd>${esc(sourceLabel(item, unit.scope))}</dd>${metadata.source_app && metadata.source_app.trim().toLowerCase() !== sourceLabel(item, unit.scope).trim().toLowerCase() ? `<dt>${esc(tr("原记录应用", "Recorded app"))}</dt><dd>${esc(metadata.source_app)}</dd>` : ""}<dt>${esc(tr("事件时间", "Event time"))}</dt><dd>${esc(nowDate(unit.temporal?.t_event))}</dd><dt>${esc(tr("记录时间", "Recorded"))}</dt><dd>${esc(nowDate(unit.temporal?.t_ingest))}</dd><dt>${esc(tr("状态", "Status"))}</dt><dd>${esc(({active: tr("有效", "Active"), superseded: tr("已纠正", "Superseded"), forgotten: tr("已遗忘", "Forgotten"), archived: tr("已归档", "Archived")})[unit.lifecycle] || tr("未记录", "Not recorded"))}</dd></dl>` : ""}
      ${view && (view.duty_ids?.length || view.valid_duties?.length) ? `<div class="ma-section"><h3>${esc(tr("职责归属", "Duty attribution"))}</h3><p>${esc(tr("条目职责：", "Item duties: "))}${esc(dutyNames(view.duty_ids) || tr("未标注", "Not tagged"))}</p><p>${esc(tr("本次有效职责：", "Valid duties: "))}${esc(dutyNames(view.valid_duties) || tr("跨职责或未确定", "Cross-duty or undetermined"))}</p></div>` : ""}
      <details class="ma-tech"><summary>${esc(tr("技术详情", "Technical details"))}</summary><pre>${esc(JSON.stringify({unit_id: item.unit_id, scope: unit?.scope || state.detailReference.scope, source_ref: unit?.source_ref, tags: unit?.tags, view: view || null, source_metadata: unit?.system_metadata || metadata}, null, 2))}</pre></details>
      ${state.staleRefs.has(recordKey(state.detailReference.scope, state.detailReference.unit_id)) ? `<p class="ma-error">${esc(tr("引用内容已变化，请重新检索。", "The referenced content changed. Search again."))}</p>` : ""}
    `;
    $ma("ma-copy-detail").onclick = copyDetail;
    if ($ma("ma-detail-task")) $ma("ma-detail-task").onclick = () => { $ma("multi-agent-detail-dialog").close(); openTask(metadata.continuity_task); };
    if ($ma("ma-detail-anchor")) $ma("ma-detail-anchor").onclick = async () => {
      state.anchor = await memoryRefOf(unit);
      $ma("ma-role-query").value = "";
      state.roleScope = state.task ? "task" : "project";
      $ma("ma-role-scope").value = state.roleScope;
      $ma("multi-agent-detail-dialog").close();
      invalidateRoleRequests();
      state.roleResults = {};
      state.roleErrors = {};
      state.rolePending.clear();
      setPage("role");
      runRoles();
    };
    if (state.detailPaths?.length) {
      const related = state.detailPaths.flatMap(path => path.steps || []).filter(step =>
        [step.source, step.target].some(ref => recordKey(ref.scope, ref.unit_id) === recordKey(state.detailReference.scope, item.unit_id)));
      if (related.length) {
        root.insertAdjacentHTML("beforeend", `<div class="ma-section"><h3>${esc(tr("已返回的关联依据", "Returned relation evidence"))}</h3>${related.map(step => `<button type="button" class="ma-path-node" data-ma-related="${encodeURIComponent(JSON.stringify(step.source.unit_id === item.unit_id ? step.target : step.source))}">${esc(relationLabel(step.relation_type))} · ${esc(tr("读取关联端点", "Read related endpoint"))}</button>`).join("")}</div>`);
        root.querySelectorAll("[data-ma-related]").forEach(button => button.onclick = () => openReference(JSON.parse(decodeURIComponent(button.dataset.maRelated)), [], state.detailReference.scope, state.detailPaths));
      }
    }
  }
  async function copyDetail() {
    if (!state.detailUnit) return;
    try {
      await navigator.clipboard.writeText((state.detailUnit.segments || []).map(segment => segment.content).join("\n\n"));
      toast(tr("已复制原始正文。", "Original body copied."));
    } catch {
      toast(tr("当前浏览器未开放剪贴板权限。", "Clipboard access is unavailable."));
    }
  }

  function openGaps(results) {
    const gaps = [];
    const seen = new Set();
    for (const result of results) {
      for (const gap of result?.gaps || []) {
        const key = `${gap.code}:${(gap.affected_item_ids || []).join(",")}:${gap.blocking}`;
        if (!seen.has(key)) { seen.add(key); gaps.push(gap); }
      }
    }
    const errors = results.flatMap(result => result?.errors || []);
    $ma("ma-gaps-body").innerHTML = `${gaps.length ? gaps.map(gap => `<div class="ma-gap-detail"><span class="ma-pill ${gap.blocking ? "red" : "amber"}">${esc(gap.blocking ? tr("阻塞缺口", "Blocking") : tr("覆盖提醒", "Coverage"))}</span> <strong>${esc(gapLabel(gap.code))}</strong><details class="ma-tech"><summary>${esc(tr("技术详情", "Technical details"))}</summary><pre>${esc(JSON.stringify(gap, null, 2))}</pre></details></div>`).join("") : `<p class="ma-page-note">${esc(tr("本次响应未报告缺口。", "This response reported no gaps."))}</p>`}${errors.length ? `<div class="ma-error">${esc(tr("部分检索通道失败，已保留成功结果。", "Some retrieval channels failed; successful results were kept."))}</div>` : ""}<p class="ma-page-note">${esc(tr("缺口不是任务安全或历史完整的证明。", "Gaps are not proof of task safety or complete history."))}</p>`;
    $ma("ma-gaps-dialog").showModal();
  }
  function openTraces() {
    $ma("ma-traces-body").innerHTML = state.traces.length ? state.traces.map((trace, index) => `<details class="ma-trace-item" ${index === 0 ? "open" : ""}><summary><code>POST /v1/${esc(trace.verb)}</code><span>${esc(trace.status || tr("未完成", "Pending"))} · ${esc(trace.ms)}ms</span></summary><div class="ma-page-note">${esc(tr("请求摘要", "Request summary"))}</div><pre>${esc(JSON.stringify(trace.summary, null, 2))}</pre><div class="ma-page-note">${esc(tr("状态", "Status"))}: ${esc(trace.status || tr("未完成", "Pending"))} · ${esc(tr("请求 ID", "Request ID"))}: ${esc(trace.requestId || tr("未提供", "Not provided"))}${trace.error ? ` · ${esc(trace.error)}` : ""}</div></details>`).join("") : `<p class="ma-page-note">${esc(tr("暂无请求记录。", "No request records."))}</p>`;
    $ma("ma-traces-dialog").showModal();
  }

  section.querySelectorAll("[data-ma-page]").forEach(button => button.onclick = () => setPage(button.dataset.maPage));
  $ma("ma-refresh-capability").onclick = () => loadCapability({refresh: true});
  $ma("ma-traces-open").onclick = openTraces;
  $ma("ma-role-form").onsubmit = event => { event.preventDefault(); runRoles(); };
  $ma("ma-role-scope").onchange = () => {
    if ($ma("ma-role-scope").value === "choose-task") {
      $ma("ma-role-scope").value = state.roleScope;
      showLookup("query", "role");
      return;
    }
    state.roleScope = $ma("ma-role-scope").value;
    state.anchor = null;
    invalidateRoleRequests();
    state.roleResults = {};
    state.roleErrors = {};
    state.rolePending.clear();
    renderRole();
    if ($ma("ma-role-query").value.trim() || state.roleScope === "task") runRoles();
  };
  $ma("ma-duty").onchange = () => {
    state.duty = $ma("ma-duty").value;
    invalidateRoleRequests();
    state.roleResults = {};
    state.roleErrors = {};
    state.rolePending.clear();
    renderDuties();
    renderRole();
    if ($ma("ma-role-query").value.trim() || state.anchor || (state.roleScope === "task" && state.task)) runRoles();
  };
  $ma("ma-compare").onchange = () => {
    state.compare = $ma("ma-compare").checked;
    if (state.compare && !state.compareDuties.length && state.dutyMap.has(state.duty)) {
      state.compareDuties = [state.duty];
    }
    invalidateRoleRequests();
    state.roleResults = {};
    state.roleErrors = {};
    state.rolePending.clear();
    renderDuties();
    renderRole();
  };
  $ma("ma-role-limit").onchange = () => { if ($ma("ma-role-query").value.trim() || state.anchor || (state.roleScope === "task" && state.task)) runRoles(); };
  document.querySelectorAll("#multi-agent-dialogs [data-ma-close]").forEach(button => button.onclick = () => $(button.dataset.maClose).close());
  document.querySelectorAll("#multi-agent-dialogs dialog").forEach(dialog => dialog.addEventListener("click", event => {
    if (event.target !== dialog) return;
    const rect = dialog.getBoundingClientRect();
    if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close();
  }));

  installNavigation();
  renderCurrent();
  window.MultiAgentPortal = {activate, applyLanguage: () => { localize(); renderCurrent(); }};
})();
