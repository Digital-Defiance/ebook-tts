from pathlib import Path

import pytest

from ebook_tts.editions.accessibility import (
    add_cover_alt,
    apply_accessibility,
    inject_accessibility,
    strip_managed,
)
from ebook_tts.errors import ManuscriptError


def test_inject_accessibility_is_idempotent() -> None:
  opf = """<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
  <metadata>
    <dc:title>Example</dc:title>
  </metadata>
</package>
"""
  once = inject_accessibility(opf, certified_by="Tester")
  twice = inject_accessibility(once, certified_by="Tester")
  assert twice.count("schema:accessModeSufficient") == 1
  assert "a11y:certifiedBy" in twice
  assert "textual" in twice


def test_strip_managed_then_reinject_keeps_one_set() -> None:
  opf = """<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
  <metadata>
    <dc:title>Example</dc:title>
    <meta property="schema:accessMode">textual</meta>
  </metadata>
</package>
"""
  cleaned = strip_managed(opf)
  assert "schema:accessMode" not in cleaned
  reinjected = inject_accessibility(cleaned, summary="Custom summary.")
  assert reinjected.count("schema:accessModeSufficient") == 1
  assert "Custom summary." in reinjected


def test_img_cover_gets_alt_text() -> None:
  labelled, ok = add_cover_alt('<p><img src="cover.png"/></p>', "Cover of the book")
  assert ok
  assert 'alt="Cover of the book"' in labelled
  replaced, ok = add_cover_alt(labelled, "Updated cover")
  assert ok
  assert 'alt="Updated cover"' in replaced


def test_missing_metadata_closes_hard() -> None:
  with pytest.raises(ManuscriptError, match="</metadata>"):
    inject_accessibility("<package><metadata></package>")


def test_svg_cover_gets_a_title(epub_factory, tmp_path) -> None:
  labelled, ok = add_cover_alt(
      '<svg xmlns="http://www.w3.org/2000/svg"><image href="cover.png"/></svg>',
      "Cover of the book",
  )
  assert ok
  assert 'role="img"' in labelled
  assert "Cover of the book" in labelled
  source = epub_factory(include_cover=True, text_repeat=1)
  output = tmp_path / "accessible.epub"
  result = apply_accessibility(source, output, cover_alt="Test cover")
  assert output.is_file()
  assert result["package"]
