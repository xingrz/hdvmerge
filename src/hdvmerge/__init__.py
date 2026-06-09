"""hdvmerge — detect damage in, align, and losslessly merge overlapping HDV tape captures.

A worn HDV/DV tape is often captured in several passes (you stop, rewind and retry whenever
a glitch appears on screen), producing several ``.m2t`` files that overlap and each carry some
errors. hdvmerge finds the damage, aligns the captures by *content*, and stitches the good
parts into one continuous file — **without ever re-encoding or remuxing**, preserving every
transport-stream byte including Sony's private AUX recording-timecode stream.

The whole pipeline is byte-level 188-byte MPEG-TS manipulation: standard library only, no
third-party packages, no ffmpeg (even ``ffmpeg -c copy`` strips the ``0xA1`` AUX stream).
"""

__version__ = "0.1.0"

# Transport stream constants.
SYNC = 0x47
TS = 188                      # bytes per TS packet
PCR_HZ = 90_000              # PTS/PCR base clock (90 kHz)

# MPEG-2 video start codes (4 bytes: 00 00 01 xx) we care about.
SEQ_START = b"\x00\x00\x01\xb3"   # sequence_header
GOP_START = b"\x00\x00\x01\xb8"   # group_of_pictures_header  (a splice unit boundary)
PIC_START = b"\x00\x00\x01\x00"   # picture_header  (one per coded frame)
