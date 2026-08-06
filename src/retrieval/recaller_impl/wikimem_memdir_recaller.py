"""wikimem Markdown memory directory DOCUMENT recaller."""

from __future__ import annotations

import hashlib
from pathlib import Path

from common.type_def import Scope
from retrieval.base import RetrievalOperatorType
from retrieval.wikimem_memdir import (
    WikimemDirectory,
    load_memory_entrypoints,
    load_relevant_memory_files,
)
from retrieval.wikimem_options import parse_wikimem_options
from retrieval.recaller import Recaller, RecallerProducer
from retrieval.types import ParsedQuery, RecallChannel, ScoredUnit


def memory_file_unit_id(scope_name: str, file_path: str) -> str:
    """Return the deterministic MemoryUnit id used for a projected memory file."""

    normalized = str(Path(file_path).expanduser()).replace("\\", "/").lower()
    digest = hashlib.sha256(f"{scope_name}:{normalized}".encode("utf-8")).hexdigest()[:24]
    return f"wikimem:file:{scope_name}:{digest}"


def memory_entrypoint_unit_id(scope_name: str, file_path: str) -> str:
    """Return the deterministic MemoryUnit id used for a projected MEMORY.md entrypoint."""

    normalized = str(Path(file_path).expanduser()).replace("\\", "/").lower()
    digest = hashlib.sha256(f"{scope_name}:{normalized}".encode("utf-8")).hexdigest()[:24]
    return f"wikimem:entrypoint:{scope_name}:{digest}"


class WikimemMemdirRecaller(Recaller):
    """Recall wikimem Markdown file candidates through the DOCUMENT channel."""

    def operator_type(self) -> RetrievalOperatorType:
        return RetrievalOperatorType.RECALLER

    def health(self) -> None:
        return None

    def channel(self) -> RecallChannel:
        return RecallChannel.DOCUMENT

    def recall(self, scope: Scope, query: ParsedQuery, top_k: int) -> list[ScoredUnit]:
        options = parse_wikimem_options(query.extensions)
        if not options.memory_dirs:
            return []

        directories = [
            WikimemDirectory(scope=directory.scope, path=directory.path)
            for directory in options.memory_dirs
        ]
        text = query.rewritten or query.raw
        files = load_relevant_memory_files(
            text,
            directories,
            top_k=top_k,
            recent_tools=options.recent_tools,
            already_surfaced_file_paths=options.already_surfaced_file_paths,
        )
        results = [
            ScoredUnit(
                unit_id=memory_file_unit_id(file.scope, file.file_path),
                score=float(max(top_k - index, 1)),
                channel=RecallChannel.DOCUMENT,
            )
            for index, file in enumerate(files)
        ]
        if options.include_entrypoints:
            for entrypoint in load_memory_entrypoints(directories):
                results.append(
                    ScoredUnit(
                        unit_id=memory_entrypoint_unit_id(entrypoint.scope, entrypoint.file_path),
                        score=1.0,
                        channel=RecallChannel.DOCUMENT,
                    )
                )
        return results[:top_k]


@RecallerProducer.register("wikimem_memdir")
def _build(config):
    return WikimemMemdirRecaller()
