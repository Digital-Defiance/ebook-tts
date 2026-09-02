"""Visible, block-preserving narration extraction from XHTML."""

from __future__ import annotations

import re
from dataclasses import dataclass
import xml.etree.ElementTree as ET

from ..epub.container import attribute_by_local_name, local_name, parse_xml_bytes
from ..errors import EpubError
from .chunk import normalize_block


_BLOCK_TAGS = {
    "address",
    "article",
    "aside",
    "blockquote",
    "dd",
    "div",
    "dt",
    "figcaption",
    "footer",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "header",
    "li",
    "main",
    "p",
    "pre",
    "section",
    "td",
    "th",
}
_SKIP_TAGS = {"audio", "canvas", "nav", "noscript", "script", "style", "svg", "video"}
_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}


@dataclass(frozen=True)
class ExtractedBlock:
  text: str
  tag: str
  anchors: tuple[str, ...]


def _is_hidden(element: ET.Element) -> bool:
  tag = local_name(element.tag).lower()
  if tag in _SKIP_TAGS:
    return True
  if "hidden" in {local_name(key).lower() for key in element.attrib}:
    return True
  if (element.get("aria-hidden") or "").strip().lower() == "true":
    return True
  style = re.sub(r"\s+", "", (element.get("style") or "").lower())
  if "display:none" in style or "visibility:hidden" in style:
    return True
  epub_type = (attribute_by_local_name(element, "type") or "").lower().split()
  return "noteref" in epub_type or "pagebreak" in epub_type


def _element_anchors(element: ET.Element) -> tuple[str, ...]:
  values = []
  for name in ("id", "name"):
    value = attribute_by_local_name(element, name)
    if value and value not in values:
      values.append(value)
  return tuple(values)


def _visible_text(element: ET.Element) -> str:
  pieces: list[str] = []

  def visit(node: ET.Element) -> None:
    if _is_hidden(node):
      return
    tag = local_name(node.tag).lower()
    if tag == "img":
      return
    if node.text:
      pieces.append(node.text)
    for child in node:
      visit(child)
      if child.tail:
        pieces.append(child.tail)

  visit(element)
  return normalize_block("".join(pieces))


def extract_blocks(value: bytes, source_name: str) -> list[ExtractedBlock]:
  """Extract ordered leaf semantic blocks and their fragment anchors."""
  root = parse_xml_bytes(value, source_name)
  body = next(
      (element for element in root.iter() if local_name(element.tag).lower() == "body"),
      None,
  )
  if body is None:
    raise EpubError(f"XHTML resource has no body: {source_name}")

  blocks: list[ExtractedBlock] = []

  def visit(element: ET.Element, inherited: tuple[str, ...]) -> bool:
    if _is_hidden(element):
      return False
    anchors = inherited + tuple(
        anchor for anchor in _element_anchors(element) if anchor not in inherited
    )
    tag = local_name(element.tag).lower()
    descendant_block = any(
        not _is_hidden(child)
        and (
            local_name(child.tag).lower() in _BLOCK_TAGS
            or any(
                not _is_hidden(item)
                and local_name(item.tag).lower() in _BLOCK_TAGS
                for item in child.iter()
                if item is not child
            )
        )
        for child in element
    )
    emitted = False
    if tag in _BLOCK_TAGS and not descendant_block:
      text = _visible_text(element)
      text = re.sub(r"^•\s*", "", text)
      if text:
        blocks.append(ExtractedBlock(text=text, tag=tag, anchors=anchors))
        return True
    for child in element:
      emitted = visit(child, anchors) or emitted
    if not emitted and tag not in {"body", "html"}:
      text = _visible_text(element)
      text = re.sub(r"^•\s*", "", text)
      if text:
        blocks.append(ExtractedBlock(text=text, tag=tag, anchors=anchors))
        emitted = True
    return emitted

  for child in body:
    visit(child, _element_anchors(body))
  return blocks


def slice_blocks(
    blocks: list[ExtractedBlock],
    fragment: str | None,
    next_fragment: str | None,
    source_name: str,
) -> list[ExtractedBlock]:
  """Select a TOC-fragment range from extracted blocks."""
  if not fragment:
    return blocks

  def position(target: str) -> int | None:
    return next(
        (index for index, block in enumerate(blocks) if target in block.anchors),
        None,
    )

  start = position(fragment)
  if start is None:
    raise EpubError(f"TOC fragment #{fragment} was not found in {source_name}.")
  end = position(next_fragment) if next_fragment else None
  if end is not None and end <= start:
    raise EpubError(
        f"TOC fragments are not in document order in {source_name}: "
        f"#{fragment}, #{next_fragment}"
    )
  return blocks[start:end]


def narration_text(
    blocks: list[ExtractedBlock],
    title: str,
    *,
    announce_title: bool,
) -> str:
  """Convert blocks to narration, avoiding duplicate chapter headings."""
  values = [block.text for block in blocks if block.text]
  if not values:
    return ""
  normalized_title = normalize_block(title)
  chapter = re.fullmatch(r"Chapter\s+(\d+)", normalized_title, flags=re.IGNORECASE)
  if chapter and values[0] == chapter.group(1):
    values[0] = normalized_title
  elif (
      announce_title
      and normalize_block(values[0]).casefold() != normalized_title.casefold()
  ):
    values.insert(0, normalized_title)
  return "\n\n".join(values).strip() + "\n"


def inferred_title(blocks: list[ExtractedBlock], fallback: str) -> str:
  for block in blocks:
    if block.tag in _HEADING_TAGS and block.text:
      return block.text
  return fallback
