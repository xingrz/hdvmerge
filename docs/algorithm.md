# Algorithm

## 1. Alignment by content hash (indexing)

HDV/DV FireWire capture is a bit-exact copy of the tape's MPEG-2 stream, so the same tape GOP
produces identical compressed bytes in every capture. Each GOP's bytes hash to a 64-bit value
(`blake2b-8`) that is therefore a **frame-accurate, metadata-free coordinate** for that tape
position.

To place all captures on one tape axis we anchor greedily: seed with one capture (axis = its
GOP index), then repeatedly add the capture that shares the most hashes with what's already
placed, voting for the integer shift `axis_index − local_index` (the mode wins; ubiquitous
hashes from black/static frames are ignored). This auto-derives the capture order and the
overlaps without any filenames or timestamps. Gaps (axis positions no capture covers) are
reported.

A single per-file shift is only used for the coarse axis/overlap picture. It is **not** trusted
for the merge, because a dropped or duplicated GOP (common around damage) shifts every later GOP
by one — see the next section.

## 2. The greedy walk (planning)

We walk the tape one GOP at a time, emitting `(capture, gop_index)`:

- **Stay** on the current capture while its next GOP is clean (no continuity break / TEI) and
  not within the head/tail `EDGE` margin.
- **Switch** when the next GOP is damaged or we near an edge: locate the same tape position in
  another capture *by content hash* and continue from its clean copy.

Locating by hash (with a one-GOP context check to disambiguate repeats) makes seams robust to
indels: the join is defined by "this GOP is byte-identical in both captures", so a dropped GOP
elsewhere can't slide it. The true next tape GOP is decided by **hash majority** across the
available clean copies, and a damaged candidate is never chosen when a clean one exists — so a
continuity break that swallowed several GOPs in one capture is recovered from another that has
them.

Each emitted run on one capture becomes a byte-range **segment**. Because we cut only at GOP
boundaries and every seam is hash-verified tape-adjacent, concatenating the segments is lossless
and seamless (the planner asserts `bad_seams == 0`).

### Damage handling and residuals

- `cc` (continuity break) and `tei` are reliable TS-level damage flags.
- The decode pass decodes the video with ffmpeg (on by default whenever ffmpeg is on PATH;
  `--no-decode` skips it) and tags GOPs with intra-frame damage (`dec`) the TS layer can't see.
  ffmpeg is used for this detection only — never to build output. It over-reports (a decode error
  cascades onto later byte-clean GOPs), but harmlessly: a GOP whose bytes match a clean copy
  elsewhere is chosen by hash regardless of its `dec` flag, so only genuine single-copy damage
  becomes a residual.
- In an **overlap**, two clean copies that hash differently reveal intra-frame damage even
  without decoding → recorded as a `divergence` for review.
- A `residual` is a tape position where *no* capture has a clean copy — it survives into the
  output and is listed with its real recording time as a re-capture target.

## 3. Seamless concatenation (building)

`build` concatenates each segment's byte range and **re-phases the continuity counters**. No
re-encode, no remux: every byte that carries tape content — the video ES, audio, Sony's `0xA1` AUX
timecode, the PCR — is copied verbatim.

At each seam the two captures meet with independent, capture-relative CC (the video CC jumps even
though the tape-absolute PCR/PTS stay continuous). Rather than leave or mark that break, `build`
adds a constant per-PID offset to the incoming segment so its CC continues from the outgoing one. A
constant offset preserves every internal relationship (payload packets +1, adaptation-only
unchanged, a residual's real damage break kept exactly), so the only thing that changes is the
cross-capture phase that was never tape-faithful. CC lives in the TS header, not the ES, so GOP
hashes are unchanged and a built file re-indexes/re-merges like a raw capture.

The payoff: CC, PCR and PTS are all continuous at every seam, so a decoder runs straight through (no
reset, no leading-B-frame failure) and a re-fed merge shows zero continuity breaks and zero decode
errors at its own seams — it reads back like one capture. After the build, `verify.verify_build`
self-checks the output: AUX timecode survival, CC/TEI integrity (the output's breaks must equal what
the plan emitted — re-phasing adds none), and an ffmpeg decode pass whose every error must land on a
known-damaged GOP. Any deviation fails the build.

## Why open GOPs still splice cleanly

HDV uses open GOPs: a GOP's leading B-frames reference the previous GOP's anchor frame. At a
seam, the previous segment's last GOP **is** that anchor (the seam is tape-adjacent and
byte-identical to what the incoming GOP expects), so decoding continues without artefacts. Only
the very first GOP of the whole output has no prior reference and loses its 2 leading B-frames.

## Patching in a re-capture

Re-capture a residual spot (cover ≥15–20 s of good footage on each side so it can anchor), drop
the file beside the others, and re-run `hdvmerge` (add `-o` once the list is empty to build). Only
the new file is indexed; the patch lands by content hash and the re-capture list shrinks. This is
why the per-capture index is cached and idempotent — the expensive analysis is done once per file,
never redone for the rest.
