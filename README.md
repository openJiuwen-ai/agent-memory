<h1 align="center">agent-memory</h1>

<p align="center">
  <strong>Framework-Agnostic · Layered Memory · Multi-Form Access — Long-Term Memory Infrastructure for AI Agents</strong>
</p>

<p align="center">
  <a href="README_zh.md">Chinese</a>
  ·
  <a href="docs/design/VISION.md">Vision</a>
  ·
  <a href="docs/design/architecture.md">Architecture</a>
  ·
  <a href="https://gitcode.com/openJiuwen/agent-memory">GitCode</a>
</p>

<p align="center">
  <a href="LICENSE">
    <img src="https://img.shields.io/badge/license-Apache--2.0-green.svg" alt="License" />
  </a>
  <img src="https://img.shields.io/badge/python-≥3.11-blue.svg" alt="Python Version" />
  <img src="https://img.shields.io/badge/os-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey.svg" alt="OS Support" />
</p>

---

## Introduction

openJiuwen **agent-memory** (also known as Jiuwen Memory) is a user-centric, high-precision, high-performance, natively secure, and configurable agent memory system open-sourced by the openJiuwen community. It goes beyond simple vector retrieval — integrating multi-form indexing, self-evolving memory, fused retrieval, and multiple integration methods into a general-purpose memory infrastructure. Whether the host is a chat assistant, a coding agent, or an autonomous agent, the same memory capabilities can be accessed conveniently through SDK, HTTP API, CLI, and more.

**Note**: Future memory-related evolution in openJiuwen will primarily happen in this project. Memory capabilities in agent-core will gradually migrate here.


### Why agent-memory?

| Capability | Value |
| --- | --- |
| Framework-Agnostic, Multi-Form Access | Memory is no longer tied to a specific framework or product: in-process SDK embedding and out-of-process HTTP API access both converge to a unified `MemoryAPI` |
| Flexible & Configurable | Layered design with pipeline-based processing and pluggable operators — horizontally extensible and configurable per scenario |
| Layered Memory Structure | **Extraction → Abstraction/Refinement → Association Analysis** from raw data, accumulating memories across granularities: low (facts/snippets), mid (events/relationships/topics), high (profiles/preferences/skills) — supporting both detailed and macro perspectives |
| Multi-Form Indexing | Beyond vectors: keyword (BM25) / vector / graph (planned) / document (planned) indexes enabled per configuration, with **hybrid recall + reranking** for both semantic accuracy and interpretability |
| Self-Evolving Memory | Memory grows with interaction: extraction → association → **conflict resolution** → refinement → **forgetting/decay** — a complete closed loop with online hot-path and offline background channels |
| Edge / Cloud / Hybrid Deployment (Planned) | Same abstraction, different deployment: edge-side for privacy, cloud-side for elastic retrieval, hybrid for hot/private on-edge + cold/shared on-cloud (selective sync + conflict merging) |
| Native Scope Isolation & Sharing | `org + space` hard isolation with multi-tenancy: single-agent exclusive or multi-agent shared pools, with the same granularity for edge-cloud placement and sync |
| Transparent & Governable | Memories can be inspected / edited / audited / lineage-traced / forgotten, with `as_of` historical lookback and retrieval trajectory observability |

## Features

| Component | Description |
| --- | --- |
| **Memory API** | `add / search / list / get / update / delete / evolve / admin` + governance (inspect/trace/audit/grant) and space management — all access forms map to the same semantics |
| **Memory Retrieval** | Query understanding & denoising → Storage-preferred pipeline (recall/get/Fuser) → Reranker → relevance threshold → **progressive disclosure L0→L1→L2**, with observable retrieval trajectories and structured error returns |
| **Memory Construction** | Six composable operator types: extractor / abstractor / associator / classifier / index_builder / evolver — covering the full pipeline from raw information to indexed memory |
| **Storage Abstraction** | Unified `Storage` facade with six standard ports: **KV / Vector / Full-text / Graph / Fused / Filesystem** — capability discovery + two-tier security boundary, pluggable backends |
| **Agent Plugins** | `agent_plugin/` provides integration with JiuwenSwarm; OpenClaw / Codex / Hermes ecosystems are also being planned |
| **Evaluation Framework** | Two-layer evaluation: component-level IR metrics (Recall@k/MRR/nDCG, etc.) and end-to-end QA (LLM-as-judge), with built-in LoCoMo / LongMemEval adapters + smoke tests |

## Architecture Overview

```
┌──────────────────────────────────────────────────────────────────────────┐
│  A. Access Layer        CLI · Skill · SDK(Python) · HTTP/gRPC · MCP      │
│                         + Multi-modal sources (dialog/docs/code/traces/  │
│                           images/audio/video)                            │
│  B. Memory API          add · search · get · update · delete ·           │
│                         evolve · admin (form-agnostic, PEP auth/audit)   │
├──────────────────────────────────────────────────────────────────────────┤
│  C. Memory Management   Lifecycle · Governance (inspect/edit/audit/      │
│                          forget) · Permissions · Config/Policies         │
├──────────────────────────────────────────────────────────────────────────┤
│  D. Memory Retrieval    Query parsing · Storage retrieval core ·         │
│                          Reranking · Progressive disclosure              │
├──────────────────────────────────────────────────────────────────────────┤
│  E. Memory Construction Layered memory structure (all reconstructible    │
│     (Layered Memory)    from raw data): extract → abstract/refine/       │
│                         associate → multi-granularity memories           │
│                         + multi-form indexes (doc/keyword/vector/graph); │
│                         continuously built & maintained by self-evolution│
├──────────────────────────────────────────────────────────────────────────┤
│  F. Memory Storage      Unified Storage domain ops · Capability          │
│                          discovery · Security boundary · Retrieval      │
│                          adaptation                                      │
│                          Backend ports: KV · Vector · Fulltext · Graph  │
│                          · Fused · Filesystem                            │
├──────────────────────────────────────────────────────────────────────────┤
│  G. Data Layer          User memory data · Agent memory data             │
│                          (raw data, single source of truth)              │
└──────────────────────────────────────────────────────────────────────────┘
   Cross-cutting: Edge/Cloud/Hybrid deployment · Observability (retrieval
   trajectories) · Multi-tenant isolation · Security & compliance
```


## Quick Start

### Prerequisites

- Python 3.11+
- (Optional) LLM configuration (for self-evolution extraction/abstraction) and embedding model

### Installation

```bash
# Option 1: From source (recommended, repo root = package root)
git clone https://gitcode.com/openJiuwen/agent-memory.git
cd agent-memory
pip install -e .                 # Minimal core + SDK
pip install -e ".[dev]"          # + dev/test dependencies
pip install -e ".[deploy]"       # + real storage backends (Milvus / ES / Redis / PostgreSQL)
pip install -e ".[embed]"        # + advanced embedding / reranking (torch, BGE, etc.)
```

### Quick Integration (In-Process SDK)

```python
from jiuwen_memory.api import SearchOptions, assemble, legacy_request_context
from jiuwen_memory.config import Config
from jiuwen_memory.common.type_def import Scope, Context

# Assemble from YAML (falls back to built-in defaults — pure in-memory offline stack)
api = assemble(config=Config.from_yaml("examples/config.yml"))

scope = Scope(org="acme", user="alice", agent="assistant", session="s1")
security = legacy_request_context(scope)

# Write memory
units = api.add(
    "Alice prefers Americano in the morning, no sugar.",
    scope,
    security=security,
    tags=["demo"],
)

# Retrieve memory (hybrid recall + observable trajectory)
res = api.search(
    "coffee morning",
    Context(scope),
    options=SearchOptions(top_k=3, with_trajectory=True), security=security,
)
for item in res.items:
    print(item.content)
```

### HTTP Server

```bash
scripts/run-server.sh                       # Starts at http://127.0.0.1:8080 by default
scripts/run-cli.sh --server http://127.0.0.1:8080 search --query "coffee" \
  --context '{"scope":{"org":"local","user":"developer"}}' --options '{"top_k":3}'
curl -X POST http://127.0.0.1:8080/v1/add \
  -H "Content-Type: application/json" \
  -d '{"tenant_id": "default", "scope": "alice", "content": "Alice is a Python developer."}'
```

## Running Evaluations

```bash
# Smoke Test (required for CI)
python -m pytest evaluation/smoke_test -v

# Component-level IR evaluation (built-in smoke benchmark by default)
python evaluation/scripts/run_ir_eval.py --json results.json

# End-to-end QA evaluation (requires JUDGE_* env vars; built-in LoCoMo / LongMemEval adapters)
export JUDGE_BASE_URL=...  JUDGE_MODEL=...  JUDGE_API_KEY=...
python evaluation/scripts/run_e2e_eval.py --dataset locomo
```

## Storage Backend Configuration

Use `examples/config_template.yml` (two-level namespace: component → named instance) to swap default implementations as needed. Changing backends only requires config changes — no code changes across layers:

```yaml
globals:
  vector_enabled: true        # Vector indexing + recall path
  graph_enabled: true         # Graph recall path
  rerank_enabled: true        # Reranking before disclosure

# Swap storage backends: override the default instance under each namespace
kv_store:
  default: { target: redis, params: { url: "redis://localhost:6379/0", db: 0 } }
vector_store:
  default: { target: milvus, params: { uri: "http://localhost:19530" } }
```

## Documentation

| Document | Content |
| --- | --- |
| [Vision](docs/design/VISION.md) | Design principles, five capability pillars, competitive analysis & differentiation, success criteria |
| [Architecture](docs/design/architecture.md) | Seven-layer architecture, data model (MemoryUnit/Scope), retrieval/construction/storage/deployment details, open questions |
| [Competitor Analysis](docs/design/competitor_analysis.md) | Survey of mainstream memory systems |
| [Benchmark Survey](docs/design/memory_benchmarks.md) | LoCoMo / LongMemEval / BEAM benchmark selection |
| [Evaluation Guide](evaluation/README.md) | Two-layer evaluation usage and dataset guide |
| [Feature Design](docs/features/) | Design decisions per module (storage / construction / retrieval / control / common) |
| [Technical Specs](docs/specs/) | Module design specifications |
| [Developer Guides](docs/en/) | Agent integration guides, API docs, FAQ, installation guides |


## Contributing

Contributions to agent-memory are welcome. You can contribute in the following ways:

- Submit bugs, feature suggestions, or usage questions: [Issues](https://github.com/openJiuwen-ai/agent-memory/issues)
- Submit code, documentation, or examples: [Pull Requests](https://github.com/openJiuwen-ai/agent-memory/pulls)

**Important**: When merging PRs/MRs, please select `mem2.0` (the current development branch) as the base branch instead of the default `main`.

Before contributing, please read the [Development Guidelines](.claude/CLAUDE.md) for code style, layering, and testing strategy. The project follows three contract categories — operators, plugins, and storage — please identify the appropriate layer in `docs/design/architecture.md` before proposing new features.

## FAQ

1. **Why is the core package named `jiuwen_memory` instead of `src`?** The core has been migrated from `src/` to a top-level package `jiuwen_memory` for proper wheel distribution (avoiding interference from `src` layout).
2. **Can it work without LLM / vector databases?** Yes. The default assembly is a pure in-memory offline stack (no external dependencies) — keyword retrieval works out of the box; self-evolution, graph, and vector capabilities can be enabled via configuration.
3. For more, see the [FAQ](docs/en/FAQ/).

## License

This project is open-sourced under the [Apache License 2.0](LICENSE).

This product is a memory infrastructure and does not include built-in AI model capabilities. When connecting AI models for specific business use cases, users are responsible for ensuring compliance with applicable regulations such as GDPR, the EU AI Act, and others.
