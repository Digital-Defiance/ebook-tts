# Architecture

## Boundaries

- `epub/`: bounded ZIP access and publication metadata/spine/navigation parsing
- `text/`: visible XHTML extraction, checked normalization, and chunking
- `workspace/`: immutable plans, locks, paid checkpoints, runs, samples, recovery,
  and offline legacy-v1 adoption
- `providers/`: provider-neutral protocols and ElevenLabs adapters
- `media/`: executable preflight, probing, complete decoding, and MP3 assembly
- `qa/`: signal metrics, STT caching, edit alignment, JSON/HTML reports
- `outputs/`: QA-gated distribution directories and ZIP profiles
- `cli.py`: thin composition of the same public library functions

No module depends on a particular book, expected chapter count, fixed EPUB path,
or built-in voice.

## Identifiers

- **Plan ID:** canonical hash of source identity and every extraction,
  normalization, selection, model-limit, and chunk-boundary input
- **Request ID (local):** canonical hash of text, context, model, voice, output,
  and voice settings
- **Run ID:** canonical hash of the plan plus paid-request identity; installed SDK
  version and local final-assembly policy do not change the paid request
- **Assembly ID:** canonical hash of plan/run/section identity, ordered verified
  chunks and media, output profile, metadata intent, and cover identity
- **QA ID:** canonical hash of the run ID, exact canonical run-manifest hash, and
  QA/transcriber configuration
- **Package ID:** canonical hash of every ordered member name, size, and SHA-256

Timestamps and installed SDK versions are provenance, not semantic identity.
Assembly policy remains separately persisted and verified so local packaging
changes never silently cause another paid request.

## Workspace trust boundary

A workspace receives a marker before managed content. The planner refuses to
adopt a non-empty unmarked directory. Plan manifests and every referenced text,
chunk, and cover artifact are path-confined and re-hashed before paid work.
Input absolute paths and API keys are not serialized. Derived runs never delete
other plans or runs.

Native generation stores an attempt marker before each provider call. Explicit
retry authorization is workspace-locked, accepts only a relative active marker
inside the selected run, rejects symlinks and traversal, moves partial bytes
before the marker, and rolls back on publication failure. A completed native
track is resumable only when its final bytes and full assembly receipt still
match; an uncheckpointed final MP3 is never promoted to authority.

Legacy adoption uses a separate stricter publication boundary: the destination
must be absent or empty and outside the read-only legacy tree. Validation occurs
before publication in an importer-owned sibling staging directory. Audio is
stream-copied with source and destination hashing plus fsync, then the complete
workspace is atomically renamed into place. Failures remove only that staging
directory. Legacy path references must remain contained and may not traverse
symbolic links; active attempt/partial evidence blocks adoption, while
quarantined evidence is inventoried but never activated.

Adoption does not call the native extractor, normalizer, chunker, generator, or
assembler. It records legacy chunker version 2 and exact legacy boundaries in a
new aggregate plan. A deterministic adoption record binds source, legacy plan
and generation fingerprints, verified artifact inventory, and counts into the
plan ID without serializing absolute paths or credentials. Adopted audio remains
byte-identical and is explicitly verification-only.

The EPUB reader does not extract the archive to disk. It validates every member,
then reads bounded resources in place. Hrefs are resolved as POSIX package paths
and cannot escape the container.

## Manifest evolution

All persisted records include `manifest_version` and `kind`. JSON Schemas in
`schemas/` describe the emitted v1 record structure; runtime path confinement,
content hashing, cross-record identity, media probing, and semantic checks remain
the authoritative validators. A future incompatible shape increments the
version and must not silently reinterpret old paid checkpoints.

## Provider behavior

Provider adapters perform no retries. Retry/billing policy belongs to the
workspace state machine, which has durable evidence. The ElevenLabs adapter uses
a raw response to retain request and billed-character headers while still
streaming response blocks to disk.

## QA and distribution independence

QA reads plan/run manifests but does not modify them. A newer STT model or
threshold creates another QA directory, and the QA identity includes the exact
run state it evaluated. Packaging reloads that state under the workspace lock,
requires a matching report unless explicitly overridden, and deeply verifies
existing outputs before reuse. Distribution manifests never include source text.
