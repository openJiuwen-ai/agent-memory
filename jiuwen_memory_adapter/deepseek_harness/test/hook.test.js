import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import {
  CONTEXT_NAME,
  createPlugin,
  pickTurnMessages,
  readInstallToken,
} from "../index.js";

function makeHome({ nonce = "abc" } = {}) {
  const home = mkdtempSync(join(tmpdir(), "dsh-memory-"));
  mkdirSync(join(home, "run"), { recursive: true });
  mkdirSync(join(home, "installs", "dsh-1"), { recursive: true });
  writeFileSync(
    join(home, "run", "service.json"),
    JSON.stringify({ port: 1234, instance_nonce: nonce, protocol: 1 }),
  );
  writeFileSync(join(home, "installs", "dsh-1", "token"), "token-1\n");
  writeFileSync(
    join(home, "installs", "dsh-1", "install.json"),
    JSON.stringify({ host: "dsh", state: "connected" }),
  );
  return home;
}

function fakeFetch(handler) {
  const calls = [];
  const impl = async (url, options = {}) => {
    calls.push({ url, options });
    const result = handler(url, options);
    if (result instanceof Error) throw result;
    return result;
  };
  impl.calls = calls;
  return impl;
}

const okJson = (body) => ({ ok: true, json: async () => body });

function callsTo(fetchImpl, path) {
  return fetchImpl.calls.filter((call) => call.url.endsWith(path));
}

test("assemble appends one named context", async () => {
  const home = makeHome();
  const fetchImpl = fakeFetch((url) => {
    if (url.endsWith("/local/status")) return okJson({ instance_nonce: "abc" });
    return okJson({ view: { items: [{ content: "remembered fact" }] }, notice: null });
  });
  const plugin = createPlugin({ env: { JIUWEN_MEMORY_HOME: home }, fetchImpl, home });
  const payload = { session_id: "s1", turn_id: "t1", prompt: "why?", contexts: [] };
  const result = await plugin.hooks["system-prompt/assemble"](payload);
  assert.equal(result.contexts.length, 1);
  assert.equal(result.contexts[0].name, CONTEXT_NAME);
  assert.match(result.contexts[0].content, /remembered fact/);
  const recall = callsTo(fetchImpl, "/local/hook/recall")[0];
  assert.equal(recall.options.method, "POST");
});

test("assemble leaves the payload alone when the service is unreachable", async () => {
  const home = mkdtempSync(join(tmpdir(), "dsh-memory-"));
  const fetchImpl = fakeFetch(() => new Error("refused"));
  const plugin = createPlugin({
    env: { JIUWEN_MEMORY_HOME: home },
    fetchImpl,
    home,
    spawnSyncImpl: () => ({}),
  });
  const payload = { session_id: "s1", prompt: "why?", contexts: [] };
  const result = await plugin.hooks["system-prompt/assemble"](payload);
  assert.deepEqual(result.contexts, []);
  assert.equal(fetchImpl.calls.length, 0);
});

test("assemble refuses a stale instance nonce", async () => {
  const home = makeHome({ nonce: "old" });
  const fetchImpl = fakeFetch(() => okJson({ instance_nonce: "new" }));
  const plugin = createPlugin({ env: { JIUWEN_MEMORY_HOME: home }, fetchImpl, home });
  const payload = { session_id: "s1", prompt: "why?", contexts: [] };
  const result = await plugin.hooks["system-prompt/assemble"](payload);
  assert.deepEqual(result.contexts, []);
  assert.equal(callsTo(fetchImpl, "/local/hook/recall").length, 0);
});

test("turn/end captures only the human prompt and final answer", async () => {
  const home = makeHome();
  const fetchImpl = fakeFetch(() => okJson({ status: "committed" }));
  const plugin = createPlugin({ env: { JIUWEN_MEMORY_HOME: home }, fetchImpl, home });
  const payload = {
    session_id: "s1",
    turn_id: "t1",
    events: [
      { source: "system", type: "system/message", text: "system prompt" },
      { source: "user", type: "user/message", text: "fix the bug" },
      { source: "tool", type: "tool/result", text: "tool output" },
      { source: "assistant", type: "assistant/message", text: "done: changed x" },
    ],
  };
  await plugin.hooks["turn/end"](payload);
  const capture = callsTo(fetchImpl, "/local/hook/capture")[0];
  const body = JSON.parse(capture.options.body);
  assert.equal(body.prompt, "fix the bug");
  assert.equal(body.answer, "done: changed x");
  assert.equal(body.turn_id, "t1");
});

test("turn/end without a human message writes nothing", async () => {
  const home = makeHome();
  const fetchImpl = fakeFetch(() => okJson({ status: "committed" }));
  const plugin = createPlugin({ env: { JIUWEN_MEMORY_HOME: home }, fetchImpl, home });
  await plugin.hooks["turn/end"]({
    session_id: "s1",
    turn_id: "t1",
    events: [{ source: "assistant", type: "assistant/message", text: "hi" }],
  });
  assert.equal(callsTo(fetchImpl, "/local/hook/capture").length, 0);
});

test("pickTurnMessages ignores injected memory and tool events", () => {
  const { prompt, answer } = pickTurnMessages([
    { source: "user", type: "user/message", text: "q1" },
    { source: "user", type: "user/message", text: "q2" },
    { source: "tool", type: "tool/result", text: "t" },
    { type: "assistant/message", text: "a1" },
    { type: "assistant/message", text: "a2" },
  ]);
  assert.equal(prompt, "q2");
  assert.equal(answer, "a2");
});

test("install token can be resolved uniquely without an explicit id", () => {
  const home = makeHome();
  assert.equal(readInstallToken(home, "", {}), "token-1");
  assert.equal(readInstallToken(home, "missing", {}), "");
});
