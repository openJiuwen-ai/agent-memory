# bootstrap/cli — the command-line surface

The **CLI surface**: a §15 protocol adapter peer to `bootstrap/http_server` (the HTTP
surface). It parses argv into a `(verb, payload)` and routes it through the same
kernel dispatch the HTTP path uses — **no business logic lives here**. It is the
concrete, scriptable 对外接口 (external interface) of the memory engine, and the
interface the LoCoMo evaluation harness drives.

- Stdlib only; no third-party deps.
- Entry: `scripts/run-cli.sh …` → `python3 bootstrap/cli/__main__.py …`.
- Tests: exercised by the evaluation harness (`evaluation/`, e.g. LoCoMo /
  LongMemEval) end to end, plus
  the manual smoke calls below.

## Two backends, one client shape (`client.py`)

Every backend implements `call(verb, payload) -> (status, body)` and
`healthz()`. `make_client(server_url, configs)` picks one:

- **`InProcessClient` (default).** Assembles the engine in *this* process exactly
  like `bootstrap/http_server/__main__` — `server.build(load_config([OFFLINE,
  *configs]))` — and routes each call through `handler.dispatch`, the very code
  path the HTTP surface uses minus the socket. The `Server` is held for the
  client's lifetime, so repeated writes share the in-memory store.
- **`HttpClient`.** `POST {base_url}/v1/<verb>` (and `GET /healthz`) over
  `urllib` against a running `scripts/run-server.sh`. HTTP domain errors (non-2xx
  JSON bodies) are returned as `(status, body)`; a dead server surfaces as
  `(0, {"error": "ConnectionError"})`.

Because both reuse the engine's open `invoke`, **any** verb — built-in or plugged
in after assembly — is reachable with no CLI change; the command table only
shapes argv ergonomics.

### The `server` import-root subtlety

`server.py`、`handler.py`、`profiles.py` 是共享目录 `bootstrap/core/` 下的
flat import root。如果 `bootstrap/` 排在 `bootstrap/core/` 前面，`import server`
就可能被同名模块遮蔽。因此 CLI 仍以脚本方式启动（不用 `python3 -m`），并由
`scripts/run-cli.sh` 把仓库根与 `bootstrap/core` 前置到 `PYTHONPATH`，确保
`import server` 解析到 `core/server.py`（所有 surface 复用的共享应用核），同时
避免在运行时把路径强插到 `sys.path` 最前。

## Subcommands are a table, not a switch (`commands.py`)

`COMMANDS: dict[str, Command]` maps each verb to `(add_arguments, build_payload)`.
The argparse subparser is built from the row and the payload is assembled from the
parsed args — adding a verb is adding a row, never editing a dispatch `if/else`
(the same A20 "route by table" rule the engine follows). Conventions:

- Scope is required on every data verb (I1 user isolation): pass `-u/--user-id`
  (Mem0) or `--scope` (native), or set `$AGENT_MEMORY_USER_ID`; `--trace` is optional.
- `update` omits content/tags when not given, so a partial update means "leave
  unchanged" rather than "set empty" (the `None` sentinel, end to end).
- Output: `-o/--output {json,text,table,quiet}` (default `json`); `--json`/
  `--agent` force JSON; `--pretty` indents. 2xx → stdout exit 0; errors → stderr
  exit 1; bad input (missing scope/bad JSON) → exit 2.

## Mem0 compatibility

The verb + flag vocabulary deliberately tracks [Mem0's CLI](https://docs.mem0.ai/platform/cli)
so a Mem0 user drives agent-memory with the same muscle memory. Verbs already line
up (`add`/`search`/`list`/`get`/`update`/`delete`); the flags are mapped as:

| Mem0 | here | note |
|------|------|------|
| `-u/--user-id` | `-u/--user-id` → scope | primary scoping; `--scope` is the native alias |
| (n/a) | `--tenant` | our extra multi-tenant dimension; optional, default `default` / `$AGENT_MEMORY_TENANT` |
| `add 'text'` (positional) | positional `text` (+ `-c/--content`) | |
| `--messages '[{role,content}]'` | `--messages` | flattened to one memory (`role: content` lines) |
| `-f/--file` (`-`=stdin) | `-f/--file` | JSON message arrays are flattened; else raw text |
| `--categories` | `--categories` → tags | Mem0 categories ≈ our tags (`--tags` also works) |
| `search 'q'`, `-k/--top-k` (def 10) | positional `query`, `-k/--top-k` (def 10) | `--k`, `--query` are aliases |
| `--threshold` | `--threshold` | hits below the score are filtered client-side |
| `get/update/delete <id>` (positional) | positional `memory-id` (+ `--item-id`) | |
| `delete --all` / `--force` | `delete --all` / `--force` | `--all` fans out over a `list` client-side; `--force` is a no-op (we never prompt) |
| `-o/--output`, `--json/--agent` | same | **default differs: we default to `json`** (programmatic-first surface), use `-o text` for humans |
| `--base-url`, `status` | `--server`/`--base-url`, `status`/`health` | |
| `MEM0_USER_ID` / `MEM0_BASE_URL` | `AGENT_MEMORY_USER_ID` / `AGENT_MEMORY_SERVER` | env defaults |

**Intentional divergences** (our engine contract, not Mem0's):

- A `tenant_id` is mandatory in the kernel (I1); Mem0 has no tenant. We default it
  so a Mem0-shaped call (`add 'x' -u alice`) just works.
- `agent_id` / `run_id` are not modelled yet (the engine scopes by a single
  `scope` string) — only `user_id` maps today.
- `--metadata` is not yet persisted (the engine entity has `tags`, no free-form
  metadata) — `--categories`/`--tags` is the supported structured field.
- **`delete` soft-deletes**: it flips `lifecycle_state` to `soft_deleted` but the
  record (authoritative) still appears in `list` and remains searchable in the
  current engine — unlike Mem0's hard removal. `--hard` + `--approval-token` is
  the real removal path. (This is L5/L4 engine behavior, surfaced here as-is.)

### `batch` — one engine, many ops (the stateful path)

`batch` reads NDJSON `{"op": "<verb>", ...payload}` from stdin/a file and runs
each op on **one** client. In-process this means a single assembled engine, so
successive `add`s accumulate and a later `search` sees them — the stateful
session an in-memory store needs **without** a running server. This is the
ingest primitive the LoCoMo harness uses.

```bash
# in-process single shot, Mem0-style (state is NOT shared across invocations)
scripts/run-cli.sh add    "buy milk" -u alice --categories groceries
scripts/run-cli.sh search "milk" -u alice -k 3 -o text

# stateful in one process: add ×2 then search, all sharing the store
printf '%s\n' \
  '{"op":"add","tenant_id":"default","scope":"bob","content":"the quick brown fox"}' \
  '{"op":"add","tenant_id":"default","scope":"bob","content":"lazy dog sleeps"}' \
  '{"op":"search","tenant_id":"default","scope":"bob","query":"quick brown fox","k":3}' \
  | scripts/run-cli.sh batch --input -

# drive a long-running server instead (state lives on the server)
scripts/run-server.sh --port 8137 &
scripts/run-cli.sh --server http://127.0.0.1:8137 add    "hi" -u carol
scripts/run-cli.sh --server http://127.0.0.1:8137 search "hi" -u carol -k 3
```

## Two id spaces (same note as the HTTP surface)

`add`/`get`/`list`/`update`/`delete` use the L5 lifecycle's **authoritative** id
(e.g. `t1:alice:0`); `search` returns the L3-derived **index projection** id
(e.g. `t1:alice:0-0-0`). 原文存一份, 索引另建 — resolving a hit back to its record
is a separate `get`.

## Config & profiles (in-process mode)

`--config a.json b.json …` stacks JSON layers on top of `OFFLINE` (nearest-wins),
identical to the server's positional config args. `--server` mode ignores
`--config` (the server owns its own assembly) and prints a note. Selecting a real
LLM/storage plugin (e.g. `config/examples/vllm.json`) works the same here as for
the server, including the loud `vllm`-misconfig refusal at build time.
