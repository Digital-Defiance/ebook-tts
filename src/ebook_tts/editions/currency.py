"""Detect when a derived EPUB is older than its manuscript."""

from __future__ import annotations

from pathlib import Path

from ..models import ManuscriptConfig


def newest_prose_mtime(root: Path, config: ManuscriptConfig) -> float:
  newest = 0.0
  chapters = root / config.chapters
  if chapters.is_dir():
    for path in chapters.rglob("*.md"):
      newest = max(newest, path.stat().st_mtime)
  for name in (config.front_matter, config.back_matter):
    extra = root / name
    if extra.is_file():
      newest = max(newest, extra.stat().st_mtime)
  return newest


def editions_stale(root: Path, epub: Path, config: ManuscriptConfig) -> bool:
  if not epub.is_file():
    return True
  prose = newest_prose_mtime(root, config)
  return prose > epub.stat().st_mtime
