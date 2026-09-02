"""Offline, fail-closed adoption of verified legacy-v1 audiobook artifacts."""

from __future__ import annotations

import hashlib
import math
import os
import re
import shutil
import tempfile
import zipfile
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, TypeVar
from urllib.parse import unquote, urlsplit

from ..epub.container import EpubContainer
from ..epub.package import _cover, _manifest, _metadata, _rootfile, _spine
from ..errors import EpubError, MediaError, WorkspaceError
from ..media.tools import (
    MediaTools,
    decode_audio,
    expected_audio_format,
    media_record,
    preflight,
    probe_audio,
)
from ..models import MANIFEST_VERSION, AppConfig, BookMetadata, MediaInfo, Plan
from ..utils import (
    atomic_write_json,
    atomic_write_text,
    canonical_json,
    fsync_directory,
    load_json,
    sha256_bytes,
    sha256_file,
    sha256_text,
    utc_now,
)
from .generation import GenerationRun, load_run
from .manifests import load_plan
from .request_identity import (
    expanded_request_record,
    request_fingerprint,
    sparse_legacy_request_record,
)


LEGACY_FORMAT = "legacy-v1"
IMPORTER_VERSION = 1
LEGACY_CHUNKER_VERSION = 2
_MAX_PROVENANCE_FILE_BYTES = 4 * 1024 * 1024
_MAX_ZIP_COMPRESSION_RATIO = 100
_BENIGN_METADATA = {".DS_Store", "Thumbs.db", "desktop.ini"}
_SAFE_STEM = re.compile(r"^[0-9]{3}_[A-Za-z0-9][A-Za-z0-9_-]*$")
_HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class AdoptionResult:
  """A newly published verification-only workspace and its current records."""

  workspace: Path
  plan: Plan
  run: GenerationRun
  adoption_path: Path
  tracks: int
  chunks: int
  audio_bytes: int

  @property
  def plan_id(self) -> str:
    return self.plan.plan_id

  @property
  def run_id(self) -> str:
    return self.run.run_id

  @property
  def plan_path(self) -> Path:
    return self.plan.plan_path

  @property
  def run_path(self) -> Path:
    return self.run.run_path


@dataclass(frozen=True)
class _LegacyChunk:
  index: int
  text: str
  text_sha256: str
  characters: int
  request_sha256: str
  audio_path: Path
  audio_sha256: str
  audio_bytes: int
  media: MediaInfo
  provider_request_id: str | None
  billed_characters: int | None
  sidecar_path: Path


@dataclass(frozen=True)
class _LegacyTrack:
  track_number: int
  chapter_number: int | None
  title: str
  output_stem: str
  source_relative_path: str
  source_href: str | None
  source_member: str | None
  source_sha256: str
  text: str
  text_sha256: str
  characters: int
  chunks: tuple[_LegacyChunk, ...]
  final_path: Path
  final_sha256: str
  final_bytes: int
  final_media: MediaInfo
  plan_sha256: str
  generation_fingerprint: str
  generation: Mapping[str, Any]
  sdk_version: str | None
  plan_path: Path
  generation_path: Path


@dataclass(frozen=True)
class _SourcePublication:
  source_path: Path
  source_sha256: str
  package_path: str
  metadata: BookMetadata
  cover_bytes: bytes | None
  cover_media_type: str | None
  cover_extension: str | None


@dataclass(frozen=True)
class _LegacyInventory:
  tracks: tuple[_LegacyTrack, ...]
  generation: Mapping[str, Any]
  quarantine_files: tuple[Path, ...]
  flat_archives: tuple[Path, ...]


def _error(message: str) -> WorkspaceError:
  return WorkspaceError(f"Legacy-v1 adoption failed: {message}")


def _require_object(value: object, label: str) -> dict[str, Any]:
  if not isinstance(value, dict):
    raise _error(f"{label} must be a JSON object.")
  return value


def _require_list(value: object, label: str) -> list[Any]:
  if not isinstance(value, list):
    raise _error(f"{label} must be a JSON array.")
  return value


def _require_string(value: object, label: str, *, nonempty: bool = True) -> str:
  if not isinstance(value, str) or (nonempty and not value):
    raise _error(f"{label} must be a{' non-empty' if nonempty else ''} string.")
  if "\x00" in value:
    raise _error(f"{label} contains a NUL byte.")
  return value


def _require_int(value: object, label: str, *, minimum: int = 0) -> int:
  if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
    raise _error(f"{label} must be an integer of at least {minimum}.")
  return value


def _require_sha256(value: object, label: str) -> str:
  digest = _require_string(value, label)
  if not _HEX_SHA256.fullmatch(digest):
    raise _error(f"{label} must be a lowercase SHA-256 value.")
  return digest


def _optional_int(value: object, label: str) -> int | None:
  if value is None:
    return None
  return _require_int(value, label)


def _assert_version(record: Mapping[str, Any], label: str) -> None:
  if record.get("manifest_version") != 1:
    raise _error(f"{label} must have manifest_version 1.")


def _without_key(record: Mapping[str, Any], key: str) -> dict[str, Any]:
  result = dict(record)
  result.pop(key, None)
  return result


def _assert_fingerprint(
    record: Mapping[str, Any],
    *,
    field: str,
    unsigned: Mapping[str, Any],
    label: str,
) -> str:
  expected = _require_sha256(record.get(field), f"{label}.{field}")
  actual = sha256_text(canonical_json(unsigned))
  if actual != expected:
    raise _error(
        f"{label} fingerprint mismatch; expected {expected}, calculated {actual}."
    )
  return expected


def _is_within(path: Path, root: Path) -> bool:
  try:
    path.relative_to(root)
  except ValueError:
    return False
  return True


def _absolute_lexical(path: Path) -> Path:
  """Make a path absolute without resolving symbolic links."""
  return Path(os.path.abspath(path))


def _assert_no_symlink_components(path: Path, label: str) -> None:
  absolute = _absolute_lexical(path)
  current = Path(absolute.anchor)
  for part in absolute.parts[1:]:
    current = current / part
    if current.is_symlink():
      raise _error(f"{label} traverses symbolic link {current}.")


def _assert_no_symlinks(path: Path, root: Path, label: str) -> None:
  absolute = _absolute_lexical(path)
  lexical_root = _absolute_lexical(root)
  try:
    relative = absolute.relative_to(lexical_root)
  except ValueError as exc:
    raise _error(f"{label} escapes {root}.") from exc
  _assert_no_symlink_components(lexical_root, label)
  current = lexical_root
  for part in relative.parts:
    current = current / part
    if current.is_symlink():
      raise _error(f"{label} traverses symbolic link {current}.")


def _contained_path(
    value: object,
    *,
    bases: Iterable[Path],
    root: Path,
    label: str,
    file: bool = True,
) -> Path:
  raw = _require_string(value, label)
  if "\\" in raw:
    raise _error(f"{label} contains a non-portable path separator.")
  supplied = Path(raw).expanduser()
  candidates = [supplied] if supplied.is_absolute() else [base / supplied for base in bases]
  matches: list[Path] = []
  for candidate in candidates:
    lexical = _absolute_lexical(candidate)
    if not _is_within(lexical, root):
      continue
    _assert_no_symlinks(lexical, root, label)
    try:
      resolved = lexical.resolve(strict=True)
    except OSError:
      continue
    if not _is_within(resolved, root):
      continue
    if file and not resolved.is_file():
      continue
    if not file and not resolved.is_dir():
      continue
    if resolved not in matches:
      matches.append(resolved)
  if not matches:
    raise _error(f"{label} is missing, unsafe, or outside {root}: {raw!r}.")
  if len(matches) > 1:
    raise _error(f"{label} is ambiguous relative to the legacy tree: {raw!r}.")
  return matches[0]


def _read_verified_text(
    path: Path,
    label: str,
    *,
    expected_sha256: str,
    expected_characters: int,
) -> str:
  try:
    raw = path.read_bytes()
    value = raw.decode("utf-8")
  except (OSError, UnicodeDecodeError) as exc:
    raise _error(f"{label} is not readable strict UTF-8: {path}.") from exc
  candidates = [value]
  if value.endswith("\n"):
    candidates.append(value[:-1])
  matches = {
      candidate
      for candidate in candidates
      if len(candidate) == expected_characters
      and sha256_text(candidate) == expected_sha256
  }
  if len(matches) != 1:
    raise _error(
        f"{label} has no unique raw-or-LF-stripped representation matching "
        "its recorded hash and character count."
    )
  return next(iter(matches))


_T = TypeVar("_T")
_MISSING = object()


def _coalesced(
    entries: Iterable[tuple[str, object]],
    *,
    label: str,
    parser: Callable[[object, str], _T],
    required: bool = True,
) -> _T | object:
  present = list(entries)
  if not present:
    if required:
      raise _error(f"{label} is missing.")
    return _MISSING
  parsed = [(name, parser(value, name)) for name, value in present]
  first = parsed[0][1]
  if any(value != first for _, value in parsed[1:]):
    fields = ", ".join(name for name, _ in parsed)
    raise _error(f"{label} has conflicting aliases: {fields}.")
  return first


def _alias_value(
    record: Mapping[str, Any],
    *names: str,
    label: str,
    required: bool = True,
) -> object:
  present = [(f"{label}.{name}", record[name]) for name in names if name in record]
  value = _coalesced(
      present,
      label=label,
      parser=lambda item, _name: item,
      required=required,
  )
  return value


def _path_field(record: Mapping[str, Any], *names: str, label: str) -> object:
  return _alias_value(record, *names, label=label)


def _sha_field(record: Mapping[str, Any], *names: str, label: str) -> str:
  value = _coalesced(
      [(f"{label}.{name}", record[name]) for name in names if name in record],
      label=label,
      parser=_require_sha256,
  )
  assert isinstance(value, str)
  return value


def _verify_count_fields(
    record: Mapping[str, Any],
    names: tuple[str, ...],
    *,
    expected: int,
    label: str,
    required: bool,
) -> None:
  present = [name for name in names if name in record]
  if required and not present:
    raise _error(f"{label} must record one of: {', '.join(names)}.")
  for name in present:
    value = _require_int(record[name], f"{label}.{name}", minimum=0)
    if value != expected:
      raise _error(
          f"{label}.{name} is {value}, but the verified chunk count is {expected}."
      )


def _portable_relative_parts(value: object, label: str) -> tuple[str, ...]:
  raw = _require_string(value, label)
  if (
      raw.startswith("/")
      or "\\" in raw
      or re.match(r"^[A-Za-z]:", raw)
  ):
    raise _error(f"{label} must be a portable relative path: {raw!r}.")
  parts = tuple(raw.split("/"))
  if not parts or any(part in {"", ".", ".."} for part in parts):
    raise _error(f"{label} contains an unsafe path component: {raw!r}.")
  return parts


def _legacy_artifact_path(
    value: object,
    *,
    bases: Iterable[Path],
    legacy_root: Path,
    output: Path,
    label: str,
) -> Path:
  parts = _portable_relative_parts(value, label)
  matches: list[Path] = []
  for base in bases:
    candidate = base.joinpath(*parts)
    lexical = _absolute_lexical(candidate)
    if not _is_within(lexical, legacy_root) or not _is_within(lexical, output):
      continue
    _assert_no_symlinks(lexical, legacy_root, label)
    try:
      resolved = lexical.resolve(strict=True)
    except OSError:
      continue
    if not resolved.is_file():
      continue
    if not _is_within(resolved, legacy_root) or not _is_within(resolved, output):
      continue
    if resolved not in matches:
      matches.append(resolved)
  if not matches:
    raise _error(
        f"{label} is missing, unsafe, or outside the legacy output: "
        f"{'/'.join(parts)!r}."
    )
  if len(matches) > 1:
    raise _error(f"{label} is ambiguous relative to the legacy tree.")
  return matches[0]


def _artifact_alias_path(
    record: Mapping[str, Any],
    names: tuple[str, ...],
    *,
    bases: Iterable[Path],
    legacy_root: Path,
    output: Path,
    label: str,
    required: bool = True,
) -> Path | None:
  present = [(name, record[name]) for name in names if name in record]
  if not present:
    if required:
      raise _error(f"{label} must record one of: {', '.join(names)}.")
    return None
  resolved = [
      (
          name,
          _legacy_artifact_path(
              value,
              bases=bases,
              legacy_root=legacy_root,
              output=output,
              label=f"{label}.{name}",
          ),
      )
      for name, value in present
  ]
  first = resolved[0][1]
  if any(path != first for _, path in resolved[1:]):
    fields = ", ".join(name for name, _ in resolved)
    raise _error(f"{label} has conflicting path aliases: {fields}.")
  return first


def _media_mapping(record: Mapping[str, Any], label: str) -> Mapping[str, Any]:
  if "media" not in record:
    return {}
  return _require_object(record["media"], f"{label}.media")


def _media_value(
    record: Mapping[str, Any],
    names: tuple[str, ...],
    *,
    label: str,
    parser: Callable[[object, str], _T],
    required: bool = True,
) -> _T | object:
  nested = _media_mapping(record, label)
  entries = [
      (f"{label}.{name}", record[name]) for name in names if name in record
  ]
  entries.extend(
      (f"{label}.media.{name}", nested[name])
      for name in names
      if name in nested
  )
  return _coalesced(
      entries,
      label=f"{label}.{names[0]}",
      parser=parser,
      required=required,
  )


def _record_bytes(record: Mapping[str, Any], label: str) -> int:
  value = _media_value(
      record,
      ("bytes",),
      label=label,
      parser=lambda item, name: _require_int(item, name, minimum=1),
  )
  assert isinstance(value, int)
  return value


def _record_audio_sha(record: Mapping[str, Any], label: str) -> str:
  value = _media_value(
      record,
      ("audio_sha256", "sha256"),
      label=label,
      parser=_require_sha256,
  )
  assert isinstance(value, str)
  return value


def _float(value: object, label: str) -> float:
  if isinstance(value, bool) or not isinstance(value, (int, float)):
    raise _error(f"{label} must be numeric.")
  result = float(value)
  if not math.isfinite(result) or result <= 0:
    raise _error(f"{label} must be finite and positive.")
  return result


def _bit_rate(value: object, label: str) -> int | None:
  if value is None:
    return None
  return _require_int(value, label, minimum=1)


def _verify_media_record(record: object, actual: MediaInfo, label: str) -> None:
  value = _require_object(record, label)
  codec = _media_value(
      value,
      ("codec",),
      label=label,
      parser=lambda item, name: _require_string(item, name),
  )
  sample_rate = _media_value(
      value,
      ("sample_rate",),
      label=label,
      parser=lambda item, name: _require_int(item, name, minimum=1),
  )
  bit_rate = _media_value(
      value,
      ("bit_rate",),
      label=label,
      parser=_bit_rate,
  )
  channels = _media_value(
      value,
      ("channels",),
      label=label,
      parser=lambda item, name: _require_int(item, name, minimum=1),
  )
  duration = _media_value(
      value,
      ("duration_seconds",),
      label=label,
      parser=_float,
  )
  expected_bytes = _record_bytes(value, label)
  expected_sha = _record_audio_sha(value, label)

  if codec != actual.codec:
    raise _error(f"{label}.codec does not match current ffprobe output.")
  if sample_rate != actual.sample_rate:
    raise _error(f"{label}.sample_rate does not match current ffprobe output.")
  if channels != actual.channels:
    raise _error(f"{label}.channels does not match current ffprobe output.")
  if expected_bytes != actual.bytes:
    raise _error(f"{label}.bytes does not match the audio file.")
  if expected_sha != actual.sha256:
    raise _error(f"{label}.sha256 does not match the audio file.")
  assert isinstance(duration, float)
  duration_tolerance = max(0.01, actual.duration_seconds * 0.005)
  if abs(duration - actual.duration_seconds) > duration_tolerance:
    raise _error(f"{label}.duration_seconds differs from current ffprobe output.")
  if bit_rate is None:
    if actual.bit_rate is not None:
      raise _error(f"{label}.bit_rate is missing but current ffprobe reports one.")
  else:
    if actual.bit_rate is None:
      raise _error(f"{label}.bit_rate is present but current ffprobe omits it.")
    tolerance = max(2_000, int(actual.bit_rate * 0.03))
    if abs(bit_rate - actual.bit_rate) > tolerance:
      raise _error(f"{label}.bit_rate differs from current ffprobe output.")


def _verify_audio(
    path: Path,
    record: Mapping[str, Any],
    *,
    tools: MediaTools,
    output_format: str,
    label: str,
    require_media: bool = True,
) -> MediaInfo:
  expected_hash = _record_audio_sha(record, label)
  expected_bytes = _record_bytes(record, label)
  actual_hash = sha256_file(path)
  actual_bytes = path.stat().st_size
  if actual_hash != expected_hash or actual_bytes != expected_bytes:
    raise _error(f"{label} audio bytes do not match the recorded size/hash: {path}.")
  info = probe_audio(path, tools.ffprobe, expected_format=output_format)
  decode_audio(path, tools.ffmpeg)
  if require_media:
    _verify_media_record(record, info, label)
  return info


def _verified_request_sha256(
    generated: Mapping[str, Any],
    *,
    text: str,
    previous_text: str,
    next_text: str,
    generation: Mapping[str, Any],
    label: str,
) -> str:
  common = {
      "text": text,
      "voice_id": str(generation["voice_id"]),
      "model_id": str(generation["model_id"]),
      "output_format": str(generation["output_format"]),
      "previous_text": previous_text,
      "next_text": next_text,
  }
  sparse = sparse_legacy_request_record(**common)
  request: Mapping[str, Any] = sparse
  if "request" in generated:
    persisted = _require_object(generated["request"], f"{label}.request")
    expanded = expanded_request_record(**common)
    if persisted != sparse and persisted != expanded:
      raise _error(f"{label}.request does not prove a supported paid request shape.")
    request = persisted
  request_sha = request_fingerprint(request)
  recorded_sha = _require_sha256(
      generated.get("request_sha256"), f"{label}.request_sha256"
  )
  if recorded_sha != request_sha:
    raise _error(f"{label} request fingerprint mismatch.")
  return request_sha


def _legacy_sdk_version(record: Mapping[str, Any], label: str) -> str | None:
  value = _coalesced(
      [
          (f"{label}.{name}", record[name])
          for name in ("elevenlabs_sdk_version", "provider_sdk_version")
          if name in record
      ],
      label=f"{label} SDK version",
      parser=lambda item, name: _require_string(item, name),
      required=False,
  )
  if value is _MISSING:
    return None
  assert isinstance(value, str)
  return value


def _generation_core(record: Mapping[str, Any], label: str) -> dict[str, Any]:
  provider = record.get("provider", "elevenlabs")
  if provider != "elevenlabs":
    raise _error(f"{label}.provider must be 'elevenlabs'.")
  voice_id = _require_string(record.get("voice_id"), f"{label}.voice_id")
  model_id = _require_string(record.get("model_id"), f"{label}.model_id")
  output_format = _require_string(record.get("output_format"), f"{label}.output_format")
  try:
    expected_audio_format(output_format)
  except MediaError as exc:
    raise _error(f"{label}.output_format is not a supported MP3 profile: {exc}") from exc
  context = _require_int(
      record.get("context_characters"), f"{label}.context_characters", minimum=0
  )
  settings = record.get("voice_settings", {})
  if settings not in ({}, None):
    raise _error(f"{label}.voice_settings must be empty in legacy v1.")
  _legacy_sdk_version(record, label)
  return {
      "provider": "elevenlabs",
      "voice_id": voice_id,
      "model_id": model_id,
      "output_format": output_format,
      "context_characters": context,
      "voice_settings": {},
  }


def _legacy_generation_fingerprint(
    plan_sha256: str,
    generation: Mapping[str, Any],
) -> str:
  legacy_generation = {
      "voice_id": generation["voice_id"],
      "model_id": generation["model_id"],
      "output_format": generation["output_format"],
      "context_characters": generation["context_characters"],
  }
  return sha256_text(
      canonical_json(
          {"plan_sha256": plan_sha256, "generation": legacy_generation}
      )
  )


def _scan_tree(root: Path) -> tuple[Path, ...]:
  files: list[Path] = []

  def visit(directory: Path) -> None:
    try:
      entries = sorted(os.scandir(directory), key=lambda item: item.name)
    except OSError as exc:
      raise _error(f"could not inventory {directory}: {exc}") from exc
    for entry in entries:
      path = Path(entry.path)
      if entry.is_symlink():
        raise _error(f"symbolic links are not allowed in legacy output: {path}.")
      if entry.is_dir(follow_symlinks=False):
        visit(path)
      elif entry.is_file(follow_symlinks=False):
        files.append(path.resolve())
      else:
        raise _error(f"unsupported filesystem entry in legacy output: {path}.")

  visit(root)
  return tuple(files)


def _in_quarantine(path: Path, output: Path) -> bool:
  relative = path.relative_to(output)
  return any(part.casefold() == "quarantine" for part in relative.parts[:-1])


def _inventory_attempts(output: Path, files: Iterable[Path]) -> tuple[Path, ...]:
  quarantine: list[Path] = []
  for path in files:
    is_evidence = path.name.endswith(".attempt.json") or path.name.endswith(".part")
    if not is_evidence:
      continue
    if _in_quarantine(path, output):
      quarantine.append(path)
    else:
      raise _error(f"active ambiguous request evidence blocks adoption: {path}.")
  return tuple(sorted(quarantine))


def _track_directories(output: Path) -> tuple[Path, ...]:
  chunks_root = output / "chunks"
  if not chunks_root.is_dir() or chunks_root.is_symlink():
    raise _error(f"legacy chunk directory is missing or unsafe: {chunks_root}.")
  directories: list[Path] = []
  for entry in sorted(chunks_root.iterdir(), key=lambda item: item.name):
    if entry.name in _BENIGN_METADATA or entry.name.startswith("._"):
      if entry.is_dir():
        raise _error(f"benign metadata name unexpectedly names a directory: {entry}.")
      continue
    if entry.is_symlink() or not entry.is_dir():
      raise _error(f"unexpected entry under legacy chunks directory: {entry}.")
    if not (entry / "chunk_plan.json").is_file():
      raise _error(f"track directory lacks chunk_plan.json: {entry}.")
    if not (entry / "generation_manifest.json").is_file():
      raise _error(f"track directory lacks generation_manifest.json: {entry}.")
    directories.append(entry.resolve())
  if not directories:
    raise _error(f"no legacy track directories were found under {chunks_root}.")
  return tuple(directories)


def _identity(record: Mapping[str, Any], label: str) -> tuple[int, int | None, str, str]:
  track = _require_int(record.get("track_number"), f"{label}.track_number", minimum=1)
  chapter = _optional_int(record.get("chapter_number"), f"{label}.chapter_number")
  title = _require_string(record.get("title"), f"{label}.title")
  stem = _require_string(record.get("output_stem"), f"{label}.output_stem")
  if Path(stem).name != stem or not _SAFE_STEM.fullmatch(stem):
    raise _error(f"{label}.output_stem is unsafe: {stem!r}.")
  if not stem.startswith(f"{track:03d}_"):
    raise _error(f"{label}.output_stem does not match track {track}.")
  return track, chapter, title, stem


def _legacy_chunker_version(plan: Mapping[str, Any], label: str) -> int:
  direct = plan.get("chunker_version")
  versions = plan.get("versions")
  nested = versions.get("chunker") if isinstance(versions, dict) else None
  values = [value for value in (direct, nested) if value is not None]
  if not values or any(value != LEGACY_CHUNKER_VERSION for value in values):
    raise _error(f"{label} must record legacy chunker version 2.")
  return LEGACY_CHUNKER_VERSION


def _source_record(plan: Mapping[str, Any], label: str) -> tuple[object, str]:
  nested_value = plan.get("source", {})
  nested = _require_object(nested_value, f"{label}.source") if "source" in plan else {}
  file_entries: list[tuple[str, object]] = []
  if "source_file" in plan:
    file_entries.append((f"{label}.source_file", plan["source_file"]))
  if "file" in nested:
    file_entries.append((f"{label}.source.file", nested["file"]))
  source_file = _coalesced(
      file_entries,
      label=f"{label}.source_file",
      parser=lambda item, _name: item,
  )

  sha_entries: list[tuple[str, object]] = []
  if "source_sha256" in plan:
    sha_entries.append((f"{label}.source_sha256", plan["source_sha256"]))
  if "sha256" in nested:
    sha_entries.append((f"{label}.source.sha256", nested["sha256"]))
  source_sha = _coalesced(
      sha_entries,
      label=f"{label}.source_sha256",
      parser=_require_sha256,
  )
  assert isinstance(source_sha, str)
  return source_file, source_sha


def _full_text_record(
    plan: Mapping[str, Any], label: str
) -> tuple[object, str, int]:
  nested_value = plan.get("text", {})
  nested = _require_object(nested_value, f"{label}.text") if "text" in plan else {}
  file_entries = [
      (f"{label}.{name}", plan[name])
      for name in ("text_file", "full_text_file")
      if name in plan
  ]
  if "file" in nested:
    file_entries.append((f"{label}.text.file", nested["file"]))
  text_file = _coalesced(
      file_entries,
      label=f"{label}.text_file",
      parser=lambda item, _name: item,
  )

  sha_entries: list[tuple[str, object]] = []
  if "text_sha256" in plan:
    sha_entries.append((f"{label}.text_sha256", plan["text_sha256"]))
  if "sha256" in nested:
    sha_entries.append((f"{label}.text.sha256", nested["sha256"]))
  text_sha = _coalesced(
      sha_entries,
      label=f"{label}.text_sha256",
      parser=_require_sha256,
  )

  character_entries: list[tuple[str, object]] = []
  if "characters" in plan:
    character_entries.append((f"{label}.characters", plan["characters"]))
  if "characters" in nested:
    character_entries.append(
        (f"{label}.text.characters", nested["characters"])
    )
  characters = _coalesced(
      character_entries,
      label=f"{label}.characters",
      parser=lambda item, name: _require_int(item, name),
  )
  assert isinstance(text_sha, str)
  assert isinstance(characters, int)
  return text_file, text_sha, characters


def _verify_legacy_track(
    directory: Path,
    *,
    legacy_root: Path,
    output: Path,
    tools: MediaTools,
) -> _LegacyTrack:
  plan_path = directory / "chunk_plan.json"
  generation_path = directory / "generation_manifest.json"
  plan = load_json(plan_path)
  generation_manifest = load_json(generation_path)
  plan_label = f"{directory.name}/chunk_plan.json"
  generation_label = f"{directory.name}/generation_manifest.json"
  _assert_version(plan, plan_label)
  _assert_version(generation_manifest, generation_label)
  if plan.get("kind") not in (None, "ebook-tts-chunk-plan", "ebook-tts-plan"):
    raise _error(f"{plan_label}.kind is not a recognized legacy plan kind.")
  if generation_manifest.get("kind") not in (
      None,
      "ebook-tts-generation",
      "ebook-tts-generation-manifest",
  ):
    raise _error(f"{generation_label}.kind is not a recognized generation kind.")
  _legacy_chunker_version(plan, plan_label)
  plan_sha = _assert_fingerprint(
      plan,
      field="plan_sha256",
      unsigned=_without_key(plan, "plan_sha256"),
      label=plan_label,
  )
  plan_identity = _identity(plan, plan_label)
  generation_identity = _identity(generation_manifest, generation_label)
  if generation_identity != plan_identity:
    raise _error(f"track/title/chapter/stem fields disagree in {directory}.")
  track_number, chapter_number, title, output_stem = plan_identity
  if directory.name != output_stem:
    raise _error(f"track directory name does not match output_stem {output_stem!r}.")

  source_file_value, source_sha = _source_record(plan, plan_label)
  source_path = _contained_path(
      source_file_value,
      bases=(directory, legacy_root),
      root=legacy_root,
      label=f"{plan_label}.source_file",
  )
  if sha256_file(source_path) != source_sha:
    raise _error(f"{plan_label} source file hash mismatch: {source_path}.")
  source_relative_path = source_path.relative_to(legacy_root).as_posix()
  source_href_value = plan.get("source_href")
  source_href = (
      _require_string(source_href_value, f"{plan_label}.source_href")
      if source_href_value is not None
      else None
  )

  text_file_value, text_sha, characters = _full_text_record(plan, plan_label)
  text_path = _contained_path(
      text_file_value,
      bases=(directory, legacy_root),
      root=legacy_root,
      label=f"{plan_label}.text_file",
  )
  text = _read_verified_text(
      text_path,
      f"{plan_label}.text_file",
      expected_sha256=text_sha,
      expected_characters=characters,
  )
  if sha256_text(text) != text_sha or len(text) != characters:
    raise _error(f"{plan_label} full text hash/character count mismatch.")

  raw_chunks = _require_list(plan.get("chunks"), f"{plan_label}.chunks")
  chunk_count = len(raw_chunks)
  if chunk_count < 1:
    raise _error(f"{plan_label}.chunks must contain at least one chunk.")
  _verify_count_fields(
      plan,
      ("chunk_count",),
      expected=chunk_count,
      label=plan_label,
      required=True,
  )
  planned: list[tuple[int, str, str, int]] = []
  for position, raw_chunk in enumerate(raw_chunks, start=1):
    chunk_label = f"{plan_label}.chunks[{position - 1}]"
    record = _require_object(raw_chunk, chunk_label)
    index = _require_int(record.get("index"), f"{chunk_label}.index", minimum=1)
    if index != position:
      raise _error(f"{plan_label} chunk indices are not ordered and contiguous from 1.")
    chunk_path = _contained_path(
        _path_field(record, "text_file", "file", label=f"{chunk_label}.text_file"),
        bases=(directory, legacy_root),
        root=legacy_root,
        label=f"{chunk_label}.text_file",
    )
    expected_chunk_name = f"chunk_{index:03d}.txt"
    if chunk_path.parent != directory or chunk_path.name != expected_chunk_name:
      raise _error(
          f"{chunk_label}.text_file must resolve to {directory / expected_chunk_name}."
      )
    chunk_sha = _sha_field(
        record, "sha256", "text_sha256", label=f"{chunk_label}.sha256"
    )
    chunk_characters = _require_int(
        record.get("characters"), f"{chunk_label}.characters"
    )
    chunk_text = _read_verified_text(
        chunk_path,
        f"{plan_label} chunk {index}",
        expected_sha256=chunk_sha,
        expected_characters=chunk_characters,
    )
    if sha256_text(chunk_text) != chunk_sha or len(chunk_text) != chunk_characters:
      raise _error(f"{plan_label} chunk {index} text hash/character count mismatch.")
    planned.append((index, chunk_text, chunk_sha, chunk_characters))
  full_tokens = re.findall(r"\S+", text)
  chunk_tokens = [token for _, value, _, _ in planned for token in re.findall(r"\S+", value)]
  if chunk_tokens != full_tokens:
    raise _error(f"{plan_label} chunk token sequence does not reproduce full track text.")

  if generation_manifest.get("status") != "complete":
    raise _error(f"{generation_label} is not complete.")
  if generation_manifest.get("plan_sha256") != plan_sha:
    raise _error(f"{generation_label}.plan_sha256 does not match its chunk plan.")
  raw_generation = _require_object(
      generation_manifest.get("generation"), f"{generation_label}.generation"
  )
  core_generation = _generation_core(raw_generation, f"{generation_label}.generation")
  sdk_version = _legacy_sdk_version(raw_generation, f"{generation_label}.generation")
  generation_fingerprint = _require_sha256(
      generation_manifest.get("generation_fingerprint"),
      f"{generation_label}.generation_fingerprint",
  )
  calculated_generation = _legacy_generation_fingerprint(plan_sha, core_generation)
  if generation_fingerprint != calculated_generation:
    raise _error(f"{generation_label} generation fingerprint mismatch.")
  raw_generated_chunks = _require_list(
      generation_manifest.get("chunks"), f"{generation_label}.chunks"
  )
  generation_count = len(raw_generated_chunks)
  _verify_count_fields(
      generation_manifest,
      ("chunk_count", "completed_chunks"),
      expected=generation_count,
      label=generation_label,
      required=True,
  )
  if generation_count != chunk_count:
    raise _error(f"{generation_label} chunk records disagree with its plan.")

  effective_context = (
      0 if core_generation["model_id"] == "eleven_v3" else core_generation["context_characters"]
  )
  verified_chunks: list[_LegacyChunk] = []
  for position, ((index, chunk_text, chunk_sha, chunk_characters), raw_generated) in enumerate(
      zip(planned, raw_generated_chunks), start=1
  ):
    chunk_label = f"{generation_label}.chunks[{position - 1}]"
    generated = _require_object(raw_generated, chunk_label)
    generated_index = _require_int(
        generated.get("index"), f"{chunk_label}.index", minimum=1
    )
    if generated_index != index:
      raise _error(f"{generation_label} generated chunks are not ordered and contiguous.")
    previous_text = planned[position - 2][1][-effective_context:] if effective_context and position > 1 else ""
    next_text = planned[position][1][:effective_context] if effective_context and position < len(planned) else ""
    request_sha = _verified_request_sha256(
        generated,
        text=chunk_text,
        previous_text=previous_text,
        next_text=next_text,
        generation=core_generation,
        label=chunk_label,
    )
    generated_text_sha = _sha_field(
        generated, "text_sha256", label=f"{chunk_label}.text_sha256"
    )
    if generated_text_sha != chunk_sha:
      raise _error(f"{generation_label} chunk {index} text fingerprint mismatch.")

    audio_path = _legacy_artifact_path(
        generated.get("audio_file"),
        bases=(directory, legacy_root, output),
        legacy_root=legacy_root,
        output=output,
        label=f"{chunk_label}.audio_file",
    )
    expected_name = f"chunk_{index:03d}_{request_sha}.mp3"
    if audio_path.parent != directory or audio_path.name != expected_name:
      raise _error(
          f"{generation_label} chunk {index} audio must resolve to "
          f"{directory / expected_name}."
      )

    sidecar_path = audio_path.with_suffix(audio_path.suffix + ".json")
    recorded_sidecar = _artifact_alias_path(
        generated,
        ("audio_sidecar", "sidecar_file"),
        bases=(directory, legacy_root, output),
        legacy_root=legacy_root,
        output=output,
        label=f"{chunk_label}.sidecar",
        required=False,
    )
    if recorded_sidecar is not None and recorded_sidecar != sidecar_path:
      raise _error(f"{generation_label} chunk {index} sidecar path is not adjacent to audio.")
    if not sidecar_path.is_file() or sidecar_path.is_symlink():
      raise _error(f"{generation_label} chunk {index} sidecar is missing or unsafe.")
    sidecar_path = sidecar_path.resolve(strict=True)
    _assert_no_symlinks(sidecar_path, legacy_root, f"{generation_label} chunk {index} sidecar")
    sidecar = load_json(sidecar_path)
    sidecar_label = sidecar_path.name
    _assert_version(sidecar, sidecar_label)
    if sidecar.get("kind") not in (None, "ebook-tts-chunk"):
      raise _error(f"{sidecar_label}.kind is invalid.")
    if _require_sha256(
        sidecar.get("request_sha256"), f"{sidecar_label}.request_sha256"
    ) != request_sha:
      raise _error(f"{sidecar_label}.request_sha256 does not match the paid request.")
    if _require_sha256(
        sidecar.get("text_sha256"), f"{sidecar_label}.text_sha256"
    ) != chunk_sha:
      raise _error(f"{sidecar_label}.text_sha256 does not match the plan.")
    sidecar_audio_path = _legacy_artifact_path(
        sidecar.get("audio_file"),
        bases=(directory, legacy_root, output),
        legacy_root=legacy_root,
        output=output,
        label=f"{sidecar_label}.audio_file",
    )
    if sidecar_audio_path != audio_path:
      raise _error(f"{sidecar_label}.audio_file is not adjacent to its sidecar.")

    generated_hash = _record_audio_sha(generated, f"{generation_label} chunk {index}")
    generated_bytes = _record_bytes(generated, f"{generation_label} chunk {index}")
    if _record_audio_sha(sidecar, sidecar_label) != generated_hash:
      raise _error(f"{sidecar_label} audio hash disagrees with generation record.")
    if _record_bytes(sidecar, sidecar_label) != generated_bytes:
      raise _error(f"{sidecar_label} byte count disagrees with generation record.")
    info = _verify_audio(
        audio_path,
        sidecar,
        tools=tools,
        output_format=core_generation["output_format"],
        label=sidecar_label,
    )
    _verify_media_record(generated, info, f"{generation_label} chunk {index}")
    provider_request_id = sidecar.get("provider_request_id")
    if provider_request_id is not None and not isinstance(provider_request_id, str):
      raise _error(f"{sidecar_path.name}.provider_request_id must be a string or null.")
    billed_characters = sidecar.get("billed_characters")
    if billed_characters is not None:
      billed_characters = _require_int(
          billed_characters, f"{sidecar_path.name}.billed_characters"
      )
    verified_chunks.append(
        _LegacyChunk(
            index=index,
            text=chunk_text,
            text_sha256=chunk_sha,
            characters=chunk_characters,
            request_sha256=request_sha,
            audio_path=audio_path,
            audio_sha256=info.sha256,
            audio_bytes=info.bytes,
            media=info,
            provider_request_id=provider_request_id,
            billed_characters=billed_characters,
            sidecar_path=sidecar_path.resolve(),
        )
    )

  final_record = _require_object(
      generation_manifest.get("final_audio"), f"{generation_label}.final_audio"
  )
  expected_final_name = f"{output_stem}.mp3"
  final_path = _artifact_alias_path(
      final_record,
      ("audio_file", "file"),
      bases=(output / "audio", legacy_root, output, directory),
      legacy_root=legacy_root,
      output=output,
      label=f"{generation_label}.final_audio",
  )
  assert final_path is not None
  expected_final_path = (output / "audio" / expected_final_name).resolve()
  if final_path != expected_final_path or final_path.name != expected_final_name:
    raise _error(
        f"{generation_label} final audio must resolve to {expected_final_path}."
    )
  final_info = _verify_audio(
      final_path,
      final_record,
      tools=tools,
      output_format=core_generation["output_format"],
      label=f"{generation_label}.final_audio",
  )
  expected_duration = sum(chunk.media.duration_seconds for chunk in verified_chunks)
  allowed_duration = max(2.0, expected_duration * 0.02)
  if abs(final_info.duration_seconds - expected_duration) > allowed_duration:
    raise _error(
        f"{generation_label} final duration differs from chunk sum by more than "
        f"{allowed_duration:.2f}s."
    )

  return _LegacyTrack(
      track_number=track_number,
      chapter_number=chapter_number,
      title=title,
      output_stem=output_stem,
      source_relative_path=source_relative_path,
      source_href=source_href,
      source_member=None,
      source_sha256=source_sha,
      text=text,
      text_sha256=text_sha,
      characters=characters,
      chunks=tuple(verified_chunks),
      final_path=final_path,
      final_sha256=final_info.sha256,
      final_bytes=final_info.bytes,
      final_media=final_info,
      plan_sha256=plan_sha,
      generation_fingerprint=generation_fingerprint,
      generation=dict(raw_generation),
      sdk_version=sdk_version,
      plan_path=plan_path.resolve(),
      generation_path=generation_path.resolve(),
  )


def _verify_flat_archives(output: Path, tracks: tuple[_LegacyTrack, ...]) -> tuple[Path, ...]:
  archives = tuple(
      sorted(
          path.resolve()
          for path in output.iterdir()
          if path.is_file() and not path.is_symlink() and path.suffix.casefold() == ".zip"
      )
  )
  expected_names = [track.final_path.name for track in tracks]
  expected = {track.final_path.name: (track.final_sha256, track.final_bytes) for track in tracks}
  for archive_path in archives:
    try:
      with zipfile.ZipFile(archive_path, "r") as archive:
        members = archive.infolist()
        if [member.filename for member in members] != expected_names:
          raise _error(
              f"legacy flat ZIP {archive_path.name} does not contain exact ordered finals."
          )
        for member in members:
          expected_hash, expected_bytes = expected[member.filename]
          if member.is_dir() or member.file_size != expected_bytes:
            raise _error(
                f"legacy flat ZIP member {member.filename} has an invalid declared size."
            )
          if member.compress_size <= 0 or (
              expected_bytes
              > member.compress_size * _MAX_ZIP_COMPRESSION_RATIO + 1024 * 1024
          ):
            raise _error(
                f"legacy flat ZIP member {member.filename} has an unsafe compression ratio."
            )
          digest = hashlib.sha256()
          count = 0
          with archive.open(member, "r") as source:
            while True:
              block = source.read(min(1024 * 1024, expected_bytes - count + 1))
              if not block:
                break
              count += len(block)
              if count > expected_bytes:
                raise _error(
                    f"legacy flat ZIP member {member.filename} exceeds its verified final."
                )
              digest.update(block)
          if digest.hexdigest() != expected_hash or count != expected_bytes:
            raise _error(
                f"legacy flat ZIP member {member.filename} is not byte-identical to its final."
            )
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
      raise _error(f"legacy flat ZIP is unreadable: {archive_path}: {exc}") from exc
  return archives


def _verify_source_epub(path: Path) -> _SourcePublication:
  supplied = path.expanduser()
  _assert_no_symlink_components(supplied, "SOURCE_EPUB")
  try:
    source = supplied.resolve(strict=True)
  except OSError as exc:
    raise _error(f"SOURCE_EPUB does not exist: {supplied}.") from exc
  if not source.is_file():
    raise _error(f"SOURCE_EPUB is not a file: {source}.")
  with EpubContainer(source) as container:
    opf_path = _rootfile(container)
    opf_root = container.read_xml(opf_path)
    manifest = _manifest(container, opf_path, opf_root)
    _spine(opf_root, manifest)
    metadata = _metadata(opf_root, AppConfig())
    cover_bytes, cover_media_type, cover_extension = _cover(
        container, opf_root, manifest
    )
  return _SourcePublication(
      source_path=source,
      source_sha256=sha256_file(source),
      package_path=opf_path,
      metadata=metadata,
      cover_bytes=cover_bytes,
      cover_media_type=cover_media_type,
      cover_extension=cover_extension,
  )


def _source_member(
    container: EpubContainer,
    source: _SourcePublication,
    track: _LegacyTrack,
) -> str:
  names = set(container.names())
  relative = track.source_relative_path
  candidates: set[str] = set()

  if track.source_href is not None:
    parsed = urlsplit(track.source_href)
    if parsed.scheme or parsed.netloc or parsed.query:
      raise _error(
          f"{track.plan_path.name}.source_href must identify a local EPUB member."
      )
    decoded_path = unquote(parsed.path)
    if not decoded_path:
      raise _error(f"{track.plan_path.name}.source_href has no member path.")
    try:
      if decoded_path in names:
        candidates.add(decoded_path)
      resolved_member, _fragment = container.resolve_href(
          source.package_path, track.source_href
      )
      if resolved_member in names:
        candidates.add(resolved_member)
    except EpubError as exc:
      raise _error(
          f"{track.plan_path.name}.source_href is unsafe or invalid: {exc}"
      ) from exc
    if len(candidates) != 1:
      raise _error(
          f"{track.plan_path.name}.source_href does not resolve uniquely in SOURCE_EPUB."
      )
  else:
    candidates = {
        name for name in names if relative == name or relative.endswith(f"/{name}")
    }
    if len(candidates) != 1:
      raise _error(
          f"{track.plan_path.name}.source_file does not correspond uniquely to "
          "a SOURCE_EPUB member."
      )

  member = next(iter(candidates))
  if relative != member and not relative.endswith(f"/{member}"):
    raise _error(
        f"{track.plan_path.name}.source_file does not preserve member path {member!r}."
    )
  member_bytes = container.read_bytes(member)
  if sha256_bytes(member_bytes) != track.source_sha256:
    raise _error(
        f"SOURCE_EPUB member {member!r} does not match the verified unpacked "
        f"source for track {track.track_number}."
    )
  return member


def _check_source_correspondence(
    legacy_root: Path,
    source: _SourcePublication,
    tracks: tuple[_LegacyTrack, ...],
) -> dict[int, str]:
  for track in tracks:
    plan = load_json(track.plan_path)
    for field in ("source_epub_sha256", "epub_sha256", "source_archive_sha256"):
      if field in plan and _require_sha256(
          plan[field], f"{track.plan_path.name}.{field}"
      ) != source.source_sha256:
        raise _error(f"SOURCE_EPUB does not match {field} in {track.plan_path}.")

  root_publications = [
      path.resolve()
      for path in legacy_root.iterdir()
      if path.is_file()
      and not path.is_symlink()
      and path.suffix.casefold() in {".epub", ".zip"}
  ]
  if root_publications and source.source_sha256 not in {
      sha256_file(path) for path in root_publications
  }:
    raise _error("SOURCE_EPUB does not match any EPUB/ZIP archived in LEGACY_ROOT.")

  with EpubContainer(source.source_path) as container:
    return {
        track.track_number: _source_member(container, source, track)
        for track in tracks
    }


def _legacy_inventory(
    legacy_root: Path,
    output: Path,
    *,
    tools: MediaTools,
    source: _SourcePublication,
) -> _LegacyInventory:
  files = _scan_tree(output)
  quarantine_files = _inventory_attempts(output, files)
  tracks = tuple(
      _verify_legacy_track(
          directory,
          legacy_root=legacy_root,
          output=output,
          tools=tools,
      )
      for directory in _track_directories(output)
  )
  tracks = tuple(sorted(tracks, key=lambda track: track.track_number))
  numbers = [track.track_number for track in tracks]
  if numbers != list(range(1, len(tracks) + 1)):
    raise _error("legacy track numbers must be unique, ordered, and contiguous from 1.")
  stable_configs = [
      {
          **_generation_core(track.generation, f"track {track.track_number} generation"),
      }
      for track in tracks
  ]
  if any(config != stable_configs[0] for config in stable_configs[1:]):
    raise _error("legacy tracks do not share one voice/model/output/context configuration.")
  source_members = _check_source_correspondence(legacy_root, source, tracks)
  tracks = tuple(
      replace(track, source_member=source_members[track.track_number])
      for track in tracks
  )
  archives = _verify_flat_archives(output, tracks)
  return _LegacyInventory(
      tracks=tracks,
      generation=stable_configs[0],
      quarantine_files=quarantine_files,
      flat_archives=archives,
  )


def _copy_file_verified(source: Path, destination: Path, expected_sha256: str) -> int:
  destination.parent.mkdir(parents=True, exist_ok=True)
  source_digest = hashlib.sha256()
  bytes_written = 0
  try:
    with source.open("rb") as input_stream, destination.open("xb") as output_stream:
      for block in iter(lambda: input_stream.read(1024 * 1024), b""):
        source_digest.update(block)
        output_stream.write(block)
        bytes_written += len(block)
      output_stream.flush()
      os.fsync(output_stream.fileno())
  except OSError as exc:
    raise _error(f"could not copy {source} to staging: {exc}") from exc
  if source_digest.hexdigest() != expected_sha256:
    raise _error(f"legacy source changed while it was being copied: {source}.")
  if sha256_file(destination) != expected_sha256 or destination.stat().st_size != bytes_written:
    raise _error(f"staged copy verification failed: {destination}.")
  fsync_directory(destination.parent)
  return bytes_written


def _write_bytes_verified(destination: Path, value: bytes) -> None:
  destination.parent.mkdir(parents=True, exist_ok=True)
  expected = sha256_bytes(value)
  try:
    with destination.open("xb") as stream:
      stream.write(value)
      stream.flush()
      os.fsync(stream.fileno())
  except OSError as exc:
    raise _error(f"could not write staged artifact {destination}: {exc}") from exc
  if sha256_file(destination) != expected:
    raise _error(f"staged artifact verification failed: {destination}.")
  fsync_directory(destination.parent)


def _fsync_staged_tree(stage: Path) -> None:
  """Persist every staged directory entry bottom-up before root publication."""
  for raw_directory, directory_names, file_names in os.walk(stage, topdown=False):
    directory = Path(raw_directory)
    for name in (*directory_names, *file_names):
      if (directory / name).is_symlink():
        raise _error(f"staging unexpectedly contains a symbolic link: {directory / name}.")
    fsync_directory(directory)


def _artifact_inventory_sha256(source: _SourcePublication, inventory: _LegacyInventory) -> str:
  record = {
      "source_sha256": source.source_sha256,
      "source_members": [
          {
              "track": track.track_number,
              "member": track.source_member,
              "sha256": track.source_sha256,
          }
          for track in inventory.tracks
      ],
      "plans": [track.plan_sha256 for track in inventory.tracks],
      "generation_fingerprints": [
          track.generation_fingerprint for track in inventory.tracks
      ],
      "chunks": [
          {
              "track": track.track_number,
              "index": chunk.index,
              "text_sha256": chunk.text_sha256,
              "request_sha256": chunk.request_sha256,
              "audio_sha256": chunk.audio_sha256,
              "bytes": chunk.audio_bytes,
          }
          for track in inventory.tracks
          for chunk in track.chunks
      ],
      "finals": [
          {
              "track": track.track_number,
              "audio_sha256": track.final_sha256,
              "bytes": track.final_bytes,
          }
          for track in inventory.tracks
      ],
  }
  return sha256_text(canonical_json(record))


def _adoption_provenance(
    source: _SourcePublication,
    inventory: _LegacyInventory,
) -> dict[str, Any]:
  chunks = sum(len(track.chunks) for track in inventory.tracks)
  audio_bytes = sum(
      track.final_bytes + sum(chunk.audio_bytes for chunk in track.chunks)
      for track in inventory.tracks
  )
  core = {
      "format": LEGACY_FORMAT,
      "importer_version": IMPORTER_VERSION,
      "verification_only": True,
      "canonicalization": "strict-utf8-with-zero-or-one-trailing-lf",
      "source_sha256": source.source_sha256,
      "legacy_plan_sha256": [track.plan_sha256 for track in inventory.tracks],
      "legacy_generation_fingerprints": [
          track.generation_fingerprint for track in inventory.tracks
      ],
      "legacy_sdk_versions": sorted(
          {track.sdk_version or "unknown" for track in inventory.tracks}
      ),
      "source_members": [
          {
              "track": track.track_number,
              "member": track.source_member,
              "sha256": track.source_sha256,
          }
          for track in inventory.tracks
      ],
      "artifact_inventory_sha256": _artifact_inventory_sha256(source, inventory),
      "verified": {
          "tracks": len(inventory.tracks),
          "chunks": chunks,
          "audio_files": chunks + len(inventory.tracks),
          "audio_bytes": audio_bytes,
          "flat_archives": len(inventory.flat_archives),
          "quarantine_evidence_files": len(inventory.quarantine_files),
      },
  }
  return {**core, "adoption_sha256": sha256_text(canonical_json(core))}


def _plan_draft(
    source: _SourcePublication,
    inventory: _LegacyInventory,
    adoption: Mapping[str, Any],
) -> dict[str, Any]:
  sections: list[dict[str, Any]] = []
  for track in inventory.tracks:
    sections.append(
        {
            "track_number": track.track_number,
            "chapter_number": track.chapter_number,
            "title": track.title,
            "output_stem": track.output_stem,
            "source_href": track.source_member,
            "source_fragment": None,
            "source_sha256": track.source_sha256,
            "text_sha256": track.text_sha256,
            "characters": track.characters,
            "text_file": f"text/{track.output_stem}.txt",
            "chunk_count": len(track.chunks),
            "chunks": [
                {
                    "index": chunk.index,
                    "characters": chunk.characters,
                    "text_sha256": chunk.text_sha256,
                    "text_file": (
                        f"chunks/{track.output_stem}/chunk_{chunk.index:04d}.txt"
                    ),
                }
                for chunk in track.chunks
            ],
        }
    )
  cover = None
  if source.cover_bytes is not None:
    cover = {
        "file": f"cover{source.cover_extension or '.img'}",
        "media_type": source.cover_media_type,
        "sha256": sha256_bytes(source.cover_bytes),
        "bytes": len(source.cover_bytes),
    }
  max_characters = max(chunk.characters for track in inventory.tracks for chunk in track.chunks)
  return {
      "manifest_version": MANIFEST_VERSION,
      "kind": "ebook-tts-plan",
      "versions": {
          "extraction": 1,
          "normalization": 1,
          "chunker": LEGACY_CHUNKER_VERSION,
      },
      "source": {
          "filename": source.source_path.name,
          "sha256": source.source_sha256,
      },
      "book": asdict(source.metadata),
      "cover": cover,
      "warnings": [
          "Adopted from verified legacy-v1 artifacts; this plan is verification-only."
      ],
      "configuration": {
          "book": {"title": None, "authors": [], "language": None},
          "sections": {
              "include": [],
              "exclude": [],
              "announce_titles": True,
              "minimum_characters": 1,
              "title_overrides": {},
          },
          "normalization": [],
          "planning": {
              "model_id": inventory.generation["model_id"],
              "max_characters": max_characters,
          },
      },
      "track_count": len(inventory.tracks),
      "characters": sum(track.characters for track in inventory.tracks),
      "chunk_count": sum(len(track.chunks) for track in inventory.tracks),
      "sections": sections,
      "adopted": True,
      "verification_only": True,
      "adoption": dict(adoption),
  }


def _run_id(plan_id: str, generation: Mapping[str, Any]) -> str:
  stable = dict(generation)
  stable.pop("provider_sdk_version", None)
  return sha256_text(
      canonical_json({"plan_sha256": plan_id, "generation": stable})
  )


def _copy_private_provenance(
    stage: Path,
    output: Path,
    inventory: _LegacyInventory,
) -> list[dict[str, Any]]:
  copied: list[dict[str, Any]] = []
  provenance_root = stage / "provenance" / LEGACY_FORMAT
  known: list[tuple[Path, Path]] = []
  for track in inventory.tracks:
    track_root = provenance_root / "tracks" / track.output_stem
    known.extend(
        [
            (track.plan_path, track_root / "chunk_plan.json"),
            (track.generation_path, track_root / "generation_manifest.json"),
        ]
    )
    known.extend(
        (chunk.sidecar_path, track_root / chunk.sidecar_path.name)
        for chunk in track.chunks
    )
  for source, destination in known:
    digest = sha256_file(source)
    size = source.stat().st_size
    if size > _MAX_PROVENANCE_FILE_BYTES:
      continue
    _copy_file_verified(source, destination, digest)
    copied.append(
        {
            "file": destination.relative_to(stage).as_posix(),
            "sha256": digest,
            "bytes": size,
        }
    )
  for source in inventory.quarantine_files:
    size = source.stat().st_size
    if size > _MAX_PROVENANCE_FILE_BYTES:
      continue
    relative = source.relative_to(output)
    destination = provenance_root / "quarantine" / relative
    digest = sha256_file(source)
    _copy_file_verified(source, destination, digest)
    copied.append(
        {
            "file": destination.relative_to(stage).as_posix(),
            "sha256": digest,
            "bytes": size,
        }
    )
  return sorted(copied, key=lambda item: item["file"])


def _stage_workspace(
    stage: Path,
    *,
    source: _SourcePublication,
    inventory: _LegacyInventory,
    output: Path,
    adoption: Mapping[str, Any],
) -> tuple[str, str, int]:
  created_at = utc_now()
  atomic_write_json(
      stage / ".ebook-tts-workspace.json",
      {
          "manifest_version": MANIFEST_VERSION,
          "kind": "ebook-tts-workspace",
          "created_at": created_at,
          "adopted": True,
      },
  )
  draft = _plan_draft(source, inventory, adoption)
  plan_id = sha256_text(canonical_json(draft))
  plan_root = stage / "plans" / plan_id
  for track in inventory.tracks:
    atomic_write_text(plan_root / "text" / f"{track.output_stem}.txt", track.text)
    for chunk in track.chunks:
      atomic_write_text(
          plan_root
          / "chunks"
          / track.output_stem
          / f"chunk_{chunk.index:04d}.txt",
          chunk.text + "\n",
      )
  if source.cover_bytes is not None and isinstance(draft.get("cover"), dict):
    _write_bytes_verified(plan_root / str(draft["cover"]["file"]), source.cover_bytes)
  plan_manifest = {
      **draft,
      "plan_sha256": plan_id,
      "created_at": created_at,
  }
  atomic_write_json(plan_root / "plan.json", plan_manifest)
  atomic_write_json(
      stage / "workspace.json",
      {
          "manifest_version": MANIFEST_VERSION,
          "kind": "ebook-tts-workspace-registry",
          "plans": [plan_id],
          "current_plan": plan_id,
          "updated_at": created_at,
      },
  )

  sdk_versions = sorted(
      {track.sdk_version or "unknown" for track in inventory.tracks}
  )
  generation = {
      "provider": "elevenlabs",
      "provider_sdk_version": (
          sdk_versions[0] if len(sdk_versions) == 1 else "legacy-v1-mixed"
      ),
      "voice_id": inventory.generation["voice_id"],
      "model_id": inventory.generation["model_id"],
      "output_format": inventory.generation["output_format"],
      "context_characters": inventory.generation["context_characters"],
      "voice_settings": {},
  }
  run_id = _run_id(plan_id, generation)
  run_root = stage / "runs" / run_id
  states: dict[str, Any] = {}
  copied_audio_bytes = 0
  for track in inventory.tracks:
    chunk_states: list[dict[str, Any]] = []
    chunk_root = run_root / "chunks" / track.output_stem
    for chunk in track.chunks:
      destination = (
          chunk_root
          / f"chunk_{chunk.index:04d}_{chunk.request_sha256}.mp3"
      )
      copied_audio_bytes += _copy_file_verified(
          chunk.audio_path, destination, chunk.audio_sha256
      )
      state = {
          "manifest_version": MANIFEST_VERSION,
          "kind": "ebook-tts-chunk",
          "request_sha256": chunk.request_sha256,
          "text_sha256": chunk.text_sha256,
          "audio_file": destination.name,
          "audio_sha256": chunk.audio_sha256,
          "provider_request_id": chunk.provider_request_id,
          "billed_characters": chunk.billed_characters,
          "media": media_record(chunk.media),
          "adopted": True,
      }
      atomic_write_json(destination.with_suffix(destination.suffix + ".json"), state)
      chunk_states.append(state)
    final_destination = run_root / "audio" / track.final_path.name
    copied_audio_bytes += _copy_file_verified(
        track.final_path, final_destination, track.final_sha256
    )
    states[str(track.track_number)] = {
        "track_number": track.track_number,
        "title": track.title,
        "output_stem": track.output_stem,
        "status": "complete",
        "chunks": chunk_states,
        "completed_chunks": len(chunk_states),
        "final_audio": {
            "file": final_destination.name,
            **media_record(track.final_media),
        },
        "adopted": True,
    }
  run_manifest = {
      "manifest_version": MANIFEST_VERSION,
      "kind": "ebook-tts-generation",
      "run_id": run_id,
      "plan_sha256": plan_id,
      "status": "complete",
      "created_at": created_at,
      "updated_at": created_at,
      "generation": generation,
      "track_count": len(inventory.tracks),
      "completed_tracks": len(inventory.tracks),
      "sections": states,
      "adopted": True,
      "verification_only": True,
      "adoption": dict(adoption),
  }
  atomic_write_json(run_root / "run.json", run_manifest)

  copied_provenance = _copy_private_provenance(stage, output, inventory)
  adoption_record = {
      "manifest_version": MANIFEST_VERSION,
      "kind": "ebook-tts-adoption",
      **dict(adoption),
      "plan_sha256": plan_id,
      "run_id": run_id,
      "source": {
          "filename": source.source_path.name,
          "sha256": source.source_sha256,
      },
      "generation": generation,
      "private_provenance": copied_provenance,
      "created_at": created_at,
  }
  atomic_write_json(stage / "adoption.json", adoption_record)

  staged_plan = load_plan(stage)
  staged_run = load_run(staged_plan, run_id)
  if staged_run.manifest.get("status") != "complete":
    raise _error("staged current generation run is not complete.")
  return plan_id, run_id, copied_audio_bytes


def _resolve_legacy_roots(
    legacy_root: Path,
    output_directory: Path | None,
) -> tuple[Path, Path]:
  supplied_root = legacy_root.expanduser()
  _assert_no_symlink_components(supplied_root, "LEGACY_ROOT")
  try:
    root = supplied_root.resolve(strict=True)
  except OSError as exc:
    raise _error(f"LEGACY_ROOT does not exist: {supplied_root}.") from exc
  if not root.is_dir():
    raise _error(f"LEGACY_ROOT is not a directory: {root}.")
  raw_output = output_directory.expanduser() if output_directory else supplied_root / "chapters_output"
  _assert_no_symlink_components(raw_output, "legacy output")
  try:
    output = raw_output.resolve(strict=True)
  except OSError as exc:
    raise _error(f"legacy output does not exist: {raw_output}.") from exc
  if not output.is_dir() or not _is_within(output, root):
    raise _error(f"legacy output must be a directory contained in LEGACY_ROOT: {output}.")
  return root, output


def _destination(
    workspace: Path,
    *,
    legacy_root: Path,
    output: Path,
) -> tuple[Path, bool]:
  supplied = workspace.expanduser()
  if supplied.name in {"", ".", ".."}:
    raise _error(f"NEW_WORKSPACE has an unsafe final component: {supplied}.")
  _assert_no_symlink_components(supplied.parent, "NEW_WORKSPACE parent")
  try:
    parent = supplied.parent.resolve(strict=True)
  except OSError as exc:
    raise _error(f"NEW_WORKSPACE parent does not exist: {supplied.parent}.") from exc
  destination = parent / supplied.name
  if _is_within(destination, output):
    raise _error("NEW_WORKSPACE must not be inside the legacy output directory.")
  if _is_within(destination, legacy_root):
    raise _error("NEW_WORKSPACE must not be inside LEGACY_ROOT; legacy input is read-only.")
  if destination.is_symlink():
    raise _error(f"NEW_WORKSPACE must not be a symbolic link: {destination}.")
  if not destination.exists():
    return destination, False
  if not destination.is_dir():
    raise _error(f"NEW_WORKSPACE already exists and is not an empty directory: {destination}.")
  try:
    if any(destination.iterdir()):
      raise _error(f"NEW_WORKSPACE must be absent or empty: {destination}.")
  except OSError as exc:
    raise _error(f"NEW_WORKSPACE cannot be inspected safely: {destination}.") from exc
  return destination, True


def _publish(stage: Path, destination: Path, destination_was_empty: bool) -> None:
  removed_empty = False
  if destination_was_empty:
    if not destination.is_dir() or destination.is_symlink() or any(destination.iterdir()):
      raise _error("NEW_WORKSPACE changed before atomic publication.")
    destination.rmdir()
    removed_empty = True
  elif destination.exists():
    raise _error("NEW_WORKSPACE appeared before atomic publication.")

  try:
    os.replace(stage, destination)
  except Exception:
    if removed_empty and not destination.exists():
      destination.mkdir()
      fsync_directory(destination.parent)
    raise

  try:
    fsync_directory(destination.parent)
  except Exception as sync_error:
    try:
      os.replace(destination, stage)
      if destination_was_empty:
        destination.mkdir()
      fsync_directory(destination.parent)
    except Exception as rollback_error:
      raise _error(
          "the workspace rename completed, but parent-directory fsync and rollback "
          f"failed; inspect {destination} and {stage} before retrying: {rollback_error}"
      ) from sync_error
    raise _error(
        "parent-directory fsync failed after publication; the rename was rolled back "
        "without modifying legacy input."
    ) from sync_error


def adopt_legacy_v1(
    *,
    legacy_root: Path,
    source_epub: Path,
    workspace: Path,
    output_directory: Path | None = None,
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
) -> AdoptionResult:
  """Verify legacy-v1 artifacts offline and atomically publish a current workspace.

  The function never invokes planning, chunking, synthesis, assembly, or speech to
  text. It accepts only an absent/empty destination and never mutates legacy input.
  """
  root, output = _resolve_legacy_roots(legacy_root, output_directory)
  destination, destination_was_empty = _destination(
      workspace, legacy_root=root, output=output
  )
  source = _verify_source_epub(source_epub)
  tools = preflight(ffmpeg, ffprobe)
  inventory = _legacy_inventory(
      root,
      output,
      tools=tools,
      source=source,
  )
  adoption = _adoption_provenance(source, inventory)

  stage = Path(
      tempfile.mkdtemp(
          prefix=f".{destination.name}.adoption-",
          dir=destination.parent,
      )
  )
  published = False
  try:
    plan_id, run_id, audio_bytes = _stage_workspace(
        stage,
        source=source,
        inventory=inventory,
        output=output,
        adoption=adoption,
    )
    _fsync_staged_tree(stage)
    _publish(stage, destination, destination_was_empty)
    published = True
    plan = load_plan(destination, plan_id)
    run = load_run(plan, run_id)
    return AdoptionResult(
        workspace=destination,
        plan=plan,
        run=run,
        adoption_path=destination / "adoption.json",
        tracks=len(inventory.tracks),
        chunks=sum(len(track.chunks) for track in inventory.tracks),
        audio_bytes=audio_bytes,
    )
  finally:
    if not published and stage.exists():
      shutil.rmtree(stage)


# Concise library alias for callers that already know the input format.
adopt_legacy = adopt_legacy_v1
