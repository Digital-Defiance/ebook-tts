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
- **Run ID:** canonical hash of plan plus generation configuration
- **QA ID:** canonical hash of run plus QA/transcriber configuration

Timestamps and installed SDK versions are recorded for provenance but excluded
from identity hashes where they do not change the semantic request.

## Workspace trust boundary

A workspace receives a marker before managed content. The planner refuses to
adopt a non-empty unmarked directory. Paths written into manifests are relative;
input absolute paths and API keys are not serialized. Derived runs never delete
other plans or runs.

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
plan ID without serializing absolute paths or credentials. The corresponding
run ID still uses the standard stable plan-plus-generation convention.

The EPUB reader does not extract the archive to disk. It validates every member,
then reads bounded resources in place. Hrefs are resolved as POSIX package paths
and cannot escape the container.

## Manifest evolution

All persisted records include `manifest_version` and `kind`. Human-readable
schemas live in `schemas/`. A future incompatible shape increments the version;
it must not silently reinterpret old paid checkpoints. Migrations should create
new records while retaining originals.

## Provider behavior

Provider adapters perform no retries. Retry/billing policy belongs to the
workspace state machine, which has durable evidence. The ElevenLabs adapter uses
a raw response to retain request and billed-character headers while still
streaming response blocks to disk.

## QA independence

QA reads plan/run manifests but does not modify them. A newer STT model or
threshold creates another QA directory, so re-evaluation never invalidates paid
TTS artifacts. Distribution manifests link all four identities without
including source text.
