from __future__ import annotations

import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from ebook_tts.config import default_config
from ebook_tts.editions.currency import editions_stale
from ebook_tts.editions.epub import build_epub
from ebook_tts.manuscript.document import count_prose_words, render_chapter_markdown


pytestmark = pytest.mark.skipif(
    shutil.which("pandoc") is None,
    reason="pandoc is required for EPUB edition builds",
)


def _authored(tmp_path: Path) -> tuple[Path, object]:
  root = tmp_path / "manuscript"
  chapters = root / "chapters"
  chapters.mkdir(parents=True)
  (root / "front-matter.md").write_text("# Authored Book\n\n", encoding="utf-8")
  (root / "back-matter.md").write_text("", encoding="utf-8")
  body = (
      "The apparatus receives tokens from the page and nothing else is required "
      "for this short chapter to compile into a reader EPUB.\n"
  )
  (chapters / "001-start.md").write_text(
      render_chapter_markdown(
          header={
              "chapter": 1,
              "title": "Start",
              "words": count_prose_words(body),
              "status": "draft",
          },
          prose=body,
      ),
      encoding="utf-8",
  )
  base = default_config()
  config = replace(
      base,
      project=replace(base.project, source="manuscript"),
      manuscript=replace(
          base.manuscript,
          root=str(root),
          epub=str(tmp_path / "dist" / "book.epub"),
      ),
      book=replace(base.book, title="Authored Book", authors=("Example Author",)),
      accessibility=replace(
          base.accessibility,
          certified_by="Test Suite",
          cover_alt="Synthetic cover",
          summary="A short synthetic accessibility summary.",
      ),
  )
  return root, config


def test_build_epub_writes_accessible_edition(tmp_path: Path) -> None:
  root, config = _authored(tmp_path)
  epub = build_epub(config, cwd=tmp_path)
  assert epub.is_file()
  assert epub.stat().st_size > 0
  assert not editions_stale(root, epub, config.manuscript)
  # Pandoc staging files must not remain beside the final edition.
  assert not list(epub.parent.glob("*.staging.epub"))
  assert not list(epub.parent.glob("*.a11y.epub"))

  import zipfile

  with zipfile.ZipFile(epub) as archive:
    opf_name = next(name for name in archive.namelist() if name.endswith(".opf"))
    opf = archive.read(opf_name).decode("utf-8")
  assert "schema:accessModeSufficient" in opf
  assert "a11y:certifiedBy" in opf
  assert "Test Suite" in opf


def test_build_epub_if_stale_skips_when_current(tmp_path: Path) -> None:
  root, config = _authored(tmp_path)
  first = build_epub(config, cwd=tmp_path)
  mtime = first.stat().st_mtime
  second = build_epub(config, cwd=tmp_path, if_stale=True)
  assert second == first
  assert second.stat().st_mtime == mtime
