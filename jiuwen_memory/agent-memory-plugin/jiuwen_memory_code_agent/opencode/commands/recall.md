Search past session memories and summaries for relevant context. Uses `search_memories` and `search_history_summaries` MCP tools.

## Usage

```
/recall [query]
```

## Instructions

1. Call `search_memories` with the query and `num: 10`, `threshold: 0.3`.
2. Call `search_history_summaries` with the same query and `num: 5`.
3. Combine and present results:
   - Group memories by type (user_profile, semantic_memory, episodic_memory)
   - Show history summaries separately
   - Highlight high-score memories (score >= 0.7)
4. If no results from either, suggest 2-3 alternative search terms.
5. **Never hallucinate results.** Only present what the MCP tools actually return.
