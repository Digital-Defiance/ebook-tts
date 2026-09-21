# Quality assurance

Quality assurance is a release gate, not a claim that automated metrics can
replace listening.

## Deterministic checks

Every generated or adopted chunk and final track is checked for:

- Content-addressed request, source-text, and audio hashes
- A complete ffmpeg decode with `-xerror`
- Configured codec, sample rate, and approximate bitrate
- Positive duration and channel count
- Final-track duration consistent with constituent chunks
- Ordered, contiguous track coverage before packaging
- Archive member order, CRC readability, and source hashes

Failures are hard errors. These checks run for every chunk even when STT is
disabled, so `ebook-tts validate WORKSPACE --stt none` is a complete local
structural/media/signal re-verification for native and adopted runs. Adopted
legacy-v1 plans preserve their original chunk boundaries; local QA validates
that mapping rather than running the current chunker.

## Signal analysis

Final tracks are decoded for:

- EBU R128 integrated loudness and loudness range
- True peak and sample peak
- Leading, trailing, and internal silence intervals
- Configured peak and internal-silence review warnings

A peak warning does not by itself prove clipping; it identifies material for
review. Likewise, long silence may be intentional. Signal values remain in the
JSON report so a publisher can define house thresholds later.

## Speech-to-text comparison

When enabled, each generated chunk is transcribed independently. Chunk-sized
validation gives an exact reference mapping and makes transcription reusable.
The evaluator:

1. NFKC/case-normalizes reference and transcript.
2. Removes punctuation differences while retaining Unicode words/apostrophes.
3. Computes exact Levenshtein WER and CER with a bit-parallel implementation.
4. Produces changed spans for human review.
5. Flags long omission/repetition/substitution spans.
6. Checks protected terms only in chunks where each term occurs in the source.
7. Aggregates weighted metrics by track and book.

When `qa.spoken_gate = true`, each chunk also runs a spoken-aware gate that
treats numeral words, clock times, UK/US spelling, and weak function-word ASR
flips as equivalent to the manuscript. Exact WER/CER remain in the report;
the spoken gate is the extra release failure for local engines.

## Diagnosing omissions

The spoken gate reports that words are missing (`max_expected_gap`); it does not
name them or show where to cut the audio. Use `ebook-tts diagnose` after a
failing validate:

```bash
# Diff planned text against a transcript (or QA transcript JSON)
ebook-tts diagnose find --expected planned.txt --heard transcript.txt

# Workspace mode: load planned track/chunk text + cached STT transcript
ebook-tts diagnose find WORKSPACE --track 13 --chunk 1 --audio path/to/track.mp3

# Confirm a splice window reads cleanly (local Whisper)
ebook-tts diagnose seam track.mp3 --start 412.1 --end 418.4

# Confirm repaired WAV/MP3/M4B durations still agree
ebook-tts diagnose formats --reference chapter.wav --mp3 chapter.mp3 \
  --m4b book.m4b --chapter-title "13."
```

`find` prints each non-benign expected-gap (≥5 tokens by default), the surrounding
prose, and—when audio is supplied—an energy-based `--start/--end` suggestion for
a verified phrase patch. It does not repair audio; choose a re-render or an
explicit `[[tts.local.tracks]]` patch after listening.

Raw provider responses and word timestamps are stored only in the private QA
workspace. Generic distribution archives include the summarized report, not raw
transcripts.

Scribe support uses the ElevenLabs batch STT API and word timestamps. Local
Whisper (`qa.stt_provider = "local"`) is an on-device alternative. STT is never
part of adoption or default validation.
An attempt marker is written before each uncached request, and ambiguous
failures block automatic resubmission.

## Live local smoke

Offline tests use fake TTS/STT. To exercise real Fish S2 Pro rendering, voice
anchor discard, and Whisper on Apple Silicon, see
[../tests/live/README.md](../tests/live/README.md).

## Human review

Always review:

- The voice sample before full generation
- Protected names and non-default-language passages
- Every long transcript difference span
- Signal warnings and unusual silence
- Samples around chunk boundaries
- At least one random passage from each final track
- Opening/closing metadata and track transitions in the target player

STT may emit the expected spelling despite an undesirable accent or
pronunciation. It also cannot judge character acting, emotion, pacing, or voice
identity. Those remain human editorial decisions.

## Status

- `pass`: deterministic gates pass and no warnings remain
- `warn`: deterministic gates pass but review findings remain
- `fail`: corruption, missing output, hash mismatch, configured WER/CER breach,
  or another hard gate failed

Packaging accepts `pass` and `warn`; it rejects `fail` and missing reports by
default.
