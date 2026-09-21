# Authoring and bidirectional editions

ebook-tts can consume a finished EPUB, or it can author a book from markdown
and keep the EPUB and audiobook as derived editions. The two directions meet
at the same immutable plan.

```text
commercial EPUB ──extract──► markdown chapters ◄── author edits
                                │
                                ├─ check (objective header/word-count gate)
                                ├─ compile (accessible EPUB)
                                └─ plan → generate → validate → package
```

The markdown tree is the source of truth in `project.source = "manuscript"`.
The EPUB is the source of truth in `project.source = "epub"`. Audio never
writes back into either; a pronunciation or omission fix is a manuscript (or
normalization) change that produces a new plan.

## Why this is not a Kiro hook

The production novel pipeline bound three jobs to Kiro editor events:

| Kiro trigger | Job | Why it existed |
| --- | --- | --- |
| `PostFileSave` | Chapter-scope word-count/header check | Header `words:` drifted behind a line edit |
| `Stop` | Rebuild EPUB if prose was newer | Editions lag the manuscript |
| `UserPromptSubmit` | Surface pending defects | Save/stop stdout never reached the session |

Those JSON files only run inside Kiro. The same jobs are now ordinary commands:

```bash
ebook-tts check --chapter chapters/001-start.md   # on-save equivalent
ebook-tts compile --if-stale                      # end-of-turn rebuild
ebook-tts status                                  # start-of-session report
ebook-tts hooks install                           # git pre-commit + post-commit
```

`hooks install` writes marked scripts into `.git/hooks/`. It does not change
git config. `pre-commit` fails the commit when a staged chapter's declared
word count no longer matches the prose. `post-commit` rebuilds a stale EPUB
and never fails the commit; build errors wait in `.build/pending-defects.txt`
for `ebook-tts status`.

Editorial judgement stays a human gate. The checker only reports facts:
missing headers, unreadable files, duplicate chapter numbers, and
declared-versus-observed word counts.

## Start from markdown

```bash
ebook-tts init --manuscript manuscript
# edit manuscript/chapters/001-title.md
ebook-tts check
ebook-tts compile --markdown-only    # no pandoc
ebook-tts compile                    # pandoc EPUB + accessibility metadata
ebook-tts plan --workspace book.ebook-tts
```

Chapter files use a restricted `---` header, not YAML. Required keys are
`chapter`, `title`, `words`, and `status`. Extra keys are allowed so a
project can keep POV or motif fields without ebook-tts interpreting them.

A `---` line in the body is a scene break. It is only treated as a second
header when the next line looks like a required header key.

## Start from a commercial EPUB

```bash
ebook-tts extract novel.epub --manuscript manuscript
# edit the chapters, then compile and plan as above
```

Extract writes one markdown file per narratable spine section and does not
bake spoken title announcements into the body. After extract, set
`project.source = "manuscript"` in `audiobook.toml` (the manuscript init
template already does).

## Accessibility editions

Compiled EPUBs get EPUB Accessibility 1.1 package metadata after Pandoc,
because Pandoc cannot emit schema.org accessibility properties. The pass
claims only what is true of a reflowable, DRM-free text edition with a
labelled cover. It does not claim `synchronizedAudioText` or a WCAG level.

## Currency

A manuscript identity hash is the plan's source hash when authoring. Changing
a chapter creates a new plan rather than silently reusing paid or local audio
built from earlier prose. `ebook-tts status` reports when the EPUB is older
than the chapters.
