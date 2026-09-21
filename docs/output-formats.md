# Output formats

All outputs derive from the same validated tagged MP3 tracks. MP3 data is stored
without ZIP compression because recompressing already-compressed audio wastes
CPU and normally saves little or no space. ZIP64 is enabled for books over 4 GiB
or with large members.

`PACKAGEID` below is the first 20 hexadecimal characters of a digest over every
ordered package member's name, byte count, and SHA-256.

## Tracks

```text
book-title-tracks-PACKAGEID/
├── audio/001_title.mp3
├── audio/002_chapter_one.mp3
├── cover.jpg
├── metadata.json
├── manifest.json
├── SHA256SUMS
└── qa/report.{json,html}
```

This is the easiest output to inspect, sync, or feed to another packaging tool.
The manifest and QA members bind the selected plan, exact run state, and current
track identities unless packaging was explicitly allowed without QA.

## BookPlayer

```text
book-title-bookplayer-PACKAGEID.zip
├── 001_title.mp3
├── 002_chapter_one.mp3
└── ...
```

This is an ordinary flat ZIP, not a proprietary database. Numeric filenames and
embedded ID3 track numbers make ordering explicit. No sidecar, run, or QA files
are included, so this profile's package ID commits only to the ordered MP3
members. BookPlayer's public project documentation states that ZIP archives are
supported and can become playlists automatically:
https://github.com/TortugaPower/BookPlayer

## Generic archive

```text
book-title-archive-PACKAGEID.zip
└── book-title/
    ├── audio/*.mp3
    ├── cover.*
    ├── metadata.json
    ├── manifest.json
    ├── SHA256SUMS
    └── qa/report.{json,html}
```

The archive excludes normalized source text, chunk text, raw provider responses,
and raw transcripts. This avoids accidentally redistributing book content beyond
the finished audio and keeps provider/debug data private.

## Determinism and reuse

ZIP members have fixed timestamps, permissions, ordering, and storage method.
Rebuilding the same member set on the same supported Python/ZIP implementation
produces stable bytes. Across implementations, member hashes and checksums remain
authoritative. Existing ZIPs are reused only after exact member order, names,
sizes, storage flags, and SHA-256 values pass. Existing track directories must
contain exactly the expected non-symlink files and directories with matching
hashes; a checksum file alone is not trusted.

## M4B

```text
book-title-PACKAGEID.m4b
```

A single chaptered audiobook file built from the validated MP3 tracks. Chapters
follow track order and titles. Cover art is embedded when the plan has one.

This is a second lossy encode (MP3→AAC). Prefer the MP3 track packages as the
archival/master outputs; use M4B as a convenience distribution for players that
expect one file with chapter markers.

```bash
ebook-tts package WORKSPACE --format m4b --format bookplayer
```
