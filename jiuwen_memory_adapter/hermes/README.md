# Hermes MemoryProvider

`JiuwenMemoryProvider` lets Hermes use the local jiuwen-memory service without an MCP
tool call per turn. It calls `/local/hook/recall` before a turn and
`/local/hook/capture` after it. Installation and rollback are managed by
`jiuwen-memory connect hermes --hooks`; disabling the hook restores the previous
`memory.provider`.

Failures return an empty recall and never raise into the host turn.
