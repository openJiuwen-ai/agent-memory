#!/usr/bin/env node
// Stop — no-op.
//
// Only the user's prompts are written to memory (via prompt-submit.mjs).
// Agent's final reply is intentionally NOT recorded, so this hook now just
// drains stdin and exits.
async function main() {
  let input = "";
  for await (const chunk of process.stdin) input += chunk;
}

main();
