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
from .editions.epub import build_epub
from .errors import ConfigError, EbookTTSError, ProviderError
from .hooks.git import install_git_hooks
from .hooks.lifecycle import pre_commit_check, rebuild_if_stale, status_report
from .manuscript.check import check_manuscript
from .manuscript.compile import (
    manuscript_root,
    publication_from_manuscript,
    write_compiled_markdown,
)
from .manuscript.extract import extract_manuscript
from .media.tools import preflight
from .models import MODEL_PROFILES, AppConfig, BookMetadata, Plan, Publication
from .outputs.package import (
    PackageArtifact,
    package_archive,
    package_bookplayer,
    package_m4b,
    package_tracks,
)
from .providers.base import STTProvider, TTSProvider
from .qa.evaluate import evaluate_run
from .qa.omissions import (
    DEFAULT_MIN_GAP,
    check_format_durations,
    diagnose_omissions,
    format_find_report,
    load_audio_mono,
    load_transcript_cache,
    robust_transcribe_window,
)
from .qa.scoring import assess_transcript
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
    "extract",
    "check",
    "chapters",
    "compile",
    "status",
    "hooks",
    "plan",
    "adopt",
    "sample",
    "generate",
    "validate",
    "diagnose",
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
      description="Build validated, accessible audiobooks from DRM-free EPUBs or authored manuscripts.",
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
      "init", help="Create an editable audiobook.toml for an EPUB or manuscript."
  )
  init_parser.add_argument(
      "epub",
      type=Path,
      nargs="?",
      help="Source EPUB (omit when --manuscript is used).",
  )
  init_parser.add_argument("--output", type=Path, default=Path("audiobook.toml"))
  init_parser.add_argument(
      "--manuscript",
      type=Path,
      help="Author a book from this markdown tree instead of consuming an EPUB.",
  )
  init_parser.add_argument("--force", action="store_true")
  init_parser.set_defaults(handler=_command_init)

  extract_parser = subparsers.add_parser(
      "extract",
      help="Turn a commercial EPUB into an editable markdown manuscript.",
  )
  extract_parser.add_argument("epub", type=Path)
  _add_config(extract_parser)
  extract_parser.add_argument("--manuscript", type=Path, required=True)
  extract_parser.set_defaults(handler=_command_extract)

  check_parser = subparsers.add_parser(
      "check",
      help="Run objective manuscript checks (headers, word counts, identities).",
  )
  _add_config(check_parser)
  check_parser.add_argument("--chapter", type=Path, help="Check one chapter file.")
  check_parser.add_argument(
      "--write-counts",
      action="store_true",
      help=(
          "Rewrite each chapter's declared words: header to the observed prose "
          "count. Touches only that header line, never the prose. Opt-in."
      ),
  )
  check_parser.add_argument("--json", action="store_true")
  check_parser.set_defaults(handler=_command_check)

  chapters_parser = subparsers.add_parser(
      "chapters",
      help="List manuscript chapters, optionally grouped or filtered by a header key.",
  )
  _add_config(chapters_parser)
  chapters_parser.add_argument(
      "--group-by",
      metavar="KEY",
      help="Group output by this header key (for example pov_id or status).",
  )
  chapters_parser.add_argument(
      "--where",
      metavar="KEY=VALUE",
      action="append",
      default=[],
      help="Keep only chapters whose header KEY equals VALUE. Repeatable.",
  )
  chapters_parser.add_argument("--json", action="store_true")
  chapters_parser.set_defaults(handler=_command_chapters)

  compile_parser = subparsers.add_parser(
      "compile",
      help="Build a compiled markdown edition and an accessible EPUB.",
  )
  _add_config(compile_parser)
  compile_parser.add_argument("--cover", type=Path)
  compile_parser.add_argument("--output", type=Path)
  compile_parser.add_argument(
      "--markdown-only",
      action="store_true",
      help="Write assembled markdown without calling pandoc.",
  )
  compile_parser.add_argument(
      "--if-stale",
      action="store_true",
      help="Skip the EPUB rebuild when prose is not newer.",
  )
  compile_parser.set_defaults(handler=_command_compile)

  status_parser = subparsers.add_parser(
      "status",
      help="Report pending chapter defects and stale EPUB editions.",
  )
  _add_config(status_parser)
  status_parser.set_defaults(handler=_command_status)

  hooks_parser = subparsers.add_parser(
      "hooks",
      help="Install or run portable git lifecycle hooks.",
  )
  hook_commands = hooks_parser.add_subparsers(dest="hooks_command", required=True)
  hook_install = hook_commands.add_parser(
      "install", help="Write pre-commit and post-commit hooks into .git/hooks."
  )
  hook_install.add_argument("--force", action="store_true")
  hook_install.set_defaults(handler=_command_hooks_install)
  hook_pre = hook_commands.add_parser(
      "pre-commit", help="Check staged chapter files (used by the git hook)."
  )
  _add_config(hook_pre)
  hook_pre.set_defaults(handler=_command_hooks_pre_commit)
  hook_post = hook_commands.add_parser(
      "post-commit", help="Rebuild a stale EPUB (used by the git hook)."
  )
  _add_config(hook_post)
  hook_post.set_defaults(handler=_command_hooks_post_commit)

  plan_parser = subparsers.add_parser(
      "plan", help="Extract, normalize, chunk, and estimate without API calls."
  )
  plan_parser.add_argument(
      "epub",
      type=Path,
      nargs="?",
      help="Source EPUB. Omit when project.source = \"manuscript\".",
  )
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
      choices=("none", "elevenlabs", "local"),
      help="Override qa.stt_provider for this evaluation.",
  )
  validate.set_defaults(handler=_command_validate)

  diagnose = subparsers.add_parser(
      "diagnose",
      help="Turn spoken-gate failures into actionable omission and format reports.",
  )
  diagnose_commands = diagnose.add_subparsers(dest="diagnose_command", required=True)

  diagnose_find = diagnose_commands.add_parser(
      "find",
      help="Report real manuscript omissions from expected vs heard text.",
  )
  diagnose_find.add_argument(
      "workspace",
      type=Path,
      nargs="?",
      help="Optional workspace; with --track loads planned text and cached transcript.",
  )
  diagnose_find.add_argument("--plan-id", help="Plan SHA-256 (default: current plan).")
  diagnose_find.add_argument("--run-id")
  diagnose_find.add_argument(
      "--track",
      type=int,
      help="Track number when resolving text/transcript from a workspace.",
  )
  diagnose_find.add_argument(
      "--chunk",
      type=int,
      help="Chunk index within the track (default: whole track when possible).",
  )
  diagnose_find.add_argument(
      "--expected",
      type=Path,
      help="Manuscript / planned spoken text file.",
  )
  diagnose_find.add_argument(
      "--heard",
      type=Path,
      help="ASR transcript text file, or QA transcript JSON with a text field.",
  )
  diagnose_find.add_argument(
      "--audio",
      type=Path,
      help="Track audio for energy-based patch region suggestions.",
  )
  diagnose_find.add_argument(
      "--assembly-map",
      type=Path,
      help="Optional assembly_map JSON (speech_segment entries) for seam localization.",
  )
  diagnose_find.add_argument(
      "--min-gap",
      type=int,
      default=None,
      help="Minimum expected-token run to report (default 5).",
  )
  diagnose_find.add_argument("--json", action="store_true")
  _add_config(diagnose_find)
  diagnose_find.set_defaults(handler=_command_diagnose_find)

  diagnose_seam = diagnose_commands.add_parser(
      "seam",
      help="Re-transcribe a splice window to confirm it reads clean.",
  )
  diagnose_seam.add_argument("audio", type=Path)
  diagnose_seam.add_argument("--start", type=float, required=True)
  diagnose_seam.add_argument("--end", type=float, required=True)
  diagnose_seam.add_argument(
      "--model",
      default="mlx-community/whisper-large-v3-turbo",
  )
  _add_config(diagnose_seam)
  diagnose_seam.set_defaults(handler=_command_diagnose_seam)

  diagnose_formats = diagnose_commands.add_parser(
      "formats",
      help="Check that reference audio, MP3, and M4B chapter durations agree.",
  )
  diagnose_formats.add_argument(
      "--reference",
      type=Path,
      required=True,
      help="Reference WAV or MP3 whose duration is authoritative.",
  )
  diagnose_formats.add_argument("--mp3", type=Path, default=None)
  diagnose_formats.add_argument("--m4b", type=Path, default=None)
  diagnose_formats.add_argument(
      "--chapter-title",
      default=None,
      help="M4B chapter title prefix, e.g. '39.'",
  )
  _add_config(diagnose_formats)
  diagnose_formats.set_defaults(handler=_command_diagnose_formats)

  package = subparsers.add_parser(
      "package", help="Create QA-gated distribution artifacts."
  )
  _add_workspace_selection(package)
  package.add_argument("--run-id")
  package.add_argument(
      "--format",
      action="append",
      choices=("bookplayer", "archive", "tracks", "m4b"),
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
      "build",
      help="Plan, generate, validate, and package from an EPUB or manuscript.",
  )
  build.add_argument(
      "epub",
      type=Path,
      nargs="?",
      help="Source EPUB. Omit when project.source = \"manuscript\".",
  )
  _add_config(build)
  build.add_argument("--workspace", type=Path)
  build.add_argument(
      "--format",
      action="append",
      choices=("bookplayer", "archive", "tracks", "m4b"),
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

  def record(name: str, ok: bool, detail: str, warning: bool = False) -> None:
    item = {"name": name, "ok": ok, "detail": detail}
    if warning:
      item["warning"] = True
    checks.append(item)

  record("python", sys.version_info >= (3, 11), sys.version.split()[0])
  try:
    tools = preflight(config.audio.ffmpeg, config.audio.ffprobe)
    record("ffmpeg", True, tools.ffmpeg)
    record("ffprobe", True, tools.ffprobe)
  except EbookTTSError as exc:
    record("media-tools", False, str(exc))
  record("source", True, config.project.source)
  if config.book.cover:
    cover = Path(config.book.cover)
    record("cover", cover.is_file(), str(cover))
    # A cover with no alt text is a silent accessibility defect: the image
    # ships, the screen reader announces nothing, and no output looks wrong.
    record(
        "cover-alt",
        bool(config.accessibility.cover_alt),
        config.accessibility.cover_alt or "set accessibility.cover_alt to describe the cover",
        warning=not config.accessibility.cover_alt,
    )
  else:
    record("cover", True, "none configured; set book.cover to ship one", warning=True)
  if config.tts.provider == "local":
    try:
      import mlx_audio

      record("mlx-audio", True, getattr(mlx_audio, "__file__", "present"))
    except ImportError:
      record("mlx-audio", False, "install ebook-tts[local] on Apple Silicon")
    wav = Path(config.tts.local.reference_wav)
    txt = Path(config.tts.local.reference_text)
    record("reference-wav", wav.is_file(), str(wav) if config.tts.local.reference_wav else "set tts.local.reference_wav")
    record(
        "reference-text",
        txt.is_file(),
        str(txt) if config.tts.local.reference_text else "set tts.local.reference_text",
    )
  else:
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
      config.tts.voice_id or "set tts.voice_id",
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
  if config.project.source == "manuscript":
    import shutil

    record(
        "pandoc",
        shutil.which("pandoc") is not None,
        shutil.which("pandoc") or "install pandoc to build EPUB editions",
        warning=shutil.which("pandoc") is None,
    )
  if config.tts.output_format == "mp3_44100_192":
    record(
        "output-tier",
        True,
        "192 kbps requires an eligible ElevenLabs subscription tier",
        warning=True,
    )
  if args.json:
    print(json.dumps({"checks": checks}, indent=2))
  else:
    for check in checks:
      marker = "WARN" if check.get("warning") else "OK" if check["ok"] else "FAIL"
      print(f"[{marker:4}] {check['name']}: {check['detail']}")
  blocking = [item for item in checks if not item["ok"] and not item.get("warning")]
  return 0 if not blocking else 1


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
  if args.manuscript is not None:
    root = args.manuscript.expanduser()
    root.mkdir(parents=True, exist_ok=True)
    (root / "chapters").mkdir(exist_ok=True)
    metadata = BookMetadata(title=root.name.replace("-", " ").title(), authors=())
    write_config_template(
        output,
        metadata,
        source="manuscript",
        manuscript_root=str(root),
    )
    print(f"Created {output}")
    print(f"Manuscript root: {root}")
    print("Add chapter files under manuscript/chapters/, then run `ebook-tts check`.")
    return 0
  if args.epub is None:
    raise ConfigError("Provide an EPUB path, or pass --manuscript DIR to author a book.")
  publication = load_publication(args.epub, default_config())
  write_config_template(output, publication.metadata)
  print(f"Created {output}")
  print("Set tts.voice_id, then run `ebook-tts plan`.")
  return 0


def _selected_cover(args: argparse.Namespace, config: AppConfig) -> Path | None:
  """Resolve the cover once, so every edition uses the same image.

  Precedence is an explicit `--cover` flag, then `book.cover` in the config. A
  configured path that does not exist is an error rather than a silent
  coverless build, because a missing cover is invisible in the output.
  """
  override = getattr(args, "cover", None)
  if override is not None:
    candidate = override.expanduser()
    if not candidate.is_file():
      raise ConfigError(f"--cover is not a file: {candidate}")
    return candidate
  if not config.book.cover:
    return None
  candidate = Path(config.book.cover).expanduser()
  if not candidate.is_file():
    raise ConfigError(f"book.cover is not a file: {candidate}")
  return candidate


def _load_publication(args: argparse.Namespace, config: AppConfig) -> Publication:
  epub = getattr(args, "epub", None)
  if epub is not None:
    return load_publication(epub, config)
  if config.project.source == "manuscript":
    return publication_from_manuscript(
        manuscript_root(config), config, cover_path=_selected_cover(args, config)
    )
  raise ConfigError(
      'Provide an EPUB path, or set project.source = "manuscript" in audiobook.toml.'
  )


def _default_workspace(publication: Publication) -> Path:
  return Path(f"{slugify(publication.metadata.title)}.ebook-tts")


def _command_plan(args: argparse.Namespace) -> int:
  config = _load_command_config(args)
  publication = _load_publication(args, config)
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


def _command_extract(args: argparse.Namespace) -> int:
  config = _load_command_config(args)
  publication = extract_manuscript(args.epub, args.manuscript.expanduser(), config)
  print(f"Extracted {len(publication.sections)} chapter(s) into {args.manuscript}")
  print("The markdown tree is now the source of truth. Edit it, then compile and plan.")
  return 0


def _command_check(args: argparse.Namespace) -> int:
  config = _load_command_config(args)
  if args.write_counts:
    if args.chapter is not None:
      raise ConfigError("--write-counts reconciles the whole manuscript; drop --chapter.")
    from .manuscript.check import reconcile_word_counts

    fixes = reconcile_word_counts(manuscript_root(config), config)
    if args.json:
      print(
          json.dumps(
              {
                  "reconciled": len(fixes),
                  "changes": [
                      {
                          "path": item.path,
                          "declared": item.declared,
                          "observed": item.observed,
                      }
                      for item in fixes
                  ],
              },
              indent=2,
          )
      )
    elif fixes:
      for item in fixes:
        print(item.format_text())
      print(f"Reconciled {len(fixes)} declared word count(s).")
    else:
      print("Every declared word count already matches the prose.")
  if args.chapter is not None:
    from .manuscript.check import check_chapter
    from .manuscript.discover import load_chapter

    document = load_chapter(args.chapter.expanduser(), config.manuscript)
    findings = check_chapter(document)
    if args.json:
      print(
          json.dumps(
              {
                  "path": document.path,
                  "ok": not any(item.severity == "error" for item in findings),
                  "diagnostics": [item.format_text() for item in findings],
              },
              indent=2,
          )
      )
    else:
      if not findings:
        print(f"OK {args.chapter}")
      for item in findings:
        print(item.format_text())
    return 0 if not any(item.severity == "error" for item in findings) else 1
  report = check_manuscript(manuscript_root(config), config)
  if args.json:
    print(
        json.dumps(
            {
                "ok": report.ok,
                "chapters": len(report.documents),
                "diagnostics": [item.format_text() for item in report.diagnostics],
            },
            indent=2,
        )
    )
  else:
    print(f"{len(report.documents)} chapter(s)")
    for item in report.diagnostics:
      print(item.format_text())
    if report.ok:
      print("Objective manuscript checks passed.")
  return 0 if report.ok else 1


def _command_chapters(args: argparse.Namespace) -> int:
  """List chapters by any header key.

  The tool stays ignorant of what a key means. `pov_id`, `movement`, and
  `mode` are the author's vocabulary, not this package's, so grouping is
  generic and reports header values verbatim. Reading one narrator's chapters
  consecutively is a human review technique; this command only gathers them.
  """
  config = _load_command_config(args)
  report = check_manuscript(manuscript_root(config), config)
  filters: list[tuple[str, str]] = []
  for clause in args.where:
    key, separator, value = clause.partition("=")
    if not separator or not key.strip():
      raise ConfigError(f"--where expects KEY=VALUE, received {clause!r}.")
    filters.append((key.strip(), value.strip()))

  rows: list[dict[str, Any]] = []
  for document in report.documents:
    header = document.header
    if any(str(header.get(key, "")) != value for key, value in filters):
      continue
    rows.append(
        {
            "chapter": header.get("chapter"),
            "title": header.get("title"),
            "words": header.get("words"),
            "status": header.get("status"),
            "path": document.path,
            "group": str(header.get(args.group_by, "")) if args.group_by else None,
        }
    )
  rows.sort(key=lambda row: (row["chapter"] is None, row["chapter"]))

  if args.json:
    print(json.dumps({"chapters": rows, "total_words": sum(
        row["words"] for row in rows if isinstance(row["words"], int)
    )}, indent=2))
    return 0

  if not rows:
    print("No chapters matched.")
    return 0

  groups: dict[str, list[dict[str, Any]]] = {}
  for row in rows:
    groups.setdefault(row["group"] or "", []).append(row)
  for name in sorted(groups):
    members = groups[name]
    if args.group_by:
      total = sum(row["words"] for row in members if isinstance(row["words"], int))
      label = name or "(unset)"
      print(f"{args.group_by}={label}  {len(members)} chapter(s), {total:,} words")
    for row in members:
      indent = "  " if args.group_by else ""
      print(
          f"{indent}{row['chapter']:>4}  {str(row['words']):>6}w  "
          f"{str(row['status']):<12} {row['title']}"
      )
    if args.group_by:
      print()
  print(
      f"{len(rows)} chapter(s), "
      f"{sum(row['words'] for row in rows if isinstance(row['words'], int)):,} words"
  )
  return 0


def _command_compile(args: argparse.Namespace) -> int:
  config = _load_command_config(args)
  root = manuscript_root(config)
  markdown = root.parent / ".build" / "compiled.md"
  write_compiled_markdown(root, config, markdown)
  print(f"compiled {markdown}")
  if args.markdown_only:
    return 0
  epub = build_epub(
      config,
      cover=_selected_cover(args, config),
      output=args.output,
      if_stale=args.if_stale,
  )
  print(f"wrote {epub} ({epub.stat().st_size:,} bytes)")
  return 0


def _command_status(args: argparse.Namespace) -> int:
  blocks = status_report(_load_command_config(args), cwd=Path.cwd())
  if blocks:
    print("\n\n".join(blocks))
    return 1
  print("No pending manuscript defects; editions are current.")
  return 0


def _command_hooks_install(args: argparse.Namespace) -> int:
  written = install_git_hooks(Path.cwd(), force=args.force)
  for path in written:
    print(f"Installed {path}")
  print("pre-commit checks staged chapters; post-commit rebuilds a stale EPUB.")
  return 0


def _command_hooks_pre_commit(args: argparse.Namespace) -> int:
  return pre_commit_check(_load_command_config(args), cwd=Path.cwd())


def _command_hooks_post_commit(args: argparse.Namespace) -> int:
  result = rebuild_if_stale(_load_command_config(args), cwd=Path.cwd())
  print(f"Edition rebuild: {result}")
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
  if config.tts.provider == "local":
    from .providers.local import LocalTTSProvider

    return LocalTTSProvider(config)
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
        "Generation requires an approved matching voice sample. Run "
        "`ebook-tts sample create`, listen, and approve it; or pass --yes to "
        "explicitly bypass this gate."
    )
  count, characters, chunks = _generation_summary(plan, selected)
  billed = config.tts.provider != "local"
  kind = "paid generation" if billed else "local generation"
  print(
      f"Authorized {kind}: {count} track(s), {characters:,} text "
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
  if config.qa.stt_provider == "local":
    from .providers.local import LocalSTTProvider

    return LocalSTTProvider(model_id=config.qa.stt_model)
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


def _read_text_or_transcript(path: Path) -> str:
  if path.suffix.lower() == ".json":
    try:
      return load_transcript_cache(path)
    except (OSError, ValueError, json.JSONDecodeError):
      payload = json.loads(path.read_text(encoding="utf-8"))
      text = payload.get("text")
      if isinstance(text, str):
        return text
      raise ConfigError(f"JSON file has no transcript text: {path}") from None
  return path.read_text(encoding="utf-8")


def _resolve_diagnose_sources(
    args: argparse.Namespace,
) -> tuple[str, str, str, Path | None, list[dict[str, Any]] | None]:
  """Return expected text, heard text, label, optional audio, optional assembly map."""
  from .workspace.manifests import read_planned_chunk_text, read_planned_section_text

  expected: str | None = None
  heard: str | None = None
  label = "stdin"
  audio_path: Path | None = args.audio.expanduser() if args.audio else None
  assembly_map: list[dict[str, Any]] | None = None

  if args.assembly_map:
    payload = json.loads(args.assembly_map.expanduser().read_text(encoding="utf-8"))
    if isinstance(payload, list):
      assembly_map = [item for item in payload if isinstance(item, dict)]
    elif isinstance(payload, dict) and isinstance(payload.get("assembly_map"), list):
      assembly_map = [
          item for item in payload["assembly_map"] if isinstance(item, dict)
      ]
    else:
      raise ConfigError("--assembly-map must be a JSON list or object with assembly_map.")

  if args.expected:
    expected = _read_text_or_transcript(args.expected.expanduser())
    label = str(args.expected)
  if args.heard:
    heard = _read_text_or_transcript(args.heard.expanduser())

  if args.workspace is not None:
    if args.track is None:
      raise ConfigError("Workspace diagnose find requires --track.")
    plan = _load_selected_plan(args)
    run = load_run(plan, args.run_id)
    sections = plan.manifest.get("sections")
    if not isinstance(sections, list):
      raise ConfigError("Plan sections are invalid.")
    section = next(
        (
            item
            for item in sections
            if isinstance(item, dict) and item.get("track_number") == args.track
        ),
        None,
    )
    if section is None:
      raise ConfigError(f"Track {args.track} is not in the plan.")
    stem = str(section.get("output_stem") or "")
    if args.chunk is not None:
      chunks = section.get("chunks")
      if not isinstance(chunks, list):
        raise ConfigError(f"Track {args.track} has no planned chunks.")
      planned = next(
          (
              item
              for item in chunks
              if isinstance(item, dict) and item.get("index") == args.chunk
          ),
          None,
      )
      if planned is None:
        raise ConfigError(f"Track {args.track} has no chunk {args.chunk}.")
      if expected is None:
        expected = read_planned_chunk_text(
            plan, planned, label=f"Track {args.track} chunk {args.chunk}"
        )
      label = f"track {args.track} chunk {args.chunk}"
      transcript_name = f"chunk_{args.chunk:04d}.json"
    else:
      if expected is None:
        expected = read_planned_section_text(plan, section)
      label = f"track {args.track} ({stem})"
      transcript_name = None
    if heard is None:
      qa_root = run.run_path.parent / "qa"
      if not qa_root.is_dir():
        raise ConfigError(
            f"No QA directory under {run.run_path.parent}; run validate with STT first "
            "or pass --heard."
        )
      candidates = sorted(qa_root.glob(f"*/transcripts/{stem}"))
      if not candidates:
        raise ConfigError(
            f"No cached transcripts for {stem}; run validate with STT or pass --heard."
        )
      transcript_dir = candidates[-1]
      if transcript_name is None:
        parts: list[str] = []
        for path in sorted(transcript_dir.glob("chunk_*.json")):
          parts.append(load_transcript_cache(path))
        if not parts:
          raise ConfigError(f"No chunk transcripts in {transcript_dir}")
        heard = " ".join(parts)
      else:
        cache = transcript_dir / transcript_name
        if not cache.is_file():
          raise ConfigError(f"Missing transcript cache: {cache}")
        heard = load_transcript_cache(cache)
    if audio_path is None:
      final = run.run_path.parent / "audio" / f"{stem}.mp3"
      if final.is_file():
        audio_path = final

  if expected is None or heard is None:
    raise ConfigError(
        "diagnose find requires --expected and --heard, or a workspace with "
        "--track (and cached transcripts)."
    )
  return expected, heard, label, audio_path, assembly_map


def _command_diagnose_find(args: argparse.Namespace) -> int:
  expected, heard, label, audio_path, assembly_map = _resolve_diagnose_sources(args)
  min_gap = DEFAULT_MIN_GAP if args.min_gap is None else int(args.min_gap)
  audio = None
  rate = 44100
  if audio_path is not None:
    config = _load_command_config(args)
    tools = preflight(config.audio.ffmpeg, config.audio.ffprobe)
    audio, rate = load_audio_mono(audio_path, ffmpeg=tools.ffmpeg)
  gaps = diagnose_omissions(
      expected,
      heard,
      min_gap=min_gap,
      audio=audio,
      sample_rate=rate,
      assembly_map=assembly_map,
  )
  gate = assess_transcript(expected, heard)
  if args.json:
    print(
        json.dumps(
            {
                "label": label,
                "min_gap": min_gap,
                "gate": {
                    "passed": gate["passed"],
                    "wer": gate["wer"],
                    "coverage": gate["coverage"],
                    "max_expected_gap": gate["max_expected_gap"],
                },
                "omissions": [gap.to_dict() for gap in gaps],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
  else:
    print(
        format_find_report(
            gaps,
            label=label,
            gate={
                "passed": gate["passed"],
                "wer": gate["wer"],
                "coverage": gate["coverage"],
                "max_expected_gap": gate["max_expected_gap"],
            },
            min_gap=min_gap,
        ),
        end="",
    )
  return 1 if gaps else 0


def _command_diagnose_seam(args: argparse.Namespace) -> int:
  config = _load_command_config(args)
  tools = preflight(config.audio.ffmpeg, config.audio.ffprobe)
  audio, rate = load_audio_mono(args.audio.expanduser(), ffmpeg=tools.ffmpeg)
  if args.end <= args.start:
    raise ConfigError("--end must be greater than --start.")
  text = robust_transcribe_window(
      audio, rate, args.start, args.end, model=args.model
  )
  print(f"seam [{args.start:.2f}-{args.end:.2f}s]:")
  print(f"  {text}")
  return 0


def _command_diagnose_formats(args: argparse.Namespace) -> int:
  config = _load_command_config(args)
  tools = preflight(config.audio.ffmpeg, config.audio.ffprobe)
  reference = args.reference.expanduser()
  from .media.tools import probe_audio

  ref_info = probe_audio(reference, tools.ffprobe)
  checks = check_format_durations(
      reference_path=reference,
      reference_duration=ref_info.duration_seconds,
      mp3_path=args.mp3.expanduser() if args.mp3 else None,
      m4b_path=args.m4b.expanduser() if args.m4b else None,
      chapter_title=args.chapter_title,
      ffprobe=tools.ffprobe,
  )
  ok = True
  for check in checks:
    print(f"{check.label:9}: {check.detail}")
    ok = ok and check.ok
  return 0 if ok else 1


def _package_formats(
    *,
    plan: Plan,
    run: GenerationRun,
    formats: list[str] | None,
    output_directory: Path | None,
    allow_unvalidated: bool,
    allow_failed_qa: bool,
    chapter_gap_ms: float = 2500.0,
) -> list[PackageArtifact]:
  output = (output_directory or (plan.workspace / "dist")).expanduser().resolve()
  selected = formats or ["bookplayer", "archive"]
  artifacts: list[PackageArtifact] = []
  for name in selected:
    if name == "m4b":
      artifact = package_m4b(
          plan=plan,
          run=run,
          output_directory=output,
          allow_unvalidated=allow_unvalidated,
          allow_failed_qa=allow_failed_qa,
          chapter_gap_ms=chapter_gap_ms,
      )
    elif name == "bookplayer":
      artifact = package_bookplayer(
          plan=plan,
          run=run,
          output_directory=output,
          allow_unvalidated=allow_unvalidated,
          allow_failed_qa=allow_failed_qa,
      )
    elif name == "archive":
      artifact = package_archive(
          plan=plan,
          run=run,
          output_directory=output,
          allow_unvalidated=allow_unvalidated,
          allow_failed_qa=allow_failed_qa,
      )
    elif name == "tracks":
      artifact = package_tracks(
          plan=plan,
          run=run,
          output_directory=output,
          allow_unvalidated=allow_unvalidated,
          allow_failed_qa=allow_failed_qa,
      )
    else:
      raise ConfigError(f"Unsupported package format: {name}")
    artifacts.append(artifact)
    print(f"{name}: {artifact.path} ({artifact.bytes:,} bytes)")
  return artifacts


def _command_package(args: argparse.Namespace) -> int:
  plan = _load_selected_plan(args)
  config = _load_command_config(args)
  _package_formats(
      plan=plan,
      run=load_run(plan, args.run_id),
      formats=args.formats,
      output_directory=args.output_dir,
      allow_unvalidated=args.allow_unvalidated,
      allow_failed_qa=args.allow_failed_qa,
      chapter_gap_ms=config.audio.m4b_chapter_gap_ms,
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
  publication = _load_publication(args, config)
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
      chapter_gap_ms=config.audio.m4b_chapter_gap_ms,
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
