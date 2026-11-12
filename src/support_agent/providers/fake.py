"""Deterministic providers: no network, no API key, reproducible in CI.

These are not a quality baseline. They exist so the full pipeline (chunking →
embedding → vector search → prompt assembly → tool loop → citation check) can be
tested and evaluated without a vendor.

Embeddings use the hashing trick over word unigrams + bigrams, L2-normalised.
That makes cosine similarity behave like lexical overlap, so retrieval ranking
is *meaningful* — but it is still bag-of-words, not semantics.

The fake LLM answers extractively and always emits the JSON envelope the real
providers are asked for. It refuses when the context has no lexical overlap.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import re
from collections.abc import Sequence

from support_agent.core.config import get_settings
from support_agent.core.constants import REFUSAL_ANSWER, UNTRUSTED_DOC_RE
from support_agent.providers.base import ChatMessage, LLMResult, ToolCall, ToolSpec

_WORD_RE = re.compile(r"[a-z0-9][a-z0-9'_-]*")
_STOPWORDS = frozenset(
    (
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "been",
        "but",
        "by",
        "can",
        "could",
        "do",
        "does",
        "for",
        "from",
        "had",
        "has",
        "have",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "may",
        "me",
        "my",
        "no",
        "not",
        "of",
        "on",
        "or",
        "our",
        "should",
        "so",
        "than",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "to",
        "us",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
    )
)
# Sentence splitter: keep it dependency-free.
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+|\n+")
_WANTS_HUMAN = re.compile(
    r"\b(human|agent|person|someone|manager|representative|supervisor|speak to|talk to)\b",
    re.IGNORECASE,
)


def _tokens(text: str) -> list[str]:
    return [_stem(t) for t in _WORD_RE.findall(text.lower()) if len(t) > 1 and t not in _STOPWORDS]


def _stem(token: str) -> str:
    """Crude suffix stripping so 'refund'/'refunds'/'refunded' match.

    Deliberately tiny and deterministic: the fake provider is a test double,
    not a language model. The same function is used for indexing and matching,
    so behaviour stays consistent.
    """
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 4 and token.endswith("es"):
        return token[:-2]
    if len(token) > 3 and token.endswith("s"):
        return token[:-1]
    if len(token) > 4 and token.endswith("ed"):
        return token[:-2]
    if len(token) > 5 and token.endswith("ing"):
        return token[:-3]
    return token


class FakeEmbeddingProvider:
    name = "fake"

    def __init__(self, dim: int | None = None) -> None:
        settings = get_settings()
        self.dim = dim or settings.embedding_dim
        self.model = f"fake-hashing-{self.dim}"

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        vector = [0.0] * self.dim
        tokens = _tokens(text)
        features = tokens + [f"{a}_{b}" for a, b in itertools.pairwise(tokens)]
        for feature in features:
            digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "big") % self.dim
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[index] += sign
        norm = math.sqrt(sum(v * v for v in vector))
        if norm == 0.0:
            # Avoid zero vectors: pgvector cosine distance is undefined for them.
            vector[0] = 1.0
            return vector
        return [v / norm for v in vector]


class FakeLLMProvider:
    name = "fake"

    def __init__(self) -> None:
        self.model = "fake-extractive-v1"

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        tools: Sequence[ToolSpec] = (),
        force_tool: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 800,
    ) -> LLMResult:
        question = self._last_user_text(messages)
        docs = self._parse_docs("\n".join(m.content for m in messages))

        available = {t.name for t in tools}
        tokens_in = _approx_tokens(messages)

        def tool_call(name: str, arguments: dict[str, object]) -> LLMResult:
            return LLMResult(
                text="",
                model=self.model,
                tool_calls=[ToolCall(id=f"fake-call-{name}", name=name, arguments=arguments)],
                input_tokens=tokens_in,
                finish_reason="tool_calls",
            )

        # Honour an explicit forced tool (the loop forces search_docs first).
        # The real tool caps queries at 300 chars; mirror that here.
        query = question[:300]
        if force_tool == "search_docs" and "search_docs" in available:
            return tool_call("search_docs", {"query": query})

        # No context yet and a search tool is available -> ask for it, so the
        # tool loop is exercised exactly like a real model would.
        if not docs and "search_docs" in available:
            return tool_call("search_docs", {"query": query})

        # Emulate a model choosing to escalate when the user asks for a person.
        if (
            "escalate_to_human" in available
            and _WANTS_HUMAN.search(question)
            and not self._answered(docs, question)
        ):
            return tool_call(
                "escalate_to_human",
                {
                    "reason": "customer_requested_human",
                    # EscalateArgs.summary requires >= 10 characters; a terse
                    # prompt like "human?" must not produce an invalid tool call.
                    "summary": f"User asked: {question}"[:500],
                    "priority": "normal",
                },
            )

        answer, citations = self._answer(question, docs)
        text = json.dumps({"answer": answer, "citations": citations})
        return LLMResult(
            text=text,
            model=self.model,
            input_tokens=tokens_in,
            output_tokens=_approx_tokens([ChatMessage("assistant", text)]),
        )

    def _answered(self, docs: list[tuple[str, str, str]], question: str) -> bool:
        answer, _ = self._answer(question, docs)
        return answer != REFUSAL_ANSWER

    @staticmethod
    def _last_user_text(messages: Sequence[ChatMessage]) -> str:
        for message in reversed(messages):
            if message.role == "user":
                return message.content.strip()
        return ""

    @staticmethod
    def _parse_docs(blob: str) -> list[tuple[str, str, str]]:
        return [
            (m.group("chunk_id"), m.group("source"), m.group("content"))
            for m in UNTRUSTED_DOC_RE.finditer(blob)
        ]

    def _answer(self, question: str, docs: list[tuple[str, str, str]]) -> tuple[str, list[str]]:
        if not docs:
            return REFUSAL_ANSWER, []
        q_tokens = set(_tokens(question))
        if not q_tokens:
            return REFUSAL_ANSWER, []

        # Rank by question-token coverage first, then by sentence precision.
        # The tie-break matters: a short on-topic sentence ("refunds within 30
        # days") should beat a long sentence that merely repeats two keywords.
        best: tuple[float, float, str, str] = (0.0, 0.0, "", "")
        for chunk_id, _source, content in docs:
            for sentence in _SENTENCE_RE.split(content):
                sentence = sentence.strip(" -*\t")
                if not sentence:
                    continue
                s_tokens = set(_tokens(sentence))
                if not s_tokens:
                    continue
                matched = len(q_tokens & s_tokens)
                overlap = matched / len(q_tokens)
                precision = matched / len(s_tokens)
                if (overlap, precision) > best[:2]:
                    best = (overlap, precision, sentence, chunk_id)

        # Below this the sentence is not really on-topic -> refuse instead of
        # hallucinating. Matches the "don't answer outside the docs" behaviour
        # the real prompt asks for.
        if best[0] < 0.30:
            return REFUSAL_ANSWER, []
        return best[2], [best[3]]


def _approx_tokens(messages: Sequence[ChatMessage]) -> int:
    return max(1, sum(len(m.content) for m in messages) // 4)
