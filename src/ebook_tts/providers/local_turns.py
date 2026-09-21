"""Turn planning for session-continuous local engines.

Fish S2 Pro only batches long text when it sees `<|speaker:N|>` turns. Plain
narration otherwise becomes a single truncated generate() call. Tagging each
paragraph as a speaker turn keeps Conversation continuity without the
warm-up-and-trim machinery that deleted words at seams.

Long chapters still need a second bound: `max_words_per_call` packs paragraph
turns into separate generate() calls (each re-anchored), because a single call
past roughly a thousand words has been observed to truncate mid-prose even with
speaker turns present.
"""

from __future__ import annotations

import re

from ..models import DEFAULT_VOICE_ANCHOR, LocalTTSConfig
from ..text.chunk import split_sentences
from ..text.spoken import verbalize_speech_text


SPEAKER_PREFIX = "<|speaker:0|>"
PAUSE_TAG_RE = re.compile(r"\[[^]]+\]")


def plan_spoken_text(
    text: str,
    config: LocalTTSConfig,
    *,
    spoken_replace: tuple = (),
) -> str:
  from .local_tracks import apply_spoken_replacements

  updated = apply_spoken_replacements(text, spoken_replace) if spoken_replace else text
  if config.verbalize_numerals:
    return verbalize_speech_text(updated, style=config.numeral_style)
  return updated


def pause_between_sentences(paragraph: str, tag: str) -> str:
  sentences = split_sentences(paragraph)
  if len(sentences) < 2:
    return paragraph
  return sentences[0] + " " + " ".join(f"{tag} {sentence}" for sentence in sentences[1:])


def narration_turns(text: str, config: LocalTTSConfig) -> tuple[str, ...]:
  paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
  if config.sentence_pause != "none":
    tag = "[short pause]" if config.sentence_pause == "short" else "[pause]"
    paragraphs = [pause_between_sentences(part, tag) for part in paragraphs]
  if config.sentence_turns:
    return tuple(sentence for part in paragraphs for sentence in split_sentences(part))
  return tuple(paragraphs)


def turn_word_count(turn: str) -> int:
  """Count spoken words, ignoring pause markup Fish does not speak."""
  return len(PAUSE_TAG_RE.sub("", turn).split())


def generation_parts(
    turns: tuple[str, ...],
    max_words_per_call: int,
) -> tuple[tuple[str, ...], ...]:
  """Pack turns into generate() calls without splitting a turn mid-paragraph."""
  if not turns:
    return ()
  if max_words_per_call <= 0:
    return (turns,)
  parts: list[list[str]] = [[]]
  part_words = 0
  for unit in turns:
    unit_words = turn_word_count(unit)
    if parts[-1] and part_words + unit_words > max_words_per_call:
      parts.append([])
      part_words = 0
    parts[-1].append(unit)
    part_words += unit_words
  return tuple(tuple(part) for part in parts)


def tagged_payload(turns: tuple[str, ...], config: LocalTTSConfig) -> str:
  tagged = "\n".join(f"{SPEAKER_PREFIX}{turn}" for turn in turns)
  if not config.anchor:
    return tagged
  if len(DEFAULT_VOICE_ANCHOR.encode("utf-8")) <= config.chunk_length:
    raise ValueError(
        "Voice anchor is smaller than tts.local.chunk_length, so it would be "
        "batched with the first paragraph and could not be discarded cleanly."
    )
  return f"{SPEAKER_PREFIX}{DEFAULT_VOICE_ANCHOR}\n{tagged}"


def tagged_payloads(
    turns: tuple[str, ...],
    config: LocalTTSConfig,
) -> tuple[str, ...]:
  """One Fish generate() payload per packed part, each with its own voice anchor."""
  if config.max_words_per_call > 0 and config.sentence_turns:
    raise ValueError(
        "tts.local.max_words_per_call requires paragraph turns "
        "(sentence_turns = false)."
    )
  parts = generation_parts(turns, config.max_words_per_call)
  return tuple(tagged_payload(part, config) for part in parts)
