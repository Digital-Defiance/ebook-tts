"""Turn an EPUB spine into an editable markdown manuscript."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from ..epub.package import load_publication
from ..errors import ManuscriptError
from ..models import AppConfig, Publication
from ..utils import slugify
from .document import count_prose_words, render_chapter_markdown


def extract_manuscript(
    epub: Path,
    destination: Path,
    config: AppConfig,
    *,
    status: str = "draft",
) -> Publication:
  """Write one chapter file per narratable EPUB section.

  The EPUB remains the ingest source; after this command the markdown tree is
  the source of truth and later `compile` rebuilds an edition from it.
  """
  stripped = replace(
      config,
      sections=replace(config.sections, announce_titles=False),
  )
  publication = load_publication(epub, stripped)
  if destination.exists() and any(destination.iterdir()):
    raise ManuscriptError(
        f"Refusing to extract into a non-empty directory: {destination}"
    )
  chapters = destination / config.manuscript.chapters
  chapters.mkdir(parents=True, exist_ok=True)
  for section in publication.sections:
    number = section.chapter_number or section.track_number
    slug = slugify(section.title)
    path = chapters / f"{number:03d}-{slug}.md"
    prose = section.text.strip()
    header = {
        "chapter": number,
        "title": section.title,
        "words": count_prose_words(prose),
        "status": status,
    }
    path.write_text(
        render_chapter_markdown(
            header=header,
            prose=prose,
            key_order=config.manuscript.required_header_keys,
        ),
        encoding="utf-8",
    )
  (destination / config.manuscript.front_matter).write_text(
      f"# {publication.metadata.title}\n\n",
      encoding="utf-8",
  )
  (destination / config.manuscript.back_matter).write_text("", encoding="utf-8")
  return publication
