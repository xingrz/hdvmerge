# hdvmerge

Detect damage in, align, and losslessly merge **overlapping HDV (Sony MPEG-TS)
tape captures** into one continuous file. When a worn HDV/DV tape reads poorly you
capture it in several passes — stop, rewind a little, and retry whenever a glitch
shows on screen — ending up with several `.m2t` files that overlap and each carry
some errors. hdvmerge finds the damage, works out how the captures line up, and
stitches the good parts together.

It rebuilds the file **without ever re-encoding or remuxing**. The merge is byte-level
188-byte transport-stream manipulation; **ffmpeg is never used to build output**, because
even `ffmpeg -c copy` strips Sony's private AUX stream (`stream_type 0xA1`, usually
PID `0x811`) — the stream that carries the camera's recording date/time. That timecode is
preserved exactly and is used to label exactly which spots, if any, still need a re-capture.
ffmpeg *is* used, optionally, for one thing: decoding the video to **detect** intra-frame
damage the transport layer can't see (the decode pass). Detection only — never the merge.

## How it works

HDV/DV capture over FireWire is a **bit-exact** copy of the MPEG-2 stream on the
tape, so the same tape GOP yields identical compressed bytes in every capture.
hdvmerge hashes each GOP and uses that hash as a frame-accurate, metadata-free
coordinate — to find overlaps, to verify every seam is tape-adjacent, and to route
around damage.

```
   overlapping captures (.m2t, read-only)
        │
      index    per capture, cache its GOP table next to it: <capture>.idx.jsonl
        │      (content hash, continuity breaks, TEI, recording time). The one expensive
        │      step — rebuilt only when a file's content changes (idempotent).
        ▼
      derive   align all captures onto one tape axis by hash (auto-orders the files) and
        │      pick each tape GOP's cleanest copy. Cheap; always recomputed from the indices.
        ▼
      report the re-capture list: damage with no clean copy, by recording time
        │
        └─►  with -o   copy the exact byte ranges into one file — byte concatenation,
                       every stream (incl. the 0xA1 AUX timecode) preserved, a small
                       discontinuity marker added at each seam.               merged.m2t
```

Indexing is the only costly work, so it is cached per file next to the capture and reused
untouched when the file is unchanged — analyse once, re-derive freely. Re-captured a bad spot?
Drop the new file in and re-run: only the new file is indexed, the patch lands by content hash,
and the re-capture list shrinks.

## Install

Python 3.9+. The core needs no third-party packages. ffmpeg is optional — only the intra-frame
decode damage-detection pass uses it (never the merge); without it that pass is simply skipped.

```sh
pip install -e .          # provides the `hdvmerge` command
```

## Use

```sh
hdvmerge CLIP-*.m2t                    # analyse: index (cached) + print the re-capture list
hdvmerge CLIP-*.m2t -o merged.m2t      # same, then build the merged file (byte concat + seam markers)
```

The loop: run it to see what needs re-capturing → re-capture those spots → drop the new files in
→ run it again (only the new file is indexed) until the list is empty (or you accept it) → add
`-o merged.m2t` to build. The ffmpeg intra-frame damage pass runs automatically when ffmpeg is on
PATH; pass `--no-decode` to skip it.

## Options

| Flag | Purpose |
| --- | --- |
| `INPUT…` | Capture files or a directory of them. Each is indexed (cached as `<capture>.idx.jsonl`, rebuilt only on change); all are then aligned and the re-capture list is printed. |
| `-o FILE` | Also build the merged file at `FILE` by byte concatenation (one discontinuity marker inserted per seam; no source byte modified), and write `FILE.report.md` beside it. |
| `--no-decode` | Skip the ffmpeg intra-frame decode detection pass (otherwise on whenever ffmpeg is available; detection only, never affects the merged bytes). |
| `--index-dir DIR` | Store and read index caches in `DIR` (keyed by file name) instead of beside each capture. |
| `--no-index` | Don't read or write any index cache; build the index in memory each run. |

## What you get

- One continuous file from many overlapping captures, cut only at GOP boundaries
  and joined where the content is provably tape-adjacent (hash-verified seams).
- Mid-file damage is routed around automatically wherever an overlapping capture
  has a clean copy of that GOP — including a continuity break that swallowed
  several GOPs, recovered from the other capture.
- Every source TS byte is preserved (only an AF-only discontinuity marker is added
  at each seam), so Sony's `0xA1` AUX recording timecode is intact — `merge`
  self-verifies it is still readable at both ends of the output.
- A **re-capture list**: the exact spots where no capture has a clean copy, each
  labelled with both the camera's real recording time *and* the tape SMPTE timecode
  to cue on the deck (both read from the AUX stream, never extrapolated — the
  recording clock is not linear with tape position).

## Limitations

- With `--no-decode` (or no ffmpeg on PATH), damage is detected from TS structure
  (continuity breaks, transport-error flags) and, in overlaps, from byte-level disagreement
  between two clean copies (reported as `divergences`). Intra-frame bitstream damage that
  leaves the TS structure intact in a single-copy region is then invisible — the decode pass
  (ffmpeg, on by default) catches it, or re-capture to create an overlap.
- The decode pass over-reports a little: a decode error can cascade onto later,
  byte-clean GOPs. That is harmless to the output — a GOP whose bytes match a clean
  copy elsewhere is chosen by hash regardless — and genuine single-copy damage still
  surfaces as a residual.
- v1 builds 188-byte TS (and reads 192-byte M2TS). Mixing framings in one merge is
  not supported.

## Documentation

- [docs/hdv-internals.md](docs/hdv-internals.md) — HDV/MPEG-TS byte-level reference.
- [docs/algorithm.md](docs/algorithm.md) — alignment, the greedy walk, why seams are safe.
- [docs/formats.md](docs/formats.md) — the `<capture>.idx.jsonl` index format.

## Development

```sh
python -m unittest discover         # from the repo root; no install needed
```

Tests are deterministic and build tiny synthetic transport streams in
[tests/fixtures.py](tests/fixtures.py) — no sample captures required.
