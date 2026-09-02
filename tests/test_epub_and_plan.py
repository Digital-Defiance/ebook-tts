from __future__ import annotations

import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from ebook_tts.config import default_config
from ebook_tts.epub.container import EpubContainer
from ebook_tts.epub.package import load_publication
from ebook_tts.errors import EpubError
from ebook_tts.workspace.manifests import create_plan, load_plan


def test_loads_metadata_cover_and_ordered_sections(epub_factory) -> None:
  publication = load_publication(epub_factory(), default_config())
  assert publication.metadata.title == "Synthetic Public Domain Book"
  assert publication.metadata.authors == ("Example Author",)
  assert publication.metadata.language == "en"
  assert publication.cover_bytes
  assert [section.title for section in publication.sections] == [
      "Chapter One",
      "Chapter Two",
  ]
  assert publication.sections[0].text.startswith("Chapter One\n\n")


def test_splits_multiple_toc_fragments_in_one_spine_item(epub_factory) -> None:
  publication = load_publication(epub_factory(fragmented=True), default_config())
  assert len(publication.sections) == 2
  assert [section.source_fragment for section in publication.sections] == ["one", "two"]
  assert "second synthetic passage" not in publication.sections[0].text
  assert "second synthetic passage" in publication.sections[1].text


def test_rejects_zip_path_traversal(tmp_path: Path) -> None:
  path = tmp_path / "unsafe.epub"
  with zipfile.ZipFile(path, "w") as archive:
    archive.writestr("../outside", "bad")
    archive.writestr("META-INF/container.xml", "<container/>")
  with pytest.raises(EpubError, match="traversal"):
    EpubContainer(path)


def test_plan_is_reused_and_changed_chunk_limit_gets_new_id(
    epub_factory,
    tmp_path: Path,
) -> None:
  publication = load_publication(epub_factory(include_cover=False), default_config())
  workspace = tmp_path / "workspace"
  first = create_plan(publication, default_config(), workspace)
  second = create_plan(publication, default_config(), workspace)
  assert first.plan_id == second.plan_id
  assert load_plan(workspace).plan_id == first.plan_id
  changed_config = replace(
      default_config(),
      tts=replace(default_config().tts, max_characters=500),
  )
  changed = create_plan(publication, changed_config, workspace)
  assert changed.plan_id != first.plan_id
  assert first.plan_path.is_file()
  assert changed.plan_path.is_file()
