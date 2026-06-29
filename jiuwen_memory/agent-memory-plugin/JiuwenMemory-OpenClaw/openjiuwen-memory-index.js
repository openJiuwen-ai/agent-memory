import {
  buildConfig,
  addMessages,
  searchMemory,
  extractText,
  searchUserHistorySummary,
  buildMemoryPrompt,
  USER_QUERY_MARKER
} from "./lib/openjiuwen-memory-api.js";

let lastCaptureTime = 0;
const conversationCounters = new Map();
const ENV_FILE_SEARCH_HINTS = ["~/.openclaw/.env", "~/.moltbot/.env", "~/.clawdbot/.env"];
const MEMORY_SOURCE = "openclaw";

function warnMissingApiKey(log, context) {
  const heading = "[memory] Missing API key";
  const header = `${heading}${context ? `; ${context} skipped` : ""}. Configure it with:`;
  log.warn?.(
    [
      header,
      "echo 'export MEMORY_API_KEY=\"your-api-key\"' >> ~/.zshrc",
      "source ~/.zshrc",
      "or",
      "echo 'export MEMORY_API_KEY=\"your-api-key\"' >> ~/.bashrc",
      "source ~/.bashrc",
    ].join("\n"),
  );
}

function stripPrependedPrompt(content) {
  if (!content) return content;
  const idx = content.lastIndexOf(USER_QUERY_MARKER);
  if (idx === -1) return content;
  return content.slice(idx + USER_QUERY_MARKER.length).trimStart();
}

function getCounterSuffix(sessionKey) {
  if (!sessionKey) return "";
  const current = conversationCounters.get(sessionKey) ?? 0;
  return current > 0 ? `#${current}` : "";
}

function bumpConversationCounter(sessionKey) {
  if (!sessionKey) return;
  const current = conversationCounters.get(sessionKey) ?? 0;
  conversationCounters.set(sessionKey, current + 1);
}

function resolveScopeId(cfg, ctx) {
  if (cfg.scopeId) return cfg.scopeId;
  const base = ctx?.sessionKey || ctx?.sessionId || (ctx?.agentId ? `openclaw:${ctx.agentId}` : "");
  const dynamicSuffix = cfg.scopeSuffixMode === "counter" ? getCounterSuffix(ctx?.sessionKey) : "";
  const prefix = cfg.scopeIdPrefix || "";
  const suffix = cfg.scopeIdSuffix || "";
  if (base) return `${prefix}${base}${dynamicSuffix}${suffix}`;
  return `${prefix}openclaw-${Date.now()}${dynamicSuffix}${suffix}`;
}

function truncate(text, maxLen) {
  if (!text) return "";
  if (!maxLen) return text;
  return text.length > maxLen ? `${text.slice(0, maxLen)}...` : text;
}

function pickLastTurnMessages(messages, cfg) {
  const lastUserIndex = messages
    .map((m, idx) => ({ m, idx }))
    .filter(({ m }) => m?.role === "user")
    .map(({ idx }) => idx)
    .pop();

  if (lastUserIndex === undefined) return [];

  const slice = messages.slice(lastUserIndex);
  const results = [];

  for (const msg of slice) {
    if (!msg || !msg.role) continue;
    if (msg.role === "user") {
      const content = stripPrependedPrompt(extractText(msg.content));
      if (content) results.push({ role: "user", content: truncate(content, cfg.maxMessageChars) });
      continue;
    }
    if (msg.role === "assistant" && cfg.includeAssistant) {
      const content = extractText(msg.content);
      if (content) results.push({ role: "assistant", content: truncate(content, cfg.maxMessageChars) });
    }
  }

  return results;
}

function pickFullSessionMessages(messages, cfg) {
  const results = [];
  for (const msg of messages) {
    if (!msg || !msg.role) continue;
    if (msg.role === "user") {
      const content = stripPrependedPrompt(extractText(msg.content));
      if (content) results.push({ role: "user", content: truncate(content, cfg.maxMessageChars) });
    }
    if (msg.role === "assistant" && cfg.includeAssistant) {
      const content = extractText(msg.content);
      if (content) results.push({ role: "assistant", content: truncate(content, cfg.maxMessageChars) });
    }
  }
  return results;
}

export default {
  id: "openjiuwen-memory-index",
  name: "Memory OpenClaw Plugin",
  description: "Memory recall + add memory via lifecycle hooks",
  kind: "lifecycle",

  register(api) {
    const cfg = buildConfig(api.pluginConfig);
    const log = api.logger ?? console;

    if (!cfg.envFileStatus?.found) {
      const searchPaths = cfg.envFileStatus?.searchPaths?.join(", ") ?? ENV_FILE_SEARCH_HINTS.join(", ");
      log.warn?.(`[memory] No .env found in ${searchPaths}; falling back to process env or plugin config.`);
    }

    // 注册 counter 模式的新会话 hook
    if (cfg.scopeSuffixMode === "counter" && cfg.resetOnNew) {
      if (api.config?.hooks?.internal?.enabled !== true) {
        log.warn?.("[memory] command:new hook requires hooks.internal.enabled = true");
      }
      api.registerHook(
        ["command:new"],
        (event) => {
          if (event?.type === "command" && event?.action === "new") {
            bumpConversationCounter(event.sessionKey);
          }
        },
        {
          name: "memory-conversation-new",
          description: "Increment memory conversation suffix on /new",
        },
      );
    }

    // before_agent_start: 搜索记忆
    api.on("before_agent_start", async (event, ctx) => {
      if (!cfg.searchEnabled) return;
      if (!event?.prompt || event.prompt.length < 3) return;

      const scopeId = resolveScopeId(cfg, ctx);
      let memoryResult = null;
      let summaryResult = null;

      try {
        // 1. 搜索普通记忆
        memoryResult = await searchMemory(
          cfg,
          event.prompt,
          {
            num: cfg.num ?? 10,
            threshold: cfg.threshold ?? 0.3,
            userId: cfg.userId,
            scopeId: cfg.recallGlobal ? "" : scopeId,
          }
        );
      } catch (err) {
        log.warn?.(`[memory] searchMemory failed: ${String(err)}`);
      }

      try {
        // 2. 搜索用户历史摘要
        summaryResult = await searchUserHistorySummary(
          cfg,
          event.prompt,
          {
            num: cfg.summaryNum ?? 5,
            threshold: cfg.summaryThreshold ?? 0.3,
            userId: cfg.userId,
            scopeId: cfg.recallGlobal ? "" : scopeId,
          }
        );
      } catch (err) {
        log.warn?.(`[memory] searchUserHistorySummary failed: ${String(err)}`);
      }

      // 使用 buildMemoryPrompt 构建系统提示词
      const promptText = buildMemoryPrompt(memoryResult, summaryResult, {
        maxItemChars: cfg.maxMessageChars,
        currentTime: new Date(),
      });

      if (!promptText) return;

      return {
        prependContext: promptText,
      };
    });

    // agent_end: 添加记忆
    api.on("agent_end", async (event, ctx) => {
      if (!cfg.addEnabled) return;
      if (!event?.success || !event?.messages?.length) return;

      const now = Date.now();
      if (cfg.throttleMs && now - lastCaptureTime < cfg.throttleMs) {
        return;
      }
      lastCaptureTime = now;

      const scopeId = resolveScopeId(cfg, ctx);

      try {
        const messages =
          cfg.captureStrategy === "full_session"
            ? pickFullSessionMessages(event.messages, cfg)
            : pickLastTurnMessages(event.messages, cfg);

        if (!messages.length) return;

        await addMessages(
          cfg,
          messages,
          {
            userId: cfg.userId,
            scopeId: cfg.recallGlobal ? "" : scopeId,
          }
        );
      } catch (err) {
        log.warn?.(`[memory] add failed: ${String(err)}`);
      }
    });
  },
};
