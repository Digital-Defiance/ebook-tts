"""Objective manuscript checks. Editorial judgement is a separate human gate."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..models import AppConfig
from .document import ChapterDiagnostic, ChapterDocument, count_prose_words
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
