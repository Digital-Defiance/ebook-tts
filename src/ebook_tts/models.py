"""Typed domain models shared by the pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


MANIFEST_VERSION = 1
EXTRACTION_VERSION = 1
NORMALIZATION_VERSION = 1
CHUNKER_VERSION = 3


@dataclass(frozen=True)
class ModelProfile:
  """Known API constraints and safe defaults for one TTS model."""

  model_id: str
  maximum_characters: int
  default_chunk_characters: int
  supports_text_context: bool = True


MODEL_PROFILES: Mapping[str, ModelProfile] = {
    "eleven_multilingual_v2": ModelProfile(
        model_id="eleven_multilingual_v2",
        maximum_characters=10_000,
        default_chunk_characters=9_500,
    ),
    "eleven_v3": ModelProfile(
        model_id="eleven_v3",
        maximum_characters=5_000,
        default_chunk_characters=4_900,
        supports_text_context=False,
    ),
    "eleven_flash_v2_5": ModelProfile(
        model_id="eleven_flash_v2_5",
        maximum_characters=40_000,
        default_chunk_characters=39_000,
    ),
    "eleven_turbo_v2_5": ModelProfile(
        model_id="eleven_turbo_v2_5",
        maximum_characters=40_000,
        default_chunk_characters=39_000,
    ),
    "eleven_flash_v2": ModelProfile(
        model_id="eleven_flash_v2",
        maximum_characters=30_000,
        default_chunk_characters=29_000,
    ),
    "eleven_turbo_v2": ModelProfile(
        model_id="eleven_turbo_v2",
        maximum_characters=30_000,
        default_chunk_characters=29_000,
    ),
}


@dataclass(frozen=True)
class BookMetadata:
  """Publication metadata discovered from OPF with optional overrides."""

  title: str
  authors: tuple[str, ...] = ()
  language: str | None = None
  identifier: str | None = None
  publisher: str | None = None
  description: str | None = None

  @property
  def author_display(self) -> str:
    return ", ".join(self.authors) if self.authors else "Unknown Author"


@dataclass(frozen=True)
class Section:
  """One ordered narratable unit."""

  track_number: int
  title: str
  output_stem: str
  source_href: str
  source_fragment: str | None
  source_sha256: str
  text: str
  text_sha256: str
  chapter_number: int | None = None


@dataclass(frozen=True)
class Publication:
  """A safely parsed EPUB publication."""

  source_path: Path
  source_sha256: str
  metadata: BookMetadata
  sections: tuple[Section, ...]
  cover_bytes: bytes | None = None
  cover_media_type: str | None = None
  cover_extension: str | None = None
  warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class NormalizationRule:
  """One versioned literal or regular-expression replacement."""

  pattern: str
  replacement: str
  regex: bool = False
  expected_count: int | None = None
  flags: str = ""


@dataclass(frozen=True)
class BookOverrides:
  """User-supplied publication metadata overrides."""

  title: str | None = None
  authors: tuple[str, ...] = ()
  language: str | None = None


@dataclass(frozen=True)
class SectionConfig:
  """Selection and naming behavior for EPUB sections."""

  include: tuple[str, ...] = ()
  exclude: tuple[str, ...] = ()
  announce_titles: bool = True
  minimum_characters: int = 1
  title_overrides: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class TTSConfig:
  """Text-to-speech generation settings."""

  provider: str = "elevenlabs"
  voice_id: str = ""
  model_id: str = "eleven_multilingual_v2"
  output_format: str = "mp3_44100_128"
  max_characters: int = 9_500
  context_characters: int = 500
  voice_settings: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AudioConfig:
  """Local media-tool and metadata settings."""

  ffmpeg: str = "ffmpeg"
  ffprobe: str = "ffprobe"
  genre: str = "Audiobook"


@dataclass(frozen=True)
class QAConfig:
  """Signal and transcription quality settings."""

  stt_provider: str = "none"
  stt_model: str = "scribe_v2"
  language: str | None = None
  max_word_error_rate: float | None = None
  max_character_error_rate: float | None = None
  max_internal_silence_seconds: float = 8.0
  clipping_peak_db: float = -0.1
  protected_terms: tuple[str, ...] = ()


@dataclass(frozen=True)
class AppConfig:
  """Complete versioned user configuration."""

  book: BookOverrides = field(default_factory=BookOverrides)
  sections: SectionConfig = field(default_factory=SectionConfig)
  normalization: tuple[NormalizationRule, ...] = ()
  tts: TTSConfig = field(default_factory=TTSConfig)
  audio: AudioConfig = field(default_factory=AudioConfig)
  qa: QAConfig = field(default_factory=QAConfig)


@dataclass(frozen=True)
class Chunk:
  """One bounded TTS request text."""

  index: int
  text: str
  text_sha256: str


@dataclass(frozen=True)
class PreparedSection:
  """A section and its deterministic chunk plan."""

  section: Section
  chunks: tuple[Chunk, ...]


@dataclass(frozen=True)
class Plan:
  """An immutable on-disk plan."""

  workspace: Path
  plan_path: Path
  plan_id: str
  manifest: Mapping[str, Any]


@dataclass(frozen=True)
class MediaInfo:
  """Validated properties of one audio file."""

  codec: str
  sample_rate: int
  bit_rate: int | None
  channels: int
  duration_seconds: float
  bytes: int
  sha256: str
