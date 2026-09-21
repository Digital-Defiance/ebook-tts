from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from ebook_tts.config import default_config, load_config
from ebook_tts.errors import ManuscriptError
from ebook_tts.manuscript.check import check_chapter, check_manuscript, reconcile_word_counts
from ebook_tts.manuscript.compile import (
    assemble_manuscript,
    manuscript_identity,
    publication_from_manuscript,
    write_compiled_markdown,
)
from ebook_tts.manuscript.document import (
    count_prose_words,
    parse_chapter_document,
    render_chapter_markdown,
    rewrite_header_word_count,
)
from ebook_tts.manuscript.extract import extract_manuscript
from ebook_tts.workspace.manifests import create_plan


def _chapter(
    *,
    words: int | None = None,
    extra: str = "",
    chapter: int = 1,
    title: str = "Noise Floor",
    body: str | None = None,
) -> str:
  prose = body or (
      "The apparatus receives. It has no transmit stage.\n\nA second paragraph follows."
  )
  declared = count_prose_words(prose) if words is None else words
  header = {
      "chapter": chapter,
      "title": title,
      "words": declared,
      "status": "draft",
  }
  return render_chapter_markdown(header=header, prose=prose + extra)


def _manuscript_config(root: Path):
  base = default_config()
  return replace(
      base,
      project=replace(base.project, source="manuscript"),
      manuscript=replace(base.manuscript, root=str(root)),
      book=replace(base.book, title="Authored Book", authors=("Example Author",)),
  )


def _write_manuscript(root: Path, *chapters: str) -> None:
  chapters_dir = root / "chapters"
  chapters_dir.mkdir(parents=True)
  (root / "front-matter.md").write_text("# Title\n\n", encoding="utf-8")
  (root / "back-matter.md").write_text("", encoding="utf-8")
  for index, text in enumerate(chapters, start=1):
    (chapters_dir / f"{index:03d}-chapter.md").write_text(text, encoding="utf-8")


def test_word_count_mismatch_is_an_objective_defect() -> None:
  document = parse_chapter_document(_chapter(words=3), item="ch.md")
  findings = check_chapter(document)
  assert any(item.code == "CHAPTER_WORD_COUNT_MISMATCH" for item in findings)


def test_scene_break_dashes_are_not_a_second_header() -> None:
  body = _chapter() + "\n---\n\nThe next scene begins.\n"
  document = parse_chapter_document(body, item="ch.md")
  assert document.prose_body is not None
  assert "The next scene begins." in document.prose_body
  assert not any(item.code == "CHAPTER_HEADER_SECOND_BLOCK" for item in document.diagnostics)


def test_second_header_block_is_incomplete() -> None:
  text = _chapter() + "\n---\nchapter: 2\n"
  document = parse_chapter_document(text, item="ch.md")
  assert document.prose_body is None
  assert document.diagnostics[0].code == "CHAPTER_HEADER_SECOND_BLOCK"


def test_manuscript_round_trip_and_publication(tmp_path: Path) -> None:
  root = tmp_path / "manuscript"
  _write_manuscript(root, _chapter())
  config = _manuscript_config(root)
  report = check_manuscript(root, config)
  assert report.ok
  compiled = assemble_manuscript(root, config)
  assert "# Chapter 1" in compiled
  assert "## Noise Floor" in compiled
  publication = publication_from_manuscript(root, config)
  assert publication.metadata.title == "Authored Book"
  assert publication.sections[0].chapter_number == 1
  assert "apparatus" in publication.sections[0].text


def test_extract_writes_chapter_files(epub_factory, tmp_path: Path) -> None:
  epub = epub_factory(include_cover=False, text_repeat=1)
  destination = tmp_path / "from-epub"
  publication = extract_manuscript(epub, destination, default_config())
  written = list((destination / "chapters").glob("*.md"))
  assert len(written) == len(publication.sections) == 2
  document = parse_chapter_document(written[0].read_text(encoding="utf-8"), item=written[0].name)
  assert document.ok
  assert isinstance(document.header["words"], int)


def test_extract_refuses_non_empty_destination(epub_factory, tmp_path: Path) -> None:
  epub = epub_factory(include_cover=False, text_repeat=1)
  destination = tmp_path / "occupied"
  destination.mkdir()
  (destination / "sentinel.txt").write_text("keep", encoding="utf-8")
  with pytest.raises(ManuscriptError, match="non-empty"):
    extract_manuscript(epub, destination, default_config())


def test_extract_edit_plan_round_trip(epub_factory, tmp_path: Path) -> None:
  epub = epub_factory(include_cover=False, text_repeat=1)
  destination = tmp_path / "from-epub"
  extract_manuscript(epub, destination, default_config())
  config = replace(
      _manuscript_config(destination),
      tts=replace(
          default_config().tts,
          provider="fake",
          voice_id="test-voice",
          max_characters=9_500,
      ),
      sections=replace(default_config().sections, announce_titles=False),
  )
  original = manuscript_identity(destination, config)
  chapter = next((destination / "chapters").glob("*.md"))
  document = parse_chapter_document(chapter.read_text(encoding="utf-8"), item=chapter.name)
  assert document.prose_body is not None
  edited = document.prose_body.rstrip() + "\n\nAn editorial clarification lands here.\n"
  chapter.write_text(
      render_chapter_markdown(
          header={
              **document.header,
              "words": count_prose_words(edited),
          },
          prose=edited,
      ),
      encoding="utf-8",
  )
  edited_identity = manuscript_identity(destination, config)
  assert edited_identity != original
  publication = publication_from_manuscript(destination, config)
  assert "editorial clarification" in publication.sections[0].text
  plan = create_plan(publication, config, tmp_path / "workspace")
  assert plan.manifest["source"]["sha256"] == edited_identity
  assert publication.source_sha256 == edited_identity


def test_duplicate_chapter_numbers_fail_closed(tmp_path: Path) -> None:
  root = tmp_path / "manuscript"
  _write_manuscript(
      root,
      _chapter(chapter=1, title="First"),
      _chapter(chapter=1, title="Also First"),
  )
  report = check_manuscript(root, _manuscript_config(root))
  assert not report.ok
  assert any(item.code == "CHAPTER_NUMBER_DUPLICATE" for item in report.diagnostics)


def test_missing_chapters_directory_is_a_hard_error(tmp_path: Path) -> None:
  root = tmp_path / "manuscript"
  root.mkdir()
  with pytest.raises(ManuscriptError, match="chapters directory is missing"):
    check_manuscript(root, _manuscript_config(root))


def test_empty_chapters_directory_is_a_hard_error(tmp_path: Path) -> None:
  root = tmp_path / "manuscript"
  (root / "chapters").mkdir(parents=True)
  with pytest.raises(ManuscriptError, match="No chapter markdown"):
    check_manuscript(root, _manuscript_config(root))


def test_announce_titles_prefix_section_text(tmp_path: Path) -> None:
  root = tmp_path / "manuscript"
  _write_manuscript(root, _chapter())
  config = replace(
      _manuscript_config(root),
      sections=replace(default_config().sections, announce_titles=True),
  )
  publication = publication_from_manuscript(root, config)
  text = publication.sections[0].text
  assert text.startswith("Chapter 1")
  assert "Noise Floor" in text.splitlines()[2]


def test_write_compiled_markdown(tmp_path: Path) -> None:
  root = tmp_path / "manuscript"
  _write_manuscript(root, _chapter())
  output = tmp_path / ".build" / "compiled.md"
  written = write_compiled_markdown(root, _manuscript_config(root), output)
  assert written == output
  body = output.read_text(encoding="utf-8")
  assert "# Chapter 1" in body
  assert "apparatus" in body
  assert "words:" not in body


def test_malformed_title_and_chapter_are_defects() -> None:
  body = "One two three four five six seven eight."
  text = render_chapter_markdown(
      header={"chapter": "one", "title": "  ", "words": count_prose_words(body), "status": "draft"},
      prose=body,
  )
  document = parse_chapter_document(text, item="bad.md")
  findings = check_chapter(document)
  codes = {item.code for item in findings}
  assert "CHAPTER_NUMBER_MALFORMED" in codes
  assert "CHAPTER_TITLE_MISSING" in codes


def test_header_delimiter_and_key_defects() -> None:
  missing_open = parse_chapter_document("chapter: 1\n", item="a.md")
  assert missing_open.diagnostics[0].code == "CHAPTER_HEADER_OPEN_DELIMITER_MISSING"

  missing_close = parse_chapter_document("---\nchapter: 1\n", item="b.md")
  assert missing_close.diagnostics[0].code == "CHAPTER_HEADER_CLOSE_DELIMITER_MISSING"

  unknown = parse_chapter_document(
      '---\nchapter: 1\ntitle: "T"\nwords: 1\nstatus: draft\npov: Ravi\n---\nWord.\n',
      item="c.md",
      extra_header_keys=False,
  )
  assert any(item.code == "CHAPTER_HEADER_KEY_UNKNOWN" for item in unknown.diagnostics)

  duplicate = parse_chapter_document(
      '---\nchapter: 1\ntitle: "T"\nwords: 1\nstatus: draft\nchapter: 2\n---\nWord.\n',
      item="d.md",
  )
  assert any(item.code == "CHAPTER_HEADER_KEY_DUPLICATE" for item in duplicate.diagnostics)

  blank = parse_chapter_document(
      '---\nchapter: 1\n\ntitle: "T"\nwords: 1\nstatus: draft\n---\nWord.\n',
      item="e.md",
  )
  assert any(item.code == "CHAPTER_HEADER_LINE_MALFORMED" for item in blank.diagnostics)


def test_config_accepts_manuscript_and_local_tables(tmp_path: Path) -> None:
  path = tmp_path / "audiobook.toml"
  path.write_text(
      """
[project]
source = "manuscript"
[tts]
provider = "local"
voice_id = "narrator"
model_id = "mlx-community/fish-audio-s2-pro"
max_characters = 200000
[tts.local]
reference_wav = "voices/narrator.wav"
anchor = true
[qa]
stt_provider = "local"
spoken_gate = true
""",
      encoding="utf-8",
  )
  config = load_config(path)
  assert config.project.source == "manuscript"
  assert config.tts.provider == "local"
  assert config.tts.local.anchor is True
  assert config.qa.spoken_gate is True
  assert config.tts.max_characters == 200_000


def test_rewrite_header_word_count_touches_only_the_words_line() -> None:
  prose = "One two three four five."
  text = (
      "---\n"
      "chapter: 7\n"
      'title: "A Title: With Punctuation"\n'
      "words: 999\n"
      "status: draft\n"
      "pov_id: POV-NIA\n"
      'hook: "words: 4 appears inside this hook"\n'
      "---\n"
      f"{prose}\n"
  )
  rewritten = rewrite_header_word_count(text, count_prose_words(prose))
  assert "words: 5\n" in rewritten
  assert "words: 999" not in rewritten
  # Every other header line, and the prose, survive byte for byte.
  assert 'title: "A Title: With Punctuation"' in rewritten
  assert "pov_id: POV-NIA" in rewritten
  assert 'hook: "words: 4 appears inside this hook"' in rewritten
  assert rewritten.endswith(f"{prose}\n")
  document = parse_chapter_document(rewritten, item="ch.md")
  assert not check_chapter(document)


def test_rewrite_header_word_count_ignores_a_words_line_in_the_prose() -> None:
  prose = "words: 4321 is a line of prose, not a header."
  text = f'---\nchapter: 1\ntitle: "T"\nwords: 0\nstatus: draft\n---\n{prose}\n'
  rewritten = rewrite_header_word_count(text, count_prose_words(prose))
  assert rewritten.count("words: 4321") == 1
  assert f"words: {count_prose_words(prose)}\n" in rewritten


def test_rewrite_header_word_count_fails_closed_on_malformed_input() -> None:
  with pytest.raises(ManuscriptError, match="opening header delimiter"):
    rewrite_header_word_count("chapter: 1\n", 1)
  with pytest.raises(ManuscriptError, match="closing header delimiter"):
    rewrite_header_word_count("---\nchapter: 1\n", 1)
  with pytest.raises(ManuscriptError, match="no words key"):
    rewrite_header_word_count('---\nchapter: 1\ntitle: "T"\nstatus: draft\n---\nWord.\n', 1)


def test_reconcile_word_counts_repairs_drift_and_is_idempotent(tmp_path: Path) -> None:
  root = tmp_path / "manuscript"
  _write_manuscript(root, _chapter(words=3), _chapter(chapter=2, title="Second"))
  config = _manuscript_config(root)
  assert not check_manuscript(root, config).ok

  observed = count_prose_words(
      parse_chapter_document(
          (root / "chapters" / "001-chapter.md").read_text(encoding="utf-8"),
          item="001-chapter.md",
      ).prose_body
  )
  fixes = reconcile_word_counts(root, config)
  assert len(fixes) == 1
  assert fixes[0].declared == 3
  assert fixes[0].observed == observed
  assert check_manuscript(root, config).ok

  assert reconcile_word_counts(root, config) == ()


def test_reconcile_word_counts_refuses_an_unreadable_chapter(tmp_path: Path) -> None:
  root = tmp_path / "manuscript"
  _write_manuscript(root, _chapter())
  (root / "chapters" / "broken.md").write_text("no header here\n", encoding="utf-8")
  with pytest.raises(ManuscriptError, match="Refusing to reconcile"):
    reconcile_word_counts(root, _manuscript_config(root))


def test_reconcile_word_counts_preserves_extra_header_keys(tmp_path: Path) -> None:
  root = tmp_path / "manuscript"
  chapters = root / "chapters"
  chapters.mkdir(parents=True)
  (chapters / "001.md").write_text(
      '---\nchapter: 1\ntitle: "Kept"\nwords: 1\nstatus: draft\n'
      "movement: adoption\npov_id: POV-MARA\nmode: none\n---\n"
      "Four words of prose.\n",
      encoding="utf-8",
  )
  config = _manuscript_config(root)
  reconcile_word_counts(root, config)
  document = parse_chapter_document(
      (chapters / "001.md").read_text(encoding="utf-8"), item="001.md"
  )
  assert document.header["words"] == 4
  assert document.header["pov_id"] == "POV-MARA"
  assert document.header["movement"] == "adoption"
  assert document.header["mode"] == "none"
