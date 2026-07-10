#!/usr/bin/env node
// SessionEnd — no-op. jiuwen captures every turn via PostToolUse/PromptSubmit,
// so there is no bulk "session dump" to do at end.
async function main() {
  let input = "";
  for await (const chunk of process.stdin) input += chunk;
  // Drain stdin, do nothing. All data already captured per-turn.
}

main();
