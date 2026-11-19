"""Okapi BM25 term weighting used by lexical indexes and candidate fusion."""

import math


def inverse_document_frequency(documents: int, matching_documents: int) -> float:
    return math.log(1.0 + (documents - matching_documents + 0.5) / (matching_documents + 0.5))


def term_score(idf: float, frequency: int, length_penalty: float, k1: float) -> float:
    return idf * frequency * (k1 + 1.0) / (frequency + length_penalty)
