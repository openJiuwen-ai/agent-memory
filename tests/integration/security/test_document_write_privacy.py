"""The protected document boundary covers disk, shadow, and existing human access."""

import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Barrier

import pytest

from jiuwen_memory.api import (
    Context,
    MemoryPatch,
    NotFoundError,
    ValidationError,
    assemble_server_runtime,
)
from jiuwen_memory.common.factory.factory import Factory
from jiuwen_memory.storage.shadow_impl.sqlite_shadow_index import SqliteDocumentShadowIndex
from jiuwen_memory.storage.watchdog_impl.local_watchdog import LocalWatchdog
from tests.integration.portal.test_document_portal import document_config
from tests.integration.security.test_memory_privacy import INVITE, SCOPE, config, security
from tests.support.virtual_passkey import VirtualPasskey

pytestmark = pytest.mark.integration


def settings(tmp_path):
    values = document_config(tmp_path / "documents", watch=True)
    secure = config(tmp_path, human=True)
    for key in (
        "privacy", "privacy_store", "cryptography", "key_provider", "human_authenticator",
    ):
        values[key] = secure[key]
    values["globals"]["privacy_enabled"] = True
    return values


def _file(tmp_path, unit):
    return tmp_path / "documents" / unit.system_metadata["md_filename"]


def test_document_add_edit_recall_and_human_view(tmp_path):
    runtime = assemble_server_runtime(config=settings(tmp_path))
    ctx = security()
    context = Context(scope=SCOPE)
    phone = "13800138000"
    new_phone = "13900139001"
    try:
        api = runtime.api
        old = api.add(
            f"联系：{phone}", SCOPE, security=ctx,
            system_metadata={"md_title": f"联系人 {phone}"},
        )[0]
        path = _file(tmp_path, old)
        original = path.read_text(encoding="utf-8")
        assert phone not in original and original.count("[JW-PRIVATE:") == 2
        assert api.read_md_file(context, path.name, security=ctx)[0].id == old.id
        current, = api.edit_md_file(
            context, path.name, old.content, f"联系：{new_phone}", security=ctx,
        )
        assert current.supersedes == old.id
        assert phone not in path.read_text(encoding="utf-8")
        assert new_phone not in path.read_text(encoding="utf-8")
        assert api.get(current.id, SCOPE, security=ctx).content == current.content
        matches = api.search("联系", context, security=ctx)
        assert new_phone not in str(matches)
        assert any(item.unit_id == current.id for item in matches.items)

        view, key = runtime.human_view, VirtualPasskey()
        begin = view.begin({"invite": INVITE}, "browser")
        assert view.finish({
            "challenge_id": begin["challenge_id"],
            "credential": key.response(begin, enroll=True),
        }, "browser") == {"enrolled": True}
        begin = view.begin({
            "scope": {"org": SCOPE.org, "user": SCOPE.user}, "unit_id": current.id,
        }, "browser", security=ctx)
        assert view.finish({
            **begin["target"], "challenge_id": begin["challenge_id"],
            "credential": key.response(begin, counter=2),
        }, "browser")["content"] == f"联系：{new_phone}"
    finally:
        runtime.close()


def test_unverified_documents_do_not_feed_reads_writes_or_recall_after_restart(tmp_path):
    values = settings(tmp_path)
    ctx = security()
    context = Context(scope=SCOPE)
    runtime = assemble_server_runtime(config=values)
    try:
        stored = runtime.api.add("可信记忆 13800138000", SCOPE, security=ctx)[0]
        path = _file(tmp_path, stored)
    finally:
        runtime.close()

    outside = "outside-change 13900139001"
    path.write_text(f"# External\n{outside}\n\n", encoding="utf-8")
    watchdogs = []

    def observe(instance):
        if isinstance(instance, LocalWatchdog):
            watchdogs.append(instance)

    with Factory.observe_builds(observe):
        runtime = assemble_server_runtime(config=values)
    try:
        assert not watchdogs
        api = runtime.api
        with pytest.raises(ValidationError, match="last protected version"):
            api.read_md_file(context, path.name, security=ctx)
        with pytest.raises(ValidationError, match="last protected version"):
            api.edit_md_file(context, path.name, outside, "another edit", security=ctx)
        with pytest.raises(ValidationError, match="last protected version"):
            api.add("新记忆", SCOPE, security=ctx)
        assert outside in path.read_text(encoding="utf-8")
        assert api.get(stored.id, SCOPE, security=ctx).content == stored.content
        assert outside not in str(api.search("outside-change", context, security=ctx))
        with pytest.raises(ValidationError, match="require edit_md_file"):
            api.update(stored.id, SCOPE, MemoryPatch(content="更新"), security=ctx)
    finally:
        runtime.close()


def test_existing_unprotected_shadow_requires_migration(tmp_path):
    previous = document_config(tmp_path / "documents")
    runtime = assemble_server_runtime(config=previous)
    try:
        old, = runtime.api.add("旧数据：13800138000", SCOPE, security=security())
        path = _file(tmp_path, old)
    finally:
        runtime.close()

    with pytest.raises(ValidationError, match="requires privacy migration"):
        assemble_server_runtime(config=settings(tmp_path))
    assert "13800138000" in path.read_text(encoding="utf-8")


def test_markdown_publish_failure_keeps_previous_shadow_and_document(tmp_path, monkeypatch):
    values = settings(tmp_path)
    runtime = assemble_server_runtime(config=values)
    ctx = security()
    context = Context(scope=SCOPE)
    try:
        api = runtime.api
        old = api.add("旧号码 13800138000", SCOPE, security=ctx)[0]
        path = _file(tmp_path, old)
        before = path.read_bytes()
        original_replace = os.replace

        def reject(source, target, **options):
            if Path(target) == path:
                assert b"13900139001" not in Path(source).read_bytes()
                raise OSError("test publish error")
            return original_replace(source, target, **options)

        with monkeypatch.context() as patch:
            patch.setattr(os, "replace", reject)
            with pytest.raises(OSError, match="publish error"):
                api.edit_md_file(
                    context, path.name, old.content, "新号码 13900139001", security=ctx,
                )
        assert path.read_bytes() == before
        assert not any(path.parent.glob(".privacy-*"))
        assert api.read_md_file(context, path.name, security=ctx)[0].id == old.id
        assert api.get(old.id, SCOPE, security=ctx).system_metadata["privacy_revision"] == (
            old.system_metadata["privacy_revision"]
        )
        current, = api.edit_md_file(
            context, path.name, old.content, "新号码 13900139001", security=ctx,
        )
        assert current.supersedes == old.id
        assert "13900139001" not in path.read_text(encoding="utf-8")
        assert not any(path.parent.glob(".privacy-*"))
        if os.name == "posix":
            assert path.stat().st_mode & 0o077 == 0
    finally:
        runtime.close()


def test_shadow_failure_after_markdown_publish_restores_previous_version(tmp_path, monkeypatch):
    runtime = assemble_server_runtime(config=settings(tmp_path))
    ctx = security()
    context = Context(scope=SCOPE)
    try:
        api = runtime.api
        old, = api.add("工单联系：13800138000", SCOPE, security=ctx)
        path = _file(tmp_path, old)
        before = path.read_bytes()
        commit = SqliteDocumentShadowIndex.commit_protected_documents

        def fail_after_publish(self, changes, inserted, superseded, publish):
            @contextmanager
            def reject_commit():
                with publish():
                    assert path.read_bytes() != before
                    assert b"13900139001" not in path.read_bytes()
                    raise OSError("test shadow commit error")

            return commit(self, changes, inserted, superseded, reject_commit)

        with monkeypatch.context() as patch:
            patch.setattr(
                SqliteDocumentShadowIndex, "commit_protected_documents", fail_after_publish,
            )
            with pytest.raises(OSError, match="shadow commit error"):
                api.edit_md_file(
                    context, path.name, old.content, "工单联系：13900139001", security=ctx,
                )
        assert path.read_bytes() == before
        assert [unit.id for unit in api.read_md_file(context, path.name, security=ctx)] == [old.id]
        current, = api.edit_md_file(
            context, path.name, old.content, "工单联系：13900139001", security=ctx,
        )
        assert current.supersedes == old.id
    finally:
        runtime.close()


def test_concurrent_edit_checks_entire_affected_block(tmp_path):
    runtime = assemble_server_runtime(config=settings(tmp_path))
    ctx = security()
    context = Context(scope=SCOPE)
    try:
        api = runtime.api
        old, = api.add("联系电话：13800138待补", SCOPE, security=ctx)
        path = _file(tmp_path, old)
        simultaneous = Barrier(2, timeout=5)

        def edit(suffix):
            simultaneous.wait()
            try:
                return api.edit_md_file(context, path.name, "待补", suffix, security=ctx)[0]
            except NotFoundError:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            committed, = [result for result in pool.map(edit, ["000", "001"]) if result]
        assert committed.supersedes == old.id
        assert committed.content.startswith("联系电话：[JW-PRIVATE:")
        disk = path.read_text(encoding="utf-8")
        assert "13800138" not in disk and "待补" not in disk
        assert [unit.id for unit in api.read_md_file(context, path.name, security=ctx)] == [
            committed.id,
        ]
    finally:
        runtime.close()
