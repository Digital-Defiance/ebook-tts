"""Deterministic paragraph/sentence-aware text chunking."""

from __future__ import annotations

import re

from ..errors import ConfigError


_SENTENCE_END_RE = re.compile(r'''[.!?]+[”’"')\]]*(?=\s+|$)''')
_ABBREVIATIONS = {
    "capt",
    "col",
    "dr",
    "etc",
    "gen",
    "jr",
    "lt",
    "mr",
    "mrs",
    "ms",
    "mt",
    "no",
    "prof",
    "sgt",
    "sr",
    "st",
    "vs",
}


def normalize_block(value: str) -> str:
  value = value.replace("\u00a0", " ").replace("\u202f", " ")
  return re.sub(r"\s+", " ", value).strip()


def _is_abbreviation(candidate: str) -> bool:
  bare = candidate.rstrip('”’"\')]').rstrip()
  if not bare.endswith(".") or bare.endswith(".."):
    return False
  word_match = re.search(r"([A-Za-z]+)\.$", bare)
  if word_match and word_match.group(1).lower() in _ABBREVIATIONS:
    return True
  return bool(re.search(r"(?:\b[A-Z]\.){1,4}$", bare))


def split_sentences(paragraph: str) -> list[str]:
  """Split at likely sentence endings while retaining punctuation."""
  sentences: list[str] = []
  start = 0
  for match in _SENTENCE_END_RE.finditer(paragraph):
    end = match.end()
    if _is_abbreviation(paragraph[start:end]):
      continue
    sentence = paragraph[start:end].strip()
    if sentence:
      sentences.append(sentence)
    start = end
    while start < len(paragraph) and paragraph[start].isspace():
      start += 1
  remainder = paragraph[start:].strip()
  if remainder:
    sentences.append(remainder)
  return sentences or [paragraph]


def _split_oversized_unit(value: str, max_characters: int) -> list[str]:
  pieces: list[str] = []
  remaining = value.strip()
  minimum_preferred = max(1, int(max_characters * 0.55))
  while len(remaining) > max_characters:
    window = remaining[: max_characters + 1]
    preferred = [
        match.end()
        for match in re.finditer(r"[,;:—–-]\s+", window)
        if minimum_preferred <= match.end() <= max_characters
    ]
    if preferred:
      cut = preferred[-1]
    else:
      whitespace = [
          match.start()
          for match in re.finditer(r"\s+", window)
          if 0 < match.start() <= max_characters
      ]
      cut = whitespace[-1] if whitespace else max_characters
    piece = remaining[:cut].strip()
    if not piece:
      cut = max_characters
      piece = remaining[:cut]
    pieces.append(piece)
    remaining = remaining[cut:].strip()
  if remaining:
    pieces.append(remaining)
  return pieces


def _split_paragraph(paragraph: str, max_characters: int) -> list[str]:
  units: list[str] = []
  for sentence in split_sentences(paragraph):
    if len(sentence) <= max_characters:
      units.append(sentence)
    else:
      units.extend(_split_oversized_unit(sentence, max_characters))
  pieces: list[str] = []
  current = ""
  for unit in units:
    candidate = f"{current} {unit}" if current else unit
    if len(candidate) <= max_characters:
      current = candidate
    else:
      if current:
        pieces.append(current)
      current = unit
  if current:
    pieces.append(current)
  return pieces


def chunk_text(text: str, max_characters: int) -> list[str]:
  """Create bounded chunks while preserving all non-whitespace tokens."""
  if max_characters < 100:
    raise ConfigError("Chunk size must be at least 100 characters.")
  paragraphs = [
      normalize_block(paragraph)
      for paragraph in re.split(r"\n\s*\n", text)
      if normalize_block(paragraph)
  ]
  if not paragraphs:
    raise ConfigError("Cannot chunk empty narration text.")

  chunks: list[str] = []
  current = ""
  for paragraph in paragraphs:
    for piece_index, piece in enumerate(_split_paragraph(paragraph, max_characters)):
      separator = "\n\n" if piece_index == 0 else " "
      candidate = f"{current}{separator}{piece}" if current else piece
      if len(candidate) <= max_characters:
        current = candidate
      else:
        if current:
          chunks.append(current)
        current = piece
  if current:
    chunks.append(current)

  if not chunks or any(not chunk or len(chunk) > max_characters for chunk in chunks):
    raise ConfigError("Chunking produced an empty or oversized chunk.")
  source_tokens = re.findall(r"\S+", text)
  chunk_tokens = re.findall(r"\S+", " ".join(chunks))
  if source_tokens != chunk_tokens:
    # A token longer than the provider limit must be hard-split across requests.
    # In that exceptional case token boundaries differ, but the complete ordered
    # non-whitespace character stream must still be identical.
    if "".join(source_tokens) != "".join(chunk_tokens):
      raise ConfigError("Chunking changed, dropped, or reordered source text.")
  return chunks
