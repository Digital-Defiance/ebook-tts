"""Resource-bounded, traversal-safe access to an EPUB ZIP container."""

from __future__ import annotations

import stat
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

from ..errors import EpubError


MAX_ENTRIES = 10_000
MAX_MEMBER_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024
MAX_XML_BYTES = 8 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200


def local_name(value: object) -> str:
  return value.rsplit("}", 1)[-1] if isinstance(value, str) else ""


def attribute_by_local_name(element: ET.Element, name: str) -> str | None:
  for key, value in element.attrib.items():
    if local_name(key) == name:
      return value
  return None


class _DoctypeForbidden(Exception):
  """Internal signal raised before a document type declaration is processed."""


class _RejectingTreeBuilder(ET.TreeBuilder):
  def doctype(
      self,
      _name: str,
      _pubid: str | None,
      _system: str | None,
  ) -> None:
    raise _DoctypeForbidden


def parse_xml_bytes(value: bytes, source_name: str) -> ET.Element:
  """Parse bounded XML while rejecting parser-recognized DOCTYPE declarations."""
  if len(value) > MAX_XML_BYTES:
    raise EpubError(f"XML resource exceeds {MAX_XML_BYTES:,} bytes: {source_name}")
  try:
    parser = ET.XMLParser(target=_RejectingTreeBuilder())
    return ET.fromstring(value, parser=parser)
  except _DoctypeForbidden as exc:
    raise EpubError(f"DTD/entity declarations are not allowed in {source_name}.") from exc
  except ET.ParseError as exc:
    raise EpubError(f"Invalid XML in {source_name}: {exc}") from exc


def _safe_member_name(value: str) -> str:
  if not value or "\x00" in value or "\\" in value:
    raise EpubError(f"Unsafe EPUB member name: {value!r}")
  if value.startswith("/"):
    raise EpubError(f"Absolute EPUB member path is not allowed: {value!r}")
  parts: list[str] = []
  for part in PurePosixPath(value).parts:
    if part in {"", "."}:
      continue
    if part == "..":
      raise EpubError(f"EPUB member path traversal is not allowed: {value!r}")
    parts.append(part)
  if not parts:
    raise EpubError(f"Unsafe EPUB member name: {value!r}")
  return "/".join(parts)


def _join_member(base_name: str, relative: str) -> str:
  if "\\" in relative or "\x00" in relative:
    raise EpubError(f"Unsafe EPUB href: {relative!r}")
  if relative.startswith("/"):
    raise EpubError(f"Absolute EPUB href is not allowed: {relative!r}")
  parts = list(PurePosixPath(base_name).parent.parts)
  for part in PurePosixPath(relative).parts:
    if part in {"", "."}:
      continue
    if part == "..":
      if not parts:
        raise EpubError(f"EPUB href escapes the container: {relative!r}")
      parts.pop()
    else:
      parts.append(part)
  if not parts:
    raise EpubError(f"EPUB href resolves to no resource: {relative!r}")
  return "/".join(parts)


class EpubContainer:
  """Validated random access to one local DRM-free EPUB."""

  def __init__(self, path: Path) -> None:
    self.path = path.expanduser().resolve()
    if not self.path.is_file():
      raise EpubError(f"EPUB file does not exist: {self.path}")
    try:
      self._archive = zipfile.ZipFile(self.path, "r")
    except (OSError, zipfile.BadZipFile) as exc:
      raise EpubError(f"Not a readable EPUB ZIP: {self.path}: {exc}") from exc
    self._members: dict[str, zipfile.ZipInfo] = {}
    try:
      self._validate()
    except Exception:
      self._archive.close()
      raise

  def _validate(self) -> None:
    infos = self._archive.infolist()
    if len(infos) > MAX_ENTRIES:
      raise EpubError(
          f"EPUB has {len(infos):,} entries; safety limit is {MAX_ENTRIES:,}."
      )
    total = 0
    for info in infos:
      name = _safe_member_name(info.filename.rstrip("/"))
      if info.is_dir():
        continue
      if name in self._members:
        raise EpubError(f"EPUB contains duplicate member {name!r}.")
      if info.flag_bits & 0x1:
        raise EpubError(f"Encrypted/DRM EPUB member is unsupported: {name}")
      unix_mode = info.external_attr >> 16
      if unix_mode and stat.S_ISLNK(unix_mode):
        raise EpubError(f"Symbolic links are not allowed in EPUB files: {name}")
      if info.file_size > MAX_MEMBER_BYTES:
        raise EpubError(
            f"EPUB member {name} is {info.file_size:,} bytes; safety limit is "
            f"{MAX_MEMBER_BYTES:,}."
        )
      total += info.file_size
      if total > MAX_TOTAL_BYTES:
        raise EpubError(
            f"EPUB expands beyond the {MAX_TOTAL_BYTES:,}-byte safety limit."
        )
      if info.file_size and info.compress_size == 0:
        raise EpubError(f"Invalid compressed size for EPUB member {name}.")
      if info.compress_size:
        ratio = info.file_size / info.compress_size
        if ratio > MAX_COMPRESSION_RATIO:
          raise EpubError(
              f"EPUB member {name} has suspicious {ratio:.1f}:1 compression."
          )
      self._members[name] = info

    mimetype = self._members.get("mimetype")
    if mimetype is not None:
      value = self.read_bytes("mimetype", max_bytes=128).strip()
      if value != b"application/epub+zip":
        raise EpubError("EPUB mimetype member is not application/epub+zip.")
    if "META-INF/container.xml" not in self._members:
      raise EpubError("EPUB is missing META-INF/container.xml.")

  def close(self) -> None:
    self._archive.close()

  def __enter__(self) -> EpubContainer:
    return self

  def __exit__(self, *_: object) -> None:
    self.close()

  def names(self) -> tuple[str, ...]:
    return tuple(self._members)

  def has(self, name: str) -> bool:
    return _safe_member_name(name) in self._members

  def read_bytes(self, name: str, *, max_bytes: int = MAX_MEMBER_BYTES) -> bytes:
    safe_name = _safe_member_name(name)
    info = self._members.get(safe_name)
    if info is None:
      raise EpubError(f"EPUB resource does not exist: {safe_name}")
    if info.file_size > max_bytes:
      raise EpubError(
          f"EPUB resource {safe_name} exceeds the {max_bytes:,}-byte read limit."
      )
    try:
      value = self._archive.read(info)
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
      raise EpubError(f"Could not read EPUB resource {safe_name}: {exc}") from exc
    if len(value) != info.file_size:
      raise EpubError(f"Truncated EPUB resource: {safe_name}")
    return value

  def read_xml(self, name: str) -> ET.Element:
    return parse_xml_bytes(
        self.read_bytes(name, max_bytes=MAX_XML_BYTES),
        name,
    )

  def resolve_href(self, base_name: str, href: str) -> tuple[str, str | None]:
    """Resolve a local package href and return member name plus fragment."""
    parsed = urlsplit(href)
    if parsed.scheme or parsed.netloc:
      raise EpubError(f"External EPUB href is not allowed: {href!r}")
    if parsed.query:
      raise EpubError(f"Query strings in EPUB resource hrefs are unsupported: {href!r}")
    relative = unquote(parsed.path)
    member = _join_member(base_name, relative) if relative else _safe_member_name(base_name)
    fragment = unquote(parsed.fragment).strip() or None
    return member, fragment
