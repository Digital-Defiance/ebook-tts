from __future__ import annotations

import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest

from ebook_tts.config import default_config
from ebook_tts.epub.container import MAX_XML_BYTES, EpubContainer, parse_xml_bytes
from ebook_tts.epub.package import load_publication
from ebook_tts.errors import EpubError


def _xml_bytes(content: str, encoding: str) -> bytes:
  document = f'<?xml version="1.0" encoding="{encoding}"?>{content}'
  return document.encode(encoding)


def _replace_epub_member(
    source: Path,
    destination: Path,
    member_name: str,
    value: bytes,
) -> Path:
  replaced = False
  with (
      zipfile.ZipFile(source, "r") as input_archive,
      zipfile.ZipFile(destination, "w", allowZip64=True) as output_archive,
  ):
    for info in input_archive.infolist():
      is_target = info.filename == member_name
      member_value = value if is_target else input_archive.read(info)
      output_archive.writestr(
          info.filename,
          member_value,
          compress_type=zipfile.ZIP_STORED if is_target else info.compress_type,
      )
      replaced = replaced or is_target
  assert replaced, f"Test EPUB has no member {member_name}"
  return destination


@pytest.mark.parametrize("encoding", ["UTF-8", "UTF-16"])
def test_rejects_internal_entity_doctype_in_supported_encodings(encoding: str) -> None:
  value = _xml_bytes(
      '<!DOCTYPE root [<!ENTITY secret "expanded">]><root>&secret;</root>',
      encoding,
  )

  with pytest.raises(EpubError) as exc_info:
    parse_xml_bytes(value, "OEBPS/chapter.xhtml")

  assert "DTD/entity declarations are not allowed" in str(exc_info.value)
  assert "OEBPS/chapter.xhtml" in str(exc_info.value)


def test_rejects_external_system_doctype() -> None:
  value = _xml_bytes(
      '<!DOCTYPE root SYSTEM "https://example.invalid/external.dtd"><root/>',
      "UTF-8",
  )

  with pytest.raises(EpubError) as exc_info:
    parse_xml_bytes(value, "OEBPS/external.xhtml")

  assert "DTD/entity declarations are not allowed" in str(exc_info.value)
  assert "OEBPS/external.xhtml" in str(exc_info.value)


@pytest.mark.parametrize("encoding", ["UTF-8", "UTF-16"])
def test_allows_doctype_text_in_comments_and_cdata(encoding: str) -> None:
  text = '<!DOCTYPE root [<!ENTITY harmless "text">]>'
  value = _xml_bytes(
      f"<root><!-- {text} --><content><![CDATA[{text}]]></content></root>",
      encoding,
  )

  root = parse_xml_bytes(value, "OEBPS/clean.xhtml")

  assert root.findtext("content") == text


def test_malformed_xml_is_a_source_labelled_epub_error() -> None:
  with pytest.raises(
      EpubError,
      match=r"Invalid XML in OEBPS/malformed\.xhtml:",
  ) as exc_info:
    parse_xml_bytes(b"<root><broken></root>", "OEBPS/malformed.xhtml")

  assert isinstance(exc_info.value.__cause__, ET.ParseError)


def test_container_read_xml_uses_hardened_parser(epub_factory, tmp_path: Path) -> None:
  path = _replace_epub_member(
      epub_factory(),
      tmp_path / "unsafe-container.epub",
      "META-INF/container.xml",
      _xml_bytes("<!DOCTYPE container><container/>", "UTF-16"),
  )

  with EpubContainer(path) as container:
    with pytest.raises(EpubError) as exc_info:
      container.read_xml("META-INF/container.xml")

  assert "DTD/entity declarations are not allowed" in str(exc_info.value)
  assert "META-INF/container.xml" in str(exc_info.value)


def test_load_publication_spine_uses_hardened_parser(
    epub_factory,
    tmp_path: Path,
) -> None:
  xhtml = _xml_bytes(
      """<!DOCTYPE html [<!ENTITY secret "expanded">]>
<html xmlns="http://www.w3.org/1999/xhtml"><body><p>&secret;</p></body></html>""",
      "UTF-16",
  )
  path = _replace_epub_member(
      epub_factory(),
      tmp_path / "unsafe-spine.epub",
      "OEBPS/one.xhtml",
      xhtml,
  )

  with pytest.raises(EpubError) as exc_info:
    load_publication(path, default_config())

  assert "DTD/entity declarations are not allowed" in str(exc_info.value)
  assert "OEBPS/one.xhtml" in str(exc_info.value)


def test_load_publication_bounds_spine_before_reading(
    epub_factory,
    tmp_path: Path,
) -> None:
  path = _replace_epub_member(
      epub_factory(),
      tmp_path / "oversized-spine.epub",
      "OEBPS/one.xhtml",
      b" " * (MAX_XML_BYTES + 1),
  )

  with pytest.raises(EpubError) as exc_info:
    load_publication(path, default_config())

  assert "OEBPS/one.xhtml" in str(exc_info.value)
  assert f"{MAX_XML_BYTES:,}-byte read limit" in str(exc_info.value)
