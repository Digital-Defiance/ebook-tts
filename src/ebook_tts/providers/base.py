"""Provider-neutral synthesis and transcription interfaces."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol


@dataclass(frozen=True)
class SynthesisRequest:
  """One immutable provider request."""

  text: str
  voice_id: str
  model_id: str
  output_format: str
  previous_text: str = ""
  next_text: str = ""
  previous_request_ids: tuple[str, ...] = ()
  settings: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SynthesisResponse:
  """A streamed audio response and provider trace identifier."""

  blocks: Iterable[bytes]
  request_id: str | None = None
  billed_characters: int | None = None


@dataclass(frozen=True)
class TranscriptWord:
  """One timestamped transcription token."""

  text: str
  start: float | None = None
  end: float | None = None
  confidence: float | None = None


@dataclass(frozen=True)
class Transcript:
  """Normalized provider transcription result."""

  text: str
  language: str | None
  words: tuple[TranscriptWord, ...]
  raw: Mapping[str, Any]


class TTSProvider(Protocol):
  """A provider capable of one text-to-speech request."""

  name: str

  def synthesize(self, request: SynthesisRequest) -> SynthesisResponse:
    """Submit one request without internal retries."""


class STTProvider(Protocol):
  """A provider capable of transcribing one local audio file."""

  name: str

  def transcribe(self, audio_path: Path, *, language: str | None) -> Transcript:
    """Return text and any available word timings without internal retries."""
