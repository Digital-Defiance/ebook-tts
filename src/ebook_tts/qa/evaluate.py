"""Finished-run structural, signal, and optional transcription evaluation."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from pathlib import Path
import re
from typing import Any

from ..errors import QualityError, WorkspaceError
from ..media.tools import MediaTools, decode_audio, preflight, probe_audio
from ..models import MANIFEST_VERSION, AppConfig, Plan
from ..providers.base import STTProvider, Transcript, TranscriptWord
from ..utils import (
    atomic_write_json,
    canonical_json,
    load_json,
    sha256_file,
    sha256_text,
    utc_now,
)
from ..workspace.generation import GenerationRun, is_adopted_plan
from ..workspace.locking import WorkspaceLock
from ..workspace.manifests import read_planned_chunk_text
from ..workspace.request_identity import request_fingerprint_candidates
from .alignment import align_text, comparison_words, missing_protected_terms
from .report import render_html
from .signal import analyze_signal, signal_record


QA_VERSION = 1
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class QualityReport:
  qa_id: str
  report_path: Path
  html_path: Path
  report: dict[str, Any]


@dataclass(frozen=True)
class _ValidatedChunk:
  planned: dict[str, Any]
  generated: dict[str, Any]
  index: int
  path: Path
  reference: str
  audio_sha256: str
  media: Any


def _term_occurs(reference: str, term: str) -> bool:
  reference_words = comparison_words(reference)
  term_words = comparison_words(term)
  if not term_words:
    return False
  return any(
      reference_words[index : index + len(term_words)] == term_words
      for index in range(len(reference_words) - len(term_words) + 1)
  )


def _qa_configuration(config: AppConfig, stt: STTProvider | None) -> dict[str, Any]:
  return {
      "qa_version": QA_VERSION,
      "stt_provider": stt.name if stt else "none",
      "stt_model": config.qa.stt_model if stt else None,
      "language": config.qa.language,
      "max_word_error_rate": config.qa.max_word_error_rate,
      "max_character_error_rate": config.qa.max_character_error_rate,
      "max_internal_silence_seconds": config.qa.max_internal_silence_seconds,
      "clipping_peak_db": config.qa.clipping_peak_db,
      "protected_terms": list(config.qa.protected_terms),
  }


def _transcript_from_record(record: dict[str, Any]) -> Transcript:
  words = tuple(
      TranscriptWord(
          text=str(item.get("text", "")),
          start=item.get("start"),
          end=item.get("end"),
          confidence=item.get("confidence"),
      )
      for item in record.get("words", [])
      if isinstance(item, dict)
  )
  raw = record.get("raw") if isinstance(record.get("raw"), dict) else {}
  return Transcript(
      text=str(record.get("text", "")),
      language=record.get("language"),
      words=words,
      raw=raw,
  )


def _transcribe_cached(
    *,
    provider: STTProvider,
    audio_path: Path,
    audio_sha256: str,
    reference_sha256: str,
    cache_path: Path,
    language: str | None,
    model_id: str,
) -> Transcript:
  fingerprint = sha256_text(
      canonical_json(
          {
              "provider": provider.name,
              "model_id": model_id,
              "language": language,
              "audio_sha256": audio_sha256,
              "reference_sha256": reference_sha256,
          }
      )
  )
  if cache_path.exists():
    record = load_json(cache_path)
    if record.get("fingerprint") != fingerprint:
      raise QualityError(f"Transcript cache fingerprint mismatch: {cache_path}")
    return _transcript_from_record(record)

  attempt_path = cache_path.with_name(f"{cache_path.stem}.attempt.json")
  if attempt_path.exists():
    raise QualityError(
        f"A prior paid STT request has an ambiguous outcome: {attempt_path}. "
        "No request was reissued."
    )
  atomic_write_json(
      attempt_path,
      {
          "manifest_version": MANIFEST_VERSION,
          "kind": "ebook-tts-stt-attempt",
          "fingerprint": fingerprint,
          "audio_sha256": audio_sha256,
          "reference_sha256": reference_sha256,
          "provider": provider.name,
          "model_id": model_id,
          "started_at": utc_now(),
      },
  )
  try:
    transcript = provider.transcribe(audio_path, language=language)
    record = {
        "manifest_version": MANIFEST_VERSION,
        "kind": "ebook-tts-transcript",
        "fingerprint": fingerprint,
        "audio_sha256": audio_sha256,
        "reference_sha256": reference_sha256,
        "provider": provider.name,
        "model_id": model_id,
        "language": transcript.language,
        "text": transcript.text,
        "words": [asdict(word) for word in transcript.words],
        "raw": dict(transcript.raw),
        "created_at": utc_now(),
    }
    atomic_write_json(cache_path, record)
    attempt_path.unlink()
    return transcript
  except Exception as exc:
    raise QualityError(
        f"STT request failed. Ambiguous billing evidence remains at "
        f"{attempt_path}; no retry will be automatic: {exc}"
    ) from exc


def _plan_sections(plan: Plan) -> dict[int, dict[str, Any]]:
  raw = plan.manifest.get("sections")
  if not isinstance(raw, list):
    raise WorkspaceError("Plan sections are invalid.")
  sections: dict[int, dict[str, Any]] = {}
  for item in raw:
    if not isinstance(item, dict):
      raise WorkspaceError("Plan section records are invalid.")
    number = item.get("track_number")
    if not isinstance(number, int) or isinstance(number, bool) or number in sections:
      raise WorkspaceError("Plan track numbers are invalid or duplicated.")
    sections[number] = item
  expected = list(range(1, len(raw) + 1))
  if sorted(sections) != expected or plan.manifest.get("track_count") != len(raw):
    raise WorkspaceError("Plan tracks are not contiguous or count-consistent.")
  return sections


def _audio_path(root: Path, filename: object) -> Path:
  if not isinstance(filename, str) or Path(filename).name != filename:
    raise WorkspaceError(f"Invalid audio filename in manifest: {filename!r}")
  return root / filename


def _validated_chunks(
    *,
    plan: Plan,
    section: dict[str, Any],
    state: dict[str, Any],
    run_root: Path,
    tools: MediaTools,
    output_format: str,
    generation: dict[str, Any],
    legacy_v1: bool,
) -> list[_ValidatedChunk]:
  """Re-verify exact request, sidecar, reference, and media coverage offline."""
  track_number = int(section["track_number"])
  plan_chunks = section.get("chunks")
  run_chunks = state.get("chunks")
  if not isinstance(plan_chunks, list) or not isinstance(run_chunks, list):
    raise QualityError(f"Track {track_number} has invalid chunk manifests.")
  planned_count = section.get("chunk_count")
  if not isinstance(planned_count, int) or isinstance(planned_count, bool):
    raise QualityError(f"Track {track_number} has an invalid planned chunk count.")
  if planned_count != len(plan_chunks) or len(run_chunks) != len(plan_chunks):
    raise QualityError(
        f"Track {track_number} has {len(run_chunks)} generated chunks but "
        f"{len(plan_chunks)} planned chunks."
    )
  completed_count = state.get("completed_chunks")
  if completed_count is not None and completed_count != len(run_chunks):
    raise QualityError(f"Track {track_number} completed chunk count is inconsistent.")

  references: list[str] = []
  for position, (raw_planned, raw_generated) in enumerate(
      zip(plan_chunks, run_chunks), start=1
  ):
    if not isinstance(raw_planned, dict) or not isinstance(raw_generated, dict):
      raise QualityError(f"Track {track_number}, chunk {position} record is invalid.")
    if raw_planned.get("index") != position:
      raise QualityError(
          f"Track {track_number} planned chunks are not ordered and contiguous from 1."
      )
    reference = read_planned_chunk_text(
        plan,
        raw_planned,
        label=f"Track {track_number}, chunk {position}",
    )
    reference_sha = raw_planned.get("text_sha256")
    if not isinstance(reference_sha, str) or sha256_text(reference) != reference_sha:
      raise QualityError(
          f"Track {track_number}, chunk {position} reference hash mismatch."
      )
    characters = raw_planned.get("characters")
    if (
        not isinstance(characters, int)
        or isinstance(characters, bool)
        or len(reference) != characters
    ):
      raise QualityError(
          f"Track {track_number}, chunk {position} reference character count mismatch."
      )
    references.append(reference)

  result: list[_ValidatedChunk] = []
  chunk_directory = run_root / "chunks" / str(section["output_stem"])
  for position, (raw_planned, raw_generated, reference) in enumerate(
      zip(plan_chunks, run_chunks, references), start=1
  ):
    generated_index = raw_generated.get("index")
    if generated_index is not None and generated_index != position:
      raise QualityError(
          f"Track {track_number} generated chunks are not ordered and contiguous."
      )
    reference_sha = str(raw_planned["text_sha256"])
    if raw_generated.get("text_sha256") != reference_sha:
      raise QualityError(
          f"Track {track_number}, chunk {position} generated/reference hash mismatch."
      )
    request_sha = raw_generated.get("request_sha256")
    if not isinstance(request_sha, str) or not _SHA256.fullmatch(request_sha):
      raise QualityError(
          f"Track {track_number}, chunk {position} request fingerprint is invalid."
      )
    try:
      candidates = request_fingerprint_candidates(
          texts=references,
          position=position - 1,
          generation=generation,
          legacy_v1=legacy_v1,
      )
    except ValueError as exc:
      raise QualityError(
          f"Track {track_number}, chunk {position} request identity is invalid: {exc}"
      ) from exc
    if request_sha not in candidates:
      raise QualityError(
          f"Track {track_number}, chunk {position} request fingerprint mismatch."
      )
    filename = raw_generated.get("audio_file")
    expected_filename = f"chunk_{position:04d}_{request_sha}.mp3"
    if filename != expected_filename:
      raise QualityError(
          f"Track {track_number}, chunk {position} audio filename is out of order."
      )
    chunk_path = _audio_path(chunk_directory, filename)
    sidecar = load_json(chunk_path.with_suffix(chunk_path.suffix + ".json"))
    if (
        sidecar.get("manifest_version") != MANIFEST_VERSION
        or sidecar.get("kind") != "ebook-tts-chunk"
        or sidecar != raw_generated
    ):
      raise QualityError(
          f"Track {track_number}, chunk {position} sidecar disagrees with the run."
      )
    audio_sha = raw_generated.get("audio_sha256")
    if (
        not isinstance(audio_sha, str)
        or not _SHA256.fullmatch(audio_sha)
        or sha256_file(chunk_path) != audio_sha
    ):
      raise QualityError(
          f"Track {track_number}, chunk {position} audio hash mismatch."
      )
    chunk_media = probe_audio(
        chunk_path,
        tools.ffprobe,
        expected_format=output_format,
    )
    decode_audio(chunk_path, tools.ffmpeg)
    stored_media = raw_generated.get("media")
    if not isinstance(stored_media, dict):
      raise QualityError(
          f"Track {track_number}, chunk {position} has no media checkpoint."
      )
    if (
        stored_media.get("sha256") != chunk_media.sha256
        or stored_media.get("bytes") != chunk_media.bytes
        or stored_media.get("codec") != chunk_media.codec
        or stored_media.get("sample_rate") != chunk_media.sample_rate
        or stored_media.get("channels") != chunk_media.channels
    ):
      raise QualityError(
          f"Track {track_number}, chunk {position} media checkpoint mismatch."
      )
    try:
      stored_duration = float(stored_media["duration_seconds"])
    except (KeyError, TypeError, ValueError) as exc:
      raise QualityError(
          f"Track {track_number}, chunk {position} duration checkpoint is invalid."
      ) from exc
    if not math.isfinite(stored_duration) or abs(
        stored_duration - chunk_media.duration_seconds
    ) > max(0.01, chunk_media.duration_seconds * 0.005):
      raise QualityError(
          f"Track {track_number}, chunk {position} duration checkpoint mismatch."
      )
    result.append(
        _ValidatedChunk(
            planned=raw_planned,
            generated=raw_generated,
            index=position,
            path=chunk_path,
            reference=reference,
            audio_sha256=audio_sha,
            media=chunk_media,
        )
    )
  return result


def evaluate_run(
    *,
    plan: Plan,
    run: GenerationRun,
    config: AppConfig,
    stt_provider: STTProvider | None = None,
    tools: MediaTools | None = None,
) -> QualityReport:
  """Evaluate generated audio and publish cached JSON plus HTML reports."""
  if config.qa.stt_provider != "none" and stt_provider is None:
    raise QualityError(
        f"qa.stt_provider={config.qa.stt_provider!r} requires an STT provider."
    )
  media_tools = tools or preflight(config.audio.ffmpeg, config.audio.ffprobe)
  qa_configuration = _qa_configuration(config, stt_provider)
  qa_id = sha256_text(
      canonical_json({"run_id": run.run_id, "configuration": qa_configuration})
  )
  run_root = run.run_path.parent
  qa_root = run_root / "qa" / qa_id
  report_path = qa_root / "report.json"
  html_path = qa_root / "report.html"

  with WorkspaceLock(plan.workspace):
    run_manifest = load_json(run.run_path)
    if (
        run_manifest.get("manifest_version") != MANIFEST_VERSION
        or run_manifest.get("kind") != "ebook-tts-generation"
        or run_manifest.get("run_id") != run.run_id
        or run_manifest.get("plan_sha256") != plan.plan_id
        or run.run_path.parent.name != run.run_id
    ):
      raise WorkspaceError("Generation run identity is invalid.")
    plan_sections = _plan_sections(plan)
    run_sections = run_manifest.get("sections")
    generation = run_manifest.get("generation")
    if not isinstance(run_sections, dict) or not isinstance(generation, dict):
      raise WorkspaceError("Generation run sections or settings are invalid.")
    if run_manifest.get("track_count") != len(plan_sections):
      raise WorkspaceError("Generation run track count does not match the plan.")
    plan_adopted = is_adopted_plan(plan)
    run_adopted = bool(
        run_manifest.get("adopted")
        or run_manifest.get("verification_only")
        or run_manifest.get("adoption")
    )
    if plan_adopted != run_adopted:
      raise WorkspaceError("Plan and generation run adoption identities disagree.")
    failures: list[str] = []
    warnings: list[str] = []
    track_reports: list[dict[str, Any]] = []
    total_word_edits = 0
    total_reference_words = 0
    total_character_edits = 0
    total_reference_characters = 0
    output_format = str(generation.get("output_format", ""))
    language = config.qa.language or plan.manifest.get("book", {}).get("language")

    if run_manifest.get("status") != "complete":
      warnings.append(
          f"Generation run is {run_manifest.get('status')}; report covers only "
          "completed tracks."
      )

    for track_number in sorted(plan_sections):
      section = plan_sections[track_number]
      state = run_sections.get(str(track_number))
      if not isinstance(state, dict) or state.get("status") != "complete":
        failures.append(f"Track {track_number} is missing or incomplete.")
        continue
      title = str(section["title"])
      output_stem = str(section["output_stem"])
      if (
          state.get("track_number") != track_number
          or state.get("title") != section["title"]
          or state.get("output_stem") != section["output_stem"]
      ):
        failures.append(
            f"Track {track_number} run identity does not match its immutable plan section."
        )
        continue
      track_warnings: list[str] = []
      final_record = state.get("final_audio")
      if not isinstance(final_record, dict):
        failures.append(f"Track {track_number} has no final audio record.")
        continue
      expected_final_name = f"{output_stem}.mp3"
      if final_record.get("file") != expected_final_name:
        failures.append(
            f"Track {track_number} final audio filename does not match its plan section."
        )
        continue
      final_path = _audio_path(run_root / "audio", expected_final_name)
      try:
        final_sha = final_record.get("sha256")
        if (
            not isinstance(final_sha, str)
            or not _SHA256.fullmatch(final_sha)
            or sha256_file(final_path) != final_sha
        ):
          raise QualityError("final audio hash mismatch")
        media = probe_audio(
            final_path,
            media_tools.ffprobe,
            expected_format=output_format,
        )
        decode_audio(final_path, media_tools.ffmpeg)
        if (
            final_record.get("bytes") != media.bytes
            or final_record.get("codec") != media.codec
            or final_record.get("sample_rate") != media.sample_rate
            or final_record.get("channels") != media.channels
        ):
          raise QualityError("final audio media checkpoint mismatch")
        try:
          recorded_final_duration = float(final_record["duration_seconds"])
        except (KeyError, TypeError, ValueError) as exc:
          raise QualityError("final audio duration checkpoint is invalid") from exc
        if not math.isfinite(recorded_final_duration) or abs(
            recorded_final_duration - media.duration_seconds
        ) > max(0.01, media.duration_seconds * 0.005):
          raise QualityError("final audio duration checkpoint mismatch")
        signal = analyze_signal(
            final_path,
            media_tools,
            max_internal_silence_seconds=config.qa.max_internal_silence_seconds,
            clipping_peak_db=config.qa.clipping_peak_db,
        )
        track_warnings.extend(signal.warnings)
      except Exception as exc:
        failures.append(f"Track {track_number} ({title}) media validation failed: {exc}")
        continue

      try:
        validated_chunks = _validated_chunks(
            plan=plan,
            section=section,
            state=state,
            run_root=run_root,
            tools=media_tools,
            output_format=output_format,
            generation=generation,
            legacy_v1=plan_adopted,
        )
        chunk_duration = sum(item.media.duration_seconds for item in validated_chunks)
        duration_delta = abs(media.duration_seconds - chunk_duration)
        duration_allowed = max(2.0, chunk_duration * 0.02)
        if duration_delta > duration_allowed:
          raise QualityError(
              f"final duration differs from chunk sum by {duration_delta:.2f}s "
              f"(allowed {duration_allowed:.2f}s)"
          )
      except Exception as exc:
        failures.append(
            f"Track {track_number} ({title}) chunk validation failed: {exc}"
        )
        continue

      transcription_record: dict[str, Any] | None = None
      if stt_provider is not None:
        chunk_reports: list[dict[str, Any]] = []
        track_word_edits = 0
        track_reference_words = 0
        track_character_edits = 0
        track_reference_characters = 0
        missing_terms: set[str] = set()
        for validated in validated_chunks:
          transcript = _transcribe_cached(
              provider=stt_provider,
              audio_path=validated.path,
              audio_sha256=validated.audio_sha256,
              reference_sha256=str(validated.planned["text_sha256"]),
              cache_path=(
                  qa_root
                  / "transcripts"
                  / str(section["output_stem"])
                  / f"chunk_{validated.index:04d}.json"
              ),
              language=str(language) if language else None,
              model_id=config.qa.stt_model,
          )
          alignment = align_text(validated.reference, transcript.text)
          relevant_terms = [
              term
              for term in config.qa.protected_terms
              if _term_occurs(validated.reference, term)
          ]
          chunk_missing = missing_protected_terms(transcript.text, relevant_terms)
          missing_terms.update(chunk_missing)
          suspicious = [
              span
              for span in alignment.spans
              if int(span["reference_word_count"]) >= 8
              or int(span["hypothesis_word_count"]) >= 8
          ]
          if suspicious:
            track_warnings.append(
                f"Chunk {validated.index} has {len(suspicious)} long transcript "
                "difference span(s) requiring review."
            )
          if chunk_missing:
            track_warnings.append(
                f"Chunk {validated.index} did not transcribe protected term(s): "
                + ", ".join(chunk_missing)
            )
          track_word_edits += alignment.word_edits
          track_reference_words += alignment.reference_words
          track_character_edits += alignment.character_edits
          track_reference_characters += alignment.reference_characters
          chunk_reports.append(
              {
                  "index": validated.index,
                  "audio_sha256": validated.audio_sha256,
                  "reference_sha256": validated.planned["text_sha256"],
                  "word_error_rate": alignment.word_error_rate,
                  "character_error_rate": alignment.character_error_rate,
                  "word_edits": alignment.word_edits,
                  "reference_words": alignment.reference_words,
                  "character_edits": alignment.character_edits,
                  "reference_characters": alignment.reference_characters,
                  "missing_protected_terms": list(chunk_missing),
                  "review_spans": suspicious,
              }
          )
        word_rate = track_word_edits / max(1, track_reference_words)
        character_rate = track_character_edits / max(1, track_reference_characters)
        transcription_record = {
            "word_error_rate": word_rate,
            "character_error_rate": character_rate,
            "word_edits": track_word_edits,
            "reference_words": track_reference_words,
            "character_edits": track_character_edits,
            "reference_characters": track_reference_characters,
            "missing_protected_terms": sorted(missing_terms),
            "chunks": chunk_reports,
        }
        total_word_edits += track_word_edits
        total_reference_words += track_reference_words
        total_character_edits += track_character_edits
        total_reference_characters += track_reference_characters
        if (
            config.qa.max_word_error_rate is not None
            and word_rate > config.qa.max_word_error_rate
        ):
          failures.append(
              f"Track {track_number} WER {word_rate:.2%} exceeds "
              f"{config.qa.max_word_error_rate:.2%}."
          )
        if (
            config.qa.max_character_error_rate is not None
            and character_rate > config.qa.max_character_error_rate
        ):
          failures.append(
              f"Track {track_number} CER {character_rate:.2%} exceeds "
              f"{config.qa.max_character_error_rate:.2%}."
          )

      warnings.extend(f"Track {track_number}: {item}" for item in track_warnings)
      track_reports.append(
          {
              "track_number": track_number,
              "title": title,
              "audio_file": final_path.name,
              "audio_sha256": media.sha256,
              "duration_seconds": media.duration_seconds,
              "media": asdict(media),
              "signal": signal_record(signal),
              "transcription": transcription_record,
              "warnings": track_warnings,
          }
      )

    transcription_summary = None
    if stt_provider is not None:
      transcription_summary = {
          "word_error_rate": total_word_edits / max(1, total_reference_words),
          "character_error_rate": total_character_edits
          / max(1, total_reference_characters),
          "word_edits": total_word_edits,
          "reference_words": total_reference_words,
          "character_edits": total_character_edits,
          "reference_characters": total_reference_characters,
      }
    status = "fail" if failures else "warn" if warnings else "pass"
    report = {
        "manifest_version": MANIFEST_VERSION,
        "kind": "ebook-tts-quality-report",
        "qa_id": qa_id,
        "run_id": run.run_id,
        "plan_sha256": plan.plan_id,
        "created_at": utc_now(),
        "status": status,
        "configuration": qa_configuration,
        "book": plan.manifest["book"],
        "expected_tracks": plan.manifest["track_count"],
        "validated_tracks": len(track_reports),
        "failures": failures,
        "warnings": warnings,
        "transcription": transcription_summary,
        "tracks": track_reports,
    }
    atomic_write_json(report_path, report)
    render_html(report, html_path)
    return QualityReport(qa_id, report_path, html_path, report)
