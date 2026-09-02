"""Billing-safe, content-addressed, resumable audiobook generation."""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from ..errors import AmbiguousRequestError, MediaError, ProviderError, WorkspaceError
from ..media.tools import (
    MediaTools,
    assemble_mp3_track,
    decode_audio,
    media_record,
    preflight,
    probe_audio,
)
from ..models import MANIFEST_VERSION, AppConfig, Plan
from ..providers.base import SynthesisRequest, TTSProvider
from ..utils import (
    atomic_write_json,
    canonical_json,
    fsync_directory,
    load_json,
    sha256_file,
    sha256_text,
    utc_now,
)
from .locking import WorkspaceLock
from .manifests import (
    read_planned_chunk_text,
    read_planned_section_text,
    verified_cover_path,
    verify_plan_artifacts,
)
from .request_identity import (
    effective_context_characters,
    expanded_request_record,
    request_fingerprint,
)


MAX_CHUNK_AUDIO_BYTES = 512 * 1024 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}")
_ATTEMPT_AUDIO = re.compile(r"chunk_\d{4}_([0-9a-f]{64})\.mp3")


@dataclass(frozen=True)
class GenerationRun:
  workspace: Path
  run_path: Path
  run_id: str
  manifest: dict[str, Any]


def is_adopted_plan(plan: Plan) -> bool:
  """Return whether a plan is verification-only legacy adoption output."""
  return bool(
      plan.manifest.get("adopted")
      or plan.manifest.get("verification_only")
      or plan.manifest.get("adoption")
  )


def require_native_generation_plan(plan: Plan) -> None:
  """Verify immutable artifacts and reject synthesis against adopted plans."""
  verify_plan_artifacts(plan)
  if is_adopted_plan(plan):
    raise WorkspaceError(
        "Adopted legacy-v1 plans are verification-only and cannot generate audio "
        "or voice samples. Create a fresh native plan from the source EPUB before "
        "regeneration."
    )


def _status_code(exc: BaseException) -> int | None:
  status = getattr(exc, "status_code", None)
  if isinstance(status, int):
    return status
  response = getattr(exc, "response", None)
  status = getattr(response, "status_code", None)
  return status if isinstance(status, int) else None


def _definitive_rejection(exc: BaseException) -> bool:
  return _status_code(exc) in {400, 401, 402, 403, 404, 405, 413, 415, 422, 429}


def _generation_record(config: AppConfig, provider: TTSProvider) -> dict[str, Any]:
  return {
      "provider": provider.name,
      "provider_sdk_version": getattr(provider, "version", "unknown"),
      "voice_id": config.tts.voice_id,
      "model_id": config.tts.model_id,
      "output_format": config.tts.output_format,
      "context_characters": config.tts.context_characters,
      "voice_settings": dict(config.tts.voice_settings),
  }


def _run_id(plan: Plan, generation: dict[str, Any]) -> str:
  stable = dict(generation)
  stable.pop("provider_sdk_version", None)
  return sha256_text(
      canonical_json({"plan_sha256": plan.plan_id, "generation": stable})
  )


def _new_run(plan: Plan, config: AppConfig, provider: TTSProvider) -> GenerationRun:
  planning = plan.manifest.get("configuration", {}).get("planning", {})
  if planning.get("model_id") != config.tts.model_id:
    raise WorkspaceError(
        "The selected plan was chunked for a different model. Run plan again "
        "instead of mixing model limits with existing text artifacts."
    )
  if not config.tts.voice_id:
    raise ProviderError(
        "A voice ID is required. Set tts.voice_id or ELEVENLABS_VOICE_ID."
    )
  generation = _generation_record(config, provider)
  run_id = _run_id(plan, generation)
  run_path = plan.workspace / "runs" / run_id / "run.json"
  if run_path.exists():
    manifest = load_json(run_path)
    if (
        manifest.get("kind") != "ebook-tts-generation"
        or manifest.get("run_id") != run_id
        or manifest.get("plan_sha256") != plan.plan_id
    ):
      raise WorkspaceError(f"Generation run identity mismatch: {run_path}")
    return GenerationRun(plan.workspace, run_path, run_id, manifest)

  manifest = {
      "manifest_version": MANIFEST_VERSION,
      "kind": "ebook-tts-generation",
      "run_id": run_id,
      "plan_sha256": plan.plan_id,
      "status": "in_progress",
      "created_at": utc_now(),
      "updated_at": utc_now(),
      "generation": generation,
      "track_count": plan.manifest["track_count"],
      "sections": {},
  }
  atomic_write_json(run_path, manifest)
  return GenerationRun(plan.workspace, run_path, run_id, manifest)


def _save_run(run: GenerationRun, manifest: dict[str, Any]) -> None:
  manifest["updated_at"] = utc_now()
  atomic_write_json(run.run_path, manifest)


def _request_for_chunk(
    *,
    section: dict[str, Any],
    chunk_position: int,
    plan: Plan,
    config: AppConfig,
) -> SynthesisRequest:
  chunk_records = section["chunks"]
  track_number = int(section["track_number"])
  current = read_planned_chunk_text(
      plan,
      chunk_records[chunk_position],
      label=f"Track {track_number}, chunk {chunk_position + 1}",
  )
  context_size = effective_context_characters(
      config.tts.model_id,
      config.tts.context_characters,
      legacy_v1=False,
  )
  previous_text = ""
  next_text = ""
  if context_size and chunk_position > 0:
    previous = read_planned_chunk_text(
        plan,
        chunk_records[chunk_position - 1],
        label=f"Track {track_number}, chunk {chunk_position}",
    )
    previous_text = previous[-context_size:]
  if context_size and chunk_position + 1 < len(chunk_records):
    following = read_planned_chunk_text(
        plan,
        chunk_records[chunk_position + 1],
        label=f"Track {track_number}, chunk {chunk_position + 2}",
    )
    next_text = following[:context_size]
  return SynthesisRequest(
      text=current,
      voice_id=config.tts.voice_id,
      model_id=config.tts.model_id,
      output_format=config.tts.output_format,
      previous_text=previous_text,
      next_text=next_text,
      settings=config.tts.voice_settings,
  )


def _request_record(request: SynthesisRequest) -> dict[str, Any]:
  return expanded_request_record(
      text=request.text,
      voice_id=request.voice_id,
      model_id=request.model_id,
      output_format=request.output_format,
      previous_text=request.previous_text,
      next_text=request.next_text,
      previous_request_ids=request.previous_request_ids,
      settings=request.settings,
  )


def _audio_extension(output_format: str) -> str:
  codec = output_format.split("_", 1)[0]
  if codec != "mp3":
    raise ProviderError("ebook-tts v1 generation currently supports MP3 output only.")
  return ".mp3"


def _verify_existing_chunk(
    *,
    audio_path: Path,
    sidecar_path: Path,
    request_hash: str,
    text_hash: str,
    tools: MediaTools,
    output_format: str,
) -> dict[str, Any] | None:
  if not audio_path.exists():
    if sidecar_path.exists():
      raise WorkspaceError(f"Chunk sidecar exists without audio: {sidecar_path}")
    return None
  info = probe_audio(audio_path, tools.ffprobe, expected_format=output_format)
  decode_audio(audio_path, tools.ffmpeg)
  if sidecar_path.exists():
    sidecar = load_json(sidecar_path)
    if sidecar.get("request_sha256") != request_hash:
      raise WorkspaceError(f"Request fingerprint mismatch for {audio_path}")
    if sidecar.get("text_sha256") != text_hash:
      raise WorkspaceError(f"Text fingerprint mismatch for {audio_path}")
    if sidecar.get("audio_sha256") != info.sha256:
      raise WorkspaceError(f"Audio hash mismatch for {audio_path}")
    return sidecar
  sidecar = {
      "manifest_version": MANIFEST_VERSION,
      "kind": "ebook-tts-chunk",
      "request_sha256": request_hash,
      "text_sha256": text_hash,
      "audio_file": audio_path.name,
      "audio_sha256": info.sha256,
      "provider_request_id": None,
      "billed_characters": None,
      "media": media_record(info),
      "recovered_after_interruption": True,
  }
  atomic_write_json(sidecar_path, sidecar)
  return sidecar


def _generate_chunk(
    *,
    provider: TTSProvider,
    request: SynthesisRequest,
    chunk_directory: Path,
    chunk_index: int,
    text_hash: str,
    tools: MediaTools,
) -> dict[str, Any]:
  request_record = _request_record(request)
  request_hash = request_fingerprint(request_record)
  extension = _audio_extension(request.output_format)
  audio_path = chunk_directory / f"chunk_{chunk_index:04d}_{request_hash}{extension}"
  sidecar_path = audio_path.with_suffix(audio_path.suffix + ".json")
  attempt_path = audio_path.with_suffix(audio_path.suffix + ".attempt.json")
  partial_path = audio_path.with_name(f".{audio_path.name}.part")
  chunk_directory.mkdir(parents=True, exist_ok=True)

  existing = _verify_existing_chunk(
      audio_path=audio_path,
      sidecar_path=sidecar_path,
      request_hash=request_hash,
      text_hash=text_hash,
      tools=tools,
      output_format=request.output_format,
  )
  if existing is not None:
    if attempt_path.exists():
      attempt_path.unlink()
    return existing
  if attempt_path.exists() or partial_path.exists():
    evidence = ", ".join(
        path.name for path in (attempt_path, partial_path) if path.exists()
    )
    raise AmbiguousRequestError(
        f"A prior paid request has an ambiguous outcome ({evidence}). No request "
        "was reissued. Reconcile the attempt before retrying."
    )

  attempt = {
      "manifest_version": MANIFEST_VERSION,
      "kind": "ebook-tts-attempt",
      "status": "request_started",
      "request_sha256": request_hash,
      "text_sha256": text_hash,
      "audio_file": audio_path.name,
      "partial_file": partial_path.name,
      "started_at": utc_now(),
  }
  atomic_write_json(attempt_path, attempt)
  fsync_directory(chunk_directory)

  bytes_written = 0
  response = None
  try:
    response = provider.synthesize(request)
    attempt["provider_request_id"] = response.request_id
    attempt["billed_characters"] = response.billed_characters
    atomic_write_json(attempt_path, attempt)
    with partial_path.open("xb") as stream:
      for block in response.blocks:
        if not isinstance(block, (bytes, bytearray)):
          raise ProviderError("TTS provider returned a non-byte audio block.")
        bytes_written += len(block)
        if bytes_written > MAX_CHUNK_AUDIO_BYTES:
          raise ProviderError(
              f"TTS response exceeded the {MAX_CHUNK_AUDIO_BYTES:,}-byte safety limit."
          )
        stream.write(block)
      stream.flush()
      os.fsync(stream.fileno())
  except Exception as exc:
    has_bytes = partial_path.exists() and partial_path.stat().st_size > 0
    definitive = bytes_written == 0 and not has_bytes and _definitive_rejection(exc)
    if definitive:
      partial_path.unlink(missing_ok=True)
      attempt_path.unlink(missing_ok=True)
    status = _status_code(exc)
    suffix = f" (HTTP {status})" if status is not None else ""
    if definitive:
      raise ProviderError(
          f"Provider explicitly rejected request{suffix}; correct the problem and rerun: {exc}"
      ) from exc
    raise AmbiguousRequestError(
        f"Paid request failed with an ambiguous outcome{suffix}. Attempt evidence "
        f"was preserved and will block automatic retry: {exc}"
    ) from exc

  if bytes_written == 0:
    raise AmbiguousRequestError(
        "TTS provider returned an empty stream. Attempt evidence was preserved."
    )
  os.replace(partial_path, audio_path)
  fsync_directory(chunk_directory)
  try:
    info = probe_audio(audio_path, tools.ffprobe, expected_format=request.output_format)
    decode_audio(audio_path, tools.ffmpeg)
    sidecar = {
        "manifest_version": MANIFEST_VERSION,
        "kind": "ebook-tts-chunk",
        "request_sha256": request_hash,
        "text_sha256": text_hash,
        "audio_file": audio_path.name,
        "audio_sha256": info.sha256,
        "provider_request_id": response.request_id if response else None,
        "billed_characters": response.billed_characters if response else None,
        "media": media_record(info),
    }
    atomic_write_json(sidecar_path, sidecar)
    attempt_path.unlink()
    return sidecar
  except Exception as exc:
    raise AmbiguousRequestError(
        f"Paid audio was preserved at {audio_path}, but local validation or "
        f"checkpointing failed. No retry will be attempted: {exc}"
    ) from exc


def _chunk_path(chunk_directory: Path, sidecar: dict[str, Any]) -> Path:
  filename = sidecar.get("audio_file")
  if not isinstance(filename, str) or Path(filename).name != filename:
    raise WorkspaceError(f"Invalid chunk audio filename in sidecar: {filename!r}")
  return chunk_directory / filename


def _cover_path(plan: Plan) -> Path | None:
  return verified_cover_path(plan)


def generate(
    *,
    plan: Plan,
    config: AppConfig,
    provider: TTSProvider,
    selected_tracks: set[int] | None = None,
    tools: MediaTools | None = None,
) -> GenerationRun:
  """Generate selected tracks, resume exact checkpoints, and assemble final MP3s."""
  require_native_generation_plan(plan)
  media_tools = tools or preflight(config.audio.ffmpeg, config.audio.ffprobe)
  with WorkspaceLock(plan.workspace):
    verify_plan_artifacts(plan)
    run = _new_run(plan, config, provider)
    manifest = load_json(run.run_path)
    sections_state = manifest.setdefault("sections", {})
    plan_sections = plan.manifest.get("sections")
    if not isinstance(plan_sections, list):
      raise WorkspaceError("Plan sections are missing or invalid.")
    selected = selected_tracks or {
        int(section["track_number"]) for section in plan_sections
    }
    known = {int(section["track_number"]) for section in plan_sections}
    unknown = sorted(selected.difference(known))
    if unknown:
      raise WorkspaceError(f"Unknown track number(s): {unknown}")

    run_root = run.run_path.parent
    for section in plan_sections:
      track_number = int(section["track_number"])
      if track_number not in selected:
        continue
      key = str(track_number)
      state = sections_state.get(key, {})
      if state.get("status") == "complete":
        final = state.get("final_audio", {})
        final_name = final.get("file") if isinstance(final, dict) else None
        final_path = run_root / "audio" / str(final_name)
        if final_path.is_file() and sha256_file(final_path) == final.get("sha256"):
          probe_audio(final_path, media_tools.ffprobe, expected_format=config.tts.output_format)
          continue
        raise WorkspaceError(f"Completed final-track checkpoint is invalid: {final_path}")

      state = {
          "track_number": track_number,
          "title": section["title"],
          "output_stem": section["output_stem"],
          "status": "in_progress",
          "chunks": [],
      }
      sections_state[key] = state
      _save_run(run, manifest)
      chunk_directory = run_root / "chunks" / section["output_stem"]
      chunk_sidecars: list[dict[str, Any]] = []
      for position, chunk in enumerate(section["chunks"]):
        request = _request_for_chunk(
            section=section,
            chunk_position=position,
            plan=plan,
            config=config,
        )
        sidecar = _generate_chunk(
            provider=provider,
            request=request,
            chunk_directory=chunk_directory,
            chunk_index=int(chunk["index"]),
            text_hash=str(chunk["text_sha256"]),
            tools=media_tools,
        )
        chunk_sidecars.append(sidecar)
        state["chunks"] = chunk_sidecars
        state["completed_chunks"] = len(chunk_sidecars)
        _save_run(run, manifest)

      chunk_paths = [_chunk_path(chunk_directory, item) for item in chunk_sidecars]
      durations = [float(item["media"]["duration_seconds"]) for item in chunk_sidecars]
      output_path = run_root / "audio" / f"{section['output_stem']}.mp3"
      if output_path.exists() and not state.get("final_audio"):
        info = probe_audio(
            output_path,
            media_tools.ffprobe,
            expected_format=config.tts.output_format,
        )
        decode_audio(output_path, media_tools.ffmpeg)
      else:
        info = assemble_mp3_track(
            chunk_paths=chunk_paths,
            chunk_durations=durations,
            output_path=output_path,
            concat_path=run_root / "assembly" / f"{section['output_stem']}.ffconcat",
            tools=media_tools,
            output_format=config.tts.output_format,
            title=str(section["title"]),
            album=str(plan.manifest["book"]["title"]),
            artist=", ".join(plan.manifest["book"].get("authors", [])) or "Unknown Author",
            genre=config.audio.genre,
            track_number=track_number,
            track_count=int(plan.manifest["track_count"]),
            cover_path=_cover_path(plan),
        )
      state["status"] = "complete"
      state["final_audio"] = {
          "file": output_path.name,
          **media_record(info),
      }
      _save_run(run, manifest)

    complete_tracks = {
        int(key)
        for key, state in sections_state.items()
        if isinstance(state, dict) and state.get("status") == "complete"
    }
    manifest["status"] = "complete" if complete_tracks == known else "partial"
    manifest["completed_tracks"] = len(complete_tracks)
    _save_run(run, manifest)
    return GenerationRun(
        workspace=run.workspace,
        run_path=run.run_path,
        run_id=run.run_id,
        manifest=manifest,
    )


def load_run(plan: Plan, run_id: str | None = None) -> GenerationRun:
  """Load the selected run, or the only/latest run for this plan."""
  runs_root = plan.workspace / "runs"
  if run_id is not None:
    if _SHA256.fullmatch(run_id) is None:
      raise WorkspaceError(f"Invalid generation run ID: {run_id!r}.")
    path = runs_root / run_id / "run.json"
  else:
    candidates = sorted(
        (
            path
            for path in runs_root.glob("*/run.json")
            if path.is_file()
            and not path.is_symlink()
            and not path.parent.is_symlink()
            and _SHA256.fullmatch(path.parent.name) is not None
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    matching = [
        path
        for path in candidates
        if load_json(path).get("plan_sha256") == plan.plan_id
    ]
    if not matching:
      raise WorkspaceError(f"No generation run exists for plan {plan.plan_id[:12]}.")
    path = matching[0]
  if path.is_symlink() or path.parent.is_symlink() or not path.is_file():
    raise WorkspaceError(f"Generation run manifest is missing or unsafe: {path}")
  manifest = load_json(path)
  if (
      manifest.get("kind") != "ebook-tts-generation"
      or manifest.get("plan_sha256") != plan.plan_id
  ):
    raise WorkspaceError(f"Run does not belong to selected plan: {path}")
  actual_id = path.parent.name
  if manifest.get("run_id") != actual_id:
    raise WorkspaceError(f"Run directory and manifest ID disagree: {path}")
  return GenerationRun(plan.workspace, path, actual_id, manifest)


def ambiguous_attempts(run: GenerationRun) -> list[Path]:
  """Return active attempt markers, excluding retained quarantine evidence."""
  root = run.run_path.parent
  return sorted(
      path
      for path in root.rglob("*.attempt.json")
      if not any(
          part.casefold() == "quarantine"
          for part in path.relative_to(root).parts[:-1]
      )
  )


def _confined_attempt_path(plan: Plan, value: Path) -> tuple[Path, str]:
  """Resolve one lexical run-relative marker without following symbolic links."""
  if value.is_absolute() or ".." in value.parts:
    raise WorkspaceError(
        "Attempt path must be a relative path inside the selected workspace."
    )
  workspace = plan.workspace.resolve(strict=True)
  candidate = workspace.joinpath(*value.parts)
  try:
    relative = candidate.relative_to(workspace)
  except ValueError as exc:
    raise WorkspaceError("Attempt path escapes the selected workspace.") from exc
  if not relative.parts or relative.parts[0] != "runs":
    raise WorkspaceError("Attempt marker must be inside the workspace runs directory.")
  if len(relative.parts) != 5 or relative.parts[2] != "chunks":
    raise WorkspaceError(
        "Attempt marker must use runs/<run-id>/chunks/<section>/<marker>."
    )
  if any(part.casefold() == "quarantine" for part in relative.parts):
    raise WorkspaceError("Quarantined attempt evidence is not active.")
  run_id = relative.parts[1]
  if _SHA256.fullmatch(run_id) is None:
    raise WorkspaceError(f"Attempt path has an invalid run ID: {run_id!r}.")

  current = workspace
  for part in relative.parts:
    current = current / part
    if current.is_symlink():
      raise WorkspaceError(f"Attempt path traverses a symbolic link: {current}")
  try:
    resolved = candidate.resolve(strict=True)
  except OSError as exc:
    raise WorkspaceError(f"Attempt marker does not exist: {candidate}") from exc
  if resolved != candidate or not resolved.is_file():
    raise WorkspaceError(f"Attempt marker is not a regular confined file: {candidate}")
  return resolved, run_id


def _validate_active_attempt(
    *, plan: Plan, run: GenerationRun, attempt_path: Path
) -> tuple[dict[str, Any], Path | None]:
  """Validate an active generation marker and its optional partial payload."""
  if attempt_path not in ambiguous_attempts(run):
    raise WorkspaceError(f"Attempt marker is not active for run {run.run_id}: {attempt_path}")
  section_stem = attempt_path.parent.name
  valid_stems = {
      str(section.get("output_stem"))
      for section in plan.manifest.get("sections", [])
      if isinstance(section, dict)
  }
  if section_stem not in valid_stems:
    raise WorkspaceError(
        f"Attempt marker uses a section not present in the selected plan: {section_stem!r}."
    )

  attempt = load_json(attempt_path)
  if (
      attempt.get("manifest_version") != MANIFEST_VERSION
      or attempt.get("kind") != "ebook-tts-attempt"
      or attempt.get("status") != "request_started"
  ):
    raise WorkspaceError(f"Attempt marker is not an active generation attempt: {attempt_path}")
  audio_name = attempt.get("audio_file")
  if not isinstance(audio_name, str) or Path(audio_name).name != audio_name:
    raise WorkspaceError(f"Attempt marker has an unsafe audio filename: {audio_name!r}")
  match = _ATTEMPT_AUDIO.fullmatch(audio_name)
  request_hash = attempt.get("request_sha256")
  text_hash = attempt.get("text_sha256")
  if (
      match is None
      or not isinstance(request_hash, str)
      or _SHA256.fullmatch(request_hash) is None
      or match.group(1) != request_hash
      or not isinstance(text_hash, str)
      or _SHA256.fullmatch(text_hash) is None
  ):
    raise WorkspaceError(f"Attempt marker has invalid request identity: {attempt_path}")
  if attempt_path.name != f"{audio_name}.attempt.json":
    raise WorkspaceError(f"Attempt marker filename does not match its payload: {attempt_path}")
  partial_name = attempt.get("partial_file")
  if partial_name != f".{audio_name}.part":
    raise WorkspaceError(f"Attempt marker has an unsafe partial filename: {partial_name!r}")

  audio_path = attempt_path.parent / audio_name
  sidecar_path = attempt_path.parent / f"{audio_name}.json"
  if audio_path.exists() or sidecar_path.exists():
    raise WorkspaceError(
        "Final chunk evidence already exists; rerun generation to recover and verify it "
        "instead of authorizing another paid request."
    )
  partial = attempt_path.parent / partial_name
  if partial.is_symlink():
    raise WorkspaceError(f"Partial audio must not be a symbolic link: {partial}")
  if partial.exists() and not partial.is_file():
    raise WorkspaceError(f"Partial audio is not a regular file: {partial}")
  return attempt, partial if partial.exists() else None


def authorize_retry(*, plan: Plan, attempt_path: Path, reason: str) -> Path:
  """Quarantine active attempt evidence under the workspace mutation lock."""
  authorization_reason = reason.strip()
  if not authorization_reason:
    raise WorkspaceError("Retry authorization requires a non-empty reason.")

  with WorkspaceLock(plan.workspace):
    marker, run_id = _confined_attempt_path(plan, attempt_path)
    run = load_run(plan, run_id)
    attempt, partial = _validate_active_attempt(
        plan=plan,
        run=run,
        attempt_path=marker,
    )

    quarantine_parent = marker.parent / "quarantine"
    if quarantine_parent.is_symlink():
      raise WorkspaceError(
          f"Attempt quarantine must not be a symbolic link: {quarantine_parent}"
      )
    if quarantine_parent.exists() and not quarantine_parent.is_dir():
      raise WorkspaceError(
          f"Attempt quarantine path is not a directory: {quarantine_parent}"
      )
    quarantine_parent.mkdir(exist_ok=True)
    quarantine = quarantine_parent / f"authorized-{time.time_ns()}"
    quarantine.mkdir()
    fsync_directory(quarantine_parent)

    authorization_path = quarantine / "authorization.json"
    intended = ([partial.name] if partial is not None else []) + [marker.name]
    prepared = {
        "manifest_version": MANIFEST_VERSION,
        "kind": "ebook-tts-retry-authorization",
        "status": "prepared",
        "prepared_at": utc_now(),
        "reason": authorization_reason,
        "run_id": run_id,
        "plan_sha256": plan.plan_id,
        "request_sha256": attempt["request_sha256"],
        "provider_request_id": attempt.get("provider_request_id"),
        "intended": intended,
        "moved": [],
    }
    moved: list[tuple[Path, Path]] = []
    try:
      atomic_write_json(authorization_path, prepared)
      for source in (partial, marker):
        if source is None:
          continue
        destination = quarantine / source.name
        os.replace(source, destination)
        moved.append((source, destination))
        fsync_directory(source.parent)
        fsync_directory(quarantine)
      completed = dict(prepared)
      completed.update(
          {
              "status": "authorized",
              "authorized_at": utc_now(),
              "moved": [destination.name for _, destination in moved],
          }
      )
      atomic_write_json(authorization_path, completed)
      fsync_directory(quarantine)
      fsync_directory(quarantine_parent)
      return quarantine
    except BaseException as exc:
      rollback_errors: list[str] = []
      for source, destination in reversed(moved):
        try:
          if source.exists():
            raise OSError(f"rollback destination already exists: {source}")
          os.replace(destination, source)
        except OSError as rollback_exc:
          rollback_errors.append(str(rollback_exc))
      try:
        authorization_path.unlink(missing_ok=True)
        quarantine.rmdir()
        fsync_directory(marker.parent)
        fsync_directory(quarantine_parent)
      except OSError as rollback_exc:
        rollback_errors.append(str(rollback_exc))
      if rollback_errors:
        details = "; ".join(rollback_errors)
        raise WorkspaceError(
            "Retry authorization failed and evidence rollback was incomplete; "
            f"inspect {quarantine}: {details}"
        ) from exc
      if isinstance(exc, (KeyboardInterrupt, SystemExit, WorkspaceError)):
        raise
      raise WorkspaceError(
          f"Retry authorization failed; original evidence was restored: {exc}"
      ) from exc


@dataclass(frozen=True)
class VoiceSample:
  sample_id: str
  manifest_path: Path
  audio_path: Path
  approved: bool


def _sample_excerpt(text: str, maximum: int) -> str:
  if not 200 <= maximum <= 2_000:
    raise WorkspaceError("Sample length must be between 200 and 2,000 characters.")
  compact = " ".join(text.split())
  if len(compact) <= maximum:
    return compact
  window = compact[: maximum + 1]
  preferred = max(window.rfind(". "), window.rfind("? "), window.rfind("! "))
  if preferred >= int(maximum * 0.6):
    return window[: preferred + 1]
  whitespace = window.rfind(" ", 0, maximum + 1)
  return window[: whitespace if whitespace > 0 else maximum]


def generate_sample(
    *,
    plan: Plan,
    config: AppConfig,
    provider: TTSProvider,
    track_number: int | None = None,
    characters: int = 1_000,
    tools: MediaTools | None = None,
) -> VoiceSample:
  """Generate a short, checkpointed sample that can be explicitly approved."""
  require_native_generation_plan(plan)
  if not config.tts.voice_id:
    raise ProviderError(
        "A voice ID is required. Set tts.voice_id or ELEVENLABS_VOICE_ID."
    )
  raw_sections = plan.manifest.get("sections")
  if not isinstance(raw_sections, list) or not raw_sections:
    raise WorkspaceError("The plan has no sections available for a voice sample.")
  if track_number is None:
    section = max(raw_sections, key=lambda item: int(item.get("characters", 0)))
  else:
    section = next(
        (
            item
            for item in raw_sections
            if int(item.get("track_number", -1)) == track_number
        ),
        None,
    )
    if section is None:
      raise WorkspaceError(f"Unknown sample track number: {track_number}")
  source_text = read_planned_section_text(plan, section)
  excerpt = _sample_excerpt(source_text, characters)
  request = SynthesisRequest(
      text=excerpt,
      voice_id=config.tts.voice_id,
      model_id=config.tts.model_id,
      output_format=config.tts.output_format,
      settings=config.tts.voice_settings,
  )
  generation = _generation_record(config, provider)
  stable_generation = dict(generation)
  stable_generation.pop("provider_sdk_version", None)
  draft = {
      "plan_sha256": plan.plan_id,
      "track_number": int(section["track_number"]),
      "title": section["title"],
      "excerpt_sha256": sha256_text(excerpt),
      "characters": len(excerpt),
      "generation": stable_generation,
  }
  sample_id = sha256_text(canonical_json(draft))
  sample_directory = plan.workspace / "samples" / sample_id
  manifest_path = sample_directory / "sample.json"
  media_tools = tools or preflight(config.audio.ffmpeg, config.audio.ffprobe)
  with WorkspaceLock(plan.workspace):
    sidecar = _generate_chunk(
        provider=provider,
        request=request,
        chunk_directory=sample_directory,
        chunk_index=1,
        text_hash=sha256_text(excerpt),
        tools=media_tools,
    )
    audio_path = _chunk_path(sample_directory, sidecar)
    if manifest_path.exists():
      manifest = load_json(manifest_path)
      if manifest.get("sample_id") != sample_id:
        raise WorkspaceError(f"Voice sample identity mismatch: {manifest_path}")
    else:
      manifest = {
          "manifest_version": MANIFEST_VERSION,
          "kind": "ebook-tts-voice-sample",
          "sample_id": sample_id,
          **draft,
          "provider_sdk_version": generation["provider_sdk_version"],
          "status": "ready",
          "audio_file": audio_path.name,
          "audio_sha256": sidecar["audio_sha256"],
          "created_at": utc_now(),
      }
      atomic_write_json(manifest_path, manifest)
    return VoiceSample(
        sample_id=sample_id,
        manifest_path=manifest_path,
        audio_path=audio_path,
        approved=manifest.get("status") == "approved",
    )


def approve_sample(plan: Plan, sample_id: str) -> VoiceSample:
  """Record the user's explicit acceptance of one generated voice sample."""
  if len(sample_id) != 64 or any(character not in "0123456789abcdef" for character in sample_id):
    raise WorkspaceError("A sample ID must be a 64-character lowercase SHA-256 value.")
  manifest_path = plan.workspace / "samples" / sample_id / "sample.json"
  with WorkspaceLock(plan.workspace):
    manifest = load_json(manifest_path)
    if (
        manifest.get("kind") != "ebook-tts-voice-sample"
        or manifest.get("sample_id") != sample_id
        or manifest.get("plan_sha256") != plan.plan_id
    ):
      raise WorkspaceError(f"Voice sample does not belong to this plan: {manifest_path}")
    filename = manifest.get("audio_file")
    audio_path = _chunk_path(manifest_path.parent, {"audio_file": filename})
    if not audio_path.is_file() or sha256_file(audio_path) != manifest.get("audio_sha256"):
      raise WorkspaceError(f"Voice sample audio is missing or changed: {audio_path}")
    manifest["status"] = "approved"
    manifest["approved_at"] = utc_now()
    atomic_write_json(manifest_path, manifest)
    return VoiceSample(sample_id, manifest_path, audio_path, True)


def approved_sample(plan: Plan, config: AppConfig, provider_name: str) -> VoiceSample | None:
  """Return the newest approval matching the current plan and voice settings."""
  expected = {
      "provider": provider_name,
      "voice_id": config.tts.voice_id,
      "model_id": config.tts.model_id,
      "output_format": config.tts.output_format,
      "context_characters": config.tts.context_characters,
      "voice_settings": dict(config.tts.voice_settings),
  }
  candidates = sorted(
      plan.workspace.glob("samples/*/sample.json"),
      key=lambda path: path.stat().st_mtime,
      reverse=True,
  )
  for path in candidates:
    manifest = load_json(path)
    if (
        manifest.get("kind") == "ebook-tts-voice-sample"
        and manifest.get("plan_sha256") == plan.plan_id
        and manifest.get("status") == "approved"
        and manifest.get("generation") == expected
    ):
      audio_path = _chunk_path(
          path.parent, {"audio_file": manifest.get("audio_file")}
      )
      if audio_path.is_file() and sha256_file(audio_path) == manifest.get("audio_sha256"):
        return VoiceSample(str(manifest["sample_id"]), path, audio_path, True)
  return None
