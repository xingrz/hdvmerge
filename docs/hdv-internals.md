# HDV / MPEG-TS byte-level reference

What hdvmerge needs to know about the bytes. HDV1080i (the common Sony HDV) is MPEG-2
video (1440×1080, 25 Mbit) + MPEG-1 Layer II audio in an MPEG-2 transport stream.

## Transport stream packet (188 bytes)

```
byte 0      0x47                       sync
byte 1      bit7 TEI  bit6 PUSI  bit5 priority  bits4-0 PID high
byte 2      PID low                    PID = ((b1 & 0x1F) << 8) | b2
byte 3      bits7-6 scrambling
            bits5-4 adaptation_field_control (AFC): 1=payload 2=AF-only 3=both
            bits3-0 continuity_counter (CC)
[AF]        if AFC in (2,3): byte4 = adaptation_field_length, then that many AF bytes
payload     after the AF (if any)
```

- **CC** increments by 1 (mod 16) per packet of a PID *that carries payload*. A jump that
  is not flagged by the adaptation field's `discontinuity_indicator` (AF byte 5 bit 7) means
  dropped/garbled packets — a damage signal.
- **M2TS (192-byte)** prefixes each packet with a 4-byte arrival timestamp; the sync byte is
  still 0x47, just at stride 192. hdvmerge detects stride by the longest run of strided syncs.

## PSI: finding the streams

- **PAT** (PID 0) lists programs → PMT PID.
- **PMT** lists elementary streams as `(stream_type, PID, ES_info)`. For HDV:
  - `0x02` MPEG-2 video, `0x03`/`0x04` MPEG audio,
  - `0xA0` / `0xA1` Sony private HDV streams. **`0xA1` carries the recording timecode**;
    `0xA0` is PCR-ish timing. Prefer `0xA1`.
- PMT sections can span multiple TS packets — reassemble by `payload_unit_start` before
  parsing, or a long PMT's `0xA1` entry can be missed.

## MPEG-2 video elementary stream

Start codes are `00 00 01 xx`:

- `B3` sequence_header, `B8` group_of_pictures_header (**GOP** — the splice unit),
  `00` picture_header (one per coded frame), `B5` extension, `B2` user_data.
- The GOP header's 25-bit `time_code` is **`00:00:00:00` on Sony HDV** (unused) — do not
  rely on it. The GOP header's `closed_gop`/`broken_link` bits live just after the timecode
  (byte +7 of the GOP header here).
- A GOP is ~6/12/15 frames depending on the camera; count `picture_header`s to get the frame
  count. hdvmerge hashes the bytes from one GOP start code to the next as the GOP's content
  fingerprint.

## Sony AUX recording date/time + tape timecode (`stream_type 0xA1`)

Inside a `private_stream_2` PES (`00 00 01 BF`), at fixed offsets from a `0x63` anchor sit two
distinct clocks (one anchor, decoded in a single pass by `aux.parse_aux`):

```
63 HH FF SS MM   c0 .. DD MM YY   ff   ss mm hh ..
└ tape timecode pack (frame-accurate)  └ wall-clock seconds, minutes, hours (BCD; reversed vs DV)
   HH FF SS MM, BCD          └ 0xC0 rec_date pack: +2 day  +3 month  +4 year   (BCD)
```

`bcd(b) = (b & 0x0F) + ((b >> 4) & 0x0F) * 10`. The PES context + the `63 .. c0 .. ff` shape
is specific enough that random bytes don't false-match.

- **Wall-clock date/time** (the `0xC0` date pack + the `ss mm hh` after the `0xFF`) is the
  camera's real-time clock, **second** resolution.
- **Tape timecode** (the `0x63` pack's four data bytes, `HH FF SS MM`) is the camcorder's
  rec-run TC track, **frame** resolution. Field order verified on real captures: decoding
  `HH FF SS MM` yields a clean SMPTE timecode (mask flag bits `HH&0x3F FF&0x3F SS&0x7F MM&0x7F`).
  The hour is a camera **preset** — on these tapes a constant `07`, so the meaningful, varying part
  is `MM:SS:FF`; a 60-min tape runs `07:00:00:00`→`07:59:59:xx` and never reaches `08` (verified at
  a 65-min tape's EOF: `07:59:59:22`, hour byte still `0x07`).

How the two clocks move is **different**, and neither is a safe coordinate:

- The **wall-clock** jumps with each take (the camera was used across hours/days), so it is *not*
  linear with tape position. Verified: one tape's takes span two calendar days while its tape TC
  climbs as one smooth ramp.
- The **tape TC** is rec-run, so it *continues* across takes — it does **not** reset every time you
  press record. On a tape recorded head-to-tail on one machine (incl. pauses, or rewind-and-
  overwrite where the camera regens the existing TC at the resume point) it is continuous/monotonic
  along the tape. **But** where a new recording starts on *blank* tape after a fast-forward gap,
  there is no TC to regen from and the camera restarts at the preset (`07:00:00:00`) — so a capture
  can contain a **TC jump-back**. It is therefore piecewise-monotonic; **never assume global
  monotonicity, and never use it for ordering/alignment** (that is always content-hash work). A
  jump-back in a capture is legitimate tape behaviour, not a parse error or a merge bug.

So both are **per-position labels, never extrapolated**. The tape TC is the value you cue on a deck
to re-capture, so the index carries it per GOP (`tc`) and the report shows it beside the wall-clock.
(Earlier notes called this pack "unreliable, ignored"; that was wrong — it is a valid, frame-
accurate timecode, just a rec-run/offset clock distinct from the wall-clock.)

The other private stream, `0xA0` (usually PID `0x815`), carries only a per-frame timing counter —
no date, timecode, or camera metadata — and the camera-data DV packs (`0x70`/`0x71`) that *can*
ride in `0xA1` are written empty (`0xff`, auto mode) on the Sony HDV captures inspected.

## Why ffmpeg never builds the output

`ffmpeg -c copy` re-muxes and drops unknown private streams, including `0xA1` — the recording
timecode would be lost. So the merge (`build`) and verification (`verify`) are byte-level work:
exact source byte ranges copied verbatim, every stream preserved, plus one AF-only
`discontinuity_indicator` marker inserted at each seam (`ts.make_disc_marker`) to signal the
capture-relative CC jump there — additive only, no source byte modified, no ES so GOP hashes are
unchanged. ffmpeg is used in one place only: the decode pass (`probe`) decodes the video to
*detect* intra-frame damage the TS layer can't reveal — on by default when ffmpeg is on PATH,
skipped with `--no-decode`. Detection never touches the output bytes.
