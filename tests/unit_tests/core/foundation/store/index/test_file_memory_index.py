# coding: utf-8
"""FileMemoryIndex regression tests.

Covers the bug-fixes applied on top of the V2 design:
- chunk_parser line-number round-trip + unclosed-frontmatter tolerance
- incremental sync_file with line-number rebase for unchanged blocks
- delete_memories fallback for ids not yet indexed in the DB
- delete_by_path encapsulation (file + chunks + vec/fts cleanup)
- watchdog graceful degradation when watchdog is unavailable
"""

import asyncio

import pytest

from jiuwen_memory.foundation.store.base_memory_index import MemoryDoc
from jiuwen_memory.foundation.store.filter_dsl import (
    FilterCondition,
    FilterGroup,
    FilterOperator,
)
from jiuwen_memory.foundation.store.index.file_index._chunk_parser import (
    Block,
    blocks_to_markdown,
    parse_blocks,
)
from jiuwen_memory.foundation.store.index.file_index._file_watcher import MemoryFileWatcher
from jiuwen_memory.foundation.store.index.file_index._md_store import MarkdownStore
from jiuwen_memory.foundation.store.index.file_index._vector_index import (
    SearchConstraints,
    TenantScope,
    VectorIndex,
)
from jiuwen_memory.foundation.store.index.file_index.file_memory_index import FileMemoryIndex


# ---------------------------------------------------------------------------
# Shared fakes (mirror V1's FakeEmbedding so behaviour is comparable)
# ---------------------------------------------------------------------------


class FakeEmbedding:
    """Deterministic embedding for tests; counts calls to verify cache/incremental."""

    def __init__(self, dim: int = 8):
        self._dim = dim
        self.call_count = 0

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.call_count += len(texts)
        return [self._vec(t) for t in texts]

    async def embed_query(self, text: str) -> list[float]:
        self.call_count += 1
        return self._vec(text)

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * self._dim
        for i, ch in enumerate(text):
            v[i % self._dim] += float(ord(ch))
        norm = sum(x * x for x in v) ** 0.5 or 1.0
        return [x / norm for x in v]


class ReverseCodec:
    @staticmethod
    def encode(text: str) -> str:
        return text[::-1]

    @staticmethod
    def decode(data: str) -> str:
        return data[::-1]


# ===========================================================================
# chunk_parser: line-number round-trip & tolerance
# ===========================================================================


def test_blocks_to_markdown_assigns_accurate_line_numbers():
    blocks = [
        Block(mem_id="m1", type="note", text="first body line1\nfirst body line2"),
        Block(mem_id="m2", type="note", text="second body"),
        Block(mem_id="m3", type="note", text="third"),
    ]
    md = blocks_to_markdown(blocks)

    # Re-parsing must reproduce identical content and matching line numbers.
    reparsed = parse_blocks(md)
    assert [b.mem_id for b in reparsed] == ["m1", "m2", "m3"]
    for original, reparsed_b in zip(blocks, reparsed):
        assert original.start_line == reparsed_b.start_line
        assert original.end_line == reparsed_b.end_line
        assert original.text == reparsed_b.text


def test_parse_blocks_unclosed_frontmatter_not_dropped():
    # m2's frontmatter opens with valid id/type but never closes (no second
    # '---'). The block must still be recovered (not silently dropped) so the
    # memory isn't lost; its id is preserved.
    content = "---\nid: m1\ntype: note\n---\nreal body\n\n---\nid: m2\ntype: note"
    blocks = parse_blocks(content)
    ids = [b.mem_id for b in blocks]
    assert "m2" in ids, "unclosed frontmatter block must not be dropped"
    m2 = next(b for b in blocks if b.mem_id == "m2")
    assert m2.type == "note"


def test_parse_blocks_malformed_frontmatter_not_dropped_silently():
    # A block whose frontmatter YAML is garbage should not abort parsing of
    # subsequent valid blocks.
    content = "---\nid: good1\ntype: note\n---\nbody one\n\n" \
              "---\nnot valid yaml: : :\n---\nshould be body\n\n" \
              "---\nid: good2\ntype: note\n---\nbody two"
    blocks = parse_blocks(content)
    ids = [b.mem_id for b in blocks]
    # good1 and good2 must survive even though the middle block is malformed.
    assert "good1" in ids
    assert "good2" in ids


def test_parse_blocks_empty_and_garbage_input():
    assert parse_blocks("") == []
    assert parse_blocks("   \n  \n") == []
    # No frontmatter at all → no blocks
    assert parse_blocks("just plain text\nno frontmatter here") == []


def test_blocks_to_markdown_empty_list():
    assert blocks_to_markdown([]) == ""


# ===========================================================================
# VectorIndex: incremental sync_file + line-number rebase
# ===========================================================================


@pytest.fixture
def vi(tmp_path):
    return VectorIndex(
        db_path=str(tmp_path / "memory.db"),
        embedding_model=FakeEmbedding(),
    )


def _make_blocks(*pairs: tuple[str, str]) -> list[Block]:
    """Build blocks from (id, text) pairs."""
    return [Block(mem_id=mid, type="note", text=txt) for mid, txt in pairs]


def _write_md(tmp_path, user, scope, type_name, blocks):
    """Serialize blocks to a {Type}.md file and return the raw content."""
    import pathlib
    p = pathlib.Path(tmp_path) / "memories" / user / scope / f"{type_name}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    content = blocks_to_markdown([Block(b.mem_id, b.type, b.text) for b in blocks])
    p.write_text(content, encoding="utf-8")
    return content, str(p)


@pytest.mark.asyncio
async def test_sync_file_only_reembeds_changed_blocks(vi, tmp_path):
    content, _ = _write_md(tmp_path, "u1", "s1", "note", _make_blocks(("m1", "alpha"), ("m2", "beta")))
    rel = "memories/u1/s1/note.md"
    blocks = parse_blocks(content)
    await vi.sync_file(rel, TenantScope("u1", "s1"), "note", content, blocks)
    calls_after_first = vi.embedding_model.call_count

    # Modify only m2's text. m1 is unchanged → must NOT be re-embedded.
    content2, _ = _write_md(tmp_path, "u1", "s1", "note", _make_blocks(("m1", "alpha"), ("m2", "beta CHANGED")))
    blocks2 = parse_blocks(content2)
    await vi.sync_file(rel, TenantScope("u1", "s1"), "note", content2, blocks2)

    # Only one new embedding call (for m2). m1 hit the cache OR was skipped;
    # either way the model is not invoked for it.
    assert vi.embedding_model.call_count - calls_after_first == 1
    # m2 text updated in DB
    assert vi.get_text("m2") == "beta CHANGED"
    vi.close()


@pytest.mark.asyncio
async def test_sync_file_rebases_line_numbers_for_unchanged_blocks(vi, tmp_path):
    # Three blocks; after sync, record m1's line range.
    content, _ = _write_md(tmp_path, "u1", "s1", "note",
                           _make_blocks(("m1", "a1\na2"), ("m2", "b1"), ("m3", "c1")))
    rel = "memories/u1/s1/note.md"
    await vi.sync_file(rel, TenantScope("u1", "s1"), "note", content, parse_blocks(content))

    m3_start_before, m3_end_before = vi.get_chunk_line_numbers("m3")

    # Grow m1 by two lines → m2 and m3 must shift down, even though their text
    # (and thus hash) is unchanged. Without the rebase fix their line numbers
    # would stay stale.
    content2, _ = _write_md(tmp_path, "u1", "s1", "note",
                            _make_blocks(("m1", "a1\na2\na3\na4"), ("m2", "b1"), ("m3", "c1")))
    await vi.sync_file(rel, TenantScope("u1", "s1"), "note", content2, parse_blocks(content2))

    m3_start_after, m3_end_after = vi.get_chunk_line_numbers("m3")

    assert m3_start_after == m3_start_before + 2  # m1 grew by 2 lines
    assert m3_end_after == m3_end_before + 2
    # m3 text unchanged → embedding not recomputed for it
    vi.close()


@pytest.mark.asyncio
async def test_sync_file_short_circuits_when_unchanged(vi, tmp_path):
    content, _ = _write_md(tmp_path, "u1", "s1", "note", _make_blocks(("m1", "alpha")))
    rel = "memories/u1/s1/note.md"
    await vi.sync_file(rel, TenantScope("u1", "s1"), "note", content, parse_blocks(content))
    calls = vi.embedding_model.call_count

    # Re-sync identical content → no embedding calls, no DB churn.
    await vi.sync_file(rel, TenantScope("u1", "s1"), "note", content, parse_blocks(content))
    assert vi.embedding_model.call_count == calls
    vi.close()


@pytest.mark.asyncio
async def test_sync_file_handles_deleted_blocks(vi, tmp_path):
    content, _ = _write_md(tmp_path, "u1", "s1", "note",
                           _make_blocks(("m1", "alpha"), ("m2", "beta")))
    rel = "memories/u1/s1/note.md"
    await vi.sync_file(rel, TenantScope("u1", "s1"), "note", content, parse_blocks(content))

    # Remove m1 from the file → sync should delete it from DB.
    content2, _ = _write_md(tmp_path, "u1", "s1", "note", _make_blocks(("m2", "beta")))
    await vi.sync_file(rel, TenantScope("u1", "s1"), "note", content2, parse_blocks(content2))
    assert vi.get_text("m1") is None
    assert vi.get_text("m2") == "beta"
    vi.close()


def test_delete_by_path_clears_chunks_vec_fts_and_files(vi, tmp_path):
    async def _run():
        content, _ = _write_md(tmp_path, "u1", "s1", "note",
                               _make_blocks(("m1", "alpha"), ("m2", "beta")))
        rel = "memories/u1/s1/note.md"
        await vi.sync_file(rel, TenantScope("u1", "s1"), "note", content, parse_blocks(content))
        assert vi.get_text("m1") is not None
        assert vi.get_file_hash_from_db(rel) is not None

        vi.delete_by_path(rel)
        assert vi.get_text("m1") is None
        assert vi.get_text("m2") is None
        assert vi.get_file_hash_from_db(rel) is None

    asyncio.run(_run())
    vi.close()


@pytest.mark.asyncio
async def test_v2_search_pure_python_fallback(vi):
    """sqlite-vec 不可用时，退化为纯 Python 余弦相似度仍能正确召回。

    V2 的 _search_fallback 按 user_id/scope_id/type 过滤后用 cosine 相似度
    线性扫 chunks.embedding。强制 _vec_available=False 模拟 sqlite-vec 缺失，
    验证：① 能召回相关记忆；② scope 隔离仍生效；③ mem_types 过滤仍生效。
    """
    # 写入两条记忆到 u1/s1，一条到 u2/s1（验证跨 user 隔离）
    await vi.upsert(MemoryDoc(id="a", text="hello world", type="note"), TenantScope("u1", "s1"))
    await vi.upsert(MemoryDoc(id="b", text="hello there", type="note"), TenantScope("u1", "s1"))
    await vi.upsert(MemoryDoc(id="c", text="hello world", type="profile"), TenantScope("u1", "s1"))
    await vi.upsert(MemoryDoc(id="d", text="hello world", type="note"), TenantScope("u2", "s1"))

    # 强制走 fallback（模拟 sqlite-vec 未安装）
    original = vi.vec_available
    vi.vec_available = False
    try:
        qv = await vi.embedding_model.embed_query("hello world")

        # ① 召回相关记忆（u1/s1 下有 a/b/c 三条）
        hits = await vi.search(
            TenantScope("u1", "s1"), qv, top_k=5,
            constraints=SearchConstraints(query_text="hello world"),
        )
        assert len(hits) >= 1
        hit_ids = {h[0] for h in hits}
        assert "a" in hit_ids

        # ② 跨 user 隔离：u1 搜不到 u2 的记忆 d
        assert "d" not in hit_ids

        # ③ mem_types 过滤：只取 note，排除 profile(c)
        hits_note = await vi.search(
            TenantScope("u1", "s1"), qv, top_k=5,
            constraints=SearchConstraints(mem_types=["note"], query_text="hello world"),
        )
        note_ids = {h[0] for h in hits_note}
        assert "c" not in note_ids
        assert "a" in note_ids

        # ④ 分数有区分度（不是全 1.0——fallback 用 cosine 相似度，相关项应高于不相关项）
        scores = [h[1] for h in hits]
        assert max(scores) > 0.0
    finally:
        vi.vec_available = original
    vi.close()


# ===========================================================================
# MarkdownStore: write_blocks propagates recalculated line numbers
# ===========================================================================


@pytest.mark.asyncio
async def test_write_blocks_propagates_line_numbers(tmp_path):
    md = MarkdownStore(root_dir=str(tmp_path))
    import pathlib
    p = pathlib.Path(tmp_path) / "memories" / "u1" / "s1" / "note.md"
    blocks = _make_blocks(("m1", "alpha"), ("m2", "beta"))
    # Start with stale line numbers (as _doc_to_block would produce).
    for b in blocks:
        b.start_line = 0
        b.end_line = 0
    await md.write_blocks(p, blocks)
    # After write, blocks carry accurate line numbers matching the file.
    assert blocks[0].start_line == 1
    assert blocks[1].start_line > blocks[0].end_line
    # And they match what parse_blocks would compute from the file.
    reparsed = parse_blocks(p.read_text(encoding="utf-8"))
    for src, rp in zip(blocks, reparsed):
        assert src.start_line == rp.start_line
        assert src.end_line == rp.end_line


@pytest.mark.asyncio
async def test_md_store_codec_roundtrip_per_block(tmp_path):
    md = MarkdownStore(root_dir=str(tmp_path))
    md.set_codec(ReverseCodec())
    import pathlib
    p = pathlib.Path(tmp_path) / "memories" / "u1" / "s1" / "note.md"
    blocks = _make_blocks(("m1", "secret one"), ("m2", "secret two"))
    await md.write_blocks(p, blocks)

    # On disk, bodies are reversed (ciphertext).
    raw = p.read_text(encoding="utf-8")
    assert "secret one" not in raw
    assert "eno terces" in raw

    # Reading back decodes to plaintext.
    got = await md.read_blocks(p)
    texts = {b.mem_id: b.text for b in got}
    assert texts == {"m1": "secret one", "m2": "secret two"}


@pytest.mark.asyncio
async def test_write_file_atomic_on_crash(tmp_path, monkeypatch):
    """write_text 中途崩溃不应破坏原文件（os.replace 原子换）。

    回归检视意见：裸 write_text 的 open('w') 先 O_TRUNC 清空文件再逐段写，
    写入中途崩溃留下半写残缺文件，损坏半径是整个 {Type}.md（该类型全部
    记忆）。修复后写 .tmp 再 os.replace，崩溃时原文件保持完整旧内容。

    用 monkeypatch 拦截所有 Path.write_text，对"写新内容"调用抛 OSError
    （模拟 write 中途崩溃/磁盘满）。原子版写 .tmp 时抛 → 原文件不坏；
    非原子版（裸写 path）会先 O_TRUNC 再抛 → 原文件被清空破坏。旧内容
    初始化绕过 monkeypatch，确保测试基线正确。
    """
    import pathlib
    md = MarkdownStore(root_dir=str(tmp_path))
    p = pathlib.Path(tmp_path) / "memories" / "u1" / "s1" / "note.md"
    p.parent.mkdir(parents=True, exist_ok=True)

    # 旧内容初始化：绕过 monkeypatch，直接用底层 open 写
    old_content = "---\nid: old\ntype: note\n---\nold memory\n"
    with open(p, "w", encoding="utf-8") as f:
        f.write(old_content)

    # 拦截所有 write_text，模拟"open(truncate) 后写到一半崩溃"。
    # 真实 write_text = open('w')（O_TRUNC 清空）→ write → close；
    # 崩溃发生在 write 之后、close 之前。boom 先 open('w') 触发 truncate
    # （非原子版会清空 path；原子版清空的是 tmp），再抛 OSError 模拟崩溃。
    orig_write_text = pathlib.Path.write_text

    def boom(self, data, *args, **kwargs):
        # open('w') 触发 O_TRUNC —— 真实写入的第一步
        with open(self, "w", encoding="utf-8"):
            pass  # 立即关闭，文件已被清空（模拟写到一半前的 truncate）
        raise OSError("simulated crash mid-write")

    monkeypatch.setattr(pathlib.Path, "write_text", boom)

    # 调 write_file —— 写新内容时 write_text 抛 OSError
    with pytest.raises(OSError, match="simulated crash"):
        await md.write_file(p, "new content that should not appear")

    # ★ 核心断言：原文件未被破坏，仍是旧内容
    # 原子版：写到 .tmp 时抛，path 未被触碰 → 保持旧内容
    # 非原子版：open('w') 已 O_TRUNC 清空 path → path 为空或残缺
    assert p.read_text(encoding="utf-8") == old_content, (
        "original file corrupted by crashed write — atomic replace failed"
    )


@pytest.mark.asyncio
async def test_write_file_normal_no_tmp_leftover(tmp_path):
    """正常写入后 .md 内容正确且无 .tmp 残留。"""
    import pathlib
    md = MarkdownStore(root_dir=str(tmp_path))
    p = pathlib.Path(tmp_path) / "memories" / "u1" / "s1" / "note.md"
    content = "---\nid: m1\ntype: note\n---\nhello\n"
    await md.write_file(p, content)
    assert p.read_text(encoding="utf-8") == content
    tmp = p.with_suffix(p.suffix + ".tmp")
    assert not tmp.exists()


# ===========================================================================
# FileMemoryIndex: end-to-end + delete fallback
# ===========================================================================


@pytest.fixture
def index(tmp_path):
    return FileMemoryIndex(root_dir=str(tmp_path), embedding_model=FakeEmbedding())


def test_v2_construct_creates_dirs(tmp_path):
    root = tmp_path / "v2root"
    FileMemoryIndex(root_dir=str(root))
    assert root.exists()
    assert (root / "memories").exists()
    assert (root / "memory.db").exists()


@pytest.mark.asyncio
async def test_v2_add_grouped_by_type_into_one_file(index):
    await index.add_memories("u1", "s1", [
        MemoryDoc(id="m1", text="alpha", type="note"),
        MemoryDoc(id="m2", text="beta", type="note"),
        MemoryDoc(id="m3", text="gamma", type="profile"),
    ])
    # Two type files, note.md holding m1+m2
    import pathlib
    note = pathlib.Path(index.root_dir) / "memories" / "u1" / "s1" / "note.md"
    profile = pathlib.Path(index.root_dir) / "memories" / "u1" / "s1" / "profile.md"
    assert note.exists() and profile.exists()
    note_ids = {b.mem_id for b in parse_blocks(note.read_text(encoding="utf-8"))}
    assert note_ids == {"m1", "m2"}


@pytest.mark.asyncio
async def test_v2_get_by_id_after_add(index):
    await index.add_memories("u1", "s1", [MemoryDoc(id="m1", text="hello", type="note")])
    got = await index.get_by_id("u1", "s1", "m1")
    assert got is not None and got.text == "hello"
    assert await index.get_by_id("u1", "s1", "missing") is None


@pytest.mark.asyncio
async def test_v2_search_returns_docs_with_scores(index):
    await index.add_memories("u1", "s1", [
        MemoryDoc(id="a", text="hello world", type="note"),
        MemoryDoc(id="b", text="hello there", type="note"),
        MemoryDoc(id="c", text="different", type="profile"),
    ])
    hits = await index.search("u1", "s1", "hello world", top_k=2)
    assert len(hits) == 2
    assert "a" in [h[0].id for h in hits]
    scores = [h[1] for h in hits]
    assert scores == sorted(scores, reverse=True)


@pytest.mark.asyncio
async def test_v2_search_mem_types_filter(index):
    await index.add_memories("u1", "s1", [
        MemoryDoc(id="a", text="hello world", type="note"),
        MemoryDoc(id="b", text="hello world again", type="profile"),
    ])
    hits = await index.search("u1", "s1", "hello world", mem_types=["note"], top_k=5)
    assert {h[0].id for h in hits} == {"a"}


@pytest.mark.asyncio
async def test_v2_delete_memories_removes_block_from_file(index):
    await index.add_memories("u1", "s1", [
        MemoryDoc(id="d1", text="hello", type="note"),
        MemoryDoc(id="d2", text="world", type="note"),
    ])
    await index.delete_memories("u1", "s1", ["d1"])
    assert await index.get_by_id("u1", "s1", "d1") is None
    assert (await index.get_by_id("u1", "s1", "d2")) is not None
    # d1 must not be searchable anymore
    hits = await index.search("u1", "s1", "hello", top_k=5)
    assert all(h[0].id != "d1" for h in hits)


@pytest.mark.asyncio
async def test_v2_delete_memories_falls_back_for_unindexed_id(index, tmp_path):
    # Add via the API so the file exists, then corrupt the DB's view by
    # manually deleting the chunk row — simulating "DB doesn't know this id".

    await index.add_memories("u1", "s1", [
        MemoryDoc(id="orphan", text="i am unindexed", type="note"),
        MemoryDoc(id="kept", text="i stay", type="note"),
    ])
    index.vec_index.delete_chunk_by_mem_id("orphan")
    assert index.vec_index.get_path_for_mem_id_scoped("orphan", "u1", "s1") is None  # DB oblivious

    # Now delete it — the fallback scan must find it in the file and remove it.
    await index.delete_memories("u1", "s1", ["orphan"])
    import pathlib
    note = pathlib.Path(index.root_dir) / "memories" / "u1" / "s1" / "note.md"
    ids = {b.mem_id for b in parse_blocks(note.read_text(encoding="utf-8"))}
    assert "orphan" not in ids
    assert "kept" in ids


@pytest.mark.asyncio
async def test_v2_delete_last_block_removes_file(index):
    await index.add_memories("u1", "s1", [MemoryDoc(id="solo", text="only one", type="note")])
    import pathlib
    note = pathlib.Path(index.root_dir) / "memories" / "u1" / "s1" / "note.md"
    assert note.exists()
    await index.delete_memories("u1", "s1", ["solo"])
    assert not note.exists()
    assert await index.get_by_id("u1", "s1", "solo") is None


@pytest.mark.asyncio
async def test_v2_external_edit_picked_up_on_search(index, tmp_path):
    import pathlib
    note = pathlib.Path(index.root_dir) / "memories" / "u1" / "s1" / "note.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    content = blocks_to_markdown([Block(mem_id="ext", type="note", text="external memory")])
    note.write_text(content, encoding="utf-8")

    hits = await index.search("u1", "s1", "external memory", top_k=5)
    assert any(h[0].id == "ext" for h in hits)


@pytest.mark.asyncio
async def test_v2_persistence_across_instances(tmp_path):
    idx1 = FileMemoryIndex(root_dir=str(tmp_path), embedding_model=FakeEmbedding())
    await idx1.add_memories("u1", "s1", [MemoryDoc(id="p1", text="persisted", type="note")])
    idx2 = FileMemoryIndex(root_dir=str(tmp_path), embedding_model=FakeEmbedding())
    got = await idx2.get_by_id("u1", "s1", "p1")
    assert got is not None and got.text == "persisted"
    hits = await idx2.search("u1", "s1", "persisted", top_k=5)
    assert any(h[0].id == "p1" for h in hits)


@pytest.mark.asyncio
async def test_v2_update_memories(index):
    await index.add_memories("u1", "s1", [MemoryDoc(id="u", text="old", type="note")])
    await index.update_memories("u1", "s1", [MemoryDoc(id="u", text="new text", type="note")])
    got = await index.get_by_id("u1", "s1", "u")
    assert got.text == "new text"
    hits = await index.search("u1", "s1", "new text", top_k=5)
    assert any(h[0].id == "u" for h in hits)


@pytest.mark.asyncio
async def test_v2_delete_by_user_scope_isolation(index):
    async def _seed():
        await index.add_memories("u1", "s1", [MemoryDoc(id="a", text="a", type="n")])
        await index.add_memories("u1", "s2", [MemoryDoc(id="b", text="b", type="n")])
        await index.add_memories("u2", "s1", [MemoryDoc(id="c", text="c", type="n")])
    await _seed()
    await index.delete_by_user_and_scope("u1", "s1")
    assert await index.get_by_id("u1", "s1", "a") is None
    assert (await index.get_by_id("u1", "s2", "b")) is not None
    assert (await index.get_by_id("u2", "s1", "c")) is not None


@pytest.mark.asyncio
async def test_v2_list_memories_and_scopes(index):
    await index.add_memories("u1", "s1", [
        MemoryDoc(id=f"n{i}", text=f"note {i}", type="note") for i in range(3)
    ])
    await index.add_memories("u1", "s1", [MemoryDoc(id="p1", text="profile", type="profile")])
    all_list = await index.list_memories("u1", "s1", offset=0, limit=10)
    assert {d.id for d in all_list} == {"n0", "n1", "n2", "p1"}
    notes = await index.list_memories("u1", "s1", offset=0, limit=10, mem_types=["note"])
    assert {d.id for d in notes} == {"n0", "n1", "n2"}

    await index.add_memories("u2", "s1", [MemoryDoc(id="x", text="x", type="n")])
    scopes = set(await index.list_user_scopes())
    assert ("u1", "s1") in scopes and ("u2", "s1") in scopes


@pytest.mark.asyncio
async def test_v2_get_by_id_rejects_cross_tenant_read(index):
    """跨租户 get_by_id 必须返回 None（复现并锁定 FMI-DEF-002）。

    对齐 ``test_file_memory_index_007`` 的 Step3：两个租户各写一条记忆，
    userA 用 b1 的 id 去 get_by_id，不应读到 userB 的内容。007 用真实
    embedding（@real_llm）跑，本用例用 FakeEmbedding 复现同一隔离缺陷，
    无需外部服务即可回归。
    """
    user_a, scope_a = "uA", "sA"
    user_b, scope_b = "uB", "sB"

    # 两个租户各自写一条语义相近的记忆（都含「打篮球」）
    await index.add_memories(user_a, scope_a, [
        MemoryDoc(id="a1", text="用户A最喜欢的一项运动是打篮球", type="user_profile"),
    ])
    await index.add_memories(user_b, scope_b, [
        MemoryDoc(id="b1", text="用户B最喜欢的一项运动是打篮球", type="user_profile"),
    ])

    # 跨租户 get_by_id b1 —— 应返回 None（不可越权读 userB 的内容）
    cross = await index.get_by_id(user_a, scope_a, "b1")
    assert cross is None, (
        f"跨租户 get_by_id 越权读到了 userB 的 b1: {cross!r}（FMI-DEF-002）"
    )
    # 反向同理：userB 也不应读到 a1
    cross_rev = await index.get_by_id(user_b, scope_b, "a1")
    assert cross_rev is None, (
        f"跨租户 get_by_id 越权读到了 userA 的 a1: {cross_rev!r}（FMI-DEF-002）"
    )
    # 同租户仍可正常读（隔离不应误伤本租户读取）
    own = await index.get_by_id(user_a, scope_a, "a1")
    assert own is not None and own.id == "a1" and own.text == "用户A最喜欢的一项运动是打篮球"

    # 额外强化：跨租户共用同一 mem_id 的极端场景。
    # 两个租户都写 id="shared"，各自内容不同。userA 读 "shared" 应只拿到
    # 自己的版本（text 标识来源），绝不能拿到 userB 的版本。
    await index.add_memories(user_a, scope_a, [
        MemoryDoc(id="shared", text="来自租户A的私有内容", type="user_profile"),
    ])
    await index.add_memories(user_b, scope_b, [
        MemoryDoc(id="shared", text="来自租户B的私有内容", type="user_profile"),
    ])
    got_a = await index.get_by_id(user_a, scope_a, "shared")
    got_b = await index.get_by_id(user_b, scope_b, "shared")
    assert got_a is not None and got_a.text == "来自租户A的私有内容", (
        f"userA 读 own 'shared' 失败或读到了他人内容: {got_a!r}"
    )
    assert got_b is not None and got_b.text == "来自租户B的私有内容", (
        f"userB 读 own 'shared' 失败或读到了他人内容: {got_b!r}"
    )

    # 越权 delete：userA 用 b1 的 id 删自己 scope——绝不能删掉 userB 的 b1。
    # delete_memories 同样经 get_path_for_mem_id_scoped 定位，跨租户 id 解析
    # 不到 path → unresolved → 仅扫本 scope 文件，userB 的 b1 必须安然无恙。
    await index.delete_memories(user_a, scope_a, ["b1"])
    survivor_b = await index.get_by_id(user_b, scope_b, "b1")
    assert survivor_b is not None and survivor_b.id == "b1", (
        f"跨租户 delete 越权删掉了 userB 的 b1: {survivor_b!r}（FMI-DEF-002 delete 变体）"
    )
    # userA 自己也没凭空多出/少东西（b1 本就不属于 userA）
    assert await index.get_by_id(user_a, scope_a, "b1") is None


# ===========================================================================
# VectorIndex: _dims 恢复 —— 重启不清空向量索引
# ===========================================================================


@pytest.mark.asyncio
async def test_dims_recovered_on_restart_no_vector_loss(tmp_path):
    """重启后 _dims 从磁盘恢复，首次写入不再 DROP chunks_vec。

    回归检视意见：_dims 是内存变量，init 置 None 且启动时不读回。重启后第一次
    upsert 走 _ensure_vec_table 的 ``_dims is None`` 分支 → DROP TABLE chunks_vec
    → 存量向量全清，且增量 sync 不重嵌正文未变的旧记忆，向量永久丢失、静默无报错。

    修复后 _recover_dims_if_exists 从 sqlite_master 建表 SQL 反查维度，重启后
    _dims 有值，不再 DROP。本测试用两个 VectorIndex 实例（同 db_path）模拟重启。
    """
  
    db_path = str(tmp_path / "memory.db")
    tenant = TenantScope("u1", "s1")

    # 第一次启动：写 3 条 note
    vi1 = VectorIndex(db_path=db_path, embedding_model=FakeEmbedding())
    for i in range(3):
        await vi1.upsert(MemoryDoc(id=f"m{i}", text=f"memory {i}", type="note"), tenant)
    vec_count_before = vi1.conn.execute("SELECT COUNT(*) FROM chunks_vec").fetchone()[0]
    assert vec_count_before == 3
    assert vi1.dims is not None  # 首次写入后 dims 已设
    vi1.close()

    # 模拟重启：新实例指向同一 db_path
    vi2 = VectorIndex(db_path=db_path, embedding_model=FakeEmbedding())
    # ★ 关键断言：重启后 dims 必须从磁盘恢复，不再是 None
    assert vi2.dims is not None, "dims not recovered from disk — first upsert will DROP!"
    assert vi2.dims == 8  # 与 FakeEmbedding 默认维度一致

    # 重启后写第 4 条 —— 修复前会 DROP 清空（3→1），修复后保留存量（3→4）
    await vi2.upsert(MemoryDoc(id="m_new", text="memory after restart", type="note"), tenant)
    vec_count_after = vi2.conn.execute("SELECT COUNT(*) FROM chunks_vec").fetchone()[0]
    assert vec_count_after == 4, (
        f"vector index cleared on restart! expected 4, got {vec_count_after} "
        f"(DROP-on-restart bug:存量向量被清空)"
    )

    # 存量向量仍可被 search 召回（验证不只是 count 对，向量真在用）
    qv = await vi2.embedding_model.embed_query("memory 0")
    hits = await vi2.search(
        tenant, qv, top_k=5,
        constraints=SearchConstraints(query_text="memory 0"),
    )
    hit_ids = {h[0] for h in hits}
    assert "m0" in hit_ids, "pre-restart memory m0 not recalled — its vector was lost"
    vi2.close()


@pytest.mark.asyncio
async def test_recovery_failure_does_not_drop_chunks_vec(tmp_path, monkeypatch):
    """_recover_dims_if_exists 恢复失败时不应 DROP 存量向量。

    回归检视意见：_recover_dims_if_exists 恢复成功时 _dims 有值不再 DROP（主场景
    已修），但正则不匹配 / sqlite_master 异常会让 _dims 保持 None；原 _ensure_vec_table
    把"不知道维度"当"该清表"→ DROP → 存量向量全清，又退回原 P0。

    修复：删掉 _dims is None 的 DROP 分支，由下方 CREATE VIRTUAL TABLE IF NOT EXISTS
    保证"表不存在则建、表存在则跳过"。恢复失败时 IF NOT EXISTS 跳过旧表，旧数据保全。

    用 monkeypatch 让 _recover_dims_if_exists 变为 no-op（模拟恢复失败），验证重启
    后写入不清空存量向量。
    """

    db_path = str(tmp_path / "memory.db")
    tenant = TenantScope("u1", "s1")

    # 第一次启动：写 3 条
    vi1 = VectorIndex(db_path=db_path, embedding_model=FakeEmbedding())
    for i in range(3):
        await vi1.upsert(MemoryDoc(id=f"m{i}", text=f"memory {i}", type="note"), tenant)
    assert vi1.conn.execute("SELECT COUNT(*) FROM chunks_vec").fetchone()[0] == 3
    vi1.close()

    # 模拟重启：让 _recover_dims_if_exists 变成 no-op，_dims 保持 None
    vi2 = VectorIndex(db_path=db_path, embedding_model=FakeEmbedding())
    # monkeypatch 恢复方法为空操作
    monkeypatch.setattr(vi2, "_recover_dims_if_exists", lambda: None)
    # 强制 _dims = None（构造函数已设，这里再确认）
    vi2.dims = None
    assert vi2.dims is None, "dims should be None after recovery failure"

    # 重启后写第 4 条 —— 修复前：_dims is None → DROP 清空 (3→1)
    # 修复后：IF NOT EXISTS 跳过 → 存量保留 (3→4)
    await vi2.upsert(MemoryDoc(id="m_new", text="memory after restart", type="note"), tenant)
    cnt = vi2.conn.execute("SELECT COUNT(*) FROM chunks_vec").fetchone()[0]
    assert cnt == 4, (
        f"recovery failure DROPped chunks_vec! expected 4, got {cnt} "
        f"(IF NOT EXISTS should skip existing table when dims unrecovered)"
    )

    # 存量向量仍可被 search 召回
    qv = await vi2.embedding_model.embed_query("memory 0")
    hits = await vi2.search(
        tenant, qv, top_k=5,
        constraints=SearchConstraints(query_text="memory 0"),
    )
    hit_ids = {h[0] for h in hits}
    assert "m0" in hit_ids, "pre-restart memory m0 not recalled — its vector was lost"
    vi2.close()


# ===========================================================================
# VectorIndex: bm25_rank_to_score 方向 & 融合排序
# ===========================================================================


def test_bm25_rank_to_score_monotonic_and_range():
    """FTS5 rank 越负（越相关）→ 分数越高（单调递增），且值域 (0,1)。

    回归检视意见：原负数分支 ``1/(1-rank)`` 随 |rank| 增大而减小，方向反了——
    关键词命中越强融合分越低、反而排到后面。修复后 ``m/(1+m)`` 单调递增。
    """

    # 单调性：越负越相关 → 分数越高
    s_weak = VectorIndex.bm25_rank_to_score(-0.1)
    s_mid = VectorIndex.bm25_rank_to_score(-1.0)
    s_strong = VectorIndex.bm25_rank_to_score(-10.0)
    assert s_weak < s_mid < s_strong, (
        f"score not monotonic in relevance: weak={s_weak} mid={s_mid} strong={s_strong}"
    )
    # 值域 (0,1)
    for rank in (-0.01, -0.5, -1.0, -10.0, -1000.0):
        s = VectorIndex.bm25_rank_to_score(rank)
        assert 0.0 < s < 1.0, f"score {s} out of (0,1) for rank={rank}"
    # 极强命中趋近 1，弱命中趋近 0
    assert VectorIndex.bm25_rank_to_score(-1000.0) > 0.999
    assert VectorIndex.bm25_rank_to_score(-0.01) < 0.01
    # 正数/零 rank（FTS5 异常情况）不应抢分
    assert VectorIndex.bm25_rank_to_score(0.0) == 0.0
    assert VectorIndex.bm25_rank_to_score(5.0) == 0.0


@pytest.mark.asyncio
async def test_fts_strong_hit_ranks_above_weak_hit(index):
    """FTS 命中强度应正向影响融合排序：强命中排前。

    端到端验证修复效果。构造两条记忆，向量分接近（FakeEmbedding 下文本
    字符分布相似 → cosine 相近），但一条含查询关键词（FTS 强命中）、一条
    不含。修复前 bm25 方向反了，强命中融合分更低、可能排到弱命中后面；
    修复后强命中应排第一。

    用纯 ASCII token（applepie）避免 jieba 中文分词把 query 和存储拆成不同
    token 导致 FTS 命中过弱（rank 趋近 0、融合分被向量主导）。
    """
    # a/b 文本结构相似（向量分接近），但 a 含 applepie、b 含 bananapie
    await index.add_memories("u1", "s1", [
        MemoryDoc(id="a", text="user likes applepie very much indeed", type="note"),
        MemoryDoc(id="b", text="user likes bananapie very much indeed", type="note"),
    ])
    # 搜 applepie —— a 强命中 FTS，b 无 FTS 命中；向量分两者接近
    hits = await index.search("u1", "s1", "applepie", top_k=2)
    assert len(hits) == 2
    assert hits[0][0].id == "a", (
        f"strong FTS hit 'a' should rank first, got {[h[0].id for h in hits]}"
    )


# ===========================================================================
# FileMemoryIndex.search: 无 embedding 降级 FTS-only
# ===========================================================================


@pytest.mark.asyncio
async def test_search_degrades_to_fts_only_when_no_embedding(index):
    """embedding 缺失时 search 降级为 FTS-only 关键词检索，不返回空。

    回归检视意见：原 search 入口在 self._embedding_model 缺失时 log error 后
    直接 return []，把已建好的 FTS5（jieba+BM25）挡在门外。embedding API 临时
    故障 + 存量数据场景下搜索全空，本可 FTS 兜底。修复后降级走 FTS-only。

    构造：用 FakeEmbedding 写入数据（建好 FTS 索引），再 set_embedding_model(None)
    移除 embedding（模拟 API 故障），search 关键词应仍能经 FTS 召回。
    """
    # 用 embedding 写入，建好 vec + fts 索引
    await index.add_memories("u1", "s1", [
        MemoryDoc(id="a", text="user likes applepie very much", type="note"),
        MemoryDoc(id="b", text="user enjoys bananapie occasionally", type="note"),
    ])

    # 移除 embedding 模拟临时故障
    index.set_embedding_model(None)

    # search 应降级 FTS-only，召回含关键词 applepie 的 a
    hits = await index.search("u1", "s1", "applepie", top_k=2)
    hit_ids = {h[0].id for h in hits}
    assert "a" in hit_ids, (
        f"FTS-only degradation failed — 'a' not recalled without embedding: {hit_ids}"
    )
    # b 不含 applepie，不应被召回（FTS 精确匹配）
    assert "b" not in hit_ids, f"FTS-only leaked non-matching 'b': {hit_ids}"


@pytest.mark.asyncio
async def test_search_with_embedding_still_uses_hybrid(index):
    """有 embedding 时 search 仍走向量+FTS 混合，降级不影响正常路径。"""
    await index.add_memories("u1", "s1", [
        MemoryDoc(id="a", text="user likes applepie very much", type="note"),
        MemoryDoc(id="b", text="user enjoys bananapie occasionally", type="note"),
    ])
    # embedding 存在，正常混合检索
    assert index.embedding_model is not None
    hits = await index.search("u1", "s1", "applepie", top_k=2)
    assert len(hits) >= 1
    assert "a" in {h[0].id for h in hits}


# ===========================================================================
# VectorIndex: _delete_vec_fts_rows 异常保护 —— vec 表损坏不中断 delete
# ===========================================================================


def test_delete_by_path_tolerates_corrupt_chunks_vec(vi, tmp_path):
    """chunks_vec 表损坏/缺失时，delete_by_path 不应抛异常中断清理。

    回归检视意见：_delete_vec_fts_rows 的 chunks_vec executemany 无 try/except
    （chunks_fts 有），若 chunks_vec 表不存在或损坏会抛 OperationalError，
    导致 chunks 表已删但 files 表清理 + commit 被跳过（半成功状态）。修复后
    与 chunks_fts 对称保护，记 warning 不抛，chunks/files 正常清理完成。
    """
    import sqlite3

    async def _run():
        content, _ = _write_md(tmp_path, "u1", "s1", "note",
                               _make_blocks(("m1", "alpha"), ("m2", "beta")))
        rel = "memories/u1/s1/note.md"
        await vi.sync_file(rel, TenantScope("u1", "s1"), "note", content, parse_blocks(content))
        assert vi.get_text("m1") is not None
        assert vi.get_file_hash_from_db(rel) is not None

        # 模拟 chunks_vec 表损坏（外部删除 / DB schema 不一致）
        vi.conn.execute("DROP TABLE IF EXISTS chunks_vec")
        # _vec_available 仍为 True（表存在时加载成功），但表已被 DROP ——
        # 触发 DELETE FROM chunks_vec 抛 OperationalError 的真实场景。

        # 修复前：此调用抛 sqlite3.OperationalError；修复后：记 warning，不抛
        vi.delete_by_path(rel)

        # chunks 主表和 files 表应正常清理完成（不被 vec 异常中断）
        assert vi.get_text("m1") is None
        assert vi.get_text("m2") is None
        assert vi.get_file_hash_from_db(rel) is None

    asyncio.run(_run())
    vi.close()


def test_delete_by_user_and_scope_tolerates_corrupt_chunks_vec(vi, tmp_path):
    """delete_by_user_and_scope 同样应在 chunks_vec 损坏时不中断。

    覆盖另一个 delete 路径：批量删除走 _delete_vec_fts_rows 的 rowids 分支，
    vec 表损坏时 chunks 主表 + files 表应照常清理。
    """
    async def _run():
        content, _ = _write_md(tmp_path, "u1", "s1", "note",
                               _make_blocks(("m1", "alpha"), ("m2", "beta")))
        rel = "memories/u1/s1/note.md"
        await vi.sync_file(rel, TenantScope("u1", "s1"), "note", content, parse_blocks(content))
        assert vi.get_text("m1") is not None

        vi.conn.execute("DROP TABLE IF EXISTS chunks_vec")

        # 批量 delete 路径，vec 损坏不应抛
        vi.delete_by_user_and_scope("u1", "s1")
        assert vi.get_text("m1") is None
        assert vi.get_text("m2") is None

    asyncio.run(_run())
    vi.close()


@pytest.mark.asyncio
async def test_search_ignores_orphan_fts_rows_after_vec_corruption(index, tmp_path):
    """vec/fts 残留孤立行不应导致已删记忆被召回。

    delete 时 chunks_vec 删除失败产生孤立 fts 行（rowid 在 chunks 表已不存在）。
    search 时 _search_fts 按 rowid 回查 chunks 表的 rowid_to_mem 映射，查不到
    的孤立 rowid 被跳过 —— 已删记忆不会被错误召回，语义正确。
    """
    import pathlib

    await index.add_memories("u1", "s1", [
        MemoryDoc(id="m1", text="orphan test memory content", type="note"),
    ])
    # 确认可搜到
    hits = await index.search("u1", "s1", "orphan test", top_k=5)
    assert any(h[0].id == "m1" for h in hits)

    # 模拟 delete 时 vec 清理失败留下孤立 fts 行：直接清 chunks 表但保留 fts
    vi = index.vec_index
    vi.conn.execute("DELETE FROM chunks WHERE mem_id=?", ("m1",))
    vi.conn.commit()
    # chunks_fts 仍保留 m1 的 rowid（孤立行）

    # search 不应召回已删的 m1（fts 孤立行回查 chunks 查不到 → 跳过）
    hits_after = await index.search("u1", "s1", "orphan test", top_k=5)
    assert not any(h[0].id == "m1" for h in hits_after), (
        "orphan fts row caused deleted memory m1 to be recalled"
    )


# ===========================================================================
# Watchdog: graceful degradation
# ===========================================================================


def test_watcher_available_flag_is_bool(tmp_path):
    w = MemoryFileWatcher(tmp_path, lambda *_: None)
    assert isinstance(w.available, bool)
    # start() must be a no-op (not raise) when watchdog is unavailable
    if not w.available:
        # No running loop here; start() should return without scheduling.
        try:
            w.start()
        except RuntimeError:
            # No event loop — acceptable, the no-op path is what we care about
            pass


def test_watcher_stop_is_safe_when_not_started(tmp_path):
    w = MemoryFileWatcher(tmp_path, lambda *_: None)
    # stop() before start() must not raise
    w.stop()


@pytest.mark.asyncio
async def test_watcher_enqueue_via_threadsafe(tmp_path):
    w = MemoryFileWatcher(tmp_path, lambda *_: None)
    w.simulate_file_event(str(tmp_path / "memories" / "u1" / "s1" / "note.md"), "modified")
    # Let the loop process the call_soon_threadsafe callback.
    await asyncio.sleep(0)
    assert any("note.md" in p for p in w.get_pending_paths())
    w.stop()


def test_start_watcher_idempotent_no_observer_leak(index, mocker):
    """重复调 start_watcher 不应创建第二个 Observer（幂等）。

    回归检视意见接线：register_store 内部启动 watcher 后，file_memory_server
    的 startup 也会调 start_watcher，重复调用若不幂等会创建第二个 Observer
    并覆盖第一个的引用，泄漏后台线程。start_watcher 检查 _watcher.running
    已运行则 no-op。
    """
    from unittest.mock import PropertyMock
    # 模拟 watcher 已在运行（测试环境 watchdog 未安装，无法真实启动）
    mocker.patch.object(type(index.watcher), "running", new_callable=PropertyMock, return_value=True)
    spy_start = mocker.patch.object(index.watcher, "start")

    index.start_watcher()  # running=True → 应 no-op，不调 _watcher.start
    assert not spy_start.called, "start_watcher called _watcher.start despite running=True (not idempotent)"


def test_start_watcher_starts_when_not_running(index, mocker):
    """watcher 未运行时 start_watcher 正常调 _watcher.start。"""
    from unittest.mock import PropertyMock
    mocker.patch.object(type(index.watcher), "running", new_callable=PropertyMock, return_value=False)
    spy_start = mocker.patch.object(index.watcher, "start")

    index.start_watcher()
    assert spy_start.called, "start_watcher did not call _watcher.start when not running"


@pytest.mark.asyncio
async def test_debounced_sync_clears_watch_timer_before_processing(tmp_path):
    """_debounced_sync 处理循环前置 _watch_timer=None，防新事件自取消。

    回归检视意见：_debounced_sync 原在处理循环期间 _watch_timer 仍指向自己，
    新事件到达 _reset_timer 见 timer 未 done → cancel 当前任务，批次中未处理
    文件永久丢失。修复后 sleep 结束即置 _watch_timer=None，新事件另起一轮
    debounce 而非取消当前批次。
    """
    events_done = []

    async def _handler(path, _event_type):
        events_done.append(path)
        # 模拟处理 A 时外部事件 D 到达
        if path == "A.md":
            w.enqueue_event("D.md", "modified")

    w = MemoryFileWatcher(tmp_path, _handler)
    w.debounce_s = 0.01
    w.loop = asyncio.get_running_loop()
    w.initialized = True

    # 入队 A + B
    w.enqueue_event("A.md", "modified")
    w.enqueue_event("B.md", "modified")

    # ★ 断言：sleep 结束后 _watch_timer 已被 _debounced_sync 置 None
    # 等待 debounce 触发并完成（A 处理时注入 D，D 另起一轮）
    await asyncio.sleep(0.2)
    # 原批次 A、B 都被处理
    assert "A.md" in events_done, "A should be processed"
    assert "B.md" in events_done, "B should not be cancelled mid-batch"
    # D 被新 timer 处理（新起的一轮 debounce）
    assert "D.md" in events_done, "D should start new debounce, not cancel current batch"
    w.stop()


@pytest.mark.asyncio
async def test_debounced_sync_processing_does_not_self_cancel(tmp_path):
    """处理期间 _reset_timer 不 cancel 当前批次——多 item 批次不被中途取消。

    回归：入队 A+B+C 同批次，等 debounce 开始处理。在处理 A 的 _on_file_changed
    期间入队 D（新事件）。无修复时 A 处理中的 _enqueue_event(D) → _reset_timer
    → cancel 当前 task → B/C 的 await 处抛 CancelledError → B/C 丢失。
    修复后 _watch_timer=None，新事件另起一轮，B/C 不受影响。
    """
    processed = []
    barrier = asyncio.Event()

    async def _handler(path, _event_type):
        processed.append(path)
        if path == "A.md":
            # 在处理 A 期间，外部事件 D 到达
            w.enqueue_event("D.md", "modified")
        if path == "D.md":
            barrier.set()

    w = MemoryFileWatcher(tmp_path, _handler)
    w.debounce_s = 0.01
    w.loop = asyncio.get_running_loop()
    w.initialized = True

    # 入队 A、B、C（同一批次）
    w.enqueue_event("A.md", "modified")
    w.enqueue_event("B.md", "modified")
    w.enqueue_event("C.md", "modified")
    # 等所有批次完成（A→触发 D，D 的新批次完成时 set barrier）
    await asyncio.wait_for(barrier.wait(), timeout=2.0)
    await asyncio.sleep(0.05)  # 确保 D 批次全部写完
    # ★ A、B、C 必须全部被处理（未被 cancel 跳过）
    assert "A.md" in processed, "A should be processed"
    assert "B.md" in processed, "B should not be cancelled mid-batch"
    assert "C.md" in processed, "C should not be cancelled mid-batch"
    # D 被新批次处理
    assert "D.md" in processed, "D should be processed in new batch"
    w.stop()


# ===========================================================================
# Concurrency: per-file lock prevents silent memory loss
# ===========================================================================


@pytest.mark.asyncio
async def test_concurrent_add_memories_no_silent_loss(index, monkeypatch):
    """并发写同一 {Type}.md 不应静默丢记忆。

    回归检视意见：两个并发 add_memories 写同一文件时，各自读到旧快照、
    后写的会覆盖先写的新 block，且 sync 会把丢失的 block 从索引删掉——
    文件和索引双双丢失，全程无报错。修复后每文件锁串行化 read→write→sync，
    全部 N 条记忆必须完整落盘且可被 search 召回。

    为了稳定复现 read-modify-write 交错（FakeEmbedding + 本地 IO 太快，
    自然 await 点不足以触发竞态），用 monkeypatch 给 md_store.read_blocks /
    write_blocks 各注入一个 ``await asyncio.sleep(0)`` yield 点，强制事件循环
    在读后、写前切换到别的协程。无锁时此 patch 必触发丢记忆；有锁时锁串行化，
    patch 不影响正确性。
    """
    md_store = index.md_store

    async def _slow_read_blocks(path):
        await asyncio.sleep(0)  # yield → 让并发请求都读到同一旧快照
        return await _orig_read_blocks(path)

    async def _slow_write_blocks(path, blocks):
        await asyncio.sleep(0)  # yield → 写前让别的请求基于旧快照算完 merged
        return await _orig_write_blocks(path, blocks)

    _orig_read_blocks = md_store.read_blocks
    _orig_write_blocks = md_store.write_blocks
    monkeypatch.setattr(md_store, "read_blocks", _slow_read_blocks)
    monkeypatch.setattr(md_store, "write_blocks", _slow_write_blocks)

    n = 12  # 并发请求数；每个加 1 条不同 id 的 note 记忆
    docs_per_call = [
        [MemoryDoc(id=f"c{i}", text=f"concurrent memory number {i}", type="note")]
        for i in range(n)
    ]

    await asyncio.gather(*[
        index.add_memories("u1", "s1", docs) for docs in docs_per_call
    ])

    # ① 文件里应有全部 N 条（按 id 集合校验，不依赖顺序）
    import pathlib
    note = pathlib.Path(index.root_dir) / "memories" / "u1" / "s1" / "note.md"
    assert note.exists()
    ids_on_disk = {b.mem_id for b in parse_blocks(note.read_text(encoding="utf-8"))}
    missing = {f"c{i}" for i in range(n)} - ids_on_disk
    assert not missing, f"memory blocks silently lost on disk: {missing}"

    # ② 索引里也应能按 id 取回全部 N 条（验证 sync 没把丢失的删掉，也没漏索引）
    for i in range(n):
        got = await index.get_by_id("u1", "s1", f"c{i}")
        assert got is not None, f"c{i} lost from index"
        assert got.text == f"concurrent memory number {i}"

    # ③ search 能召回（验证向量+FTS 索引完整）
    hits = await index.search("u1", "s1", "concurrent memory", top_k=n)
    hit_ids = {h[0].id for h in hits}
    assert {f"c{i}" for i in range(n)} <= hit_ids, "some memories not searchable"


@pytest.mark.asyncio
async def test_concurrent_add_delete_no_interleave_loss(index, monkeypatch):
    """并发 add + delete 同一文件不应互相丢失对方的结果。

    add 写 c_new，delete 删 c_old（事先存在的）。加锁前两者交错可能出现：
    add 读到含 c_old 的快照、delete 也读到同快照 → delete 先写回去掉 c_old
    的版本 → add 基于旧快照写回含 c_old+c_new 的版本 → c_old 被复活，
    且 c_new 可能因快照时序丢失。加锁后结果确定：c_old 必被删，c_new 必在。
    """
    md_store = index.md_store
    _orig_read = md_store.read_blocks
    _orig_write = md_store.write_blocks

    async def _slow_read(path):
        await asyncio.sleep(0)
        return await _orig_read(path)

    async def _slow_write(path, blocks):
        await asyncio.sleep(0)
        return await _orig_write(path, blocks)

    monkeypatch.setattr(md_store, "read_blocks", _slow_read)
    monkeypatch.setattr(md_store, "write_blocks", _slow_write)

    # 预置 c_old
    await index.add_memories("u1", "s1", [MemoryDoc(id="c_old", text="old memory", type="note")])

    # 并发：一边删 c_old，一边加 c_new（同 user/scope/type 文件）
    await asyncio.gather(
        index.delete_memories("u1", "s1", ["c_old"]),
        index.add_memories("u1", "s1", [MemoryDoc(id="c_new", text="new memory", type="note")]),
    )

    assert await index.get_by_id("u1", "s1", "c_old") is None, "c_old should be deleted"
    got_new = await index.get_by_id("u1", "s1", "c_new")
    assert got_new is not None and got_new.text == "new memory", "c_new must survive"


@pytest.mark.asyncio
async def test_concurrent_update_memories_atomic_no_loss(index, monkeypatch):
    """并发 update 同一 {Type}.md 不应丢写、中间态不外泄。

    回归检视意见：update_memories 原为 delete+add 两次独立加锁，中间无锁空档
    允许并发 update/add 插入与 add 的整文件重写交错、互相覆盖（丢写窗口），
    且 delete 完 add 未开始时记忆暂时消失（中间态外泄，并发 search 漏召）。
    修复后 update 按 rel_path 外层持锁，delete+add 同一 async with 内不释放，原子。

    验证两点：① 两个并发 update 各改不同 id，结果都落盘（不丢写）；② update
    过程中并发 get_by_id 不会因中间态永久漏掉（update 完成后必可读到新内容）。
    用 monkeypatch 在 read_blocks 后 yield 放大竞态窗口。
    """
    md_store = index.md_store
    _orig_read = md_store.read_blocks
    _orig_write = md_store.write_blocks

    async def _slow_read(path):
        await asyncio.sleep(0)
        return await _orig_read(path)

    async def _slow_write(path, blocks):
        await asyncio.sleep(0)
        return await _orig_write(path, blocks)

    monkeypatch.setattr(md_store, "read_blocks", _slow_read)
    monkeypatch.setattr(md_store, "write_blocks", _slow_write)

    # 预置 u1、u2 两条
    await index.add_memories("u1", "s1", [
        MemoryDoc(id="u1", text="old text 1", type="note"),
        MemoryDoc(id="u2", text="old text 2", type="note"),
    ])

    # 并发 update：u1→新文本、u2→新文本（同文件不同 id）
    await asyncio.gather(
        index.update_memories("u1", "s1", [
            MemoryDoc(id="u1", text="new text 1 updated", type="note")]),
        index.update_memories("u1", "s1", [
            MemoryDoc(id="u2", text="new text 2 updated", type="note")]),
    )

    # ① 两个 update 的结果都应保留（不丢写）
    got1 = await index.get_by_id("u1", "s1", "u1")
    got2 = await index.get_by_id("u1", "s1", "u2")
    assert got1 is not None and got1.text == "new text 1 updated", (
        f"u1 update lost: {got1.text if got1 else None}"
    )
    assert got2 is not None and got2.text == "new text 2 updated", (
        f"u2 update lost: {got2.text if got2 else None}"
    )

    # ② search 能召回更新后的内容（最终一致）
    hits = await index.search("u1", "s1", "updated", top_k=5)
    hit_ids = {h[0].id for h in hits}
    assert {"u1", "u2"} <= hit_ids, f"updated memories not searchable: {hit_ids}"


@pytest.mark.asyncio
async def test_update_with_concurrent_add_no_loss(index, monkeypatch):
    """update 与并发 add 同一文件不应互相覆盖丢写。

    检视意见的丢写窗口：update 的 delete→add 空档允许并发 add 插入，update 的
    add 若基于旧快照写回会覆盖并发 add 的新 block。修复后 update 外层持锁，
    add 在锁外等待，两者串行不交错。
    """
    md_store = index.md_store
    _orig_read = md_store.read_blocks
    _orig_write = md_store.write_blocks

    async def _slow_read(path):
        await asyncio.sleep(0)
        return await _orig_read(path)

    async def _slow_write(path, blocks):
        await asyncio.sleep(0)
        return await _orig_write(path, blocks)

    monkeypatch.setattr(md_store, "read_blocks", _slow_read)
    monkeypatch.setattr(md_store, "write_blocks", _slow_write)

    # 预置 u1
    await index.add_memories("u1", "s1", [MemoryDoc(id="u1", text="old", type="note")])

    # 并发：update u1 + add u2（同文件）
    await asyncio.gather(
        index.update_memories("u1", "s1", [
            MemoryDoc(id="u1", text="u1 updated", type="note")]),
        index.add_memories("u1", "s1", [
            MemoryDoc(id="u2", text="u2 new", type="note")]),
    )

    # 两者都应保留：u1 是更新后内容，u2 是新增
    got1 = await index.get_by_id("u1", "s1", "u1")
    got2 = await index.get_by_id("u1", "s1", "u2")
    assert got1 is not None and got1.text == "u1 updated", "u1 update lost"
    assert got2 is not None and got2.text == "u2 new", "concurrent add u2 lost"


# ===========================================================================
# Ebbinghaus forgetting: blacklisted / is_important persistence + filters
# ===========================================================================
#
# These tests pin the regression reported in the test handoff: FileMemoryIndex
# search / list_memories accepted no filters kwarg →上层硬传 filters 时
# TypeError → MEMORY_GET_MEMORY_EXECUTION_ERROR. The fix adds:
#   1) blacklisted / is_important as top-level Block + MemoryDoc fields
#   2) the columns on the chunks table + a direct UPDATE path so
#      update_mem_by_id scalar flips don't get dropped by sync_file
#   3) *, filters: Optional[FilterGroup] = None on search / list_memories,
#      with SQL pushdown for EQ/NE on blacklisted/is_important


def _eq_blacklisted() -> FilterGroup:
    return FilterGroup(
        conditions=[FilterCondition(field="blacklisted",
                                    op=FilterOperator.EQ, value=True)]
    )


def _ne_blacklisted() -> FilterGroup:
    return FilterGroup(
        conditions=[FilterCondition(field="blacklisted",
                                    op=FilterOperator.NE, value=True)]
    )


@pytest.mark.asyncio
async def test_doc_to_block_to_doc_round_trips_blacklisted_and_is_important(index):
    """The .md frontmatter must round-trip the forgetting flags as top-level
    keys — exercised end-to-end via the public add_memories + get_by_id
    surface, without touching ``_doc_to_block`` / ``_block_to_doc`` directly
    (those are protected helpers; this test keeps the access pattern honest
    by mirroring what callers actually do).
    """
    # Build a Block with both flags set and serialise to .md frontmatter.
    block = Block(
        mem_id="m1", type="note", text="hello",
        blacklisted=True, is_important=True,
    )
    md = blocks_to_markdown([block])
    parsed = parse_blocks(md)
    assert parsed[0].blacklisted is True, "blacklisted dropped on .md round-trip"
    assert parsed[0].is_important is True, "is_important dropped on .md round-trip"

    # End-to-end through the public FileMemoryIndex API: the write path
    # (add_memories) and read path (get_by_id) must both preserve the flags
    # without any caller-level protected-member access.
    doc = MemoryDoc(
        id="m1", text="hello", type="note",
        blacklisted=True, is_important=True,
    )
    await index.add_memories("u1", "s1", [doc])
    fetched = await index.get_by_id("u1", "s1", "m1")
    assert fetched is not None
    assert fetched.blacklisted is True, "blacklisted lost across add+get"
    assert fetched.is_important is True, "is_important lost across add+get"


@pytest.mark.asyncio
async def test_search_accepts_filters_kwarg_and_returns_results(index):
    """Reproduces the reported TypeError: default search with filters=None
    on FileMemoryIndex used to crash because the signature lacked the
    ``filters`` kwarg entirely.
    """
    await index.add_memories(
        "u1", "s1",
        [MemoryDoc(id="m1", text="hello world", type="note")],
    )
    # Default search (filters=None) must not raise.
    hits = await index.search("u1", "s1", "hello", top_k=5)
    assert len(hits) == 1
    assert hits[0][0].id == "m1"


@pytest.mark.asyncio
async def test_list_memories_accepts_filters_kwarg_default_excludes_nothing(index):
    """list_memories with filters=None returns all memories — the
    search_manager entrypoint is responsible for injecting the default
    NE(blacklisted, True); FileMemoryIndex itself stays neutral.
    """
    await index.add_memories(
        "u1", "s1",
        [
            MemoryDoc(id="m1", text="alpha", type="note"),
            MemoryDoc(id="m2", text="beta", type="note", blacklisted=True),
        ],
    )
    listed = await index.list_memories("u1", "s1", 0, 100)
    assert {d.id for d in listed} == {"m1", "m2"}


@pytest.mark.asyncio
async def test_list_memories_ne_blacklisted_excludes_forgotten(index):
    """The default-injected NE(blacklisted, True) FilterGroup must hide
    blacklisted memories on the list path.
    """
    await index.add_memories(
        "u1", "s1",
        [
            MemoryDoc(id="m1", text="alpha", type="note"),
            MemoryDoc(id="m2", text="beta", type="note", blacklisted=True),
        ],
    )
    listed = await index.list_memories(
        "u1", "s1", 0, 100, filters=_ne_blacklisted(),
    )
    assert [d.id for d in listed] == ["m1"]


@pytest.mark.asyncio
async def test_list_memories_eq_blacklisted_returns_only_forgotten(index):
    """EQ(blacklisted, True) is the recall path: surface blacklisted
    memories only.
    """
    await index.add_memories(
        "u1", "s1",
        [
            MemoryDoc(id="m1", text="alpha", type="note"),
            MemoryDoc(id="m2", text="beta", type="note", blacklisted=True),
        ],
    )
    listed = await index.list_memories(
        "u1", "s1", 0, 100, filters=_eq_blacklisted(),
    )
    assert [d.id for d in listed] == ["m2"]


@pytest.mark.asyncio
async def test_update_mem_by_id_blacklisted_flip_persists_across_syncs(index):
    """update_mem_by_id(blacklisted=True) must persist to BOTH the .md
    frontmatter AND the chunks table — otherwise the next sync_file
    (which only re-upserts text-changed blocks) would clobber the flag.

    Mirrors what the Ebbinghaus forget pipeline does via
    LongTermMemory._mark_blacklisted.
    """
    await index.add_memories(
        "u1", "s1",
        [MemoryDoc(id="m1", text="hello world", type="note")],
    )
    # Flip blacklisted=True via the scalar update path (no re-embedding).
    await index.update_mem_by_id("u1", "s1", "m1", {"blacklisted": True})

    # 1) .md frontmatter round-trip: the flag is now a top-level key.
    listed = await index.list_memories("u1", "s1", 0, 100)
    doc = next(d for d in listed if d.id == "m1")
    assert doc.blacklisted is True, "blacklisted flag lost in .md round-trip"

    # 2) chunks table scalar: a fresh index over the same root_dir must
    #    surface the flag without re-syncing, so EQ(blacklisted, True)
    #    list_memories (the recall path) returns it.
    recall = await index.list_memories(
        "u1", "s1", 0, 100, filters=_eq_blacklisted(),
    )
    assert [d.id for d in recall] == ["m1"]

    # 3) Default path (NE(blacklisted, True)) now hides it.
    active = await index.list_memories(
        "u1", "s1", 0, 100, filters=_ne_blacklisted(),
    )
    assert active == [], "blacklisted memory leaked into default list path"


@pytest.mark.asyncio
async def test_search_ne_blacklisted_excludes_forgotten(index):
    """Search with the default NE(blacklisted, True) must hide blacklisted
    memories from the KNN candidate set.
    """
    await index.add_memories(
        "u1", "s1",
        [
            MemoryDoc(id="m1", text="alpha beta gamma", type="note"),
            MemoryDoc(id="m2", text="alpha beta gamma", type="note", blacklisted=True),
        ],
    )
    # Default search: NE(blacklisted, True) hides the blacklisted copy.
    hits = await index.search("u1", "s1", "alpha", top_k=5, filters=_ne_blacklisted())
    ids = [h[0].id for h in hits]
    assert "m2" not in ids, "blacklisted memory leaked into default search"
    assert "m1" in ids

    # Recall search: EQ(blacklisted, True) surfaces only the forgotten one.
    recall_hits = await index.search("u1", "s1", "alpha", top_k=5, filters=_eq_blacklisted())
    recall_ids = [h[0].id for h in recall_hits]
    assert recall_ids == ["m2"], f"recall path missing blacklisted mem, got {recall_ids}"


@pytest.mark.asyncio
async def test_search_non_pushdown_field_filter_excludes_in_python(index):
    """Non-SQL-pushable conditions (anything outside blacklisted /
    is_important) must be re-evaluated in Python after the MemoryDoc
    lookup in ``search`` — same fallback ``list_memories`` uses.

    Without the Python re-evaluation, ``_vec_index.search`` only pushes
    EQ/NE on blacklisted/is_important to SQL and silently drops the
    ``type`` condition, so the two read paths return inconsistent
    results for the same ``filters``.
    """
    await index.add_memories(
        "u1", "s1",
        [
            MemoryDoc(id="m1", text="alpha beta gamma", type="note"),
            MemoryDoc(id="m2", text="alpha beta gamma", type="private"),
        ],
    )
    # NE(type, "private") is NOT SQL-pushable (type is not in
    # _SQL_PUSHDOWN_FIELDS), so the SQL layer returns both rows and
    # the Python fallback in ``search`` must drop m2.
    hits = await index.search(
        "u1", "s1", "alpha", top_k=5,
        filters=FilterGroup(
            conditions=[FilterCondition(field="type",
                                       op=FilterOperator.NE, value="private")],
        ),
    )
    ids = [h[0].id for h in hits]
    assert "m2" not in ids, "non-pushable condition dropped by search"
    assert "m1" in ids

    # Cross-check: list_memories (which already had the Python fallback)
    # must agree with search on the same filters.
    listed = await index.list_memories(
        "u1", "s1", 0, 100,
        filters=FilterGroup(
            conditions=[FilterCondition(field="type",
                                        op=FilterOperator.NE, value="private")],
        ),
    )
    assert [d.id for d in listed] == ids, (
        "search and list_memories disagree on non-pushable filters"
    )


@pytest.mark.asyncio
async def test_search_default_path_no_filters_does_not_crash(index):
    """Even with zero memories and no filters, search must not raise
    MEMORY_GET_MEMORY_EXECUTION_ERROR — that's the original symptom
    from the report (no forgotten memories yet, default search).
    """
    hits = await index.search("u1", "s1", "anything", top_k=5)
    assert hits == []
    listed = await index.list_memories("u1", "s1", 0, 10)
    assert listed == []


@pytest.mark.asyncio
async def test_legacy_db_without_blacklisted_column_migrates(tmp_path):
    """A pre-Ebbinghaus chunks table (no blacklisted/is_important columns)
    must be migrated in place on reopen. Without the ALTER TABLE guard,
    upsert would raise 'table chunks has no column named blacklisted'.
    """
    # 1) Create a fresh VectorIndex so the schema is laid down.
    db_path = str(tmp_path / "memory.db")
    vi1 = VectorIndex(db_path=db_path, embedding_model=FakeEmbedding())
    # 2) Simulate a pre-Ebbinghaus schema by dropping the new columns.
    #    SQLite doesn't support DROP COLUMN before 3.35; rebuild the
    #    table without them by renaming + CREATE + INSERT SELECT + DROP.
    vi1.conn.execute("CREATE TABLE chunks_old AS SELECT mem_id, path, user_id, "
                     "scope_id, type, start_line, end_line, hash, text, embedding, "
                     "updated_at FROM chunks")
    vi1.conn.execute("DROP TABLE chunks")
    vi1.conn.execute("""
        CREATE TABLE chunks (
            mem_id TEXT PRIMARY KEY,
            path TEXT,
            user_id TEXT,
            scope_id TEXT,
            type TEXT,
            start_line INTEGER DEFAULT 0,
            end_line INTEGER DEFAULT 0,
            hash TEXT,
            text TEXT,
            embedding BLOB,
            updated_at TEXT
        )
    """)
    vi1.conn.execute("INSERT INTO chunks SELECT * FROM chunks_old")
    vi1.conn.execute("DROP TABLE chunks_old")
    vi1.conn.commit()
    vi1.close()

    # 3) Reopen. _ensure_schema's _migrate_add_column_if_missing must add
    #    the two columns back; a subsequent upsert must not raise.
    vi2 = VectorIndex(db_path=db_path, embedding_model=FakeEmbedding())
    await vi2.upsert(
        MemoryDoc(id="m1", text="hello", type="note", blacklisted=True),
        tenant=TenantScope("u1", "s1"),
    )
    # Column was added and the value round-trips.
    row = vi2.conn.execute(
        "SELECT blacklisted, is_important FROM chunks WHERE mem_id=?", ("m1",)
    ).fetchone()
    assert row is not None
    assert row[0] == 1
    assert row[1] == 0
