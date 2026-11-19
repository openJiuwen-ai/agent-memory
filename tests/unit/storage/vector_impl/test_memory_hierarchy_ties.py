from jiuwen_memory.common.type_def import Scope
from jiuwen_memory.storage.types import VectorQuery, VectorRecord
from jiuwen_memory.storage.vector_impl.in_memory_vector_store import InMemoryVectorStore


def test_same_cosine_returns_same_source_before_top_k_after_rebuild():
    def summary(uid, start):
        return VectorRecord(uid, [1.0, 0.0], {"hierarchy_role": "time_span",
            "tree_span_start": start, "tree_span_end": start + 1,
            "system_metadata.graphmem_session_scope": "same-session",
            "indexed_text_sha256": "same-text"})

    scope = Scope(org="o")
    query = VectorQuery([1.0, 0.0], top_k=1)
    for earlier, later, reverse in (("z", "a", True), ("b", "y", False)):
        docs = [summary(earlier, 10), summary(later, 20)]
        store = InMemoryVectorStore(stable_hierarchy_ties=True)
        store.insert(scope, docs[::-1] if reverse else docs)
        for hits in (store.search(scope, query), store.recall(scope, query)):
            assert [(h.id, h.score) for h in hits] == [(earlier, 1.0)]
