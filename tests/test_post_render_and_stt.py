"""Post-render local assembly and STT evaluation edges."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from ebook_tts.config import default_config
from ebook_tts.errors import QualityError
from ebook_tts.epub.package import load_publication
from ebook_tts.providers.base import Transcript, TranscriptWord
from ebook_tts.qa.alignment import missing_protected_terms
from ebook_tts.qa.evaluate import evaluate_run
from ebook_tts.workspace.generation import generate
from ebook_tts.workspace.manifests import create_plan, read_planned_text


np = pytest.importorskip("numpy")


def test_flatten_slope_reduces_loudness_drift() -> None:
  from ebook_tts.providers.local_assembly import flatten_slope

  rate = 16_000
  t = np.linspace(0, 2.0, rate * 2, endpoint=False)
  # Rising amplitude envelope over voiced frames.
  envelope = 0.05 + 0.45 * (t / t[-1])
  audio = (np.sin(2 * np.pi * 220 * t) * envelope).astype(np.float32)
  flattened, correction = flatten_slope(audio, rate)
  assert flattened.dtype == np.float32
  assert flattened.shape == audio.shape
  assert abs(correction) > 0.0
  assert float(np.abs(flattened).max()) <= 0.99 + 1e-5


def test_match_levels_pulls_quiet_segment_toward_median() -> None:
  from ebook_tts.providers.local_assembly import active_rms, match_levels

  rate = 16_000
  t = np.linspace(0, 1.0, rate, endpoint=False)
  loud = (np.sin(2 * np.pi * 180 * t) * 0.4).astype(np.float32)
  quiet = (np.sin(2 * np.pi * 180 * t) * 0.1).astype(np.float32)
  adjusted, gains, target = match_levels([loud, quiet], rate)
  assert target > 0
  assert gains[1] > 0
  assert active_rms(adjusted[1], rate) > active_rms(quiet, rate)


def test_assemble_session_inserts_gap_between_segments() -> None:
  from ebook_tts.providers.local_assembly import assemble_session

  rate = 16_000
  left = (np.sin(np.linspace(0, 40, rate)).astype(np.float32) * 0.2)
  right = (np.sin(np.linspace(0, 55, rate)).astype(np.float32) * 0.25)
  joined = assemble_session([left, right], rate, gap_ms=120.0)
  assert joined.dtype == np.float32
  # Comfort gap may be silence when no floor can be harvested; length still grows.
  assert joined.size >= left.size + right.size
  assert float(np.abs(joined).max()) <= 0.99 + 1e-5


def test_assemble_session_rejects_empty_input() -> None:
  from ebook_tts.providers.local_assembly import assemble_session

  with pytest.raises(RuntimeError, match="no audio segments"):
    assemble_session([], 16_000, gap_ms=50.0)


def test_comfort_gap_returns_silence_without_donor_floor() -> None:
  from ebook_tts.providers.local_assembly import comfort_gap

  rate = 8_000
  tone = (np.sin(np.linspace(0, 20, rate)).astype(np.float32) * 0.3)
  gap = comfort_gap([tone], samples=rate // 10, rate=rate, seed=7)
  assert gap.shape == (rate // 10,)
  assert float(np.abs(gap).max()) == 0.0


def test_missing_protected_terms_detects_absent_names() -> None:
  assert missing_protected_terms("Ravi said hello.", ["Ravi Anand"]) == ("Ravi Anand",)
  assert missing_protected_terms("Ravi Anand said hello.", ["Ravi Anand"]) == ()


def test_stt_cache_and_protected_term_failure(
    epub_factory,
    tmp_path: Path,
    tone_mp3: bytes,
    media_tools,
) -> None:
  from ebook_tts.providers.base import SynthesisRequest, SynthesisResponse

  class FakeTTS:
    name = "fake"
    version = "test"

    def synthesize(self, request: SynthesisRequest) -> SynthesisResponse:
      return SynthesisResponse(blocks=(tone_mp3,), request_id="1", billed_characters=1)

  class CountingSTT:
    name = "fake-stt"

    def __init__(self, text: str) -> None:
      self.text = text
      self.calls = 0

    def transcribe(self, audio_path: Path, *, language: str | None) -> Transcript:
      self.calls += 1
      words = tuple(
          TranscriptWord(token, float(i), float(i) + 0.1, 1.0)
          for i, token in enumerate(self.text.split())
      )
      return Transcript(text=self.text, language=language, words=words, raw={})

  base = default_config()
  config = replace(
      base,
      tts=replace(
          base.tts,
          provider="fake",
          voice_id="test-voice",
          output_format="mp3_44100_128",
      ),
      qa=replace(
          base.qa,
          stt_provider="fake-stt",
          stt_model="offline-test",
          clipping_peak_db=1.0,
          spoken_gate=False,
          protected_terms=("synthetic paragraph",),
      ),
  )
  publication = load_publication(
      epub_factory(include_cover=False, text_repeat=2),
      config,
  )
  # Ensure the protected phrase appears in at least one planned reference.
  plan = create_plan(publication, config, tmp_path / "workspace")
  run = generate(plan=plan, config=config, provider=FakeTTS(), tools=media_tools)
  references = [
      read_planned_text(plan, chunk["text_file"])
      for section in plan.manifest["sections"]
      for chunk in section["chunks"]
  ]
  assert any("synthetic paragraph" in text.casefold() for text in references)

  failing = CountingSTT("a short wrong transcript")
  quality = evaluate_run(
      plan=plan,
      run=run,
      config=config,
      stt_provider=failing,
      tools=media_tools,
  )
  assert failing.calls == len(references)
  missing = []
  for track in quality.report["tracks"]:
    missing.extend(track["transcription"]["missing_protected_terms"])
  assert "synthetic paragraph" in missing

  cached = CountingSTT("unused")
  evaluate_run(
      plan=plan,
      run=run,
      config=config,
      stt_provider=cached,
      tools=media_tools,
  )
  assert cached.calls == 0


def test_ambiguous_stt_attempt_blocks_retry(
    epub_factory,
    tmp_path: Path,
    tone_mp3: bytes,
    media_tools,
) -> None:
  from ebook_tts.providers.base import SynthesisRequest, SynthesisResponse

  class FakeTTS:
    name = "fake"
    version = "test"

    def synthesize(self, request: SynthesisRequest) -> SynthesisResponse:
      return SynthesisResponse(blocks=(tone_mp3,), request_id="1", billed_characters=1)

  class BoomSTT:
    name = "fake-stt"

    def transcribe(self, audio_path: Path, *, language: str | None) -> Transcript:
      raise RuntimeError("transport lost after billing")

  base = default_config()
  config = replace(
      base,
      tts=replace(
          base.tts, provider="fake", voice_id="test-voice", output_format="mp3_44100_128"
      ),
      qa=replace(
          base.qa,
          stt_provider="fake-stt",
          stt_model="offline-test",
          clipping_peak_db=1.0,
      ),
  )
  publication = load_publication(epub_factory(include_cover=False, text_repeat=1), config)
  plan = create_plan(publication, config, tmp_path / "workspace")
  run = generate(plan=plan, config=config, provider=FakeTTS(), tools=media_tools)

  with pytest.raises(QualityError, match="Ambiguous billing evidence"):
    evaluate_run(
        plan=plan,
        run=run,
        config=config,
        stt_provider=BoomSTT(),
        tools=media_tools,
    )
  attempts = list(plan.workspace.rglob("*.attempt.json"))
  assert attempts

  class NeverCalled:
    name = "fake-stt"

    def transcribe(self, audio_path: Path, *, language: str | None) -> Transcript:
      raise AssertionError("STT must not be reissued while an attempt marker remains")

  with pytest.raises(QualityError, match="ambiguous outcome"):
    evaluate_run(
        plan=plan,
        run=run,
        config=config,
        stt_provider=NeverCalled(),
        tools=media_tools,
    )
