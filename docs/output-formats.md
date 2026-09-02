# Output formats

All outputs derive from the same validated tagged MP3 tracks. MP3 data is stored
without ZIP compression because recompressing already-compressed audio wastes
CPU and normally increases little or no space. ZIP64 is enabled for books over
4 GiB or with large members.

## Tracks

```text
book-title-tracks-RUNID/
├── audio/001_title.mp3
├── audio/002_chapter_one.mp3
├── cover.jpg
├── metadata.json
├── manifest.json
├── SHA256SUMS
└── qa/report.{json,html}
```

This is the easiest output to inspect, sync, or feed to another packaging tool.

## BookPlayer

```text
book-title-bookplayer-RUNID.zip
├── 001_title.mp3
├── 002_chapter_one.mp3
└── ...
```

This is an ordinary flat ZIP, not a proprietary database. Numeric filenames and
embedded ID3 track numbers make ordering explicit. No sidecar files are included
in this profile. BookPlayer's public project documentation states that ZIP
archives are supported and can become playlists automatically:
https://github.com/TortugaPower/BookPlayer

## Generic archive

```text
book-title-archive-RUNID-QAID.zip
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

## Determinism

ZIP members have fixed timestamps, permissions, ordering, and storage method.
Artifact names include run and QA identities. Rebuilding unchanged inputs on the
same supported Python/ZIP implementation produces stable bytes; the source
hashes and checksums remain authoritative across implementations.

## M4B

M4B is not part of v1. Converting provider MP3 to M4B requires MP3→AAC transcoding
and another lossy encode. A future backend should preferably request or retain a
lossless intermediate and create chapter metadata in one controlled encode.
