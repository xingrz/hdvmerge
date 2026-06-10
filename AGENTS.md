# Working context for agents

`hdvmerge` merges overlapping HDV tape captures (Sony MPEG-TS, `.m2t`). A worn tape
is captured in several passes — stop/rewind/retry whenever a glitch appears — so the
files overlap and each carries some damage. The job: detect the damage, align the
captures by content, and stitch the good parts into one continuous file.

## The load-bearing idea

HDV/DV capture over FireWire copies the tape's MPEG-2 **elementary stream** bit-for-bit, so the
same tape GOP has byte-identical ES across captures. (The *full* TS bytes are not identical — the
4-bit continuity_counter is regenerated per capture pass, so two captures of one tape GOP have the
same ES but different CC.) Hash each GOP's ES → a frame-accurate, metadata-free coordinate.
Alignment, overlap detection, and seam-adjacency checks are all hash equality; **never** assume
"capture's GOP *j* = tape GOP *j* + a fixed offset" — a dropped/duplicated GOP (common at damage)
shifts that, and the hash-defined seams are what make the merge robust to it.

## The hard constraint

**The merge is byte-level 188-byte TS manipulation; ffmpeg must never build output.** Even
`ffmpeg -c copy` strips Sony's private AUX stream (`stream_type 0xA1`, usually PID `0x811`),
which carries the camera recording date/time — only GOP-level byte copying preserves it. Every
byte that carries tape content — the video ES, audio, the `0xA1` AUX timecode, and the **PCR** (the
tape's real clock) — is copied verbatim. The *one* field `build` rewrites is the continuity_counter
(see "Things worth knowing"): it is capture plumbing, not tape data, so re-phasing it to make the
output seamless loses nothing. After a `-o` build, `verify.verify_build` self-checks the output
(AUX timecode survival + CC/TEI integrity + an ffmpeg decode pass) — see Invariants.

ffmpeg **is** allowed for *detection only* (the decode pass, on by default whenever ffmpeg is on
PATH): decoding the video to flag intra-frame damage the TS layer can't see. Third-party Python
deps are allowed too; the core simply doesn't need any (pure-Python scanning is already ~300 MB/s).

## Architecture

The one expensive step is **indexing**, cached per capture as `<capture>.idx.jsonl` (beside the
source, or together under `--index-dir`; `--no-index` skips the cache entirely) and rebuilt only
when the file's content changes (idempotent — a content fingerprint, no mtime, no timestamps/abs-
paths inside, sorted keys). Everything downstream is cheap and re-derived from the indices every
run, so there is no second persisted artifact — just the indices, the merged `.m2t`, and the human
Markdown report.

One command, which always ensures indices first; `-o` is the only thing that escalates analysis
into a build:

```
hdvmerge INPUT…              -> indices (cached) + the Markdown re-capture report   (no build)
hdvmerge INPUT… -o out.m2t   -> same, then build out.m2t (seamless, self-checked) + report
```

Internally three stages, exposed as a library rather than separate commands:
`scan.analyze` (index + align) → `plan.build_plan` (greedy walk → segments + residuals +
divergences) → `build.build` (the ONLY writer of output bytes). `report.render` turns a plan
into Markdown; `verify.verify` checks AUX survival (run after a `-o` build).

## Layout

```
src/hdvmerge/
  __init__.py   constants (SYNC, TS, start codes), __version__
  ts.py         byte-level TS primitives — the shared core (framing, packet fields, with_cc)
  psi.py        PAT/PMT -> video PID + Sony AUX PID, multi-packet section reassembly
  aux.py        Sony AUX recording date/time decode (the 63 .. c0 .. ff anchor)
  gop.py        MPEG-2 GOP splitter + content hash (streams in constant space)
  model.py      FileIndex (persisted, JSONL) + in-memory Report/Segment/Plan; index I/O
  scan.py       fingerprint + idempotent per-file index cache + greedy hash alignment -> Report
  probe.py      optional ffmpeg decode-damage detection (detection only, never the merge)
  plan.py       greedy indel-proof walk -> Plan (segments + residuals + divergences)
  build.py      byte-concat + CC re-phasing (the only writer of output bytes)
  report.py     Plan -> human Markdown report;  verify.py  AUX survival;  cli.py  the command
tests/  fixtures.py + test_*.py   (synthetic TS, no sample data needed)
docs/   hdv-internals.md  algorithm.md  formats.md
```

The byte-level primitives live in **one** place (`ts.py`/`psi.py`/`aux.py`/`gop.py`) and
are imported everywhere — do not fork copies into the stage modules.

## Things worth knowing about the data

- **GOP timecode is zero.** Sony HDV writes `00:00:00:00` into the MPEG GOP header
  timecode; the real clock is only in the AUX stream. Align by content hash, not GOP TC.
- **The AUX `0xA1` anchor carries two clocks** (both via `aux.parse_aux`, both stored per GOP):
  the **wall-clock** date/time (`rec`, second resolution) and the **tape SMPTE timecode** (`tc`,
  the `0x63` pack `HH FF SS MM`, frame-accurate; hour is a camera preset, constant `07` here). The
  tape TC is what you cue a deck on for a re-capture, so the report shows it; see
  `docs/hdv-internals.md`.
- **Neither clock is a safe coordinate — align by hash, label by clock.** The wall-clock jumps with
  each take (not linear with tape position). The tape TC is rec-run: it *continues* across takes
  (regen on resume), so it is usually monotonic along a tape recorded head-to-tail — **but** a new
  recording on blank tape after a gap restarts it at the preset (`07:00:00`), so a capture can hold
  a legitimate **TC jump-back**. So it is piecewise-monotonic; never assume global monotonicity,
  never extrapolate `base + frames/fps`, and never order/align by either clock — that is hash work.
  A TC jump-back in a capture/merge is real tape behaviour, not a bug. Same anchor method as the
  sibling `iina-dv-timecode` (`src/sources/m2t.ts`).
- **GOPs are variable length** (~12 frames here). Cut only at GOP boundaries; `gop.py`
  finds them by the `00 00 01 B8` start code.
- **CC is capture plumbing, not tape data — `build` re-phases it.** The 4-bit continuity_counter
  is regenerated per capture pass (proven: two captures of one tape GOP have identical ES but
  different CC, so a plain concatenation breaks CC at every seam even though the content is
  tape-adjacent). `build` adds a constant per-PID offset to each segment so CC continues seamlessly
  across clean seams; a constant offset preserves every internal relationship (payload +1, AF-only
  unchanged, a residual's real break kept). CC lives in the TS header, not the ES, so GOP hashes are
  unchanged — a built file re-indexes/re-merges like a raw capture.
- **Open GOPs splice cleanly, and re-phasing makes the seam truly seamless** — the leading B-frames
  of a GOP reference the previous GOP's anchor, and because a seam is hash-verified tape-adjacent,
  that anchor is exactly the preceding segment's last GOP. With CC (and the tape-absolute PCR/PTS)
  continuous, a decoder runs straight through a seam — no reset, no leading-B-frame failure — so a
  re-fed merge shows zero continuity breaks and zero decode errors at its own seams (only the very
  first GOP of the whole output loses its 2 leading B-frames). This is why there are no per-seam
  markers and no "seam decode over-report" to discount: a clean seam is genuinely not a discontinuity.
- **A continuity break can swallow several GOPs.** A damaged GOP's bytes are garbage with
  a unique hash; the surrounding clean GOPs still match the other capture. `plan` must
  never pick a damaged candidate when a clean one exists, or it silently drops the good
  GOPs the other capture has (it routes by hash majority / contiguity, preferring clean).
- **Two clean copies can still disagree.** Intra-frame damage that leaves TS structure
  intact produces a different hash with no continuity break. In an overlap that is a
  `divergence` (reported for review); in a single-copy region it needs the decode pass
  (ffmpeg, on by default) to be seen at all.
- **The decode pass over-reports.** A decode error cascades from a lost reference onto later,
  byte-clean GOPs (e.g. ffmpeg flagged A#1414 though its bytes equal a clean B copy). Harmless:
  the walk picks by hash, so an identical clean copy wins regardless of the `dec` flag, and only
  genuine single-copy damage becomes a residual. Don't "fix" this by trusting `dec` over hashes.
- **File heads/tails are often slightly corrupt** (capture start/stop). `plan` keeps a
  GOP `EDGE` margin and prefers interior copies, so every file's edges are avoided except
  the global tape start and end.

## Invariants (assert + test)

- Sources are read-only; `build` is the only writer. It copies every byte that carries tape content
  verbatim (video ES, audio, `0xA1` AUX, PCR) and rewrites **only** the continuity_counter nibble
  to re-phase CC across seams. GOP content hashes are unchanged (CC is not in the ES), so a built
  file re-indexes/re-merges like a raw capture.
- `build` must catch its own mistakes: after a `-o` build, `verify.verify_build` re-scans the output
  and asserts its continuity/transport breaks equal what the plan emitted (`plan.emitted_cc/tei` —
  re-phasing adds none), the AUX timecode survives at both ends, and (with ffmpeg) every decode
  error sits on a *known* damaged GOP. Any deviation is a hard failure — this is the net for any
  way our TS handling could be wrong.
- Every cross-file seam is tape-adjacent: `plan` counts `bad_seams` (must be 0) by checking
  the incoming GOP's predecessor hash equals the outgoing GOP's hash.
- No silent drop of good content: a damaged GOP is only emitted (as a `residual`) when no
  capture has a clean copy of that tape position.

## When making changes

- ffmpeg may detect (the decode pass) but must never build output; third-party deps are fine but
  the core needs none.
- The only persisted artifact is the per-capture index. Keep it **idempotent**: no mtime, no
  timestamps, no absolute paths in it; `tag` is the basename; serialize with sorted keys. Change
  detection is the content `fingerprint` (size + head/tail hash), never mtime.
- `build` is the only writer of output bytes; it asserts packet alignment (never slides to resync)
  and may rewrite only the CC nibble, no other byte. Re-run `python -m unittest discover` (from the
  repo root) after touching `ts.py`, `gop.py`, `scan.py`, `plan.py`, `build.py`, or `verify.py`.
- Pure-Python packet scanning runs ~300 MB/s — benchmarked at parity with a numpy version, so
  numpy buys nothing here (deps are allowed, this one just isn't worth it).
