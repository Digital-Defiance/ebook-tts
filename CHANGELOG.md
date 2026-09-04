# Changelog

All notable changes follow [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
