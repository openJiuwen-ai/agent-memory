#!/usr/bin/env node
// PreToolUse — stash file paths from Edit/Write/Read/Glob/Grep for later enrichment.
// No HTTP call here; just a local marker that post-tool-use can reference.
// (jiuwen doesn't have a separate /observe endpoint — we batch into add_messages.)

// This script intentionally does nothing — jiuwen captures tool results via
// PostToolUse instead. Keeping it as a no-op placeholder to match the hooks
// declaration so the hook surface is consistent with agentmemory.
async function main() {
  // Read stdin to drain it (required by Claude Code hook contract).
  let input = "";
  for await (const chunk of process.stdin) input += chunk;
}

main();
