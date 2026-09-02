"""Validated deterministic track-directory, BookPlayer, and archival outputs."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Iterable

from ..errors import PackagingError
from ..models import MANIFEST_VERSION, Plan
from ..utils import (
    atomic_write_json,
    atomic_write_text,
    canonical_json,
    load_json,
    sha256_bytes,
    sha256_file,
    sha256_text,
    slugify,
    utc_now,
)
from ..workspace.generation import GenerationRun


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
  duration_seconds: float


def _collect_tracks(plan: Plan, run: GenerationRun) -> list[TrackArtifact]:
  if run.manifest.get("status") != "complete":
    raise PackagingError("Packaging requires a complete generation run.")
  sections = plan.manifest.get("sections")
  states = run.manifest.get("sections")
  if not isinstance(sections, list) or not isinstance(states, dict):
    raise PackagingError("Plan or generation section manifests are invalid.")
  if len(sections) != int(plan.manifest.get("track_count", -1)):
    raise PackagingError("Plan track count is inconsistent.")
  run_root = run.run_path.parent
  tracks: list[TrackArtifact] = []
  for section in sections:
    number = int(section["track_number"])
    state = states.get(str(number))
    if not isinstance(state, dict) or state.get("status") != "complete":
      raise PackagingError(f"Track {number} is incomplete.")
    final = state.get("final_audio")
    if not isinstance(final, dict):
      raise PackagingError(f"Track {number} has no final audio checkpoint.")
    filename = final.get("file")
    if not isinstance(filename, str) or Path(filename).name != filename:
      raise PackagingError(f"Track {number} has an unsafe filename.")
    path = run_root / "audio" / filename
    if not path.is_file():
      raise PackagingError(f"Track {number} audio is missing: {path}")
    digest = sha256_file(path)
    if digest != final.get("sha256"):
      raise PackagingError(f"Track {number} audio hash does not match its checkpoint.")
    tracks.append(
        TrackArtifact(
            track_number=number,
            title=str(section["title"]),
            path=path,
            sha256=digest,
            duration_seconds=float(final.get("duration_seconds", 0.0)),
        )
    )
  expected = list(range(1, len(sections) + 1))
  if [track.track_number for track in tracks] != expected:
    raise PackagingError("Tracks are not contiguous and ordered from 1.")
  return tracks


def _quality_report(
    run: GenerationRun,
    *,
    allow_unvalidated: bool,
    allow_failed_qa: bool,
) -> tuple[dict[str, Any] | None, Path | None]:
  candidates = sorted(
      run.run_path.parent.glob("qa/*/report.json"),
      key=lambda path: path.stat().st_mtime,
      reverse=True,
  )
  if not candidates:
    if allow_unvalidated:
      return None, None
    raise PackagingError(
        "No QA report exists for this run. Run `ebook-tts validate`, or use "
        "--allow-unvalidated explicitly."
    )
  path = candidates[0]
  report = load_json(path)
  if report.get("run_id") != run.run_id:
    raise PackagingError(f"QA report does not match this generation run: {path}")
  if report.get("status") == "fail" and not allow_failed_qa:
    raise PackagingError(
        f"QA report {path} failed. Correct the failures or explicitly use "
        "--allow-failed-qa."
    )
  return report, path


def _zip_info(name: str) -> zipfile.ZipInfo:
  info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
  info.compress_type = zipfile.ZIP_STORED
  info.create_system = 3
  info.external_attr = 0o100644 << 16
  info.flag_bits |= 0x800
  return info


def _write_stream(archive: zipfile.ZipFile, name: str, source: BinaryIO) -> None:
  with archive.open(_zip_info(name), "w", force_zip64=True) as destination:
    shutil.copyfileobj(source, destination, length=1024 * 1024)


def _write_file(archive: zipfile.ZipFile, name: str, path: Path) -> None:
  with path.open("rb") as source:
    _write_stream(archive, name, source)


def _write_bytes(archive: zipfile.ZipFile, name: str, value: bytes) -> None:
  with archive.open(_zip_info(name), "w", force_zip64=True) as destination:
    destination.write(value)


def _publish_zip(
    target: Path,
    writer: Any,
    expected_names: list[str],
) -> PackageArtifact:
  target.parent.mkdir(parents=True, exist_ok=True)
  if target.exists():
    try:
      with zipfile.ZipFile(target, "r") as archive:
        if archive.namelist() == expected_names and archive.testzip() is None:
          return PackageArtifact("existing", target, sha256_file(target), target.stat().st_size)
    except zipfile.BadZipFile:
      pass
    raise PackagingError(
        f"Refusing to overwrite an existing non-matching artifact: {target}"
    )
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
      writer(archive)
    with zipfile.ZipFile(temporary, "r") as archive:
      if archive.namelist() != expected_names:
        raise PackagingError("Generated ZIP member order or names are incorrect.")
      invalid = archive.testzip()
      if invalid is not None:
        raise PackagingError(f"Generated ZIP failed CRC validation at {invalid}.")
    os.replace(temporary, target)
  finally:
    temporary.unlink(missing_ok=True)
  return PackageArtifact("zip", target, sha256_file(target), target.stat().st_size)


def _book_record(
    plan: Plan,
    run: GenerationRun,
    tracks: list[TrackArtifact],
    quality: dict[str, Any] | None,
) -> dict[str, Any]:
  return {
      "manifest_version": MANIFEST_VERSION,
      "kind": "ebook-tts-distribution",
      "plan_sha256": plan.plan_id,
      "run_id": run.run_id,
      "book": plan.manifest["book"],
      "quality": (
          {
              "qa_id": quality.get("qa_id"),
              "status": quality.get("status"),
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
              "bytes": track.path.stat().st_size,
          }
          for track in tracks
      ],
  }


def package_bookplayer(
    *,
    plan: Plan,
    run: GenerationRun,
    output_directory: Path,
    allow_unvalidated: bool = False,
    allow_failed_qa: bool = False,
) -> PackageArtifact:
  """Build BookPlayer's convenient flat ordered-MP3 ZIP profile."""
  tracks = _collect_tracks(plan, run)
  _quality_report(
      run,
      allow_unvalidated=allow_unvalidated,
      allow_failed_qa=allow_failed_qa,
  )
  book_slug = slugify(str(plan.manifest["book"]["title"]))
  target = output_directory / f"{book_slug}-bookplayer-{run.run_id[:12]}.zip"
  names = [track.path.name for track in tracks]

  def writer(archive: zipfile.ZipFile) -> None:
    for track in tracks:
      _write_file(archive, track.path.name, track.path)

  artifact = _publish_zip(target, writer, names)
  return PackageArtifact("bookplayer", artifact.path, artifact.sha256, artifact.bytes)


def package_archive(
    *,
    plan: Plan,
    run: GenerationRun,
    output_directory: Path,
    allow_unvalidated: bool = False,
    allow_failed_qa: bool = False,
) -> PackageArtifact:
  """Build a portable ZIP with tracks, cover, checksums, manifest, and QA report."""
  tracks = _collect_tracks(plan, run)
  quality, quality_path = _quality_report(
      run,
      allow_unvalidated=allow_unvalidated,
      allow_failed_qa=allow_failed_qa,
  )
  book_slug = slugify(str(plan.manifest["book"]["title"]))
  root = book_slug
  record = _book_record(plan, run, tracks, quality)
  manifest_bytes = (json.dumps(record, ensure_ascii=False, indent=2) + "\n").encode()
  metadata_bytes = (
      json.dumps(plan.manifest["book"], ensure_ascii=False, indent=2) + "\n"
  ).encode()
  members: list[tuple[str, Path | bytes]] = [
      (f"{root}/metadata.json", metadata_bytes),
  ]
  cover = plan.manifest.get("cover")
  if isinstance(cover, dict) and isinstance(cover.get("file"), str):
    cover_path = plan.plan_path.parent / cover["file"]
    if not cover_path.is_file() or sha256_file(cover_path) != cover.get("sha256"):
      raise PackagingError("Plan cover artwork is missing or has changed.")
    members.append((f"{root}/{cover_path.name}", cover_path))
  for track in tracks:
    members.append((f"{root}/audio/{track.path.name}", track.path))
  if quality is not None and quality_path is not None:
    members.append((f"{root}/qa/report.json", quality_path))
    quality_html = quality_path.with_name("report.html")
    if quality_html.is_file():
      members.append((f"{root}/qa/report.html", quality_html))
  members.append((f"{root}/manifest.json", manifest_bytes))

  checksums: list[str] = []
  for name, source in members:
    digest = sha256_file(source) if isinstance(source, Path) else sha256_bytes(source)
    relative = name.removeprefix(f"{root}/")
    checksums.append(f"{digest}  {relative}")
  checksums_bytes = ("\n".join(checksums) + "\n").encode()
  members.append((f"{root}/SHA256SUMS", checksums_bytes))
  quality_hash = sha256_file(quality_path)[:12] if quality_path else "unvalidated"
  target = output_directory / (
      f"{book_slug}-archive-{run.run_id[:12]}-{quality_hash}.zip"
  )
  names = [name for name, _ in members]

  def writer(archive: zipfile.ZipFile) -> None:
    for name, source in members:
      if isinstance(source, Path):
        _write_file(archive, name, source)
      else:
        _write_bytes(archive, name, source)

  artifact = _publish_zip(target, writer, names)
  return PackageArtifact("archive", artifact.path, artifact.sha256, artifact.bytes)


def package_tracks(
    *,
    plan: Plan,
    run: GenerationRun,
    output_directory: Path,
    allow_unvalidated: bool = False,
    allow_failed_qa: bool = False,
) -> PackageArtifact:
  """Publish a directly browsable directory of tagged MP3 tracks."""
  tracks = _collect_tracks(plan, run)
  quality, quality_path = _quality_report(
      run,
      allow_unvalidated=allow_unvalidated,
      allow_failed_qa=allow_failed_qa,
  )
  book_slug = slugify(str(plan.manifest["book"]["title"]))
  target = output_directory / f"{book_slug}-tracks-{run.run_id[:12]}"
  if target.exists():
    checksums_path = target / "SHA256SUMS"
    if checksums_path.is_file():
      return PackageArtifact("tracks", target, sha256_file(checksums_path), 0)
    raise PackagingError(f"Refusing to overwrite existing directory: {target}")
  output_directory.mkdir(parents=True, exist_ok=True)
  temporary = Path(
      tempfile.mkdtemp(prefix=f".{target.name}.", dir=output_directory)
  )
  try:
    audio_directory = temporary / "audio"
    audio_directory.mkdir()
    for track in tracks:
      shutil.copyfile(track.path, audio_directory / track.path.name)
    cover = plan.manifest.get("cover")
    if isinstance(cover, dict) and isinstance(cover.get("file"), str):
      source = plan.plan_path.parent / cover["file"]
      if source.is_file():
        shutil.copyfile(source, temporary / source.name)
    atomic_write_json(temporary / "metadata.json", plan.manifest["book"], mode=0o644)
    atomic_write_json(
        temporary / "manifest.json",
        _book_record(plan, run, tracks, quality),
        mode=0o644,
    )
    if quality_path:
      qa_directory = temporary / "qa"
      qa_directory.mkdir()
      shutil.copyfile(quality_path, qa_directory / "report.json")
      if quality_path.with_name("report.html").is_file():
        shutil.copyfile(quality_path.with_name("report.html"), qa_directory / "report.html")
    checksum_lines = [
        f"{sha256_file(path)}  {path.relative_to(temporary).as_posix()}"
        for path in sorted(temporary.rglob("*"))
        if path.is_file() and path.name != "SHA256SUMS"
    ]
    atomic_write_text(
        temporary / "SHA256SUMS",
        "\n".join(checksum_lines) + "\n",
        mode=0o644,
    )
    os.replace(temporary, target)
  finally:
    if temporary.exists():
      shutil.rmtree(temporary)
  checksums_path = target / "SHA256SUMS"
  total_bytes = sum(path.stat().st_size for path in target.rglob("*") if path.is_file())
  return PackageArtifact("tracks", target, sha256_file(checksums_path), total_bytes)
