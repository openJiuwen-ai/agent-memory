"""qmd_consensus retrieval plugin compatibility tests."""

from __future__ import annotations

import math

from evaluation.wikimem.qmd_consensus import (
    CandidateFile,
    RetrievedMemoryFile,
    apply_query_augmentation,
    build_cached_file_lexical_features,
    build_qmd_consensus_augmentation,
    build_qmd_consensus_candidate_proposals,
    build_qmd_consensus_late_bridge_proposals,
    build_qmd_consensus_rerank_proposals,
    build_question_profile,
    candidate_query_hits_with_features,
    normalize_memory_path,
    qmd_consensus_is_conservative,
    score_line,
)


def test_build_question_profile_matches_rust_flags_and_tokens() -> None:
    profile = build_question_profile(
        "Where would Alice pursue art planning with Bob?",
        ["Alice", "Bob", "Charlie"],
    )

    assert profile.query_tokens == ["alice", "pursue", "art", "planning", "with", "bob"]
    assert "Alice" in profile.named_entities
    assert "Bob" in profile.named_entities
    assert profile.location is True
    assert profile.hypothetical is True
    assert qmd_consensus_is_conservative(profile) is False


def test_qmd_conservative_profile_skips_plain_temporal_questions() -> None:
    profile = build_question_profile("When did Alice visit the museum?", ["Alice"])

    assert profile.temporal is True
    assert qmd_consensus_is_conservative(profile) is True


def test_cached_features_collect_normalized_bridge_targets() -> None:
    source = _file(
        "/tmp/kb/wiki/sources/session_1.md",
        "[turn](../turns/T7.md)\n[event](../events/session_2.md)\n[web](https://example.com)",
    )
    features = build_cached_file_lexical_features(source)

    assert normalize_memory_path(r"C:\tmp\KB\wiki\turns\T7.md") == "c:/tmp/kb/wiki/turns/t7.md"
    assert features.bridge_target_paths == [
        "/tmp/kb/wiki/turns/t7.md",
        "/tmp/kb/wiki/events/session_2.md",
    ]


def test_cached_features_reuse_equal_files_without_stale_content() -> None:
    build_cached_file_lexical_features.cache_clear()
    file = _file("/tmp/kb/wiki/turns/T7.md", "Alice inspected the drawer.")

    first = build_cached_file_lexical_features(file)
    second = build_cached_file_lexical_features(file)
    changed = build_cached_file_lexical_features(
        _file("/tmp/kb/wiki/turns/T7.md", "Bob inspected the drawer.")
    )

    assert second is first
    assert changed is not first


def test_score_line_uses_exact_fuzzy_phrase_and_soft_overlap() -> None:
    profile = build_question_profile("What musical activities did Alice pursue?", ["Alice"])

    assert score_line("Alice pursued musicals at the academy.", profile, profile.question) > 6.0
    assert math.isclose(
        score_line("Unrelated weather note.", profile, profile.question),
        0.0,
    )


def test_candidate_query_hits_counts_token_and_fuzzy_overlap() -> None:
    profile = build_question_profile("What did Alice inspect in the drawer?", ["Alice"])
    features = build_cached_file_lexical_features(
        _file("/tmp/kb/wiki/turns/T7.md", "Alice inspected the drawer and found tools.")
    )

    assert candidate_query_hits_with_features(features, profile) >= 4


def test_qmd_augmentation_uses_supported_seed_lines() -> None:
    profile = build_question_profile("What musical activities would Alice pursue?", ["Alice"])
    root_files = [
        _file(
            "/tmp/kb/wiki/sources/session_1.md",
            "Alice discussed ceramics and musical theatre with Bob.\n"
            "Alice discussed ceramics and musical theatre again.",
        ),
        _file(
            "/tmp/kb/wiki/entities/alice.md",
            "Alice would pursue ceramics and musical theatre after classes.",
        ),
    ]

    augmentation = build_qmd_consensus_augmentation(profile.question, profile, root_files)

    assert "ceramics" in augmentation.tokens
    assert any(phrase == "musical theatre" for phrase in augmentation.phrases)
    expanded = apply_query_augmentation(profile, augmentation)
    assert "ceramics" in expanded.expansion_tokens


def test_qmd_candidate_proposals_fuse_source_anchor_and_linked_views() -> None:
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
    profile = build_question_profile("What did Alice find in the drawer?", ["Alice"])

    proposals = build_qmd_consensus_candidate_proposals(profile.question, profile, files)

    paths = [proposal.file_path for proposal in proposals]
    assert "/tmp/kb/wiki/sources/session_1.md" in paths
    assert "/tmp/kb/wiki/observations/d1_obs.md" in paths
    assert "/tmp/kb/wiki/turns/t7.md" in paths


def test_qmd_rerank_proposals_add_linked_targets_from_seed_sources() -> None:
    files = [
        _file(
            "/tmp/kb/wiki/sources/session_1.md",
            "## Summary\nAlice inspected the drawer and found brass tools.\n"
            "## Turn Index\n- [turn 7](../turns/T7.md)",
        ),
        _file(
            "/tmp/kb/wiki/turns/T7.md",
            "- Session: D1\n- Evidence: D1:7\nAlice inspected the drawer and found brass tools.",
        ),
    ]
    profile = build_question_profile("What did Alice find in the drawer?", ["Alice"])
    ranked = [CandidateFile(file=files[0], query_hits=3, seed_boost=7.0)]

    proposals = build_qmd_consensus_rerank_proposals(profile.question, profile, ranked, files)

    assert any(proposal.file_path == "/tmp/kb/wiki/turns/t7.md" for proposal in proposals)


def test_qmd_late_bridge_proposals_follow_seed_links_to_cross_session_targets() -> None:
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
    profile = build_question_profile(
        "What activities did Alice connect with brass tools?",
        ["Alice"],
    )

    proposals = build_qmd_consensus_late_bridge_proposals(
        profile.question,
        profile,
        seed_files=[files[0]],
        files=files,
    )

    assert [proposal.file_path for proposal in proposals] == ["/tmp/kb/wiki/turns/t8.md"]


def _file(path: str, content: str) -> RetrievedMemoryFile:
    return RetrievedMemoryFile(
        filename=path.rsplit("/", maxsplit=1)[-1],
        file_path=path,
        mtime_ms=0,
        content=content,
    )
