"""wikimem retained retrieval profile tests."""

from __future__ import annotations

from evaluation.wikimem.qmd_consensus import (
    CandidateFile,
    RetrievedMemoryFile,
    build_question_profile,
)
from evaluation.wikimem.retrieval_profile import (
    build_corpus_consensus_augmentation,
    build_session_source_files,
    collect_session_source_companions,
    infer_session_number_from_path,
    rank_global_session_sources,
    retrieve_qmd_consensus_files,
    scoped_budgets,
    select_scoped_candidate_files,
    source_companion_budget,
    source_injection_budget,
)


def test_retrieve_qmd_consensus_files_preserves_root_then_adds_plugin_candidates() -> None:
    files = [
        _file(
            "/tmp/kb/wiki/sources/session_1.md",
            "## Summary\nAlice inspected the drawer and found brass tools.\n"
            "## Turn Index\n- [turn 7](../turns/T7.md)",
        ),
        _file(
            "/tmp/kb/wiki/observations/D1_obs.md",
            "- Session: D1\n- Evidence: D1:7\nAlice found brass tools in the drawer.",
        ),
        _file(
            "/tmp/kb/wiki/turns/T7.md",
            "- Session: D1\n- Evidence: D1:7\nAlice inspected the drawer for brass tools.",
        ),
    ]

    result = retrieve_qmd_consensus_files(
        question="What did Alice find in the drawer?",
        files=files,
        root_files=[files[0]],
        entity_names=["Alice"],
        top_k=3,
    )

    assert result.profile.query_tokens == ["alice", "find", "drawer"]
    assert result.files[0].file_path == "/tmp/kb/wiki/sources/session_1.md"
    assert {file.file_path for file in result.files} == {
        "/tmp/kb/wiki/sources/session_1.md",
        "/tmp/kb/wiki/observations/D1_obs.md",
        "/tmp/kb/wiki/turns/T7.md",
    }
    assert result.coverage.root_file_paths == ["/tmp/kb/wiki/sources/session_1.md"]
    assert "/tmp/kb/wiki/turns/t7.md" in result.coverage.late_bridge_file_paths


def test_auto_root_selection_uses_bounded_projection_before_late_atomic_pages() -> None:
    files = [
        _file(
            "/tmp/kb/wiki/sources/session_1.md",
            "## Summary\nThe session covered a general project update.\n",
        ),
        _file(
            "/tmp/kb/wiki/turns/T1.md",
            "- Session: D1\n- Evidence: D1:1\nA general project update.",
        ),
    ]
    files.extend(
        _file(
            f"/tmp/kb/wiki/observations/D1_obs_{index}.md",
            "A neutral observation with no query terms.",
            mtime_ms=index,
        )
        for index in range(1, 205)
    )
    files.append(
        _file(
            "/tmp/kb/wiki/entities/Caroline.md",
            "Caroline identity details that should stay in the scoped pool.",
        )
    )

    result = retrieve_qmd_consensus_files(
        question="What is Caroline's identity?",
        files=files,
        entity_names=["Caroline"],
        top_k=3,
    )

    assert all("/wiki/observations/" not in path for path in result.coverage.root_file_paths)


def test_auto_root_selection_uses_body_phrase_fallback_for_plain_markdown() -> None:
    file = _file(
        "/tmp/kb/D1_4.md",
        "Evidence: D1:4\nartificial intelligence education image",
    )

    result = retrieve_qmd_consensus_files(
        question="education image",
        files=[file],
        top_k=1,
    )

    assert result.files[0].file_path == "/tmp/kb/D1_4.md"


def test_retrieve_qmd_consensus_files_respects_top_k_and_deduplicates_paths() -> None:
    files = [
        _file(
            "/tmp/kb/wiki/sources/session_1.md",
            "## Summary\nAlice inspected the drawer and found brass tools.\n"
            "## Turn Index\n- [turn 7](../turns/T7.md)",
        ),
        _file(
            "/tmp/kb/wiki/turns/T7.md",
            "- Session: D1\n- Evidence: D1:7\nAlice inspected the drawer for brass tools.",
        ),
    ]

    result = retrieve_qmd_consensus_files(
        question="What did Alice inspect?",
        files=files,
        root_files=[files[0], files[0]],
        entity_names=["Alice"],
        top_k=1,
    )

    assert [file.file_path for file in result.files] == ["/tmp/kb/wiki/sources/session_1.md"]
    assert result.coverage.final_file_paths == ["/tmp/kb/wiki/sources/session_1.md"]


def test_corpus_consensus_augmentation_corrects_query_typo_from_sources() -> None:
    files = [
        _file(
            "/tmp/kb/wiki/sources/session_1.md",
            "## Summary\nAlice repaired the drawer latch with brass tools.\n"
            "## Turn Index\n- [turn 1](../turns/T1.md)",
        ),
        _file(
            "/tmp/kb/wiki/sources/session_2.md",
            "## Summary\nAlice cleaned the drawer after the repair.\n"
            "## Turn Index\n- [turn 2](../turns/T2.md)",
        ),
    ]
    profile = build_question_profile("What did Alice put in the drawre?", ["Alice"])

    augmentation = build_corpus_consensus_augmentation(
        "What did Alice put in the drawre?",
        profile,
        build_session_source_files(files),
    )

    assert augmentation.tokens == ["drawer"]
    assert "drawe" in augmentation.fuzzy_tokens
    assert augmentation.phrases == []


def test_corpus_consensus_augmentation_adds_supported_anchor_tokens_and_phrases() -> None:
    files = [
        _file(
            "/tmp/kb/wiki/sources/session_1.md",
            "## Summary\nAlice organized the drawer brass tools for pottery studio work.\n"
            "## Turn Index\n- [turn 1](../turns/T1.md)",
        ),
        _file(
            "/tmp/kb/wiki/sources/session_2.md",
            "## Summary\nAlice photographed drawer brass tools beside ceramic glaze.\n"
            "## Turn Index\n- [turn 2](../turns/T2.md)",
        ),
    ]
    profile = build_question_profile("What activities did Alice do with the drawre?", ["Alice"])

    augmentation = build_corpus_consensus_augmentation(
        "What activities did Alice do with the drawre?",
        profile,
        build_session_source_files(files),
    )

    assert augmentation.tokens[:2] == ["drawer", "brass"]
    assert "tools" in augmentation.tokens
    assert "brass tools" in augmentation.phrases


def test_retrieve_qmd_consensus_files_uses_corpus_correction_for_candidates() -> None:
    files = [
        _file(
            "/tmp/kb/wiki/entities/alice.md",
            "Alice keeps household repair notes.",
        ),
        _file(
            "/tmp/kb/wiki/sources/session_1.md",
            "## Summary\nAlice repaired the drawer latch with brass tools.\n"
            "## Turn Index\n- [turn 1](../turns/T1.md)",
        ),
        _file(
            "/tmp/kb/wiki/observations/D1_obs.md",
            "- Session: D1\n- Evidence: D1:2\nAlice placed brass tools in the drawer.",
        ),
    ]

    result = retrieve_qmd_consensus_files(
        question="What did Alice put in the drawre?",
        files=files,
        root_files=[files[0]],
        entity_names=["Alice"],
        top_k=3,
    )

    assert "drawer" in result.profile.expansion_tokens
    assert "/tmp/kb/wiki/observations/D1_obs.md" in result.coverage.scoped_file_paths
    assert "/tmp/kb/wiki/observations/D1_obs.md" in result.coverage.final_file_paths


def test_rank_global_session_sources_prefers_query_phrase_entity_and_mtime() -> None:
    files = [
        _file(
            "/tmp/kb/wiki/sources/session_1.md",
            "## Summary\nAlice repaired the brass drawer latch.\n"
            "## Turn Index\n- [turn 1](../turns/T1.md)",
            mtime_ms=10,
        ),
        _file(
            "/tmp/kb/wiki/sources/session_2.md",
            "## Summary\nAlice repaired the brass drawer latch.\n"
            "## Turn Index\n- [turn 2](../turns/T2.md)",
            mtime_ms=30,
        ),
        _file(
            "/tmp/kb/wiki/sources/session_3.md",
            "## Summary\nBlake reviewed kitchen plans.\n"
            "## Turn Index\n- [turn 3](../turns/T3.md)",
            mtime_ms=99,
        ),
    ]
    profile = build_question_profile("What did Alice repair in the brass drawer?", ["Alice"])

    ranked = rank_global_session_sources(
        "What did Alice repair in the brass drawer?",
        profile,
        build_session_source_files(files),
    )

    assert [source.session_number for source in ranked] == [2, 1, 3]


def test_source_budget_helpers_match_retained_profile_flags() -> None:
    aggregate = build_question_profile("What activities did Alice do?", ["Alice"])
    temporal = build_question_profile("When did Alice visit the gallery?", ["Alice"])
    location = build_question_profile("Where did Alice meet Blake?", ["Alice"])
    neutral = build_question_profile("What did Alice repair?", ["Alice"])

    assert source_injection_budget(aggregate) == 8
    assert source_injection_budget(temporal) == 4
    assert source_injection_budget(location) == 3
    assert source_injection_budget(neutral) == 3

    assert source_companion_budget(aggregate, has_plugin_retrieval=False, top_k=24) == 2
    assert source_companion_budget(aggregate, has_plugin_retrieval=True, top_k=24) == 4
    assert source_companion_budget(temporal, has_plugin_retrieval=True, top_k=24) == 3
    assert source_companion_budget(aggregate, has_plugin_retrieval=True, top_k=2) == 2


def test_scoped_budgets_match_retained_profile_flags() -> None:
    temporal = build_question_profile("When did Alice visit the gallery?", ["Alice"])
    identity = build_question_profile("Who is Alice's mentor?", ["Alice"])
    location = build_question_profile("Where did Alice meet Blake?", ["Alice"])
    neutral = build_question_profile("What did Alice repair?", ["Alice"])

    assert scoped_budgets(temporal) == [
        ("wiki/sources", 3, 5.0),
        ("wiki/observations", 3, 4.0),
        ("wiki/events", 2, 3.0),
        ("wiki/turns", 1, 1.0),
        ("wiki/entities", 1, 2.0),
    ]
    assert scoped_budgets(identity) == [
        ("wiki/entities", 2, 4.0),
        ("wiki/sources", 3, 5.0),
        ("wiki/observations", 3, 4.0),
        ("wiki/turns", 1, 2.0),
        ("wiki/events", 1, 1.0),
    ]
    assert scoped_budgets(location) == [
        ("wiki/sources", 2, 4.0),
        ("wiki/observations", 3, 4.0),
        ("wiki/events", 2, 3.0),
        ("wiki/turns", 1, 1.0),
        ("wiki/entities", 1, 1.0),
    ]
    assert scoped_budgets(neutral) == [
        ("wiki/sources", 2, 3.0),
        ("wiki/observations", 3, 3.0),
        ("wiki/events", 2, 2.0),
        ("wiki/entities", 1, 2.0),
        ("wiki/turns", 1, 1.0),
    ]


def test_select_scoped_candidate_files_prefers_matching_files_per_scope() -> None:
    files = [
        _file(
            "/tmp/kb/wiki/observations/D7_obs.md",
            "- Session: D7\n- Evidence: D7:2\nAlice repaired the drawer on Monday.",
        ),
        _file(
            "/tmp/kb/wiki/observations/D2_obs.md",
            "- Session: D2\n- Evidence: D2:1\nBlake planned groceries.",
        ),
        _file(
            "/tmp/kb/wiki/events/session_7_event.md",
            "Alice repair event: the drawer latch was fixed on Monday.",
        ),
        _file(
            "/tmp/kb/wiki/turns/T7.md",
            "- Session: D7\nAlice repaired the drawer during the evening.",
        ),
    ]
    profile = build_question_profile("When did Alice repair the drawer?", ["Alice"])

    candidates = select_scoped_candidate_files(
        "When did Alice repair the drawer?",
        profile,
        files,
    )

    assert [candidate.file.file_path for candidate in candidates][:3] == [
        "/tmp/kb/wiki/observations/D7_obs.md",
        "/tmp/kb/wiki/events/session_7_event.md",
        "/tmp/kb/wiki/turns/T7.md",
    ]
    assert all(candidate.query_hits >= 1 for candidate in candidates[:3])


def test_retrieve_qmd_consensus_files_adds_scoped_candidates_for_temporal_query() -> None:
    files = [
        _file(
            "/tmp/kb/wiki/sources/session_7.md",
            "## Summary\nAlice repaired the drawer after lunch.\n"
            "## Turn Index\n- [turn 7](../turns/T7.md)",
        ),
        _file(
            "/tmp/kb/wiki/observations/D7_obs.md",
            "- Session: D7\n- Evidence: D7:2\nAlice repaired the drawer after lunch.",
        ),
        _file(
            "/tmp/kb/wiki/events/session_7_event.md",
            "Alice drawer repair event happened after lunch.",
        ),
        _file(
            "/tmp/kb/wiki/entities/blake.md",
            "Blake discussed unrelated travel plans.",
        ),
    ]

    result = retrieve_qmd_consensus_files(
        question="When did Alice repair the drawer?",
        files=files,
        root_files=[files[0]],
        entity_names=["Alice"],
        top_k=4,
    )

    assert "/tmp/kb/wiki/observations/D7_obs.md" in result.coverage.scoped_file_paths
    assert "/tmp/kb/wiki/events/session_7_event.md" in result.coverage.scoped_file_paths
    assert "/tmp/kb/wiki/observations/D7_obs.md" in result.coverage.final_file_paths


def test_retrieve_qmd_consensus_files_records_late_bridge_targets_from_seed_links() -> None:
    files = [
        _file(
            "/tmp/kb/wiki/sources/session_1.md",
            "## Summary\nAlice mentioned a linked workshop note.\n"
            "## Turn Index\n- [turn 8](../turns/T8.md)",
        ),
        _file(
            "/tmp/kb/wiki/turns/T8.md",
            "- Session: D8\n- Evidence: D8:2\n"
            "The brass tools were displayed during the workshop.",
        ),
    ]

    result = retrieve_qmd_consensus_files(
        question="What activities did Alice connect with brass tools?",
        files=files,
        root_files=[files[0]],
        entity_names=["Alice"],
        top_k=2,
    )

    assert result.coverage.late_bridge_file_paths == ["/tmp/kb/wiki/turns/t8.md"]
    assert "/tmp/kb/wiki/turns/T8.md" in result.coverage.final_file_paths


def test_retrieve_qmd_consensus_files_injects_ranked_sources_before_candidates() -> None:
    files = [
        _file(
            "/tmp/kb/wiki/entities/alice.md",
            "Alice keeps notes about household repairs.",
        ),
        _file(
            "/tmp/kb/wiki/sources/session_7.md",
            "## Summary\nAlice repaired the brass drawer latch.\n"
            "## Turn Index\n- [turn 7](../turns/T7.md)",
            mtime_ms=70,
        ),
        _file(
            "/tmp/kb/wiki/turns/T7.md",
            "- Session: D7\n- Evidence: D7:1\nAlice repaired the brass drawer latch.",
        ),
        _file(
            "/tmp/kb/wiki/observations/D7_obs.md",
            "- Session: D7\n- Evidence: D7:1\nThe brass drawer latch repair succeeded.",
        ),
    ]

    result = retrieve_qmd_consensus_files(
        question="What did Alice repair in the brass drawer?",
        files=files,
        root_files=[files[0]],
        entity_names=["Alice"],
        top_k=3,
    )

    assert [file.file_path for file in result.files][:2] == [
        "/tmp/kb/wiki/entities/alice.md",
        "/tmp/kb/wiki/sources/session_7.md",
    ]
    assert len(result.files) == 3
    assert "/tmp/kb/wiki/sources/session_7.md" in result.coverage.source_file_paths


def test_collect_session_source_companions_uses_path_and_content_session_signals() -> None:
    source_7 = _file(
        "/tmp/kb/wiki/sources/session_7.md",
        "## Summary\nAlice repaired the brass drawer latch.\n"
        "## Turn Index\n- [turn 7](../turns/T7.md)",
    )
    source_8 = _file(
        "/tmp/kb/wiki/sources/session_8.md",
        "## Summary\nBlake documented D8 gallery logistics.\n"
        "## Turn Index\n- [turn 8](../turns/T8.md)",
    )
    sources = build_session_source_files([source_7, source_8])
    sources_by_session = {source.session_number: source for source in sources}
    profile = build_question_profile("What did Alice repair in the brass drawer?", ["Alice"])
    candidates = [
        CandidateFile(
            file=_file(
                "/tmp/kb/wiki/turns/T7.md",
                "- Session: D7\n- Evidence: D7:1\nAlice repaired the brass drawer latch.",
            ),
            query_hits=2,
            seed_boost=1.5,
        ),
        CandidateFile(
            file=_file(
                "/tmp/kb/wiki/entities/alice.md",
                "Alice asked Blake to compare session_8 and D7 logistics.",
            ),
            query_hits=1,
            seed_boost=0.5,
        ),
    ]

    companions = collect_session_source_companions(
        "What did Alice repair in the brass drawer?",
        profile,
        candidates,
        sources_by_session,
    )

    assert [companion.file.file_path for companion in companions] == [
        "/tmp/kb/wiki/sources/session_7.md",
        "/tmp/kb/wiki/sources/session_8.md",
    ]
    assert infer_session_number_from_path("/tmp/kb/wiki/turns/T7.md") == 7
    assert infer_session_number_from_path("/tmp/kb/wiki/observations/D8_obs.md") == 8


def _file(path: str, content: str, mtime_ms: int = 0) -> RetrievedMemoryFile:
    return RetrievedMemoryFile(
        filename=path.rsplit("/", maxsplit=1)[-1],
        file_path=path,
        mtime_ms=mtime_ms,
        content=content,
    )
