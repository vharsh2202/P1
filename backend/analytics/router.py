"""The semantic router.

A small local sentence-transformer maps a free-text question onto one of the
analysis templates in :mod:`analytics.analyses`. No LLM is involved and no
text is ever turned into SQL directly - the router only picks *what kind of
analysis* to run; the columns come from :mod:`analytics.profile`.

Three guards keep the router honest:

1. **Vocabulary guard** - obviously off-domain questions are rejected before
   the model is consulted.
2. **Negative class** - out-of-scope phrases live in the same embedding space
   as the analyses, so the model learns what "not about this data" sounds like.
3. **Similarity floor** - below the threshold the question is refused rather
   than guessed at.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .analyses import ANALYSES, Analysis

# --------------------------------------------------------------------------- #
# Out-of-scope vocabulary
# --------------------------------------------------------------------------- #

OFF_DOMAIN_TERMS = frozenset(
    {
        "weather", "forecast", "election", "president", "parliament", "senate",
        "recipe", "cook", "cooking", "ingredients", "poem", "poetry", "lyrics",
        "song", "joke", "pun", "horoscope", "zodiac", "lottery", "meme",
        "stock", "stocks", "crypto", "bitcoin", "nft", "gossip", "celebrity",
        "football", "soccer", "cricket", "nba", "fpl", "transfer", "transfermarkt",
        "flight", "hotel", "restaurant", "movie", "film", "album", "tv",
        "doctor", "symptom", "diagnosis", "medicine", "dosage",
        "lawyer", "lawsuit", "statute", "verdict", "sentence",
        "translate", "translation", "grammar", "spellcheck",
        "password", "hack", "exploit", "malware", "phishing",
        "resume", "cover letter", "interview", "salary negotiation",
        "homework", "essay", "thesis", "citation",
        "html", "css", "javascript", "python", "rust", "regex", "stacktrace",
        "docker", "kubernetes", "nginx", "terraform", "compile",
        "joke", "pun", "riddle",
    }
)

OUT_OF_SCOPE_PHRASES: Tuple[str, ...] = (
    "what is the weather today",
    "who won the football match",
    "tell me a joke",
    "write me a poem",
    "what is for dinner",
    "how do i boil an egg",
    "help me write a cover letter",
    "what is the capital of france",
    "translate this to spanish",
    "who should i vote for",
    "explain quantum physics",
    "what is the meaning of life",
    "give me medical advice",
    "how do i hack into an account",
    "what time is it",
    "who am i",
    "tell me a story",
    "what is two plus two",
    "recommend a restaurant nearby",
    "what is the stock price of apple",
)

# --------------------------------------------------------------------------- #
# Lexical signals
# --------------------------------------------------------------------------- #
#
# The embedding model is good at gist and bad at structure: "revenue by region"
# and "revenue by region and product" land almost on top of each other. These
# keywords carry the *shape* of a question - the verb pattern - which the
# embeddings blur. Adding them as a small bonus makes routing both sharper
# and more predictable.

LEXICAL_SIGNALS: Dict[str, Tuple[str, ...]] = {
    "overview": (
        "summary", "summarise", "summarize", "overview", "headline",
        "big picture", "at a glance", "overall", "total", "totals",
        "average", "mean", "median", "maximum", "minimum", "how much",
        "how many",
    ),
    "breakdown": (
        "break down", "breakdown", "split", "split by", "grouped by",
        "group by",
    ),
    "trend": (
        "over time", "trend", "trending", "time series", "month by month",
        "year by year", "monthly", "yearly", "weekly", "daily", "quarterly",
        "growth", "changing", "changed", "history", "historical",
    ),
    "ranking": (
        "top", "best", "worst", "biggest", "largest", "smallest",
        "highest", "lowest", "leading", "rank", "ranked",
    ),
    "distribution": (
        "distribution", "histogram", "spread", "bucket", "buckets",
    ),
    "comparison": (
        "compare", "comparison", "cross tab", "crosstab", "cross-tab",
        "side by side", "versus", "vs",
    ),
    "correlation": (
        "correlate", "correlated", "correlation", "relationship",
        "related", "move together", "driven by", "tracks",
    ),
    "preview": (
        "columns", "schema", "fields", "sample", "preview", "what is in",
        "what's in", "what does it contain", "describe", "shape of",
        "show me the data",
    ),
}

# Weaker hints. Useful on their own, but not enough to swing a decision.
LEXICAL_WEAK_SIGNALS: Dict[str, Tuple[str, ...]] = {
    "overview": ("in total", "key numbers", "highlights", "key figures"),
    "breakdown": ("per", "each", "for each", "by each", "across", "break out"),
    "trend": ("by month", "by year", "by day", "by week", "over the months"),
    "ranking": ("most", "least", "who are"),
    "distribution": ("how varied", "how wide", "distribution of"),
    "correlation": ("track",),
    "preview": ("column", "field", "list the"),
}

# How much a lexical hit is worth. Strong hints count for more, but the bonus
# is capped so it can never outrank a clear semantic match on its own.
LEXICAL_WEIGHT = 0.15
LEXICAL_WEAK_WEIGHT = 0.07
LEXICAL_CAP = 0.26

# Structure bonuses sit *outside* the cap. These encode grammar the embedding
# model cannot see: whether a question groups by one column or cross-tabulates
# two. They are high precision, so they are allowed to be decisive.
BREAKDOWN_STRUCTURAL = 0.30
COMPARISON_STRUCTURAL = 0.30
CORRELATION_STRUCTURAL = 0.28

# Words that only ever appear when the question is about two measures moving
# together. Finding one is enough to call it a correlation.
CORRELATION_WORDS = (
    "relationship",
    "correlate",
    "correlated",
    "correlation",
    "related",
    "associated",
    "association",
    "move together",
    "moves together",
    "driven by",
    "tracks",
    "track",
)

# A question that names two things is never a single-number summary, and a
# grouping question is never a single-series histogram.
OVERVIEW_GROUP_PENALTY = 0.26
DISTRIBUTION_GROUP_PENALTY = 0.20
# The two grouping shapes are mutually exclusive, so whichever the grammar
# picks, the other loses ground.
BREAKDOWN_ELSEWHERE_PENALTY = 0.22
COMPARISON_ELSEWHERE_PENALTY = 0.20
# Signals have to match a whole word, so "top" must not fire on "topic".
WORD_SIGNALS = frozenset({"top", "most", "least", "per", "each", "vs", "versus", "track"})


def _merge(base: Dict[str, float], extra: Dict[str, float]) -> Dict[str, float]:
    """Add two boost maps together, key by key."""
    merged = dict(base)
    for name, value in extra.items():
        merged[name] = merged.get(name, 0.0) + value
    return merged


def _matches(signal: str, padded: str) -> bool:
    """Whole-word match, so "top" does not fire on "topic"."""
    return f" {signal} " in padded


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #


@dataclass
class RouteResult:
    status: str  # matched | fallback | ambiguous
    analysis: Optional[str] = None
    score: float = 0.0
    candidates: List[Dict[str, Any]] = field(default_factory=list)
    reason: Optional[str] = None


# --------------------------------------------------------------------------- #
# Router
# --------------------------------------------------------------------------- #


class SemanticRouter:
    """Maps a question onto an analysis template using local embeddings."""

    def __init__(
        self,
        model: Any,
        analyses: Sequence[Analysis] = ANALYSES,
        threshold: float = 0.42,
        ambiguity_margin: float = 0.05,
        scope_margin: float = 0.03,
        scope_phrases: Sequence[str] = OUT_OF_SCOPE_PHRASES,
        off_domain_terms: frozenset = OFF_DOMAIN_TERMS,
    ) -> None:
        self._model = model
        self._analyses = list(analyses)
        self._threshold = threshold
        self._ambiguity_margin = ambiguity_margin
        self._scope_margin = scope_margin
        self._scope_phrases = list(scope_phrases)
        self._off_domain_terms = off_domain_terms
        self._matrix: Any = None
        self._owners: List[str] = []
        self._scope_matrix: Any = None
        self._cache: Dict[str, RouteResult] = {}
        self._cache_lock = threading.Lock()

    # -- index ------------------------------------------------------------ #

    def build_index(self) -> None:
        import numpy as np

        phrases: List[str] = []
        owners: List[str] = []
        for analysis in self._analyses:
            for phrase in analysis.examples:
                phrases.append(phrase)
                owners.append(analysis.name)

        vectors = self._model.encode(
            phrases,
            normalize_embeddings=True,
            show_progress_bar=False,
            convert_to_numpy=True,
        )
        self._matrix = np.asarray(vectors, dtype=np.float32)
        self._owners = owners

        scope_vectors = self._model.encode(
            self._scope_phrases,
            normalize_embeddings=True,
            show_progress_bar=False,
            convert_to_numpy=True,
        )
        self._scope_matrix = np.asarray(scope_vectors, dtype=np.float32)

    # -- routing ---------------------------------------------------------- #

    def route(self, question: str) -> RouteResult:
        key = " ".join(str(question or "").lower().split())
        with self._cache_lock:
            cached = self._cache.get(key)
        if cached is not None:
            return cached

        result = self._route_uncached(question)
        with self._cache_lock:
            if len(self._cache) > 512:
                self._cache.clear()
            self._cache[key] = result
        return result

    def _route_uncached(self, question: str) -> RouteResult:
        import numpy as np

        text = str(question or "").strip()
        if not text:
            return RouteResult(status="fallback", reason="empty")

        lowered = text.lower()

        # -- guard 1: off-domain vocabulary -------------------------------- #
        words = {w.strip(".,?!:;\"'()[]") for w in lowered.split()}
        hits = sorted(words & self._off_domain_terms)
        if hits:
            return RouteResult(
                status="fallback",
                reason="off_domain",
                candidates=[
                    {
                        "analysis": "out_of_scope",
                        "score": 1.0,
                        "matched_term": hits[0],
                    }
                ],
            )

        if self._matrix is None:
            return RouteResult(status="fallback", reason="no_index")

        vector = self._model.encode(
            [text],
            normalize_embeddings=True,
            show_progress_bar=False,
            convert_to_numpy=True,
        )[0]
        sims = self._matrix @ vector

        # Best example score per analysis.
        raw: Dict[str, float] = {}
        for owner, score in zip(self._owners, sims):
            value = float(score)
            if value > raw.get(owner, -1.0):
                raw[owner] = value

        if not raw:
            return RouteResult(status="fallback", reason="no_candidates")

        # -- hybrid score: embedding gist + lexical structure -------------- #
        boosts = self._lexical_boost(lowered)
        combined = {
            name: raw.get(name, 0.0) + boosts.get(name, 0.0) for name in raw
        }

        ranked = sorted(combined.items(), key=lambda kv: kv[1], reverse=True)
        candidates = [
            {"analysis": name, "score": round(score, 4)}
            for name, score in ranked[:4]
        ]

        top_name, top_score = ranked[0]

        # -- guard 2: negative class --------------------------------------- #
        # Compared against the *raw* embedding score so that a lexical bonus
        # can never make an off-domain question look like a good match.
        raw_top = max(raw.values())
        scope_score = float(np.max(self._scope_matrix @ vector))
        if scope_score >= raw_top - self._scope_margin:
            return RouteResult(
                status="fallback",
                reason="out_of_scope",
                score=round(top_score, 4),
                candidates=candidates,
            )

        # -- guard 3: similarity floor ------------------------------------- #
        if top_score < self._threshold:
            return RouteResult(
                status="fallback",
                reason="below_threshold",
                score=round(top_score, 4),
                candidates=candidates,
            )

        # -- ambiguity ----------------------------------------------------- #
        if len(ranked) > 1 and (ranked[0][1] - ranked[1][1]) < self._ambiguity_margin:
            return RouteResult(
                status="ambiguous",
                analysis=top_name,
                score=round(top_score, 4),
                candidates=candidates,
                reason="ambiguous",
            )

        return RouteResult(
            status="matched",
            analysis=top_name,
            score=round(top_score, 4),
            candidates=candidates,
        )

    # -- lexical layer ----------------------------------------------------- #

    def _lexical_boost(self, lowered: str) -> Dict[str, float]:
        """Bonus scores for the analyses whose vocabulary the question uses.

        The embedding model captures gist but blurs structure - it cannot tell
        "revenue by region" from "revenue by region and product". These
        keywords carry the shape of the question, so they get a small, capped
        bonus on top of the semantic score.
        """
        padded = f" {re.sub(r'[^a-z0-9%]+', ' ', lowered).strip()} "

        boosts: Dict[str, float] = {}
        for name, signals in LEXICAL_SIGNALS.items():
            hits = sum(1 for signal in signals if f" {signal} " in padded)
            if hits:
                boosts[name] = min(LEXICAL_CAP, LEXICAL_WEIGHT * hits)
        for name, signals in LEXICAL_WEAK_SIGNALS.items():
            hits = sum(1 for signal in signals if f" {signal} " in padded)
            if hits:
                boosts[name] = min(
                    LEXICAL_CAP, boosts.get(name, 0.0) + LEXICAL_WEAK_WEIGHT * hits
                )

        # Structural hints are uncapped: they encode grammar rather than
        # vocabulary, and grammar is what decides between the shapes that the
        # embedding model mixes up.
        boosts = _merge(boosts, self._structure(padded))
        return boosts

    @staticmethod
    def _structure(padded: str) -> Dict[str, float]:
        """Parse the question's shape: how it groups, compares and relates.

        Returns structural bonuses for the analyses whose *shape* the question
        matches, plus penalties for the shapes it rules out. These are not part
        of the lexical cap: grammar is what the embedding model is worst at, so
        these are allowed to be decisive.
        """
        hints: Dict[str, float] = {}

        grouping = re.findall(r"\b(?:by|per|across|for each|within)\b", padded)
        pairing = re.findall(r"\b(?:and|vs|versus|against|between)\b", padded)
        # "Break down x by y" does not appear next to a comparison word.
        separate = " versus " in padded or " vs " in padded

        # "compare a and b" - an explicit comparison word plus two named things.
        explicit = " and " in padded and any(
            f" {word} " in padded
            for word in ("compare", "comparison", "crosstab", "cross tab", "cross-tab")
        )
        # "how many x and y" / "show x and y by month": two things counted.
        two_measures = " and " in padded and (
            " how many " in padded or " how much " in padded
        )
        # "by x by y" - two grouping columns, i.e. a cross-tab.
        repeated_by = len(grouping) > 1

        if separate or explicit or two_measures or repeated_by:
            hints["comparison"] = COMPARISON_STRUCTURAL
        elif grouping:
            hints["breakdown"] = BREAKDOWN_STRUCTURAL
        elif pairing:
            # Two things named with no grouping word: still a pairing question.
            hints["comparison"] = COMPARISON_STRUCTURAL

        if any(f" {word} " in padded for word in CORRELATION_WORDS):
            hints["correlation"] = CORRELATION_STRUCTURAL
        else:
            # "x against y" is a plot of one measure versus another, which is
            # what correlation draws - but only when no relationship word is
            # present, since "compare x against y" is a cross-tab.
            if " against " in padded:
                hints["correlation"] = CORRELATION_STRUCTURAL * 0.8

        # The chosen shape excludes the others: a grouping question is not a
        # one-number summary, and a named pairing is not a histogram.
        if grouping or pairing:
            hints["overview"] = -OVERVIEW_GROUP_PENALTY
            hints["distribution"] = -DISTRIBUTION_GROUP_PENALTY
        if "comparison" in hints:
            hints["breakdown"] = -BREAKDOWN_ELSEWHERE_PENALTY
            hints["overview"] = -OVERVIEW_GROUP_PENALTY
            hints["distribution"] = -DISTRIBUTION_GROUP_PENALTY
        elif "breakdown" in hints:
            hints["comparison"] = -COMPARISON_ELSEWHERE_PENALTY

        return hints

    # -- introspection ---------------------------------------------------- #

    @property
    def threshold(self) -> float:
        return self._threshold

    def analyses(self) -> List[Dict[str, Any]]:
        return [a.to_dict() for a in self._analyses]
