"""Parse EPUB metadata, navigation, spine sections, and cover artwork."""

from __future__ import annotations

import fnmatch
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable

from ..errors import EpubError
from ..models import AppConfig, BookMetadata, Publication, Section
from ..text.chunk import normalize_block
from ..text.extract import extract_blocks, inferred_title, narration_text, slice_blocks
from ..text.normalize import normalize_narration
from ..utils import sha256_bytes, sha256_file, sha256_text, track_stem
from .container import (
    MAX_XML_BYTES,
    EpubContainer,
    attribute_by_local_name,
    local_name,
)


@dataclass(frozen=True)
class ManifestItem:
  item_id: str
  path: str
  media_type: str
  properties: frozenset[str]


@dataclass(frozen=True)
class NavTarget:
  path: str
  fragment: str | None
  label: str


_IMAGE_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
}


def _first_text(root: ET.Element, name: str) -> str | None:
  for element in root.iter():
    if local_name(element.tag).lower() == name.lower():
      value = normalize_block("".join(element.itertext()))
      if value:
        return value
  return None


def _all_text(root: ET.Element, name: str) -> tuple[str, ...]:
  values: list[str] = []
  for element in root.iter():
    if local_name(element.tag).lower() == name.lower():
      value = normalize_block("".join(element.itertext()))
      if value and value not in values:
        values.append(value)
  return tuple(values)


def _metadata(opf_root: ET.Element, config: AppConfig) -> BookMetadata:
  title = config.book.title or _first_text(opf_root, "title")
  if not title:
    raise EpubError("EPUB metadata does not contain a title; set book.title.")
  authors = config.book.authors or _all_text(opf_root, "creator")
  language = config.book.language or _first_text(opf_root, "language")
  return BookMetadata(
      title=title,
      authors=authors,
      language=language,
      identifier=_first_text(opf_root, "identifier"),
      publisher=_first_text(opf_root, "publisher"),
      description=_first_text(opf_root, "description"),
  )


def _rootfile(container: EpubContainer) -> str:
  root = container.read_xml("META-INF/container.xml")
  for element in root.iter():
    if local_name(element.tag).lower() == "rootfile":
      value = element.get("full-path")
      if value:
        path, fragment = container.resolve_href("META-INF/container.xml", "../" + value)
        if fragment:
          raise EpubError("The EPUB rootfile path cannot contain a fragment.")
        return path
  raise EpubError("EPUB container.xml does not identify an OPF rootfile.")


def _manifest(container: EpubContainer, opf_path: str, root: ET.Element) -> dict[str, ManifestItem]:
  items: dict[str, ManifestItem] = {}
  for element in root.iter():
    if local_name(element.tag).lower() != "item":
      continue
    item_id = element.get("id")
    href = element.get("href")
    media_type = element.get("media-type") or ""
    if not item_id or not href:
      raise EpubError("Every OPF manifest item must have id and href attributes.")
    if item_id in items:
      raise EpubError(f"Duplicate OPF manifest id: {item_id}")
    path, fragment = container.resolve_href(opf_path, href)
    if fragment:
      raise EpubError(f"OPF manifest href cannot contain a fragment: {href!r}")
    if not container.has(path):
      raise EpubError(f"OPF manifest resource is missing: {path}")
    items[item_id] = ManifestItem(
        item_id=item_id,
        path=path,
        media_type=media_type,
        properties=frozenset((element.get("properties") or "").split()),
    )
  if not items:
    raise EpubError("EPUB OPF manifest is empty.")
  return items


def _spine(root: ET.Element, manifest: dict[str, ManifestItem]) -> tuple[list[ManifestItem], str | None]:
  spine = next(
      (element for element in root.iter() if local_name(element.tag).lower() == "spine"),
      None,
  )
  if spine is None:
    raise EpubError("EPUB OPF does not contain a spine.")
  items: list[ManifestItem] = []
  for reference in spine:
    if local_name(reference.tag).lower() != "itemref":
      continue
    item_id = reference.get("idref")
    if not item_id or item_id not in manifest:
      raise EpubError(f"Invalid OPF spine reference: {item_id!r}")
    if (reference.get("linear") or "yes").lower() == "no":
      continue
    item = manifest[item_id]
    if item.media_type in {"application/xhtml+xml", "text/html"}:
      items.append(item)
  if not items:
    raise EpubError("EPUB has no linear XHTML spine resources.")
  return items, spine.get("toc")


def _anchor_label(anchor: ET.Element) -> str:
  return normalize_block("".join(anchor.itertext()))


def _navigation_targets(
    container: EpubContainer,
    opf_path: str,
    manifest: dict[str, ManifestItem],
    spine_toc_id: str | None,
) -> list[NavTarget]:
  nav_item = next((item for item in manifest.values() if "nav" in item.properties), None)
  targets: list[NavTarget] = []
  if nav_item:
    root = container.read_xml(nav_item.path)
    navs = [element for element in root.iter() if local_name(element.tag).lower() == "nav"]
    toc = next(
        (
            element
            for element in navs
            if "toc" in (attribute_by_local_name(element, "type") or "").split()
            or (element.get("id") or "").lower() == "toc"
        ),
        navs[0] if navs else None,
    )
    if toc is not None:
      for anchor in toc.iter():
        if local_name(anchor.tag).lower() != "a" or not anchor.get("href"):
          continue
        label = _anchor_label(anchor)
        if not label:
          continue
        path, fragment = container.resolve_href(nav_item.path, anchor.get("href") or "")
        targets.append(NavTarget(path, fragment, label))
  elif spine_toc_id and spine_toc_id in manifest:
    ncx = manifest[spine_toc_id]
    root = container.read_xml(ncx.path)
    for point in root.iter():
      if local_name(point.tag).lower() != "navpoint":
        continue
      label = _first_text(point, "text")
      content = next(
          (child for child in point.iter() if local_name(child.tag).lower() == "content"),
          None,
      )
      if label and content is not None and content.get("src"):
        path, fragment = container.resolve_href(ncx.path, content.get("src") or "")
        targets.append(NavTarget(path, fragment, label))
  return targets


def _cover(
    container: EpubContainer,
    opf_root: ET.Element,
    manifest: dict[str, ManifestItem],
) -> tuple[bytes | None, str | None, str | None]:
  item = next((value for value in manifest.values() if "cover-image" in value.properties), None)
  if item is None:
    cover_id = None
    for element in opf_root.iter():
      if (
          local_name(element.tag).lower() == "meta"
          and (element.get("name") or "").lower() == "cover"
      ):
        cover_id = element.get("content")
        break
    if cover_id:
      item = manifest.get(cover_id)
  if item is None:
    item = next(
        (
            value
            for value in manifest.values()
            if value.media_type.startswith("image/")
            and "cover" in PurePosixPath(value.path).stem.casefold()
        ),
        None,
    )
  if item is None or not item.media_type.startswith("image/"):
    return None, None, None
  extension = _IMAGE_EXTENSIONS.get(item.media_type) or PurePosixPath(item.path).suffix
  return container.read_bytes(item.path), item.media_type, extension or ".img"


def _matches(patterns: Iterable[str], title: str, href: str) -> bool:
  title_folded = title.casefold()
  href_folded = href.casefold()
  return any(
      fnmatch.fnmatchcase(title_folded, pattern.casefold())
      or fnmatch.fnmatchcase(href_folded, pattern.casefold())
      for pattern in patterns
  )


def _override_title(config: AppConfig, href: str, title: str) -> str:
  options = config.sections.title_overrides
  return options.get(href, options.get(title, title)).strip() or title


def load_publication(path: Path, config: AppConfig) -> Publication:
  """Load all selected narratable sections from a validated EPUB."""
  warnings: list[str] = []
  source_sha256 = sha256_file(path.expanduser().resolve())
  with EpubContainer(path) as container:
    opf_path = _rootfile(container)
    opf_root = container.read_xml(opf_path)
    metadata = _metadata(opf_root, config)
    manifest = _manifest(container, opf_path, opf_root)
    spine, spine_toc_id = _spine(opf_root, manifest)
    targets = _navigation_targets(container, opf_path, manifest, spine_toc_id)
    targets_by_path: dict[str, list[NavTarget]] = {}
    for target in targets:
      current = targets_by_path.setdefault(target.path, [])
      if (target.fragment, target.label) not in {
          (existing.fragment, existing.label) for existing in current
      }:
        current.append(target)
    cover_bytes, cover_type, cover_extension = _cover(container, opf_root, manifest)

    candidates: list[tuple[str, str | None, str, str, str]] = []
    for item in spine:
      source = container.read_bytes(item.path, max_bytes=MAX_XML_BYTES)
      blocks = extract_blocks(source, item.path)
      if not blocks:
        warnings.append(f"Skipped empty/image-only linear spine item: {item.path}")
        continue
      nav_targets = targets_by_path.get(item.path, [])
      fragmented = [target for target in nav_targets if target.fragment]
      if len(fragmented) > 1:
        for index, target in enumerate(fragmented):
          following = fragmented[index + 1].fragment if index + 1 < len(fragmented) else None
          selected = slice_blocks(blocks, target.fragment, following, item.path)
          text = narration_text(
              selected,
              target.label,
              announce_title=config.sections.announce_titles,
          )
          candidates.append(
              (item.path, target.fragment, target.label, text, sha256_bytes(source))
          )
      else:
        target = nav_targets[0] if nav_targets else None
        fallback = PurePosixPath(item.path).stem.replace("_", " ").replace("-", " ")
        title = target.label if target else inferred_title(blocks, fallback.title())
        text = narration_text(
            blocks,
            title,
            announce_title=config.sections.announce_titles,
        )
        candidates.append((item.path, target.fragment if target else None, title, text, sha256_bytes(source)))

    sections: list[Section] = []
    for href, fragment, discovered_title, raw_text, source_hash in candidates:
      identity = f"{href}#{fragment}" if fragment else href
      title = _override_title(config, identity, discovered_title)
      if config.sections.include and not _matches(config.sections.include, title, identity):
        continue
      if _matches(config.sections.exclude, title, identity):
        continue
      text = normalize_narration(raw_text, config.normalization)
      if len(text.strip()) < config.sections.minimum_characters:
        warnings.append(
            f"Skipped {identity}: only {len(text.strip())} narratable character(s)."
        )
        continue
      track = len(sections) + 1
      chapter_match = re.fullmatch(r"Chapter\s+(\d+)", title, flags=re.IGNORECASE)
      sections.append(
          Section(
              track_number=track,
              title=title,
              output_stem=track_stem(track, title),
              source_href=href,
              source_fragment=fragment,
              source_sha256=source_hash,
              text=text,
              text_sha256=sha256_text(text),
              chapter_number=int(chapter_match.group(1)) if chapter_match else None,
          )
      )

    if not sections:
      raise EpubError("No narratable sections remain after extraction and selection.")
    return Publication(
        source_path=container.path,
        source_sha256=source_sha256,
        metadata=metadata,
        sections=tuple(sections),
        cover_bytes=cover_bytes,
        cover_media_type=cover_type,
        cover_extension=cover_extension,
        warnings=tuple(warnings),
    )
