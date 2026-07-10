// jiuwen-memory CodeAgent plugin — shared utilities.
//
// All hook scripts import from this module for:
//   - REST URL / auth headers / timeouts
//   - resolveProject(cwd) — git toplevel basename for scope isolation
//   - addMessages() / searchAndFormat() / health() — HTTP calls to memory_server
//   - Common constants

import { execSync } from "node:child_process";
import { basename } from "node:path";

// ---------------------------------------------------------------------------
// Platform detection
//
// Claude Code and Codex share this one script file, but inject different
// env vars when invoking hooks:
//   - Claude Code sets CLAUDE_PLUGIN_ROOT (hooks.json uses ${CLAUDE_PLUGIN_ROOT})
//   - Codex sets PLUGIN_ROOT (hooks.codex.json uses ${PLUGIN_ROOT})
//
// We use this to derive a per-platform default user_id so the two agents
// don't share/cross-pollute each other's memories. An explicit JIUWEN_USER_ID
// always wins.
// ---------------------------------------------------------------------------
export function detectPlatform() {
  if (process.env.CLAUDE_PLUGIN_ROOT) return "claude-code";
  if (process.env.PLUGIN_ROOT) return "codex";
  return "unknown";
}

const PLATFORM_USER_ID = {
  "claude-code": "cc-user",
  "codex": "codex-user",
  "unknown": "__default__",
};

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------
export const REST_URL = process.env.JIUWEN_MEMORY_URL || "http://localhost:8000";
export const SECRET = process.env.JIUWEN_MEMORY_API_KEY || "";
export const DEFAULT_USER_ID =
  process.env.JIUWEN_USER_ID || PLATFORM_USER_ID[detectPlatform()];
export const DEBUG = process.env.JIUWEN_CODEAGENT_DEBUG === "1";

export const SEARCH_MEM_NUM = 5;
export const SEARCH_SUMMARY_NUM = 3;
export const DEFAULT_THRESHOLD = 0.3;
export const MAX_TRUNCATE = 8000;

// ---------------------------------------------------------------------------
// Auth
// ---------------------------------------------------------------------------
export function authHeaders() {
  const h = { "Content-Type": "application/json" };
  if (SECRET) h["Authorization"] = `Bearer ${SECRET}`;
  return h;
}

// ---------------------------------------------------------------------------
// Project resolution — mirrors agentmemory's resolveProject().
// scope_id = project basename (git toplevel or cwd), ensuring per-project isolation.
// ---------------------------------------------------------------------------
export function resolveProject(cwd) {
  const explicit = process.env["JIUWEN_MEMORY_PROJECT_NAME"];
  if (explicit && explicit.trim()) return explicit.trim();
  const dir = cwd && cwd.trim() ? cwd : process.cwd();
  try {
    const top = execSync("git rev-parse --show-toplevel", {
      cwd: dir,
      stdio: ["ignore", "pipe", "ignore"],
      timeout: 500,
    }).toString().trim();
    if (top) return basename(top);
  } catch {}
  return basename(dir);
}

// ---------------------------------------------------------------------------
// HTTP helpers
// ---------------------------------------------------------------------------
export async function post(path, body, timeoutMs = 3000) {
  try {
    const res = await fetch(`${REST_URL}/${path}`, {
      method: "POST",
      headers: authHeaders(),
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(timeoutMs),
    });
    // Fire-and-forget: we don't throw on 4xx/5xx (caller may not catch),
    // but surface the HTTP status in DEBUG so silent failures are diagnosable.
    if (DEBUG && !res.ok) console.error(`[jiuwen] POST /${path} returned ${res.status}`);
  } catch (e) {
    if (DEBUG) console.error(`[jiuwen] POST /${path} failed:`, e?.message || e);
  }
}

export async function postJson(path, body, timeoutMs = 3000) {
  try {
    const res = await fetch(`${REST_URL}/${path}`, {
      method: "POST",
      headers: authHeaders(),
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(timeoutMs),
    });
    if (res.ok) return await res.json();
  } catch (e) {
    if (DEBUG) console.error(`[jiuwen] POST /${path} (json) failed:`, e?.message || e);
  }
  return null;
}

// ---------------------------------------------------------------------------
// jiuwen memory_server endpoints (trailing slash matches FastAPI declarations)
// ---------------------------------------------------------------------------
const EP_ADD_MESSAGES = "add_messages/";
const EP_SEARCH_MEMORY = "search_memory/";
const EP_SEARCH_SUMMARY = "search_user_history_summary/";
const EP_HEALTH = "health";

// ---------------------------------------------------------------------------
// High-level operations
// ---------------------------------------------------------------------------
export async function addMessages(messages, scopeId, userId = DEFAULT_USER_ID) {
  await post(EP_ADD_MESSAGES, {
    messages,
    user_id: userId,
    scope_id: scopeId,
  }, 3000);
}

/**
 * Combined search — calls /search_memory/ and /search_user_history_summary/,
 * merges results into a formatted context string for stdout injection
 * (SessionStart / PreCompact hooks).
 */
export async function searchAndFormat(query, scopeId, userId = DEFAULT_USER_ID) {
  const [memResult, summaryResult] = await Promise.all([
    postJson(EP_SEARCH_MEMORY, {
      query,
      num: SEARCH_MEM_NUM,
      user_id: userId,
      scope_id: scopeId,
      threshold: DEFAULT_THRESHOLD,
    }),
    postJson(EP_SEARCH_SUMMARY, {
      query,
      num: SEARCH_SUMMARY_NUM,
      user_id: userId,
      scope_id: scopeId,
      threshold: DEFAULT_THRESHOLD,
    }),
  ]);

  const memItems = (memResult?.results || []);
  const summaryItems = (summaryResult?.results || []);
  const lines = [];

  if (memItems.length) {
    lines.push("## Related Memories");
    for (const r of memItems) {
      const label = r.type ? `[${r.type}]` : "";
      lines.push(`- ${label} ${String(r.content || "").slice(0, 300)} (score: ${Number(r.score || 0).toFixed(2)})`);
    }
  }

  if (summaryItems.length) {
    if (lines.length) lines.push("");
    lines.push("## Related History Summaries");
    for (const r of summaryItems) {
      lines.push(`- ${String(r.content || "").slice(0, 300)} (score: ${Number(r.score || 0).toFixed(2)})`);
    }
  }

  return lines.join("\n");
}

export async function healthCheck() {
  try {
    const res = await fetch(`${REST_URL}/${EP_HEALTH}`, {
      method: "GET",
      headers: authHeaders(),
      signal: AbortSignal.timeout(800),
    });
    return res.ok;
  } catch {
    return false;
  }
}

// ---------------------------------------------------------------------------
// SDK child guard — prevents sub-agents from double-capturing.
// jiuwen has no SDK child mechanism, but we keep the check for forward compat.
// ---------------------------------------------------------------------------
export function isSdkChildContext(payload) {
  if (process.env.JIUWEN_SDK_CHILD === "1") return true;
  if (!payload || typeof payload !== "object") return false;
  return payload.entrypoint === "sdk-ts";
}

// ---------------------------------------------------------------------------
// Output format — auto-detect from environment.
//
// Claude Code / Codex: stdout is plain text → injected directly into LLM context.
// Cursor: stdout must be JSON with specific fields → Cursor parses them.
//
// The CURSOR_PLUGIN_ROOT env var is set by Cursor when it invokes hooks.
// If it's present, we switch to JSON output mode automatically.
// ---------------------------------------------------------------------------
export const IS_CURSOR = !!process.env.CURSOR_PLUGIN_ROOT;

/**
 * Format stdout output for the current platform.
 *
 * - Claude Code / Codex: returns the raw text string.
 * - Cursor sessionStart: returns {"additional_context":"<text>"} JSON.
 * - Cursor preCompact / beforeSubmitPrompt / userPromptSubmit: returns {"user_message":"<text>"} JSON.
 */
export function formatOutput(text, eventType = "generic") {
  if (!text) {
    return IS_CURSOR ? "{}" : "";
  }
  if (!IS_CURSOR) return text;

  // Cursor JSON format depends on event type
  if (eventType === "sessionStart") {
    return JSON.stringify({ additional_context: text });
  }
  if (eventType === "preCompact" || eventType === "beforeSubmitPrompt" || eventType === "userPromptSubmit") {
    // beforeSubmitPrompt also needs "continue":true
    const obj = eventType === "beforeSubmitPrompt"
      ? { continue: true, user_message: text }
      : { user_message: text };
    return JSON.stringify(obj);
  }
  // postToolUse and other events: empty JSON
  return "{}";
}

// ---------------------------------------------------------------------------
// Utility
// ---------------------------------------------------------------------------
export function truncate(value, max = MAX_TRUNCATE) {
  if (typeof value === "string" && value.length > max) return value.slice(0, max) + "\n[...truncated]";
  if (value && typeof value === "object") {
    try {
      const s = JSON.stringify(value);
      if (s.length > max) return s.slice(0, max) + "...[truncated]";
    } catch {}
  }
  return value;
}

export function safeSlice(v, max) {
  if (typeof v === "string") return v.slice(0, max);
  if (v == null) return "";
  try { return JSON.stringify(v).slice(0, max); } catch { return ""; }
}

export function coerceText(content) {
  if (!content) return "";
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content
      .map(b => typeof b === "string" ? b : (b?.text || b?.content || ""))
      .filter(Boolean)
      .join(" ");
  }
  return String(content);
}
