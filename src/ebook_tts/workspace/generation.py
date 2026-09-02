"""Billing-safe, content-addressed, resumable audiobook generation."""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import AmbiguousRequestError, ProviderError, WorkspaceError
from ..media.tools import (
    MediaTools,
    assemble_mp3_track,
    decode_audio,
    media_record,
    preflight,
    probe_audio,
)
from ..models import MANIFEST_VERSION, AppConfig, MediaInfo, Plan
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
    request_fingerprint_candidates,
)


MAX_CHUNK_AUDIO_BYTES = 512 * 1024 * 1024
ASSEMBLY_CHECKPOINT_VERSION = 1
ASSEMBLY_POLICY_VERSION = 1
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


def _confined_workspace_path(workspace: Path, path: Path, label: str) -> Path:
  """Return one lexical workspace path after rejecting every symlink component."""
  root = workspace.resolve(strict=True)
  candidate = Path(os.path.abspath(path))
  try:
    relative = candidate.relative_to(root)
  except ValueError as exc:
    raise WorkspaceError(f"{label} escapes the selected workspace: {path}") from exc
  current = root
  for part in relative.parts:
    current = current / part
    if current.is_symlink():
      raise WorkspaceError(f"{label} traverses a symbolic link: {current}")
  return candidate


def _regular_workspace_file(workspace: Path, path: Path, label: str) -> Path:
  """Require one regular, non-symlinked file at an exact workspace location."""
  candidate = _confined_workspace_path(workspace, path, label)
  try:
    resolved = candidate.resolve(strict=True)
  except OSError as exc:
    raise WorkspaceError(f"{label} is missing: {candidate}") from exc
  if resolved != candidate or not candidate.is_file():
    raise WorkspaceError(f"{label} is not a regular confined file: {candidate}")
  return candidate


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
      "assembly": {
          "policy_version": ASSEMBLY_POLICY_VERSION,
          "checkpoint_version": ASSEMBLY_CHECKPOINT_VERSION,
          "id3v2_version": 3,
          "genre": config.audio.genre,
      },
  }


def _stable_generation(generation: dict[str, Any]) -> dict[str, Any]:
  """Return paid-request identity; local assembly policy is checkpointed separately."""
  stable = dict(generation)
  stable.pop("provider_sdk_version", None)
  stable.pop("assembly", None)
  return stable


def _run_id(plan: Plan, generation: dict[str, Any]) -> str:
  return sha256_text(
      canonical_json(
          {
              "plan_sha256": plan.plan_id,
              "generation": _stable_generation(generation),
          }
      )
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
  _confined_workspace_path(plan.workspace, run_path, "Generation run manifest")
  if run_path.exists():
    run = load_run(plan, run_id)
    persisted = run.manifest.get("generation")
    if (
        not isinstance(persisted, dict)
        or _stable_generation(persisted) != _stable_generation(generation)
    ):
      raise WorkspaceError(f"Generation run settings do not match its ID: {run_path}")
    expected_assembly = generation["assembly"]
    persisted_assembly = persisted.get("assembly")
    if persisted_assembly != expected_assembly:
      if persisted_assembly is not None:
        raise WorkspaceError(
            "Assembly settings changed for an existing paid-request run. Use the "
            "original audio.genre setting or a separate workspace; no request was sent."
        )
      states = run.manifest.get("sections")
      has_final_checkpoint = isinstance(states, dict) and any(
          isinstance(state, dict)
          and (state.get("status") == "complete" or "final_audio" in state)
          for state in states.values()
      )
      audio_root = _confined_workspace_path(
          plan.workspace,
          run_path.parent / "audio",
          "Generation final-audio directory",
      )
      has_final_file = audio_root.is_dir() and any(audio_root.glob("*.mp3"))
      if has_final_checkpoint or has_final_file:
        raise WorkspaceError(
            "This native run predates authenticated assembly checkpoints. Its paid "
            "chunks were preserved, but existing final tracks cannot be resumed "
            "automatically; no request was sent."
        )
      updated_manifest = dict(run.manifest)
      updated_generation = dict(persisted)
      updated_generation["assembly"] = expected_assembly
      updated_manifest["generation"] = updated_generation
      _save_run(run, updated_manifest)
      return GenerationRun(
          plan.workspace,
          run.run_path,
          run.run_id,
          updated_manifest,
      )
    return run

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


def _require_media_record(record: object, info: MediaInfo, label: str) -> None:
  if not isinstance(record, dict) or record != media_record(info):
    raise WorkspaceError(f"{label} media checkpoint does not match the audio file.")


def _verify_existing_chunk(
    *,
    audio_path: Path,
    sidecar_path: Path,
    request_hash: str,
    text_hash: str,
    tools: MediaTools,
    output_format: str,
) -> dict[str, Any] | None:
  if audio_path.is_symlink() or sidecar_path.is_symlink():
    raise WorkspaceError(f"Chunk checkpoint must not use symbolic links: {audio_path}")
  if not audio_path.exists():
    if sidecar_path.exists():
      raise WorkspaceError(f"Chunk sidecar exists without audio: {sidecar_path}")
    return None
  if not audio_path.is_file():
    raise WorkspaceError(f"Chunk audio is not a regular file: {audio_path}")
  info = probe_audio(audio_path, tools.ffprobe, expected_format=output_format)
  decode_audio(audio_path, tools.ffmpeg)
  if sidecar_path.exists():
    if not sidecar_path.is_file():
      raise WorkspaceError(f"Chunk sidecar is not a regular file: {sidecar_path}")
    sidecar = load_json(sidecar_path)
    if (
        sidecar.get("manifest_version") != MANIFEST_VERSION
        or sidecar.get("kind") != "ebook-tts-chunk"
        or sidecar.get("audio_file") != audio_path.name
        or sidecar.get("request_sha256") != request_hash
        or sidecar.get("text_sha256") != text_hash
        or sidecar.get("audio_sha256") != info.sha256
    ):
      raise WorkspaceError(f"Chunk checkpoint identity mismatch for {audio_path}")
    _require_media_record(sidecar.get("media"), info, f"Chunk {audio_path.name}")
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


def _native_assembly_settings(run: GenerationRun) -> tuple[str, str]:
  generation = run.manifest.get("generation")
  if not isinstance(generation, dict):
    raise WorkspaceError("Generation settings are missing from the run manifest.")
  output_format = generation.get("output_format")
  assembly = generation.get("assembly")
  if not isinstance(output_format, str) or not output_format:
    raise WorkspaceError("Generation output format is invalid.")
  if (
      not isinstance(assembly, dict)
      or assembly.get("policy_version") != ASSEMBLY_POLICY_VERSION
      or assembly.get("checkpoint_version") != ASSEMBLY_CHECKPOINT_VERSION
      or assembly.get("id3v2_version") != 3
      or not isinstance(assembly.get("genre"), str)
  ):
    raise WorkspaceError(
        "Native run lacks the current assembly identity. Its existing final tracks "
        "cannot be trusted as resumable checkpoints."
    )
  return output_format, str(assembly["genre"])


def _native_assembly_contract(
    *,
    plan: Plan,
    run: GenerationRun,
    section: dict[str, Any],
    state: dict[str, Any],
    tools: MediaTools,
) -> tuple[dict[str, Any], list[Path], list[float], Path | None]:
  """Recompute the exact native assembly inputs and semantic output contract."""
  output_format, genre = _native_assembly_settings(run)
  track_number = int(section["track_number"])
  output_stem = str(section["output_stem"])
  planned_chunks = section.get("chunks")
  generated_chunks = state.get("chunks")
  if not isinstance(planned_chunks, list) or not isinstance(generated_chunks, list):
    raise WorkspaceError(f"Track {track_number} has invalid chunk checkpoints.")
  if not planned_chunks or len(generated_chunks) != len(planned_chunks):
    raise WorkspaceError(
        f"Track {track_number} generated chunk count does not match its plan."
    )
  if state.get("completed_chunks") != len(generated_chunks):
    raise WorkspaceError(f"Track {track_number} completed chunk count is invalid.")
  generation = run.manifest.get("generation")
  if not isinstance(generation, dict):
    raise WorkspaceError("Generation settings are missing from the run manifest.")
  references = [
      read_planned_chunk_text(
          plan,
          planned,
          label=f"Track {track_number}, chunk {position}",
      )
      for position, planned in enumerate(planned_chunks, start=1)
      if isinstance(planned, dict)
  ]
  if len(references) != len(planned_chunks):
    raise WorkspaceError(f"Track {track_number} planned chunks are invalid.")

  run_root = _confined_workspace_path(
      plan.workspace,
      run.run_path.parent,
      "Generation run directory",
  )
  chunk_directory = _confined_workspace_path(
      plan.workspace,
      run_root / "chunks" / output_stem,
      f"Track {track_number} chunk directory",
  )
  inputs: list[dict[str, Any]] = []
  chunk_paths: list[Path] = []
  durations: list[float] = []
  profiles: set[tuple[str, int, int]] = set()
  for position, (planned, generated) in enumerate(
      zip(planned_chunks, generated_chunks), start=1
  ):
    if not isinstance(planned, dict) or not isinstance(generated, dict):
      raise WorkspaceError(
          f"Track {track_number}, chunk {position} checkpoint is invalid."
      )
    text_hash = planned.get("text_sha256")
    request_hash = generated.get("request_sha256")
    if (
        planned.get("index") != position
        or not isinstance(text_hash, str)
        or _SHA256.fullmatch(text_hash) is None
        or generated.get("text_sha256") != text_hash
        or not isinstance(request_hash, str)
        or _SHA256.fullmatch(request_hash) is None
    ):
      raise WorkspaceError(
          f"Track {track_number}, chunk {position} identity is invalid."
      )
    try:
      valid_requests = request_fingerprint_candidates(
          texts=references,
          position=position - 1,
          generation=generation,
          legacy_v1=False,
      )
    except ValueError as exc:
      raise WorkspaceError(
          f"Track {track_number}, chunk {position} request identity is invalid: {exc}"
      ) from exc
    if request_hash not in valid_requests:
      raise WorkspaceError(
          f"Track {track_number}, chunk {position} request fingerprint is invalid."
      )
    expected_name = f"chunk_{position:04d}_{request_hash}.mp3"
    if (
        generated.get("manifest_version") != MANIFEST_VERSION
        or generated.get("kind") != "ebook-tts-chunk"
        or generated.get("audio_file") != expected_name
    ):
      raise WorkspaceError(
          f"Track {track_number}, chunk {position} filename or schema is invalid."
      )
    audio_path = _regular_workspace_file(
        plan.workspace,
        chunk_directory / expected_name,
        f"Track {track_number}, chunk {position} audio",
    )
    sidecar_path = _regular_workspace_file(
        plan.workspace,
        audio_path.with_suffix(audio_path.suffix + ".json"),
        f"Track {track_number}, chunk {position} sidecar",
    )
    if load_json(sidecar_path) != generated:
      raise WorkspaceError(
          f"Track {track_number}, chunk {position} sidecar disagrees with the run."
      )
    info = probe_audio(audio_path, tools.ffprobe, expected_format=output_format)
    decode_audio(audio_path, tools.ffmpeg)
    if generated.get("audio_sha256") != info.sha256:
      raise WorkspaceError(
          f"Track {track_number}, chunk {position} audio hash is invalid."
      )
    _require_media_record(
        generated.get("media"),
        info,
        f"Track {track_number}, chunk {position}",
    )
    profiles.add((info.codec, info.sample_rate, info.channels))
    actual_media = media_record(info)
    inputs.append(
        {
            "index": position,
            "text_sha256": text_hash,
            "request_sha256": request_hash,
            "audio_file": expected_name,
            "audio_sha256": info.sha256,
            "media": actual_media,
        }
    )
    chunk_paths.append(audio_path)
    durations.append(info.duration_seconds)
  if len(profiles) != 1:
    raise WorkspaceError(
        f"Track {track_number} chunks do not share one concat-compatible profile."
    )

  book = plan.manifest.get("book")
  if not isinstance(book, dict):
    raise WorkspaceError("Plan book metadata is invalid.")
  artist = ", ".join(str(value) for value in book.get("authors", []))
  artist = artist or "Unknown Author"
  metadata = {
      "title": str(section["title"]),
      "album": str(book["title"]),
      "artist": artist,
      "album_artist": artist,
      "genre": genre,
      "track": f"{track_number}/{int(plan.manifest['track_count'])}",
      "id3v2_version": 3,
  }
  cover_path = _cover_path(plan)
  cover_manifest = plan.manifest.get("cover")
  if cover_path is None:
    cover_record = None
  else:
    if not isinstance(cover_manifest, dict):
      raise WorkspaceError("Verified cover path has no matching plan record.")
    cover_record = {
        "file": cover_path.name,
        "media_type": cover_manifest.get("media_type"),
        "sha256": cover_manifest.get("sha256"),
        "bytes": cover_manifest.get("bytes"),
    }
  contract = {
      "checkpoint_version": ASSEMBLY_CHECKPOINT_VERSION,
      "policy_version": ASSEMBLY_POLICY_VERSION,
      "plan_sha256": plan.plan_id,
      "run_id": run.run_id,
      "section_sha256": sha256_text(canonical_json(section)),
      "track_number": track_number,
      "output_file": f"{output_stem}.mp3",
      "output_format": output_format,
      "inputs": inputs,
      "metadata": metadata,
      "cover": cover_record,
  }
  return contract, chunk_paths, durations, cover_path


def verify_native_final_checkpoint(
    *,
    plan: Plan,
    run: GenerationRun,
    section: dict[str, Any],
    state: dict[str, Any],
    tools: MediaTools,
) -> tuple[MediaInfo, str]:
  """Strictly verify one completed native track and its assembly receipt."""
  if is_adopted_plan(plan):
    raise WorkspaceError("Adopted final tracks use legacy verification, not native receipts.")
  track_number = int(section["track_number"])
  if (
      state.get("status") != "complete"
      or state.get("track_number") != track_number
      or state.get("title") != section.get("title")
      or state.get("output_stem") != section.get("output_stem")
  ):
    raise WorkspaceError(
        f"Track {track_number} completed state does not match its immutable section."
    )
  contract, _, _, _ = _native_assembly_contract(
      plan=plan,
      run=run,
      section=section,
      state=state,
      tools=tools,
  )
  assembly_hash = sha256_text(canonical_json(contract))
  final = state.get("final_audio")
  expected_name = str(contract["output_file"])
  if (
      not isinstance(final, dict)
      or final.get("checkpoint_version") != ASSEMBLY_CHECKPOINT_VERSION
      or final.get("file") != expected_name
      or final.get("assembly") != contract
      or final.get("assembly_sha256") != assembly_hash
  ):
    raise WorkspaceError(
        f"Track {track_number} final checkpoint does not match its assembly inputs."
    )
  final_path = _regular_workspace_file(
      plan.workspace,
      run.run_path.parent / "audio" / expected_name,
      f"Track {track_number} final audio",
  )
  info = probe_audio(
      final_path,
      tools.ffprobe,
      expected_format=str(contract["output_format"]),
  )
  decode_audio(final_path, tools.ffmpeg)
  actual_media = media_record(info)
  if any(final.get(name) != value for name, value in actual_media.items()):
    raise WorkspaceError(
        f"Track {track_number} final media checkpoint does not match its audio."
    )
  return info, assembly_hash


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
    raw_states = manifest.get("sections")
    if raw_states is None:
      raw_states = {}
      manifest["sections"] = raw_states
    if not isinstance(raw_states, dict):
      raise WorkspaceError("Generation run sections are invalid.")
    sections_state: dict[str, Any] = raw_states
    plan_sections = plan.manifest.get("sections")
    if not isinstance(plan_sections, list):
      raise WorkspaceError("Plan sections are missing or invalid.")
    if manifest.get("track_count") != len(plan_sections):
      raise WorkspaceError("Generation run track count does not match its plan.")
    selected = (
        selected_tracks
        if selected_tracks is not None
        else {int(section["track_number"]) for section in plan_sections}
    )
    known = {int(section["track_number"]) for section in plan_sections}
    unknown = sorted(selected.difference(known))
    if unknown:
      raise WorkspaceError(f"Unknown track number(s): {unknown}")
    unexpected_states = sorted(set(sections_state).difference(str(value) for value in known))
    if unexpected_states:
      raise WorkspaceError(
          f"Generation run contains unknown section state(s): {unexpected_states}"
      )

    run_root = _confined_workspace_path(
        plan.workspace,
        run.run_path.parent,
        "Generation run directory",
    )
    current_run = GenerationRun(plan.workspace, run.run_path, run.run_id, manifest)
    verified_complete: set[int] = set()
    for section in plan_sections:
      track_number = int(section["track_number"])
      existing = sections_state.get(str(track_number))
      if isinstance(existing, dict) and existing.get("status") == "complete":
        verify_native_final_checkpoint(
            plan=plan,
            run=current_run,
            section=section,
            state=existing,
            tools=media_tools,
        )
        verified_complete.add(track_number)

    for section in plan_sections:
      track_number = int(section["track_number"])
      if track_number not in selected or track_number in verified_complete:
        continue
      key = str(track_number)
      state: dict[str, Any] = {
          "track_number": track_number,
          "title": section["title"],
          "output_stem": section["output_stem"],
          "status": "in_progress",
          "chunks": [],
          "completed_chunks": 0,
      }
      sections_state[key] = state
      _save_run(run, manifest)
      chunk_directory = _confined_workspace_path(
          plan.workspace,
          run_root / "chunks" / str(section["output_stem"]),
          f"Track {track_number} chunk directory",
      )
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

      current_run = GenerationRun(plan.workspace, run.run_path, run.run_id, manifest)
      contract, chunk_paths, durations, cover_path = _native_assembly_contract(
          plan=plan,
          run=current_run,
          section=section,
          state=state,
          tools=media_tools,
      )
      output_path = _confined_workspace_path(
          plan.workspace,
          run_root / "audio" / str(contract["output_file"]),
          f"Track {track_number} final audio",
      )
      if output_path.is_symlink() or output_path.exists():
        raise WorkspaceError(
            f"Uncheckpointed final audio already exists: {output_path}. Remove or "
            "quarantine it after inspection, then rerun; ebook-tts will not adopt it."
        )
      concat_path = _confined_workspace_path(
          plan.workspace,
          run_root / "assembly" / f"{section['output_stem']}.ffconcat",
          f"Track {track_number} concat manifest",
      )
      metadata = contract["metadata"]
      if not isinstance(metadata, dict):
        raise WorkspaceError(f"Track {track_number} assembly metadata is invalid.")
      info = assemble_mp3_track(
          chunk_paths=chunk_paths,
          chunk_durations=durations,
          output_path=output_path,
          concat_path=concat_path,
          tools=media_tools,
          output_format=str(contract["output_format"]),
          title=str(metadata["title"]),
          album=str(metadata["album"]),
          artist=str(metadata["artist"]),
          genre=str(metadata["genre"]),
          track_number=track_number,
          track_count=int(plan.manifest["track_count"]),
          cover_path=cover_path,
      )
      state["status"] = "complete"
      state["final_audio"] = {
          "file": output_path.name,
          **media_record(info),
          "checkpoint_version": ASSEMBLY_CHECKPOINT_VERSION,
          "assembly": contract,
          "assembly_sha256": sha256_text(canonical_json(contract)),
      }
      _save_run(run, manifest)
      current_run = GenerationRun(plan.workspace, run.run_path, run.run_id, manifest)
      verify_native_final_checkpoint(
          plan=plan,
          run=current_run,
          section=section,
          state=state,
          tools=media_tools,
      )
      verified_complete.add(track_number)

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
  """Load one path-confined run whose ID matches its persisted generation settings."""
  runs_root = _confined_workspace_path(
      plan.workspace,
      plan.workspace / "runs",
      "Generation runs directory",
  )
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
  path = _regular_workspace_file(plan.workspace, path, "Generation run manifest")
  manifest = load_json(path)
  generation = manifest.get("generation")
  actual_id = path.parent.name
  if (
      manifest.get("manifest_version") != MANIFEST_VERSION
      or manifest.get("kind") != "ebook-tts-generation"
      or manifest.get("plan_sha256") != plan.plan_id
      or manifest.get("run_id") != actual_id
      or not isinstance(generation, dict)
      or _run_id(plan, generation) != actual_id
  ):
    raise WorkspaceError(f"Generation run identity is invalid: {path}")
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
  stable_generation = _stable_generation(generation)
  stable_generation.pop("assembly", None)
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
