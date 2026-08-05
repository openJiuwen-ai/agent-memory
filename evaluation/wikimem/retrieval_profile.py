"""Retained wikimem retrieval profile assembly helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from pathlib import PurePosixPath

from common.llm.base import LLM

from evaluation.wikimem.qmd_consensus import (
    CandidateFile,
    QueryAugmentation,
    QuestionProfile,
    RetrievedMemoryFile,
    apply_query_augmentation,
    build_cached_file_lexical_features,
    build_qmd_consensus_augmentation,
    build_qmd_consensus_candidate_proposals,
    build_qmd_consensus_late_bridge_proposals,
    build_qmd_consensus_rerank_proposals,
    build_question_profile,
    candidate_query_hits_with_features,
    keyword_ngrams,
    normalize_memory_path,
    score_line,
    significant_phrases,
    tokenize_fuzzy_query,
    tokenize_query,
)
from evaluation.wikimem.llm_semantics import QueryUnderstanding, understand_query


@dataclass(frozen=True)
class RetrievalProfileCoverage:
    root_file_paths: list[str] = field(default_factory=list)
    source_file_paths: list[str] = field(default_factory=list)
    source_companion_file_paths: list[str] = field(default_factory=list)
    scoped_file_paths: list[str] = field(default_factory=list)
    candidate_pool_file_paths: list[str] = field(default_factory=list)
    late_bridge_file_paths: list[str] = field(default_factory=list)
    final_file_paths: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RetrievalProfileResult:
    profile: QuestionProfile
    files: list[RetrievedMemoryFile]
    coverage: RetrievalProfileCoverage


@dataclass(frozen=True)
class SessionSourceFile:
    session_number: int
    file: RetrievedMemoryFile
    search_text: str
    search_tokens: list[str]
    search_fuzzy_tokens: list[str]


def _apply_llm_query_understanding(
    profile: QuestionProfile,
    llm: LLM,
    question: str,
    known_entities: list[str],
) -> QuestionProfile:
    """Merge conservative LLM intent/entity expansions into the lexical profile."""

    try:
        understanding: QueryUnderstanding = understand_query(
            llm, question, known_entities=known_entities
        )
    except Exception:
        # Query understanding is an enhancement, never a retrieval hard dependency.
        return profile
    intent = understanding.intent.casefold()
    extra_terms = tuple(
        value
        for value in (
            *understanding.expanded_terms,
            understanding.relation,
            understanding.time_expression,
            *understanding.memory_kinds,
        )
        if value
    )
    extra_phrases = tuple(
        value
        for value in (understanding.relation, understanding.time_expression)
        if value
    )
    return replace(
        profile,
        named_entities=list(dict.fromkeys((*profile.named_entities, *understanding.entities))),
        expansion_tokens=list(
            dict.fromkeys(
                (
                    *profile.expansion_tokens,
                    *tokenize_query(" ".join(extra_terms)),
                )
            )
        ),
        expansion_fuzzy_tokens=list(
            dict.fromkeys(
                (
                    *profile.expansion_fuzzy_tokens,
                    *tokenize_fuzzy_query(" ".join(extra_terms)),
                )
            )
        ),
        expansion_phrases=list(
            dict.fromkeys(
                (
                    *profile.expansion_phrases,
                    *extra_phrases,
                    *[term for term in extra_terms if len(tokenize_query(term)) > 1],
                )
            )
        ),
        temporal=profile.temporal or bool(understanding.time_expression),
        relational=(
            profile.relational
            or intent in {"compare", "decision", "preference"}
            or bool(understanding.relation)
        ),
        identity=profile.identity or intent == "profile",
    )


def retrieve_qmd_consensus_files(
    *,
    question: str,
    files: list[RetrievedMemoryFile],
    root_files: list[RetrievedMemoryFile] | None = None,
    entity_names: list[str] | None = None,
    top_k: int = 24,
    knowledge_root: str | Path | None = None,
    retrieval_plugins: list[str] | None = None,
    llm: LLM | None = None,
) -> RetrievalProfileResult:
    """Assemble qmd_consensus retrieval files using the Rust retained profile order."""

    limit = max(top_k, 1)
    internal_limit = max(top_k, 8)
    plugin_names = (
        ["qmd_consensus"]
        if retrieval_plugins is None
        else [name.strip().lower().replace("-", "_") for name in retrieval_plugins]
    )
    unsupported_plugins = set(plugin_names) - {"qmd_consensus"}
    if unsupported_plugins:
        raise ValueError(
            "Python retained profile only implements qmd_consensus; unsupported "
            f"plugins: {sorted(unsupported_plugins)}"
        )
    use_qmd_plugin = "qmd_consensus" in plugin_names
    cached_files = [file for file in files if _is_rust_cached_retrieval_file(file)]
    cached_files_by_path = {
        normalize_memory_path(file.file_path): file for file in cached_files
    }
    profile = build_question_profile(question, entity_names or [])
    if llm is not None:
        profile = _apply_llm_query_understanding(profile, llm, question, entity_names or [])
    roots = (
        list(root_files)
        if root_files
        else _rank_initial_root_files(question, files, internal_limit, knowledge_root)
    )
    session_sources = build_session_source_files(files)
    corpus_augmentation = build_corpus_consensus_augmentation(question, profile, session_sources)
    scoped_query = _compose_augmented_retrieval_query(question, corpus_augmentation)
    profile = apply_query_augmentation(profile, corpus_augmentation)
    augmentation = (
        build_qmd_consensus_augmentation(question, profile, roots)
        if use_qmd_plugin
        else QueryAugmentation(tokens=[], fuzzy_tokens=[], phrases=[])
    )
    profile = apply_query_augmentation(profile, augmentation)

    candidate_proposals = (
        build_qmd_consensus_candidate_proposals(question, profile, cached_files)
        if use_qmd_plugin
        else []
    )
    plugin_candidates = [
        CandidateFile(
            file=cached_files_by_path[proposal.file_path],
            query_hits=proposal.query_hits,
            seed_boost=proposal.seed_boost,
        )
        for proposal in candidate_proposals
        if proposal.file_path in cached_files_by_path
    ]
    ranked_candidates = _merge_candidate_files(
        [CandidateFile(file=file, query_hits=2, seed_boost=0.0) for file in roots],
        plugin_candidates,
    )
    scoped_candidates = select_scoped_candidate_files(
        scoped_query,
        profile,
        files,
        knowledge_root=knowledge_root,
    )
    ranked_candidates = _merge_candidate_files(ranked_candidates, scoped_candidates)
    sources_by_session = {source.session_number: source for source in session_sources}
    source_companions = collect_session_source_companions(
        question,
        profile,
        ranked_candidates,
        sources_by_session,
    )
    # Rust inserts companion proposals into the main candidate map before the
    # candidate ranking pass, while retaining the same proposals for the
    # dedicated companion budget in final assembly.
    ranked_candidates = _merge_candidate_files(ranked_candidates, source_companions)
    ranked_candidates = _rank_candidate_files(
        question,
        profile,
        ranked_candidates,
        knowledge_root=knowledge_root,
    )
    ranked_sources = rank_global_session_sources(question, profile, session_sources)
    ranked_source_companions = _rank_candidate_files(
        question,
        profile,
        source_companions,
        knowledge_root=knowledge_root,
    )
    rerank_proposals = (
        build_qmd_consensus_rerank_proposals(
            question,
            profile,
            ranked_candidates,
            cached_files,
        )
        if use_qmd_plugin
        else []
    )
    reranked_candidates = _rerank_candidate_files(
        question,
        profile,
        ranked_candidates,
        rerank_proposals,
        cached_files_by_path,
        knowledge_root=knowledge_root,
    )
    late_bridge_proposals = (
        build_qmd_consensus_late_bridge_proposals(
            question,
            profile,
            _collect_late_bridge_seed_files(
                roots,
                reranked_candidates,
                ranked_source_companions,
            ),
            cached_files,
        )
        if use_qmd_plugin
        else []
    )
    ranked_late_bridges = _rank_candidate_files(
        question,
        profile,
        [
            CandidateFile(
                file=cached_files_by_path[proposal.file_path],
                query_hits=proposal.query_hits,
                seed_boost=proposal.seed_boost,
            )
            for proposal in late_bridge_proposals
            if proposal.file_path in cached_files_by_path
        ],
        knowledge_root=knowledge_root,
    )

    final_files: list[RetrievedMemoryFile] = []
    included: set[str] = set()
    preserved_primary = min(limit, max(3, (limit + 1) // 2))

    for file in roots[:preserved_primary]:
        _push_unique_file(final_files, included, file, limit)
    selected_sources = select_diverse_session_sources(
        profile,
        roots[:preserved_primary],
        ranked_sources,
        source_injection_budget(profile),
    )
    for source in selected_sources:
        _push_unique_file(final_files, included, source.file, limit)
    companion_limit = source_companion_budget(
        profile,
        has_plugin_retrieval=use_qmd_plugin,
        top_k=limit,
    )
    for companion in ranked_source_companions[:companion_limit]:
        _push_unique_file(final_files, included, companion.file, limit)
    for candidate in reranked_candidates:
        _push_unique_file(final_files, included, candidate.file, limit)
        if len(final_files) >= limit:
            break
    for candidate in ranked_late_bridges:
        _push_unique_file(final_files, included, candidate.file, limit)
        if len(final_files) >= limit:
            break

    return RetrievalProfileResult(
        profile=profile,
        files=final_files,
        coverage=RetrievalProfileCoverage(
            root_file_paths=[file.file_path for file in roots],
            source_file_paths=[source.file.file_path for source in selected_sources],
            source_companion_file_paths=[
                candidate.file.file_path for candidate in ranked_source_companions
            ],
            scoped_file_paths=[candidate.file.file_path for candidate in scoped_candidates],
            candidate_pool_file_paths=[
                candidate.file.file_path
                for candidate in ranked_candidates + ranked_source_companions
            ],
            late_bridge_file_paths=[proposal.file_path for proposal in late_bridge_proposals],
            final_file_paths=[file.file_path for file in final_files],
        ),
    )


def scoped_budgets(profile: QuestionProfile) -> list[tuple[str, int, float]]:
    if profile.temporal:
        return [
            ("wiki/sources", 3, 5.0),
            ("wiki/memory", 3, 4.5),
            ("wiki/observations", 3, 4.0),
            ("wiki/events", 2, 3.0),
            ("wiki/turns", 1, 1.0),
            ("wiki/entities", 1, 2.0),
        ]
    if profile.identity or profile.hypothetical or profile.relational:
        return [
            ("wiki/entities", 2, 4.0),
            ("wiki/memory", 3, 4.5),
            ("wiki/sources", 3, 5.0),
            ("wiki/observations", 3, 4.0),
            ("wiki/turns", 1, 2.0),
            ("wiki/events", 1, 1.0),
        ]
    if profile.location:
        return [
            ("wiki/sources", 2, 4.0),
            ("wiki/memory", 3, 4.0),
            ("wiki/observations", 3, 4.0),
            ("wiki/events", 2, 3.0),
            ("wiki/turns", 1, 1.0),
            ("wiki/entities", 1, 1.0),
        ]
    return [
        ("wiki/sources", 2, 3.0),
        ("wiki/memory", 3, 3.0),
        ("wiki/observations", 3, 3.0),
        ("wiki/events", 2, 2.0),
        ("wiki/entities", 1, 2.0),
        ("wiki/turns", 1, 1.0),
    ]


def build_corpus_consensus_augmentation(
    question: str,
    base_profile: QuestionProfile,
    session_sources: list[SessionSourceFile],
) -> QueryAugmentation:
    if not session_sources:
        return QueryAugmentation(tokens=[], fuzzy_tokens=[], phrases=[])

    token_df = session_source_token_document_frequency(session_sources)
    corrected_tokens = [
        correction
        for token in base_profile.query_tokens
        if (
            correction := best_corpus_correction(
                token,
                token_df,
                base_profile.named_entities,
            )
        )
        is not None
    ]
    correction_augmentation = QueryAugmentation(
        tokens=corrected_tokens,
        fuzzy_tokens=tokenize_fuzzy_query(" ".join(corrected_tokens)),
        phrases=[],
    )
    corrected_profile = apply_query_augmentation(base_profile, correction_augmentation)
    if base_profile.temporal and not base_profile.hypothetical and not base_profile.aggregate:
        return correction_augmentation
    if (
        not base_profile.hypothetical
        and not base_profile.identity
        and not base_profile.relational
        and not base_profile.aggregate
    ):
        return correction_augmentation

    return _extend_corpus_augmentation_with_anchors(
        question,
        corrected_profile,
        session_sources,
        token_df,
        correction_augmentation,
    )


def session_source_token_document_frequency(
    session_sources: list[SessionSourceFile],
) -> dict[str, int]:
    document_frequency: dict[str, int] = {}
    for source in session_sources:
        for token in set(source.search_tokens):
            document_frequency[token] = document_frequency.get(token, 0) + 1
    return document_frequency


def best_corpus_correction(
    token: str,
    token_df: dict[str, int],
    named_entities: list[str],
) -> str | None:
    if (
        len(token) < 5
        or token in token_df
        or any(entity.lower() == token.lower() for entity in named_entities)
    ):
        return None

    query_features = tokenize_fuzzy_query(token)
    candidates = []
    for candidate, df in token_df.items():
        if (
            len(candidate) < 5
            or candidate == token
            or candidate[0] != token[0]
            or abs(len(candidate) - len(token)) > 2
        ):
            continue
        distance = _bounded_edit_distance(token, candidate, 2)
        if distance is None:
            continue
        feature_overlap = _token_overlap(query_features, tokenize_fuzzy_query(candidate))
        if distance > 1 and feature_overlap == 0:
            continue
        candidates.append((candidate, distance, feature_overlap, df))

    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[1], -item[2], -item[3], item[0]))
    return candidates[0][0]


def _extend_corpus_augmentation_with_anchors(
    question: str,
    corrected_profile: QuestionProfile,
    session_sources: list[SessionSourceFile],
    token_df: dict[str, int],
    correction_augmentation: QueryAugmentation,
) -> QueryAugmentation:
    query_ngrams = keyword_ngrams(question)
    significant_question_phrases = significant_phrases(question)
    named_lower = [name.lower() for name in corrected_profile.named_entities]
    ranked_sources = rank_global_session_sources(question, corrected_profile, session_sources)
    token_scores: dict[str, float] = {}
    token_support: dict[str, set[int]] = {}
    phrase_scores: dict[str, float] = {}
    phrase_support: dict[str, set[int]] = {}

    for rank, source in enumerate(ranked_sources[:4]):
        weight = 1.35 - float(rank) * 0.2
        for fragment in _best_source_anchor_fragments(
            source,
            corrected_profile,
            question,
            correction_augmentation.tokens,
        ):
            for token in tokenize_query(fragment):
                if not _should_keep_corpus_anchor_token(
                    token,
                    corrected_profile,
                    named_lower,
                    token_df,
                ):
                    continue
                token_scores[token] = token_scores.get(token, 0.0) + weight
                token_support.setdefault(token, set()).add(source.session_number)
            for phrase in keyword_ngrams(fragment):
                if phrase in significant_question_phrases or phrase in query_ngrams:
                    continue
                if not _should_keep_corpus_anchor_phrase(
                    phrase,
                    corrected_profile,
                    token_df,
                ):
                    continue
                phrase_scores[phrase] = phrase_scores.get(phrase, 0.0) + weight
                phrase_support.setdefault(phrase, set()).add(source.session_number)

    anchor_tokens = sorted(
        token_scores.items(),
        key=lambda item: (
            -len(token_support.get(item[0], set())),
            -item[1],
            token_df.get(item[0], 1),
            item[0],
        ),
    )
    merged_tokens = list(correction_augmentation.tokens)
    seen_tokens = set(merged_tokens)
    for token, score in anchor_tokens:
        support = len(token_support.get(token, set()))
        if support < 2 and score < 1.0:
            continue
        if token not in seen_tokens:
            merged_tokens.append(token)
            seen_tokens.add(token)
        if len(merged_tokens) >= 6:
            break

    token_set = set(merged_tokens)
    anchor_phrases = sorted(
        phrase_scores.items(),
        key=lambda item: (
            -len(phrase_support.get(item[0], set())),
            -item[1],
            item[0],
        ),
    )
    phrases = []
    for phrase, score in anchor_phrases:
        support = len(phrase_support.get(phrase, set()))
        if support < 2 and score < 1.1:
            continue
        if not any(token in token_set for token in phrase.split()):
            continue
        phrases.append(phrase)
        if len(phrases) >= 3:
            break

    return QueryAugmentation(
        tokens=merged_tokens,
        fuzzy_tokens=tokenize_fuzzy_query(" ".join(merged_tokens)),
        phrases=phrases,
    )


def _best_source_anchor_fragments(
    source: SessionSourceFile,
    profile: QuestionProfile,
    question: str,
    corrected_tokens: list[str],
) -> list[str]:
    corrected_fuzzy = tokenize_fuzzy_query(" ".join(corrected_tokens))
    fragments = []
    for fragment in _split_anchor_fragments(
        _extract_source_summary_snippet(source.file.content)
    ) + _split_anchor_fragments(_extract_source_turn_index_snippet(source.file.content)):
        tokens = tokenize_query(fragment)
        fuzzy_tokens = tokenize_fuzzy_query(fragment)
        score = (
            _token_overlap(profile.query_tokens, tokens) * 1.8
            + _token_overlap(profile.query_fuzzy_tokens, fuzzy_tokens) * 1.0
            + _token_overlap(profile.expansion_tokens, tokens) * 2.2
            + _token_overlap(profile.expansion_fuzzy_tokens, fuzzy_tokens) * 1.4
        )
        if score <= 0.0:
            continue
        if corrected_tokens and not (
            _token_overlap(corrected_tokens, tokens) > 0
            or _token_overlap(corrected_fuzzy, fuzzy_tokens) > 0
        ):
            continue
        if not profile.expansion_tokens and question.lower() not in fragment:
            continue
        fragments.append((fragment, score))
    fragments.sort(key=lambda item: (-item[1], -len(item[0]), item[0]))
    return [fragment for fragment, _ in fragments[:3]]


def _split_anchor_fragments(text: str) -> list[str]:
    return [
        fragment.strip().lower()
        for fragment in re.split(r"[\n.!?;,]", text)
        if len(fragment.strip()) >= 12
    ]


def _should_keep_corpus_anchor_token(
    token: str,
    profile: QuestionProfile,
    named_lower: list[str],
    token_df: dict[str, int],
) -> bool:
    blocked = {
        "about",
        "again",
        "along",
        "been",
        "being",
        "great",
        "into",
        "just",
        "look",
        "looking",
        "really",
        "said",
        "shared",
        "talked",
        "their",
        "them",
        "they",
    }
    return (
        len(token) >= 4
        and token not in blocked
        and token not in profile.query_tokens
        and token not in profile.expansion_tokens
        and token not in named_lower
        and token_df.get(token, 0) <= 8
    )


def _should_keep_corpus_anchor_phrase(
    phrase: str,
    profile: QuestionProfile,
    token_df: dict[str, int],
) -> bool:
    named_lower = [name.lower() for name in profile.named_entities]
    tokens = tokenize_query(phrase)
    return len(tokens) >= 2 and all(
        _should_keep_corpus_anchor_token(token, profile, named_lower, token_df)
        or token in profile.expansion_tokens
        for token in tokens
    )


def select_scoped_candidate_files(
    question: str,
    profile: QuestionProfile,
    files: list[RetrievedMemoryFile],
    *,
    knowledge_root: str | Path | None = None,
) -> list[CandidateFile]:
    candidates: list[CandidateFile] = []
    normalized_query = question.strip().lower()
    query_tokens = _memory_header_tokens(normalized_query)
    header_files = [_rust_header_view(file, knowledge_root) for file in files]
    for relative_dir, budget, boost in scoped_budgets(profile):
        scoped_files = [
            file
            for file in header_files
            if _relative_memory_path(file.file_path, knowledge_root).startswith(
                f"{relative_dir}/"
            )
        ]
        projection_limit = None if relative_dir in {"wiki/observations", "wiki/turns"} else 200
        projected = (
            sorted(scoped_files, key=lambda file: (-file.mtime_ms, file.filename))
            if projection_limit is None
            else sorted(scoped_files, key=lambda file: (-file.mtime_ms, file.filename))[
                :projection_limit
            ]
        )
        scored = [
            (score, file)
            for file in projected
            if (
                score := _score_memory_header(normalized_query, query_tokens, file)
            )
            > 0.0
        ]
        selected = _select_confident_root_files(
            scored,
            max(budget, 2),
            4.0,
        )
        if not selected:
            # memdir falls back to body scoring when no header reaches the
            # confidence threshold.  Keep the same projected header set and
            # 4.5 minimum used by Rust's fallback selector.
            body_scored = [
                (score, file)
                for file in projected
                if (
                    score := _score_memory_body(normalized_query, query_tokens, file)
                )
                > 0.0
            ]
            selected = _select_confident_root_files(
                body_scored,
                max(budget, 2),
                4.5,
            )
        candidates.extend(
            CandidateFile(file=file, query_hits=1, seed_boost=boost)
            for file in selected
        )
    return _merge_candidate_files([], candidates)


def build_session_source_files(files: list[RetrievedMemoryFile]) -> list[SessionSourceFile]:
    sources = []
    for file in files:
        normalized_path = normalize_memory_path(file.file_path)
        if "/wiki/sources/" not in normalized_path:
            continue
        session_number = _parse_session_source_file_number(normalized_path)
        if session_number is None:
            continue
        search_text = _build_session_source_search_text(file.content)
        source_file = replace(
            file,
            description=file.description or f"session {session_number} summary and turn index",
        )
        sources.append(
            SessionSourceFile(
                session_number=session_number,
                file=source_file,
                search_text=search_text,
                search_tokens=tokenize_query(search_text),
                search_fuzzy_tokens=tokenize_fuzzy_query(search_text),
            )
        )
    return sources


def rank_global_session_sources(
    question: str,
    profile: QuestionProfile,
    sources: list[SessionSourceFile],
) -> list[SessionSourceFile]:
    phrases = significant_phrases(question)
    quoted = _exact_quoted_phrases(question)
    ngrams = keyword_ngrams(question)
    named_lower = [name.lower() for name in profile.named_entities]
    return sorted(
        sources,
        key=lambda source: (
            -_score_session_source(phrases, quoted, ngrams, named_lower, profile, source),
            -source.file.mtime_ms,
            source.file.file_path,
        ),
    )


def _compose_augmented_retrieval_query(
    question: str,
    augmentation: QueryAugmentation,
) -> str:
    """Match Rust's scoped-header query composition after corpus correction."""

    lower_question = question.lower()
    parts = [question.strip()]
    parts.extend(
        phrase for phrase in augmentation.phrases if phrase not in lower_question
    )
    parts.extend(
        token
        for token in augmentation.tokens
        if not re.search(rf"\b{re.escape(token)}\b", lower_question)
    )
    return " ".join(part for part in parts if part)


def source_injection_budget(profile: QuestionProfile) -> int:
    if profile.aggregate:
        return 8
    if profile.temporal or profile.identity or profile.hypothetical or profile.relational:
        return 4
    return 3


def source_companion_budget(
    profile: QuestionProfile,
    *,
    has_plugin_retrieval: bool,
    top_k: int,
) -> int:
    if not has_plugin_retrieval:
        budget = 2
    elif (
        profile.aggregate
        or profile.identity
        or profile.hypothetical
        or profile.relational
        or profile.location
    ):
        budget = 4
    else:
        budget = 3
    return min(budget, max(top_k, 1))


def select_diverse_session_sources(
    profile: QuestionProfile,
    root_files: list[RetrievedMemoryFile],
    ranked_sources: list[SessionSourceFile],
    budget: int,
) -> list[SessionSourceFile]:
    if budget == 0 or not ranked_sources:
        return []
    if (
        profile.temporal
        or profile.location
        or (
            not profile.hypothetical
            and not profile.identity
            and not profile.relational
            and not profile.aggregate
        )
    ):
        return ranked_sources[:budget]

    root_contexts = [
        (
            infer_session_number_from_path(file.file_path),
            tokenize_query(f"{file.description or ''} {file.content}"),
        )
        for file in root_files
    ]
    max_rank = float(max(len(ranked_sources), 1))
    selected = [ranked_sources[0]]
    selected_indices = {0}
    selected_sessions = {ranked_sources[0].session_number}

    while len(selected) < budget and len(selected) < len(ranked_sources):
        best: tuple[float, int, SessionSourceFile] | None = None
        for index, source in enumerate(ranked_sources):
            if index in selected_indices:
                continue
            rank_score = 3.0 - float(index) * (1.4 / max_rank)
            query_alignment = _source_query_alignment_score(profile, source)
            root_redundancy = max(
                (
                    _source_redundancy_penalty(
                        source,
                        session_number,
                        tokens,
                        source.session_number,
                    )
                    for session_number, tokens in root_contexts
                ),
                default=0.0,
            )
            selected_redundancy = max(
                (
                    _source_redundancy_penalty(
                        source,
                        chosen.session_number,
                        chosen.search_tokens,
                        chosen.session_number,
                    )
                    for chosen in selected
                ),
                default=0.0,
            )
            repeated_session_penalty = (
                1.2 if source.session_number in selected_sessions else 0.0
            )
            anchor_miss_penalty = _source_semantic_anchor_miss_penalty(
                profile,
                source.search_text,
            )
            score = (
                rank_score
                + query_alignment
                - root_redundancy
                - selected_redundancy
                - repeated_session_penalty
                - anchor_miss_penalty
            )
            # Rust's comparator prefers the later ranked index on an exact
            # diversity-score tie (`right.index.cmp(left.index)`).
            key = (score, index, source)
            if best is None or key[:2] > best[:2]:
                best = key
        if best is None:
            break
        _, index, source = best
        selected_indices.add(index)
        selected_sessions.add(source.session_number)
        selected.append(source)
    return selected


def collect_session_source_companions(
    question: str,
    profile: QuestionProfile,
    candidates: list[CandidateFile],
    sources_by_session: dict[int, SessionSourceFile],
) -> list[CandidateFile]:
    phrases = significant_phrases(question)
    quoted = _exact_quoted_phrases(question)
    ngrams = keyword_ngrams(question)
    named_lower = [name.lower() for name in profile.named_entities]
    companions: dict[str, CandidateFile] = {}
    source_bonus_by_session: dict[int, float] = {}

    for candidate in candidates:
        for session_number, query_hits, seed_boost in _infer_session_source_signals(
            candidate,
            profile,
            phrases,
            named_lower,
            quoted,
            ngrams,
            sources_by_session,
        ):
            source = sources_by_session.get(session_number)
            if source is None:
                continue
            source_bonus = source_bonus_by_session.setdefault(
                session_number,
                _source_companion_relevance_bonus(
                    source,
                    profile,
                    phrases,
                    quoted,
                    ngrams,
                    named_lower,
                ),
            )
            key = normalize_memory_path(source.file.file_path)
            proposal = CandidateFile(
                file=source.file,
                query_hits=max(query_hits, int(source_bonus > 0.0)),
                seed_boost=seed_boost + source_bonus,
            )
            existing = companions.get(key)
            if existing is None or (proposal.seed_boost, proposal.query_hits) > (
                existing.seed_boost,
                existing.query_hits,
            ):
                companions[key] = proposal

    return sorted(
        companions.values(),
        key=lambda candidate: (
            -candidate.seed_boost,
            -candidate.query_hits,
            normalize_memory_path(candidate.file.file_path),
        ),
    )


def infer_session_number_from_path(path: str) -> int | None:
    normalized = path.replace("\\", "/")
    for prefix in ("session_", "/D", "\\D"):
        index = normalized.find(prefix)
        if index < 0:
            continue
        suffix = normalized[index + len(prefix) :]
        digits = re.match(r"\d+", suffix)
        if digits:
            return int(digits.group(0))
    return None


def _is_rust_cached_retrieval_file(file: RetrievedMemoryFile) -> bool:
    """Match the Rust retained evaluator's cached retrieval file projection."""

    path = normalize_memory_path(file.file_path)
    return any(
        path.startswith(marker) or f"/{marker}" in path
        for marker in (
            "wiki/sources/",
            "wiki/observations/",
            "wiki/events/",
            "wiki/entities/",
            "wiki/turns/",
            "wiki/memories/",
            "wiki/memory/",
        )
    )


def _rank_initial_root_files(
    question: str,
    files: list[RetrievedMemoryFile],
    limit: int,
    knowledge_root: str | Path | None = None,
) -> list[RetrievedMemoryFile]:
    # Rust builds a bounded root-header projection before selecting the initial
    # files.  Keep late entity/observation pages in the scoped candidate pool;
    # otherwise they can displace the source/turn roots before source rescue.
    # `scan_memory_directory_with_limit` only indexes markdown topic files;
    # MEMORY.md is an entrypoint and raw JSON is never part of Rust's header
    # projection.  Excluding them here prevents Python-only root candidates.
    header_files = [
        _rust_header_view(file, knowledge_root)
        for file in files
        if _is_rust_scanned_markdown(file, knowledge_root)
    ]
    # Rust orders the bounded projection by descending mtime and then relative
    # filename.  The Python builder stores deterministic insertion timestamps;
    # retain the same comparator rather than letting absolute paths or the
    # basename-only `filename` field change the projection.
    projected_files = sorted(
        header_files,
        key=lambda file: (-file.mtime_ms, file.filename),
    )[:200]
    normalized_query = question.strip().lower()
    query_tokens = _memory_header_tokens(normalized_query)
    scored = [
        (score, file)
        for file in projected_files
        if (score := _score_memory_header(normalized_query, query_tokens, file)) > 0.0
    ]
    selected = _select_confident_root_files(scored, limit, 4.0)
    if selected:
        return selected
    # memdir retries with body scoring when header selection is empty.
    body_scored = [
        (score, file)
        for file in projected_files
        if (score := _score_memory_body(normalized_query, query_tokens, file)) > 0.0
    ]
    return _select_confident_root_files(body_scored, limit, 4.5)


def _relative_memory_path(
    file_path: str,
    knowledge_root: str | Path | None,
) -> str:
    normalized = normalize_memory_path(file_path).rstrip("/")
    if knowledge_root is None:
        return normalized
    root = normalize_memory_path(str(knowledge_root)).rstrip("/")
    prefix = f"{root}/"
    return normalized[len(prefix) :] if normalized.startswith(prefix) else normalized


def _rust_header_view(
    file: RetrievedMemoryFile,
    knowledge_root: str | Path | None,
) -> RetrievedMemoryFile:
    relative_path = _relative_memory_path(file.file_path, knowledge_root)
    return replace(
        file,
        filename=relative_path,
        description=_frontmatter_value(file.content, "description") or file.description or None,
    )


def _score_memory_header(
    normalized_query: str,
    query_tokens: set[str],
    file: RetrievedMemoryFile,
) -> float:
    filename = file.filename.lower()
    description = (file.description or _frontmatter_value(file.content, "description")).lower()
    type_label = _frontmatter_value(file.content, "type") or "general"
    score = 0.0
    if normalized_query in filename:
        score += 4.0
    if normalized_query in description:
        score += 3.5
    if filename in normalized_query:
        score += 1.5
    if description in normalized_query:
        score += 1.0
    score += len(query_tokens & _memory_header_tokens(filename)) * 2.5
    score += len(query_tokens & _memory_header_tokens(description)) * 2.0
    score += len(query_tokens & _memory_header_tokens(type_label))
    if _contains_reference_warning_signal(f"{filename} {description}"):
        score += 2.5
    return score


def _score_memory_body(
    normalized_query: str,
    query_tokens: set[str],
    file: RetrievedMemoryFile,
) -> float:
    normalized_content = file.content.lower()
    score = _score_memory_header(normalized_query, query_tokens, file)
    if normalized_query in normalized_content:
        score += 4.0
    if normalized_content in normalized_query:
        score += 1.0
    score += len(query_tokens & _memory_header_tokens(normalized_content)) * 1.5
    return score


def _select_confident_root_files(
    scored: list[tuple[float, RetrievedMemoryFile]],
    limit: int,
    minimum: float,
    *,
    prefer_recent: bool = True,
) -> list[RetrievedMemoryFile]:
    scored.sort(
        key=lambda item: (
            -item[0],
            -item[1].mtime_ms if prefer_recent else 0,
            item[1].filename,
        )
    )
    if not scored or scored[0][0] < minimum:
        return []
    cutoff = max(minimum, scored[0][0] * 0.45)
    return [file for score, file in scored if score >= cutoff][:limit]


def _frontmatter_value(content: str, key: str) -> str:
    match = re.search(rf"(?im)^{re.escape(key)}:\s*(.+)$", content)
    return match.group(1).strip() if match else ""


def _is_rust_scanned_markdown(
    file: RetrievedMemoryFile,
    knowledge_root: str | Path | None,
) -> bool:
    """Match memdir's topic-file inclusion predicate for the virtual wiki."""

    relative = _relative_memory_path(file.file_path, knowledge_root).strip("/")
    if not relative.lower().endswith(".md"):
        return False
    if PurePosixPath(relative).name.lower() == "memory.md":
        return False
    parts = relative.split("/")
    if len(parts) == 4 and parts[0].lower() == "logs" and parts[3].lower().endswith(".md"):
        # Rust excludes daily logs from the topic scan.  The retained fixture
        # does not currently create these, but keeping the predicate exact
        # avoids a hidden Python-only root candidate.
        year, month, filename = parts[1], parts[2], parts[3]
        stem = filename[:-3]
        if (
            len(year) == 4
            and year.isdigit()
            and len(month) == 2
            and month.isdigit()
            and re.fullmatch(r"\d{4}-\d{2}-\d{2}", stem)
        ):
            return False
    return True


def _file_description(file: RetrievedMemoryFile) -> str:
    # Rust gives source companions their synthesized "summary and turn index"
    # description when loading them, while other cached files use parsed
    # frontmatter metadata. Preserve that distinction for candidate scoring.
    normalized_path = normalize_memory_path(file.file_path)
    if "/wiki/sources/" in normalized_path:
        return file.description or _frontmatter_value(file.content, "description")
    return _frontmatter_value(file.content, "description") or file.description


def _memory_header_tokens(text: str) -> set[str]:
    blocked = {
        "a",
        "an",
        "and",
        "at",
        "do",
        "for",
        "help",
        "how",
        "i",
        "if",
        "in",
        "is",
        "it",
        "me",
        "my",
        "of",
        "on",
        "or",
        "our",
        "please",
        "should",
        "that",
        "the",
        "this",
        "to",
        "use",
        "what",
        "when",
        "with",
    }
    tokens = set()
    for raw in re.split(r"[^0-9A-Za-z_-]+", text):
        token = raw.lower()
        if len(token) <= 1 or token in blocked:
            continue
        for suffix, replacement, minimum in (
            ("ies", "y", 4),
            ("ing", "", 5),
            ("ly", "", 4),
            ("ed", "", 4),
            ("es", "", 4),
            ("s", "", 3),
        ):
            if token.endswith(suffix) and len(token) > minimum:
                token = token[: -len(suffix)] + replacement
                break
        tokens.add(token)
    return tokens


def _contains_reference_warning_signal(text: str) -> bool:
    lowered = text.lower()
    return any(
        signal in lowered
        for signal in (
            "warning",
            "warn",
            "gotcha",
            "known issue",
            "pitfall",
            "danger",
            "avoid",
            "careful",
            "caution",
        )
    )


def _push_unique_file(
    output: list[RetrievedMemoryFile],
    included: set[str],
    file: RetrievedMemoryFile,
    limit: int,
) -> None:
    if len(output) >= limit:
        return
    key = normalize_memory_path(file.file_path)
    if key in included:
        return
    included.add(key)
    output.append(file)


def _merge_candidate_files(
    base: list[CandidateFile],
    additions: list[CandidateFile],
) -> list[CandidateFile]:
    merged: dict[str, CandidateFile] = {}
    order: list[str] = []
    for candidate in base + additions:
        key = normalize_memory_path(candidate.file.file_path)
        existing = merged.get(key)
        if existing is None:
            order.append(key)
            merged[key] = candidate
            continue
        selected_file = (
            candidate.file
            if candidate.file.mtime_ms > existing.file.mtime_ms
            else existing.file
        )
        merged[key] = CandidateFile(
            file=selected_file,
            query_hits=existing.query_hits + candidate.query_hits,
            seed_boost=max(existing.seed_boost, candidate.seed_boost),
        )
    return [merged[key] for key in order]


def _rerank_candidate_files(
    question: str,
    profile: QuestionProfile,
    candidates: list[CandidateFile],
    proposals,
    files_by_path: dict[str, RetrievedMemoryFile],
    *,
    knowledge_root: str | Path | None = None,
) -> list[CandidateFile]:
    by_path = {
        normalize_memory_path(candidate.file.file_path): candidate
        for candidate in candidates
    }
    for proposal in proposals:
        existing = by_path.get(proposal.file_path)
        if existing is not None:
            by_path[proposal.file_path] = CandidateFile(
                file=existing.file,
                query_hits=max(existing.query_hits, proposal.query_hits),
                seed_boost=existing.seed_boost + proposal.seed_boost,
            )
            continue
        file = files_by_path.get(proposal.file_path)
        if file is not None:
            by_path[proposal.file_path] = CandidateFile(
                file=file,
                query_hits=proposal.query_hits,
                seed_boost=proposal.seed_boost,
            )
    return _rank_candidate_files(
        question,
        profile,
        list(by_path.values()),
        knowledge_root=knowledge_root,
    )


def _rank_candidate_files(
    question: str,
    profile: QuestionProfile,
    candidates: list[CandidateFile],
    *,
    knowledge_root: str | Path | None = None,
) -> list[CandidateFile]:
    phrases = significant_phrases(question)
    named_lower = [name.lower() for name in profile.named_entities]

    def score(candidate: CandidateFile) -> float:
        relative_path = _relative_memory_path(candidate.file.file_path, knowledge_root)
        path_markers = f"/{relative_path}"
        meta_text = f"{relative_path} {_file_description(candidate.file)}".lower()
        meta_tokens = set(tokenize_query(meta_text))
        features = build_cached_file_lexical_features(candidate.file)
        value = (
            candidate.query_hits * 5.0
            + candidate.seed_boost
            + _token_overlap(profile.query_tokens, meta_tokens) * 3.0
            + _token_overlap(profile.expansion_tokens, meta_tokens) * 1.5
            + _token_overlap(profile.query_tokens, features.content_token_set)
            + _token_overlap(profile.expansion_tokens, features.content_token_set) * 0.7
            + max(
                (
                    _rust_candidate_line_score(line, profile, phrases)
                    for line in features.line_features
                ),
                default=0.0,
            )
        )
        for name in named_lower:
            if _word_boundary_contains(features.lower_content, name):
                value += 3.0
            if _word_boundary_contains(meta_text, name):
                value += 3.0
        if profile.temporal and any(
            part in path_markers
            for part in (
                "/wiki/observations/",
                "/wiki/events/",
                "/wiki/sources/",
                "/wiki/turns/",
                "/wiki/memory/",
            )
        ):
            value += 3.0
        if profile.identity and any(
            part in path_markers
            for part in ("/wiki/entities/", "/wiki/observations/", "/wiki/memory/")
        ):
            value += 4.0
        if (profile.hypothetical or profile.relational) and any(
            part in path_markers
            for part in ("/wiki/entities/", "/wiki/observations/", "/wiki/memory/")
        ):
            value += 2.0
        if profile.location and any(
            part in path_markers
            for part in ("/wiki/observations/", "/wiki/events/", "/wiki/memory/")
        ):
            value += 2.0
        if "/wiki/sources/" in path_markers:
            value += 4.0
            if profile.temporal or profile.location:
                value += 3.0
            if profile.identity or profile.hypothetical or profile.relational:
                value += 2.0
        if "/wiki/turns/" in path_markers:
            value += 1.0
            if profile.temporal or profile.location:
                value += 1.0
            if profile.identity or profile.hypothetical or profile.relational:
                value += 1.0
        for phrase in phrases:
            if len(phrase) < 6:
                continue
            if phrase in features.lower_content:
                value += 4.0
            if phrase in meta_text:
                value += 3.0
        if features.has_session_marker:
            value += 1.0
        if features.has_evidence_marker:
            value += 4.0
        return value

    return sorted(
        candidates,
        key=lambda candidate: (
            -score(candidate),
            -candidate.file.mtime_ms,
            candidate.file.file_path,
        ),
    )


def _rust_candidate_line_score(
    line,
    profile: QuestionProfile,
    phrases: list[str],
) -> float:
    """Mirror retained_eval's candidate-ranking line scorer (not qmd plugin scoring)."""

    score = (
        _token_overlap(profile.query_tokens, line.token_set) * 2.0
        + _token_overlap(profile.query_fuzzy_tokens, line.fuzzy_token_set) * 1.5
        + _token_overlap(profile.expansion_tokens, line.token_set) * 1.4
        + _token_overlap(profile.expansion_fuzzy_tokens, line.fuzzy_token_set) * 0.8
    )
    score += sum(
        2.0
        for entity in profile.named_entities
        if _word_boundary_contains(line.lower_text, entity.lower())
    )
    score += sum(
        3.0 for phrase in phrases if len(phrase) >= 6 and phrase in line.lower_text
    )
    score += sum(
        1.8
        for phrase in profile.expansion_phrases
        if len(phrase) >= 6 and phrase in line.lower_text
    )
    return score


def _word_boundary_contains(text: str, value: str) -> bool:
    return re.search(rf"\b{re.escape(value)}\b", text) is not None


def _collect_late_bridge_seed_files(
    root_files: list[RetrievedMemoryFile],
    ranked_candidates: list[CandidateFile],
    ranked_source_companions: list[CandidateFile],
) -> list[RetrievedMemoryFile]:
    files: list[RetrievedMemoryFile] = []
    included: set[str] = set()
    for file in root_files[:4]:
        _push_unique_file(files, included, file, 12)
    for candidate in ranked_candidates[:4]:
        _push_unique_file(files, included, candidate.file, 12)
    for candidate in ranked_source_companions[:4]:
        _push_unique_file(files, included, candidate.file, 12)
    return files


def _score_scoped_candidate(
    file: RetrievedMemoryFile,
    profile: QuestionProfile,
    phrases: list[str],
    boost: float,
) -> tuple[CandidateFile, float]:
    features = build_cached_file_lexical_features(file)
    best_line = max(
        (
            _score_query_line_context(
                line.lower_text,
                profile,
                phrases,
                [name.lower() for name in profile.named_entities],
            )
            for line in features.line_features
        ),
        default=0.0,
    )
    query_hits = max(candidate_query_hits_with_features(features, profile), int(best_line > 0.0))
    if query_hits == 0 and best_line <= 0.0:
        return CandidateFile(file=file, query_hits=0, seed_boost=0.0), 0.0
    score = best_line + boost + query_hits * 0.35
    return CandidateFile(file=file, query_hits=query_hits, seed_boost=boost), score


def _parse_session_source_file_number(normalized_path: str) -> int | None:
    file_name = PurePosixPath(normalized_path).name
    match = re.fullmatch(r"session_(\d+)\.md", file_name)
    return int(match.group(1)) if match else None


def _build_session_source_search_text(content: str) -> str:
    summary = _extract_source_summary_snippet(content)
    turn_index = _extract_source_turn_index_snippet(content)
    return f"{summary} {turn_index}".lower()


def _extract_source_summary_snippet(content: str) -> str:
    section = _extract_section_after_heading(content, "## Summary")
    text = section if section is not None else content
    return text.replace("\r\n", "\n").replace("\n", " ").strip()[:700]


def _extract_source_turn_index_snippet(content: str) -> str:
    section = _extract_section_after_heading(content, "## Turn Index") or ""
    lines = [line.strip() for line in section.splitlines()]
    return " ".join(line for line in lines if line.startswith("- [turn "))[:2500]


def _extract_section_after_heading(content: str, heading: str) -> str | None:
    normalized = content.replace("\r\n", "\n")
    start = normalized.find(heading)
    if start < 0:
        return None
    tail = normalized[start + len(heading) :]
    next_heading = tail.find("\n## ")
    if next_heading >= 0:
        tail = tail[:next_heading]
    return tail.strip()


def _score_session_source(
    phrases: list[str],
    quoted: list[str],
    ngrams: list[str],
    named_lower: list[str],
    profile: QuestionProfile,
    source: SessionSourceFile,
) -> float:
    score = (
        _token_overlap(profile.query_tokens, source.search_tokens) * 4.0
        + _token_overlap(profile.query_fuzzy_tokens, source.search_fuzzy_tokens) * 1.7
        + _token_overlap(profile.expansion_tokens, source.search_tokens) * 1.2
        + _token_overlap(profile.expansion_fuzzy_tokens, source.search_fuzzy_tokens) * 0.8
    )
    score += sum(8.0 for phrase in phrases if len(phrase) >= 6 and phrase in source.search_text)
    score += sum(12.0 for phrase in quoted if len(phrase) >= 3 and phrase in source.search_text)
    score += sum(5.0 for ngram in ngrams if len(ngram) >= 5 and ngram in source.search_text)
    score += sum(
        6.0
        for phrase in profile.expansion_phrases
        if len(phrase) >= 6 and phrase in source.search_text
    )
    score += sum(5.0 for entity in named_lower if entity in source.search_text)
    if profile.temporal:
        score += 2.0
    if profile.aggregate:
        score += 3.0
    return score


def _source_query_alignment_score(profile: QuestionProfile, source: SessionSourceFile) -> float:
    score = (
        _token_overlap(profile.query_tokens, source.search_tokens) * 0.35
        + _token_overlap(profile.query_fuzzy_tokens, source.search_fuzzy_tokens) * 0.12
        + _token_overlap(profile.expansion_tokens, source.search_tokens) * 1.8
        + _token_overlap(profile.expansion_fuzzy_tokens, source.search_fuzzy_tokens) * 0.65
    )
    score += sum(
        2.0
        for phrase in profile.expansion_phrases
        if len(phrase) >= 6 and phrase in source.search_text
    )
    return score


def _source_semantic_anchor_miss_penalty(
    profile: QuestionProfile,
    search_text: str,
) -> float:
    if not profile.expansion_tokens and not profile.expansion_phrases:
        return 0.0
    search_tokens = tokenize_query(search_text)
    expansion_hits = _token_overlap(profile.expansion_tokens, search_tokens)
    phrase_hits = sum(
        1
        for phrase in profile.expansion_phrases
        if len(phrase) >= 6 and phrase in search_text
    )
    return 1.2 if expansion_hits == 0 and phrase_hits == 0 else 0.0


def _source_redundancy_penalty(
    source: SessionSourceFile,
    other_session_number: int | None,
    other_tokens: list[str],
    selected_session_number: int | None,
) -> float:
    shared = float(_token_overlap(source.search_tokens, other_tokens))
    normalized_overlap = shared / float(max(len(source.search_tokens), 6))
    session_penalty = 1.6 if other_session_number == selected_session_number else 0.0
    return min(shared, 4.0) * 0.35 + normalized_overlap * 1.3 + session_penalty


def _source_companion_relevance_bonus(
    source: SessionSourceFile,
    profile: QuestionProfile,
    phrases: list[str],
    quoted: list[str],
    ngrams: list[str],
    named_lower: list[str],
) -> float:
    return min(
        _score_session_source(phrases, quoted, ngrams, named_lower, profile, source) * 0.4,
        8.0,
    )


def _infer_session_source_signals(
    candidate: CandidateFile,
    profile: QuestionProfile,
    phrases: list[str],
    named_lower: list[str],
    quoted: list[str],
    ngrams: list[str],
    sources_by_session: dict[int, SessionSourceFile],
) -> list[tuple[int, int, float]]:
    session_number = infer_session_number_from_path(candidate.file.file_path)
    if session_number is not None:
        return [
            (
                session_number,
                max(candidate.query_hits, 1),
                6.0 + min(candidate.query_hits, 2) + min(candidate.seed_boost, 2.0),
            )
        ]
    if not _should_infer_sessions_from_content(candidate.file.file_path):
        return []

    scored = _score_content_derived_session_mentions(
        candidate.file.content,
        profile,
        phrases,
        named_lower,
    )
    if scored:
        base_seed = 5.5 + min(candidate.seed_boost, 2.0)
        ranked = []
        for mentioned_session, score in scored:
            source = sources_by_session.get(mentioned_session)
            source_bonus = (
                _source_companion_relevance_bonus(
                    source,
                    profile,
                    phrases,
                    quoted,
                    ngrams,
                    named_lower,
                )
                if source is not None
                else 0.0
            )
            ranked.append(
                (mentioned_session, score, source_bonus, base_seed + score + source_bonus)
            )
        ranked.sort(key=lambda item: (-item[3], item[0]))
        selected = [
            (session_number, int(score > 0.0 or source_bonus > 0.0), base_seed + score)
            for session_number, score, source_bonus, _ in ranked[:5]
        ]
        selected_sessions = {session_number for session_number, _, _ in selected}
        bonus_backfill = [
            item
            for item in ranked
            if item[2] >= 4.5 and item[0] not in selected_sessions
        ]
        if bonus_backfill:
            session_number, score, source_bonus, _ = max(
                bonus_backfill,
                key=lambda item: (item[2], -item[1], -item[0]),
            )
            selected.append(
                (session_number, int(score > 0.0 or source_bonus > 0.0), base_seed + score)
            )
        return selected

    return [
        (session_number, 0, 5.5 + min(candidate.seed_boost, 2.0))
        for session_number in sorted(_collect_session_numbers_from_text(candidate.file.content))[:6]
    ]


def _score_content_derived_session_mentions(
    text: str,
    profile: QuestionProfile,
    phrases: list[str],
    named_lower: list[str],
) -> list[tuple[int, float]]:
    best_scores: dict[int, float] = {}
    for line in text.splitlines():
        mentioned_sessions = _collect_session_numbers_from_text(line)
        if not mentioned_sessions:
            continue
        score = _score_query_line_context(line, profile, phrases, named_lower)
        for session_number in mentioned_sessions:
            best_scores[session_number] = max(best_scores.get(session_number, score), score)
    return sorted(best_scores.items(), key=lambda item: (-item[1], item[0]))


def _score_query_line_context(
    line: str,
    profile: QuestionProfile,
    phrases: list[str],
    named_lower: list[str],
) -> float:
    lower = line.lower()
    tokens = tokenize_query(line)
    fuzzy_tokens = tokenize_fuzzy_query(line)
    score = (
        _token_overlap(profile.query_tokens, tokens) * 2.0
        + _token_overlap(profile.query_fuzzy_tokens, fuzzy_tokens) * 1.5
        + _token_overlap(profile.expansion_tokens, tokens) * 1.4
        + _token_overlap(profile.expansion_fuzzy_tokens, fuzzy_tokens) * 0.8
    )
    score += sum(
        2.0
        for entity in named_lower
        if re.search(rf"(?<![0-9A-Za-z]){re.escape(entity)}(?![0-9A-Za-z])", lower)
    )
    score += sum(3.0 for phrase in phrases if len(phrase) >= 6 and phrase in lower)
    score += sum(
        1.8
        for phrase in profile.expansion_phrases
        if len(phrase) >= 6 and phrase in lower
    )
    return score


def _collect_session_numbers_from_text(text: str) -> set[int]:
    session_numbers = {int(match) for match in re.findall(r"session_(\d+)", text)}
    session_numbers.update(
        int(match)
        for match in re.findall(r"\bD(\d+)(?=[:_])", text)
    )
    return session_numbers


def _should_infer_sessions_from_content(path: str) -> bool:
    normalized = normalize_memory_path(path)
    return (
        normalized.endswith("/memory.md")
        or "/wiki/entities/" in normalized
        or "/wiki/memory/" in normalized
    )


def _exact_quoted_phrases(question: str) -> list[str]:
    return [match.strip().lower() for match in re.findall(r'"([^"]+)"', question) if match.strip()]


def _token_overlap(left: list[str], right: list[str]) -> int:
    right_set = set(right)
    return sum(1 for token in left if token in right_set)


def _bounded_edit_distance(left: str, right: str, max_distance: int) -> int | None:
    if abs(len(left) - len(right)) > max_distance:
        return None
    previous = list(range(len(right) + 1))
    for left_index, left_char in enumerate(left, start=1):
        current = [left_index]
        row_min = current[0]
        for right_index, right_char in enumerate(right, start=1):
            substitution_cost = 0 if left_char == right_char else 1
            value = min(
                previous[right_index] + 1,
                current[right_index - 1] + 1,
                previous[right_index - 1] + substitution_cost,
            )
            current.append(value)
            row_min = min(row_min, value)
        if row_min > max_distance:
            return None
        previous = current
    distance = previous[-1]
    return distance if distance <= max_distance else None
