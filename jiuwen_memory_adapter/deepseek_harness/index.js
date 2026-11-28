// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
// @jiuwen/dsh-memory: recall/capture through the local jiuwen-memory service.
//
// The plugin never owns memory state: it discovers the machine's service, verifies the
// instance nonce, and forwards one turn at a time. Any memory failure is a clean no-op
// so a DSH turn is never interrupted.

import { existsSync, readFileSync, readdirSync, statSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

export const CONTEXT_NAME = "jiuwen-memory";
export const CONTEXT_OPEN = "<agent-memory-context>";
export const CONTEXT_CLOSE = "</agent-memory-context>";
export const NEUTRAL_OPEN = "‹agent-memory-context›";
export const NEUTRAL_CLOSE = "‹/agent-memory-context›";
export const DEFAULT_HOME = join(homedir(), ".jiuwen-memory");
export const RECALL_TIMEOUT_MS = 2000;

export function resolveHome(env = process.env) {
  return env.JIUWEN_MEMORY_HOME || DEFAULT_HOME;
}

function readJson(path) {
  try {
    return JSON.parse(readFileSync(path, "utf8"));
  } catch {
    return null;
  }
}

// One service per data directory; the discovery file names the live instance.
export function readDiscovery(home) {
  const discovery = readJson(join(home, "run", "service.json"));
  if (!discovery || typeof discovery.port !== "number" || !discovery.instance_nonce) {
    return null;
  }
  return discovery;
}

export function readInstallToken(home, installId, env = process.env) {
  const wanted = installId || env.JIUWEN_MEMORY_INSTALL || "";
  if (wanted) {
    const path = join(home, "installs", wanted, "token");
    return existsSync(path) ? readFileSync(path, "utf8").trim() : "";
  }
  // No explicit install: accept exactly one connected dsh install, as the CLI does.
  const installs = join(home, "installs");
  if (!existsSync(installs)) return "";
  const matches = [];
  for (const name of readdirSync(installs)) {
    const record = readJson(join(installs, name, "install.json"));
    const tokenPath = join(installs, name, "token");
    if (record && record.host === "dsh" && record.state === "connected" && existsSync(tokenPath)) {
      matches.push(readFileSync(tokenPath, "utf8").trim());
    }
  }
  return matches.length === 1 ? matches[0] : "";
}

export function buildRecallRequest({ hostSessionId, turnId, prompt, project, duty, topK }) {
  return {
    host_session_id: hostSessionId,
    turn_id: turnId,
    prompt,
    project: project || undefined,
    duty: duty || undefined,
    top_k: topK || undefined,
  };
}

export function buildCaptureRequest({ hostSessionId, turnId, prompt, answer }) {
  return { host_session_id: hostSessionId, turn_id: turnId, prompt, answer };
}

// Recalled text must not close the wrapper or forge a new block.
export function neutralizeMarkers(text) {
  return String(text)
    .replaceAll(CONTEXT_OPEN, NEUTRAL_OPEN)
    .replaceAll(CONTEXT_CLOSE, NEUTRAL_CLOSE);
}

// The plugin injects only the rendered memory text; the service decides what is written.
export function renderContext(view, notice) {
  const lines = [];
  if (notice) lines.push(notice);
  for (const item of (view && view.items) || []) {
    const content = neutralizeMarkers(String(item.content || "").trim());
    if (content) lines.push(`- ${content}`);
  }
  const text = lines.join("\n");
  if (!text) return "";
  return `${CONTEXT_OPEN}\n${text}\n${CONTEXT_CLOSE}`;
}

export function parseContexts(payload) {
  const contexts = payload && payload.contexts;
  return Array.isArray(contexts) ? contexts : null;
}

export function isHumanMessage(event) {
  if (!event || typeof event !== "object") return false;
  if (event.source !== "user") return false;
  const type = event.type || event.event || "";
  return type === "user/message" || type === "message/user";
}

// turn/end keeps only the human prompt and the final answer; tool and injected
// memory events never become memory text.
export function pickTurnMessages(events) {
  let prompt = "";
  let answer = "";
  for (const event of events || []) {
    if (isHumanMessage(event)) {
      const text = event.text || event.content || "";
      if (typeof text === "string" && text.trim()) prompt = text;
      continue;
    }
    const type = (event && (event.type || event.event)) || "";
    const role = event && event.role;
    if ((type === "assistant/message" || type === "message/assistant" || role === "assistant")
        && typeof (event.text || event.content) === "string") {
      answer = event.text || event.content;
    }
  }
  return { prompt: prompt.trim(), answer };
}

async function post(baseUrl, token, path, body, fetchImpl = fetch, timeoutMs = 5000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetchImpl(`${baseUrl}${path}`, {
      method: "POST",
      headers: { "content-type": "application/json", authorization: `Bearer ${token}` },
      body: JSON.stringify(body),
      signal: controller.signal,
    });
    if (!response.ok) return null;
    return await response.json();
  } finally {
    clearTimeout(timer);
  }
}

export async function ensureService(home, { spawnSyncImpl } = {}) {
  if (readDiscovery(home)) return readDiscovery(home);
  const spawn = spawnSyncImpl || (await import("node:child_process")).spawnSync;
  try {
    spawn("jiuwen-memory", ["service", "ensure", "--home", home], { timeout: 10000 });
  } catch {
    // Best effort: a missing CLI is a no-op, not a turn failure.
  }
  return readDiscovery(home);
}

export function createPlugin({ env = process.env, fetchImpl = fetch, spawnSyncImpl, home } = {}) {
  const dataHome = home || resolveHome(env);

  async function connection(callFetch) {
    const discovery = await ensureService(dataHome, { spawnSyncImpl });
    if (!discovery) return null;
    const token = readInstallToken(dataHome, env.JIUWEN_MEMORY_INSTALL, env);
    if (!token) return null;
    // A stale discovery file must not send this turn to another instance.
    try {
      const response = await callFetch(`http://127.0.0.1:${discovery.port}/local/status`, {
        method: "POST",
        headers: { authorization: `Bearer ${token}` },
      });
      if (response.ok) {
        const status = await response.json();
        if (status.instance_nonce && status.instance_nonce !== discovery.instance_nonce) {
          return null;
        }
      }
    } catch {
      return null;
    }
    return { discovery, token, callFetch };
  }

  async function assemble(payload) {
    const contexts = parseContexts(payload);
    if (!contexts) return payload;
    const state = await connection(fetchImpl);
    if (!state) return payload;
    const prompt = typeof payload.prompt === "string" ? payload.prompt : "";
    if (!prompt.trim()) return payload;
    const body = buildRecallRequest({
      hostSessionId: String(payload.session_id || payload.sessionId || "dsh"),
      turnId: String(payload.turn_id || payload.turnId || ""),
      prompt,
      project: payload.project,
      duty: payload.duty,
      topK: payload.top_k,
    });
    let result;
    try {
      result = await post(
        `http://127.0.0.1:${state.discovery.port}`,
        state.token,
        "/local/hook/recall",
        body,
        state.callFetch,
        RECALL_TIMEOUT_MS,
      );
    } catch {
      return payload;
    }
    if (!result) return payload;
    const text = renderContext(result.view, result.notice);
    if (!text) return payload;
    contexts.push({ name: CONTEXT_NAME, content: text });
    return payload;
  }

  async function turnEnd(payload) {
    const events = (payload && (payload.events || payload.messages)) || [];
    const { prompt, answer } = pickTurnMessages(events);
    const turnId = String((payload && (payload.turn_id || payload.turnId)) || "");
    const sessionId = String((payload && (payload.session_id || payload.sessionId)) || "dsh");
    if (!prompt || !answer || !turnId) return payload;
    const state = await connection(fetchImpl);
    if (!state) return payload;
    try {
      await post(
        `http://127.0.0.1:${state.discovery.port}`,
        state.token,
        "/local/hook/capture",
        buildCaptureRequest({ hostSessionId: sessionId, turnId, prompt, answer }),
        state.callFetch,
      );
    } catch {
      // A failed capture is reported by the service on the next turn, never here.
    }
    return payload;
  }

  return {
    name: CONTEXT_NAME,
    hooks: {
      "system-prompt/assemble": assemble,
      "turn/end": turnEnd,
    },
  };
}

export default createPlugin;
