"""EPUB Accessibility 1.1 metadata for authored editions.

Pandoc cannot emit schema.org accessibility properties. Reading systems and
library platforms use them to tell a reader whether a book is usable *before*
they acquire it. Overclaiming is worse than silence: this pass records only
what is true of a reflowable text EPUB with a labelled cover.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

from ..errors import ManuscriptError


DEFAULT_SUMMARY = (
    "This publication is reflowable text with a full table of contents and a "
    "structured reading order, so it can be resized, reflowed, and read with a "
    "screen reader or refreshable braille display. Headings identify each "
    "chapter, and the cover image carries a text alternative when present. "
    "There is no audio, video, motion, or flashing content, and the file is "
    "not restricted by DRM. This edition does not yet include synchronised "
    "text-and-audio playback."
)

ACCESSIBILITY_META: tuple[tuple[str, str], ...] = (
    ("schema:accessMode", "textual"),
    ("schema:accessMode", "visual"),
    ("schema:accessModeSufficient", "textual"),
    ("schema:accessibilityFeature", "structuralNavigation"),
    ("schema:accessibilityFeature", "tableOfContents"),
    ("schema:accessibilityFeature", "readingOrder"),
    ("schema:accessibilityFeature", "alternativeText"),
    ("schema:accessibilityFeature", "unlocked"),
    ("schema:accessibilityHazard", "none"),
)

MANAGED_PROPERTIES = {
    "schema:accessMode",
    "schema:accessModeSufficient",
    "schema:accessibilityFeature",
    "schema:accessibilityHazard",
    "schema:accessibilitySummary",
    "a11y:certifiedBy",
}


def escape(value: str) -> str:
  return (
      value.replace("&", "&amp;")
      .replace("<", "&lt;")
      .replace(">", "&gt;")
      .replace('"', "&quot;")
  )


def strip_managed(opf: str) -> str:
  pattern = re.compile(
      r"[ \t]*<meta[^>]*\bproperty=\"("
      + "|".join(re.escape(item) for item in MANAGED_PROPERTIES)
      + r")\"[^>]*>.*?</meta>[ \t]*\n?|"
      r"[ \t]*<meta[^>]*\bproperty=\"("
      + "|".join(re.escape(item) for item in MANAGED_PROPERTIES)
      + r")\"[^>]*/>[ \t]*\n?",
      re.DOTALL,
  )
  return pattern.sub("", opf)


def inject_accessibility(
    opf: str,
    *,
    certified_by: str = "",
    summary: str = "",
) -> str:
  opf = strip_managed(opf)
  entries = list(ACCESSIBILITY_META)
  entries.append(("schema:accessibilitySummary", summary.strip() or DEFAULT_SUMMARY))
  if certified_by.strip():
    entries.append(("a11y:certifiedBy", certified_by.strip()))
  block = "".join(
      f'    <meta property="{name}">{escape(value)}</meta>\n'
      for name, value in entries
  )
  if "a11y:" in block and "prefix=" not in opf:
    opf = re.sub(
        r"(<package\b[^>]*?)(\s*>)",
        r'\1 prefix="a11y: http://www.idpf.org/epub/vocab/package/a11y/#"\2',
        opf,
        count=1,
    )
  elif "a11y:" in block:
    package = re.search(r"<package\b[^>]*>", opf)
    if package and "a11y:" not in package.group(0):
      opf = re.sub(
          r'(<package\b[^>]*?prefix=")',
          r"\1a11y: http://www.idpf.org/epub/vocab/package/a11y/# ",
          opf,
          count=1,
      )
  match = re.search(r"</metadata>", opf)
  if match is None:
    raise ManuscriptError("Package document has no </metadata>; cannot inject.")
  return opf[: match.start()] + block + opf[match.start() :]


def add_cover_alt(xhtml: str, alt: str) -> tuple[str, bool]:
  """Label an HTML or SVG cover. Returns (markup, whether a label is present)."""
  if re.search(r"<img\b", xhtml):
    if re.search(r"<img[^>]*\balt=", xhtml):
      return (
          re.sub(
              r'(<img[^>]*\balt=")[^"]*(")',
              lambda match: match.group(1) + escape(alt) + match.group(2),
              xhtml,
              count=1,
          ),
          True,
      )
    return re.sub(r"(<img\b)", r'\1 alt="' + escape(alt) + '"', xhtml, count=1), True

  svg = re.search(r"<svg\b[^>]*>", xhtml)
  if svg is None:
    return xhtml, False
  if 'id="cover-title"' in xhtml:
    return (
        re.sub(
            r'(<title id="cover-title">).*?(</title>)',
            lambda match: match.group(1) + escape(alt) + match.group(2),
            xhtml,
            count=1,
            flags=re.DOTALL,
        ),
        True,
    )
  opening = svg.group(0)
  updated = opening
  if "role=" not in opening:
    updated = updated[:-1].rstrip() + ' role="img"' + opening[-1]
  if "aria-labelledby=" not in updated:
    updated = updated[:-1].rstrip() + ' aria-labelledby="cover-title"' + updated[-1]
  xhtml = xhtml.replace(opening, updated, 1)
  return (
      xhtml.replace(
          updated,
          updated + f'\n<title id="cover-title">{escape(alt)}</title>',
          1,
      ),
      True,
  )


def apply_accessibility(
    source: Path,
    output: Path,
    *,
    certified_by: str = "",
    cover_alt: str = "",
    summary: str = "",
) -> dict[str, int | str]:
  """Rewrite an EPUB with accessibility metadata. Never writes in place."""
  if not source.is_file():
    raise ManuscriptError(f"No EPUB at {source}")
  if output.resolve() == source.resolve():
    raise ManuscriptError("Refusing to write an EPUB in place.")
  with zipfile.ZipFile(source) as archive:
    names = archive.namelist()
    payload = {name: archive.read(name) for name in names}
  opf_names = [name for name in names if name.endswith(".opf")]
  if len(opf_names) != 1:
    raise ManuscriptError(f"Expected exactly one package document, found {opf_names}")
  opf_name = opf_names[0]
  payload[opf_name] = inject_accessibility(
      payload[opf_name].decode("utf-8"),
      certified_by=certified_by,
      summary=summary,
  ).encode("utf-8")
  cover_pages = [
      name
      for name in names
      if name.endswith((".xhtml", ".html")) and "cover" in name.lower()
  ]
  labelled = 0
  alt = cover_alt.strip() or "Book cover"
  for name in cover_pages:
    updated, ok = add_cover_alt(payload[name].decode("utf-8"), alt)
    payload[name] = updated.encode("utf-8")
    labelled += int(ok)
  if labelled == 0:
    opf_text = payload[opf_name].decode("utf-8")
    payload[opf_name] = opf_text.replace(
        '    <meta property="schema:accessibilityFeature">alternativeText</meta>\n',
        "",
    ).encode("utf-8")
  output.parent.mkdir(parents=True, exist_ok=True)
  ordered = ["mimetype"] + [name for name in names if name != "mimetype"]
  with zipfile.ZipFile(output, "w") as archive:
    for name in ordered:
      if name not in payload:
        continue
      compress = zipfile.ZIP_STORED if name == "mimetype" else zipfile.ZIP_DEFLATED
      archive.writestr(name, payload[name], compress_type=compress)
  return {
      "package": opf_name,
      "cover_pages": len(cover_pages),
      "cover_labelled": labelled,
  }
