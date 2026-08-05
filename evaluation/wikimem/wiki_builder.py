"""Dataset-independent Wiki construction for wikimem.

The builder exposes two explicit paths.  ``mode="llm"`` compiles raw source
blocks into an ontology, resolves entities, and consolidates duplicate
memories.  ``mode="deterministic"`` is a small, reproducible fallback for
regression and for environments where no model is configured.  The benchmark
adapter keeps its historical renderer for exact Rust-compatible baselines;
this module is the open-domain construction path.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable, Literal

from common.llm.base import LLM
from common.type_def.chat import ChatMessage

from evaluation.wikimem.llm_semantics import (
    MEMORY_KINDS,
    SemanticEntity,
    SemanticMemory,
    SemanticSource,
    _parse_json_payload,
    extract_semantic_memories,
)
from evaluation.wikimem.qmd_consensus import RetrievedMemoryFile


WikiBuilderMode = Literal["deterministic", "llm"]


@dataclass(frozen=True)
class WikiBuildDiagnostics:
    mode: WikiBuilderMode
    llm_used: bool
    source_count: int
    extracted_count: int
    canonical_entity_count: int
    consolidated_count: int
    fallback_reason: str = ""


@dataclass(frozen=True)
class WikiBuildResult:
    files: list[RetrievedMemoryFile]
    memories: list[SemanticMemory]
    entities: list[SemanticEntity]
    synthesis: dict[str, str]
    diagnostics: WikiBuildDiagnostics


class EntityResolver:
    """Resolve obvious aliases while retaining the original display names."""

    _STOPWORDS = {"the", "a", "an", "inc", "corp", "corporation", "company", "co", "model"}

    def __init__(self) -> None:
        self._canonical: dict[str, SemanticEntity] = {}
        self._aliases: dict[str, str] = {}

    def resolve(
        self,
        entities: Iterable[SemanticEntity],
    ) -> tuple[list[SemanticEntity], dict[str, str]]:
        for entity in entities:
            key = self._key(entity.name)
            if not key:
                continue
            canonical = self._aliases.get(key)
            if canonical is None:
                canonical = f"entity.{self._slug(key)}"
                self._aliases[key] = canonical
                self._canonical[canonical] = replace(
                    entity,
                    name=canonical,
                    aliases=tuple(dict.fromkeys((entity.name, *entity.aliases))),
                )
            else:
                current = self._canonical[canonical]
                description = current.description or entity.description
                entity_type = (
                    current.entity_type
                    if current.entity_type != "thing"
                    else entity.entity_type
                )
                self._canonical[canonical] = replace(
                    current,
                    entity_type=entity_type,
                    description=description,
                    aliases=tuple(dict.fromkeys((*current.aliases, entity.name, *entity.aliases))),
                )
        return list(self._canonical.values()), dict(self._aliases)

    def rewrite_memory(self, memory: SemanticMemory, aliases: dict[str, str]) -> SemanticMemory:
        entities = tuple(
            replace(entity, name=aliases.get(self._key(entity.name), entity.name))
            for entity in memory.entities
        )
        relations = tuple(
            replace(
                relation,
                subject=aliases.get(self._key(relation.subject), relation.subject),
                object=aliases.get(self._key(relation.object), relation.object),
            )
            for relation in memory.relations
        )
        return replace(memory, entities=entities, relations=relations)

    @classmethod
    def _key(cls, value: str) -> str:
        words = re.findall(r"[\w]+", value.casefold(), flags=re.UNICODE)
        return " ".join(word for word in words if word not in cls._STOPWORDS)

    @staticmethod
    def _slug(value: str) -> str:
        slug = re.sub(r"[^\w-]+", "-", value.casefold(), flags=re.UNICODE).strip("-")[:80]
        if slug:
            return slug
        return f"unknown-{hashlib.sha1(value.encode('utf-8')).hexdigest()[:12]}"


class MemoryConsolidator:
    """Merge duplicate semantic records and optionally synthesize summaries."""

    def consolidate(self, memories: Iterable[SemanticMemory]) -> list[SemanticMemory]:
        merged: dict[tuple[str, str], SemanticMemory] = {}
        for memory in memories:
            entity_key = ",".join(sorted(entity.name for entity in memory.entities))
            content_key = re.sub(r"\s+", " ", memory.content.casefold()).strip()
            key = (memory.kind, entity_key + "|" + content_key)
            previous = merged.get(key)
            if previous is None:
                merged[key] = memory
                continue
            evidence = previous.evidence or memory.evidence
            source_id = previous.source_id if previous.source_id == memory.source_id else (
                f"{previous.source_id},{memory.source_id}"
            )
            merged[key] = replace(
                previous,
                source_id=source_id,
                evidence=evidence,
                confidence=max(previous.confidence, memory.confidence),
                tags=tuple(dict.fromkeys((*previous.tags, *memory.tags)))[:8],
            )
        return list(merged.values())


class TemplateExtractor:
    """Deterministic source-to-memory extractor kept for regression fallback."""

    def extract(self, sources: Iterable[SemanticSource]) -> list[SemanticMemory]:
        result: list[SemanticMemory] = []
        for source in sources:
            if not source.text.strip():
                continue
            digest = hashlib.sha1(
                f"{source.source_id}:{source.text}".encode("utf-8")
            ).hexdigest()[:16]
            result.append(
                SemanticMemory(
                    memory_id=f"{source.source_id}:context:{digest}",
                    kind="context",
                    content=source.text.strip(),
                    source_id=source.source_id,
                    evidence=source.text.strip(),
                    timestamp=source.timestamp,
                    confidence=1.0,
                    metadata=dict(source.metadata),
                )
            )
        return result


class WikiBuilder:
    """Compile source blocks into a canonical, provenance-preserving Wiki."""

    def __init__(
        self,
        llm: LLM | None = None,
        *,
        mode: WikiBuilderMode = "llm",
        batch_size: int = 8,
        max_tokens: int = 4096,
        allow_fallback: bool = True,
    ) -> None:
        if mode not in {"llm", "deterministic"}:
            raise ValueError(f"unsupported wiki builder mode: {mode!r}")
        self.llm = llm
        self.mode = mode
        self.batch_size = batch_size
        self.max_tokens = max_tokens
        self.allow_fallback = allow_fallback

    def build(
        self,
        sources: Iterable[SemanticSource],
        sample_root: str | Path,
        *,
        wiki_mode: str = "text",
    ) -> WikiBuildResult:
        source_list = [source for source in sources if source.text.strip()]
        fallback_reason = ""
        llm_used = self.mode == "llm" and self.llm is not None
        if llm_used:
            try:
                memories = extract_semantic_memories(
                    self.llm, source_list, batch_size=self.batch_size, max_tokens=self.max_tokens
                )
            except Exception as exc:
                if not self.allow_fallback:
                    raise
                memories = TemplateExtractor().extract(source_list)
                llm_used = False
                fallback_reason = f"llm extraction failed: {type(exc).__name__}: {exc}"
        else:
            memories = TemplateExtractor().extract(source_list)
            if self.mode == "llm" and self.llm is None:
                fallback_reason = "no LLM configured"

        source_metadata = {source.source_id: source.metadata for source in source_list}
        memories = [
            replace(
                memory,
                metadata={
                    **source_metadata.get(memory.source_id.split(",", 1)[0], {}),
                    **memory.metadata,
                },
            )
            for memory in memories
        ]

        resolver = EntityResolver()
        extracted_count = len(memories)
        all_entities = [entity for memory in memories for entity in memory.entities]
        entities, aliases = resolver.resolve(all_entities)
        memories = [resolver.rewrite_memory(memory, aliases) for memory in memories]
        memories = MemoryConsolidator().consolidate(memories)
        synthesis: dict[str, str] = {}
        if llm_used and self.llm is not None:
            try:
                synthesis = self._synthesize(memories)
            except Exception as exc:
                if not self.allow_fallback:
                    raise
                fallback_reason = fallback_reason or (
                    f"llm synthesis failed: {type(exc).__name__}: {exc}"
                )
        files = self._render(
            source_list,
            memories,
            entities,
            sample_root,
            wiki_mode=wiki_mode,
            synthesis=synthesis,
        )
        diagnostics = WikiBuildDiagnostics(
            mode=self.mode,
            llm_used=llm_used,
            source_count=len(source_list),
            extracted_count=extracted_count,
            canonical_entity_count=len(entities),
            consolidated_count=len(memories),
            fallback_reason=fallback_reason,
        )
        return WikiBuildResult(
            files=files,
            memories=memories,
            entities=entities,
            synthesis=synthesis,
            diagnostics=diagnostics,
        )

    def _synthesize(self, memories: list[SemanticMemory]) -> dict[str, str]:
        if self.llm is None or not memories:
            return {}
        payload = json.dumps(
            [
                {
                    "kind": memory.kind,
                    "content": memory.content,
                    "entities": [entity.name for entity in memory.entities],
                    "timestamp": memory.timestamp,
                    "provenance": memory.source_id,
                }
                for memory in memories
            ],
            ensure_ascii=False,
        )
        response = self.llm.chat(
            [
                ChatMessage(
                    role="system",
                    content=(
                        "You synthesize long-term memory. Return only JSON with string keys "
                        "profile, timeline, decisions. Do not add facts not present in input."
                    ),
                ),
                ChatMessage(role="user", content=payload),
            ],
            temperature=0.0,
            max_tokens=min(self.max_tokens, 4096),
        )
        parsed = _parse_json_payload(response)
        if not isinstance(parsed, dict):
            raise ValueError("LLM memory synthesis must return a JSON object")
        return {
            key: str(parsed.get(key, "")).strip()
            for key in ("profile", "timeline", "decisions")
            if str(parsed.get(key, "")).strip()
        }

    def _render(
        self,
        sources: list[SemanticSource],
        memories: list[SemanticMemory],
        entities: list[SemanticEntity],
        sample_root: str | Path,
        *,
        wiki_mode: str,
        synthesis: dict[str, str],
    ) -> list[RetrievedMemoryFile]:
        root = Path(sample_root).as_posix().rstrip("/")
        files: list[RetrievedMemoryFile] = []
        source_by_id = {source.source_id: source for source in sources}
        index_lines = [
            "# Memory Index",
            "",
            "This Wiki is compiled from source blocks with explicit provenance.",
            "",
        ]
        for memory in memories:
            slug = self._slug(memory.memory_id)
            path = f"{root}/wiki/memory/{memory.kind}/{slug}.md"
            source_ids = memory.source_id.split(",")
            source = source_by_id.get(source_ids[0], SemanticSource(source_ids[0], ""))
            entity_names = ", ".join(entity.name for entity in memory.entities) or "(none)"
            metadata_lines = [f"{key}: {value}" for key, value in sorted(memory.metadata.items())]
            relation_lines = [
                f"- {item.subject} --{item.predicate}--> {item.object}"
                for item in memory.relations
            ]
            related_lines = [
                f"- [source {source_id}](../../sources/{self._slug(source_id)}.md)"
                for source_id in source_ids
            ] + [
                f"- [entity {entity.name}](../../entities/{self._slug(entity.name)}.md)"
                for entity in memory.entities
            ]
            body = [
                "---",
                f"memory_type: {memory.kind}",
                f"confidence: {memory.confidence:.3f}",
                f"entities: {entity_names}",
                f"provenance: {', '.join(source_ids)}",
                *metadata_lines,
                "---",
                f"# {memory.kind.title()} memory",
                "",
                memory.content,
                "",
                f"Evidence: {memory.evidence or '(not supplied)'}",
                f"Source: {source.source_id} ({source.conversation_id or 'unknown conversation'})",
            ]
            if relation_lines:
                body.extend(["", "## Relations", *relation_lines])
            if related_lines:
                body.extend(["", "## Provenance links", *related_lines])
            files.append(
                RetrievedMemoryFile(
                    filename=f"{slug}.md",
                    file_path=path,
                    mtime_ms=len(files) + 1,
                    content="\n".join(body) + "\n",
                    description=memory.content[:160],
                    memory_type=memory.kind,
                )
            )
            index_lines.append(
                f"- [{memory.kind}]({path[len(root) + 1:]}) - {memory.content[:120]}"
            )

        for entity in entities:
            slug = self._slug(entity.name)
            related = [
                memory
                for memory in memories
                if any(item.name == entity.name for item in memory.entities)
            ]
            content = [
                "---",
                f"entity_type: {entity.entity_type}",
                f"canonical_id: {entity.name}",
                f"aliases: {', '.join(entity.aliases) or entity.name}",
                "---",
                f"# Entity {entity.name}",
                "",
                entity.description or "Canonical entity resolved from source mentions.",
                "",
                "## Memories",
                *[
                    f"- [{memory.kind} memory](../memory/{memory.kind}/"
                    f"{self._slug(memory.memory_id)}.md): "
                    f"{memory.content}"
                    for memory in related
                ],
            ]
            files.append(
                RetrievedMemoryFile(
                    filename=f"{slug}.md",
                    file_path=f"{root}/wiki/entities/{slug}.md",
                    mtime_ms=len(files) + 1,
                    content="\n".join(content) + "\n",
                    description=f"canonical entity {entity.name}",
                    memory_type="entity",
                )
            )

        source_lines = ["# Sources", ""]
        session_groups: dict[str, list[SemanticSource]] = {}
        for source in sources:
            slug = self._slug(source.source_id)
            session_groups.setdefault(source.session_id or "unknown", []).append(source)
            source_lines.append(f"- [{source.source_id}](wiki/sources/{slug}.md)")
            files.append(
                RetrievedMemoryFile(
                    filename=f"{slug}.md",
                    file_path=f"{root}/wiki/sources/{slug}.md",
                    mtime_ms=len(files) + 1,
                    content=(
                        f"---\nsource_id: {source.source_id}\n"
                        f"conversation_id: {source.conversation_id}\n"
                        f"session_id: {source.session_id}\nspeaker: {source.speaker}\n"
                        + "".join(
                            f"{key}: {value}\n"
                            for key, value in sorted(source.metadata.items())
                        )
                        + f"---\n# Source {source.source_id}\n\n{source.text}\n"
                    ),
                    description=f"raw source {source.source_id}",
                )
            )
        for session_id, session_sources in sorted(session_groups.items()):
            session_number = self._session_number(session_id)
            if session_number is None:
                continue
            session_links = [
                f"- [{source.source_id}](../sources/{self._slug(source.source_id)}.md)"
                for source in session_sources
            ]
            files.append(
                RetrievedMemoryFile(
                    filename=f"session_{session_number}.md",
                    file_path=f"{root}/wiki/sources/session_{session_number}.md",
                    mtime_ms=len(files) + 1,
                    content=(
                        f"---\ndescription: Session {session_number} source index\n"
                        f"type: project\nsession_id: {session_id}\n---\n"
                        f"# Session {session_number}\n\n" + "\n".join(session_links) + "\n"
                    ),
                    description=f"session {session_number} source index",
                )
            )

        files.extend(
            [
                RetrievedMemoryFile(
                    filename="MEMORY.md",
                    file_path=f"{root}/MEMORY.md",
                    mtime_ms=0,
                    content="\n".join(index_lines + source_lines) + "\n",
                    description="canonical semantic memory index",
                ),
                RetrievedMemoryFile(
                    filename="index.md",
                    file_path=f"{root}/index.md",
                    mtime_ms=len(files) + 1,
                    content="\n".join(index_lines + source_lines) + "\n",
                ),
                RetrievedMemoryFile(
                    filename="profile.md",
                    file_path=f"{root}/wiki/synthesis/profile.md",
                    mtime_ms=len(files) + 2,
                    content=self._render_profile(memories, synthesis),
                ),
                RetrievedMemoryFile(
                    filename="timeline.md",
                    file_path=f"{root}/wiki/synthesis/timeline.md",
                    mtime_ms=len(files) + 3,
                    content=synthesis.get("timeline", self._render_timeline(memories)),
                ),
                RetrievedMemoryFile(
                    filename="decisions.md",
                    file_path=f"{root}/wiki/synthesis/decisions.md",
                    mtime_ms=len(files) + 4,
                    content=synthesis.get("decisions", self._render_decisions(memories)),
                ),
                RetrievedMemoryFile(
                    filename=".wiki-schema.md",
                    file_path=f"{root}/.wiki-schema.md",
                    mtime_ms=0,
                    content=self._render_schema(wiki_mode),
                ),
                RetrievedMemoryFile(
                    filename="log.md",
                    file_path=f"{root}/log.md",
                    mtime_ms=len(files) + 5,
                    content=(
                        f"# Build log\n\nmode: {self.mode}\n"
                        f"sources: {len(sources)}\nmemories: {len(memories)}\n"
                    ),
                ),
                RetrievedMemoryFile(
                    filename="sources.json",
                    file_path=f"{root}/raw/sources.json",
                    mtime_ms=0,
                    content=json.dumps(
                        [source.__dict__ for source in sources],
                        ensure_ascii=False,
                        indent=2,
                    ),
                ),
            ]
        )
        return files

    @staticmethod
    def _render_profile(memories: list[SemanticMemory], synthesis: dict[str, str]) -> str:
        if synthesis.get("profile"):
            return "# Profile\n\n" + synthesis["profile"] + "\n"
        lines = [
            "# Profile",
            "",
            "Stable facts, preferences, skills, and decisions consolidated from memory.",
            "",
        ]
        for kind in ("preference", "skill", "decision", "constraint", "fact", "context"):
            items = [memory for memory in memories if memory.kind == kind]
            if items:
                lines.extend([f"## {kind.title()}", *[f"- {item.content}" for item in items], ""])
        return "\n".join(lines)

    @staticmethod
    def _render_timeline(memories: list[SemanticMemory]) -> str:
        rows = sorted(
            (memory for memory in memories if memory.timestamp),
            key=lambda item: item.timestamp,
        )
        lines = ["# Timeline", ""]
        lines.extend(f"- {memory.timestamp}: {memory.content}" for memory in rows)
        return "\n".join(lines) + "\n"

    @staticmethod
    def _render_decisions(memories: list[SemanticMemory]) -> str:
        lines = ["# Decisions", ""]
        lines.extend(
            f"- {memory.content}"
            for memory in memories
            if memory.kind in {"decision", "constraint", "preference"}
        )
        return "\n".join(lines) + "\n"

    @staticmethod
    def _render_schema(wiki_mode: str) -> str:
        return (
            "# Canonical Memory Wiki Schema\n\n"
            f"- memory kinds: {', '.join(MEMORY_KINDS)}\n"
            "- every memory page carries evidence, provenance, entities, confidence, "
            "and relations\n"
            f"- source modality: {wiki_mode}\n"
        )

    @staticmethod
    def _slug(value: str) -> str:
        slug = re.sub(r"[^\w-]+", "-", value, flags=re.UNICODE).strip("-")[:100]
        return slug or f"memory-{hashlib.sha1(value.encode('utf-8')).hexdigest()[:12]}"

    @staticmethod
    def _session_number(value: str) -> int | None:
        match = re.search(r"(?:session_|D)(\d+)", value)
        return int(match.group(1)) if match else None
