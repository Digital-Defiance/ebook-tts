from __future__ import annotations

import os
import zipfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest

from ebook_tts.config import default_config
from ebook_tts.epub.package import load_publication
from ebook_tts.errors import (
    AmbiguousRequestError,
    PackagingError,
    WorkspaceError,
)
from ebook_tts.outputs.package import (
    package_bookplayer,
    package_tracks,
)
from ebook_tts.providers.base import SynthesisRequest, SynthesisResponse
from ebook_tts.qa.evaluate import evaluate_run
from ebook_tts.utils import atomic_write_json, canonical_json, load_json, sha256_text
from ebook_tts.workspace import generation as generation_module
from ebook_tts.workspace.generation import (
    GenerationRun,
    authorize_retry,
    generate,
)
from ebook_tts.workspace.locking import WorkspaceLock
from ebook_tts.workspace.manifests import create_plan, load_plan


class _FakeTTS:
  name = "fake"
  version = "hardening-test"

  def __init__(self, audio: bytes) -> None:
    self.audio = audio
    self.calls: list[SynthesisRequest] = []

  def synthesize(self, request: SynthesisRequest) -> SynthesisResponse:
    self.calls.append(request)
    return SynthesisResponse(
        blocks=(self.audio,),
        request_id=f"hardening-{len(self.calls)}",
        billed_characters=len(request.text),
    )


class _FailingTTS(_FakeTTS):
  def synthesize(self, request: SynthesisRequest) -> SynthesisResponse:
    self.calls.append(request)
    raise RuntimeError("simulated ambiguous transport failure")


@dataclass(frozen=True)
class _NativeFixture:
  plan: Any
  config: Any
  run: GenerationRun
  audio: bytes


@dataclass(frozen=True)
class _AttemptFixture:
  plan: Any
  config: Any
  marker: Path


def _config():
  base = default_config()
  return replace(
      base,
      tts=replace(
          base.tts,
          provider="fake",
          voice_id="hardening-voice",
          output_format="mp3_44100_128",
      ),
      qa=replace(
          base.qa,
          stt_provider="none",
          clipping_peak_db=1.0,
      ),
  )


def _plan(epub_factory, tmp_path: Path):
  config = _config()
  publication = load_publication(
      epub_factory(include_cover=False, text_repeat=1),
      config,
  )
  return create_plan(publication, config, tmp_path / "workspace"), config


@pytest.fixture
def native_fixture(
    epub_factory,
    tmp_path: Path,
    tone_mp3: bytes,
    media_tools,
) -> _NativeFixture:
  plan, config = _plan(epub_factory, tmp_path)
  run = generate(
      plan=plan,
      config=config,
      provider=_FakeTTS(tone_mp3),
      tools=media_tools,
  )
  return _NativeFixture(plan, config, run, tone_mp3)


@pytest.fixture
def attempt_fixture(
    epub_factory,
    tmp_path: Path,
    tone_mp3: bytes,
    media_tools,
) -> _AttemptFixture:
  plan, config = _plan(epub_factory, tmp_path)
  with pytest.raises(AmbiguousRequestError):
    generate(
        plan=plan,
        config=config,
        provider=_FailingTTS(tone_mp3),
        tools=media_tools,
    )
  markers = list((plan.workspace / "runs").rglob("*.attempt.json"))
  assert len(markers) == 1
  return _AttemptFixture(plan, config, markers[0])


def _relative_marker(value: _AttemptFixture) -> Path:
  return value.marker.relative_to(value.plan.workspace)


def _quality(value: _NativeFixture, media_tools):
  return evaluate_run(
      plan=value.plan,
      run=value.run,
      config=value.config,
      stt_provider=None,
      tools=media_tools,
  )


def test_workspace_lock_releases_and_reacquires_nonempty_file(
    tmp_path: Path,
) -> None:
  workspace = tmp_path / "workspace"
  workspace.mkdir()
  lock_path = workspace / ".lock"
  lock_path.write_bytes(b"stale-lock-owner\n" * 1_024)

  for _ in range(3):
    with WorkspaceLock(workspace):
      pass
    assert lock_path.read_text(encoding="utf-8").startswith("pid=")

  with pytest.raises(RuntimeError, match="sentinel body failure"):
    with WorkspaceLock(workspace):
      raise RuntimeError("sentinel body failure")

  with WorkspaceLock(workspace):
    pass


def test_paid_generation_rejects_tampered_plan_text_before_provider_call(
    epub_factory,
    tmp_path: Path,
    tone_mp3: bytes,
    media_tools,
) -> None:
  plan, config = _plan(epub_factory, tmp_path)
  chunk = plan.plan_path.parent / plan.manifest["sections"][0]["chunks"][0]["text_file"]
  original = chunk.read_text(encoding="utf-8")
  assert original.endswith("\n")
  chunk.write_text(original[:-1] + "tampered\n", encoding="utf-8")
  provider = _FakeTTS(tone_mp3)

  with pytest.raises(WorkspaceError, match="hash|character"):
    generate(plan=plan, config=config, provider=provider, tools=media_tools)
  assert provider.calls == []
  with pytest.raises(WorkspaceError, match="hash|character"):
    load_plan(plan.workspace)


def test_plan_loader_rejects_symlinked_artifact(
    epub_factory,
    tmp_path: Path,
) -> None:
  plan, _ = _plan(epub_factory, tmp_path)
  chunk = plan.plan_path.parent / plan.manifest["sections"][0]["chunks"][0]["text_file"]
  replacement = tmp_path / "same-content.txt"
  replacement.write_bytes(chunk.read_bytes())
  chunk.unlink()
  try:
    chunk.symlink_to(replacement)
  except OSError as exc:
    pytest.skip(f"symbolic links are unavailable: {exc}")

  with pytest.raises(WorkspaceError, match="symbolic link"):
    load_plan(plan.workspace)


@pytest.mark.parametrize("unsafe", [Path("../outside.attempt.json"), Path("/tmp/outside.attempt.json")])
def test_retry_authorization_rejects_absolute_and_traversal_paths(
    attempt_fixture: _AttemptFixture,
    unsafe: Path,
) -> None:
  with pytest.raises(WorkspaceError, match="relative path"):
    authorize_retry(
        plan=attempt_fixture.plan,
        attempt_path=unsafe,
        reason="provider history checked",
    )
  assert attempt_fixture.marker.is_file()


def test_retry_authorization_rejects_symlinked_marker(
    attempt_fixture: _AttemptFixture,
    tmp_path: Path,
) -> None:
  evidence = attempt_fixture.marker.read_bytes()
  outside = tmp_path / "outside-attempt.json"
  outside.write_bytes(evidence)
  attempt_fixture.marker.unlink()
  try:
    attempt_fixture.marker.symlink_to(outside)
  except OSError as exc:
    pytest.skip(f"symbolic links are unavailable: {exc}")

  with pytest.raises(WorkspaceError, match="symbolic link"):
    authorize_retry(
        plan=attempt_fixture.plan,
        attempt_path=_relative_marker(attempt_fixture),
        reason="provider history checked",
    )
  assert outside.read_bytes() == evidence


def test_retry_authorization_rejects_untrusted_partial_path(
    attempt_fixture: _AttemptFixture,
    tmp_path: Path,
) -> None:
  victim = tmp_path / "victim.part"
  victim.write_bytes(b"must remain")
  marker = load_json(attempt_fixture.marker)
  marker["partial_file"] = "../../../../victim.part"
  atomic_write_json(attempt_fixture.marker, marker)

  with pytest.raises(WorkspaceError, match="unsafe partial"):
    authorize_retry(
        plan=attempt_fixture.plan,
        attempt_path=_relative_marker(attempt_fixture),
        reason="provider history checked",
    )
  assert victim.read_bytes() == b"must remain"
  assert attempt_fixture.marker.is_file()


def test_retry_authorization_obeys_workspace_lock(
    attempt_fixture: _AttemptFixture,
) -> None:
  with WorkspaceLock(attempt_fixture.plan.workspace):
    with pytest.raises(WorkspaceError, match="Another ebook-tts process"):
      authorize_retry(
          plan=attempt_fixture.plan,
          attempt_path=_relative_marker(attempt_fixture),
          reason="provider history checked",
      )
  assert attempt_fixture.marker.is_file()


def test_retry_authorization_rolls_back_partial_when_marker_move_fails(
    attempt_fixture: _AttemptFixture,
    monkeypatch,
) -> None:
  marker_record = load_json(attempt_fixture.marker)
  partial = attempt_fixture.marker.parent / marker_record["partial_file"]
  partial.write_bytes(b"preserved partial evidence")
  real_replace = os.replace

  def failing_replace(source, destination):
    if Path(source) == attempt_fixture.marker:
      raise OSError("simulated marker move failure")
    return real_replace(source, destination)

  monkeypatch.setattr(generation_module.os, "replace", failing_replace)
  with pytest.raises(WorkspaceError, match="original evidence was restored"):
    authorize_retry(
        plan=attempt_fixture.plan,
        attempt_path=_relative_marker(attempt_fixture),
        reason="provider history checked",
    )

  assert attempt_fixture.marker.is_file()
  assert partial.read_bytes() == b"preserved partial evidence"
  quarantine = attempt_fixture.marker.parent / "quarantine"
  assert not quarantine.exists() or not any(quarantine.iterdir())


def test_resume_rejects_rehashed_forged_metadata_before_provider_call(
    native_fixture: _NativeFixture,
    media_tools,
) -> None:
  manifest = load_json(native_fixture.run.run_path)
  state = manifest["sections"]["1"]
  assembly = state["final_audio"]["assembly"]
  assembly["metadata"]["genre"] = "Forged Genre"
  state["final_audio"]["assembly_sha256"] = sha256_text(canonical_json(assembly))
  atomic_write_json(native_fixture.run.run_path, manifest)
  provider = _FakeTTS(native_fixture.audio)

  with pytest.raises(WorkspaceError, match="assembly inputs"):
    generate(
        plan=native_fixture.plan,
        config=native_fixture.config,
        provider=provider,
        tools=media_tools,
    )
  assert provider.calls == []


def test_resume_refuses_uncheckpointed_existing_final_without_paid_retry(
    native_fixture: _NativeFixture,
    media_tools,
) -> None:
  manifest = load_json(native_fixture.run.run_path)
  state = manifest["sections"]["1"]
  final_path = native_fixture.run.run_path.parent / "audio" / state["final_audio"]["file"]
  state["status"] = "in_progress"
  state.pop("final_audio")
  manifest["status"] = "partial"
  manifest["completed_tracks"] = 1
  atomic_write_json(native_fixture.run.run_path, manifest)
  provider = _FakeTTS(native_fixture.audio)

  with pytest.raises(WorkspaceError, match="Uncheckpointed final audio"):
    generate(
        plan=native_fixture.plan,
        config=native_fixture.config,
        provider=provider,
        tools=media_tools,
    )
  assert provider.calls == []
  assert final_path.is_file()


def test_packaging_rejects_qa_for_a_changed_run_manifest(
    native_fixture: _NativeFixture,
    media_tools,
    tmp_path: Path,
) -> None:
  _quality(native_fixture, media_tools)
  manifest = load_json(native_fixture.run.run_path)
  manifest["updated_at"] = "2099-01-01T00:00:00+00:00"
  atomic_write_json(native_fixture.run.run_path, manifest)

  with pytest.raises(PackagingError, match="No QA report matches"):
    package_bookplayer(
        plan=native_fixture.plan,
        run=native_fixture.run,
        output_directory=tmp_path / "dist",
    )


def test_packaging_rejects_forged_qa_track_hash(
    native_fixture: _NativeFixture,
    media_tools,
    tmp_path: Path,
) -> None:
  quality = _quality(native_fixture, media_tools)
  report = load_json(quality.report_path)
  report["tracks"][0]["audio_sha256"] = "0" * 64
  atomic_write_json(quality.report_path, report)

  with pytest.raises(PackagingError, match="track does not match"):
    package_bookplayer(
        plan=native_fixture.plan,
        run=native_fixture.run,
        output_directory=tmp_path / "dist",
    )


def test_bookplayer_reuse_rehashes_every_zip_member(
    native_fixture: _NativeFixture,
    media_tools,
    tmp_path: Path,
) -> None:
  _quality(native_fixture, media_tools)
  output = tmp_path / "dist"
  first = package_bookplayer(
      plan=native_fixture.plan,
      run=native_fixture.run,
      output_directory=output,
  )
  reused = package_bookplayer(
      plan=native_fixture.plan,
      run=native_fixture.run,
      output_directory=output,
  )
  assert reused.path == first.path
  assert reused.sha256 == first.sha256

  with zipfile.ZipFile(first.path, "r") as archive:
    members = [(name, archive.read(name)) for name in archive.namelist()]
  with zipfile.ZipFile(first.path, "w", compression=zipfile.ZIP_STORED) as archive:
    for index, (name, payload) in enumerate(members):
      archive.writestr(name, b"corrupt" if index == 0 else payload)

  with pytest.raises(PackagingError, match="metadata|stale or corrupt"):
    package_bookplayer(
        plan=native_fixture.plan,
        run=native_fixture.run,
        output_directory=output,
    )


def test_tracks_reuse_rehashes_every_file(
    native_fixture: _NativeFixture,
    media_tools,
    tmp_path: Path,
) -> None:
  _quality(native_fixture, media_tools)
  output = tmp_path / "dist"
  first = package_tracks(
      plan=native_fixture.plan,
      run=native_fixture.run,
      output_directory=output,
  )
  reused = package_tracks(
      plan=native_fixture.plan,
      run=native_fixture.run,
      output_directory=output,
  )
  assert reused.path == first.path
  audio = next((first.path / "audio").glob("*.mp3"))
  audio.write_bytes(b"corrupt")

  with pytest.raises(PackagingError, match="stale or corrupt"):
    package_tracks(
        plan=native_fixture.plan,
        run=native_fixture.run,
        output_directory=output,
    )


def test_packaging_obeys_workspace_lock(
    native_fixture: _NativeFixture,
    media_tools,
    tmp_path: Path,
) -> None:
  _quality(native_fixture, media_tools)
  with WorkspaceLock(native_fixture.plan.workspace):
    with pytest.raises(WorkspaceError, match="Another ebook-tts process"):
      package_bookplayer(
          plan=native_fixture.plan,
          run=native_fixture.run,
          output_directory=tmp_path / "dist",
      )
