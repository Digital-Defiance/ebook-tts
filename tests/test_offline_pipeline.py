from __future__ import annotations

import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from ebook_tts.config import default_config
from ebook_tts.epub.package import load_publication
from ebook_tts.errors import AmbiguousRequestError
from ebook_tts.outputs.package import package_archive, package_bookplayer, package_tracks
from ebook_tts.providers.base import (
    SynthesisRequest,
    SynthesisResponse,
    Transcript,
    TranscriptWord,
)
from ebook_tts.qa.evaluate import evaluate_run
from ebook_tts.workspace.generation import (
    approve_sample,
    approved_sample,
    authorize_retry,
    generate,
    generate_sample,
)
from ebook_tts.workspace.manifests import create_plan, read_planned_text


class FakeTTS:
  name = "fake"
  version = "test"

  def __init__(self, audio: bytes) -> None:
    self.audio = audio
    self.calls: list[SynthesisRequest] = []

  def synthesize(self, request: SynthesisRequest) -> SynthesisResponse:
    self.calls.append(request)
    return SynthesisResponse(
        blocks=(self.audio,),
        request_id=f"fake-{len(self.calls)}",
        billed_characters=len(request.text),
    )


class FailingTTS(FakeTTS):
  def synthesize(self, request: SynthesisRequest) -> SynthesisResponse:
    self.calls.append(request)
    raise RuntimeError("simulated transport loss")


class FakeSTT:
  name = "fake-stt"

  def __init__(self, transcripts: list[str]) -> None:
    self.transcripts = iter(transcripts)
    self.calls = 0

  def transcribe(self, audio_path: Path, *, language: str | None) -> Transcript:
    assert audio_path.is_file()
    self.calls += 1
    text = next(self.transcripts)
    words = tuple(
        TranscriptWord(token, float(index), float(index) + 0.1, 1.0)
        for index, token in enumerate(text.split())
    )
    return Transcript(text=text, language=language, words=words, raw={"text": text})


def _config():
  base = default_config()
  return replace(
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
      ),
  )


def _plan(epub_factory, tmp_path: Path):
  config = _config()
  publication = load_publication(
      epub_factory(include_cover=False, text_repeat=2),
      config,
  )
  return create_plan(publication, config, tmp_path / "workspace"), config


def test_offline_generation_qa_resume_and_all_packages(
    epub_factory,
    tmp_path: Path,
    tone_mp3: bytes,
    media_tools,
) -> None:
  plan, config = _plan(epub_factory, tmp_path)
  provider = FakeTTS(tone_mp3)
  run = generate(plan=plan, config=config, provider=provider, tools=media_tools)
  assert run.manifest["status"] == "complete"
  assert len(provider.calls) == 2
  assert len(list((run.run_path.parent / "audio").glob("*.mp3"))) == 2

  resumed_provider = FakeTTS(tone_mp3)
  resumed = generate(
      plan=plan,
      config=config,
      provider=resumed_provider,
      tools=media_tools,
  )
  assert resumed.run_id == run.run_id
  assert resumed_provider.calls == []

  references = [
      read_planned_text(plan, chunk["text_file"])
      for section in plan.manifest["sections"]
      for chunk in section["chunks"]
  ]
  stt = FakeSTT(references)
  quality = evaluate_run(
      plan=plan,
      run=run,
      config=config,
      stt_provider=stt,
      tools=media_tools,
  )
  assert quality.report["status"] == "pass"
  assert quality.report["transcription"]["word_error_rate"] == 0
  assert quality.html_path.is_file()
  assert stt.calls == 2

  output = tmp_path / "dist"
  bookplayer = package_bookplayer(plan=plan, run=run, output_directory=output)
  archive = package_archive(plan=plan, run=run, output_directory=output)
  tracks = package_tracks(plan=plan, run=run, output_directory=output)
  assert bookplayer.path.is_file()
  assert archive.path.is_file()
  assert tracks.path.is_dir()
  with zipfile.ZipFile(bookplayer.path) as value:
    assert value.namelist() == [
        "001_chapter_one.mp3",
        "002_chapter_two.mp3",
    ]
    assert value.testzip() is None
  with zipfile.ZipFile(archive.path) as value:
    names = value.namelist()
    assert any(name.endswith("/manifest.json") for name in names)
    assert any(name.endswith("/SHA256SUMS") for name in names)
    assert any(name.endswith("/qa/report.json") for name in names)
    assert not any("text/" in name or "transcript" in name for name in names)


def test_ambiguous_generation_is_blocked_until_explicit_authorization(
    epub_factory,
    tmp_path: Path,
    tone_mp3: bytes,
    media_tools,
) -> None:
  plan, config = _plan(epub_factory, tmp_path)
  failing = FailingTTS(tone_mp3)
  with pytest.raises(AmbiguousRequestError):
    generate(plan=plan, config=config, provider=failing, tools=media_tools)
  attempts = list(plan.workspace.rglob("*.attempt.json"))
  assert len(attempts) == 1

  blocked = FakeTTS(tone_mp3)
  with pytest.raises(AmbiguousRequestError):
    generate(plan=plan, config=config, provider=blocked, tools=media_tools)
  assert blocked.calls == []

  attempt = attempts[0].relative_to(plan.workspace)
  quarantine = authorize_retry(
      plan=plan,
      attempt_path=attempt,
      reason="No matching request in provider history",
  )
  assert (quarantine / "authorization.json").is_file()
  completed = generate(
      plan=plan,
      config=config,
      provider=FakeTTS(tone_mp3),
      tools=media_tools,
  )
  assert completed.manifest["status"] == "complete"


def test_voice_sample_must_be_explicitly_approved(
    epub_factory,
    tmp_path: Path,
    tone_mp3: bytes,
    media_tools,
) -> None:
  plan, config = _plan(epub_factory, tmp_path)
  sample = generate_sample(
      plan=plan,
      config=config,
      provider=FakeTTS(tone_mp3),
      characters=300,
      tools=media_tools,
  )
  assert sample.audio_path.is_file()
  assert not sample.approved
  assert approved_sample(plan, config, "fake") is None
  approved = approve_sample(plan, sample.sample_id)
  assert approved.approved
  assert approved_sample(plan, config, "fake") is not None
