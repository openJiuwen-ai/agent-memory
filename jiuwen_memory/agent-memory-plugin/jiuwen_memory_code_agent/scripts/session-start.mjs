#!/usr/bin/env node
// SessionStart — probe health only. No memory search here;
// memory search happens on every UserPromptSubmit instead.
// Auto-detects platform: plain text for Claude Code/Codex, JSON for Cursor.
import { healthCheck, isSdkChildContext, IS_CURSOR } from "./_shared.mjs";

async function main() {
  let input = "";
  for await (const chunk of process.stdin) input += chunk;
  let data;
  try { data = JSON.parse(input); } catch { return; }
  if (isSdkChildContext(data)) return;

  // Best-effort liveness check — never fatal.
  await healthCheck();

  // Cursor requires JSON output for sessionStart even if no context is injected.
  if (IS_CURSOR) process.stdout.write("{}");

  setTimeout(() => process.exit(0), 500).unref();
}

main();
