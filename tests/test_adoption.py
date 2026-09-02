from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from ebook_tts.cli import main
from ebook_tts.config import default_config
from ebook_tts.errors import WorkspaceError
from ebook_tts.media.tools import media_record, probe_audio
from ebook_tts.qa.evaluate import evaluate_run
from ebook_tts.utils import canonical_json, sha256_bytes, sha256_file, sha256_text
from ebook_tts.workspace.adoption import adopt_legacy_v1
from ebook_tts.workspace.generation import generate, generate_sample, load_run
from ebook_tts.workspace.manifests import load_plan


@dataclass(frozen=True)
class LegacyFixture:
  root: Path
  output: Path
  source_epub: Path
  source_member: str
  track_directory: Path
  plan_path: Path
  generation_path: Path
  chunk_audio: Path
  final_audio: Path
  workspace: Path


def _write_json(path: Path, value: dict[str, Any]) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _legacy_generation_fingerprint(
    plan_sha256: str, generation: dict[str, Any]
) -> str:
  fingerprinted = {
      "voice_id": generation["voice_id"],
      "model_id": generation["model_id"],
      "output_format": generation["output_format"],
      "context_characters": generation["context_characters"],
  }
  return sha256_text(
      canonical_json({"plan_sha256": plan_sha256, "generation": fingerprinted})
  )


def _legacy_media(info) -> dict[str, Any]:
  value = media_record(info)
  value.pop("sha256", None)
  return value


def _rehash_plan_and_generation(fixture: LegacyFixture) -> None:
  plan = json.loads(fixture.plan_path.read_text(encoding="utf-8"))
  plan.pop("plan_sha256", None)
  plan_sha = sha256_text(canonical_json(plan))
  plan["plan_sha256"] = plan_sha
  _write_json(fixture.plan_path, plan)

  generation = json.loads(fixture.generation_path.read_text(encoding="utf-8"))
  generation["plan_sha256"] = plan_sha
  generation["generation_fingerprint"] = _legacy_generation_fingerprint(
      plan_sha, generation["generation"]
  )
  _write_json(fixture.generation_path, generation)


def _legacy_fixture(
    *,
    tmp_path: Path,
    epub_factory,
    tone_mp3: bytes,
    media_tools,
) -> LegacyFixture:
  generated_epub = epub_factory(include_cover=True, text_repeat=1)
  root = tmp_path / "legacy"
  root.mkdir()
  source_epub = root / "source-book.zip"
  source_epub.write_bytes(generated_epub.read_bytes())
  with zipfile.ZipFile(source_epub, "r") as archive:
    source_member = next(
        name
        for name in archive.namelist()
        if Path(name).suffix.casefold() in {".xhtml", ".html", ".htm"}
    )
    source_bytes = archive.read(source_member)

  output = root / "chapters_output"
  stem = "001_legacy_chapter"
  track_directory = output / "chunks" / stem
  track_directory.mkdir(parents=True)

  source_file = root / "unpacked" / Path(source_member)
  source_file.parent.mkdir(parents=True, exist_ok=True)
  source_file.write_bytes(source_bytes)
  text = (
      "This synthetic legacy chapter preserves exact paid request boundaries "
      "during a fully offline adoption test."
  )
  full_text = output / "text" / f"{stem}.txt"
  full_text.parent.mkdir(parents=True)
  full_text.write_text(text + "\n", encoding="utf-8")
  chunk_text = track_directory / "chunk_001.txt"
  chunk_text.write_text(text + "\n", encoding="utf-8")
  text_sha = sha256_text(text)

  plan: dict[str, Any] = {
      "manifest_version": 1,
      "chunker_version": 2,
      "track_number": 1,
      "chapter_number": 1,
      "title": "Legacy Chapter",
      "output_stem": stem,
      "source_file": source_file.relative_to(root).as_posix(),
      "source_href": source_member,
      "source_sha256": sha256_bytes(source_bytes),
      "text_file": full_text.relative_to(root).as_posix(),
      "text_sha256": text_sha,
      "characters": len(text),
      "chunk_count": 1,
      "chunks": [
          {
              "index": 1,
              "text_file": chunk_text.relative_to(root).as_posix(),
              "sha256": text_sha,
              "characters": len(text),
          }
      ],
  }
  plan_sha = sha256_text(canonical_json(plan))
  plan["plan_sha256"] = plan_sha
  plan_path = track_directory / "chunk_plan.json"
  _write_json(plan_path, plan)

  generation = {
      "voice_id": "legacy-test-voice",
      "model_id": "eleven_multilingual_v2",
      "output_format": "mp3_44100_128",
      "context_characters": 500,
      "elevenlabs_sdk_version": "legacy-test-sdk",
  }
  request = {
      "voice_id": generation["voice_id"],
      "model_id": generation["model_id"],
      "output_format": generation["output_format"],
      "text": text,
  }
  request_sha = sha256_text(canonical_json(request))
  chunk_audio = track_directory / f"chunk_001_{request_sha}.mp3"
  chunk_audio.write_bytes(tone_mp3)
  chunk_info = probe_audio(
      chunk_audio,
      media_tools.ffprobe,
      expected_format=generation["output_format"],
  )
  chunk_audio_relative = chunk_audio.relative_to(root).as_posix()
  sidecar_path = chunk_audio.with_suffix(".mp3.json")
  sidecar = {
      "manifest_version": 1,
      "request_sha256": request_sha,
      "text_sha256": text_sha,
      "audio_file": chunk_audio_relative,
      "audio_sha256": chunk_info.sha256,
      "media": _legacy_media(chunk_info),
  }
  _write_json(sidecar_path, sidecar)

  final_audio = output / "audio" / f"{stem}.mp3"
  final_audio.parent.mkdir(parents=True)
  final_audio.write_bytes(tone_mp3)
  final_info = probe_audio(
      final_audio,
      media_tools.ffprobe,
      expected_format=generation["output_format"],
  )
  generation_fingerprint = _legacy_generation_fingerprint(plan_sha, generation)
  generation_manifest = {
      "manifest_version": 1,
      "track_number": 1,
      "chapter_number": 1,
      "title": "Legacy Chapter",
      "output_stem": stem,
      "plan_sha256": plan_sha,
      "generation_fingerprint": generation_fingerprint,
      "status": "complete",
      "generation": generation,
      "chunks": [
          {
              "index": 1,
              "request_sha256": request_sha,
              "text_sha256": text_sha,
              "audio_file": chunk_audio_relative,
              "audio_sidecar": sidecar_path.relative_to(root).as_posix(),
              "audio_sha256": chunk_info.sha256,
              **_legacy_media(chunk_info),
          }
      ],
      "completed_chunks": 1,
      "final_audio": {
          "audio_file": final_audio.relative_to(root).as_posix(),
          **media_record(final_info),
      },
  }
  generation_path = track_directory / "generation_manifest.json"
  _write_json(generation_path, generation_manifest)

  with zipfile.ZipFile(output / "legacy-flat.zip", "w", zipfile.ZIP_STORED) as archive:
    archive.writestr(final_audio.name, tone_mp3)

  return LegacyFixture(
      root=root,
      output=output,
      source_epub=source_epub,
      source_member=source_member,
      track_directory=track_directory,
      plan_path=plan_path,
      generation_path=generation_path,
      chunk_audio=chunk_audio,
      final_audio=final_audio,
      workspace=tmp_path / "adopted-workspace",
  )


class _ForbiddenProvider:
  name = "forbidden"
  version = "test"

  def __init__(self) -> None:
    self.calls = 0

  def synthesize(self, _request):
    self.calls += 1
    raise AssertionError("provider must not be called for an adopted plan")


def test_adopt_valid_legacy_tree_preserves_bytes_and_supports_local_qa(
    tmp_path: Path,
    epub_factory,
    tone_mp3: bytes,
    media_tools,
    monkeypatch,
    capsys,
) -> None:
  legacy = _legacy_fixture(
      tmp_path=tmp_path,
      epub_factory=epub_factory,
      tone_mp3=tone_mp3,
      media_tools=media_tools,
  )
  result = adopt_legacy_v1(
      legacy_root=legacy.root,
      source_epub=legacy.source_epub,
      workspace=legacy.workspace,
      ffmpeg=media_tools.ffmpeg,
      ffprobe=media_tools.ffprobe,
  )

  plan = load_plan(result.workspace)
  run = load_run(plan)
  assert result.plan_id == plan.plan_id
  assert result.run_id == run.run_id
  assert plan.manifest["versions"]["chunker"] == 2
  assert plan.manifest["verification_only"] is True
  assert run.manifest["verification_only"] is True
  assert run.manifest["generation"]["provider_sdk_version"] == "legacy-test-sdk"
  assert result.adoption_path.is_file()
  assert legacy.source_epub.suffix == ".zip"
  assert plan.manifest["sections"][0]["source_href"] == legacy.source_member
  with zipfile.ZipFile(legacy.source_epub, "r") as archive:
    assert sha256_bytes(archive.read(legacy.source_member)) == plan.manifest["sections"][0][
        "source_sha256"
    ]
  adoption = json.loads(result.adoption_path.read_text(encoding="utf-8"))
  assert adoption["source_members"] == [
      {
          "track": 1,
          "member": legacy.source_member,
          "sha256": plan.manifest["sections"][0]["source_sha256"],
      }
  ]
  assert adoption["legacy_sdk_versions"] == ["legacy-test-sdk"]

  adopted_chunk = next((run.run_path.parent / "chunks").rglob("*.mp3"))
  adopted_final = run.run_path.parent / "audio" / legacy.final_audio.name
  assert adopted_chunk.read_bytes() == legacy.chunk_audio.read_bytes()
  assert adopted_final.read_bytes() == legacy.final_audio.read_bytes()
  assert sha256_file(adopted_chunk) == sha256_file(legacy.chunk_audio)
  assert sha256_file(adopted_final) == sha256_file(legacy.final_audio)

  quality = evaluate_run(
      plan=plan,
      run=run,
      config=default_config(),
      stt_provider=None,
      tools=media_tools,
  )
  assert quality.report["status"] != "fail"
  assert quality.report["validated_tracks"] == 1
  assert quality.report["transcription"] is None

  provider = _ForbiddenProvider()
  with pytest.raises(WorkspaceError, match="verification-only"):
    generate(
        plan=plan,
        config=default_config(),
        provider=provider,
        tools=media_tools,
    )
  with pytest.raises(WorkspaceError, match="verification-only"):
    generate_sample(
        plan=plan,
        config=default_config(),
        provider=provider,
        tools=media_tools,
    )
  assert provider.calls == 0

  cli_workspace = tmp_path / "cli-adopted-workspace"
  monkeypatch.setattr(
      "ebook_tts.cli._tts_provider",
      lambda _config: pytest.fail("adopt must not construct a provider"),
  )
  assert main(
      [
          "adopt",
          str(legacy.root),
          "--source",
          str(legacy.source_epub),
          "--workspace",
          str(cli_workspace),
          "--ffmpeg",
          media_tools.ffmpeg,
          "--ffprobe",
          media_tools.ffprobe,
          "--json",
      ]
  ) == 0
  summary = json.loads(capsys.readouterr().out)
  assert summary["verification_only"] is True
  assert summary["tracks"] == 1


def test_adoption_rejects_tampered_plan_hash_atomically(
    tmp_path: Path,
    epub_factory,
    tone_mp3: bytes,
    media_tools,
) -> None:
  legacy = _legacy_fixture(
      tmp_path=tmp_path,
      epub_factory=epub_factory,
      tone_mp3=tone_mp3,
      media_tools=media_tools,
  )
  plan = json.loads(legacy.plan_path.read_text(encoding="utf-8"))
  plan["characters"] += 1
  _write_json(legacy.plan_path, plan)

  with pytest.raises(WorkspaceError, match="fingerprint mismatch"):
    adopt_legacy_v1(
        legacy_root=legacy.root,
        source_epub=legacy.source_epub,
        workspace=legacy.workspace,
        ffmpeg=media_tools.ffmpeg,
        ffprobe=media_tools.ffprobe,
    )
  assert not legacy.workspace.exists()
  assert not list(tmp_path.glob(".adopted-workspace.adoption-*"))


def test_adoption_rejects_escaping_legacy_path(
    tmp_path: Path,
    epub_factory,
    tone_mp3: bytes,
    media_tools,
) -> None:
  legacy = _legacy_fixture(
      tmp_path=tmp_path,
      epub_factory=epub_factory,
      tone_mp3=tone_mp3,
      media_tools=media_tools,
  )
  outside = tmp_path / "outside.txt"
  outside.write_text("outside\n", encoding="utf-8")
  plan = json.loads(legacy.plan_path.read_text(encoding="utf-8"))
  plan["source_file"] = "../outside.txt"
  plan["source_sha256"] = sha256_file(outside)
  _write_json(legacy.plan_path, plan)
  _rehash_plan_and_generation(legacy)

  with pytest.raises(WorkspaceError, match="outside|unsafe|missing"):
    adopt_legacy_v1(
        legacy_root=legacy.root,
        source_epub=legacy.source_epub,
        workspace=legacy.workspace,
        ffmpeg=media_tools.ffmpeg,
        ffprobe=media_tools.ffprobe,
    )
  assert not legacy.workspace.exists()


def test_adoption_rejects_source_member_mismatch(
    tmp_path: Path,
    epub_factory,
    tone_mp3: bytes,
    media_tools,
) -> None:
  legacy = _legacy_fixture(
      tmp_path=tmp_path,
      epub_factory=epub_factory,
      tone_mp3=tone_mp3,
      media_tools=media_tools,
  )
  plan = json.loads(legacy.plan_path.read_text(encoding="utf-8"))
  unpacked_source = legacy.root / plan["source_file"]
  unpacked_source.write_bytes(b"not the archived source member")
  plan["source_sha256"] = sha256_file(unpacked_source)
  _write_json(legacy.plan_path, plan)
  _rehash_plan_and_generation(legacy)

  with pytest.raises(WorkspaceError, match="SOURCE_EPUB member"):
    adopt_legacy_v1(
        legacy_root=legacy.root,
        source_epub=legacy.source_epub,
        workspace=legacy.workspace,
        ffmpeg=media_tools.ffmpeg,
        ffprobe=media_tools.ffprobe,
    )
  assert not legacy.workspace.exists()


def test_adoption_rejects_active_attempt_evidence(
    tmp_path: Path,
    epub_factory,
    tone_mp3: bytes,
    media_tools,
) -> None:
  legacy = _legacy_fixture(
      tmp_path=tmp_path,
      epub_factory=epub_factory,
      tone_mp3=tone_mp3,
      media_tools=media_tools,
  )
  (legacy.track_directory / "unresolved.attempt.json").write_text("{}\n", encoding="utf-8")

  with pytest.raises(WorkspaceError, match="active ambiguous request evidence"):
    adopt_legacy_v1(
        legacy_root=legacy.root,
        source_epub=legacy.source_epub,
        workspace=legacy.workspace,
        ffmpeg=media_tools.ffmpeg,
        ffprobe=media_tools.ffprobe,
    )
  assert not legacy.workspace.exists()


def test_adoption_rejects_tampered_audio_hash(
    tmp_path: Path,
    epub_factory,
    tone_mp3: bytes,
    media_tools,
) -> None:
  legacy = _legacy_fixture(
      tmp_path=tmp_path,
      epub_factory=epub_factory,
      tone_mp3=tone_mp3,
      media_tools=media_tools,
  )
  generation = json.loads(legacy.generation_path.read_text(encoding="utf-8"))
  generation["chunks"][0]["audio_sha256"] = "0" * 64
  _write_json(legacy.generation_path, generation)

  with pytest.raises(WorkspaceError, match="audio hash|audio hashes"):
    adopt_legacy_v1(
        legacy_root=legacy.root,
        source_epub=legacy.source_epub,
        workspace=legacy.workspace,
        ffmpeg=media_tools.ffmpeg,
        ffprobe=media_tools.ffprobe,
    )
  assert not legacy.workspace.exists()
