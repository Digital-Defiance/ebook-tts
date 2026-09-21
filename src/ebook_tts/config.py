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
    DEFAULT_NARRATION_INSTRUCT,
    MODEL_PROFILES,
    AccessibilityConfig,
    AppConfig,
    AudioConfig,
    BookMetadata,
    BookOverrides,
    LocalAudioPatch,
    LocalTrackOverride,
    LocalTTSConfig,
    ManuscriptConfig,
    NormalizationRule,
    ProjectConfig,
    QAConfig,
    SectionConfig,
    SpokenReplaceRule,
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

  allowed_root = {
      "book",
      "sections",
      "tts",
      "audio",
      "qa",
      "normalization",
      "project",
      "manuscript",
      "accessibility",
  }
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
          "local",
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
  provider = str(tts_data.get("provider", defaults.tts.provider)).strip()
  if provider not in {"elevenlabs", "local"}:
    raise ConfigError('tts.provider must be "elevenlabs" or "local".')
  tts = TTSConfig(
      provider=provider,
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
      local=_load_local_tts(tts_data.get("local"), path.parent if path else None),
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
          "spoken_gate",
      },
      "qa",
  )
  stt_provider = str(qa_data.get("stt_provider", defaults.qa.stt_provider)).strip()
  if stt_provider not in {"none", "elevenlabs", "local"}:
    raise ConfigError('qa.stt_provider must be "none", "elevenlabs", or "local".')
  qa = QAConfig(
      stt_provider=stt_provider,
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
      spoken_gate=_bool(qa_data.get("spoken_gate"), False, "qa.spoken_gate"),
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
      project=_load_project(data.get("project")),
      manuscript=_load_manuscript(data.get("manuscript")),
      accessibility=_load_accessibility(data.get("accessibility")),
  )


def _resolve_relative(value: str, base: Path | None) -> str:
  if not value or Path(value).is_absolute() or base is None:
    return value
  return str((base / value).expanduser())


def _load_local_tts(raw: Any, config_dir: Path | None) -> LocalTTSConfig:
  data = _table(raw, "tts.local")
  _only(
      data,
      {
          "reference_wav",
          "reference_text",
          "instruct",
          "seed",
          "anchor",
          "sentence_pause",
          "sentence_turns",
          "chunk_length",
          "max_tokens",
          "temperature",
          "top_p",
          "top_k",
          "segment_gap_ms",
          "verbalize_numerals",
          "numeral_style",
          "max_words_per_call",
          "tracks",
      },
      "tts.local",
    )
  seed_value = data.get("seed")
  if seed_value is None:
    seed: int | None = 70
  elif isinstance(seed_value, int) and not isinstance(seed_value, bool):
    seed = seed_value
  else:
    raise ConfigError("tts.local.seed must be an integer or omitted.")
  pause = str(data.get("sentence_pause", "short")).strip()
  if pause not in {"none", "short", "full"}:
    raise ConfigError('tts.local.sentence_pause must be "none", "short", or "full".')
  style = str(data.get("numeral_style", "plain")).strip()
  if style not in {"plain", "radio"}:
    raise ConfigError('tts.local.numeral_style must be "plain" or "radio".')
  instruct = data.get("instruct")
  return LocalTTSConfig(
      reference_wav=_resolve_relative(
          str(data.get("reference_wav", "")).strip(), config_dir
      ),
      reference_text=_resolve_relative(
          str(data.get("reference_text", "")).strip(), config_dir
      ),
      instruct=(
          instruct.strip()
          if isinstance(instruct, str) and instruct.strip()
          else DEFAULT_NARRATION_INSTRUCT
      ),
      seed=seed,
      anchor=_bool(data.get("anchor"), True, "tts.local.anchor"),
      sentence_pause=pause,
      sentence_turns=_bool(
          data.get("sentence_turns"), False, "tts.local.sentence_turns"
      ),
      chunk_length=_integer(
          data.get("chunk_length"), 300, "tts.local.chunk_length", 32
      ),
      max_tokens=_integer(data.get("max_tokens"), 1024, "tts.local.max_tokens", 1),
      temperature=_number(data.get("temperature"), 0.7, "tts.local.temperature"),
      top_p=_number(data.get("top_p"), 0.7, "tts.local.top_p"),
      top_k=_integer(data.get("top_k"), 30, "tts.local.top_k", 1),
      segment_gap_ms=_number(
          data.get("segment_gap_ms"), 650.0, "tts.local.segment_gap_ms"
      ),
      verbalize_numerals=_bool(
          data.get("verbalize_numerals"), True, "tts.local.verbalize_numerals"
      ),
      numeral_style=style,
      max_words_per_call=_integer(
          data.get("max_words_per_call"),
          0,
          "tts.local.max_words_per_call",
          0,
      ),
      tracks=_load_local_tracks(data.get("tracks"), config_dir),
  )


def _load_local_tracks(raw: Any, config_dir: Path | None) -> tuple[LocalTrackOverride, ...]:
  if raw is None:
    return ()
  if not isinstance(raw, list):
    raise ConfigError("tts.local.tracks must be an array of tables.")
  tracks: list[LocalTrackOverride] = []
  for index, item in enumerate(raw):
    label = f"tts.local.tracks[{index}]"
    data = _table(item, label)
    _only(
        data,
        {
            "chapter",
            "track",
            "max_words_per_call",
            "spoken_replace",
            "patches",
        },
        label,
    )
    chapter = data.get("chapter")
    track = data.get("track")
    if chapter is None and track is None:
      raise ConfigError(f"{label} requires chapter and/or track.")
    if chapter is not None and (
        not isinstance(chapter, int) or isinstance(chapter, bool) or chapter < 1
    ):
      raise ConfigError(f"{label}.chapter must be a positive integer.")
    if track is not None and (
        not isinstance(track, int) or isinstance(track, bool) or track < 1
    ):
      raise ConfigError(f"{label}.track must be a positive integer.")
    max_words = data.get("max_words_per_call")
    if max_words is not None and (
        not isinstance(max_words, int) or isinstance(max_words, bool) or max_words < 0
    ):
      raise ConfigError(f"{label}.max_words_per_call must be a non-negative integer.")
    tracks.append(
        LocalTrackOverride(
            chapter=chapter,
            track=track,
            max_words_per_call=max_words,
            spoken_replace=_load_spoken_replace(
                data.get("spoken_replace"), f"{label}.spoken_replace"
            ),
            patches=_load_local_patches(
                data.get("patches"), f"{label}.patches", config_dir
            ),
        )
    )
  return tuple(tracks)


def _load_spoken_replace(raw: Any, label: str) -> tuple[SpokenReplaceRule, ...]:
  if raw is None:
    return ()
  if not isinstance(raw, list):
    raise ConfigError(f"{label} must be an array of tables.")
  rules: list[SpokenReplaceRule] = []
  for index, item in enumerate(raw):
    entry = f"{label}[{index}]"
    data = _table(item, entry)
    _only(data, {"from", "to"}, entry)
    old = data.get("from")
    new = data.get("to")
    if not isinstance(old, str) or not old:
      raise ConfigError(f"{entry}.from must be a non-empty string.")
    if not isinstance(new, str):
      raise ConfigError(f"{entry}.to must be a string.")
    rules.append(SpokenReplaceRule(old=old, new=new))
  return tuple(rules)


def _load_local_patches(
    raw: Any, label: str, config_dir: Path | None
) -> tuple[LocalAudioPatch, ...]:
  if raw is None:
    return ()
  if not isinstance(raw, list):
    raise ConfigError(f"{label} must be an array of tables.")
  patches: list[LocalAudioPatch] = []
  for index, item in enumerate(raw):
    entry = f"{label}[{index}]"
    data = _table(item, entry)
    _only(
        data,
        {
            "phrase",
            "start",
            "end",
            "replace_unintelligible",
            "allow_duration_change",
            "announcement_text",
        },
        entry,
    )
    phrase = data.get("phrase")
    if not isinstance(phrase, str) or not phrase.strip():
      raise ConfigError(f"{entry}.phrase must be a non-empty path string.")
    start = data.get("start")
    end = data.get("end")
    if not isinstance(start, (int, float)) or isinstance(start, bool):
      raise ConfigError(f"{entry}.start must be a number of seconds.")
    if not isinstance(end, (int, float)) or isinstance(end, bool):
      raise ConfigError(f"{entry}.end must be a number of seconds.")
    if float(end) <= float(start):
      raise ConfigError(f"{entry}.end must be greater than start.")
    announcement = data.get("announcement_text")
    if announcement is not None and not isinstance(announcement, str):
      raise ConfigError(f"{entry}.announcement_text must be a string.")
    patches.append(
        LocalAudioPatch(
            phrase=_resolve_relative(phrase.strip(), config_dir),
            start=float(start),
            end=float(end),
            replace_unintelligible=_bool(
                data.get("replace_unintelligible"),
                True,
                f"{entry}.replace_unintelligible",
            ),
            allow_duration_change=_bool(
                data.get("allow_duration_change"),
                False,
                f"{entry}.allow_duration_change",
            ),
            announcement_text=announcement.strip() if isinstance(announcement, str) else None,
        )
    )
  return tuple(patches)


def _load_project(raw: Any) -> ProjectConfig:
  data = _table(raw, "project")
  _only(data, {"source"}, "project")
  source = str(data.get("source", "epub")).strip()
  if source not in {"epub", "manuscript"}:
    raise ConfigError('project.source must be "epub" or "manuscript".')
  return ProjectConfig(source=source)


def _load_manuscript(raw: Any) -> ManuscriptConfig:
  data = _table(raw, "manuscript")
  _only(
      data,
      {
          "root",
          "chapters",
          "front_matter",
          "back_matter",
          "required_header_keys",
          "extra_header_keys",
          "epub",
      },
      "manuscript",
  )
  keys = _strings(data.get("required_header_keys"), "manuscript.required_header_keys")
  return ManuscriptConfig(
      root=str(data.get("root", "manuscript")).strip() or "manuscript",
      chapters=str(data.get("chapters", "chapters")).strip() or "chapters",
      front_matter=str(data.get("front_matter", "front-matter.md")).strip(),
      back_matter=str(data.get("back_matter", "back-matter.md")).strip(),
      required_header_keys=keys or ("chapter", "title", "words", "status"),
      extra_header_keys=_bool(
          data.get("extra_header_keys"), True, "manuscript.extra_header_keys"
      ),
      epub=str(data.get("epub", "book/dist/book.epub")).strip() or "book/dist/book.epub",
  )


def _load_accessibility(raw: Any) -> AccessibilityConfig:
  data = _table(raw, "accessibility")
  _only(data, {"certified_by", "cover_alt", "summary"}, "accessibility")
  return AccessibilityConfig(
      certified_by=str(data.get("certified_by", "")).strip(),
      cover_alt=str(data.get("cover_alt", "")).strip(),
      summary=str(data.get("summary", "")).strip(),
  )


def write_config_template(
    path: Path,
    metadata: BookMetadata,
    *,
    source: str = "epub",
    manuscript_root: str = "manuscript",
) -> None:
  """Write a documented, editable config without including any API secret."""
  title = json.dumps(metadata.title, ensure_ascii=False)
  authors = ", ".join(json.dumps(author, ensure_ascii=False) for author in metadata.authors)
  language = json.dumps(metadata.language or "", ensure_ascii=False)
  if source == "manuscript":
    template = f'''# Generated by ebook-tts init --manuscript. The markdown tree is the
# source of truth; EPUB and audio are derived editions.

[project]
source = "manuscript"

[book]
title = {title}
authors = [{authors}]
language = {language}

[manuscript]
root = {json.dumps(manuscript_root, ensure_ascii=False)}
chapters = "chapters"
front_matter = "front-matter.md"
back_matter = "back-matter.md"
epub = "book/dist/book.epub"
extra_header_keys = true

[accessibility]
certified_by = ""
cover_alt = ""
summary = ""

[sections]
announce_titles = true
minimum_characters = 1

[tts]
provider = "local"
voice_id = "narrator"
model_id = "mlx-community/fish-audio-s2-pro"
output_format = "mp3_44100_128"
max_characters = 200000
context_characters = 0

[tts.local]
reference_wav = "voices/narrator.wav"
reference_text = "voices/narrator.txt"
anchor = true
sentence_pause = "short"
verbalize_numerals = true
# Book-wide default. Prefer per-chapter exceptions under [[tts.local.tracks]]
# when only a few long chapters need the long-context split.
# max_words_per_call = 0

# Working-config exceptions for this book only (not tool defaults):
# [[tts.local.tracks]]
# chapter = 13
# max_words_per_call = 1100
#
# [[tts.local.tracks]]
# chapter = 15
# spoken_replace = [
#   {{ from = "*Open Channel working group*, lower case, in a footnote", to = "Open-Channel working group, lower-case, in a footnote" }},
# ]
# patches = [
#   {{ phrase = "patches/ch015.wav", start = 25.32, end = 28.88, replace_unintelligible = true }},
# ]

[audio]
ffmpeg = "ffmpeg"
ffprobe = "ffprobe"
genre = "Audiobook"

[qa]
stt_provider = "local"
stt_model = "mlx-community/whisper-large-v3-turbo"
language = "en"
spoken_gate = true
max_internal_silence_seconds = 8.0
clipping_peak_db = -0.1
protected_terms = []

# [[normalization]]
# pattern = "source spelling"
# replacement = "spoken spelling"
# expected_count = 1
'''
  else:
    template = f'''# Generated by ebook-tts init. API keys belong in the environment.

[project]
source = "epub"

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

# Local Apple Silicon rendering instead of ElevenLabs:
# provider = "local"
# voice_id = "narrator"
# model_id = "mlx-community/fish-audio-s2-pro"
# max_characters = 200000
# [tts.local]
# reference_wav = "voices/narrator.wav"
# reference_text = "voices/narrator.txt"

[audio]
ffmpeg = "ffmpeg"
ffprobe = "ffprobe"
genre = "Audiobook"

[qa]
stt_provider = "none" # "elevenlabs" (Scribe) or "local" (Whisper)
stt_model = "scribe_v2"
language = ""
spoken_gate = false
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
