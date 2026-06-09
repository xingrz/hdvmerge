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
damage the transport layer can't see (`scan --decode`). Detection only — never the merge.

## How it works

HDV/DV capture over FireWire is a **bit-exact** copy of the MPEG-2 stream on the
tape, so the same tape GOP yields identical compressed bytes in every capture.
hdvmerge hashes each GOP and uses that hash as a frame-accurate, metadata-free
coordinate — to find overlaps, to verify every seam is tape-adjacent, and to route
around damage.

```
   overlapping captures (.m2t, read-only)
        │
  index  per capture, cache its GOP table next to it: <capture>.idx.jsonl
  │      (content hash, continuity breaks, TEI, recording time). The one expensive
  │      step — rebuilt only when a file's content changes (idempotent).
        ▼
  derive align all captures onto one tape axis by hash (auto-orders the files) and
  │      pick each tape GOP's cleanest copy. Cheap; always recomputed from the indices.
        │
        ├─►  report   the re-capture list: damage with no clean copy, by recording time
        │
        └─►  merge    copy the exact byte ranges into one file — pure concatenation,
                      every stream (incl. the 0xA1 AUX timecode) preserved.   merged.m2t
```

Indexing is the only costly work, so it is cached per file next to the capture and reused
untouched when the file is unchanged — analyse once, re-derive freely. Re-captured a bad spot?
Drop the new file in and re-run: only the new file is indexed, the patch lands by content hash,
and the re-capture list shrinks.

## Install

Python 3.9+. The core needs no third-party packages. ffmpeg is optional — only the `--decode`
damage-detection pass uses it (never the merge).

```sh
pip install -e .          # provides the `hdvmerge` command
```

## Use

```sh
hdvmerge report CLIP-*.m2t                    # analyse: index (cached) + print the re-capture list
hdvmerge merge  CLIP-*.m2t -o merged.m2t      # same, then build the file (pure byte concat)
```

The loop: `report` to see what needs re-capturing → re-capture those spots → drop the new files
in → `report` again (only the new file is indexed) until the list is empty (or you accept it) →
`merge`. Add `--decode` to either to also run the ffmpeg intra-frame damage pass.

## Commands

| Command | Purpose |
| --- | --- |
| `report INPUT…` | Index each capture (cached as `<capture>.idx.jsonl`, rebuilt only on change), align them, and print the re-capture list. `-o FILE` also writes the Markdown report; `--decode` adds the ffmpeg intra-frame damage pass. |
| `merge INPUT… -o merged.m2t` | The same analysis, then build the merged file by pure byte concatenation and write the report beside it. `--decode` as above. |

## What you get

- One continuous file from many overlapping captures, cut only at GOP boundaries
  and joined where the content is provably tape-adjacent (hash-verified seams).
- Mid-file damage is routed around automatically wherever an overlapping capture
  has a clean copy of that GOP — including a continuity break that swallowed
  several GOPs, recovered from the other capture.
- Every TS stream is byte-preserved, so Sony's `0xA1` AUX recording timecode is
  intact — `merge` self-verifies it is still readable at both ends of the output.
- A **re-capture list**: the exact spots where no capture has a clean copy, each
  labelled with the camera's real recording time (read from the AUX stream, not
  extrapolated — the recording clock is not linear with tape position when the
  original recording was paused).

## Limitations

- Without `--decode`, damage is detected from TS structure (continuity breaks,
  transport-error flags) and, in overlaps, from byte-level disagreement between two
  clean copies (reported as `divergences`). Intra-frame bitstream damage that leaves
  the TS structure intact in a single-copy region is then invisible — pass `--decode`
  (an ffmpeg decode pass) to catch it, or re-capture to create an overlap.
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
