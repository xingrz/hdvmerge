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

## Sony AUX recording timecode (`stream_type 0xA1`)

Inside a `private_stream_2` PES (`00 00 01 BF`), at fixed offsets from a `0x63` anchor:

```
63 .. .. .. ..   c0 .. .. .. ..   ff   SS MM HH ..
└ SMPTE timecode (rec-run, unreliable — ignored)
                 └ 0xC0 rec_date pack: +2 day  +3 month  +4 year   (BCD)
                                  └ separator
                                     └ wall-clock seconds, minutes, hours (BCD; reversed vs DV)
```

`bcd(b) = (b & 0x0F) + ((b >> 4) & 0x0F) * 10`. The PES context + the `63 .. c0 .. ff` shape
is specific enough that random bytes don't false-match. The recording date/time is the real
camera clock; it is **not linear with tape position** (pauses jump it forward), so read it per
position, never extrapolate.

## Why ffmpeg never builds the output

`ffmpeg -c copy` re-muxes and drops unknown private streams, including `0xA1` — the recording
timecode would be lost. So the merge (`build`) and verification (`verify`) are pure byte-level
work: exact byte ranges copied, every stream preserved. ffmpeg is used in one place only: the
decode pass (`probe`) decodes the video to *detect* intra-frame damage the TS layer can't reveal
— on by default when ffmpeg is on PATH, skipped with `--no-decode`. Detection never touches the
output bytes.
