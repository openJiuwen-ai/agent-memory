# WorkSwarm Integration

This guide explains how to integrate WorkSwarm (formerly JiuwenSwarm — renamed on the official site; the gitcode repository and code identifiers remain `jiuwenswarm`) with the agent-memory engine, giving your Agent persistent, cross-session memory. WorkSwarm connects to agent-memory through the **JiuwenMemory** external memory provider, in two modes: `server` (remote HTTP) and `sdk` (in-process kernel assembly).

The full installation, configuration, and troubleshooting steps are maintained in the WorkSwarm repository's [JiuwenMemory SDK Access guide](https://gitcode.com/openJiuwen/jiuwenswarm/blob/develop/docs/en/JiuwenMemory-SDK.md). This document only outlines the integration principle, a summary of the steps, and entry points on the agent-memory side.

## 1. How It Works

### 1.1 Two Access Modes

| Mode | How it works | Best for |
|---|---|---|
| `server` | Remote HTTP calls to an agent-memory server (`POST /v1/<verb>`) | Production deployment, multiple clients sharing one memory service |
| `sdk` | Assembles the agent-memory kernel in-process within WorkSwarm, direct calls, no HTTP hop | Single-machine embedding, no extra HTTP service, latency-sensitive |

Both modes end up at the same `MemoryAPI` semantics: `add` writes memories, `search` recalls them. The upper-layer Agent is mode-agnostic.

### 1.2 Automatic Memory Rail

Once integrated, WorkSwarm's ExternalMemoryRail (memory rail) drives memory behavior automatically — **no explicit tool calls from the Agent are required**:

```text
before_model_call (before each model call)
  └─ prefetch(user query of this turn)  ← auto-retrieves memories; hits are injected
                                           as a <memory-context> block into the prompt

after_invoke (end of each turn)
  └─ sync_turn(user query, assistant output)  ← auto-persists the turn
                                                 (optional LLM extraction + dedup)
```

The rail also exposes two tools to the Agent — `mem2_search` / `mem2_add` — as an explicit supplement for retrieval and writing. A single failed read or write never blocks the main conversation flow.

## 2. Integration Steps at a Glance

1. **Install the kernel**: run `pip install JiuwenMemory` in the same Python environment as WorkSwarm, and verify with `python -c "from jiuwen_memory.api import assemble"`;
2. **Prepare the backends**:
   - `sdk` mode: deploy Redis / Milvus / Elasticsearch (lightweight setups may fall back to a sqlite / memory combination);
   - `server` mode: start the agent-memory HTTP service following the [Deployment Overview](../Installation%20Guide/Deployment%20Overview.md);
3. **Configure**: in WorkSwarm's `config.yaml`, set `provider: jiuwenmemory` under the `memory` section and choose a `mode`; for `sdk` mode, also fill in the `type` / `url` of the three backends under the `jiwen.sdk` subsection;
4. **Verify**: on startup, a `JiuwenMemory provider built` line in the logs means the rail is mounted. Then ask the Agent to remember something in a conversation, and ask again a few turns later or in a new session — successful recall confirms the integration works.

For the exact commands of each step, field reference, the full environment-variable table, and FAQs, see the [JiuwenMemory SDK Access guide](https://gitcode.com/openJiuwen/jiuwenswarm/blob/develop/docs/en/JiuwenMemory-SDK.md).
