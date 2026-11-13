"""Task-description vector search with BM25 fallback over authoritative Registry records."""

from __future__ import annotations

import math
import threading
from collections import Counter
from dataclasses import asdict

from jiuwen_memory.common.errors import BackendError
from jiuwen_memory.common.tokenizer.tokenizer_impl.whitespace_tokenizer import WhitespaceTokenizer
from jiuwen_memory.common.type_def.continuity import fingerprint


class TaskSearchIndex:
    """Cache only the current authorized task snapshot, using the shared Embedder.

    The small task catalogue is scanned from Registry before each query. No separate
    database or MemoryUnit is created, and stale/deleted tasks cannot survive that scan.
    """

    def __init__(self, embedder=None, tokenizer=None):
        self.embedder = embedder
        self.tokenizer = tokenizer if tokenizer is not None else WhitespaceTokenizer()
        self._lock = threading.Lock()
        self._vectors: dict[str, tuple[str, list[float]]] = {}

    @staticmethod
    def _normalize(vector) -> list[float]:
        values = [float(value) for value in vector]
        if not values or not all(math.isfinite(value) for value in values):
            raise BackendError("invalid task search embedding")
        norm = math.sqrt(sum(value * value for value in values))
        return [value / norm for value in values] if norm else values

    def search(self, scope, tasks: list[dict], query: str, top_k: int) -> list[dict]:
        candidates = []
        for task in tasks:
            if task.get("state") != "open":
                continue
            if task.get("title") or task.get("description"):
                candidates.append(task)
        if not candidates:
            return []
        if self.embedder is None:
            return self._bm25_search(candidates, query, top_k)
        scope_key = fingerprint(asdict(scope))
        with self._lock:
            current = {}
            missing = []
            for task in candidates:
                text = f"{task.get('title', '')}\n{task.get('description', '')}".strip()
                key = fingerprint({"scope": scope_key, "task": task["task_id"]})
                digest = fingerprint(text)
                cached = self._vectors.get(key)
                if cached is not None and cached[0] == digest:
                    current[key] = cached
                else:
                    missing.append((key, digest, text))
            if missing:
                vectors = self.embedder.embed([entry[2] for entry in missing])
                if len(vectors) != len(missing):
                    raise BackendError("task search embedding count mismatch")
                for (key, digest, _), vector in zip(missing, vectors, strict=True):
                    current[key] = (digest, self._normalize(vector))
            query_vector = self._normalize(self.embedder.embed_query(query))
            ranked = []
            for task in candidates:
                key = fingerprint({"scope": scope_key, "task": task["task_id"]})
                vector = current[key][1]
                if len(vector) != len(query_vector):
                    raise BackendError("task search embedding dimension changed")
                score = sum(a * b for a, b in zip(vector, query_vector, strict=True))
                if score > 0:
                    ranked.append((score, task))
            # A new snapshot also evicts old scopes, keeping this projection bounded.
            self._vectors = current
            return self._results(ranked, top_k, "description_semantic")

    def _bm25_search(self, candidates: list[dict], query: str, top_k: int) -> list[dict]:
        terms = sorted(set(self.tokenizer.tokenize(query.lower())))
        if not terms:
            return []
        frequencies = []
        document_frequency = Counter()
        for task in candidates:
            text = f"{task.get('title', '')}\n{task.get('description', '')}"
            frequency = Counter(self.tokenizer.tokenize(text.lower()))
            frequencies.append(frequency)
            document_frequency.update(frequency.keys())
        lengths = [sum(frequency.values()) for frequency in frequencies]
        average_length = sum(lengths) / len(candidates)
        if not average_length:
            return []
        ranked = []
        # Okapi BM25 with positive Lucene-style IDF, k1=1.2 and b=0.75.
        for task, frequency, length in zip(candidates, frequencies, lengths, strict=True):
            score = 0.0
            normalization = 1.2 * (0.25 + 0.75 * length / average_length)
            for term in terms:
                count = frequency.get(term, 0)
                if not count:
                    continue
                df = document_frequency[term]
                idf = math.log1p((len(candidates) - df + 0.5) / (df + 0.5))
                score += idf * count * 2.2 / (count + normalization)
            if score > 0:
                ranked.append((score, task))
        return self._results(ranked, top_k, "description_bm25")

    @staticmethod
    def _results(ranked: list[tuple[float, dict]], top_k: int, match_type: str) -> list[dict]:
        ranked.sort(key=lambda item: (-item[0], item[1]["task_id"]))
        return [{"task_id": task["task_id"], "revision": task["revision"],
                 "title": task.get("title", ""),
                 "description": task.get("description", ""), "state": task["state"],
                 "match_type": match_type, "score": score}
                for score, task in ranked[:top_k]]
