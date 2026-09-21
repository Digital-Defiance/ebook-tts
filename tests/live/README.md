# Live local Fish + Whisper smoke

Default `pytest` stays offline. This directory holds opt-in smokes that load
real models and a voice reference you supply.

## Requirements

- Apple Silicon macOS
- `ffmpeg` / `ffprobe`
- `uv sync --extra local --extra dev` (or equivalent)
- A reference `.wav` + matching transcript `.txt` you have the right to clone

Optional integrity pins:

```bash
export EBOOK_TTS_LIVE_REFERENCE_WAV_SHA256=<sha256 of the wav>
export EBOOK_TTS_LIVE_REFERENCE_TEXT_SHA256=<sha256 of the txt>
```

## Run

```bash
export EBOOK_TTS_LIVE_LOCAL=1
export EBOOK_TTS_LIVE_REFERENCE_WAV=/path/to/narrator.wav
export EBOOK_TTS_LIVE_REFERENCE_TEXT=/path/to/narrator.txt
uv run pytest -m live -q
```

First run downloads Fish S2 Pro and Whisper weights into the Hugging Face
cache. Expect several minutes.

## What it asserts

1. Fish produces non-trivial MP3 audio
2. With `anchor=true`, the first generated segment is discarded and has energy
3. Whisper transcript of the **delivered** audio does not contain distinctive
   voice-anchor phrases
4. The spoken gate passes against the verbalized chapter text
5. Protected name tokens from the chapter are present in the transcript
6. With `max_words_per_call` set low enough to force two parts, each part
   discards its own anchor and the joined chapter still passes Whisper QA
