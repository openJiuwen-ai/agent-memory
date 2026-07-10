Explicitly save an insight, decision, or learning to jiuwen-memory for future sessions. Uses `add_messages` MCP tool.

## Usage

```
/remember [what to remember]
```

## Instructions

1. Analyze what needs to be remembered — extract the core insight, decision, or fact.
2. Call `add_messages` with:
   - `messages`: `[{"role": "user", "content": "<the full text to remember>"}]`
   - `infer`: `true` (so jiuwen auto-extracts structured memories)
3. Confirm the save and show a brief summary of what was stored.
4. Preserve the user's own phrasing — don't paraphrase.
