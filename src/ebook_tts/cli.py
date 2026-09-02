"""Command-line orchestration for ebook-tts."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, replace
from importlib import metadata
from pathlib import Path
from collections.abc import Sequence
from typing import Any

from . import __version__
from .config import default_config, load_config, write_config_template
from .epub.package import load_publication
from .errors import ConfigError, EbookTTSError, ProviderError
from .media.tools import preflight
from .models import MODEL_PROFILES, AppConfig, Plan, Publication
from .outputs.package import (
    PackageArtifact,
    package_archive,
    package_bookplayer,
    package_tracks,
)
from .providers.base import STTProvider, TTSProvider
from .qa.evaluate import evaluate_run
from .utils import slugify
from .workspace.adoption import adopt_legacy_v1
from .workspace.generation import (
    GenerationRun,
    approve_sample,
    approved_sample,
    authorize_retry,
    generate,
    generate_sample,
    load_run,
    require_native_generation_plan,
)
from .workspace.manifests import create_plan, load_plan


COMMANDS = (
    "doctor",
    "inspect",
    "init",
    "plan",
    "adopt",
    "sample",
    "generate",
    "validate",
    "package",
    "attempts",
    "build",
)


def _add_config(parser: argparse.ArgumentParser) -> None:
  parser.add_argument(
      "--config",
      type=Path,
      help="TOML configuration (defaults to ./audiobook.toml when present).",
  )


def _add_workspace_selection(parser: argparse.ArgumentParser) -> None:
  parser.add_argument("workspace", type=Path, help="ebook-tts workspace directory.")
  parser.add_argument("--plan-id", help="Plan SHA-256 (default: current plan).")


def build_parser() -> argparse.ArgumentParser:
  """Define the public command surface and paid-operation guardrails."""
  parser = argparse.ArgumentParser(
      prog="ebook-tts",
      description="Build validated audiobooks from DRM-free EPUB files.",
  )
  parser.add_argument("--version", action="version", version=__version__)
  subparsers = parser.add_subparsers(dest="command", required=True)

  doctor = subparsers.add_parser("doctor", help="Check local and provider prerequisites.")
  _add_config(doctor)
  doctor.add_argument("--json", action="store_true", help="Emit machine-readable output.")
  doctor.set_defaults(handler=_command_doctor)

  inspect_parser = subparsers.add_parser(
      "inspect", help="Read and report an EPUB without writing a workspace."
  )
  inspect_parser.add_argument("epub", type=Path)
  _add_config(inspect_parser)
  inspect_parser.add_argument("--json", action="store_true")
  inspect_parser.set_defaults(handler=_command_inspect)

  init_parser = subparsers.add_parser(
      "init", help="Create an editable audiobook.toml for an EPUB."
  )
  init_parser.add_argument("epub", type=Path)
  init_parser.add_argument("--output", type=Path, default=Path("audiobook.toml"))
  init_parser.add_argument("--force", action="store_true")
  init_parser.set_defaults(handler=_command_init)

  plan_parser = subparsers.add_parser(
      "plan", help="Extract, normalize, chunk, and estimate without API calls."
  )
  plan_parser.add_argument("epub", type=Path)
  _add_config(plan_parser)
  plan_parser.add_argument("--workspace", type=Path)
  plan_parser.add_argument("--json", action="store_true")
  plan_parser.set_defaults(handler=_command_plan)

  adopt = subparsers.add_parser(
      "adopt",
      help="Verify and adopt complete legacy-v1 output without provider calls.",
  )
  adopt.add_argument("legacy_root", type=Path)
  adopt.add_argument("--source", dest="source_epub", type=Path, required=True)
  adopt.add_argument("--workspace", type=Path, required=True)
  adopt.add_argument(
      "--output-dir",
      type=Path,
      help="Legacy output directory (default: LEGACY_ROOT/chapters_output).",
  )
  adopt.add_argument("--ffmpeg", default="ffmpeg")
  adopt.add_argument("--ffprobe", default="ffprobe")
  adopt.add_argument("--json", action="store_true")
  adopt.set_defaults(handler=_command_adopt)

  sample = subparsers.add_parser(
      "sample", help="Create or approve a short paid voice sample."
  )
  sample_commands = sample.add_subparsers(dest="sample_command", required=True)
  sample_create = sample_commands.add_parser("create", help="Generate a resumable sample.")
  _add_workspace_selection(sample_create)
  _add_config(sample_create)
  sample_create.add_argument("--track", type=int)
  sample_create.add_argument("--characters", type=int, default=1_000)
  sample_create.set_defaults(handler=_command_sample_create)
  sample_approve = sample_commands.add_parser("approve", help="Approve a listened-to sample.")
  _add_workspace_selection(sample_approve)
  sample_approve.add_argument("sample_id")
  sample_approve.set_defaults(handler=_command_sample_approve)

  generate_parser = subparsers.add_parser(
      "generate", help="Make paid TTS requests and resume exact checkpoints."
  )
  _add_workspace_selection(generate_parser)
  _add_config(generate_parser)
  generate_parser.add_argument(
      "--tracks", help="Track selection such as 1,3-5 (default: all)."
  )
  generate_parser.add_argument(
      "--yes",
      action="store_true",
      help="Explicitly bypass the approved-sample gate and authorize paid work.",
  )
  generate_parser.set_defaults(handler=_command_generate)

  validate = subparsers.add_parser(
      "validate", help="Run structural, signal, and optional STT quality gates."
  )
  _add_workspace_selection(validate)
  _add_config(validate)
  validate.add_argument("--run-id")
  validate.add_argument(
      "--stt",
      choices=("none", "elevenlabs"),
      help="Override qa.stt_provider for this evaluation.",
  )
  validate.set_defaults(handler=_command_validate)

  package = subparsers.add_parser(
      "package", help="Create QA-gated distribution artifacts."
  )
  _add_workspace_selection(package)
  package.add_argument("--run-id")
  package.add_argument(
      "--format",
      action="append",
      choices=("bookplayer", "archive", "tracks"),
      dest="formats",
      help="Repeat for multiple outputs (default: bookplayer and archive).",
  )
  package.add_argument("--output-dir", type=Path)
  package.add_argument("--allow-unvalidated", action="store_true")
  package.add_argument("--allow-failed-qa", action="store_true")
  package.set_defaults(handler=_command_package)

  attempts = subparsers.add_parser(
      "attempts", help="Inspect or explicitly reconcile ambiguous paid requests."
  )
  attempt_commands = attempts.add_subparsers(dest="attempt_command", required=True)
  attempt_list = attempt_commands.add_parser("list", help="List blocking attempt markers.")
  _add_workspace_selection(attempt_list)
  attempt_list.set_defaults(handler=_command_attempts_list)
  attempt_authorize = attempt_commands.add_parser(
      "authorize-retry", help="Quarantine evidence after checking provider history."
  )
  _add_workspace_selection(attempt_authorize)
  attempt_authorize.add_argument("attempt", type=Path)
  attempt_authorize.add_argument("--reason", required=True)
  attempt_authorize.set_defaults(handler=_command_attempts_authorize)

  build = subparsers.add_parser(
      "build", help="Plan, generate, validate, and package one EPUB."
  )
  build.add_argument("epub", type=Path)
  _add_config(build)
  build.add_argument("--workspace", type=Path)
  build.add_argument(
      "--format",
      action="append",
      choices=("bookplayer", "archive", "tracks"),
      dest="formats",
  )
  build.add_argument("--output-dir", type=Path)
  build.add_argument(
      "--yes",
      action="store_true",
      help="Authorize paid generation without an approved voice sample.",
  )
  build.set_defaults(handler=_command_build)
  return parser


def _config_path(value: Path | None) -> Path | None:
  if value is not None:
    return value.expanduser()
  conventional = Path("audiobook.toml")
  return conventional if conventional.is_file() else None


def _load_command_config(args: argparse.Namespace) -> AppConfig:
  return load_config(_config_path(getattr(args, "config", None)))


def _publication_record(publication: Publication) -> dict[str, Any]:
  return {
      "source": str(publication.source_path),
      "source_sha256": publication.source_sha256,
      "book": asdict(publication.metadata),
      "cover": {
          "present": publication.cover_bytes is not None,
          "media_type": publication.cover_media_type,
          "bytes": len(publication.cover_bytes or b""),
      },
      "warnings": list(publication.warnings),
      "tracks": [
          {
              "track_number": section.track_number,
              "chapter_number": section.chapter_number,
              "title": section.title,
              "source_href": section.source_href,
              "source_fragment": section.source_fragment,
              "characters": len(section.text),
          }
          for section in publication.sections
      ],
  }


def _print_publication(publication: Publication) -> None:
  print(f"Title: {publication.metadata.title}")
  print(f"Author: {publication.metadata.author_display}")
  print(f"Language: {publication.metadata.language or 'unspecified'}")
  print(f"Cover: {'yes' if publication.cover_bytes else 'no'}")
  print(f"Narratable tracks: {len(publication.sections)}")
  for section in publication.sections:
    print(
        f"  {section.track_number:03d}  {section.title}  "
        f"({len(section.text):,} characters; {section.source_href}"
        f"{'#' + section.source_fragment if section.source_fragment else ''})"
    )
  for warning in publication.warnings:
    print(f"Warning: {warning}", file=sys.stderr)


def _command_doctor(args: argparse.Namespace) -> int:
  config = _load_command_config(args)
  checks: list[dict[str, Any]] = []

  def record(name: str, ok: bool, detail: str) -> None:
    checks.append({"name": name, "ok": ok, "detail": detail})

  record("python", sys.version_info >= (3, 11), sys.version.split()[0])
  try:
    tools = preflight(config.audio.ffmpeg, config.audio.ffprobe)
    record("ffmpeg", True, tools.ffmpeg)
    record("ffprobe", True, tools.ffprobe)
  except EbookTTSError as exc:
    record("media-tools", False, str(exc))
  try:
    sdk_version = metadata.version("elevenlabs")
    record("elevenlabs-sdk", True, sdk_version)
  except metadata.PackageNotFoundError:
    record("elevenlabs-sdk", False, "install ebook-tts[elevenlabs]")
  record(
      "ELEVENLABS_API_KEY",
      bool(os.getenv("ELEVENLABS_API_KEY")),
      "set" if os.getenv("ELEVENLABS_API_KEY") else "not set",
  )
  record(
      "voice-id",
      bool(config.tts.voice_id),
      config.tts.voice_id or "set tts.voice_id or ELEVENLABS_VOICE_ID",
  )
  profile = MODEL_PROFILES.get(config.tts.model_id)
  record(
      "model-profile",
      profile is not None,
      (
          f"{config.tts.model_id}: {config.tts.max_characters:,}/"
          f"{profile.maximum_characters:,} characters"
          if profile
          else f"unknown model {config.tts.model_id}; explicit limit in use"
      ),
  )
  if config.tts.output_format == "mp3_44100_192":
    checks.append(
        {
            "name": "output-tier",
            "ok": True,
            "detail": "192 kbps requires an eligible ElevenLabs subscription tier",
            "warning": True,
        }
    )
  if args.json:
    print(json.dumps({"checks": checks}, indent=2))
  else:
    for check in checks:
      marker = "WARN" if check.get("warning") else "OK" if check["ok"] else "FAIL"
      print(f"[{marker:4}] {check['name']}: {check['detail']}")
  return 0 if all(check["ok"] for check in checks) else 1


def _command_inspect(args: argparse.Namespace) -> int:
  publication = load_publication(args.epub, _load_command_config(args))
  if args.json:
    print(json.dumps(_publication_record(publication), ensure_ascii=False, indent=2))
  else:
    _print_publication(publication)
  return 0


def _command_init(args: argparse.Namespace) -> int:
  output = args.output.expanduser()
  if output.exists() and not args.force:
    raise ConfigError(f"Configuration already exists: {output}; use --force to replace it.")
  publication = load_publication(args.epub, default_config())
  write_config_template(output, publication.metadata)
  print(f"Created {output}")
  print("Set tts.voice_id, then run `ebook-tts plan`.")
  return 0


def _default_workspace(publication: Publication) -> Path:
  return Path(f"{slugify(publication.metadata.title)}.ebook-tts")


def _command_plan(args: argparse.Namespace) -> int:
  config = _load_command_config(args)
  publication = load_publication(args.epub, config)
  workspace = args.workspace or _default_workspace(publication)
  plan = create_plan(publication, config, workspace)
  summary = {
      "workspace": str(plan.workspace),
      "plan_id": plan.plan_id,
      "title": plan.manifest["book"]["title"],
      "tracks": plan.manifest["track_count"],
      "characters": plan.manifest["characters"],
      "chunks": plan.manifest["chunk_count"],
      "max_characters": config.tts.max_characters,
      "warnings": plan.manifest["warnings"],
  }
  if args.json:
    print(json.dumps(summary, ensure_ascii=False, indent=2))
  else:
    print(f"Plan: {plan.plan_id}")
    print(f"Workspace: {plan.workspace}")
    print(
        f"{summary['tracks']} tracks; {summary['characters']:,} characters; "
        f"{summary['chunks']} paid request(s) at up to "
        f"{summary['max_characters']:,} characters."
    )
    print("No provider API calls were made.")
  return 0


def _command_adopt(args: argparse.Namespace) -> int:
  result = adopt_legacy_v1(
      legacy_root=args.legacy_root,
      source_epub=args.source_epub,
      workspace=args.workspace,
      output_directory=args.output_dir,
      ffmpeg=args.ffmpeg,
      ffprobe=args.ffprobe,
  )
  summary = {
      "workspace": str(result.workspace),
      "plan_id": result.plan.plan_id,
      "run_id": result.run.run_id,
      "tracks": result.tracks,
      "chunks": result.chunks,
      "audio_bytes": result.audio_bytes,
      "verification_only": True,
  }
  if args.json:
    print(json.dumps(summary, ensure_ascii=False, indent=2))
  else:
    print(f"Adopted plan: {result.plan.plan_id}")
    print(f"Adopted run: {result.run.run_id}")
    print(f"Workspace: {result.workspace}")
    print(
        f"Verified {result.tracks} track(s), {result.chunks} chunk(s), and "
        f"{result.audio_bytes:,} copied audio bytes."
    )
    print("The adopted plan is verification-only; no provider API calls were made.")
  return 0


def _load_selected_plan(args: argparse.Namespace) -> Plan:
  return load_plan(args.workspace, getattr(args, "plan_id", None))


def _tts_provider(config: AppConfig) -> TTSProvider:
  if config.tts.provider != "elevenlabs":
    raise ProviderError(f"Unsupported TTS provider: {config.tts.provider}")
  from .providers.elevenlabs import ElevenLabsTTSProvider

  return ElevenLabsTTSProvider(os.getenv("ELEVENLABS_API_KEY", ""))


def _command_sample_create(args: argparse.Namespace) -> int:
  plan = _load_selected_plan(args)
  require_native_generation_plan(plan)
  config = _load_command_config(args)
  sample = generate_sample(
      plan=plan,
      config=config,
      provider=_tts_provider(config),
      track_number=args.track,
      characters=args.characters,
  )
  print(f"Sample ID: {sample.sample_id}")
  print(f"Audio: {sample.audio_path}")
  if sample.approved:
    print("Status: already approved")
  else:
    print("Listen to the sample, then approve it with:")
    print(
        f"  ebook-tts sample approve {plan.workspace} {sample.sample_id}"
        f" --plan-id {plan.plan_id}"
    )
  return 0


def _command_sample_approve(args: argparse.Namespace) -> int:
  plan = _load_selected_plan(args)
  sample = approve_sample(plan, args.sample_id)
  print(f"Approved sample {sample.sample_id}")
  print(f"Audio: {sample.audio_path}")
  return 0


def _parse_tracks(value: str | None, maximum: int) -> set[int] | None:
  if value is None:
    return None
  selected: set[int] = set()
  for raw in value.split(","):
    part = raw.strip()
    if not part:
      raise ConfigError(f"Invalid track selection: {value!r}")
    if "-" in part:
      left, right = part.split("-", 1)
      if not left.isdigit() or not right.isdigit():
        raise ConfigError(f"Invalid track range: {part!r}")
      start, end = int(left), int(right)
      if start > end:
        raise ConfigError(f"Reversed track range: {part!r}")
      selected.update(range(start, end + 1))
    elif part.isdigit():
      selected.add(int(part))
    else:
      raise ConfigError(f"Invalid track number: {part!r}")
  invalid = sorted(number for number in selected if not 1 <= number <= maximum)
  if invalid:
    raise ConfigError(f"Tracks must be between 1 and {maximum}: {invalid}")
  return selected


def _generation_summary(plan: Plan, selected: set[int] | None) -> tuple[int, int, int]:
  sections = plan.manifest["sections"]
  chosen = [
      section
      for section in sections
      if selected is None or int(section["track_number"]) in selected
  ]
  return (
      len(chosen),
      sum(int(section["characters"]) for section in chosen),
      sum(int(section["chunk_count"]) for section in chosen),
  )


def _generate_from_args(
    plan: Plan,
    config: AppConfig,
    tracks_value: str | None,
    explicit_yes: bool,
) -> GenerationRun:
  require_native_generation_plan(plan)
  selected = _parse_tracks(tracks_value, int(plan.manifest["track_count"]))
  if not explicit_yes and approved_sample(plan, config, config.tts.provider) is None:
    raise ProviderError(
        "Paid generation requires an approved matching voice sample. Run "
        "`ebook-tts sample create`, listen, and approve it; or pass --yes to "
        "explicitly bypass this gate."
    )
  count, characters, chunks = _generation_summary(plan, selected)
  print(
      f"Authorized paid generation: {count} track(s), {characters:,} text "
      f"characters, {chunks} request(s)."
  )
  return generate(
      plan=plan,
      config=config,
      provider=_tts_provider(config),
      selected_tracks=selected,
  )


def _command_generate(args: argparse.Namespace) -> int:
  plan = _load_selected_plan(args)
  run = _generate_from_args(
      plan,
      _load_command_config(args),
      args.tracks,
      args.yes,
  )
  print(f"Run: {run.run_id}")
  print(f"Status: {run.manifest['status']}")
  print(f"Audio: {run.run_path.parent / 'audio'}")
  return 0


def _stt_provider(config: AppConfig) -> STTProvider | None:
  if config.qa.stt_provider == "none":
    return None
  if config.qa.stt_provider != "elevenlabs":
    raise ProviderError(f"Unsupported STT provider: {config.qa.stt_provider}")
  from .providers.elevenlabs import ElevenLabsSTTProvider

  return ElevenLabsSTTProvider(
      os.getenv("ELEVENLABS_API_KEY", ""),
      model_id=config.qa.stt_model,
  )


def _validate(
    plan: Plan,
    run: GenerationRun,
    config: AppConfig,
) -> tuple[int, Path]:
  report = evaluate_run(
      plan=plan,
      run=run,
      config=config,
      stt_provider=_stt_provider(config),
  )
  print(f"QA status: {report.report['status']}")
  print(f"JSON report: {report.report_path}")
  print(f"HTML report: {report.html_path}")
  return (2 if report.report["status"] == "fail" else 0), report.report_path


def _command_validate(args: argparse.Namespace) -> int:
  plan = _load_selected_plan(args)
  config = _load_command_config(args)
  if args.stt:
    config = replace(config, qa=replace(config.qa, stt_provider=args.stt))
  return _validate(plan, load_run(plan, args.run_id), config)[0]


def _package_formats(
    *,
    plan: Plan,
    run: GenerationRun,
    formats: list[str] | None,
    output_directory: Path | None,
    allow_unvalidated: bool,
    allow_failed_qa: bool,
) -> list[PackageArtifact]:
  output = (output_directory or (plan.workspace / "dist")).expanduser().resolve()
  selected = formats or ["bookplayer", "archive"]
  functions = {
      "bookplayer": package_bookplayer,
      "archive": package_archive,
      "tracks": package_tracks,
  }
  artifacts: list[PackageArtifact] = []
  for name in selected:
    artifact = functions[name](
        plan=plan,
        run=run,
        output_directory=output,
        allow_unvalidated=allow_unvalidated,
        allow_failed_qa=allow_failed_qa,
    )
    artifacts.append(artifact)
    print(f"{name}: {artifact.path} ({artifact.bytes:,} bytes)")
  return artifacts


def _command_package(args: argparse.Namespace) -> int:
  plan = _load_selected_plan(args)
  _package_formats(
      plan=plan,
      run=load_run(plan, args.run_id),
      formats=args.formats,
      output_directory=args.output_dir,
      allow_unvalidated=args.allow_unvalidated,
      allow_failed_qa=args.allow_failed_qa,
  )
  return 0


def _workspace_attempts(plan: Plan) -> list[Path]:
  runs_root = plan.workspace / "runs"
  if not runs_root.is_dir():
    return []
  return sorted(
      path
      for path in runs_root.rglob("*.attempt.json")
      if not any(
          part.casefold() == "quarantine"
          for part in path.relative_to(runs_root).parts[:-1]
      )
  )


def _command_attempts_list(args: argparse.Namespace) -> int:
  plan = _load_selected_plan(args)
  attempts = _workspace_attempts(plan)
  if not attempts:
    print("No ambiguous attempt markers.")
    return 0
  for path in attempts:
    print(path.relative_to(plan.workspace))
  return 2


def _command_attempts_authorize(args: argparse.Namespace) -> int:
  plan = _load_selected_plan(args)
  quarantine = authorize_retry(
      plan=plan,
      attempt_path=args.attempt,
      reason=args.reason,
  )
  print(f"Authorized evidence moved to {quarantine}")
  return 0


def _command_build(args: argparse.Namespace) -> int:
  config = _load_command_config(args)
  publication = load_publication(args.epub, config)
  plan = create_plan(
      publication,
      config,
      args.workspace or _default_workspace(publication),
  )
  run = _generate_from_args(plan, config, None, args.yes)
  validation_code, _ = _validate(plan, run, config)
  if validation_code:
    print("Packaging was skipped because QA failed.", file=sys.stderr)
    return validation_code
  _package_formats(
      plan=plan,
      run=run,
      formats=args.formats,
      output_directory=args.output_dir,
      allow_unvalidated=False,
      allow_failed_qa=False,
  )
  return 0


def main(argv: Sequence[str] | None = None) -> int:
  """Run a command and convert expected failures into concise diagnostics."""
  args = build_parser().parse_args(argv)
  try:
    return int(args.handler(args))
  except EbookTTSError as exc:
    print(f"Error: {exc}", file=sys.stderr)
    return 1
  except KeyboardInterrupt:
    print(
        "\nInterrupted. Completed checkpoints remain resumable; ambiguous paid "
        "requests remain blocked for explicit reconciliation.",
        file=sys.stderr,
    )
    return 130


if __name__ == "__main__":
  raise SystemExit(main())
