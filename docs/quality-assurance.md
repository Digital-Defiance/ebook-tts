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

Raw provider responses and word timestamps are stored only in the private QA
workspace. Generic distribution archives include the summarized report, not raw
transcripts.

Scribe support uses the ElevenLabs batch STT API and word timestamps. See the
[ElevenLabs STT documentation](https://elevenlabs.io/docs/overview/capabilities/speech-to-text).
STT is never part of adoption or default validation. Selecting
`--stt elevenlabs` is an explicit network/provider operation that may be billed.
An attempt marker is written before each uncached request, and ambiguous
failures block automatic resubmission.

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
