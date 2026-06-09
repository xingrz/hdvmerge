"""Sony HDV AUX recording date/time decode.

Sony HDV cameras write the real camera clock into a private TS stream (``stream_type`` 0xA1).
Inside each ``private_stream_2`` (PES id 0xBF) packet the metadata sits at fixed offsets from
a ``0x63`` pack anchor::

    63 .. .. .. ..   c0 .. .. .. ..   ff   SS MM HH ..
    └ SMPTE timecode (rec-run, unreliable — ignored)
                     └ 0xC0 rec_date pack (.. day month year, BCD)
                                      └ separator
                                         └ wall-clock SS MM HH (BCD, reversed vs DV)

The PES context (00 00 01 BF) plus the ``63 .. c0 .. ff`` shape is specific enough that random
bytes don't false-match. This is the same method as the iina-dv-timecode plugin. The recording
date/time is NOT linear with tape position (the original recording was paused/resumed), so it
must be read per position, never extrapolated.
"""

from . import TS
from . import ts as T


def _bcd(b):
    return (b & 0x0F) + ((b >> 4) & 0x0F) * 10


def parse_rec(pes_payload):
    """Recording datetime from one AUX PES payload as ``"YYYY-MM-DD HH:MM:SS"`` (or date-only,
    or None). ``pes_payload`` starts at the PES header (00 00 01 BF)."""
    p = pes_payload
    if len(p) < 8 or p[0] or p[1] or p[2] != 1 or p[3] != 0xBF:
        return None
    b = p[6:]  # skip 6-byte private_stream_2 PES header
    for i in range(0, len(b) - 14):
        if b[i] != 0x63 or b[i + 5] != 0xC0 or b[i + 10] != 0xFF:
            continue
        day = _bcd(b[i + 7] & 0x3F)
        month = _bcd(b[i + 8] & 0x1F)
        yb = _bcd(b[i + 9])
        if not (1 <= month <= 12 and 1 <= day <= 31):
            continue
        year = 1900 + yb if yb >= 75 else 2000 + yb
        ss, mm, hh = _bcd(b[i + 11] & 0x7F), _bcd(b[i + 12] & 0x7F), _bcd(b[i + 13] & 0x3F)
        if ss <= 59 and mm <= 59 and hh <= 23:
            return "%04d-%02d-%02d %02d:%02d:%02d" % (year, month, day, hh, mm, ss)
        return "%04d-%02d-%02d" % (year, month, day)
    return None


def parse_aux_packet(pkt):
    """Recording datetime from a single AUX TS packet (must be a PUSI packet), or None."""
    if not T.pusi(pkt):
        return None
    ps = T.payload_start(pkt)
    if ps is None:
        return None
    return parse_rec(pkt[ps:])
