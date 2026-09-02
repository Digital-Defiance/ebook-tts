"""Versioned immutable plan creation and verification."""

from __future__ import annotations

import re
from dataclasses import asdict
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from ..errors import WorkspaceError
from ..models import (
    CHUNKER_VERSION,
    EXTRACTION_VERSION,
    MANIFEST_VERSION,
    NORMALIZATION_VERSION,
    AppConfig,
    Chunk,
    Plan,
    PreparedSection,
    Publication,
)
from ..text.chunk import chunk_text
from ..utils import (
    atomic_write_bytes,
    atomic_write_json,
    atomic_write_text,
    canonical_json,
    load_json,
    sha256_bytes,
    sha256_file,
    sha256_text,
    utc_now,
)
from .locking import WorkspaceLock


_MARKER = ".ebook-tts-workspace.json"
_REGISTRY = "workspace.json"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_STEM = re.compile(r"^[0-9]{3}_[A-Za-z0-9][A-Za-z0-9_-]*$")


def _config_record(config: AppConfig) -> dict[str, Any]:
  """Record only settings that affect extraction or request boundaries."""
  return {
      "book": asdict(config.book),
      "sections": asdict(config.sections),
      "normalization": [asdict(rule) for rule in config.normalization],
      "planning": {
          "model_id": config.tts.model_id,
          "max_characters": config.tts.max_characters,
      },
  }


def _prepared(publication: Publication, config: AppConfig) -> tuple[PreparedSection, ...]:
  result: list[PreparedSection] = []
  for section in publication.sections:
    texts = chunk_text(section.text, config.tts.max_characters)
    chunks = tuple(
        Chunk(index=index, text=text, text_sha256=sha256_text(text))
        for index, text in enumerate(texts, start=1)
    )
    result.append(PreparedSection(section=section, chunks=chunks))
  return tuple(result)


def _draft_manifest(
    publication: Publication,
    config: AppConfig,
    prepared: tuple[PreparedSection, ...],
) -> dict[str, Any]:
  sections: list[dict[str, Any]] = []
  for item in prepared:
    section = item.section
    section_dir = f"chunks/{section.output_stem}"
    sections.append(
        {
            "track_number": section.track_number,
            "chapter_number": section.chapter_number,
            "title": section.title,
            "output_stem": section.output_stem,
            "source_href": section.source_href,
            "source_fragment": section.source_fragment,
            "source_sha256": section.source_sha256,
            "text_sha256": section.text_sha256,
            "characters": len(section.text),
            "text_file": f"text/{section.output_stem}.txt",
            "chunk_count": len(item.chunks),
            "chunks": [
                {
                    "index": chunk.index,
                    "characters": len(chunk.text),
                    "text_sha256": chunk.text_sha256,
                    "text_file": f"{section_dir}/chunk_{chunk.index:04d}.txt",
                }
                for chunk in item.chunks
            ],
        }
    )
  cover = None
  if publication.cover_bytes is not None:
    cover = {
        "file": f"cover{publication.cover_extension or '.img'}",
        "media_type": publication.cover_media_type,
        "sha256": sha256_bytes(publication.cover_bytes),
        "bytes": len(publication.cover_bytes),
    }
  return {
      "manifest_version": MANIFEST_VERSION,
      "kind": "ebook-tts-plan",
      "versions": {
          "extraction": EXTRACTION_VERSION,
          "normalization": NORMALIZATION_VERSION,
          "chunker": CHUNKER_VERSION,
      },
      "source": {
          "filename": publication.source_path.name,
          "sha256": publication.source_sha256,
      },
      "book": asdict(publication.metadata),
      "cover": cover,
      "warnings": list(publication.warnings),
      "configuration": _config_record(config),
      "track_count": len(prepared),
      "characters": sum(len(item.section.text) for item in prepared),
      "chunk_count": sum(len(item.chunks) for item in prepared),
      "sections": sections,
  }


def _initialize_workspace(workspace: Path) -> None:
  workspace.mkdir(parents=True, exist_ok=True)
  marker = workspace / _MARKER
  existing = [path for path in workspace.iterdir() if path.name != ".lock"]
  if not marker.exists() and existing:
    raise WorkspaceError(
        f"Refusing to manage non-empty directory without {_MARKER}: {workspace}"
    )
  if not marker.exists():
    atomic_write_json(
        marker,
        {
            "manifest_version": MANIFEST_VERSION,
            "kind": "ebook-tts-workspace",
            "created_at": utc_now(),
        },
    )


def _required_int(value: object, label: str, *, minimum: int = 0) -> int:
  if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
    raise WorkspaceError(f"{label} must be an integer of at least {minimum}.")
  return value


def _required_sha256(value: object, label: str) -> str:
  if not isinstance(value, str) or not _SHA256.fullmatch(value):
    raise WorkspaceError(f"{label} must be a lowercase SHA-256 value.")
  return value


def _artifact_path(plan: Plan, value: object, label: str) -> Path:
  if not isinstance(value, str) or not value or "\\" in value:
    raise WorkspaceError(f"{label} must be a portable relative artifact path.")
  pure = PurePosixPath(value)
  if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
    raise WorkspaceError(f"{label} contains an unsafe path component: {value!r}.")
  root = plan_directory(plan).resolve(strict=True)
  candidate = root.joinpath(*pure.parts)
  current = root
  for part in pure.parts:
    current = current / part
    if current.is_symlink():
      raise WorkspaceError(f"{label} traverses a symbolic link: {current}.")
  try:
    resolved = candidate.resolve(strict=True)
    resolved.relative_to(root)
  except (OSError, ValueError) as exc:
    raise WorkspaceError(f"{label} is missing or escapes its plan directory.") from exc
  if not resolved.is_file():
    raise WorkspaceError(f"{label} is not a regular file: {resolved}.")
  return resolved


def _read_utf8(path: Path, label: str, *, maximum_bytes: int) -> str:
  try:
    size = path.stat().st_size
    if size > maximum_bytes:
      raise WorkspaceError(
          f"{label} exceeds its bounded size ({size:,} > {maximum_bytes:,} bytes)."
      )
    return path.read_bytes().decode("utf-8")
  except UnicodeDecodeError as exc:
    raise WorkspaceError(f"{label} is not strict UTF-8: {path}.") from exc
  except OSError as exc:
    raise WorkspaceError(f"Could not read {label} at {path}: {exc}") from exc


def _verify_text(value: str, record: Mapping[str, Any], label: str) -> str:
  expected_characters = _required_int(record.get("characters"), f"{label}.characters")
  expected_sha = _required_sha256(record.get("text_sha256"), f"{label}.text_sha256")
  if len(value) != expected_characters or sha256_text(value) != expected_sha:
    raise WorkspaceError(f"{label} text hash or character count mismatch.")
  return value


def read_planned_section_text(plan: Plan, section: Mapping[str, Any]) -> str:
  """Read one full-track artifact and verify its exact immutable identity."""
  label = f"Track {section.get('track_number')} full text"
  characters = _required_int(section.get("characters"), f"{label}.characters")
  path = _artifact_path(plan, section.get("text_file"), f"{label}.text_file")
  value = _read_utf8(path, label, maximum_bytes=max(1024, characters * 4))
  return _verify_text(value, section, label)


def read_planned_chunk_text(plan: Plan, chunk: Mapping[str, Any], *, label: str) -> str:
  """Read one chunk, remove its single storage LF, and verify request text."""
  characters = _required_int(chunk.get("characters"), f"{label}.characters")
  path = _artifact_path(plan, chunk.get("text_file"), f"{label}.text_file")
  stored = _read_utf8(path, label, maximum_bytes=max(1024, characters * 4 + 1))
  if not stored.endswith("\n"):
    raise WorkspaceError(f"{label} is missing its required storage newline.")
  return _verify_text(stored[:-1], chunk, label)


def verified_cover_path(plan: Plan) -> Path | None:
  """Return verified cover artwork, or None when the immutable plan has none."""
  cover = plan.manifest.get("cover")
  if cover is None:
    return None
  if not isinstance(cover, dict):
    raise WorkspaceError("Plan cover record is invalid.")
  path = _artifact_path(plan, cover.get("file"), "Plan cover.file")
  expected_bytes = _required_int(cover.get("bytes"), "Plan cover.bytes", minimum=1)
  expected_sha = _required_sha256(cover.get("sha256"), "Plan cover.sha256")
  if path.stat().st_size != expected_bytes or sha256_file(path) != expected_sha:
    raise WorkspaceError("Plan cover artwork hash or byte count mismatch.")
  return path


def verify_plan_manifest(manifest: dict[str, Any], source: Path) -> None:
  """Verify a plan's schema marker and content-derived ID."""
  if manifest.get("manifest_version") != MANIFEST_VERSION:
    raise WorkspaceError(f"Unsupported plan manifest version in {source}.")
  if manifest.get("kind") != "ebook-tts-plan":
    raise WorkspaceError(f"Not an ebook-tts plan: {source}")
  plan_id = _required_sha256(manifest.get("plan_sha256"), f"Plan {source}")
  unsigned = dict(manifest)
  unsigned.pop("plan_sha256", None)
  unsigned.pop("created_at", None)
  calculated = sha256_text(canonical_json(unsigned))
  if calculated != plan_id:
    raise WorkspaceError(
        f"Immutable plan fingerprint mismatch in {source}; expected {plan_id}, "
        f"calculated {calculated}."
    )


def verify_plan_artifacts(plan: Plan) -> None:
  """Fail closed unless every plan-referenced artifact matches its manifest."""
  verify_plan_manifest(plan.manifest, plan.plan_path)
  if plan.plan_path.is_symlink() or plan.plan_path.parent.name != plan.plan_id:
    raise WorkspaceError(f"Plan path identity is invalid: {plan.plan_path}")
  sections = plan.manifest.get("sections")
  if not isinstance(sections, list) or not sections:
    raise WorkspaceError("Plan must contain at least one section.")
  if _required_int(plan.manifest.get("track_count"), "Plan track_count", minimum=1) != len(sections):
    raise WorkspaceError("Plan track count is inconsistent.")

  seen_stems: set[str] = set()
  total_characters = 0
  total_chunks = 0
  for expected_track, section_value in enumerate(sections, start=1):
    if not isinstance(section_value, dict):
      raise WorkspaceError(f"Plan section {expected_track} is not an object.")
    section = section_value
    track = _required_int(section.get("track_number"), "Plan section track_number", minimum=1)
    if track != expected_track:
      raise WorkspaceError("Plan track numbers must be ordered and contiguous from 1.")
    title = section.get("title")
    stem = section.get("output_stem")
    if not isinstance(title, str) or not title:
      raise WorkspaceError(f"Track {track} title is invalid.")
    if (
        not isinstance(stem, str)
        or not _SAFE_STEM.fullmatch(stem)
        or not stem.startswith(f"{track:03d}_")
        or stem in seen_stems
    ):
      raise WorkspaceError(f"Track {track} output_stem is unsafe or duplicated.")
    seen_stems.add(stem)
    _required_sha256(section.get("source_sha256"), f"Track {track} source_sha256")
    full_text = read_planned_section_text(plan, section)
    chunks = section.get("chunks")
    if not isinstance(chunks, list) or not chunks:
      raise WorkspaceError(f"Track {track} must contain at least one chunk.")
    if _required_int(section.get("chunk_count"), f"Track {track} chunk_count", minimum=1) != len(chunks):
      raise WorkspaceError(f"Track {track} chunk count is inconsistent.")
    chunk_texts: list[str] = []
    for expected_chunk, chunk_value in enumerate(chunks, start=1):
      if not isinstance(chunk_value, dict):
        raise WorkspaceError(f"Track {track}, chunk {expected_chunk} is not an object.")
      index = _required_int(
          chunk_value.get("index"),
          f"Track {track}, chunk index",
          minimum=1,
      )
      if index != expected_chunk:
        raise WorkspaceError(f"Track {track} chunk indices must be ordered from 1.")
      chunk_texts.append(
          read_planned_chunk_text(
              plan,
              chunk_value,
              label=f"Track {track}, chunk {index}",
          )
      )
    source_tokens = re.findall(r"\S+", full_text)
    chunk_tokens = [token for text in chunk_texts for token in re.findall(r"\S+", text)]
    if source_tokens != chunk_tokens and "".join(source_tokens) != "".join(chunk_tokens):
      raise WorkspaceError(f"Track {track} chunks do not reproduce its full text.")
    total_characters += _required_int(section.get("characters"), f"Track {track} characters")
    total_chunks += len(chunks)

  if _required_int(plan.manifest.get("characters"), "Plan characters") != total_characters:
    raise WorkspaceError("Plan aggregate character count is inconsistent.")
  if _required_int(plan.manifest.get("chunk_count"), "Plan chunk_count", minimum=1) != total_chunks:
    raise WorkspaceError("Plan aggregate chunk count is inconsistent.")
  verified_cover_path(plan)


def create_plan(publication: Publication, config: AppConfig, workspace: Path) -> Plan:
  """Create or reuse an immutable text/chunk plan."""
  workspace = workspace.expanduser().resolve()
  with WorkspaceLock(workspace):
    _initialize_workspace(workspace)
    prepared = _prepared(publication, config)
    draft = _draft_manifest(publication, config, prepared)
    plan_id = sha256_text(canonical_json(draft))
    plan_dir = workspace / "plans" / plan_id
    plan_path = plan_dir / "plan.json"

    if plan_path.exists():
      existing = load_json(plan_path)
      verify_plan_manifest(existing, plan_path)
      if existing.get("plan_sha256") != plan_id:
        raise WorkspaceError(f"Existing plan ID mismatch in {plan_path}.")
    else:
      plan_dir.mkdir(parents=True, exist_ok=False)
      for item in prepared:
        section = item.section
        atomic_write_text(
            plan_dir / "text" / f"{section.output_stem}.txt",
            section.text,
        )
        for chunk in item.chunks:
          atomic_write_text(
              plan_dir
              / "chunks"
              / section.output_stem
              / f"chunk_{chunk.index:04d}.txt",
              chunk.text + "\n",
          )
      if publication.cover_bytes is not None and draft["cover"]:
        atomic_write_bytes(
            plan_dir / draft["cover"]["file"],
            publication.cover_bytes,
        )
      manifest = dict(draft)
      manifest["plan_sha256"] = plan_id
      manifest["created_at"] = utc_now()
      atomic_write_json(plan_path, manifest)

    plan = Plan(
        workspace=workspace,
        plan_path=plan_path,
        plan_id=plan_id,
        manifest=load_json(plan_path),
    )
    verify_plan_artifacts(plan)
    registry_path = workspace / _REGISTRY
    registry = load_json(registry_path) if registry_path.exists() else {
        "manifest_version": MANIFEST_VERSION,
        "kind": "ebook-tts-workspace-registry",
        "plans": [],
    }
    plans = registry.get("plans")
    if not isinstance(plans, list):
      raise WorkspaceError(f"Invalid plans registry in {registry_path}.")
    if plan_id not in plans:
      plans.append(plan_id)
    registry["current_plan"] = plan_id
    registry["updated_at"] = utc_now()
    atomic_write_json(registry_path, registry)
    return plan


def load_plan(workspace: Path, plan_id: str | None = None) -> Plan:
  """Load and strictly verify the selected immutable plan and all artifacts."""
  workspace = workspace.expanduser().resolve()
  marker = workspace / _MARKER
  if marker.is_symlink() or not marker.is_file():
    raise WorkspaceError(f"Not an ebook-tts workspace: {workspace}")
  marker_record = load_json(marker)
  if (
      marker_record.get("manifest_version") != MANIFEST_VERSION
      or marker_record.get("kind") != "ebook-tts-workspace"
  ):
    raise WorkspaceError(f"Invalid workspace marker: {marker}")
  registry_path = workspace / _REGISTRY
  if registry_path.is_symlink():
    raise WorkspaceError(f"Workspace registry must not be a symbolic link: {registry_path}")
  registry = load_json(registry_path)
  if (
      registry.get("manifest_version") != MANIFEST_VERSION
      or registry.get("kind") != "ebook-tts-workspace-registry"
  ):
    raise WorkspaceError(f"Invalid workspace registry: {registry_path}")
  plans = registry.get("plans")
  if not isinstance(plans, list) or any(
      not isinstance(value, str) or not _SHA256.fullmatch(value) for value in plans
  ):
    raise WorkspaceError(f"Workspace plan registry is invalid: {registry_path}")
  selected = plan_id or registry.get("current_plan")
  if not isinstance(selected, str) or not _SHA256.fullmatch(selected):
    raise WorkspaceError(f"Workspace has no valid current plan: {workspace}")
  if selected not in plans:
    raise WorkspaceError(f"Selected plan is not registered in {registry_path}.")
  plan_path = workspace / "plans" / selected / "plan.json"
  if plan_path.is_symlink():
    raise WorkspaceError(f"Plan manifest must not be a symbolic link: {plan_path}")
  manifest = load_json(plan_path)
  verify_plan_manifest(manifest, plan_path)
  if manifest["plan_sha256"] != selected:
    raise WorkspaceError(f"Plan directory and fingerprint disagree: {plan_path}")
  plan = Plan(workspace, plan_path, selected, manifest)
  verify_plan_artifacts(plan)
  return plan


def plan_directory(plan: Plan) -> Path:
  return plan.plan_path.parent


def read_planned_text(plan: Plan, relative_path: str) -> str:
  """Read a plan text artifact safely, removing one storage LF when present."""
  path = _artifact_path(plan, relative_path, "Plan text artifact")
  try:
    return path.read_text(encoding="utf-8").removesuffix("\n")
  except (OSError, UnicodeDecodeError) as exc:
    raise WorkspaceError(f"Could not read plan artifact {path}: {exc}") from exc
