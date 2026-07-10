#!/usr/bin/env node
// Notification — no-op for jiuwen. We don't record notifications as memories.
async function main() {
  let input = "";
  for await (const chunk of process.stdin) input += chunk;
  // Drain stdin, do nothing.
}

main();
