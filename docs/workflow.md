# Workflow and artifact lifecycle

## State graph

```text
EPUB → inspect → configuration → immutable plan
                                  ↓
                         approved voice sample
                                  ↓
                     generation run + checkpoints
                                  ↓
                    final tagged MP3 track directory
                                  ↓
                 structural/signal/STT quality report
                                  ↓
                 tracks | BookPlayer ZIP | archive ZIP

legacy-v1 tree + source EPUB → offline verify/adopt → verification-only plan + run
                                                        ↓
                                             local validation/package
```

`inspect` is read-only. `plan` creates deterministic text and chunk files but
makes no provider calls. `sample`, `generate`, and Scribe-enabled `validate`
may incur provider charges and say so in their help/output.

## Inspect

`ebook-tts inspect BOOK.epub` validates the ZIP container, OPF, spine,
navigation, selected text, cover, and metadata. It reports exactly which
narratable units would become tracks. Fix unexpected sections with
`audiobook.toml`; do not discover them after a paid run.

## Plan

A plan ID fingerprints the EPUB hash, metadata/selection overrides,
normalization rules, extraction/chunker versions, model ID, and maximum chunk
size. Plans live under `WORKSPACE/plans/<plan-id>/` and are immutable. A changed
chunk size or correction creates another plan rather than deleting paid work.
Before any paid operation, the manifest and every referenced full-text, chunk,
and cover artifact are path-confined and checked against their recorded hashes,
counts, ordering, and token sequence.

Plan contents:

- `plan.json`: provenance and ordered track/chunk records
- `text/`: normalized full-track narration
- `chunks/`: exact request text
- `cover.*`: discovered cover, when present

## Sample and approval

`sample create` chooses the longest track by default and generates at most 1,000
characters. The sample uses the same voice, model, output format, and settings
as the full run. After listening, `sample approve` records the user's explicit
acceptance. Changing any relevant setting invalidates that approval for the new
configuration.

## Generate

A generation run ID fingerprints the plan and paid-request settings: provider,
voice, model, output format, context, and voice settings. Installed SDK version
and local assembly policy are recorded separately. Generation is sequential
within each track, resumable, and protected by a workspace lock.

Before a request, the tool durably records `*.attempt.json`. Audio streams to a
size-limited hidden partial. A complete stream is atomically renamed, decoded,
probed, hashed, and given a sidecar. Only then is the attempt marker removed.
Final track assembly stream-copies validated MP3 chunks and verifies complete
decode and duration.

Each native final checkpoint also stores a content-derived assembly receipt that
binds plan/run/section identity, exact ordered request/text/audio chunk hashes
and media, output format, metadata intent, and verified cover identity. Resume
recomputes that receipt and rechecks every sidecar, chunk, and final byte before
skipping work. A stale, tampered, symlinked, or uncheckpointed final MP3 fails
closed before another provider request.

Partial track selection is available through `--tracks 1,3-5`. Packaging still
requires every planned track.

## Adopt legacy-v1 output

`adopt` is a strictly local migration path for an already complete legacy-v1
build:

```bash
ebook-tts adopt LEGACY_ROOT --source SOURCE_EPUB \
  --workspace NEW_WORKSPACE
```

`LEGACY_ROOT/chapters_output` is used by default; `--output-dir` can select a
different contained legacy output directory. The destination must be absent or
empty, outside the legacy tree. The importer validates the source EPUB with the
hardened container reader, recomputes legacy plan, generation, request, text,
and audio identities, fully probes and decodes all media, checks optional flat
ZIP bytes, and refuses active attempt/partial evidence. It stages beside the
destination, fsyncs and verifies byte copies, then atomically publishes without
modifying legacy input.

The aggregate current plan retains legacy chunker version 2 and the exact
track/chunk mapping. Chunk and final MP3 files remain byte-identical; adoption
does not re-extract narration, rechunk, remux, retag, assemble, construct a
provider, or invoke STT. Quarantined evidence remains inactive in private
provenance. Adopted plans/runs are verification-only, so `sample` and `generate`
fail with instructions to create a fresh native plan for regeneration.

## Validate

Validation creates `runs/<run-id>/qa/<qa-id>/report.json` and `report.html`.
The QA ID includes the run ID, exact canonical `run.json` fingerprint, and QA
configuration. With `--stt none` (the default), validation is fully local and
always rechecks planned/generated chunk count and order, reference hashes,
chunk hashes and complete decodes, final hashes and complete decodes,
final-versus-chunk duration, and signal metrics. Native runs must match their
assembly receipts; adopted runs must match their plan/run/adoption provenance.

Scribe transcripts are cached by audio/reference/provider/model hashes and do
not alter paid generation manifests. `--stt elevenlabs` is an explicit paid
network operation and is never invoked by adoption or default local validation.

## Package

Packaging holds the workspace lock, re-verifies the plan, reloads the selected
run, validates final checkpoints and current audio, and requires a structurally
valid QA report tied to the exact run manifest and every current track by
default. Output names are derived from the exact ordered member set. Existing
ZIPs and track directories are reused only after every expected name, size, and
SHA-256 is rechecked; extra, missing, symlinked, stale, or corrupt members fail.

Use `--allow-unvalidated` only for deliberate intermediate exports.
`--allow-failed-qa` is an explicit exceptional override and is recorded only by
shell history, not as a claim that the book passed.

## One-command builds

`build` composes the same public stages; it does not use a hidden alternate
pipeline. If QA fails, package creation is skipped.
