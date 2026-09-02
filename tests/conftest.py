"""Synthetic, redistributable EPUB and audio fixtures."""

from __future__ import annotations

import base64
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Callable

import pytest

from ebook_tts.media.tools import MediaTools, preflight


_ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


@pytest.fixture
def epub_factory(tmp_path: Path) -> Callable[..., Path]:
  def create(
      *,
      fragmented: bool = False,
      include_cover: bool = True,
      title: str = "Synthetic Public Domain Book",
      text_repeat: int = 8,
  ) -> Path:
    path = tmp_path / ("fragmented.epub" if fragmented else "book.epub")
    paragraph_one = (
        "This synthetic paragraph exists only to exercise safe extraction, "
        "chunk planning, and offline audiobook tests. " * text_repeat
    ).strip()
    paragraph_two = (
        "A second synthetic passage verifies ordering, metadata, resumption, "
        "quality reports, and deterministic packaging. " * text_repeat
    ).strip()
    if fragmented:
      spine_items = '<item id="book" href="book.xhtml" media-type="application/xhtml+xml"/>'
      spine_refs = '<itemref idref="book"/>'
      nav_links = (
          '<li><a href="book.xhtml#one">Chapter One</a></li>'
          '<li><a href="book.xhtml#two">Chapter Two</a></li>'
      )
      documents = {
          "OEBPS/book.xhtml": f'''<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><body>
<section id="one"><h1>Chapter One</h1><p>{paragraph_one}</p></section>
<section id="two"><h1>Chapter Two</h1><p>{paragraph_two}</p></section>
</body></html>'''
      }
    else:
      spine_items = (
          '<item id="one" href="one.xhtml" media-type="application/xhtml+xml"/>'
          '<item id="two" href="two.xhtml" media-type="application/xhtml+xml"/>'
      )
      spine_refs = '<itemref idref="one"/><itemref idref="two"/>'
      nav_links = (
          '<li><a href="one.xhtml">Chapter One</a></li>'
          '<li><a href="two.xhtml">Chapter Two</a></li>'
      )
      documents = {
          "OEBPS/one.xhtml": f'''<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><body>
<h1>Chapter One</h1><p>{paragraph_one}</p>
</body></html>''',
          "OEBPS/two.xhtml": f'''<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><body>
<h1>Chapter Two</h1><p>{paragraph_two}</p>
</body></html>''',
      }
    cover_item = (
        '<item id="cover" href="cover.png" media-type="image/png" properties="cover-image"/>'
        if include_cover
        else ""
    )
    opf = f'''<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:identifier id="id">urn:uuid:synthetic-test-book</dc:identifier>
<dc:title>{title}</dc:title><dc:creator>Example Author</dc:creator><dc:language>en</dc:language>
</metadata><manifest>
<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
{cover_item}{spine_items}
</manifest><spine>{spine_refs}</spine></package>'''
    nav = f'''<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<body><nav epub:type="toc" id="toc"><ol>{nav_links}</ol></nav></body></html>'''
    container = '''<?xml version="1.0" encoding="utf-8"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">
<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>'''
    with zipfile.ZipFile(path, "w", allowZip64=True) as archive:
      archive.writestr(
          "mimetype",
          "application/epub+zip",
          compress_type=zipfile.ZIP_STORED,
      )
      archive.writestr("META-INF/container.xml", container)
      archive.writestr("OEBPS/content.opf", opf)
      archive.writestr("OEBPS/nav.xhtml", nav)
      for name, value in documents.items():
        archive.writestr(name, value)
      if include_cover:
        archive.writestr("OEBPS/cover.png", _ONE_PIXEL_PNG)
    return path

  return create


@pytest.fixture
def media_tools() -> MediaTools:
  if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
    pytest.skip("ffmpeg and ffprobe are required for media integration tests")
  return preflight("ffmpeg", "ffprobe")


@pytest.fixture
def tone_mp3(tmp_path: Path, media_tools: MediaTools) -> bytes:
  path = tmp_path / "tone.mp3"
  result = subprocess.run(
      [
          media_tools.ffmpeg,
          "-hide_banner",
          "-loglevel",
          "error",
          "-f",
          "lavfi",
          "-i",
          "sine=frequency=440:duration=0.35:sample_rate=44100",
          "-ac",
          "1",
          "-codec:a",
          "libmp3lame",
          "-b:a",
          "128k",
          "-y",
          str(path),
      ],
      capture_output=True,
      check=False,
  )
  if result.returncode != 0:
    pytest.skip("ffmpeg does not provide the libmp3lame encoder")
  return path.read_bytes()
