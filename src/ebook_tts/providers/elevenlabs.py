"""ElevenLabs SDK adapters with no hidden retries."""

from __future__ import annotations

import sys
from importlib import metadata
from pathlib import Path
from typing import Any, Iterator

from ..errors import ProviderError
from .base import (
    SynthesisRequest,
    SynthesisResponse,
    TTSProvider,
    Transcript,
    TranscriptWord,
)


class ElevenLabsTTSProvider(TTSProvider):
  """Raw-response TTS adapter that preserves provider request IDs."""

  name = "elevenlabs"

  def __init__(self, api_key: str) -> None:
    if not api_key:
      raise ProviderError("ELEVENLABS_API_KEY is not set.")
    try:
      from elevenlabs import ElevenLabs
    except ImportError as exc:
      raise ProviderError(
          "Install the ElevenLabs extra: pip install 'ebook-tts[elevenlabs]'"
      ) from exc
    try:
      self.version = metadata.version("elevenlabs")
    except metadata.PackageNotFoundError:
      self.version = "unknown"
    self._client = ElevenLabs(api_key=api_key)

  def synthesize(self, request: SynthesisRequest) -> SynthesisResponse:
    kwargs: dict[str, Any] = {
        "voice_id": request.voice_id,
        "text": request.text,
        "model_id": request.model_id,
        "output_format": request.output_format,
        "request_options": {"max_retries": 0},
    }
    if request.previous_text:
      kwargs["previous_text"] = request.previous_text
    if request.next_text:
      kwargs["next_text"] = request.next_text
    if request.previous_request_ids:
      kwargs["previous_request_ids"] = list(request.previous_request_ids)
    if request.settings:
      kwargs["voice_settings"] = dict(request.settings)

    response_context = self._client.text_to_speech.with_raw_response.convert(**kwargs)
    response = response_context.__enter__()
    headers = getattr(getattr(response, "_response", None), "headers", {})
    request_id = headers.get("request-id") or headers.get("request_id")
    raw_cost = headers.get("character-cost") or headers.get("character_cost")
    try:
      billed_characters = int(raw_cost) if raw_cost is not None else None
    except (TypeError, ValueError):
      billed_characters = None

    def blocks() -> Iterator[bytes]:
      try:
        yield from response.data
      except BaseException:
        response_context.__exit__(*sys.exc_info())
        raise
      else:
        response_context.__exit__(None, None, None)

    return SynthesisResponse(
        blocks=blocks(),
        request_id=str(request_id) if request_id else None,
        billed_characters=billed_characters,
    )


class ElevenLabsSTTProvider:
  """Batch Scribe adapter used by the optional QA phase."""

  name = "elevenlabs"

  def __init__(self, api_key: str, model_id: str = "scribe_v2") -> None:
    if not api_key:
      raise ProviderError("ELEVENLABS_API_KEY is not set.")
    try:
      from elevenlabs import ElevenLabs
    except ImportError as exc:
      raise ProviderError(
          "Install the ElevenLabs extra: pip install 'ebook-tts[elevenlabs]'"
      ) from exc
    try:
      self.version = metadata.version("elevenlabs")
    except metadata.PackageNotFoundError:
      self.version = "unknown"
    self.model_id = model_id
    self._client = ElevenLabs(api_key=api_key)

  def transcribe(self, audio_path: Path, *, language: str | None) -> Transcript:
    try:
      with audio_path.open("rb") as stream:
        kwargs: dict[str, Any] = {
            "model_id": self.model_id,
            "file": stream,
            "timestamps_granularity": "word",
            "diarize": False,
            "request_options": {"max_retries": 0},
        }
        if language:
          kwargs["language_code"] = language
        result = self._client.speech_to_text.convert(**kwargs)
    except OSError as exc:
      raise ProviderError(f"Could not read audio for transcription: {audio_path}") from exc

    def value(item: Any, name: str, default: Any = None) -> Any:
      if isinstance(item, dict):
        return item.get(name, default)
      return getattr(item, name, default)

    words: list[TranscriptWord] = []
    for item in value(result, "words", []) or []:
      if value(item, "type", "word") not in {"word", None}:
        continue
      text = str(value(item, "text", ""))
      if not text.strip():
        continue
      words.append(
          TranscriptWord(
              text=text,
              start=value(item, "start"),
              end=value(item, "end"),
              confidence=value(item, "confidence"),
          )
      )
    if hasattr(result, "model_dump"):
      raw = result.model_dump(mode="json")
    elif isinstance(result, dict):
      raw = result
    else:
      raw = {"text": value(result, "text", "")}
    return Transcript(
        text=str(value(result, "text", "")),
        language=value(result, "language_code"),
        words=tuple(words),
        raw=raw,
    )
