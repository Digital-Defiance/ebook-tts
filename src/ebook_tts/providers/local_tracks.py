"""Resolve per-track local recovery overrides from a book's working config."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from ..models import LocalAudioPatch, LocalTrackOverride, LocalTTSConfig, SpokenReplaceRule
from ..utils import sha256_file


def match_track_override(
    tracks: tuple[LocalTrackOverride, ...],
    *,
    chapter_number: int | None,
    track_number: int,
) -> LocalTrackOverride | None:
  """Return the unique override that matches this section, or None."""
  hits: list[LocalTrackOverride] = []
  for override in tracks:
    chapter_ok = override.chapter is None or override.chapter == chapter_number
    track_ok = override.track is None or override.track == track_number
    if override.chapter is None and override.track is None:
      continue
    if chapter_ok and track_ok:
      hits.append(override)
  if len(hits) > 1:
    raise ValueError(
        f"Multiple tts.local.tracks entries match chapter={chapter_number!r} "
        f"track={track_number}."
    )
  return hits[0] if hits else None


def effective_local_config(
    base: LocalTTSConfig,
    override: LocalTrackOverride | None,
) -> LocalTTSConfig:
  """Book defaults with optional per-track max_words_per_call."""
  if override is None or override.max_words_per_call is None:
    return base
  return replace(base, max_words_per_call=override.max_words_per_call)


def apply_spoken_replacements(
    text: str,
    replacements: tuple[SpokenReplaceRule, ...],
) -> str:
  """Apply generation-only swaps; each old string must occur exactly once."""
  updated = text
  for rule in replacements:
    if not rule.old:
      raise ValueError("spoken_replace.from must be a non-empty string.")
    count = updated.count(rule.old)
    if count != 1:
      raise ValueError(
          f"spoken_replace.from must occur exactly once; {rule.old!r} occurs {count} times."
      )
    updated = updated.replace(rule.old, rule.new, 1)
  return updated


def override_identity(
    override: LocalTrackOverride | None,
    *,
    config_dir: Path | None = None,
) -> dict[str, Any] | None:
  """Stable identity fragment for request hashing; includes phrase file digests."""
  if override is None:
    return None
  patches = []
  for patch in override.patches:
    phrase_path = Path(patch.phrase)
    if config_dir is not None and not phrase_path.is_absolute():
      phrase_path = config_dir / phrase_path
    patches.append(
        {
            "phrase": patch.phrase,
            "phrase_sha256": (
                sha256_file(phrase_path) if phrase_path.is_file() else ""
            ),
            "start": patch.start,
            "end": patch.end,
            "replace_unintelligible": patch.replace_unintelligible,
            "allow_duration_change": patch.allow_duration_change,
            "announcement_text": patch.announcement_text or "",
        }
    )
  return {
      "chapter": override.chapter,
      "track": override.track,
      "max_words_per_call": override.max_words_per_call,
      "spoken_replace": [
          {"from": rule.old, "to": rule.new} for rule in override.spoken_replace
      ],
      "patches": patches,
  }


def tracks_identity(
    tracks: tuple[LocalTrackOverride, ...],
    *,
    config_dir: Path | None = None,
) -> list[dict[str, Any]]:
  """All configured track exceptions for run-level identity."""
  return [
      fragment
      for override in tracks
      if (fragment := override_identity(override, config_dir=config_dir)) is not None
  ]


def patch_records(patches: tuple[LocalAudioPatch, ...]) -> list[dict[str, Any]]:
  return [
      {
          "phrase": patch.phrase,
          "start": patch.start,
          "end": patch.end,
          "replace_unintelligible": patch.replace_unintelligible,
          "allow_duration_change": patch.allow_duration_change,
          "announcement_text": patch.announcement_text,
      }
      for patch in patches
  ]
