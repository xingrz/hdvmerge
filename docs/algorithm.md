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

## 3. Lossless concatenation (building)

`build` copies each segment's exact byte range from the original capture and concatenates them.
No re-encode, no remux: every TS stream — including Sony's `0xA1` AUX timecode — is preserved
byte for byte. At each seam the two captures meet with independent, capture-relative continuity
counters (the video CC jumps even though the PCR/PTS, being tape-absolute, stay continuous), so
`build` inserts one AF-only `discontinuity_indicator` marker there (`ts.make_disc_marker`). It is
purely additive — no source byte is touched, and it carries no ES so GOP hashes are unchanged — and
it makes the splice explicit: a decoder resets cleanly, and re-indexing a built file reads its own
seams as signalled rather than as continuity-break damage. `verify` confirms the AUX timecode is
readable at both ends of the result.

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
