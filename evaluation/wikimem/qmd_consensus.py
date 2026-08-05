"""Deterministic qmd_consensus retrieval helpers ported from wikimem."""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import PurePosixPath


_QUERY_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "at",
    "be",
    "by",
    "did",
    "do",
    "for",
    "how",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "the",
    "to",
    "was",
    "what",
    "when",
    "where",
    "which",
    "who",
    "why",
    "would",
}

_QMD_FOCUS_STOPWORDS = {
    "what",
    "when",
    "where",
    "which",
    "would",
    "could",
    "pursue",
    "should",
    "might",
    "likely",
    "still",
    "there",
    "their",
    "about",
    "after",
    "before",
    "around",
}

_EXPANSION_STOPWORDS = {
    "about",
    "after",
    "before",
    "could",
    "might",
    "still",
    "their",
    "there",
    "these",
    "those",
    "would",
}


@dataclass(frozen=True)
class RetrievedMemoryFile:
    filename: str
    file_path: str
    mtime_ms: int
    content: str
    description: str | None = None
    memory_type: str | None = None
    scope: str = "auto"


@dataclass(frozen=True)
class QuestionProfile:
    question: str
    query_tokens: list[str]
    query_fuzzy_tokens: list[str]
    expansion_tokens: list[str]
    expansion_fuzzy_tokens: list[str]
    expansion_phrases: list[str]
    named_entities: list[str]
    temporal: bool
    location: bool
    identity: bool
    hypothetical: bool
    relational: bool
    aggregate: bool


@dataclass(frozen=True)
class QueryAugmentation:
    tokens: list[str]
    fuzzy_tokens: list[str]
    phrases: list[str]


@dataclass(frozen=True)
class CachedFileLineLexicalFeatures:
    lower_text: str
    tokens: list[str]
    fuzzy_tokens: list[str]
    normalized_tokens: list[str]
    normalized_token_set: set[str]
    normalized_prefix3_set: set[str]
    token_set: set[str]
    fuzzy_token_set: set[str]


@dataclass(frozen=True)
class CachedFileLexicalFeatures:
    lower_content: str
    content_tokens: list[str]
    content_fuzzy_tokens: list[str]
    path_tokens: list[str]
    content_token_set: set[str]
    content_fuzzy_token_set: set[str]
    path_token_set: set[str]
    bridge_target_paths: list[str]
    has_session_marker: bool
    has_evidence_marker: bool
    line_features: list[CachedFileLineLexicalFeatures]


@dataclass(frozen=True)
class QmdConsensusFileMetrics:
    query_hits: int
    best_line_score: float
    support_density: float


@dataclass(frozen=True)
class CandidateProposal:
    file_path: str
    query_hits: int
    seed_boost: float


@dataclass(frozen=True)
class RerankProposal:
    file_path: str
    query_hits: int
    seed_boost: float


@dataclass(frozen=True)
class CandidateFile:
    file: RetrievedMemoryFile
    query_hits: int
    seed_boost: float


def build_question_profile(question: str, entity_names: list[str]) -> QuestionProfile:
    lower = question.lower()
    named_entities = [
        name for name in entity_names if _word_boundary_contains(lower, name.lower())
    ]
    return QuestionProfile(
        question=question,
        query_tokens=tokenize_query(question),
        query_fuzzy_tokens=tokenize_fuzzy_query(question),
        expansion_tokens=[],
        expansion_fuzzy_tokens=[],
        expansion_phrases=[],
        named_entities=named_entities,
        temporal=_contains_any_phrase(
            lower,
            ["when", "how long", "what year", "what month", "what day"],
        ),
        location=_contains_any_phrase(lower, ["where", "which park", "which place"]),
        identity=_contains_any_phrase(
            lower,
            ["identity", "who is", "relationship status", "member of", "ally"],
        ),
        hypothetical=_contains_any_phrase(
            lower,
            ["would", "likely", "plan", "planning", "pursue"],
        ),
        relational=_contains_any_phrase(
            lower,
            ["relationship", "friends", "family", "mentor", "support"],
        ),
        aggregate=_contains_any_phrase(
            lower,
            [
                "what activities",
                "what events",
                "what books",
                "what artists",
                "what subjects",
                "what items",
                "what are some",
                "in what ways",
                "what has",
                "what kind of art",
                "what musical",
            ],
        ),
    )


def qmd_consensus_is_conservative(profile: QuestionProfile) -> bool:
    return (
        profile.temporal
        and not profile.location
        and not profile.identity
        and not profile.hypothetical
        and not profile.relational
        and not profile.aggregate
    )


@lru_cache(maxsize=8192)
def build_cached_file_lexical_features(file: RetrievedMemoryFile) -> CachedFileLexicalFeatures:
    lower_content = file.content.lower()
    normalized_path = normalize_memory_path(file.file_path)
    line_features = [
        _build_line_features(line.strip())
        for line in file.content.splitlines()
        if line.strip()
    ]
    content_tokens = tokenize_query(file.content)
    content_fuzzy_tokens = tokenize_fuzzy_query(file.content)
    path_tokens = tokenize_query(normalized_path)
    return CachedFileLexicalFeatures(
        lower_content=lower_content,
        content_tokens=content_tokens,
        content_fuzzy_tokens=content_fuzzy_tokens,
        path_tokens=path_tokens,
        content_token_set=set(content_tokens),
        content_fuzzy_token_set=set(content_fuzzy_tokens),
        path_token_set=set(path_tokens),
        bridge_target_paths=_collect_bridge_target_paths(file.file_path, file.content),
        has_session_marker="- Session: D" in file.content,
        has_evidence_marker="- Evidence: D" in file.content,
        line_features=line_features,
    )


def build_qmd_consensus_augmentation(
    question: str,
    profile: QuestionProfile,
    root_files: list[RetrievedMemoryFile],
) -> QueryAugmentation:
    if qmd_consensus_is_conservative(profile):
        return QueryAugmentation(tokens=[], fuzzy_tokens=[], phrases=[])

    significant = significant_phrases(question)
    ngrams = keyword_ngrams(question)
    focused = _qmd_focus_profile(profile)
    token_scores: dict[str, float] = {}
    token_support: dict[str, set[str]] = {}
    phrase_scores: dict[str, float] = {}
    phrase_support: dict[str, set[str]] = {}

    for file in _preferred_seed_files(root_files):
        normalized_path = normalize_memory_path(file.file_path)
        path_weight = _qmd_seed_path_weight(normalized_path)
        for line, line_score in _best_seed_lines(file.content, focused, question):
            if not _qmd_seed_line_has_anchor_overlap(line, focused):
                continue
            weighted_score = line_score * path_weight
            for token in tokenize_query(line):
                if _should_keep_expansion_token(
                    token,
                    focused.query_tokens,
                    focused.named_entities,
                    significant,
                    ngrams,
                ):
                    token_scores[token] = token_scores.get(token, 0.0) + weighted_score
                    token_support.setdefault(token, set()).add(normalized_path)
            for phrase in _expansion_phrases_from_line(line):
                if phrase in significant:
                    continue
                phrase_scores[phrase] = phrase_scores.get(phrase, 0.0) + weighted_score
                phrase_support.setdefault(phrase, set()).add(normalized_path)

    tokens = [
        token
        for token, _ in sorted(
            (
                (token, score)
                for token, score in token_scores.items()
                if len(token_support.get(token, set())) >= 2 or score >= 7.0
            ),
            key=lambda item: (
                -len(token_support.get(item[0], set())),
                -item[1],
                item[0],
            ),
        )[:6]
    ]
    phrases = [
        phrase
        for phrase, _ in sorted(
            (
                (phrase, score)
                for phrase, score in phrase_scores.items()
                if (len(phrase_support.get(phrase, set())) >= 1 and score >= 6.0)
                or len(phrase_support.get(phrase, set())) >= 2
            ),
            key=lambda item: (
                -len(phrase_support.get(item[0], set())),
                -item[1],
                item[0],
            ),
        )[:4]
    ]
    return QueryAugmentation(
        tokens=tokens,
        fuzzy_tokens=tokenize_fuzzy_query(" ".join(tokens)),
        phrases=phrases,
    )


def apply_query_augmentation(
    base_profile: QuestionProfile,
    augmentation: QueryAugmentation,
) -> QuestionProfile:
    return replace(
        base_profile,
        expansion_tokens=_merge_unique(base_profile.expansion_tokens, augmentation.tokens),
        expansion_fuzzy_tokens=_merge_unique(
            base_profile.expansion_fuzzy_tokens,
            augmentation.fuzzy_tokens,
        ),
        expansion_phrases=_merge_unique(base_profile.expansion_phrases, augmentation.phrases),
    )


def build_qmd_consensus_candidate_proposals(
    question: str,
    profile: QuestionProfile,
    files: list[RetrievedMemoryFile],
) -> list[CandidateProposal]:
    if qmd_consensus_is_conservative(profile):
        return []

    cached_files, cached_features = _cache_files(files)
    focused = _qmd_focus_profile(profile)
    significant = significant_phrases(question)
    ngrams = keyword_ngrams(question)
    named_lower = [name.lower() for name in focused.named_entities]
    metrics_cache: dict[str, QmdConsensusFileMetrics] = {}
    source_view: list[tuple[str, int, float]] = []
    anchor_view: list[tuple[str, int, float]] = []

    for normalized_path, file in cached_files.items():
        kind = _qmd_candidate_view_kind(normalized_path)
        if kind is None:
            continue
        features = cached_features[normalized_path]
        metrics = _metrics_for(
            metrics_cache,
            normalized_path,
            file,
            features,
            focused,
            significant,
        )
        score = _score_cached_file_with_metrics(
            features,
            normalized_path,
            focused,
            significant,
            ngrams,
            named_lower,
            metrics,
        )
        if kind == "source":
            score += metrics.best_line_score * 0.65
            score += metrics.support_density * 0.3
            score += metrics.query_hits * 0.25
            if score >= 5.8:
                source_view.append((normalized_path, max(metrics.query_hits, 1), score))
        else:
            score += metrics.best_line_score * 0.2
            score += _candidate_path_weight(normalized_path) * 0.4
            score += metrics.query_hits * 0.15
            if score >= 6.2:
                anchor_view.append((normalized_path, max(metrics.query_hits, 1), score))

    _sort_qmd_ranked_items(source_view)
    _sort_qmd_ranked_items(anchor_view)
    linked_view = _build_qmd_linked_candidate_view(
        question,
        focused,
        cached_files,
        cached_features,
        source_view,
        anchor_view,
    )
    return [
        CandidateProposal(file_path=path, query_hits=query_hits, seed_boost=boost)
        for path, query_hits, boost in _fuse_ranked_views(
            [(2.0, source_view), (1.5, anchor_view), (1.0, linked_view)],
            12,
        )
    ]


def build_qmd_consensus_rerank_proposals(
    question: str,
    profile: QuestionProfile,
    ranked_candidates: list[CandidateFile],
    files: list[RetrievedMemoryFile],
) -> list[RerankProposal]:
    if qmd_consensus_is_conservative(profile):
        return []

    cached_files, cached_features = _cache_files(files)
    focused = _qmd_focus_profile(profile)
    significant = significant_phrases(question)
    metrics_cache: dict[str, QmdConsensusFileMetrics] = {}

    source_view = []
    anchor_view = []
    for candidate in ranked_candidates:
        normalized_path = normalize_memory_path(candidate.file.file_path)
        features = cached_features.get(normalized_path)
        if features is None:
            continue
        metrics = _metrics_for(
            metrics_cache,
            normalized_path,
            candidate.file,
            features,
            focused,
            significant,
        )
        item = (
            normalized_path,
            max(candidate.query_hits, 1),
            candidate.seed_boost + metrics.best_line_score,
        )
        if "/wiki/sources/" in normalized_path:
            source_view.append(item)
        elif _qmd_candidate_view_kind(normalized_path) == "anchor":
            anchor_view.append(item)

    _sort_qmd_ranked_items(source_view)
    _sort_qmd_ranked_items(anchor_view)
    source_view = source_view[:6]
    anchor_view = anchor_view[:6]
    seed_files = [candidate.file for candidate in ranked_candidates[:6]]
    linked_view = [
        (proposal.file_path, max(proposal.query_hits, 1), proposal.seed_boost)
        for proposal in _build_bridge_proposals(
            question,
            focused,
            seed_files,
            cached_files,
            cached_features,
            8,
            4.2,
        )
    ]
    return [
        RerankProposal(file_path=path, query_hits=query_hits, seed_boost=boost)
        for path, query_hits, boost in _fuse_ranked_views(
            [(2.0, source_view), (1.5, anchor_view), (1.0, linked_view)],
            12,
        )
    ]


def build_qmd_consensus_late_bridge_proposals(
    question: str,
    profile: QuestionProfile,
    seed_files: list[RetrievedMemoryFile],
    files: list[RetrievedMemoryFile],
) -> list[RerankProposal]:
    if qmd_consensus_is_conservative(profile):
        return []

    cached_files, cached_features = _cache_files(files)
    focused = _qmd_focus_profile(profile)
    return _build_bridge_proposals(
        question,
        focused,
        seed_files,
        cached_files,
        cached_features,
        8,
        4.2,
    )


def score_line(text: str, profile: QuestionProfile, question: str) -> float:
    return _score_line_with_phrases(text, profile, significant_phrases(question))


def candidate_query_hits_with_features(
    file_features: CachedFileLexicalFeatures,
    profile: QuestionProfile,
) -> int:
    return _token_overlap_with_set(profile.query_tokens, file_features.content_token_set) + (
        _token_overlap_with_set(profile.query_fuzzy_tokens, file_features.content_fuzzy_token_set)
    )


def normalize_memory_path(path: str) -> str:
    return path.replace("\\", "/").lower()


def tokenize_query(text: str) -> list[str]:
    tokens = []
    for token in re.split(r"[^0-9A-Za-z]+", text):
        lowered = token.strip().lower()
        if len(lowered) <= 1 or lowered in _QUERY_STOPWORDS:
            continue
        tokens.append(lowered)
    return tokens


def tokenize_fuzzy_query(text: str) -> list[str]:
    features = []
    seen = set()
    for token in tokenize_query(text):
        for feature in _fuzzy_token_features(token):
            if len(feature) > 2 and feature not in seen:
                features.append(feature)
                seen.add(feature)
    return features


def significant_phrases(question: str) -> list[str]:
    return [phrase for phrase in keyword_ngrams(question) if len(phrase.split()) >= 2]


def keyword_ngrams(question: str) -> list[str]:
    tokens = tokenize_query(question.lower())
    seen = set()
    ngrams = []
    for size in (2, 3):
        for index in range(0, max(0, len(tokens) - size + 1)):
            phrase = " ".join(tokens[index : index + size])
            if phrase not in seen:
                ngrams.append(phrase)
                seen.add(phrase)
    return ngrams


def _build_line_features(line: str) -> CachedFileLineLexicalFeatures:
    tokens = tokenize_query(line)
    fuzzy_tokens = tokenize_fuzzy_query(line)
    normalized_tokens = [_strip_common_token_suffixes(token) for token in tokens]
    return CachedFileLineLexicalFeatures(
        lower_text=line.lower(),
        tokens=tokens,
        fuzzy_tokens=fuzzy_tokens,
        normalized_tokens=normalized_tokens,
        normalized_token_set=set(normalized_tokens),
        normalized_prefix3_set={token[:3] for token in normalized_tokens if len(token) >= 3},
        token_set=set(tokens),
        fuzzy_token_set=set(fuzzy_tokens),
    )


def _cache_files(
    files: list[RetrievedMemoryFile],
) -> tuple[dict[str, RetrievedMemoryFile], dict[str, CachedFileLexicalFeatures]]:
    cached_files = {normalize_memory_path(file.file_path): file for file in files}
    cached_features = {
        normalize_memory_path(file.file_path): build_cached_file_lexical_features(file)
        for file in files
    }
    return cached_files, cached_features


def _qmd_focus_profile(profile: QuestionProfile) -> QuestionProfile:
    focused_tokens = [
        token
        for token in profile.query_tokens
        if _qmd_keeps_focus_token(token, profile.named_entities)
    ]
    if not focused_tokens:
        return profile
    focused_expansion = [
        token
        for token in profile.expansion_tokens
        if _qmd_keeps_focus_token(token, profile.named_entities)
    ]
    return replace(
        profile,
        query_tokens=focused_tokens,
        query_fuzzy_tokens=tokenize_fuzzy_query(" ".join(focused_tokens)),
        expansion_tokens=focused_expansion,
        expansion_fuzzy_tokens=tokenize_fuzzy_query(" ".join(focused_expansion)),
    )


def _qmd_keeps_focus_token(token: str, named_entities: list[str]) -> bool:
    if len(token) < 4:
        return False
    if any(entity.lower() == token.lower() for entity in named_entities):
        return False
    return token not in _QMD_FOCUS_STOPWORDS


def _metrics_for(
    metrics_cache: dict[str, QmdConsensusFileMetrics],
    normalized_path: str,
    _file: RetrievedMemoryFile,
    features: CachedFileLexicalFeatures,
    profile: QuestionProfile,
    significant: list[str],
) -> QmdConsensusFileMetrics:
    if normalized_path not in metrics_cache:
        line_scores = [
            _score_line_features_with_phrases(line, profile, significant)
            for line in features.line_features
        ]
        best_line_score, support_density = _summarize_qmd_line_scores(line_scores)
        metrics_cache[normalized_path] = QmdConsensusFileMetrics(
            query_hits=candidate_query_hits_with_features(features, profile),
            best_line_score=best_line_score,
            support_density=support_density,
        )
    return metrics_cache[normalized_path]


def _score_cached_file_with_metrics(
    features: CachedFileLexicalFeatures,
    normalized_path: str,
    profile: QuestionProfile,
    significant: list[str],
    ngrams: list[str],
    named_entities_lower: list[str],
    metrics: QmdConsensusFileMetrics,
) -> float:
    score = metrics.best_line_score
    score += _candidate_path_weight(normalized_path) * 1.8
    score += _token_overlap_with_set(profile.query_tokens, features.path_token_set) * 1.8
    score += sum(2.3 for phrase in significant if phrase in features.lower_content)
    score += sum(1.2 for phrase in ngrams if phrase in features.lower_content)
    score += sum(1.5 for entity in named_entities_lower if entity in features.lower_content)
    if features.has_session_marker:
        score += 1.0
    if features.has_evidence_marker:
        score += 4.0
    return score


def _build_qmd_linked_candidate_view(
    question: str,
    profile: QuestionProfile,
    cached_files: dict[str, RetrievedMemoryFile],
    cached_features: dict[str, CachedFileLexicalFeatures],
    source_view: list[tuple[str, int, float]],
    anchor_view: list[tuple[str, int, float]],
) -> list[tuple[str, int, float]]:
    significant = significant_phrases(question)
    metrics_cache: dict[str, QmdConsensusFileMetrics] = {}
    direct_atomic: list[tuple[str, int, float]] = []
    for normalized_path, file in cached_files.items():
        if (
            "/wiki/turns/" not in normalized_path
            and "/wiki/memories/" not in normalized_path
            and "/wiki/memory/" not in normalized_path
        ):
            continue
        features = cached_features[normalized_path]
        metrics = _metrics_for(
            metrics_cache,
            normalized_path,
            file,
            features,
            profile,
            significant,
        )
        direct_score = (
            metrics.best_line_score * 1.15
            + metrics.support_density * 0.2
            + metrics.query_hits * 0.3
        )
        if direct_score >= 5.0:
            _push_qmd_ranked_top_k(
                direct_atomic,
                (normalized_path, max(metrics.query_hits, 1), direct_score),
                6,
            )

    seed_specs = [
        (path, hits, score, True) for path, hits, score in source_view[:8]
    ] + [(path, hits, score, False) for path, hits, score in anchor_view[:8]]
    seed_specs.sort(key=lambda item: (-item[2], -item[1], item[0]))
    proposals: dict[str, tuple[int, float]] = {}

    for seed_path, seed_query_hits, seed_score, seed_is_source in seed_specs:
        if seed_score < 6.0:
            continue
        seed = cached_files.get(seed_path)
        seed_features = cached_features.get(seed_path)
        if seed is None or seed_features is None:
            continue
        seed_session = _infer_session_number(seed.file_path)
        same_session: list[tuple[str, int, float]] = []
        cross_session: list[tuple[str, int, float]] = []
        for target in _extract_candidate_targets_with_features(seed_features, cached_files):
            normalized_target = normalize_memory_path(target.file_path)
            target_features = cached_features.get(normalized_target)
            if target_features is None:
                continue
            metrics = _metrics_for(
                metrics_cache,
                normalized_target,
                target,
                target_features,
                profile,
                significant,
            )
            target_score = metrics.best_line_score
            if target_score < 4.4:
                continue
            query_hits = max(metrics.query_hits, 1)
            proposal_score = (
                target_score * 0.9
                + min(seed_score, 10.0) * 0.22
                + _candidate_path_weight(normalized_target) * 0.3
                + seed_query_hits * 0.08
            )
            same_session_target = (
                seed_is_source
                and seed_session is not None
                and seed_session == _infer_session_number(target.file_path)
            )
            if same_session_target:
                if not _qmd_allows_same_session_source_target(
                    normalized_target,
                    target_score,
                    query_hits,
                ):
                    continue
                same_session.append((normalized_target, query_hits, proposal_score + 0.15))
            else:
                cross_session.append((normalized_target, query_hits, proposal_score))

        _sort_qmd_ranked_items(same_session)
        _sort_qmd_ranked_items(cross_session)
        target_limit = 2 if seed_is_source else 3
        for file_path, query_hits, seed_boost in same_session[:2] + cross_session[:target_limit]:
            existing = proposals.get(file_path)
            if existing is None:
                proposals[file_path] = (query_hits, seed_boost)
            else:
                proposals[file_path] = (max(existing[0], query_hits), max(existing[1], seed_boost))

    linked_view = [
        (file_path, query_hits, seed_boost)
        for file_path, (query_hits, seed_boost) in proposals.items()
    ]
    _sort_qmd_ranked_items(linked_view)
    linked_view = linked_view[:8]
    return _fuse_ranked_views([(1.2, direct_atomic), (1.0, linked_view)], 8)


def _build_bridge_proposals(
    question: str,
    profile: QuestionProfile,
    seed_files: list[RetrievedMemoryFile],
    cached_files: dict[str, RetrievedMemoryFile],
    cached_features: dict[str, CachedFileLexicalFeatures],
    max_candidates: int,
    min_target_score: float,
) -> list[RerankProposal]:
    significant = significant_phrases(question)
    proposals: dict[str, RerankProposal] = {}
    for seed in _preferred_seed_files(seed_files):
        normalized_seed = normalize_memory_path(seed.file_path)
        seed_weight = _candidate_path_weight(normalized_seed)
        seed_session = _infer_session_number(seed.file_path)
        seed_is_source = "/wiki/sources/" in normalized_seed
        seed_features = cached_features.get(normalized_seed)
        targets = (
            _extract_candidate_targets_with_features(seed_features, cached_files)
            if seed_features is not None
            else []
        )
        for target in targets:
            key = normalize_memory_path(target.file_path)
            if (
                seed_is_source
                and seed_session is not None
                and seed_session == _infer_session_number(target.file_path)
            ):
                continue
            target_features = cached_features.get(key)
            if target_features is None:
                continue
            score = _score_file_lines_with_features(target_features, profile, significant)
            if score < min_target_score:
                continue
            query_hits = max(candidate_query_hits_with_features(target_features, profile), 1)
            proposal = RerankProposal(
                file_path=key,
                query_hits=query_hits,
                seed_boost=score * 0.8 + seed_weight * 1.4,
            )
            existing = proposals.get(key)
            if existing is None or (proposal.seed_boost, proposal.query_hits) > (
                existing.seed_boost,
                existing.query_hits,
            ):
                proposals[key] = proposal

    return sorted(
        proposals.values(),
        key=lambda proposal: (-proposal.seed_boost, -proposal.query_hits, proposal.file_path),
    )[:max_candidates]


def _score_file_lines_with_features(
    features: CachedFileLexicalFeatures,
    profile: QuestionProfile,
    significant: list[str],
) -> float:
    if not features.line_features:
        return 0.0
    return max(
        _score_line_features_with_phrases(line, profile, significant)
        for line in features.line_features
    )


def _score_line_with_phrases(
    text: str,
    profile: QuestionProfile,
    significant: list[str],
) -> float:
    return _score_line_features_with_phrases(_build_line_features(text), profile, significant)


def _score_line_features_with_phrases(
    line: CachedFileLineLexicalFeatures,
    profile: QuestionProfile,
    significant: list[str],
) -> float:
    query_normalized = [_strip_common_token_suffixes(token) for token in profile.query_tokens]
    query_overlap = _token_overlap_with_set(profile.query_tokens, line.token_set)
    fuzzy_overlap = _token_overlap_with_set(profile.query_fuzzy_tokens, line.fuzzy_token_set)
    expansion_overlap = _token_overlap_with_set(profile.expansion_tokens, line.token_set)
    expansion_fuzzy = _token_overlap_with_set(profile.expansion_fuzzy_tokens, line.fuzzy_token_set)
    soft_possible = _query_soft_overlap_possible(query_normalized, line)
    if not (query_overlap or fuzzy_overlap or expansion_overlap or expansion_fuzzy or soft_possible):
        return 0.0
    score = (
        query_overlap * 2.0
        + fuzzy_overlap * 1.3
        + expansion_overlap * 1.2
        + expansion_fuzzy * 0.7
    )
    score += sum(2.4 for phrase in significant if len(phrase) >= 6 and phrase in line.lower_text)
    score += sum(
        1.6
        for phrase in profile.expansion_phrases
        if len(phrase) >= 6 and phrase in line.lower_text
    )
    score += sum(
        1.4 for entity in profile.named_entities if entity.lower() in line.lower_text
    )
    if soft_possible:
        score += _soft_token_overlap_with_line_fastpath(
            profile.query_tokens,
            query_normalized,
            line,
        ) * 0.8
    return score


def _summarize_qmd_line_scores(line_scores: list[float]) -> tuple[float, float]:
    best = 0.0
    top_scores = [0.0, 0.0, 0.0]
    for score in line_scores:
        best = max(best, score)
        if score < 2.4:
            continue
        candidate = score
        for index, slot in enumerate(top_scores):
            if candidate > slot:
                top_scores[index], candidate = candidate, slot
    density = top_scores[0] + top_scores[1] * 0.55 + top_scores[2] * 0.35
    return best, density


def _fuse_ranked_views(
    views: list[tuple[float, list[tuple[str, int, float]]]],
    limit: int,
) -> list[tuple[str, int, float]]:
    fused: dict[str, tuple[int, float, int, float]] = {}
    for weight, view in views:
        for rank, (file_path, query_hits, local_score) in enumerate(view, start=1):
            key = normalize_memory_path(file_path)
            existing = fused.get(key, (0, 0.0, 10**9, 0.0))
            fused[key] = (
                max(existing[0], query_hits),
                existing[1] + weight / (60.0 + rank),
                min(existing[2], rank),
                max(existing[3], local_score),
            )

    ranked = []
    for file_path, (query_hits, rrf_score, top_rank, local_score) in fused.items():
        if top_rank == 1:
            top_rank_bonus = 0.05
        elif top_rank <= 3:
            top_rank_bonus = 0.02
        else:
            top_rank_bonus = 0.0
        boost = rrf_score * 12.0 + top_rank_bonus * 8.0 + min(local_score, 12.0) * 0.25
        ranked.append((file_path, query_hits, boost))
    _sort_qmd_ranked_items(ranked)
    return ranked[:limit]


def _push_qmd_ranked_top_k(
    ranked: list[tuple[str, int, float]],
    candidate: tuple[str, int, float],
    limit: int,
) -> None:
    ranked.append(candidate)
    _sort_qmd_ranked_items(ranked)
    del ranked[limit:]


def _sort_qmd_ranked_items(ranked: list[tuple[str, int, float]]) -> None:
    ranked.sort(key=lambda item: (-item[2], -item[1], item[0]))


def _qmd_candidate_view_kind(path: str) -> str | None:
    if "/wiki/sources/" in path:
        return "source"
    if (
        "/wiki/entities/" in path
        or "/wiki/events/" in path
        or "/wiki/observations/" in path
        or "/wiki/memories/" in path
        or "/wiki/memory/" in path
    ):
        return "anchor"
    return None


def _qmd_allows_same_session_source_target(
    normalized_target: str,
    target_score: float,
    query_hits: int,
) -> bool:
    return (
        "/wiki/turns/" in normalized_target
        or "/wiki/observations/" in normalized_target
        or "/wiki/memory/" in normalized_target
    ) and ((query_hits >= 2 and target_score >= 5.2) or target_score >= 6.4)


def _preferred_seed_files(files: list[RetrievedMemoryFile]) -> list[RetrievedMemoryFile]:
    preferred = [
        file
        for file in files
        if any(
            part in normalize_memory_path(file.file_path)
            for part in (
                "/wiki/entities/",
                "/wiki/topics/",
                "/wiki/sources/",
                "/wiki/memories/",
                "/wiki/memory/",
            )
        )
    ]
    return (preferred or files)[:6]


def _extract_candidate_targets_with_features(
    features: CachedFileLexicalFeatures,
    cached_files: dict[str, RetrievedMemoryFile],
) -> list[RetrievedMemoryFile]:
    return [
        cached_files[path]
        for path in features.bridge_target_paths
        if path in cached_files
    ]


def _collect_bridge_target_paths(file_path: str, content: str) -> list[str]:
    if "](" not in content:
        return []
    targets = []
    seen = set()
    for target in re.findall(r"\]\(([^)]+)\)", content):
        resolved = _resolve_relative_target(file_path, target.strip())
        if resolved is None:
            continue
        key = normalize_memory_path(resolved)
        if _is_bridge_target_path(key) and key not in seen:
            targets.append(key)
            seen.add(key)
    return targets


def _resolve_relative_target(base_path: str, target: str) -> str | None:
    if target.startswith(("http://", "https://")):
        return None
    parent = str(PurePosixPath(base_path.replace("\\", "/")).parent)
    return posixpath.normpath(posixpath.join(parent, target))


def _is_bridge_target_path(path: str) -> bool:
    normalized = normalize_memory_path(path)
    return any(
        part in normalized
        for part in (
            "/wiki/observations/",
            "/wiki/turns/",
            "/wiki/events/",
            "/wiki/memory/",
        )
    )


def _infer_session_number(path: str) -> int | None:
    normalized = normalize_memory_path(path)
    for marker in ("/wiki/sources/session_", "/wiki/events/session_"):
        if marker in normalized:
            tail = normalized.split(marker, maxsplit=1)[1]
            digits = "".join(ch for ch in tail if ch.isdigit())
            return int(digits) if digits else None
    stem = PurePosixPath(normalized).stem
    if not stem.startswith("d"):
        return None
    digits = ""
    for ch in stem[1:]:
        if not ch.isdigit():
            break
        digits += ch
    return int(digits) if digits else None


def _candidate_path_weight(path: str) -> float:
    if "/wiki/observations/" in path:
        return 3.4
    if "/wiki/turns/" in path:
        return 3.0
    if "/wiki/events/" in path:
        return 2.7
    if "/wiki/topics/" in path:
        return 2.4
    if "/wiki/entities/" in path:
        return 2.2
    if "/wiki/memories/" in path:
        return 2.1
    if "/wiki/memory/" in path:
        return 2.5
    if "/wiki/sources/" in path:
        return 2.0
    return 0.5


def _qmd_seed_path_weight(path: str) -> float:
    if "/wiki/sources/" in path:
        return 1.35
    if "/wiki/entities/" in path:
        return 1.2
    if "/wiki/memories/" in path:
        return 1.15
    if "/wiki/memory/" in path:
        return 1.25
    if "/wiki/topics/" in path:
        return 1.1
    return 1.0


def _best_seed_lines(
    text: str,
    profile: QuestionProfile,
    question: str,
) -> list[tuple[str, float]]:
    lines = []
    for raw in text.splitlines():
        trimmed = raw.strip()
        if not trimmed or trimmed.startswith("#"):
            continue
        score = score_line(trimmed, profile, question)
        if score > 0.0:
            lines.append((trimmed, score))
    lines.sort(key=lambda item: (-item[1], item[0]))
    return lines[:4]


def _should_keep_expansion_token(
    token: str,
    query_tokens: list[str],
    named_entities: list[str],
    significant: list[str],
    ngrams: list[str],
) -> bool:
    if len(token) < 4 or token in query_tokens:
        return False
    if any(entity.lower() == token.lower() for entity in named_entities):
        return False
    stripped = _strip_common_token_suffixes(token)
    blocked_phrases = significant + ngrams
    return (
        token not in _EXPANSION_STOPWORDS
        and not any(phrase == token or phrase == stripped for phrase in blocked_phrases)
    )


def _expansion_phrases_from_line(line: str) -> list[str]:
    tokens = tokenize_query(line)
    return [
        f"{left} {right}"
        for left, right in zip(tokens, tokens[1:])
        if len(f"{left} {right}") >= 9
    ]


def _qmd_seed_line_has_anchor_overlap(line: str, profile: QuestionProfile) -> bool:
    line_tokens = tokenize_query(line)
    line_fuzzy = tokenize_fuzzy_query(line)
    return (
        _token_overlap(profile.query_tokens, line_tokens) > 0
        or _token_overlap(profile.query_fuzzy_tokens, line_fuzzy) > 0
        or _token_overlap(profile.expansion_tokens, line_tokens) > 0
        or _token_overlap(profile.expansion_fuzzy_tokens, line_fuzzy) > 0
        or any(
            len(phrase) >= 6 and phrase in line.lower()
            for phrase in profile.expansion_phrases
        )
    )


def _merge_unique(existing: list[str], additions: list[str]) -> list[str]:
    seen = set(existing)
    merged = list(existing)
    for item in additions:
        if item not in seen:
            merged.append(item)
            seen.add(item)
    return merged


def _token_overlap(left: list[str], right: list[str]) -> int:
    right_set = set(right)
    return sum(1 for token in left if token in right_set)


def _token_overlap_with_set(left: list[str], right: set[str]) -> int:
    return sum(1 for token in left if token in right)


def _query_soft_overlap_possible(
    query_normalized: list[str],
    line: CachedFileLineLexicalFeatures,
) -> bool:
    for token in query_normalized:
        if token in line.normalized_token_set:
            return True
        if len(token) >= 3 and token[:3] in line.normalized_prefix3_set:
            return True
    return False


def _soft_token_overlap_with_line_fastpath(
    left: list[str],
    left_normalized: list[str],
    line: CachedFileLineLexicalFeatures,
) -> float:
    total = 0.0
    for left_token, left_norm in zip(left, left_normalized):
        if left_token in line.token_set:
            best = 1.0
        elif len(left_norm) <= 3 and left_norm in line.normalized_token_set:
            best = 0.92
        elif not _qmd_line_may_have_soft_match_candidate(left_norm, line):
            best = 0.0
        else:
            best = max(
                (
                    _soft_token_similarity(left_token, left_norm, right_token, right_norm)
                    for right_token, right_norm in zip(line.tokens, line.normalized_tokens)
                ),
                default=0.0,
            )
        if best >= 0.72:
            total += best
    return total


def _qmd_line_may_have_soft_match_candidate(
    left_norm: str,
    line: CachedFileLineLexicalFeatures,
) -> bool:
    if not left_norm:
        return False
    if len(left_norm) <= 3:
        return left_norm in line.normalized_token_set
    return left_norm[:3] in line.normalized_prefix3_set


def _soft_token_similarity(
    left: str,
    left_norm: str,
    right: str,
    right_norm: str,
) -> float:
    if left == right:
        return 1.0
    if left_norm == right_norm:
        return 0.92
    if len(left_norm) >= 4 and len(right_norm) >= 4 and left_norm[:4] == right_norm[:4]:
        return 0.82
    prefix = 0
    for left_ch, right_ch in zip(left_norm, right_norm):
        if left_ch != right_ch:
            break
        prefix += 1
    return prefix / max(len(left_norm), len(right_norm))


def _fuzzy_token_features(token: str) -> list[str]:
    stripped = _strip_common_token_suffixes(token)
    features = set()
    for candidate in (token, stripped):
        if len(candidate) >= 4:
            features.add(candidate[:4])
        if len(candidate) >= 5:
            features.add(candidate[:5])
        skeleton = _consonant_skeleton(candidate)
        if len(skeleton) >= 4:
            features.add(skeleton)
    if stripped != token:
        features.add(stripped)
    return sorted(features)


def _strip_common_token_suffixes(token: str) -> str:
    suffixes = [
        ("ies", "y"),
        ("ions", ""),
        ("tion", ""),
        ("ing", ""),
        ("ment", ""),
        ("ness", ""),
        ("ity", ""),
        ("ed", ""),
        ("es", ""),
        ("s", ""),
    ]
    for suffix, replacement in suffixes:
        if len(token) > len(suffix) + 2 and token.endswith(suffix):
            return token[: -len(suffix)] + replacement
    return token


def _consonant_skeleton(token: str) -> str:
    if not token:
        return ""
    return token[0] + "".join(ch for ch in token[1:] if ch not in "aeiou")


def _contains_any_phrase(text: str, phrases: list[str]) -> bool:
    return any(phrase in text for phrase in phrases)


def _word_boundary_contains(text: str, needle: str) -> bool:
    return re.search(rf"(?<![0-9A-Za-z]){re.escape(needle)}(?![0-9A-Za-z])", text) is not None
