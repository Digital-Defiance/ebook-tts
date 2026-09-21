"""Markdown manuscript source of truth for authored books."""

from .check import check_chapter, check_manuscript
from .compile import assemble_manuscript, manuscript_identity, publication_from_manuscript
from .document import ChapterDocument, count_prose_words, parse_chapter_document
from .extract import extract_manuscript

__all__ = [
    "ChapterDocument",
    "assemble_manuscript",
    "check_chapter",
    "check_manuscript",
    "count_prose_words",
    "extract_manuscript",
    "manuscript_identity",
    "parse_chapter_document",
    "publication_from_manuscript",
]
