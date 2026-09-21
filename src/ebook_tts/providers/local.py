"""On-device Fish S2 Pro adapter with no hidden retries."""

from __future__ import annotations

import subprocess
import tempfile
from collections.abc import Callable
from importlib import metadata
from pathlib import Path
from typing import Any

from ..errors import ProviderError
from ..models import AppConfig, DEFAULT_VOICE_ANCHOR
from ..utils import sha256_file
from .base import SynthesisRequest, SynthesisResponse, Transcript, TranscriptWord
from .local_turns import narration_turns, plan_spoken_text, tagged_payloads


Renderer = Callable[[SynthesisRequest, tuple[str, ...]], bytes]


class LocalTTSProvider:
  """Session-continuous local TTS. Failures are not treated as billed."""

  name = "local"
  billed = False

  def __init__(
      self,
      config: AppConfig,
      *,
      ffmpeg: str | None = None,
      renderer: Renderer | None = None,
      anchor_sink: Callable[[Any], None] | None = None,
  ) -> None:
    self.config = config
    self.local = config.tts.local
    self.ffmpeg = ffmpeg or config.audio.ffmpeg
    self._renderer = renderer
    self._anchor_sink = anchor_sink
    try:
      self.version = metadata.version("mlx-audio")
    except metadata.PackageNotFoundError:
      self.version = "uninstalled"
    if not config.tts.voice_id:
      raise ProviderError("A voice ID is required. Set tts.voice_id to the reference name.")
    if renderer is None:
      wav = Path(self.local.reference_wav)
      txt = Path(self.local.reference_text)
      if not wav.is_file() or not txt.is_file():
        raise ProviderError(
            "Local TTS needs tts.local.reference_wav and reference_text files "
            "that you have the right to clone."
        )

  def identity_settings(self) -> dict[str, Any]:
    from .local_tracks import tracks_identity

    wav = Path(self.local.reference_wav) if self.local.reference_wav else None
    txt = Path(self.local.reference_text) if self.local.reference_text else None
    return {
        "engine": "fish_s2_pro",
        "reference_wav_sha256": sha256_file(wav) if wav and wav.is_file() else None,
        "reference_text_sha256": sha256_file(txt) if txt and txt.is_file() else None,
        "instruct": self.local.instruct,
        "seed": self.local.seed,
        "anchor": self.local.anchor,
        "anchor_text": DEFAULT_VOICE_ANCHOR if self.local.anchor else None,
        "sentence_pause": self.local.sentence_pause,
        "sentence_turns": self.local.sentence_turns,
        "chunk_length": self.local.chunk_length,
        "max_tokens": self.local.max_tokens,
        "temperature": self.local.temperature,
        "top_p": self.local.top_p,
        "top_k": self.local.top_k,
        "segment_gap_ms": self.local.segment_gap_ms,
        "verbalize_numerals": self.local.verbalize_numerals,
        "numeral_style": self.local.numeral_style,
        "max_words_per_call": self.local.max_words_per_call,
        "tracks": tracks_identity(self.local.tracks),
    }

  def _effective_local(self, request: SynthesisRequest):
    from .local_tracks import effective_local_config, match_track_override

    local_settings = (request.settings or {}).get("local") or {}
    section = local_settings.get("section") or {}
    track_number = section.get("track")
    if track_number is None:
      return self.local, ()
    try:
      override = match_track_override(
          self.local.tracks,
          chapter_number=section.get("chapter"),
          track_number=int(track_number),
      )
    except ValueError as exc:
      raise ProviderError(str(exc)) from exc
    if override is None:
      return self.local, ()
    return effective_local_config(self.local, override), override.spoken_replace

  def synthesize(self, request: SynthesisRequest) -> SynthesisResponse:
    local, spoken_replace = self._effective_local(request)
    try:
      spoken = plan_spoken_text(
          request.text, local, spoken_replace=spoken_replace
      )
    except ValueError as exc:
      raise ProviderError(str(exc)) from exc
    turns = narration_turns(spoken, local)
    if not turns:
      raise ProviderError("Local TTS received empty narration text.")
    try:
      payloads = tagged_payloads(turns, local)
    except ValueError as exc:
      raise ProviderError(str(exc)) from exc
    if self._renderer is not None:
      audio = self._renderer(request, turns)
      return SynthesisResponse(blocks=(audio,), request_id=None, billed_characters=None)
    mp3 = self._render_fish(payloads, request, local=local)
    return SynthesisResponse(blocks=(mp3,), request_id=None, billed_characters=None)

  def _render_fish(
      self,
      payloads: tuple[str, ...],
      request: SynthesisRequest,
      *,
      local: Any = None,
  ) -> bytes:
    local = local or self.local
    try:
      import numpy as np
      import soundfile as sf
      from mlx_audio.tts.utils import load_model
    except ImportError as exc:
      raise ProviderError(
          "Install the local extra on Apple Silicon: pip install 'ebook-tts[local]'"
      ) from exc
    from .local_assembly import assemble_session

    try:
      import mlx.core as mx
    except ImportError as exc:
      raise ProviderError("mlx is required for local Fish rendering.") from exc

    from .local_io import load_reference

    model = load_model(request.model_id)
    rate = int(getattr(model, "sample_rate", 44100))
    reference = load_reference(Path(local.reference_wav), rate)
    ref_text = Path(local.reference_text).read_text(encoding="utf-8").strip()
    segments = []
    for payload in payloads:
      if local.seed is not None:
        mx.random.seed(local.seed)
      skip_anchor = local.anchor
      speech_before = len(segments)
      for segment in model.generate(
          text=payload,
          ref_audio=reference,
          ref_text=ref_text,
          instruct=local.instruct,
          chunk_length=local.chunk_length,
          max_tokens=local.max_tokens,
          temperature=local.temperature,
          top_p=local.top_p,
          top_k=local.top_k,
          verbose=False,
      ):
        audio = np.asarray(segment.audio, dtype=np.float32)
        if skip_anchor:
          skip_anchor = False
          if self._anchor_sink is not None:
            self._anchor_sink(audio)
          continue
        segments.append(audio)
      if len(segments) == speech_before:
        raise ProviderError("Local TTS generate() call yielded no speech audio.")
    joined = assemble_session(segments, rate, local.segment_gap_ms)
    return self._encode_mp3(joined, rate, request.output_format)

  def _encode_mp3(self, audio: Any, rate: int, output_format: str) -> bytes:
    try:
      import soundfile as sf
    except ImportError as exc:
      raise ProviderError("soundfile is required to encode local audio.") from exc
    from ..media.tools import expected_audio_format

    codec, sample_rate, bit_rate = expected_audio_format(output_format)
    if codec != "mp3":
      raise ProviderError("Local generation currently encodes MP3 output only.")
    with tempfile.TemporaryDirectory(prefix="ebook-tts-local-") as raw:
      wav_path = Path(raw) / "segment.wav"
      mp3_path = Path(raw) / "segment.mp3"
      sf.write(str(wav_path), audio, rate, subtype="PCM_16")
      command = [
          self.ffmpeg,
          "-hide_banner",
          "-loglevel",
          "error",
          "-y",
          "-i",
          str(wav_path),
          "-ar",
          str(sample_rate),
          "-b:a",
          str(bit_rate),
          str(mp3_path),
      ]
      result = subprocess.run(command, capture_output=True, text=True, check=False)
      if result.returncode != 0 or not mp3_path.is_file():
        raise ProviderError(
            f"ffmpeg could not encode local audio: {(result.stderr or result.stdout).strip()}"
        )
      return mp3_path.read_bytes()


class LocalSTTProvider:
  """mlx-whisper adapter used by optional local QA."""

  name = "local"
  billed = False

  def __init__(self, model_id: str = "mlx-community/whisper-large-v3-turbo") -> None:
    self.model_id = model_id
    try:
      self.version = metadata.version("mlx-whisper")
    except metadata.PackageNotFoundError:
      self.version = "uninstalled"

  def transcribe(self, audio_path: Path, *, language: str | None) -> Transcript:
    try:
      import mlx_whisper
    except ImportError as exc:
      raise ProviderError(
          "Install the local extra on Apple Silicon: pip install 'ebook-tts[local]'"
      ) from exc
    result = mlx_whisper.transcribe(
        str(audio_path),
        path_or_hf_repo=self.model_id,
        language=language or "en",
        verbose=False,
        condition_on_previous_text=False,
    )
    text = str(result.get("text") or "").strip()
    words: list[TranscriptWord] = []
    for segment in result.get("segments") or []:
      for item in segment.get("words") or []:
        token = str(item.get("word") or item.get("text") or "").strip()
        if not token:
          continue
        words.append(
            TranscriptWord(
                text=token,
                start=item.get("start"),
                end=item.get("end"),
                confidence=item.get("probability"),
            )
        )
    return Transcript(
        text=text,
        language=language or "en",
        words=tuple(words),
        raw=result if isinstance(result, dict) else {"text": text},
    )
