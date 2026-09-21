"""Restricted chapter headers and prose-word counting.

The header is not YAML. It is a closed `---` block of `key: value` lines so a
word count and a title cannot silently drift into the body, and a `---` scene
break in the prose cannot be mistaken for a second header unless it is followed
by a header key. That is the defect this format exists to make impossible.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from ..errors import ManuscriptError


HEADER_DELIMITER = "---"
HEADER_LINE_RE = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*:\s*(?P<value>.*)$")
DEFAULT_REQUIRED_KEYS = ("chapter", "title", "words", "status")


@dataclass(frozen=True)
class ChapterDiagnostic:
  """One objective manuscript finding. Editorial judgement is out of scope."""

  code: str
  item: str
  observed: str
  expected: str
  severity: str = "error"

  def format_text(self) -> str:
    return (
        f"{self.severity.upper()} {self.code} {self.item} "
        f"observed={self.observed} expected={self.expected}"
    )


@dataclass(frozen=True)
class ChapterDocument:
  """One chapter file after a fail-closed header/body split."""

  path: str
  header: dict[str, Any] = field(default_factory=dict)
  prose_body: str | None = None
  diagnostics: tuple[ChapterDiagnostic, ...] = ()

  @property
  def ok(self) -> bool:
    return self.prose_body is not None and not any(
        item.severity == "error" for item in self.diagnostics
    )


def normalize_prose(value: str) -> str:
  return unicodedata.normalize("NFC", value).replace("\r\n", "\n").replace("\r", "\n")


def count_prose_words(prose: str) -> int:
  """Whitespace-separated tokens in the body after the closing header delimiter."""
  return len(normalize_prose(prose).split())


def _unquote(value: str) -> str:
  if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
    return value[1:-1]
  return value


def _parse_value(raw: str) -> Any:
  stripped = raw.strip()
  if stripped in {"[]", ""}:
    return [] if stripped == "[]" else stripped
  if stripped.startswith("[") and stripped.endswith("]"):
    inner = stripped[1:-1].strip()
    if not inner:
      return []
    return [_unquote(part.strip()) for part in inner.split(",") if part.strip()]
  if re.fullmatch(r"-?\d+", stripped):
    return int(stripped)
  return _unquote(stripped)


def parse_chapter_document(
    text: str,
    *,
    item: str,
    required_keys: tuple[str, ...] = DEFAULT_REQUIRED_KEYS,
    extra_header_keys: bool = True,
) -> ChapterDocument:
  """Split and parse one chapter. Never guesses a missing prose boundary."""
  diagnostics: list[ChapterDiagnostic] = []
  normalized = normalize_prose(text).lstrip("\ufeff")
  lines = normalized.split("\n")
  if not lines or lines[0] != HEADER_DELIMITER:
    observed = lines[0] if lines else ""
    return ChapterDocument(
        path=item,
        diagnostics=(
            ChapterDiagnostic(
                code="CHAPTER_HEADER_OPEN_DELIMITER_MISSING",
                item=item,
                observed=f"first physical line is {observed!r}",
                expected="first physical line to be exactly ---",
            ),
        ),
    )

  close_index = next(
      (index for index in range(1, len(lines)) if lines[index] == HEADER_DELIMITER),
      None,
  )
  if close_index is None:
    return ChapterDocument(
        path=item,
        diagnostics=(
            ChapterDiagnostic(
                code="CHAPTER_HEADER_CLOSE_DELIMITER_MISSING",
                item=item,
                observed="no closing --- after the opening delimiter",
                expected="a closing --- line ending the chapter header",
            ),
        ),
    )

  key_pattern = re.compile(
      r"^(?:{0}):".format("|".join(re.escape(key) for key in required_keys))
  )
  body_lines = lines[close_index + 1 :]
  for offset in range(len(body_lines) - 1):
    if body_lines[offset] != HEADER_DELIMITER:
      continue
    if key_pattern.match(body_lines[offset + 1]) is None:
      continue
    return ChapterDocument(
        path=item,
        diagnostics=(
            ChapterDiagnostic(
                code="CHAPTER_HEADER_SECOND_BLOCK",
                item=item,
                observed=f"a second header block opens at line {close_index + offset + 2}",
                expected="exactly one chapter header block per file",
            ),
        ),
    )

  header: dict[str, Any] = {}
  seen: dict[str, int] = {}
  for offset, line in enumerate(lines[1:close_index], start=2):
    if not line.strip():
      diagnostics.append(
          ChapterDiagnostic(
              code="CHAPTER_HEADER_LINE_MALFORMED",
              item=item,
              observed=f"header line {offset} is blank",
              expected="one `key: value` pair per line with no blank line",
          )
      )
      continue
    match = HEADER_LINE_RE.match(line)
    if match is None:
      diagnostics.append(
          ChapterDiagnostic(
              code="CHAPTER_HEADER_LINE_MALFORMED",
              item=item,
              observed=f"header line {offset} is {line!r}",
              expected="one `key: value` pair per line",
          )
      )
      continue
    key = match.group("key")
    raw = match.group("value").strip()
    if key not in required_keys and not extra_header_keys:
      diagnostics.append(
          ChapterDiagnostic(
              code="CHAPTER_HEADER_KEY_UNKNOWN",
              item=item,
              observed=key,
              expected=f"only the required keys {', '.join(required_keys)}",
          )
      )
      continue
    seen[key] = seen.get(key, 0) + 1
    if seen[key] > 1:
      diagnostics.append(
          ChapterDiagnostic(
              code="CHAPTER_HEADER_KEY_DUPLICATE",
              item=item,
              observed=f"{key} occurs {seen[key]} times",
              expected="exactly one occurrence of each header key",
          )
      )
      continue
    if not raw:
      diagnostics.append(
          ChapterDiagnostic(
              code="CHAPTER_HEADER_VALUE_MALFORMED",
              item=item,
              observed=f"{key} is blank",
              expected="a nonblank value on the same physical line",
          )
      )
      continue
    header[key] = _parse_value(raw)

  for key in required_keys:
    if key not in header:
      diagnostics.append(
          ChapterDiagnostic(
              code="CHAPTER_HEADER_KEY_MISSING",
              item=item,
              observed=f"{key} is absent",
              expected=f"required header key {key}",
          )
      )

  return ChapterDocument(
      path=item,
      header=header,
      prose_body="\n".join(body_lines),
      diagnostics=tuple(diagnostics),
  )


@dataclass(frozen=True)
class WordCountFix:
  """One reconciled `words:` header value."""

  path: str
  declared: int | None
  observed: int

  def format_text(self) -> str:
    was = "absent" if self.declared is None else str(self.declared)
    return f"{self.path} words: {was} -> {self.observed}"


def rewrite_header_word_count(text: str, observed: int) -> str:
  """Return `text` with only the header's `words:` line set to `observed`.

  The prose body is never examined or altered, and no other header key is
  reordered, requoted, or reformatted. A declared count is the one piece of
  chapter metadata a tool can derive, so deriving it removes a whole class of
  silent drift; rewriting anything else here would remove the author's text.
  """
  normalized = normalize_prose(text).lstrip("\ufeff")
  lines = normalized.split("\n")
  if not lines or lines[0] != HEADER_DELIMITER:
    raise ManuscriptError("Refusing to rewrite a file with no opening header delimiter.")
  close_index = next(
      (index for index in range(1, len(lines)) if lines[index] == HEADER_DELIMITER),
      None,
  )
  if close_index is None:
    raise ManuscriptError("Refusing to rewrite a file with no closing header delimiter.")
  for index in range(1, close_index):
    match = HEADER_LINE_RE.match(lines[index])
    if match is None or match.group("key") != "words":
      continue
    lines[index] = f"words: {observed}"
    return "\n".join(lines)
  raise ManuscriptError("Refusing to rewrite a header that declares no words key.")


def render_chapter_markdown(
    *,
    header: dict[str, Any],
    prose: str,
    key_order: tuple[str, ...] = DEFAULT_REQUIRED_KEYS,
) -> str:
  """Write a chapter file with a restricted header and a prose body."""
  lines = [HEADER_DELIMITER]
  ordered = list(key_order) + [key for key in header if key not in key_order]
  for key in ordered:
    if key not in header:
      continue
    value = header[key]
    if key in {"title", "hook"} and isinstance(value, str):
      lines.append(f'{key}: "{value}"')
    elif isinstance(value, list):
      inner = ", ".join(str(item) for item in value)
      lines.append(f"{key}: [{inner}]")
    else:
      lines.append(f"{key}: {value}")
  lines.append(HEADER_DELIMITER)
  body = prose.strip("\n")
  if body:
    lines.append(body)
  lines.append("")
  return "\n".join(lines)


def require_parsed(document: ChapterDocument) -> ChapterDocument:
  if document.prose_body is None:
    rendered = "; ".join(item.format_text() for item in document.diagnostics)
    raise ManuscriptError(rendered or f"Unreadable chapter: {document.path}")
  return document
