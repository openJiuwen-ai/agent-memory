"""Publish protected Markdown views with the corresponding shadow versions."""

from __future__ import annotations

import copy
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import PurePosixPath

from jiuwen_memory.common.errors import NotFoundError, ValidationError
from jiuwen_memory.common.log import get_logger
from jiuwen_memory.common.security.privacy.base import PrivacyProtection
from jiuwen_memory.common.type_def import (
    MD_FILENAME_KEY,
    MD_TITLE_KEY,
    MEMORY_CLASS_KEY,
    LifecycleState,
    MemoryUnit,
    Scope,
)
from jiuwen_memory.storage.markdown import MarkdownStore
from jiuwen_memory.storage.markdown_impl._md_blocks import document_line, edited_document_text
from jiuwen_memory.storage.shadow import DocumentShadowIndex, ProtectedDocument
from jiuwen_memory.storage.sync_gate import document_operation

logger = get_logger(__name__)
_MAX_CHANGED_BLOCKS = 64
_EMPTY = ProtectedDocument("", ())


def _split_blocks(text: str) -> list[str]:
    if not text:
        return []
    if not text.endswith("\n\n"):
        raise ValidationError("Protected edits must preserve the Markdown block layout")
    return text[:-2].split("\n\n")


def _fields(block: str) -> tuple[str, str]:
    lines = block.split("\n")
    if len(lines) != 2 or not lines[0].startswith("# ") or not lines[1].strip():
        raise ValidationError("Protected Markdown requires a heading and one body line per block")
    return lines[0][2:], lines[1]


def _render(unit: MemoryUnit) -> str:
    return f"# {unit.system_metadata.get(MD_TITLE_KEY, '')}\n{unit.segments[0].content}\n\n"


class ProtectedDocuments:
    def __init__(
        self, markdown: MarkdownStore, shadow: DocumentShadowIndex, privacy: PrivacyProtection,
    ) -> None:
        if not markdown.supports_protected_documents or not shadow.supports_protected_documents:
            raise ValidationError("Document privacy requires protected Markdown and shadow ports")
        self.markdown = markdown
        self.shadow = shadow
        self.privacy = privacy

    def _verified(self, path: str) -> ProtectedDocument | None:
        snapshot = self.shadow.protected_document(path)
        expected = snapshot.text if snapshot else None
        if not self.markdown.matches_protected_text(path, expected):
            raise ValidationError("Markdown differs from its last protected version")
        return snapshot

    @contextmanager
    def _publish_files(
        self,
        changes: dict[str, tuple[ProtectedDocument | None, ProtectedDocument]],
    ) -> Iterator[None]:
        published = []
        try:
            for path, (before, after) in changes.items():
                previous = before.text if before else None
                self.markdown.replace_protected_text(path, previous, after.text)
                published.append((path, after.text, previous))
            yield
        except BaseException:
            for path, current, previous in reversed(published):
                try:
                    self.markdown.replace_protected_text(path, current, previous)
                except Exception:
                    logger.error("Protected Markdown rollback unavailable")
            raise

    def _commit(
        self,
        units: list[MemoryUnit],
        superseded: list[MemoryUnit],
        make_changes: Callable[[], dict[str, tuple[ProtectedDocument | None, ProtectedDocument]]],
    ) -> None:
        def stage(index: int) -> None:
            if index == len(units):
                changes = make_changes()
                self.shadow.commit_protected_documents(
                    changes, units, superseded, lambda: self._publish_files(changes),
                )
                return
            unit = units[index]

            def read_back() -> MemoryUnit | None:
                return next(iter(self.shadow.get_units(unit.scope, [unit.id])), None)

            self.privacy.write(unit, lambda _: stage(index + 1), read_back)

        if len(units) > _MAX_CHANGED_BLOCKS:
            raise ValidationError("Protected document operation changes too many blocks")
        stage(0)

    def _route(self, unit: MemoryUnit) -> str:
        metadata = unit.system_metadata
        if (
            metadata.get("collective_destination")
            or metadata.get(MEMORY_CLASS_KEY) == "team_memory"
            or metadata.get("memory_type") in {"experience", "skill"}
        ):
            raise ValidationError("Protected document writes currently support personal memories")
        path = self.markdown.locate_protected_unit(unit)
        if self.privacy.preview(path, label=MD_FILENAME_KEY) != path:
            raise ValidationError("Sensitive data is not allowed in a Markdown path")
        return path

    def _prepare(self, unit: MemoryUnit) -> None:
        if len(unit.segments) != 1:
            raise ValidationError("Protected Markdown requires one text segment")
        self.privacy.prepare(unit)
        segment = unit.segments[0]
        body = document_line(segment.content.replace("\r", "\n"))
        if not body.strip():
            raise ValidationError("Protected Markdown requires a non-empty body")
        if body != segment.content:
            unit.vectors.clear()
            segment.content = body
        title = unit.system_metadata.get(MD_TITLE_KEY) or ""
        if not isinstance(title, str):
            raise ValidationError("Protected Markdown title must be text")
        unit.system_metadata[MD_TITLE_KEY] = document_line(title.replace("\r", "\n"))

    def add(self, units: list[MemoryUnit]) -> None:
        with document_operation(
            self.markdown.root, self.shadow.write_lock_key, writing=True,
        ), self.privacy.staging_scope():
            groups: dict[str, list[MemoryUnit]] = {}
            for unit in units:
                path = self._route(unit)
                self._prepare(unit)
                groups.setdefault(path, []).append(unit)
            before = {path: self._verified(path) for path in groups}

            def changes():
                return {
                    path: (before[path], self._append(before[path] or _EMPTY, batch))
                    for path, batch in groups.items()
                }

            self._commit(units, [], changes)

    @staticmethod
    def _append(old: ProtectedDocument, units: list[MemoryUnit]) -> ProtectedDocument:
        return ProtectedDocument(
            old.text + "".join(_render(unit) for unit in units),
            old.unit_ids + tuple(unit.id for unit in units),
        )

    def read(
        self, scope: Scope, path: str, line_range: tuple[int, int] | None,
    ) -> list[MemoryUnit]:
        with document_operation(self.markdown.root, self.shadow.write_lock_key):
            snapshot = self._verified(path)
            if snapshot is None:
                return []
            ids = snapshot.unit_ids
            if line_range is not None:
                start, end = line_range
                ids = tuple(uid for i, uid in enumerate(ids) if start <= 3 * i + 2 <= end)
            return [
                unit for unit in self.shadow.get_units(scope, list(ids))
                if unit.lifecycle == LifecycleState.ACTIVE
            ]

    def edit(self, scope: Scope, path: str, old_text: str, new_text: str) -> list[MemoryUnit]:
        if not isinstance(old_text, str) or not old_text or not isinstance(new_text, str):
            raise ValidationError("Protected document edit requires two text arguments")
        with document_operation(
            self.markdown.root, self.shadow.write_lock_key, writing=True,
        ), self.privacy.staging_scope():
            old = self._verified(path)
            if old is None:
                raise NotFoundError("protected Markdown document", path)
            match = old.text.find(old_text)
            if match < 0:
                raise NotFoundError("protected Markdown passage", path)
            if old.text.find(old_text, match + 1) >= 0:
                raise ValidationError("Protected Markdown passage must occur exactly once")
            old_blocks = _split_blocks(old.text)
            candidate = edited_document_text(old.text, PurePosixPath(path).name, old_text, new_text)
            new_blocks = _split_blocks(candidate)
            if len(old_blocks) != len(new_blocks) or len(old_blocks) != len(old.unit_ids):
                raise ValidationError("Protected edits must preserve the Markdown block count")
            indices = [
                i for i, (before, after) in enumerate(zip(old_blocks, new_blocks))
                if before != after
            ]
            if not indices:
                return []
            ids = [old.unit_ids[i] for i in indices]
            by_id = {
                unit.id: unit for unit in self.shadow.get_units(scope, ids)
                if unit.lifecycle == LifecycleState.ACTIVE
            }
            if len(by_id) != len(ids):
                raise ValidationError("Protected edits require active blocks in the request scope")
            now = datetime.now(UTC)
            inserted = [
                self._edited(by_id[uid], new_blocks[index], now)
                for index, uid in zip(indices, ids)
            ]

            def changes():
                updated_ids = list(old.unit_ids)
                rendered = [block + "\n\n" for block in old_blocks]
                for index, unit in zip(indices, inserted):
                    updated_ids[index] = unit.id
                    rendered[index] = _render(unit)
                return {path: (old, ProtectedDocument("".join(rendered), tuple(updated_ids)))}

            self._commit(inserted, list(by_id.values()), changes)
            return inserted

    def _edited(self, old: MemoryUnit, block: str, now: datetime) -> MemoryUnit:
        title, body = _fields(block)
        new = copy.deepcopy(old)
        new.id = str(uuid.uuid4())
        new.supersedes = old.id
        new.temporal.t_valid = now
        new.temporal.t_invalid = None
        new.system_metadata[MD_TITLE_KEY] = title
        new.segments[0].content = body
        new.vectors.clear()
        self._prepare(new)
        old.lifecycle = LifecycleState.SUPERSEDED
        old.temporal.t_invalid = now
        return new
