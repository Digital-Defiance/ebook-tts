# ebook-tts

`ebook-tts` turns a DRM-free EPUB into an ordered, resumable, and validated
MP3 audiobook. It plans every paid request before generation, fails closed when
a provider response may have been billed, evaluates finished audio, and creates
BookPlayer-compatible and generic distribution archives.

> **Status:** alpha. Keep the source EPUB and generated workspace backed up, and
> review a short voice sample before authorizing a full book.

## Features

- Reads `.epub` directly; no manual extraction or fixed `OEBPS` layout
- Discovers OPF metadata, cover art, linear spine, EPUB 3 navigation, EPUB 2 NCX,
  and multiple TOC fragments in one XHTML file
- Rejects encrypted resources, path traversal, symlinks, duplicate ZIP members,
  excessive expansion, suspicious compression, DTDs, and external resource URLs
- Configurable section selection, titles, metadata, and checked text corrections
- Paragraph/sentence-aware chunks with token-conservation checks
- Model profiles: Multilingual v2 defaults to 9,500 of its documented 10,000
  API characters
- Immutable plans whose referenced text, chunks, and cover are re-verified before
  paid work; generation runs retain stable paid-request identities
- Offline legacy-v1 adoption with verified byte-for-byte audio preservation
- Durable pre-request markers and locked, transactional retry authorization after
  ambiguous paid calls
- Native final-assembly receipts bind exact ordered chunks, output settings,
  metadata intent, and cover identity; uncheckpointed finals are never adopted
- Complete audio decode, format, duration, hash, loudness, peak, and silence QA,
  with each report bound to the exact canonical run and audio state
- Optional timestamped ElevenLabs Scribe transcription with WER/CER, long-change
  spans, and protected-term review
- Content-derived BookPlayer ZIP, generic archive ZIP, and extracted track
  outputs with deep verification before existing artifacts are reused
- No network or paid provider calls in the normal test suite

## Requirements

- Python 3.11 or newer
- `ffmpeg` and `ffprobe`
- An ElevenLabs account/API key for generation or Scribe QA
- A voice ID selected by the user; no author- or book-specific voice is shipped

On macOS:

```bash
brew install ffmpeg
```

## Install

Once published:

```bash
pipx install 'ebook-tts[elevenlabs]'
```

From a source checkout:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[elevenlabs,dev]'
```

Check the environment without making an API request:

```bash
ebook-tts doctor
```

## Quick start

```bash
export ELEVENLABS_API_KEY='...'

ebook-tts inspect novel.epub
ebook-tts init novel.epub
```

Edit `audiobook.toml` and set `tts.voice_id`. Then create a free plan:

```bash
ebook-tts plan novel.epub --config audiobook.toml \
  --workspace novel.ebook-tts
```

Generate and listen to a short paid sample:

```bash
ebook-tts sample create novel.ebook-tts --config audiobook.toml
# The command prints the sample ID and audio path.
ebook-tts sample approve novel.ebook-tts SAMPLE_ID
```

Generate or resume the complete book:

```bash
ebook-tts generate novel.ebook-tts --config audiobook.toml
```

Validate the completed audio locally:

```bash
ebook-tts validate novel.ebook-tts --config audiobook.toml --stt none
```

To enable paid Scribe validation, set `qa.stt_provider = "elevenlabs"` or run:

```bash
ebook-tts validate novel.ebook-tts --config audiobook.toml --stt elevenlabs
```

Create both ZIP profiles after QA:

```bash
ebook-tts package novel.ebook-tts \
  --format bookplayer --format archive
```

For deliberate automation, `build` performs plan → generate → validate →
package. It still requires a matching approved sample unless `--yes` explicitly
bypasses that gate:

```bash
ebook-tts build novel.epub --config audiobook.toml --yes
```

## Offline legacy-v1 adoption

A complete legacy-v1 tree can be verified and adopted without planning new
chunks, generating audio, assembling tracks, transcribing audio, loading an API
key, or contacting a provider:

```bash
ebook-tts adopt LEGACY_ROOT \
  --source SOURCE_EPUB \
  --workspace NEW_WORKSPACE
```

The legacy output defaults to `LEGACY_ROOT/chapters_output`; use
`--output-dir LEGACY_OUTPUT` only when that directory has a different location
inside `LEGACY_ROOT`. `NEW_WORKSPACE` must be absent or empty and must not be
inside the legacy tree. Adoption stages a sibling workspace, verifies every
legacy plan, request fingerprint, sidecar, audio hash, complete decode, media
record, final duration, and optional flat ZIP, then atomically publishes it.
The legacy input is never modified.

Adoption preserves legacy chunker version 2 boundaries and copies every chunk
and final MP3 byte-for-byte—there is no remuxing, retagging, or assembly. The
resulting plan and run are marked verification-only. `sample` and `generate`
will reject them; create a fresh native plan from the source EPUB if audio must
be regenerated.

Re-verify adopted audio locally, with no provider call, using:

```bash
ebook-tts validate NEW_WORKSPACE --stt none
```

This always rechecks chunk order, reference and audio hashes, complete chunk and
final decodes, media properties, final-versus-chunk duration, and final-track
signal metrics. Passing `--stt elevenlabs` is a separate, explicit paid network
operation; it is not part of adoption or default local validation.

## Billing safety

The generator writes and fsyncs an attempt marker before every paid request.
Timeouts, interrupted streams, status-less transport failures, and invalid local
checkpoints are treated as potentially billed. The exact request is blocked
until you inspect provider history and explicitly quarantine the evidence:

```bash
ebook-tts attempts list novel.ebook-tts
ebook-tts attempts authorize-retry novel.ebook-tts RELATIVE_ATTEMPT_PATH \
  --reason 'No matching request exists in provider history'
```

Never delete attempt markers merely to make a command continue. See
[`docs/recovery.md`](docs/recovery.md).

## Outputs

- **Tracks:** a directory with ordered tagged MP3s, cover, metadata, checksums,
  distribution manifest, and QA report
- **BookPlayer:** a flat ordinary ZIP containing only ordered tagged MP3 files
- **Archive:** a rooted ZIP containing audio, cover, metadata, checksums,
  distribution manifest, and QA report; extracted book text and raw transcripts
  are intentionally excluded

BookPlayer itself supports ordinary ZIP archives and can turn them into
playlists; this project does not claim a proprietary BookPlayer file format.
See the [BookPlayer project](https://github.com/TortugaPower/BookPlayer).

## ElevenLabs limits

The shipped profile uses a 9,500-character default for
`eleven_multilingual_v2`, below its documented 10,000-character API ceiling.
Limits are model-specific and validated while loading configuration. See the
[ElevenLabs character-limit documentation](https://elevenlabs.io/docs/help-center/product/core-capabilities/text-to-speech/whats-the-maximum-amount-of-characters-and-text-i-can-generate)
and [TTS API reference](https://elevenlabs.io/docs/api-reference/text-to-speech/convert).
The 192 kbps MP3 format may require a higher subscription tier; the portable
default is `mp3_44100_128`.

## What validation can and cannot prove

STT catches omissions, repetitions, and many substitutions. It cannot reliably
prove that accent, emotion, character performance, or every pronunciation is
correct. Sample approval and human review of protected terms and flagged spans
remain part of a responsible release process. See
[`docs/quality-assurance.md`](docs/quality-assurance.md).

## Development

```bash
uv sync --extra dev
uv run pytest
uv build
```

The test suite creates synthetic EPUBs and generated tone MP3s in temporary
directories. It never reads a real book or calls a provider.

See [`docs/index.md`](docs/index.md), [`CONTRIBUTING.md`](CONTRIBUTING.md), and
[`SECURITY.md`](SECURITY.md).

## Legal and privacy

Use only DRM-free content and voices you have the right to process. You are
responsible for book, cover, voice, and generated-audio rights and for the
provider's current terms and retention settings. API keys are read from the
environment and are never written to manifests.

Git ignores the default `audiobook.toml`, dotenv files, `*.ebook-tts/`
workspaces, and `work/` tree. Custom config filenames, custom workspace paths,
and output locations are not automatically protected; add them to your own
ignore rules and inspect staged files before every public commit.

Licensed under the MIT License. BookPlayer and ElevenLabs are third-party names
and are not affiliated with this project.
