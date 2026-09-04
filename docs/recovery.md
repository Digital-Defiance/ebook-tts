# Paid-request recovery

## Why requests fail closed

A timeout only proves that the client did not receive a complete response. The
provider may still have accepted and billed the request. Automatically retrying
can duplicate charges and produce two performances for the same source text.
Generation and paid STT therefore write durable attempt evidence before network
work and preserve it on ambiguous outcomes.

## Safe automatic cases

A matching complete **chunk** audio file is decoded, probed, hashed, and reused.
If a crash occurred after the atomic chunk rename but before its sidecar, the
request hash in the filename plus local validation reconstructs the sidecar
without an API call. Final tracks use a stricter assembly receipt; an existing
final MP3 without that committed receipt is never adopted automatically.

Explicit pre-audio HTTP rejections such as invalid credentials, invalid input,
or quota/rate rejection can be corrected and retried after the marker is safely
cleared by the implementation. Status-less failures, server errors, conflicts,
interruptions, partial bytes, empty streams, and local post-request errors remain
blocked.

## Procedure

1. Stop other processes using the workspace.
2. List evidence:

   ```bash
   ebook-tts attempts list WORKSPACE
   ```

3. Open the marker. Record its request hash, provider request ID when available,
   start time, text-character count, and intended output.
4. Check ElevenLabs history/usage for a matching request. Do not compare only by
   filename; use the request ID, time, model, voice, and character count.
5. If a matching history item has audio, recover/download it and verify it before
   considering a retry.
6. Only after confirming that no accepted/billed request exists, authorize:

   ```bash
   ebook-tts attempts authorize-retry WORKSPACE path/to/file.attempt.json \
     --reason 'No matching request in provider history; checked YYYY-MM-DD'
   ```

7. The command acquires the workspace lock, accepts only a relative active marker
   inside the selected run, rejects symlinks/traversal and untrusted partial
   names, writes a prepared authorization record, then moves partial bytes before
   the marker. If publication fails, moved evidence is rolled back. Successful
   evidence remains in a timestamped quarantine with `authorization.json`; it is
   never destroyed or treated as active.
8. Rerun the original command. Exact completed checkpoints are reused.

## Never do this

- Do not delete attempt markers or hidden partials merely to continue.
- Do not rename a random MP3 to the expected request hash.
- Do not edit immutable plans, chunk text, request sidecars, or run IDs.
- Do not run two generators against one workspace.
- Do not change chunk/model/voice settings inside an existing plan/run. Create a
  new plan/run instead.

## Interrupted package or QA work

Packaging and QA hold the workspace lock and publish through temporary files.
Rerun them after interruption. Existing package output is accepted only when all
members re-validate. Completed STT transcripts are cached; ambiguous STT
attempts use the same explicit recovery procedure as TTS.
