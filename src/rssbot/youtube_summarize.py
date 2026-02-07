from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import math
import re

SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")
WORD_RE = re.compile(r"\b[^\W\d_][^\W\d_'-]*\b", flags=re.UNICODE)
WHITESPACE_RE = re.compile(r"\s+")
CLAUSE_SPLIT_RE = re.compile(r"(?<=[,;:])\s+")

BASE_STOPWORDS = {
    "a",
    "about",
    "after",
    "all",
    "also",
    "am",
    "an",
    "and",
    "any",
    "are",
    "as",
    "at",
    "be",
    "because",
    "been",
    "before",
    "being",
    "between",
    "both",
    "but",
    "by",
    "can",
    "could",
    "did",
    "do",
    "does",
    "doing",
    "down",
    "during",
    "each",
    "few",
    "for",
    "from",
    "further",
    "had",
    "has",
    "have",
    "having",
    "he",
    "her",
    "here",
    "hers",
    "herself",
    "him",
    "himself",
    "his",
    "how",
    "i",
    "if",
    "in",
    "into",
    "is",
    "it",
    "its",
    "itself",
    "just",
    "me",
    "more",
    "most",
    "my",
    "myself",
    "no",
    "nor",
    "not",
    "now",
    "of",
    "off",
    "on",
    "once",
    "only",
    "or",
    "other",
    "our",
    "ours",
    "ourselves",
    "out",
    "over",
    "own",
    "same",
    "she",
    "should",
    "so",
    "some",
    "such",
    "than",
    "that",
    "the",
    "their",
    "theirs",
    "them",
    "themselves",
    "then",
    "there",
    "these",
    "they",
    "this",
    "those",
    "through",
    "to",
    "too",
    "under",
    "until",
    "up",
    "very",
    "was",
    "we",
    "were",
    "what",
    "when",
    "where",
    "which",
    "while",
    "who",
    "whom",
    "why",
    "will",
    "with",
    "you",
    "your",
    "yours",
    "yourself",
    "yourselves",
}

FILLER_WORDS = {
    "ah",
    "eh",
    "hmm",
    "like",
    "mm",
    "oh",
    "ok",
    "okay",
    "right",
    "uh",
    "um",
    "yeah",
}

META_WORDS = {
    "announcement",
    "bonus",
    "channel",
    "comment",
    "episode",
    "follow",
    "grammar",
    "link",
    "newsletter",
    "patreon",
    "podcast",
    "premium",
    "sponsor",
    "subscribers",
    "subscribe",
    "subscription",
    "transcript",
    "vocabulary",
    "website",
}

LOW_VALUE_SENTENCES = {
    "that's it",
    "thats it",
    "that's the thing",
    "thats the thing",
    "that's the end",
    "thats the end",
}

MIN_LLM_INPUT_WORDS = 220
AUTO_MAX_LLM_INPUT_WORDS = 3200


class SummarizationError(RuntimeError):
    """Raised when summary generation fails."""


@dataclass(frozen=True)
class SentenceCandidate:
    index: int
    text: str
    tokens: tuple[str, ...]
    token_set: frozenset[str]
    score: float


def _normalize_space(text: str) -> str:
    return WHITESPACE_RE.sub(" ", text).strip()


def _tokenize(text: str) -> list[str]:
    return [word.lower() for word in WORD_RE.findall(text)]


def _word_count(text: str) -> int:
    return len(_tokenize(text))


def _split_sentences(text: str) -> list[str]:
    chunks = SENTENCE_SPLIT_RE.split(_normalize_space(text))
    return [_normalize_space(chunk) for chunk in chunks if _normalize_space(chunk)]


def _merge_sentence_fragments(sentences: list[str], min_words: int = 6, max_words: int = 42) -> list[str]:
    if not sentences:
        return []

    merged: list[str] = []
    for sentence in sentences:
        wc = _word_count(sentence)
        if wc < min_words and merged:
            candidate = f"{merged[-1]} {sentence}"
            if _word_count(candidate) <= max_words:
                merged[-1] = candidate
                continue
        merged.append(sentence)

    if merged and _word_count(merged[-1]) < min_words and len(merged) > 1:
        candidate = f"{merged[-2]} {merged[-1]}"
        if _word_count(candidate) <= max_words:
            merged[-2] = candidate
            merged.pop()

    return merged


def _split_long_sentences(
    sentences: list[str], max_words: int = 55, min_chunk_words: int = 8
) -> list[str]:
    result: list[str] = []
    for sentence in sentences:
        words = _word_count(sentence)
        if words <= max_words:
            result.append(sentence)
            continue

        clauses = [part.strip() for part in CLAUSE_SPLIT_RE.split(sentence) if part.strip()]
        if len(clauses) < 2:
            result.append(sentence)
            continue

        current = clauses[0]
        for clause in clauses[1:]:
            candidate = f"{current} {clause}"
            if _word_count(candidate) <= max_words:
                current = candidate
            else:
                if _word_count(current) >= min_chunk_words:
                    result.append(current)
                current = clause

        if _word_count(current) >= min_chunk_words:
            result.append(current)

    return result or sentences


def _build_dynamic_stopwords(document_tokens: list[str], max_fraction: float = 0.08) -> set[str]:
    if not document_tokens:
        return set()

    freq = Counter(token for token in document_tokens if token not in BASE_STOPWORDS and len(token) > 2)
    if len(freq) < 30:
        return set()

    top_n = max(8, int(len(freq) * max_fraction))
    return {word for word, _ in freq.most_common(top_n)}


def _score_sentences(sentences: list[str]) -> list[SentenceCandidate]:
    sentence_tokens = [_tokenize(sentence) for sentence in sentences]
    document_tokens = [token for tokens in sentence_tokens for token in tokens]
    dynamic_stopwords = _build_dynamic_stopwords(document_tokens)
    stopwords = BASE_STOPWORDS | dynamic_stopwords

    content_per_sentence: list[list[str]] = []
    for tokens in sentence_tokens:
        content_tokens = [token for token in tokens if token not in stopwords and len(token) > 2]
        content_per_sentence.append(content_tokens)

    content_freq = Counter(token for tokens in content_per_sentence for token in tokens)
    if not content_freq:
        return []

    sentence_occurrence: Counter[str] = Counter()
    for tokens in content_per_sentence:
        sentence_occurrence.update(set(tokens))

    total_sentences = max(1, len(sentences))
    token_weight: dict[str, float] = {}
    for token, frequency in content_freq.items():
        idf = math.log(1.0 + total_sentences / (1.0 + sentence_occurrence[token]))
        token_weight[token] = frequency * idf

    scored: list[SentenceCandidate] = []
    for index, sentence in enumerate(sentences):
        raw_tokens = sentence_tokens[index]
        content_tokens = content_per_sentence[index]
        if not raw_tokens or len(content_tokens) < 3:
            continue

        unique_tokens = set(content_tokens)
        base_score = sum(token_weight[token] for token in unique_tokens)
        length_norm = math.sqrt(len(content_tokens) + 1)
        score = base_score / length_norm

        total_words = len(raw_tokens)
        if total_words < 8:
            score *= 0.45
        elif total_words > 65:
            score *= 0.40
        elif total_words > 45:
            score *= 0.80

        filler_hits = sum(1 for token in raw_tokens if token in FILLER_WORDS)
        filler_ratio = filler_hits / max(1, total_words)
        if filler_ratio >= 0.12:
            score *= 0.60

        meta_hits = sum(1 for token in raw_tokens if token in META_WORDS)
        meta_ratio = meta_hits / max(1, total_words)
        if meta_ratio >= 0.08:
            score *= 0.45

        lowered = sentence.lower().strip(" .!?")
        if lowered in LOW_VALUE_SENTENCES:
            score *= 0.20

        if index < max(3, total_sentences // 12):
            score *= 1.08

        scored.append(
            SentenceCandidate(
                index=index,
                text=sentence,
                tokens=tuple(content_tokens),
                token_set=frozenset(content_tokens),
                score=score,
            )
        )

    return scored


def _jaccard_similarity(left: frozenset[str], right: frozenset[str]) -> float:
    if not left or not right:
        return 0.0
    union = left | right
    if not union:
        return 0.0
    return len(left & right) / len(union)


def _select_diverse_sentences(candidates: list[SentenceCandidate], limit: int) -> list[SentenceCandidate]:
    if not candidates or limit <= 0:
        return []

    if len(candidates) <= limit:
        return sorted(candidates, key=lambda item: item.index)

    min_score = min(candidate.score for candidate in candidates)
    max_score = max(candidate.score for candidate in candidates)
    spread = max(1e-9, max_score - min_score)

    normalized = {
        candidate.index: (candidate.score - min_score) / spread for candidate in candidates
    }

    selected: list[SentenceCandidate] = []
    remaining = {candidate.index: candidate for candidate in candidates}
    diversity_weight = 0.26

    while remaining and len(selected) < limit:
        best_idx: int | None = None
        best_mmr = float("-inf")

        for idx, candidate in remaining.items():
            relevance = normalized[idx]
            if not selected:
                mmr = relevance
            else:
                redundancy = max(
                    _jaccard_similarity(candidate.token_set, chosen.token_set) for chosen in selected
                )
                mmr = (1.0 - diversity_weight) * relevance - diversity_weight * redundancy

            if mmr > best_mmr:
                best_mmr = mmr
                best_idx = idx

        if best_idx is None:
            break

        selected.append(remaining.pop(best_idx))

    return sorted(selected, key=lambda item: item.index)


def summarize_text(text: str, max_sentences: int = 7) -> str:
    if max_sentences < 1:
        raise ValueError("max_sentences must be >= 1")

    raw_sentences = _split_sentences(text)
    sentences = _split_long_sentences(_merge_sentence_fragments(raw_sentences))
    if not sentences:
        return "No transcript text available to summarize."

    if len(sentences) <= max_sentences:
        return "\n".join(f"- {sentence}" for sentence in sentences)

    candidates = _score_sentences(sentences)
    if not candidates:
        fallback = sentences[:max_sentences]
        return "\n".join(f"- {sentence}" for sentence in fallback)

    selected = _select_diverse_sentences(candidates, max_sentences)
    if not selected:
        fallback = sentences[:max_sentences]
        return "\n".join(f"- {sentence}" for sentence in fallback)

    return "\n".join(f"- {item.text}" for item in selected)


def _dedupe_sentences(sentences: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for sentence in sentences:
        normalized = _normalize_space(sentence)
        if not normalized:
            continue
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(normalized)
    return deduped


def _auto_llm_input_word_budget(total_words: int, max_sentences: int) -> int:
    floor = max(MIN_LLM_INPUT_WORDS, max_sentences * 120)

    if total_words <= 2400:
        return total_words
    if total_words <= 6000:
        target = 1400
    elif total_words <= 12000:
        target = 1800
    elif total_words <= 22000:
        target = 2200
    else:
        target = 2600

    return min(total_words, min(AUTO_MAX_LLM_INPUT_WORDS, max(floor, target)))


def _compress_transcript_for_llm(text: str, max_words: int, max_sentences: int) -> str:
    sentences = _dedupe_sentences(
        _split_long_sentences(_merge_sentence_fragments(_split_sentences(text)))
    )
    if not sentences:
        return _normalize_space(text)

    candidates = _score_sentences(sentences)
    prioritized: list[str]
    if candidates:
        ranked = sorted(candidates, key=lambda item: item.score, reverse=True)
        shortlist_size = min(len(ranked), max(60, max_sentences * 24, max_words // 9))
        shortlist = ranked[:shortlist_size]
        diverse_limit = min(len(shortlist), max(22, max_sentences * 6, max_words // 25))
        diverse = _select_diverse_sentences(shortlist, diverse_limit)
        prioritized = [candidate.text for candidate in diverse]

        seen_prioritized = {_normalize_space(sentence).lower() for sentence in prioritized}
        for candidate in ranked:
            normalized = _normalize_space(candidate.text)
            key = normalized.lower()
            if key not in seen_prioritized:
                prioritized.append(normalized)
                seen_prioritized.add(key)
    else:
        prioritized = sentences[:]

    selected: list[str] = []
    selected_keys: set[str] = set()
    used_words = 0
    min_fill_words = max(260, int(max_words * 0.62))

    def try_add(sentence: str) -> None:
        nonlocal used_words

        normalized = _normalize_space(sentence)
        if not normalized:
            return
        key = normalized.lower()
        if key in selected_keys:
            return

        count = _word_count(normalized)
        if count < 5:
            return
        if used_words + count > max_words:
            return

        selected.append(normalized)
        selected_keys.add(key)
        used_words += count

    for sentence in prioritized:
        try_add(sentence)
        if used_words >= max_words:
            break

    if used_words < min_fill_words:
        for sentence in sentences:
            try_add(sentence)
            if used_words >= min_fill_words:
                break

    if not selected:
        fallback: list[str] = []
        fallback_words = 0
        for sentence in sentences:
            normalized = _normalize_space(sentence)
            count = _word_count(normalized)
            if count < 2:
                continue
            if fallback_words + count > max_words and fallback:
                break
            if count > max_words and not fallback:
                tokens = _tokenize(normalized)[:max_words]
                return " ".join(tokens)
            fallback.append(normalized)
            fallback_words += count
        selected = fallback

    return "\n".join(selected)


def _prepare_llm_payload(text: str, max_sentences: int, max_input_words: int | None) -> str:
    total_words = _word_count(text)
    if total_words <= 0:
        return text

    if max_input_words is None:
        budget = _auto_llm_input_word_budget(total_words=total_words, max_sentences=max_sentences)
    else:
        budget = max(MIN_LLM_INPUT_WORDS, max_input_words)

    if total_words <= budget:
        return text

    compressed = _compress_transcript_for_llm(
        text=text,
        max_words=budget,
        max_sentences=max_sentences,
    )
    return compressed if compressed else text


def _adaptive_llm_output_tokens(max_sentences: int, custom_prompt: str | None) -> int:
    budget = 140 + (max_sentences * 65)
    if custom_prompt:
        budget += min(260, len(_tokenize(custom_prompt)) * 3)
    return max(220, min(1100, budget))


def summarize_text_with_openai(
    text: str,
    max_sentences: int = 7,
    model: str = "gpt-4.1-mini",
    custom_prompt: str | None = None,
    max_input_words: int | None = None,
    api_key: str | None = None,
) -> str:
    if max_sentences < 1:
        raise ValueError("max_sentences must be >= 1")

    payload = _normalize_space(text)
    if not payload:
        return "No transcript text available to summarize."

    try:
        from openai import OpenAI
    except ImportError as exc:
        raise SummarizationError(
            "Missing optional dependency 'openai'. Install it with: pip install openai"
        ) from exc

    system_prompt = (
        "You are an expert meeting-notes writer. Produce a concise factual summary "
        "of the transcript as bullet points. Keep language of the transcript when possible."
    )

    llm_payload = _prepare_llm_payload(
        text=payload,
        max_sentences=max_sentences,
        max_input_words=max_input_words,
    )

    user_prompt = (
        f"Create exactly up to {max_sentences} bullet points. "
        "Each bullet must contain one concrete idea, avoid filler and repetitions. "
        "Do not include intro/outro, sponsorship, or subscription calls. "
        "Transcript (possibly condensed for token efficiency):\n\n"
        f"{llm_payload}"
    )
    if custom_prompt:
        user_prompt = (
            f"{user_prompt}\n\n"
            "Additional user instructions for this summary:\n"
            f"{custom_prompt.strip()}"
        )

    try:
        client = OpenAI(api_key=api_key) if api_key else OpenAI()
        response = client.responses.create(
            model=model,
            input=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_output_tokens=_adaptive_llm_output_tokens(
                max_sentences=max_sentences,
                custom_prompt=custom_prompt,
            ),
        )
    except Exception as exc:  # pragma: no cover - external API errors are runtime-dependent
        message = str(exc).strip() or "Unknown OpenAI API error"
        raise SummarizationError(f"OpenAI summarization failed: {message}") from exc

    output = getattr(response, "output_text", None)
    summary = _normalize_space(output if isinstance(output, str) else "")
    if not summary:
        raise SummarizationError("OpenAI returned an empty summary.")

    if "- " not in summary:
        lines = [line.strip(" -") for line in summary.splitlines() if line.strip()]
        lines = lines[:max_sentences]
        return "\n".join(f"- {line}" for line in lines)

    return "\n".join(line.rstrip() for line in summary.splitlines() if line.strip())
