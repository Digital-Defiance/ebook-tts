"""Typed domain models shared by the pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


MANIFEST_VERSION = 1
EXTRACTION_VERSION = 1
NORMALIZATION_VERSION = 1
CHUNKER_VERSION = 3


DEFAULT_NARRATION_INSTRUCT = (
    "Read clearly and unhurriedly for a listener who cannot see the page. "
    "Let meaning land through timing and emphasis rather than volume. Never "
    "announce layout, skip, or paraphrase."
)

DEFAULT_VOICE_ANCHOR = (
    "Before the chapter begins, I settle into the same chair and place the "
    "notebook squarely on the desk. I take one ordinary breath and read this "
    "line in the voice I use for facts that matter: calm, exact, unhurried, "
    "and close enough to hear the thought behind the words. I do not announce, "
    "perform, or hurry. I let each sentence finish, leave the silence where it "
    "belongs, and begin the next one only when it is ready."
)


@dataclass(frozen=True)
class ModelProfile:
  """Known constraints and safe defaults for one TTS model."""

  model_id: str
  maximum_characters: int
  default_chunk_characters: int
  supports_text_context: bool = True
  billed: bool = True


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
    "mlx-community/fish-audio-s2-pro": ModelProfile(
        model_id="mlx-community/fish-audio-s2-pro",
        maximum_characters=200_000,
        default_chunk_characters=200_000,
        supports_text_context=False,
        billed=False,
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
  cover: str = ""
  """Cover image path, resolved relative to the config file.

  A cover is a stable property of the book, not of one invocation, so it
  belongs here rather than only on a command line. `compile --cover` still
  overrides it for a one-off build.
  """


@dataclass(frozen=True)
class SectionConfig:
  """Selection and naming behavior for EPUB sections."""

  include: tuple[str, ...] = ()
  exclude: tuple[str, ...] = ()
  announce_titles: bool = True
  minimum_characters: int = 1
  title_overrides: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SpokenReplaceRule:
  """Generation-only exact string swap; manuscript and ASR reference stay unchanged."""

  old: str
  new: str


@dataclass(frozen=True)
class LocalAudioPatch:
  """Verified phrase splice for a Fish omission or unintelligible span."""

  phrase: str
  start: float
  end: float
  replace_unintelligible: bool = True
  allow_duration_change: bool = False
  announcement_text: str | None = None


@dataclass(frozen=True)
class LocalTrackOverride:
  """Book-local recovery for one manuscript chapter and/or plan track."""

  chapter: int | None = None
  track: int | None = None
  max_words_per_call: int | None = None
  spoken_replace: tuple[SpokenReplaceRule, ...] = ()
  patches: tuple[LocalAudioPatch, ...] = ()


@dataclass(frozen=True)
class LocalTTSConfig:
  """Session-engine settings for on-device narration."""

  reference_wav: str = ""
  reference_text: str = ""
  instruct: str = DEFAULT_NARRATION_INSTRUCT
  seed: int | None = 70
  anchor: bool = True
  sentence_pause: str = "short"
  sentence_turns: bool = False
  chunk_length: int = 300
  max_tokens: int = 1024
  temperature: float = 0.7
  top_p: float = 0.7
  top_k: int = 30
  segment_gap_ms: float = 650.0
  verbalize_numerals: bool = True
  numeral_style: str = "plain"
  # Split long chapters into separate deterministic generate() calls at paragraph
  # boundaries, each re-anchored with the same seed. 0 keeps a single call (the
  # approved short-chapter behavior). 1100 is the long-context recovery used when
  # a ~2000-word chapter otherwise truncates or refuses tokens mid-prose.
  max_words_per_call: int = 0
  # Per-book working exceptions keyed by chapter and/or track number. Empty means
  # every track uses the book-wide defaults above.
  tracks: tuple[LocalTrackOverride, ...] = ()


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
  local: LocalTTSConfig = field(default_factory=LocalTTSConfig)


@dataclass(frozen=True)
class AudioConfig:
  """Local media-tool and metadata settings."""

  ffmpeg: str = "ffmpeg"
  ffprobe: str = "ffprobe"
  genre: str = "Audiobook"
  # Comfort-tone (or silent) pause folded into each preceding M4B chapter marker
  # so announcements do not run into the next chapter. 0 disables the pad.
  m4b_chapter_gap_ms: float = 2500.0


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
  spoken_gate: bool = False


@dataclass(frozen=True)
class ProjectConfig:
  """Whether the EPUB or a markdown manuscript is the source of truth."""

  source: str = "epub"


@dataclass(frozen=True)
class ManuscriptConfig:
  """Layout and objective-check settings for an authored manuscript."""

  root: str = "manuscript"
  chapters: str = "chapters"
  front_matter: str = "front-matter.md"
  back_matter: str = "back-matter.md"
  required_header_keys: tuple[str, ...] = ("chapter", "title", "words", "status")
  extra_header_keys: bool = True
  epub: str = "book/dist/book.epub"


@dataclass(frozen=True)
class AccessibilityConfig:
  """EPUB Accessibility 1.1 claims written into authored editions."""

  certified_by: str = ""
  cover_alt: str = ""
  summary: str = ""


@dataclass(frozen=True)
class AppConfig:
  """Complete versioned user configuration."""

  book: BookOverrides = field(default_factory=BookOverrides)
  sections: SectionConfig = field(default_factory=SectionConfig)
  normalization: tuple[NormalizationRule, ...] = ()
  tts: TTSConfig = field(default_factory=TTSConfig)
  audio: AudioConfig = field(default_factory=AudioConfig)
  qa: QAConfig = field(default_factory=QAConfig)
  project: ProjectConfig = field(default_factory=ProjectConfig)
  manuscript: ManuscriptConfig = field(default_factory=ManuscriptConfig)
  accessibility: AccessibilityConfig = field(default_factory=AccessibilityConfig)


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
