"""Objective manuscript checks. Editorial judgement is a separate human gate."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..errors import ManuscriptError
from ..models import AppConfig
from ..utils import atomic_write_text
from .document import (
    ChapterDiagnostic,
    ChapterDocument,
    WordCountFix,
    count_prose_words,
    rewrite_header_word_count,
)
from .discover import discover_chapter_files, load_chapter


@dataclass(frozen=True)
class ManuscriptReport:
  documents: tuple[ChapterDocument, ...]
  diagnostics: tuple[ChapterDiagnostic, ...]

  @property
  def ok(self) -> bool:
    return not any(item.severity == "error" for item in self.diagnostics)


def check_chapter(document: ChapterDocument) -> tuple[ChapterDiagnostic, ...]:
  """Header completeness plus declared-vs-observed prose word count."""
  findings = list(document.diagnostics)
  if document.prose_body is None:
    return tuple(findings)
  observed = count_prose_words(document.prose_body)
  declared = document.header.get("words")
  if isinstance(declared, int) and declared != observed:
    findings.append(
        ChapterDiagnostic(
            code="CHAPTER_WORD_COUNT_MISMATCH",
            item=document.path,
            observed=str(observed),
            expected=f"header words: {declared}",
        )
    )
  chapter = document.header.get("chapter")
  if chapter is not None and not isinstance(chapter, int):
    findings.append(
        ChapterDiagnostic(
            code="CHAPTER_NUMBER_MALFORMED",
            item=document.path,
            observed=repr(chapter),
            expected="an integer chapter number",
        )
    )
  title = document.header.get("title")
  if title is not None and (not isinstance(title, str) or not title.strip()):
    findings.append(
        ChapterDiagnostic(
            code="CHAPTER_TITLE_MISSING",
            item=document.path,
            observed=repr(title),
            expected="a non-empty title",
        )
    )
  return tuple(findings)


def check_manuscript(root: Path, config: AppConfig) -> ManuscriptReport:
  """Load every chapter and fail closed on identity or word-count defects."""
  documents: list[ChapterDocument] = []
  diagnostics: list[ChapterDiagnostic] = []
  numbers: dict[int, str] = {}
  for path in discover_chapter_files(root, config.manuscript):
    document = load_chapter(path, config.manuscript)
    documents.append(document)
    chapter_findings = check_chapter(document)
    diagnostics.extend(chapter_findings)
    chapter = document.header.get("chapter")
    if isinstance(chapter, int):
      previous = numbers.get(chapter)
      if previous is not None:
        diagnostics.append(
            ChapterDiagnostic(
                code="CHAPTER_NUMBER_DUPLICATE",
                item=document.path,
                observed=f"chapter {chapter} also in {previous}",
                expected="one file per chapter number",
            )
        )
      else:
        numbers[chapter] = document.path
  return ManuscriptReport(documents=tuple(documents), diagnostics=tuple(diagnostics))


def reconcile_word_counts(root: Path, config: AppConfig) -> tuple[WordCountFix, ...]:
  """Set every chapter's declared `words:` to its observed prose count.

  This is the one objective defect a tool can repair without deciding anything
  about the book: the count is derivable from the prose, so a declared value
  that disagrees with it is always the stale one. Every other diagnostic stays
  a report, because repairing it would mean choosing on the author's behalf.

  Fails closed. A chapter whose header cannot be parsed is left untouched and
  raises, so a malformed file is never silently rewritten or skipped.
  """
  fixes: list[WordCountFix] = []
  for path in discover_chapter_files(root, config.manuscript):
    document = load_chapter(path, config.manuscript)
    if document.prose_body is None:
      rendered = "; ".join(item.format_text() for item in document.diagnostics)
      raise ManuscriptError(
          f"Refusing to reconcile an unreadable chapter: {path}. {rendered}"
      )
    observed = count_prose_words(document.prose_body)
    declared = document.header.get("words")
    if declared == observed:
      continue
    original = path.read_text(encoding="utf-8")
    atomic_write_text(
        path,
        rewrite_header_word_count(original, observed),
        mode=path.stat().st_mode & 0o777,
    )
    fixes.append(
        WordCountFix(
            path=path.as_posix(),
            declared=declared if isinstance(declared, int) else None,
            observed=observed,
        )
    )
  return tuple(fixes)
