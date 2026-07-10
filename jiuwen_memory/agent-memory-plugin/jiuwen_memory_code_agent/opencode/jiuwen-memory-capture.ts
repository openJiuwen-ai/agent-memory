// jiuwen-memory OpenCode plugin — TypeScript Plugin SDK implementation.
//
// Drop this file into ~/.config/opencode/plugins/ and reference it in
// ~/.config/opencode/opencode.json:
//   { "plugin": ["./plugins/jiuwen-memory-capture.ts"] }
//
// Also register the MCP server in the same config:
//   { "mcp": { "jiuwen_memory": { "type": "local", "command": ["python", "-m", "jiuwen_memory.server.mcp_server"], "enabled": true } } }
//
// Requires: @opencode-ai/plugin types (provided by OpenCode at runtime).

import type { Plugin } from "@opencode-ai/plugin";

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------
const REST_URL = process.env.JIUWEN_MEMORY_URL || "http://localhost:8000";
const SECRET = process.env.JIUWEN_MEMORY_API_KEY || "";
const DEFAULT_USER_ID = process.env.JIUWEN_USER_ID || "opencode-user";
const DEBUG = process.env.JIUWEN_OPENCODE_DEBUG === "1";

const SEARCH_MEM_NUM = 5;
const SEARCH_SUMMARY_NUM = 3;
const DEFAULT_THRESHOLD = 0.3;

function authHeaders(): Record<string, string> {
  const h: Record<string, string> = { "Content-Type": "application/json" };
  if (SECRET) h["Authorization"] = `Bearer ${SECRET}`;
  return h;
}

// ---------------------------------------------------------------------------
// HTTP helpers
// ---------------------------------------------------------------------------
async function post(path: string, body: Record<string, unknown>, timeoutMs = 3000): Promise<void> {
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
    if (DEBUG) console.error(`[jiuwen] POST /${path} failed:`, (e as Error).message);
  }
}

async function postJson(path: string, body: Record<string, unknown>, timeoutMs = 0): Promise<unknown | null> {
  try {
    const opts: RequestInit = {
      method: "POST",
      headers: authHeaders(),
      body: JSON.stringify(body),
    };
    // Only set a timeout signal when explicitly requested; search calls pass 0
    // so they block until the server responds (no artificial abort).
    if (timeoutMs > 0) opts.signal = AbortSignal.timeout(timeoutMs);
    const res = await fetch(`${REST_URL}/${path}`, opts);
    if (res.ok) return await res.json();
  } catch (e) {
    if (DEBUG) console.error(`[jiuwen] POST /${path} (json) failed:`, (e as Error).message);
  }
  return null;
}

// ---------------------------------------------------------------------------
// High-level operations
// ---------------------------------------------------------------------------
async function addMessages(messages: Array<{ role: string; content: string }>, scopeId: string, userId = DEFAULT_USER_ID): Promise<void> {
  await post("add_messages/", { messages, user_id: userId, scope_id: scopeId });
}

/**
 * Combined search — calls /search_memory/ and /search_user_history_summary/,
 * formats results for system prompt injection.
 */
async function searchAndFormat(query: string, scopeId: string, userId = DEFAULT_USER_ID): Promise<string> {
  const [memResult, summaryResult] = await Promise.all([
    postJson("search_memory/", {
      query, num: SEARCH_MEM_NUM, user_id: userId, scope_id: scopeId, threshold: DEFAULT_THRESHOLD,
    }),
    postJson("search_user_history_summary/", {
      query, num: SEARCH_SUMMARY_NUM, user_id: userId, scope_id: scopeId, threshold: DEFAULT_THRESHOLD,
    }),
  ]);

  const memItems = ((memResult as any)?.results || []) as Array<Record<string, unknown>>;
  const summaryItems = ((summaryResult as any)?.results || []) as Array<Record<string, unknown>>;
  const lines: string[] = [];

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

// ---------------------------------------------------------------------------
// System prompt instructions
// ---------------------------------------------------------------------------
const JIUWEN_INSTRUCTIONS = `<jiuwen-memory-instructions>
You have access to jiuwen-memory for persistent cross-session memory. Use these tools proactively.

CORE TOOLS:

search_memories — Semantic search across long-term memory (user profile, episodic, semantic).
  Required: query (string). Optional: num (int, default 5), threshold (float, default 0.3).
  Use when: you need to recall past information about a user or topic.

search_history_summaries — Search past conversation summaries (topics discussed, conclusions reached).
  Required: query (string). Optional: num (int, default 3).
  Use when: you need higher-level context from past sessions. Call TOGETHER with search_memories.

add_messages — Add messages to long-term memory (auto-extracts profile/semantic/episodic/summary).
  Required: messages (array of {role, content}). Optional: infer (bool, default true).
  Use when: user says "remember this", or you want to explicitly save information.

update_memory — Overwrite a memory's text by mem_id.
delete_memory — Delete a single memory by mem_id.
  Use when: user says "forget this" or "delete that memory".

Always call search_memories AND search_history_summaries together for the fullest recall.
Never fabricate memory results — only present what the tools actually return.
</jiuwen-memory-instructions>`;

// ---------------------------------------------------------------------------
// Session state
// ---------------------------------------------------------------------------
let activeSessionId: string | null = null;
const DEFAULT_SCOPE_ID = process.env.JIUWEN_SCOPE_ID || "opencode-default";
let projectScopeId: string = DEFAULT_SCOPE_ID;
const contextInjectedSessions = new Set<string>();
/** Per-session store: the latest user query text, updated by chat.message. */
const sessionLastUserQuery = new Map<string, string>();
/** Per-session store: pending user query to be added to memory after AI finishes. */
const sessionPendingAdd = new Map<string, string>();
/**
 * Per-session cached search result. Written once by chat.message (blocking),
 * read many times by system.transform / compacting. NOT deleted on read —
 * persists until the next chat.message either overwrites it (non-empty result)
 * or clears it (empty result), so stale results never leak across turns.
 */
const sessionSearchResult = new Map<string, string>();

// ---------------------------------------------------------------------------
// Plugin
// ---------------------------------------------------------------------------
export const JiuwenMemoryCapturePlugin: Plugin = async (ctx) => {
  // Try to derive scope_id from ctx; fall back to env var or default.
  // OpenCode's ctx may not always provide worktree/project, so env override is preferred.
  const cwd = ctx.worktree || ctx.project?.id || "";
  if (cwd) {
    const raw = cwd.replace(/[\\/]+$/, "");
    const derived = raw.split(/[\\/]/).pop()?.trim();
    if (derived) projectScopeId = derived;
  }

  return {
    event: async ({ event }) => {
      const type = event.type;
      const props = (event as any).properties || {};

      // ── session.created ──
      if (type === "session.created") {
        const info = props.info as Record<string, unknown> | undefined;
        activeSessionId = (info?.id as string) || props.sessionID || null;
        if (!activeSessionId) return;
        contextInjectedSessions.delete(activeSessionId);
        sessionLastUserQuery.delete(activeSessionId);
        sessionPendingAdd.delete(activeSessionId);
        sessionSearchResult.delete(activeSessionId);
        // Probe health — jiuwen has no /session/start endpoint.
        try {
          await fetch(`${REST_URL}/health`, {
            method: "GET", headers: authHeaders(), signal: AbortSignal.timeout(800),
          });
        } catch {}
      }

      // ── session.deleted ──
      if (type === "session.deleted") {
        const sid = (props.info as any)?.id || props.sessionID || activeSessionId;
        if (sid) {
          if (sid === activeSessionId) activeSessionId = null;
          contextInjectedSessions.delete(sid);
          sessionLastUserQuery.delete(sid);
          sessionPendingAdd.delete(sid);
          sessionSearchResult.delete(sid);
        }
      }

      // ── message.updated (assistant) ──
      // AI 回复结束后，把之前存的用户 query 写入记忆（add_message 延后到这里）
      if (type === "message.updated") {
        const info = props.info as Record<string, unknown> | undefined;
        if (!info) return;
        if (info.role === "assistant") {
          const sid = props.sessionID || (info.sessionID as string) || activeSessionId;
          if (!sid) return;
          const pendingQuery = sessionPendingAdd.get(sid);
          if (!pendingQuery) return;
          // Add the user query to memory now (after AI has finished responding)
          sessionPendingAdd.delete(sid);
          await addMessages([{ role: "user", content: pendingQuery }], projectScopeId);
        }
      }
    },

    // ── chat.message ──
    // Store the user query, mark it pending for later add, AND run the search.
    // Search is blocking here — completes before system.transform fires.
    // The result is cached in sessionSearchResult so that system.transform can
    // reuse it on every invocation this turn without re-searching.
    "chat.message": async (input: any, output: any) => {
      const sid = input.sessionID || activeSessionId;
      if (!sid) return;
      const parts = output.parts || [];
      const textParts = parts.filter((p: any) => p.type === "text" && !p.synthetic && !p.ignored);
      const userText = textParts.map((p: any) => p.text || "").join("\n");
      if (!userText) return;

      const query = userText.slice(0, 2000);

      // Store query for compacting fallback
      sessionLastUserQuery.set(sid, query);

      // Mark as pending — will be added to memory after AI finishes responding
      sessionPendingAdd.set(sid, userText.slice(0, 8000));

      // Search once per user message, cache the result.
      // system.transform will read this cache instead of re-searching.
      const searchResult = await searchAndFormat(query, projectScopeId, DEFAULT_USER_ID);
      // Always update the cache — including clearing it when this turn's
      // search returns nothing. Otherwise a stale result from an earlier turn
      // would keep being injected into every subsequent system.transform until
      // the session ends, feeding the model irrelevant/misleading context.
      if (searchResult) sessionSearchResult.set(sid, searchResult);
      else sessionSearchResult.delete(sid);
    },

    // ── experimental.chat.system.transform ──
    // THE KEY HOOK: inject memory context + instructions into the system prompt.
    // This hook may fire multiple times per turn (e.g. when AI calls a tool,
    // a second AI turn is created and system.transform fires again).
    // We always read from the cache — never re-search — so multiple invocations
    // within the same turn reuse the same result without extra HTTP calls.
    "experimental.chat.system.transform": async (input: any, output: any) => {
      const sid = input.sessionID || activeSessionId;
      if (!sid) return;

      if (!Array.isArray(output.system)) return;

      // 1. Inject usage instructions (once per session)
      if (!contextInjectedSessions.has(sid)) {
        output.system.push(JIUWEN_INSTRUCTIONS);
        contextInjectedSessions.add(sid);
      }

      // 2. Inject cached search result (written by chat.message this turn).
      //    Read-only — do NOT delete. The cache persists until the next
      //    chat.message overwrites it, so repeated system.transform calls
      //    within the same turn all get the same result with zero extra searches.
      const cachedResult = sessionSearchResult.get(sid);
      if (cachedResult) {
        output.system.push(cachedResult);
      }
    },

    // ── experimental.session.compacting ──
    // Inject memories before compaction to prevent context loss.
    // Same cache strategy: reuse if available, otherwise search as fallback.
    "experimental.session.compacting": async (input: any, output: any) => {
      const sid = input.sessionID || activeSessionId;
      if (!sid) return;

      const cachedResult = sessionSearchResult.get(sid);
      const context = cachedResult || (
        sessionLastUserQuery.has(sid)
          ? await searchAndFormat(sessionLastUserQuery.get(sid)!, projectScopeId, DEFAULT_USER_ID)
          : ""
      );
      if (context && Array.isArray(output.context)) {
        output.context.push(context);
      }
    },

    // ── config ──
    config: async (input: any) => {
      if (DEBUG) {
        console.error("[jiuwen] config loaded:", { theme: input.theme, model: input.model });
      }
    },
  };
};
