"""Strict TOML configuration loading and template generation."""

from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

from .errors import ConfigError
from .models import (
    MODEL_PROFILES,
    AppConfig,
    AudioConfig,
    BookMetadata,
    BookOverrides,
    NormalizationRule,
    QAConfig,
    SectionConfig,
    TTSConfig,
)
from .utils import atomic_write_text


_OUTPUT_FORMAT_RE = re.compile(r"^[a-z0-9]+_\d+_\d+$")


def _table(value: Any, name: str) -> dict[str, Any]:
  if value is None:
    return {}
  if not isinstance(value, dict):
    raise ConfigError(f"[{name}] must be a TOML table.")
  return value


def _only(table: Mapping[str, Any], allowed: set[str], name: str) -> None:
  unknown = sorted(set(table).difference(allowed))
  if unknown:
    raise ConfigError(f"Unknown setting(s) in [{name}]: {', '.join(unknown)}")


def _strings(value: Any, name: str) -> tuple[str, ...]:
  if value is None:
    return ()
  if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
    raise ConfigError(f"{name} must be an array of strings.")
  return tuple(item.strip() for item in value if item.strip())


def _optional_string(value: Any, name: str) -> str | None:
  if value is None:
    return None
  if not isinstance(value, str):
    raise ConfigError(f"{name} must be a string.")
  stripped = value.strip()
  return stripped or None


def _integer(value: Any, default: int, name: str, minimum: int = 0) -> int:
  if value is None:
    return default
  if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
    raise ConfigError(f"{name} must be an integer of at least {minimum}.")
  return value


def _number(value: Any, default: float, name: str) -> float:
  if value is None:
    return default
  if not isinstance(value, (int, float)) or isinstance(value, bool):
    raise ConfigError(f"{name} must be a number.")
  return float(value)


def _optional_rate(value: Any, name: str) -> float | None:
  if value is None:
    return None
  number = _number(value, 0.0, name)
  if not 0.0 <= number <= 1.0:
    raise ConfigError(f"{name} must be between 0 and 1.")
  return number


def _bool(value: Any, default: bool, name: str) -> bool:
  if value is None:
    return default
  if not isinstance(value, bool):
    raise ConfigError(f"{name} must be true or false.")
  return value


def default_config() -> AppConfig:
  """Return defaults, honoring non-secret provider environment overrides."""
  tts = TTSConfig(
      voice_id=os.getenv("ELEVENLABS_VOICE_ID", "").strip(),
      model_id=os.getenv("ELEVENLABS_MODEL_ID", "eleven_multilingual_v2").strip()
      or "eleven_multilingual_v2",
  )
  profile = MODEL_PROFILES.get(tts.model_id)
  if profile:
    tts = replace(tts, max_characters=profile.default_chunk_characters)
  return AppConfig(tts=tts)


def load_config(path: Path | None) -> AppConfig:
  """Load a strict TOML config, or return environment-aware defaults."""
  defaults = default_config()
  if path is None:
    return defaults
  try:
    with path.open("rb") as stream:
      data = tomllib.load(stream)
  except FileNotFoundError as exc:
    raise ConfigError(f"Configuration file does not exist: {path}") from exc
  except tomllib.TOMLDecodeError as exc:
    raise ConfigError(f"Invalid TOML in {path}: {exc}") from exc

  allowed_root = {"book", "sections", "tts", "audio", "qa", "normalization"}
  unknown_root = sorted(set(data).difference(allowed_root))
  if unknown_root:
    raise ConfigError(f"Unknown top-level setting(s): {', '.join(unknown_root)}")

  book_data = _table(data.get("book"), "book")
  _only(book_data, {"title", "authors", "language"}, "book")
  book = BookOverrides(
      title=_optional_string(book_data.get("title"), "book.title"),
      authors=_strings(book_data.get("authors"), "book.authors"),
      language=_optional_string(book_data.get("language"), "book.language"),
  )

  section_data = _table(data.get("sections"), "sections")
  _only(
      section_data,
      {"include", "exclude", "announce_titles", "minimum_characters", "titles"},
      "sections",
  )
  titles = _table(section_data.get("titles"), "sections.titles")
  if any(not isinstance(key, str) or not isinstance(value, str) for key, value in titles.items()):
    raise ConfigError("[sections.titles] keys and values must be strings.")
  sections = SectionConfig(
      include=_strings(section_data.get("include"), "sections.include"),
      exclude=_strings(section_data.get("exclude"), "sections.exclude"),
      announce_titles=_bool(
          section_data.get("announce_titles"), True, "sections.announce_titles"
      ),
      minimum_characters=_integer(
          section_data.get("minimum_characters"), 1, "sections.minimum_characters", 1
      ),
      title_overrides=dict(titles),
  )

  tts_data = _table(data.get("tts"), "tts")
  _only(
      tts_data,
      {
          "provider",
          "voice_id",
          "model_id",
          "output_format",
          "max_characters",
          "context_characters",
          "voice_settings",
      },
      "tts",
  )
  model_id = str(tts_data.get("model_id", defaults.tts.model_id)).strip()
  if not model_id:
    raise ConfigError("tts.model_id cannot be empty.")
  profile = MODEL_PROFILES.get(model_id)
  default_max = profile.default_chunk_characters if profile else 4_500
  max_characters = _integer(
      tts_data.get("max_characters"), default_max, "tts.max_characters", 100
  )
  if profile and max_characters > profile.maximum_characters:
    raise ConfigError(
        f"tts.max_characters={max_characters:,} exceeds {model_id}'s known "
        f"{profile.maximum_characters:,}-character API limit."
    )
  output_format = str(
      tts_data.get("output_format", defaults.tts.output_format)
  ).strip()
  if not _OUTPUT_FORMAT_RE.fullmatch(output_format):
    raise ConfigError(
        "tts.output_format must resemble mp3_44100_128 or pcm_44100_16."
    )
  settings = _table(tts_data.get("voice_settings"), "tts.voice_settings")
  tts = TTSConfig(
      provider=str(tts_data.get("provider", defaults.tts.provider)).strip(),
      voice_id=str(tts_data.get("voice_id", defaults.tts.voice_id)).strip(),
      model_id=model_id,
      output_format=output_format,
      max_characters=max_characters,
      context_characters=_integer(
          tts_data.get("context_characters"),
          defaults.tts.context_characters,
          "tts.context_characters",
      ),
      voice_settings=dict(settings),
  )

  audio_data = _table(data.get("audio"), "audio")
  _only(audio_data, {"ffmpeg", "ffprobe", "genre"}, "audio")
  audio = AudioConfig(
      ffmpeg=str(audio_data.get("ffmpeg", defaults.audio.ffmpeg)).strip(),
      ffprobe=str(audio_data.get("ffprobe", defaults.audio.ffprobe)).strip(),
      genre=str(audio_data.get("genre", defaults.audio.genre)).strip(),
  )
  if not audio.ffmpeg or not audio.ffprobe:
    raise ConfigError("audio.ffmpeg and audio.ffprobe cannot be empty.")

  qa_data = _table(data.get("qa"), "qa")
  _only(
      qa_data,
      {
          "stt_provider",
          "stt_model",
          "language",
          "max_word_error_rate",
          "max_character_error_rate",
          "max_internal_silence_seconds",
          "clipping_peak_db",
          "protected_terms",
      },
      "qa",
  )
  qa = QAConfig(
      stt_provider=str(qa_data.get("stt_provider", defaults.qa.stt_provider)).strip(),
      stt_model=str(qa_data.get("stt_model", defaults.qa.stt_model)).strip(),
      language=_optional_string(qa_data.get("language"), "qa.language"),
      max_word_error_rate=_optional_rate(
          qa_data.get("max_word_error_rate"), "qa.max_word_error_rate"
      ),
      max_character_error_rate=_optional_rate(
          qa_data.get("max_character_error_rate"), "qa.max_character_error_rate"
      ),
      max_internal_silence_seconds=_number(
          qa_data.get("max_internal_silence_seconds"),
          defaults.qa.max_internal_silence_seconds,
          "qa.max_internal_silence_seconds",
      ),
      clipping_peak_db=_number(
          qa_data.get("clipping_peak_db"),
          defaults.qa.clipping_peak_db,
          "qa.clipping_peak_db",
      ),
      protected_terms=_strings(qa_data.get("protected_terms"), "qa.protected_terms"),
  )

  raw_rules = data.get("normalization", [])
  if not isinstance(raw_rules, list):
    raise ConfigError("[[normalization]] must be an array of TOML tables.")
  rules: list[NormalizationRule] = []
  for index, raw_rule in enumerate(raw_rules, start=1):
    rule = _table(raw_rule, f"normalization #{index}")
    _only(rule, {"pattern", "replacement", "regex", "expected_count", "flags"}, f"normalization #{index}")
    pattern = rule.get("pattern")
    replacement = rule.get("replacement")
    if not isinstance(pattern, str) or not isinstance(replacement, str):
      raise ConfigError(
          f"normalization #{index} requires string pattern and replacement values."
      )
    expected = rule.get("expected_count")
    if expected is not None:
      expected = _integer(expected, 0, f"normalization #{index}.expected_count")
    rules.append(
        NormalizationRule(
            pattern=pattern,
            replacement=replacement,
            regex=_bool(rule.get("regex"), False, f"normalization #{index}.regex"),
            expected_count=expected,
            flags=str(rule.get("flags", "")),
        )
    )

  return AppConfig(
      book=book,
      sections=sections,
      normalization=tuple(rules),
      tts=tts,
      audio=audio,
      qa=qa,
  )


def write_config_template(path: Path, metadata: BookMetadata) -> None:
  """Write a documented, editable config without including any API secret."""
  title = json.dumps(metadata.title, ensure_ascii=False)
  authors = ", ".join(json.dumps(author, ensure_ascii=False) for author in metadata.authors)
  language = json.dumps(metadata.language or "", ensure_ascii=False)
  template = f'''# Generated by ebook-tts init. API keys belong in the environment.

[book]
title = {title}
authors = [{authors}]
language = {language}

[sections]
announce_titles = true
minimum_characters = 1
include = []
exclude = []

# Optional exact source-href or discovered-title overrides:
# [sections.titles]
# "Text/chapter01.xhtml" = "Chapter One"

[tts]
provider = "elevenlabs"
voice_id = ""
model_id = "eleven_multilingual_v2"
output_format = "mp3_44100_128"
max_characters = 9500
context_characters = 500

[tts.voice_settings]
# stability = 0.5
# similarity_boost = 0.75

[audio]
ffmpeg = "ffmpeg"
ffprobe = "ffprobe"
genre = "Audiobook"

[qa]
stt_provider = "none" # use "elevenlabs" to enable Scribe validation
stt_model = "scribe_v2"
language = ""
max_internal_silence_seconds = 8.0
clipping_peak_db = -0.1
protected_terms = []

# Book-specific, versioned corrections belong here rather than in source code.
# [[normalization]]
# pattern = "source spelling"
# replacement = "spoken spelling"
# expected_count = 1
'''
  atomic_write_text(path, template, mode=0o644)
