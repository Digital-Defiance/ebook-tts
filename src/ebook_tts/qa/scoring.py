"""Spoken-aware transcript assessment.

Exact Levenshtein WER still runs in `alignment.py`. This gate treats numeral
words, UK/US spelling, and weak function-word ASR flips as benign, which is
what local engines need when the manuscript keeps digits and the narrator
speaks "nineteen fifty-two".
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher


TOKEN = re.compile(r"[^\W_]+(?:'[^\W_]+)*", re.UNICODE)
APOSTROPHES = str.maketrans({"’": "'", "‘": "'", "ʼ": "'", "＇": "'"})
_ONES = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
}
_TENS = {
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}
_SMALL_ONES = {word: value for word, value in _ONES.items() if 1 <= value <= 9}
_NUMBER_WORDS = {**_ONES, **_TENS, "hundred": 100}
_SKIP_WORDS = frozenset({"and", "a", "the", "equals", "point", "oh", "o"})
_ORDINALS = {
    "first": 1,
    "second": 2,
    "third": 3,
    "fourth": 4,
    "fifth": 5,
    "sixth": 6,
    "seventh": 7,
    "eighth": 8,
    "ninth": 9,
    "tenth": 10,
    "eleventh": 11,
    "twelfth": 12,
}


def _fold_hundreds(values: list[int]) -> list[int]:
  folded: list[int] = []
  index = 0
  while index < len(values):
    if index + 1 < len(values) and values[index + 1] == 100:
      head = values[index] if 0 < values[index] < 100 else 1
      total = head * 100
      index += 2
      if index < len(values) and values[index] < 100:
        total += values[index]
        index += 1
      folded.append(total)
      continue
    if values[index] == 100:
      total = 100
      index += 1
      if index < len(values) and values[index] < 100:
        total += values[index]
        index += 1
      folded.append(total)
      continue
    folded.append(values[index])
    index += 1
  return folded


def _numberish(tokens: list[str]) -> str:
  """Map digit groups and number words onto one comparable digit string.

  Tens compose with a following ones word (`fifty two` → 52) so spoken clock
  times match manuscript `19:52`. Leading zeros on digit tokens are dropped
  per group so `07:02` matches `seven oh two`.
  """
  if not tokens:
    return ""
  values: list[int] = []
  index = 0
  while index < len(tokens):
    token = tokens[index]
    if token in _SKIP_WORDS:
      index += 1
      continue
    if token.isdigit():
      values.append(int(token))
      index += 1
      continue
    if token in _ORDINALS:
      values.append(_ORDINALS[token])
      index += 1
      continue
    if token.endswith(("st", "nd", "rd", "th")) and token[:-2].isdigit():
      values.append(int(token[:-2]))
      index += 1
      continue
    if token.endswith(("st", "nd", "rd", "th")) and token[:-2] in _NUMBER_WORDS:
      values.append(_NUMBER_WORDS[token[:-2]])
      index += 1
      continue
    if token in _TENS and index + 1 < len(tokens) and tokens[index + 1] in _SMALL_ONES:
      values.append(_TENS[token] + _SMALL_ONES[tokens[index + 1]])
      index += 2
      continue
    if token in _NUMBER_WORDS:
      values.append(_NUMBER_WORDS[token])
      index += 1
      continue
    return ""
  if not values:
    return ""
  return "".join(str(value) for value in _fold_hundreds(values))


def _normalize_spelling(token: str) -> str:
  token = token.replace("'", "")
  pairs = (
      ("neighbour", "neighbor"),
      ("neighbours", "neighbors"),
      ("digitiser", "digitizer"),
      ("travelling", "traveling"),
      ("travelled", "traveled"),
      ("organised", "organized"),
      ("recognised", "recognized"),
  )
  for uk, us in pairs:
    if token in {uk, us}:
      return us
  return token


def normalized_tokens(value: str) -> tuple[str, ...]:
  normalized = unicodedata.normalize("NFC", value).translate(APOSTROPHES).casefold()
  return tuple(TOKEN.findall(normalized))


def token_wer(expected: tuple[str, ...], actual: tuple[str, ...]) -> float:
  if not expected:
    return 0.0 if not actual else 1.0
  matcher = SequenceMatcher(a=expected, b=actual, autojunk=False)
  edits = 0
  for tag, i1, i2, j1, j2 in matcher.get_opcodes():
    if tag == "equal":
      continue
    if tag == "replace":
      edits += max(i2 - i1, j2 - j1)
    elif tag == "delete":
      edits += i2 - i1
    elif tag == "insert":
      edits += j2 - j1
  return edits / len(expected)


def is_benign_replace(expected: list[str], actual: list[str]) -> bool:
  if not expected and not actual:
    return True
  expected_number = _numberish(expected)
  actual_number = _numberish(actual)
  if expected_number and actual_number and expected_number == actual_number:
    return True
  if len(expected) == 1 and len(actual) == 1:
    left = _normalize_spelling(expected[0])
    right = _normalize_spelling(actual[0])
    if left == right:
      return True
    weak = {
        "a", "an", "the", "and", "in", "on", "at", "as", "to", "of", "i", "it",
        "that", "this", "than", "then", "with", "for", "from", "by", "or", "but",
    }
    if left in weak and right in weak:
      return True
    if left.rstrip("ds") == right.rstrip("ds") and min(len(left), len(right)) >= 4:
      return True
  elif len(expected) == len(actual) and len(expected) > 1:
    if all(
        is_benign_replace([left], [right])
        for left, right in zip(expected, actual)
    ):
      return True
  expected_number = _numberish([token for token in expected if token not in {"and", "a", "the"}])
  actual_number = _numberish([token for token in actual if token not in {"and", "a", "the"}])
  if expected_number and actual_number and expected_number == actual_number:
    return True
  joined_expected = "".join(expected).replace("-", "")
  joined_actual = "".join(actual).replace("-", "")
  return bool(joined_expected) and joined_expected == joined_actual


def assess_transcript(
    expected: str,
    heard_text: str,
    *,
    max_wer: float = 0.12,
    min_coverage: float = 0.94,
    max_coverage: float = 1.06,
    max_gap: int = 8,
    max_added: int = 8,
) -> dict[str, object]:
  """Assess an ASR transcript against expected synthesis text."""
  wanted = normalized_tokens(expected)
  heard = normalized_tokens(heard_text)
  if not wanted:
    return {
        "passed": not heard,
        "wer": 0.0 if not heard else 1.0,
        "coverage": 0.0,
        "max_expected_gap": 0,
        "max_added_span": len(heard),
        "suspicious_spans": [],
        "transcript": heard_text,
    }
  matcher = SequenceMatcher(a=wanted, b=heard, autojunk=False)
  max_expected_gap = 0
  max_added_span = 0
  suspicious: list[dict[str, object]] = []
  edits = 0
  skipped_wanted = 0
  skipped_heard = 0
  for tag, i1, i2, j1, j2 in matcher.get_opcodes():
    if tag == "equal":
      continue
    expected_part = list(wanted[i1:i2])
    heard_part = list(heard[j1:j2])
    if is_benign_replace(expected_part, heard_part):
      skipped_wanted += i2 - i1
      skipped_heard += j2 - j1
      continue
    if tag in {"delete", "replace"}:
      max_expected_gap = max(max_expected_gap, i2 - i1)
    if tag in {"insert", "replace"}:
      max_added_span = max(max_added_span, j2 - j1)
    if tag == "replace":
      edits += max(i2 - i1, j2 - j1)
      suspicious.append(
          {
              "expected": expected_part,
              "heard": heard_part,
              "expected_len": i2 - i1,
              "heard_len": j2 - j1,
          }
      )
    elif tag == "delete":
      edits += i2 - i1
    elif tag == "insert":
      edits += j2 - j1
  wer = edits / len(wanted)
  remaining_wanted = len(wanted) - skipped_wanted
  remaining_heard = len(heard) - skipped_heard
  if remaining_wanted:
    coverage = remaining_heard / remaining_wanted
  else:
    coverage = 0.0 if remaining_heard else 1.0
  passed = (
      wer <= max_wer
      and min_coverage <= coverage <= max_coverage
      and max_expected_gap < max_gap
      and max_added_span < max_added
  )
  return {
      "passed": passed,
      "wer": round(wer, 4),
      "coverage": round(coverage, 4),
      "max_expected_gap": max_expected_gap,
      "max_added_span": max_added_span,
      "suspicious_spans": suspicious[:8],
      "transcript": heard_text,
  }
