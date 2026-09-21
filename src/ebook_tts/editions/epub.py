"""Build a reader EPUB from compiled manuscript markdown."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from ..errors import ManuscriptError
from ..models import AppConfig
from ..manuscript.compile import manuscript_root, write_compiled_markdown
from .accessibility import apply_accessibility
from .currency import editions_stale


def _require_pandoc() -> str:
  path = shutil.which("pandoc")
  if path is None:
    raise ManuscriptError("pandoc is required to build an EPUB edition.")
  return path


def build_epub(
    config: AppConfig,
    *,
    cwd: Path | None = None,
    cover: Path | None = None,
    output: Path | None = None,
    if_stale: bool = False,
) -> Path:
  """Compile markdown and write an accessible EPUB3 edition."""
  root = manuscript_root(config, cwd=cwd)
  epub_path = output or Path(config.manuscript.epub)
  if not epub_path.is_absolute():
    epub_path = (cwd or Path.cwd()) / epub_path
  if if_stale and not editions_stale(root, epub_path, config.manuscript):
    return epub_path
  compiled = Path(tempfile.mkdtemp(prefix="ebook-tts-edition-")) / "compiled.md"
  try:
    write_compiled_markdown(root, config, compiled)
    pandoc = _require_pandoc()
    epub_path.parent.mkdir(parents=True, exist_ok=True)
    staged = epub_path.with_name(epub_path.stem + ".staging.epub")
    command = [
        pandoc,
        str(compiled),
        "--from=markdown",
        "--to=epub3",
        "--standalone",
        "--toc",
        "--toc-depth=2",
        "--split-level=1",
        "-o",
        str(staged),
    ]
    if config.book.title:
      command.extend(["--metadata", f"title={config.book.title}"])
    if config.book.authors:
      command.extend(["--metadata", f"creator={', '.join(config.book.authors)}"])
    if config.book.language:
      command.extend(["--metadata", f"lang={config.book.language}"])
    if cover is not None and cover.is_file():
      command.append(f"--epub-cover-image={cover}")
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
      raise ManuscriptError(
          f"pandoc failed to build the EPUB: {(result.stderr or result.stdout).strip()}"
      )
    accessible = staged.with_name(staged.stem + ".a11y.epub")
    apply_accessibility(
        staged,
        accessible,
        certified_by=config.accessibility.certified_by,
        cover_alt=config.accessibility.cover_alt,
        summary=config.accessibility.summary,
    )
    staged.unlink(missing_ok=True)
    accessible.replace(epub_path)
  finally:
    shutil.rmtree(compiled.parent, ignore_errors=True)
  return epub_path


def compiled_markdown_path(config: AppConfig, *, cwd: Path | None = None) -> Path:
  root = manuscript_root(config, cwd=cwd)
  return root.parent / ".build" / "compiled.md"
