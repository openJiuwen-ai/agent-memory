"use strict";

const toggle = document.querySelector("#protection-switch");
const refresh = document.querySelector("#refresh");
const status = document.querySelector("#status");
const error = document.querySelector("#error");
const config = document.querySelector("#config");
const configEmpty = document.querySelector("#config-empty");
const writeRedaction = document.querySelector("#write-redaction");
const policyForm = document.querySelector("#policy-form");
const policySection = document.querySelector("#policy-section");
const policySave = document.querySelector("#policy-save");
const policyDiscard = document.querySelector("#policy-discard");
const policyFeedback = document.querySelector("#policy-feedback");
const outbound = document.querySelector("#outbound-settings");
const categorySettings = document.querySelector("#category-settings");
let snapshot = null;
let busy = false;
let pendingPoll;
let draftRevision = null;
let draft = null;
let dirty = false;
let policyFailure = "";
let savedRevision = null;

const states = {
  enabled: "新会话保护已生效",
  disabled: "新会话保护已关闭",
  unavailable: "AgentServer 未回报保护状态",
};
const writeRedactionStates = {
  true: "已配置开启，尚未确认生效",
  false: "未配置开启",
};
const settings = {
  memory_backend: ["记忆后端", { jiuwen: "jiuwen memory" }],
  memory_writeback: ["自动写回", { false: "关闭" }],
  model_protocol: ["模型请求", { "openai-chat": "Chat Completions 文本" }],
  tool_scope: ["工具外发", { "authorized-text-mcp": "已授权的文本 MCP" }],
  approval: ["审批入口", { "original-conversation": "原会话" }],
};

function rpc(method, params = {}) {
  return new Promise((resolve, reject) => {
    const url = new URL("/ws", window.location.href);
    url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
    const socket = new WebSocket(url);
    const id = crypto.randomUUID();
    const timer = window.setTimeout(fail, 5000);
    function finish(callback, value) {
      clearTimeout(timer);
      socket.onclose = null;
      socket.close();
      callback(value);
    }
    function fail() {
      finish(reject, new Error("无法连接 Swarm Gateway，请刷新状态。"));
    }
    socket.onerror = fail;
    socket.onclose = fail;
    socket.onopen = () => socket.send(JSON.stringify({type: "req", id, method, params}));
    socket.onmessage = (event) => {
      const frame = JSON.parse(event.data);
      if (frame.type !== "res" || frame.id !== id) return;
      if (frame.ok && frame.payload.schema === 1) finish(resolve, frame.payload);
      else finish(reject, new Error(frame.error || "无法读取保护设置。"));
    };
  });
}

function render() {
  const state = snapshot?.status || "unavailable";
  status.dataset.state = state;
  status.textContent = state === "pending"
    ? "等待 AgentServer 应用设置"
    : states[state];
  toggle.setAttribute("aria-checked", String(snapshot?.effective === true));
  toggle.disabled = busy || !snapshot || state === "unavailable" || state === "pending";
  refresh.disabled = busy;
  const redaction = snapshot?.memory_write_redaction;
  let redactionText = writeRedactionStates[String(redaction?.configured)] ?? "无法确认";
  if (redaction?.effective === true) {
    redactionText = redaction.configured === false ? "已生效，当前配置未开启" : "已生效";
  }
  writeRedaction.textContent = redactionText;
  config.replaceChildren();
  for (const [key, [label, values]] of Object.entries(settings)) {
    const text = values[String(snapshot?.configuration?.[key])];
    if (text === undefined) continue;
    const row = document.createElement("div");
    const term = document.createElement("dt");
    const description = document.createElement("dd");
    term.textContent = label;
    description.textContent = text;
    row.append(term, description);
    config.append(row);
  }
  config.hidden = config.children.length === 0;
  configEmpty.hidden = !config.hidden;
  renderPolicy();
}

function element(tag, content, className) {
  const node = document.createElement(tag);
  if (content) node.textContent = content;
  if (className) node.className = className;
  return node;
}

function checkbox(label, value, update) {
  const control = element("label", "", "policy-control");
  const input = element("input");
  input.type = "checkbox";
  input.checked = value;
  input.addEventListener("change", () => { update(input.checked); editPolicy(); });
  control.append(input, document.createTextNode(label));
  return control;
}

function level(label, selected, update, observe = false) {
  const control = element("label", "", "policy-control");
  control.append(element("span", label, "policy-muted"));
  const select = element("select");
  const choices = observe
    ? [["observe", "仅识别"], ["S1", "S1"], ["S2", "S2"], ["S3", "S3"]]
    : [["S1", "S1"], ["S2", "S2"], ["S3", "S3"]];
  for (const [id, name] of choices) {
    const option = element("option", name);
    option.value = id;
    select.append(option);
  }
  select.value = selected;
  select.addEventListener("change", () => { update(select.value); editPolicy(); });
  control.append(select);
  return control;
}

function buildOutbound() {
  outbound.replaceChildren();
  for (const [key, title, description] of [
    ["model", "模型", "关闭后，新会话不会召回九问记忆。"],
    ["tools", "工具", "关闭后，已使用九问记忆的上下文不能向外部工具发送内容。"],
    ["agents", "子 Agent", "关闭后，已使用九问记忆的上下文不能在父子 Agent 间发送。"],
  ]) {
    const row = element("div", "", "setting-row policy-row");
    const info = element("div");
    info.append(element("h3", title), element("p", description));
    row.append(info, checkbox("允许" + title + "使用记忆", draft[key], (enabled) => {
      draft[key] = enabled;
    }));
    outbound.append(row);
  }
}

function buildCategories() {
  const previous = new Set([...categorySettings.querySelectorAll("details[open]")]
    .map((group) => group.dataset.group));
  categorySettings.replaceChildren();
  const groups = new Map();
  for (const item of snapshot.categories) {
    if (!groups.has(item.group)) groups.set(item.group, []);
    groups.get(item.group).push(item);
  }
  for (const [name, items] of groups) {
    const details = element("details", "", "category-group");
    details.dataset.group = name;
    details.open = previous.has(name);
    const count = element("span", "", "policy-muted");
    const summary = element("summary", name);
    summary.append(count);
    const updateCount = () => {
      count.textContent = `已选 ${items.filter(({id}) => draft.categories[id].enabled).length}/${items.length}`;
    };
    details.append(summary);
    for (const {id, label} of items) {
      const value = draft.categories[id];
      const row = element("div", "", "setting-row category-row");
      const rank = level(label + "分级", value.level, (selected) => {
        value.level = selected;
      }, id.startsWith("injection."));
      const select = rank.querySelector("select");
      rank.querySelector("span").classList.add("visually-hidden");
      select.disabled = !value.enabled;
      row.append(checkbox(label, value.enabled, (enabled) => {
        value.enabled = enabled;
        select.disabled = !enabled;
        updateCount();
      }), rank);
      details.append(row);
    }
    updateCount();
    categorySettings.append(details);
  }
}

function editPolicy() {
  dirty = true;
  policyFailure = "";
  renderPolicy();
}

function renderPolicy() {
  if (!snapshot?.policy && !draft) return;
  policySection.hidden = false;
  if (!dirty && snapshot?.policy && draftRevision !== snapshot.revision) {
    draft = structuredClone(snapshot.policy);
    draftRevision = snapshot.revision;
    buildOutbound();
    buildCategories();
  }
  const changedRemotely = snapshot && snapshot.revision !== draftRevision;
  policyForm.querySelectorAll("fieldset").forEach((field) => { field.disabled = busy; });
  policyDiscard.disabled = !dirty || busy;
  policySave.disabled = !dirty || busy || !snapshot || changedRemotely;
  const saved = !dirty && snapshot && snapshot.revision === savedRevision;
  const message = changedRemotely && dirty
    ? "设置已在其他窗口更新。放弃更改后可载入最新策略。"
    : policyFailure || (saved ? "策略已保存" : "");
  policyFeedback.hidden = !message;
  policyFeedback.dataset.state = saved && !policyFailure ? "saved" : "";
  policyFeedback.textContent = message;
}

async function request(method, params) {
  clearTimeout(pendingPoll);
  busy = true;
  toggle.disabled = true;
  refresh.disabled = true;
  error.hidden = true;
  const updatingPolicy = method === "plugin.mindvault.policy.set";
  if (updatingPolicy) policyFailure = "";
  renderPolicy();
  try {
    snapshot = await rpc(method, params);
    if (updatingPolicy) {
      dirty = false;
      savedRevision = snapshot.revision;
    }
  } catch (failure) {
    snapshot = null;
    if (updatingPolicy) policyFailure = failure.message;
    else {
      error.textContent = failure.message;
      error.hidden = false;
    }
  } finally {
    busy = false;
    render();
  }
  if (snapshot && (snapshot.status === "pending" || snapshot.status === "unavailable")) {
    pendingPoll = window.setTimeout(load, 2000);
  } else if (snapshot) {
    // Revalidate AgentServer liveness while the page stays open.
    pendingPoll = window.setTimeout(load, 8000);
  }
}

function load() {
  return request("plugin.mindvault.get");
}

refresh.addEventListener("click", load);
policyForm.addEventListener("submit", (event) => {
  event.preventDefault();
  if (policySave.disabled) return;
  void request("plugin.mindvault.policy.set", {policy: draft, revision: draftRevision});
});
policyDiscard.addEventListener("click", () => {
  dirty = false;
  draftRevision = null;
  policyFailure = "";
  if (snapshot) renderPolicy();
  else void load();
});
toggle.addEventListener("click", () => {
  if (toggle.disabled || !snapshot) return;
  void request("plugin.mindvault.set", {
    enabled: !snapshot.effective,
    revision: snapshot.revision,
  });
});
void load();
