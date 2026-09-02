# Configuration

Configuration is TOML. `ebook-tts init BOOK.epub` creates a documented starting
file. Unknown keys fail immediately to catch spelling mistakes.

## Book metadata

```toml
[book]
title = "Optional title override"
authors = ["Author One", "Author Two"]
language = "en"
```

Omitted values come from OPF metadata. An absent creator is allowed and is
tagged as `Unknown Author` unless overridden.

## Sections

```toml
[sections]
announce_titles = true
minimum_characters = 1
include = []
exclude = ["*table of contents*", "Text/ads.xhtml"]

[sections.titles]
"Text/ch01.xhtml" = "Chapter One"
"Text/book.xhtml#prologue" = "Prologue"
```

`include` and `exclude` are case-insensitive shell patterns matched against both
discovered titles and source hrefs. Empty `include` means all narratable linear
spine content. Exclusions run after inclusions.

## Text corrections

```toml
[[normalization]]
pattern = "source spelling"
replacement = "spoken spelling"
expected_count = 3

[[normalization]]
pattern = "\\bDr\\. Example\\b"
replacement = "Doctor Example"
regex = true
flags = "i"
```

Rules run in order after NFC Unicode normalization. `expected_count` converts a
silent manuscript change into a planning failure. Regex flags are `i`, `m`, and
`s`. These rules affect hashes and therefore create a new plan.

## TTS

```toml
[tts]
provider = "elevenlabs"
voice_id = "YOUR_VOICE_ID"
model_id = "eleven_multilingual_v2"
output_format = "mp3_44100_128"
max_characters = 9500
context_characters = 500

[tts.voice_settings]
stability = 0.5
similarity_boost = 0.75
```

`ELEVENLABS_VOICE_ID` and `ELEVENLABS_MODEL_ID` supply defaults when the values
are absent. `ELEVENLABS_API_KEY` is environment-only. Known model ceilings are
validated. The v1 assembler accepts MP3 output; other provider formats are
reserved for future output backends.

## Media tools

```toml
[audio]
ffmpeg = "ffmpeg"
ffprobe = "ffprobe"
genre = "Audiobook"
```

Executable names resolve through `PATH`; explicit executable paths are also
accepted.

## Quality assurance

```toml
[qa]
stt_provider = "none"       # or "elevenlabs"
stt_model = "scribe_v2"
language = "en"
max_internal_silence_seconds = 8.0
clipping_peak_db = -0.1
protected_terms = ["Character Name", "Slàinte mhath"]
# max_word_error_rate = 0.08
# max_character_error_rate = 0.04
```

WER/CER are reported without thresholds. Setting a maximum turns an advisory
metric into a release failure. Calibrate thresholds on representative content;
names, numerals, dialect, and foreign-language text can produce valid ASR
errors.
