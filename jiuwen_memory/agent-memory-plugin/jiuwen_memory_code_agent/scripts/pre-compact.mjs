#!/usr/bin/env node
// PreCompact — inject relevant memories via stdout before context compression.
// Auto-detects platform: plain text for Claude Code/Codex, JSON for Cursor.
import { resolveProject, searchAndFormat, isSdkChildContext, coerceText, formatOutput, DEFAULT_USER_ID } from "./_shared.mjs";

async function main() {
  let input = "";
  for await (const chunk of process.stdin) input += chunk;
  let data;
  try { data = JSON.parse(input); } catch { return; }
  if (isSdkChildContext(data)) return;

  const cwd = data.cwd || process.cwd();
  const scopeId = resolveProject(cwd);

  // Extract a query from the conversation to search for relevant memories.
  const messages = data.messages || [];
  let query = "";
  for (const m of messages) {
    if (m.role === "user") query = coerceText(m.content).slice(0, 500);
  }
  if (!query) query = scopeId;

  // Search and inject context via stdout.
  // Claude Code / Codex: plain text. Cursor: {"user_message":"<text>"} JSON.
  const context = await searchAndFormat(query, scopeId, DEFAULT_USER_ID);
  if (context) process.stdout.write(formatOutput(context, "preCompact"));
}

main();
