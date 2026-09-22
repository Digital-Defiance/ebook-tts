# Changelog

All notable changes follow [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Manuscript authoring mode: restricted chapter headers, objective word-count
  checks, EPUB extract/compile, Accessibility 1.1 post-processing, and git
  lifecycle hooks that replace Kiro save/stop/prompt bindings
- Local Fish S2 Pro provider with spoken-numeral transforms, session-turn
  planning, retryable (non-billed) failures, and optional Whisper QA
- Chaptered M4B packaging from validated MP3 tracks (AAC remux with
  ffmetadata chapters; second lossy encode, convenience distribution)
- Opt-in live Fish + Whisper smoke (`pytest -m live`) that verifies voice-anchor
  discard and spoken-gate WER against real on-device audio
- `tts.local.max_words_per_call` for long-context Fish recovery: split a chapter
  into re-anchored generate() calls (~1100 words) without changing the manuscript
- Per-book `[[tts.local.tracks]]` working-config exceptions: chapter/track
  `max_words_per_call`, generation-only `spoken_replace`, and verified audio
  `patches` (PCM splice library) hashed into request identity
- `ebook-tts diagnose find|seam|formats`: turn spoken-gate failures into named
  omission reports, re-check splice windows with Whisper, and confirm
  WAV/MP3/M4B durations agree before shipping a repair
- Inter-chapter comfort pads in M4B packaging (`audio.m4b_chapter_gap_ms`,
  default 2500): V6 room tone from the preceding track when available, silence
  otherwise; pad duration is folded into the preceding chapter marker
- `ebook-tts check --write-counts`: reconcile each chapter's declared `words:`
  header to its observed prose count. Rewrites only that header line, preserves
  every other key verbatim, fails closed on an unparseable header, and is
  opt-in so the drift signal is never silently erased
- `book.cover` configuration, consumed by both the EPUB build and the audio plan
  so one image serves every edition without a remembered `--cover` flag;
  `compile --cover` still overrides it, a configured-but-absent cover fails
  loudly, and `doctor` warns when a cover ships with no `accessibility.cover_alt`
  description
- `ebook-tts chapters`: list manuscript chapters with `--group-by KEY` and
  repeatable `--where KEY=VALUE` over any header key, including author keys the
  tool does not interpret. Supports reading one viewpoint's chapters
  consecutively, which is where voice drift is visible; it gathers and never
  scores prose

## [0.1.0] - 2026-09-04

### Added

- Direct bounded EPUB ingestion and generic publication extraction
- Strict TOML configuration and checked text normalization
- Model-aware immutable chunk plans, including a 9,500-character Multilingual v2 default
- Billing-safe ElevenLabs generation, samples, approval, resumability, and recovery evidence
- Offline legacy-v1 adoption with byte-for-byte preservation and verification-only plans
- ffmpeg assembly, complete decode validation, and objective signal analysis
- Optional cached Scribe STT with exact WER/CER and review spans
- BookPlayer, generic archive, and extracted track outputs
- CLI, manifest schemas, synthetic offline tests, documentation, CI, and release workflow

### Security

- Verify all immutable plan text, chunk, cover, path, count, and token identities before paid work
- Confine retry evidence to the selected run and quarantine it transactionally under the workspace lock
- Bind native final-track checkpoints to exact sections, ordered chunks, output settings, metadata intent, and cover identity
- Refuse to adopt uncheckpointed final MP3 files during native resume
- Bind QA reports to the exact canonical run manifest and current audio/assembly identities
- Re-hash every member before reusing an existing ZIP or track-directory distribution

### Changed

- Package output names now use a digest of their exact member set
- Release automation installs and smoke-tests both wheel and source distribution before publication
- Default local configuration, dotenv files, and `*.ebook-tts/` workspaces are ignored by Git

[Unreleased]: https://github.com/Digital-Defiance/ebook-tts/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Digital-Defiance/ebook-tts/releases/tag/v0.1.0
