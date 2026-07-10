#!/usr/bin/env node
// UserPromptSubmit / beforeSubmitPrompt — record prompt + inject memory context.
//
// All platforms: record prompt via /add_messages/ + search relevant memories.
// Claude Code / Codex (UserPromptSubmit): plain text → injected directly into LLM context.
// Cursor (beforeSubmitPrompt): output JSON {"continue":true, "user_message":"<context>"}.
import { resolveProject, addMessages, searchAndFormat, isSdkChildContext, coerceText, formatOutput, IS_CURSOR, DEFAULT_USER_ID } from "./_shared.mjs";

async function main() {
  let input = "";
  for await (const chunk of process.stdin) input += chunk;
  let data;
  try { data = JSON.parse(input); } catch { return; }
  if (isSdkChildContext(data)) return;

  const cwd = data.cwd || process.cwd();
  const scopeId = resolveProject(cwd);
  const prompt = coerceText(data.prompt ?? data.userPrompt ?? "");

  if (!prompt) return;

  // Fire-and-forget background write — never block the agent loop.
  addMessages([{ role: "user", content: prompt }], scopeId, DEFAULT_USER_ID).catch(() => {});

  // Search for relevant context and inject it into the agent's context.
  // Claude Code / Codex: plain text → injected directly into LLM context.
  // Cursor: JSON {"continue":true, "user_message":"<context>"}.
  const context = await searchAndFormat(prompt, scopeId, DEFAULT_USER_ID);
  if (context) {
    const eventType = IS_CURSOR ? "beforeSubmitPrompt" : "userPromptSubmit";
    process.stdout.write(formatOutput(context, eventType));
  }

  setTimeout(() => process.exit(0), 500).unref();
}

main();
