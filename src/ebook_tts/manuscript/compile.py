"""Assemble a manuscript into narratable publication text."""

from __future__ import annotations

from pathlib import Path

from ..errors import ManuscriptError
from ..models import AppConfig, BookMetadata, Publication, Section
from ..text.normalize import normalize_narration
from ..utils import canonical_json, sha256_bytes, sha256_file, sha256_text, track_stem
from .check import check_manuscript
from .discover import discover_chapter_files
from .document import require_parsed


def manuscript_root(config: AppConfig, *, cwd: Path | None = None) -> Path:
  base = cwd or Path.cwd()
  root = Path(config.manuscript.root)
  return root if root.is_absolute() else (base / root).resolve()


def manuscript_identity(root: Path, config: AppConfig) -> str:
  """Hash every chapter, plus optional front/back matter, in discovery order."""
  payload: list[dict[str, str]] = []
  for path in discover_chapter_files(root, config.manuscript):
    payload.append(
        {
            "path": path.relative_to(root).as_posix(),
            "sha256": sha256_file(path),
        }
    )
  for name in (config.manuscript.front_matter, config.manuscript.back_matter):
    extra = root / name
    if extra.is_file():
      payload.append({"path": name, "sha256": sha256_file(extra)})
  return sha256_text(canonical_json({"kind": "ebook-tts-manuscript", "files": payload}))


def assemble_manuscript(root: Path, config: AppConfig) -> str:
  """Return the compiled Markdown edition, excluding restricted headers."""
  report = check_manuscript(root, config)
  errors = [item for item in report.diagnostics if item.severity == "error"]
  if errors:
    raise ManuscriptError(
        "Manuscript has objective defects:\n"
        + "\n".join(item.format_text() for item in errors)
    )
  blocks: list[str] = []
  front = root / config.manuscript.front_matter
  if front.is_file():
    blocks.append(front.read_text(encoding="utf-8").lstrip("\ufeff").strip())
    blocks.append("")
  ordered = sorted(
      report.documents,
      key=lambda document: int(document.header["chapter"]),
  )
  for document in ordered:
    require_parsed(document)
    title = str(document.header["title"]).strip()
    number = int(document.header["chapter"])
    body = (document.prose_body or "").strip()
    if not body:
      raise ManuscriptError(f"Empty prose body: {document.path}")
    blocks.append(f"# Chapter {number}")
    blocks.append("")
    blocks.append(f"## {title}")
    blocks.append("")
    blocks.append(body)
    blocks.append("")
  back = root / config.manuscript.back_matter
  if back.is_file():
    blocks.append(back.read_text(encoding="utf-8").lstrip("\ufeff").strip())
    blocks.append("")
  return "\n".join(blocks).strip() + "\n"


def publication_from_manuscript(
    root: Path,
    config: AppConfig,
    *,
    cover_path: Path | None = None,
) -> Publication:
  """Build a Publication from markdown so planning does not require Pandoc."""
  report = check_manuscript(root, config)
  errors = [item for item in report.diagnostics if item.severity == "error"]
  if errors:
    raise ManuscriptError(
        "Manuscript has objective defects:\n"
        + "\n".join(item.format_text() for item in errors)
    )
  ordered = sorted(
      report.documents,
      key=lambda document: int(document.header["chapter"]),
  )
  metadata = BookMetadata(
      title=config.book.title or root.name,
      authors=config.book.authors,
      language=config.book.language,
  )
  sections: list[Section] = []
  for index, document in enumerate(ordered, start=1):
    require_parsed(document)
    title = str(document.header["title"]).strip()
    number = int(document.header["chapter"])
    body = (document.prose_body or "").strip() + "\n"
    if config.sections.announce_titles:
      heading = f"Chapter {number}"
      if body.splitlines()[0].casefold() != title.casefold():
        body = f"{heading}\n\n{title}\n\n{body}"
    text = normalize_narration(body, config.normalization)
    relative = Path(document.path)
    try:
      href = relative.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
      href = relative.name
    source_bytes = Path(document.path).read_bytes()
    sections.append(
        Section(
            track_number=index,
            title=title,
            output_stem=track_stem(index, title),
            source_href=href,
            source_fragment=None,
            source_sha256=sha256_bytes(source_bytes),
            text=text,
            text_sha256=sha256_text(text),
            chapter_number=number,
        )
    )
  cover_bytes = None
  cover_type = None
  cover_extension = None
  if cover_path is not None and cover_path.is_file():
    cover_bytes = cover_path.read_bytes()
    suffix = cover_path.suffix.lower()
    cover_extension = suffix if suffix else ".img"
    cover_type = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }.get(suffix, "application/octet-stream")
  return Publication(
      source_path=root,
      source_sha256=manuscript_identity(root, config),
      metadata=metadata,
      sections=tuple(sections),
      cover_bytes=cover_bytes,
      cover_media_type=cover_type,
      cover_extension=cover_extension,
  )


def write_compiled_markdown(root: Path, config: AppConfig, output: Path) -> Path:
  output.parent.mkdir(parents=True, exist_ok=True)
  output.write_text(assemble_manuscript(root, config), encoding="utf-8")
  return output
