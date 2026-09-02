"""Validated deterministic track-directory, BookPlayer, and archival outputs."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import PackagingError
from ..models import MANIFEST_VERSION, Plan
from ..qa.evaluate import QA_VERSION, quality_report_id, run_manifest_fingerprint
from ..qa.report import html_document
from ..utils import (
    canonical_json,
    fsync_directory,
    load_json,
    sha256_bytes,
    sha256_file,
    sha256_text,
    slugify,
)
from ..workspace.generation import (
    ASSEMBLY_CHECKPOINT_VERSION,
    ASSEMBLY_POLICY_VERSION,
    GenerationRun,
    is_adopted_plan,
    load_run,
)
from ..workspace.locking import WorkspaceLock
from ..workspace.manifests import verified_cover_path, verify_plan_artifacts


_SHA256_LENGTH = 64
_REPORT_KEYS = {
    "manifest_version",
    "kind",
    "qa_id",
    "run_id",
    "run_manifest_sha256",
    "plan_sha256",
    "created_at",
    "status",
    "configuration",
    "book",
    "expected_tracks",
    "validated_tracks",
    "failures",
    "warnings",
    "transcription",
    "tracks",
}
_QA_CONFIGURATION_KEYS = {
    "qa_version",
    "stt_provider",
    "stt_model",
    "language",
    "max_word_error_rate",
    "max_character_error_rate",
    "max_internal_silence_seconds",
    "clipping_peak_db",
    "protected_terms",
}
_TRACK_REPORT_KEYS = {
    "track_number",
    "title",
    "audio_file",
    "audio_sha256",
    "assembly_sha256",
    "duration_seconds",
    "media",
    "signal",
    "transcription",
    "warnings",
}


@dataclass(frozen=True)
class PackageArtifact:
  format: str
  path: Path
  sha256: str
  bytes: int


@dataclass(frozen=True)
class TrackArtifact:
  track_number: int
  title: str
  path: Path
  sha256: str
  bytes: int
  duration_seconds: float
  media: dict[str, Any]
  assembly_sha256: str | None


@dataclass(frozen=True)
class _PackageMember:
  name: str
  source: Path | bytes
  sha256: str
  bytes: int


@dataclass(frozen=True)
class _PackageContext:
  run: GenerationRun
  tracks: tuple[TrackArtifact, ...]
  run_manifest_sha256: str
  artifact_state_sha256: str
  quality: dict[str, Any] | None
  quality_path: Path | None


def _is_sha256(value: object) -> bool:
  return (
      isinstance(value, str)
      and len(value) == _SHA256_LENGTH
      and all(character in "0123456789abcdef" for character in value)
  )


def _regular_confined_file(root: Path, path: Path, label: str) -> Path:
  """Require a regular file beneath root with no symbolic-link components."""
  try:
    resolved_root = root.resolve(strict=True)
  except OSError as exc:
    raise PackagingError(f"{label} root is missing: {root}") from exc
  candidate = Path(os.path.abspath(path))
  try:
    relative = candidate.relative_to(resolved_root)
  except ValueError as exc:
    raise PackagingError(f"{label} escapes its expected root: {path}") from exc
  current = resolved_root
  for part in relative.parts:
    current = current / part
    if current.is_symlink():
      raise PackagingError(f"{label} traverses a symbolic link: {current}")
  try:
    resolved = candidate.resolve(strict=True)
  except OSError as exc:
    raise PackagingError(f"{label} is missing: {candidate}") from exc
  if resolved != candidate or not candidate.is_file():
    raise PackagingError(f"{label} is not a regular confined file: {candidate}")
  return candidate


def _native_assembly_sha256(
    plan: Plan,
    run: GenerationRun,
    section: dict[str, Any],
    state: dict[str, Any],
) -> str:
  """Validate the pure-data portion of one authenticated native final receipt."""
  number = int(section["track_number"])
  final = state.get("final_audio")
  if not isinstance(final, dict):
    raise PackagingError(f"Track {number} has no final audio checkpoint.")
  assembly = final.get("assembly")
  assembly_sha256 = final.get("assembly_sha256")
  if (
      final.get("checkpoint_version") != ASSEMBLY_CHECKPOINT_VERSION
      or not isinstance(assembly, dict)
      or not _is_sha256(assembly_sha256)
      or sha256_text(canonical_json(assembly)) != assembly_sha256
  ):
    raise PackagingError(f"Track {number} assembly checkpoint is invalid.")

  generation = run.manifest.get("generation")
  if not isinstance(generation, dict):
    raise PackagingError("Generation settings are invalid.")
  settings = generation.get("assembly")
  if (
      not isinstance(settings, dict)
      or settings.get("policy_version") != ASSEMBLY_POLICY_VERSION
      or settings.get("checkpoint_version") != ASSEMBLY_CHECKPOINT_VERSION
      or settings.get("id3v2_version") != 3
      or not isinstance(settings.get("genre"), str)
  ):
    raise PackagingError("Generation assembly settings are invalid.")
  output_format = generation.get("output_format")
  if not isinstance(output_format, str) or not output_format:
    raise PackagingError("Generation output format is invalid.")

  planned_chunks = section.get("chunks")
  generated_chunks = state.get("chunks")
  if (
      not isinstance(planned_chunks, list)
      or not isinstance(generated_chunks, list)
      or not planned_chunks
      or len(planned_chunks) != len(generated_chunks)
      or state.get("completed_chunks") != len(generated_chunks)
  ):
    raise PackagingError(f"Track {number} chunk checkpoints are incomplete.")
  inputs: list[dict[str, Any]] = []
  for position, (planned, generated) in enumerate(
      zip(planned_chunks, generated_chunks), start=1
  ):
    if not isinstance(planned, dict) or not isinstance(generated, dict):
      raise PackagingError(f"Track {number}, chunk {position} checkpoint is invalid.")
    request_sha = generated.get("request_sha256")
    text_sha = planned.get("text_sha256")
    expected_name = f"chunk_{position:04d}_{request_sha}.mp3"
    media = generated.get("media")
    if (
        planned.get("index") != position
        or not _is_sha256(text_sha)
        or generated.get("text_sha256") != text_sha
        or not _is_sha256(request_sha)
        or generated.get("manifest_version") != MANIFEST_VERSION
        or generated.get("kind") != "ebook-tts-chunk"
        or generated.get("audio_file") != expected_name
        or not _is_sha256(generated.get("audio_sha256"))
        or not isinstance(media, dict)
        or media.get("sha256") != generated.get("audio_sha256")
    ):
      raise PackagingError(f"Track {number}, chunk {position} identity is invalid.")
    inputs.append(
        {
            "index": position,
            "text_sha256": text_sha,
            "request_sha256": request_sha,
            "audio_file": expected_name,
            "audio_sha256": generated["audio_sha256"],
            "media": media,
        }
    )

  book = plan.manifest.get("book")
  if not isinstance(book, dict):
    raise PackagingError("Plan book metadata is invalid.")
  artist = ", ".join(str(value) for value in book.get("authors", []))
  artist = artist or "Unknown Author"
  metadata = {
      "title": str(section["title"]),
      "album": str(book["title"]),
      "artist": artist,
      "album_artist": artist,
      "genre": str(settings["genre"]),
      "track": f"{number}/{int(plan.manifest['track_count'])}",
      "id3v2_version": 3,
  }
  cover_path = verified_cover_path(plan)
  cover = plan.manifest.get("cover")
  cover_record = (
      None
      if cover_path is None
      else {
          "file": cover_path.name,
          "media_type": cover.get("media_type") if isinstance(cover, dict) else None,
          "sha256": cover.get("sha256") if isinstance(cover, dict) else None,
          "bytes": cover.get("bytes") if isinstance(cover, dict) else None,
      }
  )
  expected = {
      "checkpoint_version": ASSEMBLY_CHECKPOINT_VERSION,
      "policy_version": ASSEMBLY_POLICY_VERSION,
      "plan_sha256": plan.plan_id,
      "run_id": run.run_id,
      "section_sha256": sha256_text(canonical_json(section)),
      "track_number": number,
      "output_file": f"{section['output_stem']}.mp3",
      "output_format": output_format,
      "inputs": inputs,
      "metadata": metadata,
      "cover": cover_record,
  }
  if assembly != expected:
    raise PackagingError(
        f"Track {number} assembly receipt does not match the plan and chunk state."
    )
  return str(assembly_sha256)


def _collect_tracks(plan: Plan, run: GenerationRun) -> tuple[TrackArtifact, ...]:
  if run.manifest.get("status") != "complete":
    raise PackagingError("Packaging requires a complete generation run.")
  sections = plan.manifest.get("sections")
  states = run.manifest.get("sections")
  if not isinstance(sections, list) or not isinstance(states, dict):
    raise PackagingError("Plan or generation section manifests are invalid.")
  count = int(plan.manifest.get("track_count", -1))
  if len(sections) != count or run.manifest.get("track_count") != count:
    raise PackagingError("Plan and generation track counts are inconsistent.")
  if run.manifest.get("completed_tracks") != count:
    raise PackagingError("Generation completed-track count is inconsistent.")
  expected_keys = {str(number) for number in range(1, count + 1)}
  if set(states) != expected_keys:
    raise PackagingError("Generation section state set is incomplete or unexpected.")

  adopted = is_adopted_plan(plan)
  run_adopted = bool(
      run.manifest.get("adopted")
      or run.manifest.get("verification_only")
      or run.manifest.get("adoption")
  )
  if adopted != run_adopted:
    raise PackagingError("Plan and generation adoption identities disagree.")
  if adopted and run.manifest.get("adoption") != plan.manifest.get("adoption"):
    raise PackagingError("Plan and generation adoption provenance disagrees.")

  run_root = run.run_path.parent
  audio_root = run_root / "audio"
  if audio_root.is_symlink() or not audio_root.is_dir():
    raise PackagingError("Generation final-audio directory is missing or unsafe.")
  tracks: list[TrackArtifact] = []
  for expected_number, section in enumerate(sections, start=1):
    if not isinstance(section, dict) or section.get("track_number") != expected_number:
      raise PackagingError("Plan tracks are not ordered and contiguous from 1.")
    number = expected_number
    state = states.get(str(number))
    if (
        not isinstance(state, dict)
        or state.get("status") != "complete"
        or state.get("track_number") != number
        or state.get("title") != section.get("title")
        or state.get("output_stem") != section.get("output_stem")
    ):
      raise PackagingError(f"Track {number} state does not match its plan section.")
    final = state.get("final_audio")
    expected_name = f"{section['output_stem']}.mp3"
    if not isinstance(final, dict) or final.get("file") != expected_name:
      raise PackagingError(f"Track {number} final filename is invalid.")
    path = _regular_confined_file(
        run_root,
        audio_root / expected_name,
        f"Track {number} final audio",
    )
    digest = sha256_file(path)
    size = path.stat().st_size
    if not _is_sha256(final.get("sha256")) or digest != final.get("sha256"):
      raise PackagingError(f"Track {number} audio hash does not match its checkpoint.")
    if final.get("bytes") != size:
      raise PackagingError(f"Track {number} audio size does not match its checkpoint.")
    raw_duration = final.get("duration_seconds")
    if isinstance(raw_duration, bool):
      raise PackagingError(f"Track {number} duration checkpoint is invalid.")
    try:
      duration = float(raw_duration)
    except (TypeError, ValueError) as exc:
      raise PackagingError(f"Track {number} duration checkpoint is invalid.") from exc
    if not math.isfinite(duration) or duration <= 0:
      raise PackagingError(f"Track {number} duration checkpoint is invalid.")
    codec = final.get("codec")
    sample_rate = final.get("sample_rate")
    bit_rate = final.get("bit_rate")
    channels = final.get("channels")
    if (
        not isinstance(codec, str)
        or not codec
        or isinstance(sample_rate, bool)
        or not isinstance(sample_rate, int)
        or sample_rate <= 0
        or isinstance(channels, bool)
        or not isinstance(channels, int)
        or channels <= 0
        or (
            bit_rate is not None
            and (
                isinstance(bit_rate, bool)
                or not isinstance(bit_rate, int)
                or bit_rate <= 0
            )
        )
    ):
      raise PackagingError(f"Track {number} media checkpoint is invalid.")
    stored_media = {
        "codec": codec,
        "sample_rate": sample_rate,
        "bit_rate": bit_rate,
        "channels": channels,
        "duration_seconds": duration,
        "bytes": size,
        "sha256": digest,
    }
    assembly_sha256 = (
        None
        if adopted
        else _native_assembly_sha256(plan, run, section, state)
    )
    tracks.append(
        TrackArtifact(
            track_number=number,
            title=str(section["title"]),
            path=path,
            sha256=digest,
            bytes=size,
            duration_seconds=duration,
            media=stored_media,
            assembly_sha256=assembly_sha256,
        )
    )
  return tuple(tracks)


def _artifact_state_sha256(
    plan: Plan,
    run: GenerationRun,
    tracks: tuple[TrackArtifact, ...],
    run_manifest_sha256: str,
) -> str:
  return sha256_text(
      canonical_json(
          {
              "plan_sha256": plan.plan_id,
              "run_id": run.run_id,
              "run_manifest_sha256": run_manifest_sha256,
              "tracks": [
                  {
                      "track_number": track.track_number,
                      "file": track.path.name,
                      "sha256": track.sha256,
                      "bytes": track.bytes,
                      "duration_seconds": track.duration_seconds,
                      "assembly_sha256": track.assembly_sha256,
                  }
                  for track in tracks
              ],
          }
      )
  )


def _validate_quality_report(
    *,
    plan: Plan,
    run: GenerationRun,
    tracks: tuple[TrackArtifact, ...],
    run_manifest_sha256: str,
    path: Path,
    report: dict[str, Any],
) -> None:
  configuration = report.get("configuration")
  qa_id = report.get("qa_id")
  status = report.get("status")
  failures = report.get("failures")
  warnings = report.get("warnings")
  reported_tracks = report.get("tracks")
  if (
      set(report) != _REPORT_KEYS
      or report.get("manifest_version") != MANIFEST_VERSION
      or report.get("kind") != "ebook-tts-quality-report"
      or report.get("run_id") != run.run_id
      or report.get("run_manifest_sha256") != run_manifest_sha256
      or report.get("plan_sha256") != plan.plan_id
      or report.get("book") != plan.manifest.get("book")
      or not isinstance(report.get("created_at"), str)
      or not report["created_at"]
      or not isinstance(configuration, dict)
      or set(configuration) != _QA_CONFIGURATION_KEYS
      or configuration.get("qa_version") != QA_VERSION
      or not isinstance(configuration.get("stt_provider"), str)
      or not isinstance(configuration.get("protected_terms"), list)
      or any(
          not isinstance(value, str)
          for value in configuration.get("protected_terms", [])
      )
      or not _is_sha256(qa_id)
      or qa_id != path.parent.name
      or quality_report_id(run.run_id, run_manifest_sha256, configuration) != qa_id
      or status not in {"pass", "warn", "fail"}
      or not isinstance(failures, list)
      or any(not isinstance(value, str) for value in failures)
      or not isinstance(warnings, list)
      or any(not isinstance(value, str) for value in warnings)
      or not isinstance(reported_tracks, list)
      or (
          report.get("transcription") is not None
          and not isinstance(report.get("transcription"), dict)
      )
      or report.get("expected_tracks") != len(tracks)
      or isinstance(report.get("validated_tracks"), bool)
      or not isinstance(report.get("validated_tracks"), int)
      or report.get("validated_tracks") != len(reported_tracks)
  ):
    raise PackagingError(f"QA report schema or identity is invalid: {path}")
  if status == "pass" and (failures or warnings):
    raise PackagingError(f"QA pass report contains failures or warnings: {path}")
  if status == "warn" and (failures or not warnings):
    raise PackagingError(f"QA warning report has inconsistent findings: {path}")
  if status == "fail" and not failures:
    raise PackagingError(f"QA failure report contains no failures: {path}")

  by_number = {track.track_number: track for track in tracks}
  seen: list[int] = []
  for record in reported_tracks:
    if not isinstance(record, dict) or set(record) != _TRACK_REPORT_KEYS:
      raise PackagingError(f"QA report contains an invalid track record: {path}")
    number = record.get("track_number")
    track = (
        by_number.get(number)
        if isinstance(number, int) and not isinstance(number, bool)
        else None
    )
    media = record.get("media")
    track_warnings = record.get("warnings")
    if (
        track is None
        or not isinstance(media, dict)
        or media != track.media
        or not isinstance(record.get("signal"), dict)
        or (
            record.get("transcription") is not None
            and not isinstance(record.get("transcription"), dict)
        )
        or not isinstance(track_warnings, list)
        or any(not isinstance(value, str) for value in track_warnings)
    ):
      raise PackagingError(f"QA report references an invalid track: {path}")
    if (
        number in seen
        or record.get("title") != track.title
        or record.get("audio_file") != track.path.name
        or record.get("audio_sha256") != track.sha256
        or record.get("assembly_sha256") != track.assembly_sha256
    ):
      raise PackagingError(f"QA report track does not match current audio: {path}")
    raw_duration = record.get("duration_seconds")
    media_duration = media.get("duration_seconds")
    try:
      duration = float(raw_duration)
      stored_media_duration = float(media_duration)
    except (TypeError, ValueError) as exc:
      raise PackagingError(f"QA report track duration is invalid: {path}") from exc
    tolerance = max(0.01, track.duration_seconds * 0.005)
    if (
        not math.isfinite(duration)
        or not math.isfinite(stored_media_duration)
        or abs(duration - track.duration_seconds) > tolerance
        or abs(stored_media_duration - track.duration_seconds) > tolerance
    ):
      raise PackagingError(f"QA report track duration is stale: {path}")
    seen.append(number)
  if seen != sorted(seen):
    raise PackagingError(f"QA report tracks are not ordered: {path}")
  if status in {"pass", "warn"} and seen != list(range(1, len(tracks) + 1)):
    raise PackagingError(f"QA report does not validate every current track: {path}")


def _quality_report(
    plan: Plan,
    run: GenerationRun,
    tracks: tuple[TrackArtifact, ...],
    run_manifest_sha256: str,
    *,
    allow_unvalidated: bool,
    allow_failed_qa: bool,
) -> tuple[dict[str, Any] | None, Path | None]:
  qa_root = run.run_path.parent / "qa"
  if qa_root.is_symlink() or (qa_root.exists() and not qa_root.is_dir()):
    raise PackagingError(f"QA report directory is unsafe: {qa_root}")
  candidates = (
      sorted(
          qa_root.glob("*/report.json"),
          key=lambda value: value.stat().st_mtime,
          reverse=True,
      )
      if qa_root.is_dir()
      else []
  )
  selected: tuple[dict[str, Any], Path] | None = None
  for candidate in candidates:
    if (
        candidate.is_symlink()
        or candidate.parent.is_symlink()
        or not candidate.is_file()
        or not _is_sha256(candidate.parent.name)
    ):
      raise PackagingError(f"QA report path is unsafe: {candidate}")
    report = load_json(candidate)
    if (
        report.get("run_id") != run.run_id
        or report.get("run_manifest_sha256") != run_manifest_sha256
    ):
      continue
    _validate_quality_report(
        plan=plan,
        run=run,
        tracks=tracks,
        run_manifest_sha256=run_manifest_sha256,
        path=candidate,
        report=report,
    )
    selected = report, candidate
    break
  if selected is None:
    if allow_unvalidated:
      return None, None
    raise PackagingError(
        "No QA report matches the current run manifest and audio artifacts. Run "
        "`ebook-tts validate`, or use --allow-unvalidated explicitly."
    )
  report, path = selected
  if report["status"] == "fail" and not allow_failed_qa:
    raise PackagingError(
        f"QA report {path} failed. Correct the failures or explicitly use "
        "--allow-failed-qa."
    )
  return report, path


def _package_context(
    *,
    plan: Plan,
    supplied_run: GenerationRun,
    allow_unvalidated: bool,
    allow_failed_qa: bool,
) -> _PackageContext:
  verify_plan_artifacts(plan)
  run = load_run(plan, supplied_run.run_id)
  tracks = _collect_tracks(plan, run)
  run_sha = run_manifest_fingerprint(run.manifest)
  state_sha = _artifact_state_sha256(plan, run, tracks, run_sha)
  quality, quality_path = _quality_report(
      plan,
      run,
      tracks,
      run_sha,
      allow_unvalidated=allow_unvalidated,
      allow_failed_qa=allow_failed_qa,
  )
  return _PackageContext(run, tracks, run_sha, state_sha, quality, quality_path)


def _json_bytes(value: Any) -> bytes:
  return (
      json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
  ).encode("utf-8")


def _member(name: str, source: Path | bytes) -> _PackageMember:
  if isinstance(source, Path):
    if source.is_symlink() or not source.is_file():
      raise PackagingError(f"Package source is missing or unsafe: {source}")
    return _PackageMember(name, source, sha256_file(source), source.stat().st_size)
  return _PackageMember(name, source, sha256_bytes(source), len(source))


def _members_sha256(members: list[_PackageMember]) -> str:
  return sha256_text(
      canonical_json(
          [
              {"name": member.name, "sha256": member.sha256, "bytes": member.bytes}
              for member in members
          ]
      )
  )


def _zip_info(name: str) -> zipfile.ZipInfo:
  info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
  info.compress_type = zipfile.ZIP_STORED
  info.create_system = 3
  info.external_attr = 0o100644 << 16
  info.flag_bits |= 0x800
  return info


def _write_zip_member(archive: zipfile.ZipFile, member: _PackageMember) -> None:
  with archive.open(_zip_info(member.name), "w", force_zip64=True) as destination:
    if isinstance(member.source, Path):
      if member.source.is_symlink() or not member.source.is_file():
        raise PackagingError(f"Package source changed during publication: {member.source}")
      with member.source.open("rb") as source:
        shutil.copyfileobj(source, destination, length=1024 * 1024)
    else:
      destination.write(member.source)


def _verify_zip(path: Path, members: list[_PackageMember]) -> None:
  if path.is_symlink() or not path.is_file():
    raise PackagingError(f"ZIP artifact is missing or unsafe: {path}")
  try:
    if path.stat().st_size < 22:
      raise PackagingError(f"ZIP artifact is truncated: {path}")
    with path.open("rb") as raw:
      raw.seek(-22, os.SEEK_END)
      end_record = raw.read(22)
    if end_record[:4] != b"PK\x05\x06" or end_record[-2:] != b"\x00\x00":
      raise PackagingError(f"ZIP artifact has a comment or trailing data: {path}")
    with zipfile.ZipFile(path, "r") as archive:
      records = archive.infolist()
      if [record.filename for record in records] != [member.name for member in members]:
        raise PackagingError(f"ZIP member order or names do not match: {path}")
      for record, expected in zip(records, members):
        if (
            record.is_dir()
            or record.flag_bits & 0x1
            or record.compress_type != zipfile.ZIP_STORED
            or record.file_size != expected.bytes
        ):
          raise PackagingError(f"ZIP member metadata is invalid: {record.filename}")
        digest = hashlib.sha256()
        count = 0
        with archive.open(record, "r") as source:
          while True:
            block = source.read(min(1024 * 1024, expected.bytes - count + 1))
            if not block:
              break
            count += len(block)
            if count > expected.bytes:
              raise PackagingError(f"ZIP member exceeds expected size: {record.filename}")
            digest.update(block)
        if count != expected.bytes or digest.hexdigest() != expected.sha256:
          raise PackagingError(f"ZIP member bytes are stale or corrupt: {record.filename}")
  except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
    if isinstance(exc, PackagingError):
      raise
    raise PackagingError(f"Could not verify ZIP artifact {path}: {exc}") from exc


def _publish_zip(target: Path, members: list[_PackageMember]) -> PackageArtifact:
  target.parent.mkdir(parents=True, exist_ok=True)
  if target.is_symlink():
    raise PackagingError(f"ZIP target must not be a symbolic link: {target}")
  if target.exists():
    _verify_zip(target, members)
    return PackageArtifact("existing", target, sha256_file(target), target.stat().st_size)
  descriptor, temporary_name = tempfile.mkstemp(
      prefix=f".{target.stem}.", suffix=".part.zip", dir=target.parent
  )
  os.close(descriptor)
  temporary = Path(temporary_name)
  try:
    with zipfile.ZipFile(
        temporary,
        "w",
        compression=zipfile.ZIP_STORED,
        allowZip64=True,
        strict_timestamps=True,
    ) as archive:
      for member in members:
        _write_zip_member(archive, member)
    _verify_zip(temporary, members)
    os.replace(temporary, target)
    fsync_directory(target.parent)
  finally:
    temporary.unlink(missing_ok=True)
  _verify_zip(target, members)
  return PackageArtifact("zip", target, sha256_file(target), target.stat().st_size)


def _directory_entries(root: Path) -> tuple[set[str], set[str]]:
  directories: set[str] = set()
  files: set[str] = set()
  for raw_root, names, filenames in os.walk(root, followlinks=False):
    current = Path(raw_root)
    for name in names:
      path = current / name
      if path.is_symlink():
        raise PackagingError(f"Package directory contains a symbolic link: {path}")
      directories.add(path.relative_to(root).as_posix())
    for name in filenames:
      path = current / name
      if path.is_symlink() or not path.is_file():
        raise PackagingError(f"Package directory contains an unsafe file: {path}")
      files.add(path.relative_to(root).as_posix())
  return directories, files


def _verify_directory(target: Path, members: list[_PackageMember]) -> int:
  if target.is_symlink() or not target.is_dir():
    raise PackagingError(f"Package directory is missing or unsafe: {target}")
  actual_directories, actual_files = _directory_entries(target)
  expected_files = {member.name for member in members}
  expected_directories = {
      parent.as_posix()
      for member in members
      for parent in Path(member.name).parents
      if parent.as_posix() != "."
  }
  if actual_files != expected_files or actual_directories != expected_directories:
    raise PackagingError(f"Package directory contents do not match: {target}")
  total = 0
  for member in members:
    path = target / member.name
    size = path.stat().st_size
    if size != member.bytes or sha256_file(path) != member.sha256:
      raise PackagingError(f"Package directory member is stale or corrupt: {path}")
    total += size
  return total


def _publish_directory(
    target: Path,
    members: list[_PackageMember],
) -> PackageArtifact:
  target.parent.mkdir(parents=True, exist_ok=True)
  if target.is_symlink():
    raise PackagingError(f"Package target must not be a symbolic link: {target}")
  if target.exists():
    total = _verify_directory(target, members)
    checksums = next(member for member in members if member.name == "SHA256SUMS")
    return PackageArtifact("tracks", target, checksums.sha256, total)
  temporary = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=target.parent))
  try:
    for member in members:
      destination = temporary / member.name
      destination.parent.mkdir(parents=True, exist_ok=True)
      if isinstance(member.source, Path):
        if member.source.is_symlink() or not member.source.is_file():
          raise PackagingError(
              f"Package source changed during publication: {member.source}"
          )
        shutil.copyfile(member.source, destination)
      else:
        destination.write_bytes(member.source)
    total = _verify_directory(temporary, members)
    os.replace(temporary, target)
    fsync_directory(target.parent)
  finally:
    if temporary.exists():
      shutil.rmtree(temporary)
  total = _verify_directory(target, members)
  checksums = next(member for member in members if member.name == "SHA256SUMS")
  return PackageArtifact("tracks", target, checksums.sha256, total)


def _book_record(plan: Plan, context: _PackageContext) -> dict[str, Any]:
  quality = context.quality
  return {
      "manifest_version": MANIFEST_VERSION,
      "kind": "ebook-tts-distribution",
      "plan_sha256": plan.plan_id,
      "run_id": context.run.run_id,
      "run_manifest_sha256": context.run_manifest_sha256,
      "artifact_state_sha256": context.artifact_state_sha256,
      "book": plan.manifest["book"],
      "quality": (
          {
              "qa_id": quality.get("qa_id"),
              "status": quality.get("status"),
              "run_manifest_sha256": quality.get("run_manifest_sha256"),
          }
          if quality
          else None
      ),
      "tracks": [
          {
              "track_number": track.track_number,
              "title": track.title,
              "file": f"audio/{track.path.name}",
              "sha256": track.sha256,
              "duration_seconds": track.duration_seconds,
              "bytes": track.bytes,
              "assembly_sha256": track.assembly_sha256,
          }
          for track in context.tracks
      ],
  }


def _archive_members(plan: Plan, context: _PackageContext, root: str) -> list[_PackageMember]:
  members = [_member(f"{root}/metadata.json", _json_bytes(plan.manifest["book"]))]
  cover_path = verified_cover_path(plan)
  if cover_path is not None:
    members.append(_member(f"{root}/{cover_path.name}", cover_path))
  for track in context.tracks:
    members.append(_member(f"{root}/audio/{track.path.name}", track.path))
  if context.quality is not None and context.quality_path is not None:
    members.append(_member(f"{root}/qa/report.json", context.quality_path))
    members.append(
        _member(
            f"{root}/qa/report.html",
            html_document(context.quality).encode("utf-8"),
        )
    )
  members.append(_member(f"{root}/manifest.json", _json_bytes(_book_record(plan, context))))
  checksum_lines = [
      f"{member.sha256}  {member.name.removeprefix(f'{root}/')}"
      for member in members
  ]
  members.append(
      _member(
          f"{root}/SHA256SUMS",
          ("\n".join(checksum_lines) + "\n").encode("utf-8"),
      )
  )
  return members


def _tracks_members(plan: Plan, context: _PackageContext) -> list[_PackageMember]:
  members: list[_PackageMember] = []
  for track in context.tracks:
    members.append(_member(f"audio/{track.path.name}", track.path))
  cover_path = verified_cover_path(plan)
  if cover_path is not None:
    members.append(_member(cover_path.name, cover_path))
  members.append(_member("metadata.json", _json_bytes(plan.manifest["book"])))
  members.append(_member("manifest.json", _json_bytes(_book_record(plan, context))))
  if context.quality is not None and context.quality_path is not None:
    members.append(_member("qa/report.json", context.quality_path))
    members.append(
        _member(
            "qa/report.html",
            html_document(context.quality).encode("utf-8"),
        )
    )
  checksum_lines = [f"{member.sha256}  {member.name}" for member in members]
  members.append(
      _member(
          "SHA256SUMS",
          ("\n".join(checksum_lines) + "\n").encode("utf-8"),
      )
  )
  return members


def package_bookplayer(
    *,
    plan: Plan,
    run: GenerationRun,
    output_directory: Path,
    allow_unvalidated: bool = False,
    allow_failed_qa: bool = False,
) -> PackageArtifact:
  """Build and deeply verify BookPlayer's ordered flat-MP3 ZIP."""
  with WorkspaceLock(plan.workspace):
    context = _package_context(
        plan=plan,
        supplied_run=run,
        allow_unvalidated=allow_unvalidated,
        allow_failed_qa=allow_failed_qa,
    )
    members = [_member(track.path.name, track.path) for track in context.tracks]
    package_id = _members_sha256(members)
    book_slug = slugify(str(plan.manifest["book"]["title"]))
    target = output_directory / f"{book_slug}-bookplayer-{package_id[:20]}.zip"
    artifact = _publish_zip(target, members)
    return PackageArtifact("bookplayer", artifact.path, artifact.sha256, artifact.bytes)


def package_archive(
    *,
    plan: Plan,
    run: GenerationRun,
    output_directory: Path,
    allow_unvalidated: bool = False,
    allow_failed_qa: bool = False,
) -> PackageArtifact:
  """Build and deeply verify a checksummed portable archive ZIP."""
  with WorkspaceLock(plan.workspace):
    context = _package_context(
        plan=plan,
        supplied_run=run,
        allow_unvalidated=allow_unvalidated,
        allow_failed_qa=allow_failed_qa,
    )
    book_slug = slugify(str(plan.manifest["book"]["title"]))
    members = _archive_members(plan, context, book_slug)
    package_id = _members_sha256(members)
    target = output_directory / f"{book_slug}-archive-{package_id[:20]}.zip"
    artifact = _publish_zip(target, members)
    return PackageArtifact("archive", artifact.path, artifact.sha256, artifact.bytes)


def package_tracks(
    *,
    plan: Plan,
    run: GenerationRun,
    output_directory: Path,
    allow_unvalidated: bool = False,
    allow_failed_qa: bool = False,
) -> PackageArtifact:
  """Publish and deeply verify a directly browsable tagged-track directory."""
  with WorkspaceLock(plan.workspace):
    context = _package_context(
        plan=plan,
        supplied_run=run,
        allow_unvalidated=allow_unvalidated,
        allow_failed_qa=allow_failed_qa,
    )
    members = _tracks_members(plan, context)
    package_id = _members_sha256(members)
    book_slug = slugify(str(plan.manifest["book"]["title"]))
    target = output_directory / f"{book_slug}-tracks-{package_id[:20]}"
    return _publish_directory(target, members)
