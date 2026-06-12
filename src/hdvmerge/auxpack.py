"""Sony HDV AUX recording date/time + tape-timecode decode.

Sony HDV cameras write the real camera clock into a private TS stream (``stream_type`` 0xA1).
Inside each ``private_stream_2`` (PES id 0xBF) packet the metadata sits at fixed offsets from
a ``0x63`` pack anchor::

    63 07 FF SS MM   c0 .. DD MM YY   ff   ss mm hh ..
    └ tape TC pack                         └ wall-clock ss mm hh (BCD, reversed vs DV)
       (status 0x07, then FF SS MM)  └ 0xC0 rec_date pack (.. day month year, BCD)

Two distinct clocks live in this one anchor:

- The **wall-clock** date/time (``0xC0`` date pack + the ``ss mm hh`` after the ``0xFF``) is the
  camera's real-time clock — second resolution. It is NOT linear with tape position (the original
  recording was paused/resumed), so read it per position, never extrapolate.
- The **tape timecode** in the ``0x63`` pack is the camcorder's running TC track. Its data bytes are
  ``07 FF SS MM``: a constant Sony status byte (``0x07``), then frames / seconds / minutes —
  frame-accurate, but with **no hours field** (a consumer rec-run TC never reaches an hour; the byte
  where DV carries hours is the ``0xC0`` pack boundary, i.e. 0). So the tape TC is ``00:MM:SS:FF``.
  Verified against real captures: ``FF SS MM`` advance at real speed and the ``wall-clock − TC``
  offset stays constant across a continuous capture, while ``0x07`` is constant tape-wide and the
  on-camera TC shows no hours. It is *rec-run*: it resets at each record start, so across a whole
  tape it is piecewise-monotonic with a jump at every take boundary — like the wall-clock, never
  extrapolate it across positions. Within a take it is finer-grained than the second-resolution
  wall-clock, which is exactly what a re-capture references.

The PES context (00 00 01 BF) plus the ``63 .. c0 .. ff`` shape is specific enough that random
bytes don't false-match.
"""

from . import TS
from . import ts as T


def _bcd(b):
    return (b & 0x0F) + ((b >> 4) & 0x0F) * 10


def parse_aux(pes_payload):
    """``(rec, tc)`` from one AUX PES payload, decoded from the single shared ``63..c0..ff``
    anchor. ``rec`` is the wall-clock ``"YYYY-MM-DD HH:MM:SS"`` (or date-only); ``tc`` is the tape
    timecode ``"HH:MM:SS:FF"``. Either may be None. ``pes_payload`` starts at the PES header
    (00 00 01 BF)."""
    p = pes_payload
    if len(p) < 8 or p[0] or p[1] or p[2] != 1 or p[3] != 0xBF:
        return None, None
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
            rec = "%04d-%02d-%02d %02d:%02d:%02d" % (year, month, day, hh, mm, ss)
        else:
            rec = "%04d-%02d-%02d" % (year, month, day)
        # tape timecode in the 0x63 pack's data bytes: `07 FF SS MM` — a constant Sony status byte
        # (0x07), then frames / seconds / minutes (flag bits masked off). There is NO hours field:
        # this rec-run TC resets per take and never reaches an hour, and the byte where DV would put
        # hours is the 0xC0 pack boundary (= 0). The status byte used to be mis-decoded as HH, which
        # stamped a spurious "07:" on every tape timecode (confirmed against real captures: 0x07 is
        # constant tape-wide and the on-camera TC has no hours).
        thh = 0
        tff = _bcd(b[i + 2] & 0x3F)
        tss = _bcd(b[i + 3] & 0x7F)
        tmm = _bcd(b[i + 4] & 0x7F)
        tc = None
        if thh <= 23 and tmm <= 59 and tss <= 59 and tff <= 29:  # PAL 25 / NTSC 30 frame range
            tc = "%02d:%02d:%02d:%02d" % (thh, tmm, tss, tff)
        return rec, tc
    return None, None


def parse_rec(pes_payload):
    """Wall-clock recording datetime from one AUX PES payload, or None (see :func:`parse_aux`)."""
    return parse_aux(pes_payload)[0]


def parse_tc(pes_payload):
    """Tape timecode ``"HH:MM:SS:FF"`` from one AUX PES payload, or None (see :func:`parse_aux`)."""
    return parse_aux(pes_payload)[1]


def parse_aux_packet(pkt):
    """``(rec, tc)`` from a single AUX TS packet (must be a PUSI packet); ``(None, None)`` if not."""
    if not T.pusi(pkt):
        return None, None
    ps = T.payload_start(pkt)
    if ps is None:
        return None, None
    return parse_aux(pkt[ps:])
