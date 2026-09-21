"""Two-chapter mini ebook: manuscript → EPUB → plan → audio → QA → all packages."""

from __future__ import annotations

import shutil
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from ebook_tts.models import LocalTrackOverride, SpokenReplaceRule
from ebook_tts.config import default_config
from ebook_tts.editions.epub import build_epub
from ebook_tts.manuscript.check import check_manuscript
from ebook_tts.manuscript.compile import publication_from_manuscript
from ebook_tts.manuscript.document import count_prose_words, render_chapter_markdown
from ebook_tts.media.tools import probe_chapters
from ebook_tts.outputs.package import (
    package_archive,
    package_bookplayer,
    package_m4b,
    package_tracks,
)
from ebook_tts.providers.base import (
    SynthesisRequest,
    SynthesisResponse,
    Transcript,
    TranscriptWord,
)
from ebook_tts.qa.evaluate import evaluate_run
from ebook_tts.text.spoken import verbalize_speech_text
from ebook_tts.workspace.generation import generate
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


class FakeSTT:
  name = "fake-stt"

  def __init__(self, transcripts: list[str]) -> None:
    self.transcripts = list(transcripts)
    self.calls = 0
    self._index = 0

  def transcribe(self, audio_path: Path, *, language: str | None) -> Transcript:
    assert audio_path.is_file()
    self.calls += 1
    text = self.transcripts[self._index % len(self.transcripts)]
    self._index += 1
    words = tuple(
        TranscriptWord(token, float(index), float(index) + 0.1, 1.0)
        for index, token in enumerate(text.split())
    )
    return Transcript(text=text, language=language, words=words, raw={"text": text})


def _chapter(number: int, title: str, body: str) -> str:
  return render_chapter_markdown(
      header={
          "chapter": number,
          "title": title,
          "words": count_prose_words(body),
          "status": "draft",
      },
      prose=body,
  )


def _mini_manuscript(root: Path, *, markup_phrase: bool = False) -> None:
  chapters = root / "chapters"
  chapters.mkdir(parents=True)
  (root / "front-matter.md").write_text("# Mini Frontier\n\n", encoding="utf-8")
  (root / "back-matter.md").write_text("", encoding="utf-8")
  footnote = (
      " The footnote named *Open Channel working group* exactly once."
      if markup_phrase
      else ""
  )
  chapter_one = (
      "Ravi Anand opened the log at 07:02 and read the first synthetic paragraph "
      "aloud so the pipeline had enough prose for planning. "
      "The apparatus receives tokens from the page and nothing else is required "
      "for a reliable chapter boundary in this miniature book.\n\n"
      "A second paragraph keeps the chunker honest while the clock reading stays "
      "in the manuscript as digits for the spoken gate to reconcile."
      f"{footnote}\n"
  )
  chapter_two = (
      "The second chapter begins after the first has already established the "
      "protected name Ravi Anand and a numeral that Whisper might speak as words. "
      "This passage exists only to exercise ordering, packaging, and quality "
      "reports across a two-track miniature audiobook.\n\n"
      "When the request went into the queue at 19:52 the operator waited, then "
      "closed the notebook without announcing layout or skipping a sentence.\n"
  )
  (chapters / "001-opening.md").write_text(
      _chapter(1, "Opening", chapter_one),
      encoding="utf-8",
  )
  (chapters / "002-closing.md").write_text(
      _chapter(2, "Closing", chapter_two),
      encoding="utf-8",
  )


def _config(root: Path, epub: Path, *, with_spoken_replace: bool = False):
  base = default_config()
  tracks = ()
  if with_spoken_replace:
    tracks = (
        LocalTrackOverride(
            chapter=1,
            spoken_replace=(
                SpokenReplaceRule(
                    old="*Open Channel working group*",
                    new="Open-Channel working group",
                ),
            ),
        ),
    )
  return replace(
      base,
      project=replace(base.project, source="manuscript"),
      manuscript=replace(base.manuscript, root=str(root), epub=str(epub)),
      book=replace(
          base.book,
          title="Mini Frontier",
          authors=("Example Author",),
          language="en",
      ),
      sections=replace(base.sections, announce_titles=False),
      tts=replace(
          base.tts,
          provider="local" if with_spoken_replace else "fake",
          voice_id="narrator" if with_spoken_replace else "test-voice",
          model_id=(
              "mlx-community/fish-audio-s2-pro"
              if with_spoken_replace
              else base.tts.model_id
          ),
          output_format="mp3_44100_128",
          max_characters=200_000 if with_spoken_replace else 9_500,
          context_characters=0 if with_spoken_replace else base.tts.context_characters,
          local=replace(base.tts.local, tracks=tracks),
      ),
      qa=replace(
          base.qa,
          stt_provider="fake-stt",
          stt_model="offline-test",
          clipping_peak_db=1.0,
          spoken_gate=True,
          protected_terms=("Ravi Anand",),
      ),
      accessibility=replace(
          base.accessibility,
          certified_by="Mini Ebook Test",
          summary="Synthetic two-chapter accessibility fixture.",
      ),
  )


def test_mini_ebook_full_path_to_all_packages(
    tmp_path: Path,
    tone_mp3: bytes,
    media_tools,
) -> None:
  root = tmp_path / "manuscript"
  epub_path = tmp_path / "dist" / "mini-frontier.epub"
  _mini_manuscript(root)
  config = _config(root, epub_path)

  report = check_manuscript(root, config)
  assert report.ok
  assert len(report.documents) == 2

  if shutil.which("pandoc") is None:
    pytest.skip("pandoc is required for the EPUB edition step")
  epub = build_epub(config, cwd=tmp_path)
  assert epub.is_file()

  publication = publication_from_manuscript(root, config)
  assert len(publication.sections) == 2
  assert "07:02" in publication.sections[0].text
  assert "Ravi Anand" in publication.sections[0].text

  plan = create_plan(publication, config, tmp_path / "mini.ebook-tts")
  assert plan.manifest["track_count"] == 2

  provider = FakeTTS(tone_mp3)
  run = generate(plan=plan, config=config, provider=provider, tools=media_tools)
  assert run.manifest["status"] == "complete"
  assert len(provider.calls) >= 2
  audio_files = sorted((run.run_path.parent / "audio").glob("*.mp3"))
  assert len(audio_files) == 2

  references = [
      read_planned_text(plan, chunk["text_file"])
      for section in plan.manifest["sections"]
      for chunk in section["chunks"]
  ]
  spoken = [verbalize_speech_text(text) for text in references]
  stt = FakeSTT(spoken)
  quality = evaluate_run(
      plan=plan,
      run=run,
      config=config,
      stt_provider=stt,
      tools=media_tools,
  )
  assert quality.report["status"] == "pass"
  assert stt.calls == len(references)
  for track in quality.report["tracks"]:
    assert track["transcription"] is not None
    assert track["transcription"]["missing_protected_terms"] == []
    for chunk in track["transcription"]["chunks"]:
      assert chunk["spoken_assessment"]["passed"] is True

  # Cached STT must not re-bill / re-call the provider.
  stt_again = FakeSTT(["should not be used"] * len(references))
  quality_again = evaluate_run(
      plan=plan,
      run=run,
      config=config,
      stt_provider=stt_again,
      tools=media_tools,
  )
  assert quality_again.report["status"] == "pass"
  assert stt_again.calls == 0

  output = tmp_path / "packages"
  bookplayer = package_bookplayer(plan=plan, run=run, output_directory=output)
  archive = package_archive(plan=plan, run=run, output_directory=output)
  tracks = package_tracks(plan=plan, run=run, output_directory=output)
  m4b = package_m4b(plan=plan, run=run, output_directory=output, tools=media_tools)

  assert bookplayer.path.is_file()
  assert archive.path.is_file()
  assert tracks.path.is_dir()
  assert m4b.path.is_file()
  assert m4b.path.suffix == ".m4b"

  with zipfile.ZipFile(bookplayer.path) as archive_zip:
    names = archive_zip.namelist()
    assert names == ["001_opening.mp3", "002_closing.mp3"]
    assert archive_zip.testzip() is None

  chapters = probe_chapters(m4b.path, media_tools.ffprobe)
  assert len(chapters) == 2
  titles = [
      (item.get("tags") or {}).get("title")
      for item in chapters
  ]
  assert titles == ["Opening", "Closing"]

  reused = package_m4b(plan=plan, run=run, output_directory=output, tools=media_tools)
  assert reused.path == m4b.path
  assert reused.sha256 == m4b.sha256


def test_mini_ebook_local_spoken_replace_and_patch(
    tmp_path: Path,
    tone_mp3: bytes,
    media_tools,
) -> None:
  """Local working-config: generation-only replace + verified phrase splice."""
  import numpy as np
  import soundfile as sf
  import subprocess

  from ebook_tts.models import LocalAudioPatch
  from ebook_tts.providers.local import LocalTTSProvider

  root = tmp_path / "manuscript"
  epub_path = tmp_path / "dist" / "mini-frontier.epub"
  _mini_manuscript(root, markup_phrase=True)
  config = _config(root, epub_path, with_spoken_replace=True)

  # Build a short tone MP3 with a quiet middle region suitable for a silent splice.
  rate = 44_100
  left = (0.2 * np.sin(2 * np.pi * 220 * np.arange(int(rate * 0.8)) / rate)).astype(
      np.float32
  )
  hole = np.zeros(int(rate * 0.5), dtype=np.float32)
  right = (0.2 * np.sin(2 * np.pi * 330 * np.arange(int(rate * 0.8)) / rate)).astype(
      np.float32
  )
  wav = tmp_path / "track-source.wav"
  sf.write(wav, np.concatenate([left, hole, right]), rate)
  track_mp3 = tmp_path / "track-source.mp3"
  subprocess.run(
      [
          media_tools.ffmpeg,
          "-hide_banner",
          "-loglevel",
          "error",
          "-y",
          "-i",
          str(wav),
          "-b:a",
          "128k",
          str(track_mp3),
      ],
      check=True,
  )
  phrase = (0.2 * np.sin(2 * np.pi * 440 * np.arange(int(rate * 0.3)) / rate)).astype(
      np.float32
  )
  phrase_path = tmp_path / "patches" / "open-channel.wav"
  phrase_path.parent.mkdir()
  sf.write(phrase_path, phrase, rate)

  config = replace(
      config,
      tts=replace(
          config.tts,
          local=replace(
              config.tts.local,
              tracks=(
                  LocalTrackOverride(
                      chapter=1,
                      spoken_replace=(
                          SpokenReplaceRule(
                              old="*Open Channel working group*",
                              new="Open-Channel working group",
                          ),
                      ),
                      patches=(
                          LocalAudioPatch(
                              phrase=str(phrase_path),
                              start=left.size / rate,
                              end=(left.size + hole.size) / rate,
                              replace_unintelligible=False,
                          ),
                      ),
                  ),
              ),
          ),
      ),
  )

  report = check_manuscript(root, config)
  assert report.ok
  publication = publication_from_manuscript(root, config)
  plan = create_plan(publication, config, tmp_path / "mini-local.ebook-tts")

  seen_turns: list[str] = []

  def renderer(request, turns):
    seen_turns.append(" ".join(turns))
    return track_mp3.read_bytes()

  provider = LocalTTSProvider(config, renderer=renderer)
  run = generate(plan=plan, config=config, provider=provider, tools=media_tools)
  assert run.manifest["status"] == "complete"
  assert any("Open-Channel working group" in text for text in seen_turns)
  assert all("*Open Channel working group*" not in text for text in seen_turns)
  planned = read_planned_text(plan, plan.manifest["sections"][0]["chunks"][0]["text_file"])
  assert "*Open Channel working group*" in planned

  track_state = run.manifest["sections"]["1"]
  patches = track_state["final_audio"].get("production_patches") or []
  assert len(patches) == 1
  assert patches[0]["kind"] == "verified_silent_omission"
