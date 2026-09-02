"""Fast exact edit metrics and review-oriented transcript alignment."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Hashable, Sequence, TypeVar


_T = TypeVar("_T", bound=Hashable)
_WORD_RE = re.compile(r"[^\W_]+(?:[’'][^\W_]+)*", flags=re.UNICODE)


@dataclass(frozen=True)
class AlignmentMetrics:
  reference_words: int
  hypothesis_words: int
  word_edits: int
  word_error_rate: float
  reference_characters: int
  hypothesis_characters: int
  character_edits: int
  character_error_rate: float
  spans: tuple[dict[str, object], ...]


def comparison_words(value: str) -> tuple[str, ...]:
  """Normalize orthography while retaining Unicode letters, digits, and apostrophes."""
  normalized = unicodedata.normalize("NFKC", value).casefold()
  normalized = normalized.replace("’", "'").replace("‐", "-").replace("–", "-")
  return tuple(match.group(0) for match in _WORD_RE.finditer(normalized))


def _myers_distance(reference: Sequence[_T], hypothesis: Sequence[_T]) -> int:
  """Calculate exact Levenshtein distance with Python's arbitrary-size bitsets."""
  if not reference:
    return len(hypothesis)
  if not hypothesis:
    return len(reference)
  if len(reference) > len(hypothesis):
    reference, hypothesis = hypothesis, reference
  width = len(reference)
  high_bit = 1 << (width - 1)
  mask = (1 << width) - 1
  equality: dict[_T, int] = {}
  for index, token in enumerate(reference):
    equality[token] = equality.get(token, 0) | (1 << index)
  positive = mask
  negative = 0
  score = width
  for token in hypothesis:
    equal = equality.get(token, 0)
    vertical = equal | negative
    horizontal = (((equal & positive) + positive) ^ positive) | equal
    positive_horizontal = negative | ~(horizontal | positive)
    negative_horizontal = positive & horizontal
    if positive_horizontal & high_bit:
      score += 1
    elif negative_horizontal & high_bit:
      score -= 1
    positive_horizontal = ((positive_horizontal << 1) | 1) & mask
    negative_horizontal = (negative_horizontal << 1) & mask
    positive = (negative_horizontal | ~(vertical | positive_horizontal)) & mask
    negative = (positive_horizontal & vertical) & mask
  return score


def align_text(reference: str, hypothesis: str) -> AlignmentMetrics:
  """Compute exact rates plus human-readable changed spans."""
  reference_words = comparison_words(reference)
  hypothesis_words = comparison_words(hypothesis)
  word_edits = _myers_distance(reference_words, hypothesis_words)
  reference_characters = " ".join(reference_words)
  hypothesis_characters = " ".join(hypothesis_words)
  character_edits = _myers_distance(reference_characters, hypothesis_characters)

  spans: list[dict[str, object]] = []
  matcher = SequenceMatcher(
      None,
      reference_words,
      hypothesis_words,
      autojunk=False,
  )
  for operation, ref_start, ref_end, hyp_start, hyp_end in matcher.get_opcodes():
    if operation == "equal":
      continue
    spans.append(
        {
            "operation": operation,
            "reference_start": ref_start,
            "reference_end": ref_end,
            "hypothesis_start": hyp_start,
            "hypothesis_end": hyp_end,
            "reference": " ".join(reference_words[ref_start:ref_end]),
            "hypothesis": " ".join(hypothesis_words[hyp_start:hyp_end]),
            "reference_word_count": ref_end - ref_start,
            "hypothesis_word_count": hyp_end - hyp_start,
        }
    )
  return AlignmentMetrics(
      reference_words=len(reference_words),
      hypothesis_words=len(hypothesis_words),
      word_edits=word_edits,
      word_error_rate=word_edits / max(1, len(reference_words)),
      reference_characters=len(reference_characters),
      hypothesis_characters=len(hypothesis_characters),
      character_edits=character_edits,
      character_error_rate=character_edits / max(1, len(reference_characters)),
      spans=tuple(spans),
  )


def missing_protected_terms(
    hypothesis: str,
    terms: Sequence[str],
) -> tuple[str, ...]:
  """Return configured terms whose normalized token sequence is absent."""
  words = comparison_words(hypothesis)
  missing: list[str] = []
  for term in terms:
    target = comparison_words(term)
    if not target:
      continue
    found = any(
        words[index : index + len(target)] == target
        for index in range(0, len(words) - len(target) + 1)
    )
    if not found:
      missing.append(term)
  return tuple(missing)
