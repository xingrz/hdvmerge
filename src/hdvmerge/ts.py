"""Shared byte-level MPEG-TS primitives — the single source of truth for packet parsing.

Everything operates on a 188-byte ``pkt`` (``bytes``/``memoryview`` whose first byte is the
0x47 sync). Nothing here mutates its input; the only writer, :func:`with_cc`, returns fresh
``bytes``. Reference: docs/hdv-internals.md.
"""

from . import SYNC, TS

# Framings we recognise: 188 (plain TS) and 192 (BDAV M2TS, a 4-byte timestamp prefix before
# each packet). The sync byte starts the 188-byte packet; the stride is how far to the next.
STRIDES = (188, 192)
SLOT_OFFSET = {188: 0, 192: 4}


def detect_framing(buf, min_run=4):
    """Detect TS framing from ``buf`` by the longest run of strided 0x47 syncs.

    Returns ``{"stride", "first_sync"}`` or ``None``. Picking the longest run (not the first
    sync) tolerates a few bytes of leading garbage and coincidental 0x47 bytes.
    """
    best = None  # (run, stride, start)
    n = len(buf)
    for stride in STRIDES:
        for off in range(stride):
            run = 0
            start = off
            i = off
            while i < n:
                if buf[i] == SYNC:
                    if run == 0:
                        start = i
                    run += 1
                    if best is None or run > best[0]:
                        best = (run, stride, start)
                else:
                    run = 0
                i += stride
    if best is None or best[0] < min_run:
        return None
    return {"stride": best[1], "first_sync": best[2]}


def first_sync(buf, stride, min_run=4, start=0):
    """First offset >= ``start`` with ``min_run`` consecutive syncs at ``stride``, or None."""
    n = len(buf)
    last = n - stride * min_run
    i = start
    while i <= last:
        if buf[i] == SYNC and all(buf[i + k * stride] == SYNC for k in range(1, min_run)):
            return i
        i += 1
    return None


# --- packet header fields (pkt = 188-byte slice starting at the sync byte) ---

def pid(pkt):
    return ((pkt[1] & 0x1F) << 8) | pkt[2]


def pusi(pkt):
    """payload_unit_start_indicator — this packet begins a new PES/section."""
    return bool(pkt[1] & 0x40)


def tei(pkt):
    """transport_error_indicator — the demodulator/tape flagged this packet as corrupt."""
    return bool(pkt[1] & 0x80)


def afc(pkt):
    """adaptation_field_control: 1=payload, 2=adaptation only, 3=both, 0=reserved."""
    return (pkt[3] >> 4) & 0x3


def has_payload(pkt):
    return afc(pkt) in (1, 3)


def cc(pkt):
    """continuity_counter (low nibble of byte 3)."""
    return pkt[3] & 0x0F


def payload_start(pkt):
    """Offset within ``pkt`` where the payload begins, or None if no payload / malformed AF."""
    a = afc(pkt)
    if a == 1:
        return 4
    if a == 3:
        ps = 5 + pkt[4]
        return ps if ps < TS else None
    return None


def disc_indicator(pkt):
    """adaptation field discontinuity_indicator — a legitimate, signalled CC/PCR break."""
    if afc(pkt) < 2 or pkt[4] == 0:
        return False
    return bool(pkt[5] & 0x80)


def with_cc(pkt, new_cc):
    """``pkt`` with its continuity_counter nibble replaced; every other byte preserved."""
    return pkt[:3] + bytes([(pkt[3] & 0xF0) | (new_cc & 0x0F)]) + pkt[4:]


def make_disc_marker(pid, cc=0):
    """A 188-byte payload-less TS packet on ``pid`` whose adaptation field sets the
    ``discontinuity_indicator``, telling a decoder to reset its clock/CC tracking cleanly. Clean
    seams need none (``build`` re-phases CC so they are truly continuous); this is for marking a
    *real* discontinuity — a gap with no clean copy — so a player handles the unavoidable PCR jump
    gracefully and a re-scan reads it as signalled, not as packet loss. AFC=2 (adaptation-field
    only): it carries no ES, so it never enters a GOP's ES or changes a content hash."""
    hdr = bytes([SYNC, (pid >> 8) & 0x1F, pid & 0xFF, 0x20 | (cc & 0x0F)])
    af = bytes([TS - 5, 0x80]) + b"\xFF" * (TS - 6)   # af_length, flags=discontinuity, stuffing
    return hdr + af
