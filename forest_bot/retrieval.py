"""Geography-aware hybrid retrieval over ACTIVE document chunks.

Only chunks of active documents are indexed. Applicability is computed per passage (chunk) scope,
so region advice never reaches an unrelated mitten. Ranking = BM25 (+ embeddings when available).
"""

from __future__ import annotations

import logging
import math
from collections import Counter
from dataclasses import dataclass, field

import numpy as np

from . import geography as geo
from .embeddings import Embedder, from_bytes
from .storage import Chunk, Document, Store
from .textnorm import tokenize

log = logging.getLogger(__name__)


@dataclass
class QueryScope:
    """What the question is about.

    kind:
      "mittens" – one or more specific mittens (several = comparison)
      "regions" – one or more regions as a whole (their mittens' local advice shown as differences)
      "forest"  – the whole forest, including local differences
      "general" – no place given and none needed (e.g. what to do in a fire)
    """
    kind: str
    mittens: list[str] = field(default_factory=list)
    regions: list[str] = field(default_factory=list)
    comparison: bool = False

    def __post_init__(self):
        if self.kind == "mittens" and not self.mittens:
            raise ValueError("QueryScope(kind='mittens') needs mittens")
        if self.kind == "regions" and not self.regions:
            raise ValueError("QueryScope(kind='regions') needs regions")
        if any(not geo.is_mitten(m) for m in self.mittens) or any(not geo.is_region(r) for r in self.regions):
            raise ValueError(f"invalid location codes in {self.mittens} / {self.regions}")

    @property
    def targets(self) -> list[str | None]:
        if self.kind == "mittens":
            return list(self.mittens)
        if self.kind == "regions":
            return list(self.regions)
        if self.kind == "forest":
            # Whole-forest questions: look at every mitten so local differences are not missed.
            return list(geo.MITTENS)
        return [None]

    def describe(self) -> str:
        if self.kind == "mittens":
            parts = []
            for m in self.mittens:
                regs = geo.regions_of(m)
                where = ", ".join(geo.REGIONS[r] for r in geo.sort_codes(regs)) if regs else "не належить до жодної частини лісу"
                parts.append(f"рукавичка «{geo.MITTENS[m]}» ({where})")
            return "; ".join(parts)
        if self.kind == "regions":
            return "; ".join(f"частина лісу «{geo.REGIONS[r]}» (рукавички: "
                             f"{', '.join(geo.MITTENS[m] for m in geo.mittens_in_region(r))})" for r in self.regions)
        if self.kind == "forest":
            return "увесь ліс (усі рукавички)"
        return "місце не вказане"

    def to_state(self) -> dict:
        return {"kind": self.kind, "mittens": self.mittens, "regions": self.regions, "comparison": self.comparison}

    @classmethod
    def from_state(cls, data: dict | None) -> "QueryScope | None":
        if not data:
            return None
        try:
            return cls(data.get("kind", "general"), list(data.get("mittens", [])), list(data.get("regions", [])),
                       bool(data.get("comparison")))
        except ValueError:
            return None


@dataclass
class EvidencePassage:
    chunk: Chunk
    score: float


@dataclass
class EvidenceDoc:
    document: Document
    passages: list[EvidencePassage]
    levels: dict[str | None, int]          # target -> specificity level of this evidence for it
    score: float

    @property
    def text(self) -> str:
        return "\n".join(p.chunk.text for p in sorted(self.passages, key=lambda p: p.chunk.idx))

    @property
    def scope(self) -> list[str]:
        codes: set[str] = set()
        for p in self.passages:
            codes |= set(p.chunk.scope)
        return geo.sort_codes(codes)


@dataclass
class RetrievalResult:
    scope: QueryScope
    docs: list[EvidenceDoc]
    coverage: dict[str | None, set[int]]   # levels with any applicable active passage per target


def chunk_level(scope: list[str], target: str | None) -> int:
    if target is None:
        return geo.scope_level(scope)
    if target in geo.MITTENS:
        return geo.applicability(scope, target)
    return geo.applicability_for_region(scope, target)


class Retriever:
    def __init__(self, store: Store, embedder: Embedder | None = None, *, max_docs: int = 8,
                 per_target_docs: int = 4, evidence_chars: int = 7000):
        self.store = store
        self.embedder = embedder
        self.max_docs = max_docs
        self.per_target_docs = per_target_docs
        self.evidence_chars = evidence_chars
        self._revision = None
        self._chunks: list[Chunk] = []
        self._docs: dict[int, Document] = {}
        self._tf: list[Counter] = []
        self._df: Counter = Counter()
        self._avgdl = 1.0
        self._matrix: np.ndarray | None = None

    # ------------------------------------------------------------------ index
    def refresh(self, revision: int | None = None, force: bool = False) -> None:
        if not force and revision is not None and revision == self._revision:
            return
        self._chunks = self.store.active_chunks()
        self._docs = {d.id: d for d in self.store.active_documents()}
        self._chunks = [c for c in self._chunks if c.document_id in self._docs]
        self._tf = []
        self._df = Counter()
        for c in self._chunks:
            tokens = tokenize(f"{self._docs[c.document_id].title}\n{c.text}")
            tf = Counter(tokens)
            self._tf.append(tf)
            self._df.update(tf.keys())
        self._avgdl = (sum(sum(tf.values()) for tf in self._tf) / len(self._tf)) if self._tf else 1.0
        self._matrix = None
        if self.embedder is not None and self.embedder.available and self._chunks:
            vectors = []
            for c in self._chunks:
                if c.embedding and c.embedding_model == self.embedder.model_name:
                    vectors.append(from_bytes(c.embedding))
                else:
                    vectors = None
                    break
            if vectors is not None:
                self._matrix = np.vstack(vectors)
            else:
                log.info("Some chunks lack embeddings for %s; keyword search only until reindexed",
                         self.embedder.model_name)
        self._revision = revision
        log.info("Retrieval index refreshed: %d chunks from %d active documents (embeddings: %s)",
                 len(self._chunks), len(self._docs), self._matrix is not None)

    def _bm25(self, query_tokens: list[str]) -> np.ndarray:
        n = len(self._chunks)
        scores = np.zeros(n, dtype=np.float32)
        k1, b = 1.4, 0.75
        for term in set(query_tokens):
            df = self._df.get(term, 0)
            if not df:
                continue
            idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
            for i, tf in enumerate(self._tf):
                f = tf.get(term, 0)
                if f:
                    dl = sum(tf.values())
                    scores[i] += idf * f * (k1 + 1) / (f + k1 * (1 - b + b * dl / self._avgdl))
        return scores

    def scores(self, query: str) -> np.ndarray:
        query = geo.strip_locations(query) or query
        bm25 = self._bm25(tokenize(query))
        top = float(bm25.max()) if len(bm25) else 0.0
        bm25n = bm25 / top if top > 0 else bm25
        if self._matrix is not None and self.embedder is not None:
            try:
                q = self.embedder.embed_query(query)
                cos = np.clip(self._matrix @ q, 0, 1)
                return 0.55 * cos + 0.45 * bm25n
            except Exception as exc:
                log.warning("Query embedding failed: %s", exc)
        return bm25n

    # ------------------------------------------------------------------ retrieval
    def retrieve(self, query: str, qscope: QueryScope, revision: int | None = None) -> RetrievalResult:
        self.refresh(revision)
        targets = qscope.targets
        coverage: dict[str | None, set[int]] = {t: set() for t in targets}
        if not self._chunks:
            return RetrievalResult(qscope, [], coverage)
        scores = self.scores(query)

        multi = len(targets) > 1
        if qscope.kind == "forest":
            per_target_cap = 2
        else:
            per_target_cap = self.per_target_docs if multi else self.max_docs
        selected: dict[int, dict] = {}   # doc_id -> {"chunks": {chunk_idx: score}, "levels": {}, "score": float}

        for target in targets:
            levels = [chunk_level(c.scope, target) for c in self._chunks]
            for lv in levels:
                if lv:
                    coverage[target].add(lv)
            candidates = [i for i, lv in enumerate(levels) if lv > 0]
            if not candidates:
                continue
            top = max(float(scores[i]) for i in candidates)
            if top <= 0:
                continue
            # Cosine similarity gives every passage some score, so the hybrid bar is higher.
            hybrid = self._matrix is not None
            threshold = max((0.5 if hybrid else 0.3) * top, 0.05)
            # Local (mitten) advice has top priority, so its best passage gets a generous bar;
            # the answer prompt tells the model to ignore documents that do not fit the question.
            level_threshold = max((0.2 if hybrid else 0.12) * top, 0.03)
            by_score = sorted(candidates, key=lambda i: -scores[i])
            ranked = [i for i in by_score if scores[i] >= threshold]
            # Guarantee the best passage of each specificity level (mitten, region, forest) a place
            # (with a lower relevance bar), so specific local advice is not crowded out by broader
            # documents that merely share more keywords; then fill by relevance.
            picked_docs: list[int] = []
            order: list[int] = []
            for lv in (geo.LEVEL_MITTEN, geo.LEVEL_REGION, geo.LEVEL_FOREST):
                best = next((i for i in by_score if levels[i] == lv and scores[i] >= level_threshold), None)
                if best is not None:
                    order.append(best)
            order += [i for i in ranked if i not in order]
            for i in order:
                chunk = self._chunks[i]
                doc_id = chunk.document_id
                if doc_id not in picked_docs:
                    if len(picked_docs) >= per_target_cap:
                        continue
                    picked_docs.append(doc_id)
                entry = selected.setdefault(doc_id, {"chunks": {}, "levels": {}, "score": 0.0})
                entry["chunks"][i] = max(entry["chunks"].get(i, 0.0), float(scores[i]))
                entry["levels"][target] = max(entry["levels"].get(target, 0), levels[i])
                entry["score"] = max(entry["score"], float(scores[i]))

        # Short documents are shown whole (only their applicable passages), for full context.
        for doc_id, entry in selected.items():
            for i, c in enumerate(self._chunks):
                if c.document_id == doc_id and i not in entry["chunks"]:
                    if any(chunk_level(c.scope, t) > 0 for t in entry["levels"]):
                        entry["chunks"][i] = float(scores[i])

        docs = [
            EvidenceDoc(
                document=self._docs[doc_id],
                passages=[EvidencePassage(self._chunks[i], s) for i, s in entry["chunks"].items()],
                levels=entry["levels"],
                score=entry["score"],
            )
            for doc_id, entry in selected.items()
        ]
        docs.sort(key=lambda d: (-max(d.levels.values()), -d.score))
        docs = self._fit_budget(docs)
        log.info("Retrieved %d docs for %s: %s", len(docs), qscope.to_state(),
                 [(d.document.id, d.document.title, d.levels) for d in docs])
        return RetrievalResult(qscope, docs, coverage)

    def _fit_budget(self, docs: list[EvidenceDoc]) -> list[EvidenceDoc]:
        """Drop the least relevant documents until the evidence fits the prompt budget."""
        by_relevance = sorted(docs, key=lambda d: -d.score)
        total = sum(len(d.text) for d in docs)
        while total > self.evidence_chars and len(by_relevance) > 1:
            drop = by_relevance.pop()
            total -= len(drop.text)
            docs = [d for d in docs if d is not drop]
        return docs
