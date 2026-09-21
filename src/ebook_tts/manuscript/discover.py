"""Locate and load chapter files under a manuscript root."""

from __future__ import annotations

from pathlib import Path

from ..errors import ManuscriptError
from ..models import ManuscriptConfig
from .document import ChapterDocument, parse_chapter_document


def chapters_directory(root: Path, config: ManuscriptConfig) -> Path:
  return root / config.chapters


def discover_chapter_files(root: Path, config: ManuscriptConfig) -> tuple[Path, ...]:
  directory = chapters_directory(root, config)
  if not directory.is_dir():
    raise ManuscriptError(f"Manuscript chapters directory is missing: {directory}")
  files = [
      path
      for path in sorted(directory.rglob("*.md"))
      if path.is_file() and not path.is_symlink()
  ]
  if not files:
    raise ManuscriptError(f"No chapter markdown files under {directory}")
  return tuple(files)


def load_chapter(path: Path, config: ManuscriptConfig) -> ChapterDocument:
  text = path.read_text(encoding="utf-8")
  return parse_chapter_document(
      text,
      item=path.as_posix(),
      required_keys=config.required_header_keys,
      extra_header_keys=config.extra_header_keys,
  )
