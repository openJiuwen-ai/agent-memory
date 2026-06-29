import { readFileSync } from "node:fs";
import { join } from "node:path";
import { homedir } from "node:os";
import { setTimeout as delay } from "node:timers/promises";

// 默认的 Memory Server API 地址
// 本地开发使用: http://127.0.0.1:8000
const DEFAULT_BASE_URL = process.env.MEMORY_BASE_URL || "http://127.0.0.1:8000";

// 环境变量源配置
const ENV_SOURCES = [
  { name: "openclaw", path: join(homedir(), ".openclaw", ".env") },
  { name: "moltbot", path: join(homedir(), ".moltbot", ".env") },
  { name: "clawdbot", path: join(homedir(), ".clawdbot", ".env") },
];

let envFilesLoaded = false;
const envFileContents = new Map();
const envFileValues = new Map();

/**
 * 去除字符串的引号
 */
function stripQuotes(value) {
  if (!value) return value;
  const trimmed = value.trim();
  if (
    (trimmed.startsWith('"') && trimmed.endsWith('"')) ||
    (trimmed.startsWith("'") && trimmed.endsWith("'"))
  ) {
    return trimmed.slice(1, -1);
  }
  return trimmed;
}

/**
 * 从结果中提取数据
 */
function extractResultData(result) {
  if (!result || typeof result !== "object") return null;
  return result.data ?? result.data?.data ?? result.data?.result ?? null;
}

/**
 * 补零
 */
function pad2(value) {
  return String(value).padStart(2, "0");
}

/**
 * 格式化时间
 */
function formatTime(value) {
  if (value === undefined || value === null || value === "") return "";
  if (typeof value === "number") {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "";
    return `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())} ${pad2(
      date.getHours(),
    )}:${pad2(date.getMinutes())}`;
  }
  if (typeof value === "string") {
    const trimmed = value.trim();
    if (!trimmed) return "";
    if (/^\d+$/.test(trimmed)) return formatTime(Number(trimmed));
    return trimmed;
  }
  return "";
}

/**
 * 解析环境变量文件
 */
function parseEnvFile(content) {
  const values = new Map();
  for (const line of content.split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;
    const idx = trimmed.indexOf("=");
    if (idx <= 0) continue;
    const key = trimmed.slice(0, idx).trim();
    const rawValue = trimmed.slice(idx + 1);
    if (!key) continue;
    values.set(key, stripQuotes(rawValue));
  }
  return values;
}

/**
 * 加载环境变量文件
 */
function loadEnvFiles() {
  if (envFilesLoaded) return;
  envFilesLoaded = true;
  for (const source of ENV_SOURCES) {
    try {
      const content = readFileSync(source.path, "utf-8");
      envFileContents.set(source.name, content);
      envFileValues.set(source.name, parseEnvFile(content));
    } catch {
      // ignore missing files
    }
  }
}

/**
 * 从文件中加载环境变量
 */
function loadEnvFromFiles(name) {
  for (const source of ENV_SOURCES) {
    const values = envFileValues.get(source.name);
    if (!values) continue;
    if (values.has(name)) return values.get(name);
  }
  return undefined;
}

/**
 * 加载环境变量
 */
function loadEnvVar(name) {
  loadEnvFiles();
  const fromFiles = loadEnvFromFiles(name);
  if (fromFiles !== undefined) return fromFiles;
  if (envFileContents.size === 0) return process.env[name];
  return undefined;
}

/**
 * 获取环境变量文件状态
 */
export function getEnvFileStatus() {
  loadEnvFiles();
  const sources = ENV_SOURCES.filter((source) => envFileContents.has(source.name));
  return {
    found: sources.length > 0,
    sources: sources.map((source) => source.name),
    paths: sources.map((source) => source.path),
    searchPaths: ENV_SOURCES.map((source) => source.path),
  };
}

/**
 * 解析布尔值
 */
function parseBool(value, fallback) {
  if (value === undefined || value === null || value === "") return fallback;
  if (typeof value === "boolean") return value;
  const normalized = String(value).trim().toLowerCase();
  if (["1", "true", "yes", "y", "on"].includes(normalized)) return true;
  if (["0", "false", "no", "n", "off"].includes(normalized)) return false;
  return fallback;
}

/**
 * 构建配置
 */
export function buildConfig(pluginConfig = {}) {
  const cfg = pluginConfig ?? {};

  // Memory Server 基础配置
  const baseUrl = cfg.baseUrl || loadEnvVar("MEMORY_BASE_URL") || loadEnvVar("MEMOS_BASE_URL") || DEFAULT_BASE_URL;
  const apiKey = cfg.apiKey || loadEnvVar("MEMORY_API_KEY") || loadEnvVar("MEMOS_API_KEY") || "";
  
  // 用户和会话配置
  const userId = cfg.userId || loadEnvVar("MEMORY_USER_ID") || loadEnvVar("MEMOS_USER_ID") || "openclaw-user";
  const scopeId = cfg.scopeId || loadEnvVar("MEMORY_SCOPE_ID") || loadEnvVar("MEMOS_CONVERSATION_ID") || "";
  
  // 功能开关
  const addEnabled = parseBool(cfg.addEnabled, parseBool(loadEnvVar("MEMORY_ADD_ENABLED"), true));
  const searchEnabled = parseBool(cfg.searchEnabled, parseBool(loadEnvVar("MEMORY_SEARCH_ENABLED"), true));
  
  // 搜索配置
  const threshold = cfg.threshold ?? Number(loadEnvVar("MEMORY_THRESHOLD")) ?? 0.3;
  const num = cfg.num ?? Number(loadEnvVar("MEMORY_NUM")) ?? 10;

  return {
    // 基础配置
    baseUrl: baseUrl.replace(/\/+$/, ""),
    apiKey,
    userId,
    scopeId,
    
    // 功能开关
    addEnabled,
    searchEnabled,
    
    // 搜索配置
    threshold,
    num,
    
    // 环境变量状态
    envFileStatus: getEnvFileStatus(),
    
    // 请求配置
    timeoutMs: cfg.timeoutMs ?? 5000,
    retries: cfg.retries ?? 1,
  };
}

/**
 * 调用 Memory Server API
 */
export async function callApi({ baseUrl, timeoutMs = 5000, retries = 1, apiKey }, path, body) {
  let lastError;
  for (let attempt = 0; attempt <= retries; attempt += 1) {
    try {
      const controller = new AbortController();
      const timeoutId = setTimeout(() => controller.abort(), timeoutMs);

      const headers = {
        "Content-Type": "application/json",
      };
      if (apiKey) {
        headers["Authorization"] = `Bearer ${apiKey}`;
      }

      const res = await fetch(`${baseUrl}${path}`, {
        method: "POST",
        headers,
        body: JSON.stringify(body),
        signal: controller.signal,
      });

      clearTimeout(timeoutId);

      if (!res.ok) {
        const errorText = await res.text().catch(() => "");
        throw new Error(`HTTP ${res.status}: ${errorText}`);
      }

      return await res.json();
    } catch (err) {
      lastError = err;
      if (attempt < retries) {
        await delay(100 * (attempt + 1));
      }
    }
  }

  throw lastError;
}

/**
 * 添加消息到记忆
 * 对应 memory_server.py 的 /add_messages/ 端点
 * 
 * @param {Object} cfg - 配置对象
 * @param {Array} messages - 消息数组，格式为 [{role: string, content: string}]
 * @param {Object} options - 可选参数
 * @returns {Promise<Object>} - API 响应结果
 */
export async function addMessages(cfg, messages, options = {}) {
  if (!cfg.addEnabled) {
    return { status: "skipped", message: "Add messages is disabled" };
  }

  if (!Array.isArray(messages) || messages.length === 0) {
    throw new Error("Messages must be a non-empty array");
  }

  // 验证消息格式
  for (const msg of messages) {
    if (!msg.role || !msg.content) {
      throw new Error("Each message must have 'role' and 'content' fields");
    }
  }

  const payload = {
    messages: messages.map(msg => ({
      role: msg.role,
      content: msg.content
    })),
    user_id: options.userId || cfg.userId || "openclaw-user",
    scope_id: options.scopeId || cfg.scopeId || "",
  };

  return callApi(cfg, "/add_messages/", payload);
}

/**
 * 搜索内存内容
 * 对应 memory_server.py 的 /search_memory/ 端点
 * 
 * @param {Object} cfg - 配置对象
 * @param {string} query - 搜索查询
 * @param {Object} options - 可选参数
 * @returns {Promise<Object>} - API 响应结果
 */
export async function searchMemory(cfg, query, options = {}) {
  if (!cfg.searchEnabled) {
    return { status: "skipped", message: "Search memory is disabled", results: [] };
  }

  if (!query || typeof query !== "string") {
    throw new Error("Query must be a non-empty string");
  }

  const payload = {
    query: query,
    num: options.num ?? cfg.num ?? 10,
    user_id: options.userId || cfg.userId || "openclaw-user",
    scope_id: options.scopeId || cfg.scopeId || "",
    threshold: options.threshold ?? cfg.threshold ?? 0.3,
  };

  return callApi(cfg, "/search_memory/", payload);
}

/**
 * 搜索用户历史摘要
 * 对应 memory_server.py 的 /search_user_history_summary/ 端点
 * 
 * @param {Object} cfg - 配置对象
 * @param {string} query - 搜索查询
 * @param {Object} options - 可选参数
 * @returns {Promise<Object>} - API 响应结果
 */
export async function searchUserHistorySummary(cfg, query, options = {}) {
  if (!cfg.searchEnabled) {
    return { status: "skipped", message: "Search user history summary is disabled", results: [] };
  }

  if (!query || typeof query !== "string") {
    throw new Error("Query must be a non-empty string");
  }

  const payload = {
    query: query,
    num: options.num ?? cfg.num ?? 10,
    user_id: options.userId || cfg.userId || "openclaw-user",
    scope_id: options.scopeId || cfg.scopeId || "",
    threshold: options.threshold ?? cfg.threshold ?? 0.3,
  };

  return callApi(cfg, "/search_user_history_summary/", payload);
}

/**
 * 更新记忆内容（根据ID）
 * 对应 memory_server.py 的 /update_mem_by_id/ 端点
 */
export async function updateMemoryById(cfg, memId, memory, options = {}) {
  if (!memId || !memory) {
    throw new Error("memId and memory are required");
  }

  const payload = {
    mem_id: memId,
    memory: memory,
    user_id: options.userId || cfg.userId || "openclaw-user",
    scope_id: options.scopeId || cfg.scopeId || "",
  };

  return callApi(cfg, "/update_mem_by_id/", payload);
}

/**
 * 更新用户变量
 * 对应 memory_server.py 的 /update_variables/ 端点
 */
export async function updateVariables(cfg, variables, options = {}) {
  if (!variables || typeof variables !== "object") {
    throw new Error("variables must be an object");
  }

  const payload = {
    variables: variables,
    user_id: options.userId || cfg.userId || "openclaw-user",
    scope_id: options.scopeId || cfg.scopeId || "",
  };

  return callApi(cfg, "/update_variables/", payload);
}

/**
 * 删除用户变量
 * 对应 memory_server.py 的 /delete_variables/ 端点
 */
export async function deleteVariables(cfg, names, options = {}) {
  if (!Array.isArray(names) || names.length === 0) {
    throw new Error("names must be a non-empty array");
  }

  const payload = {
    names: names,
    user_id: options.userId || cfg.userId || "openclaw-user",
    scope_id: options.scopeId || cfg.scopeId || "",
  };

  return callApi(cfg, "/delete_variables/", payload);
}

/**
 * 删除特定范围内的所有内存
 * 对应 memory_server.py 的 /delete_mem_by_scope/ 端点
 */
export async function deleteMemByScope(cfg, scopeId) {
  if (!scopeId) {
    throw new Error("scopeId is required");
  }

  const payload = {
    scope_id: scopeId,
  };

  return callApi(cfg, "/delete_mem_by_scope/", payload);
}

/**
 * 获取用户变量
 * 对应 memory_server.py 的 /get_variables/ 端点
 */
export async function getVariables(cfg, options = {}) {
  const payload = {
    names: options.names || null,
    user_id: options.userId || cfg.userId || "openclaw-user",
    scope_id: options.scopeId || cfg.scopeId || "",
  };

  return callApi(cfg, "/get_variables/", payload);
}

/**
 * 分页获取用户内存
 * 对应 memory_server.py 的 /get_user_mem_by_page/ 端点
 */
export async function getUserMemByPage(cfg, options = {}) {
  const payload = {
    user_id: options.userId || cfg.userId || "openclaw-user",
    scope_id: options.scopeId || cfg.scopeId || "",
    page_size: options.pageSize ?? 10,
    page_idx: options.pageIdx ?? 0,
    memory_type: options.memoryType ?? "UNKNOWN",
  };

  return callApi(cfg, "/get_user_mem_by_page/", payload);
}

/**
 * 健康检查
 * 对应 memory_server.py 的 /health 端点
 */
export async function healthCheck(cfg) {
  try {
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), cfg.timeoutMs ?? 5000);

    const res = await fetch(`${cfg.baseUrl}/health`, {
      method: "GET",
      signal: controller.signal,
    });

    clearTimeout(timeoutId);

    if (!res.ok) {
      return { status: "unhealthy", error: `HTTP ${res.status}` };
    }

    return await res.json();
  } catch (err) {
    return { status: "unhealthy", error: err.message };
  }
}

/**
 * 提取文本内容
 */
export function extractText(content) {
  if (!content) return "";
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content
      .filter((block) => block && typeof block === "object" && block.type === "text")
      .map((block) => block.text)
      .join(" ");
  }
  return "";
}

/**
 * 格式化搜索结果为可读文本
 */
export function formatSearchResults(result, options = {}) {
  const data = extractResultData(result);
  if (!data || !Array.isArray(data.results)) return "";

  const maxChars = options.maxItemChars ?? 200;
  const lines = [];

  for (const item of data.results) {
    const content = item.content ?? "";
    const score = item.score ?? 0;
    const type = item.type ?? "UNKNOWN";
    const memId = item.mem_id ?? "unknown";

    if (!content) continue;

    const truncated = content.length > maxChars ? `${content.slice(0, maxChars)}...` : content;
    lines.push(`[${type}] (score: ${score.toFixed(3)}) ${truncated}`);
  }

  return lines.join("\n");
}

/**
 * 格式化添加消息结果为可读文本
 */
export function formatAddMessagesResult(result) {
  if (!result) return "添加消息失败";
  if (result.status === "success") {
    return "消息添加成功";
  }
  if (result.status === "skipped") {
    return `消息添加被跳过: ${result.message}`;
  }
  return `添加消息失败: ${result.detail || result.message || "未知错误"}`;
}

/**
 * 用户查询标记，用于剥离提示词中的原始query
 * 参考 memos 插件的 USER_QUERY_MARKER
 */
export const USER_QUERY_MARKER = "user原始query：​";

/**
 * 清理内联文本（去除换行符）
 * 参考 memos 插件的 sanitizeInlineText
 */
function sanitizeInlineText(text) {
  if (text === undefined || text === null) return "";
  return String(text).replace(/\r?\n+/g, " ").trim();
}

/**
 * 截断文本
 * 参考 memos 插件的 truncate
 */
function truncate(text, maxLen) {
  if (!text) return "";
  if (!maxLen || maxLen <= 0) return text;
  return text.length > maxLen ? `${text.slice(0, maxLen)}...` : text;
}

/**
 * 从搜索结果中提取数据
 */
function extractSearchData(result) {
  if (!result || typeof result !== "object") return null;
  // 尝试多种可能的数据结构
  const data = result.data ?? result.result ?? result;
  return data;
}

/**
 * 格式化记忆行
 * 参考 memos 插件的 formatMemoryLine
 */
function formatMemoryLine(item, text, options = {}) {
  const cleaned = sanitizeInlineText(text);
  if (!cleaned) return "";
  const maxChars = options.maxItemChars;
  const truncated = truncate(cleaned, maxChars);
  const time = formatTime(item?.create_time);
  if (time) return `   -[${time}] ${truncated}`;
  return `   - ${truncated}`;
}

/**
 * 格式化历史摘要行
 * 参考 memos 插件的 formatPreferenceLine
 */
function formatHistorySummaryLine(item, text, options = {}) {
  const cleaned = sanitizeInlineText(text);
  if (!cleaned) return "";
  const maxChars = options.maxItemChars;
  const truncated = truncate(cleaned, maxChars);
  const time = formatTime(item?.create_time ?? item?.timestamp);
  const type = item?.summary_type ?? "Summary";
  const typeLabel = type ? ` [${type}]` : "";
  if (time) return `   -[${time}]${typeLabel} ${truncated}`;
  return `   -${typeLabel} ${truncated}`;
}

/**
 * 包裹代码块
 * 参考 memos 插件的 wrapCodeBlock
 */
function wrapCodeBlock(lines, options = {}) {
  if (!options.wrapTagBlocks) return lines;
  return ["```text", ...lines, "```"];
}

/**
 * 构建系统提示词，将记忆和用户历史摘要整合到上下文中
 * 完全参考 memos 插件的 buildPromptFromData 格式
 *
 * @param {Object} memoryResult - 普通记忆搜索结果
 * @param {Object} summaryResult - 用户历史摘要搜索结果
 * @param {Object} options - 配置选项
 * @returns {string} - 构建好的系统提示词
 */
export function buildMemoryPrompt(memoryResult, summaryResult, options = {}) {
  const memoryData = extractSearchData(memoryResult);
  const summaryData = extractSearchData(summaryResult);

  // 提取记忆列表
  const memoryList = memoryData?.results ?? memoryData?.memories ?? [];
  
  // 提取摘要列表
  const summaryList = summaryData?.results ?? summaryData?.summaries ?? [];

  // 如果没有数据，返回空字符串
  if (memoryList.length === 0 && summaryList.length === 0) {
    return "";
  }

  const now = options.currentTime ?? Date.now();
  const nowText = formatTime(now) || formatTime(Date.now()) || "";

  // 构建记忆行
  const memoryLines = memoryList
    .map((item) => {
      const text = item?.content ?? item?.memory ?? item?.memory_value ?? item?.memory_key ?? "";
      return formatMemoryLine(item, text, options);
    })
    .filter(Boolean);

  // 构建摘要行
  const summaryLines = summaryList
    .map((item) => {
      const text = item?.content ?? item?.summary ?? item?.preference ?? "";
      return formatHistorySummaryLine(item, text, options);
    })
    .filter(Boolean);

  const hasContent = memoryLines.length > 0 || summaryLines.length > 0;
  if (!hasContent) return "";

  // 构建 memoriesBlock
  const memoriesBlock = [
    "<memories>",
    "  <Fragmented Memory>",
    ...memoryLines,
    "  </Fragmented Memory>",
  ];

  // 如果有摘要，添加 summaries 部分
  if (summaryLines.length > 0) {
    memoriesBlock.push("  <Summary Memory>");
    memoriesBlock.push(...summaryLines);
    memoriesBlock.push("  </Summary Memory>");
  }

  memoriesBlock.push("</memories>");

  // 构建最终提示词
  const lines = [
    "# Role",
    "",
    "You are an intelligent assistant with long-term memory capabilities (openjiuwen-memory-Assistant). Your goal is to combine retrieved memory fragments to provide highly personalized, accurate, and logically rigorous responses.",
    "",
    "# System Context",
    "",
    `* Current Time: ${nowText} (Use this as the baseline for freshness checks)`,
    "",
    "# Memory Data",
    "",
    'Below is the information retrieved by openjiuwen-memory, categorized into "Fragmented Memory" and "Summary Memory".',
    "* **Fragmented Memory**: Contains key information such as personal preferences, personal experiences, factual information, concept definitions, and long-term rules.",
    "* **Summary Memory**: Historical summaries of user conversation queries.",
    "* **Special Note**: Content tagged with '[assistant观点]' or '[模型总结]' represents **past AI inference**, **not** direct user statements.",
    "",
    ...wrapCodeBlock(memoriesBlock, options),
    "",
    "# Response Guidelines",
    "",
    "- If the Memory contains relevant information that directly addresses the Question, use it as the primary basis for your answer.",
    "- If the Memory is empty, irrelevant, or insufficient, answer using your general knowledge—but do not fabricate details or pretend the memory contains information it doesn't.",
    "- If the memory is partial or ambiguous, acknowledge that clearly and supplement with reasonable inference or clarification when appropriate.",
    "- Keep your response concise, natural, and **directly** responsive to the question.",
    "",
    "# Instructions",
    "",
    "1. **Review**: Read the retrieved memories above to understand relevant context about the user.",
    "2. **Time Analysis**: Every memory has its own conversation time. Carefully understand the conversation information and analyze the event time based on the conversation time.",
    "3. **Execute**: Use the retrieved memories as context to provide personalized responses, following the Response Guidelines above.",
    "4. **Output**: Answer directly. Never mention internal terms such as \"memory store\", \"retrieval\", or \"memory system\".",
    "5. **Attention**: Additional memory context is already provided. Do not read from or write to local `MEMORY.md` or `memory/*` files for reference, as they may be outdated or irrelevant to the current query.",
    USER_QUERY_MARKER,
  ];

  return lines.join("\n");
}

/**
 * 格式化记忆上下文为简单的文本块
 * 适用于轻量级场景
 */
export function formatMemoryContext(memoryResult, summaryResult, options = {}) {
  const memoryData = extractSearchData(memoryResult);
  const summaryData = extractSearchData(summaryResult);

  const memoryList = memoryData?.results ?? memoryData?.memories ?? [];
  const summaryList = summaryData?.results ?? summaryData?.summaries ?? [];

  if (memoryList.length === 0 && summaryList.length === 0) {
    return "";
  }

  const lines = [];

  if (memoryList.length > 0) {
    lines.push("相关记忆:");
    for (const item of memoryList) {
      const content = item.content ?? item.memory ?? "";
      if (content) {
        lines.push(`- ${content}`);
      }
    }
    lines.push("");
  }

  if (summaryList.length > 0) {
    lines.push("历史摘要:");
    for (const item of summaryList) {
      const content = item.content ?? item.summary ?? "";
      if (content) {
        lines.push(`- ${content}`);
      }
    }
    lines.push("");
  }

  return lines.join("\n");
}
