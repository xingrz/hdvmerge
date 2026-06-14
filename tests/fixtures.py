"""Deterministic synthetic HDV transport streams for the test suite — no sample captures.

Builds a tiny "tape" as a list of GOPs (each with a unique salt so its content hash is unique,
and byte-identical across captures of the same tape position), then renders overlapping
captures of GOP ranges with PAT/PMT, a Sony AUX recording-time stream, and optional injected
damage (a continuity break, or a byte-corrupted GOP).
"""

import struct

from hdvmerge import SYNC

PMT_PID = 0x100
VIDEO_PID = 0x200
AUX_PID = 0x201


def _bcd(v):
    return ((v // 10) << 4) | (v % 10)


def _pkt(pid, payload, pusi, ccval):
    assert len(payload) == 184, len(payload)
    b1 = (0x40 if pusi else 0) | ((pid >> 8) & 0x1F)
    return bytes([SYNC, b1, pid & 0xFF, 0x10 | (ccval & 0x0F)]) + payload


def _pad184(b):
    if len(b) % 184:
        b = b + b"\xff" * (184 - len(b) % 184)
    return b


def _pat():
    sec = (bytes([0x00, 0xB0, 0x0D, 0x00, 0x01, 0xC1, 0x00, 0x00, 0x00, 0x01,
                  0xE0 | (PMT_PID >> 8), PMT_PID & 0xFF]) + b"\x00\x00\x00\x00")
    return _pad184(b"\x00" + sec)


def _pmt():
    es = (bytes([0x02, 0xE0 | (VIDEO_PID >> 8), VIDEO_PID & 0xFF, 0xF0, 0x00])
          + bytes([0xA1, 0xE0 | (AUX_PID >> 8), AUX_PID & 0xFF, 0xF0, 0x00]))
    body = (b"\x00\x01\xC1\x00\x00"
            + bytes([0xE0 | (VIDEO_PID >> 8), VIDEO_PID & 0xFF]) + b"\xF0\x00"
            + es + b"\x00\x00\x00\x00")
    slen = len(body)
    sec = bytes([0x02, 0xB0 | ((slen >> 8) & 0x0F), slen & 0xFF]) + body
    return _pad184(b"\x00" + sec)


def gop_es(tape_idx, frames=4, closed=1, broken=0, corrupt=False):
    """ES bytes for one tape GOP. Same (tape_idx, corrupt) -> identical bytes -> identical hash."""
    seq = b"\x00\x00\x01\xb3" + b"\x11\x22\x33\x44"
    goph = b"\x00\x00\x01\xb8" + struct.pack(">I", (closed << 6) | (broken << 5))
    body = bytearray()
    for fr in range(frames):
        body += b"\x00\x00\x01\x00"
        if corrupt:
            body += bytes([0xC0, 0x44, tape_idx & 0xFF, fr & 0xFF, 0x55])
        else:
            body += bytes([0xAA, (tape_idx >> 8) & 0xFF, tape_idx & 0xFF, fr & 0xFF, 0x55])
    return seq + goph + bytes(body)


def aux_payload(y, mo, d, h, mi, s, tc=None):
    """One Sony-AUX PES. ``tc`` is an optional tape timecode written into the 0x63 pack as it really
    appears: a constant ``0x07`` status byte, then ``FF SS MM`` (no hours field). Pass ``(MM, SS, FF)``
    for an hour-0 code, or ``(HH, MM, SS, FF)`` — the hour is carried in the rec-date pack ID
    (``0xC0 | HH``), exactly as a real deck encodes it. Omitted -> a zeroed 0x63 pack."""
    if tc is None:
        thh = tmm = tss = tff = 0
    elif len(tc) == 4:
        thh, tmm, tss, tff = tc
    else:
        thh, (tmm, tss, tff) = 0, tc
    anchor = bytes([0x63, 0x07, _bcd(tff), _bcd(tss), _bcd(tmm),
                    0xC0 | (thh & 0x03), 0x00, _bcd(d), _bcd(mo), _bcd(y % 100),
                    0xFF, _bcd(s), _bcd(mi), _bcd(h), 0x00])
    return b"\x00\x00\x01\xbf" + struct.pack(">H", len(anchor)) + anchor


class Capture:
    """Accumulate TS packets with per-PID continuity counters."""

    def __init__(self):
        self.buf = bytearray()
        self.cc = {}

    def _emit(self, pid, payload, pusi, bump=True):
        c = self.cc.get(pid, 0)
        self.buf += _pkt(pid, payload, pusi, c)
        if bump:
            self.cc[pid] = (c + 1) & 0x0F

    def psi(self):
        self._emit(0, _pat(), True)
        self._emit(PMT_PID, _pmt(), True)
        return self

    def aux(self, dt, tc=None):
        self._emit(AUX_PID, _pad184(aux_payload(*dt, tc=tc)), True)
        return self

    def gop(self, es, cc_break=False):
        data = _pad184(es)
        if cc_break:                       # drop one from the counter -> a continuity break
            self.cc[VIDEO_PID] = (self.cc.get(VIDEO_PID, 0) + 1) & 0x0F
        for k in range(0, len(data), 184):
            self._emit(VIDEO_PID, data[k:k + 184], pusi=(k == 0))
        return self

    def bytes(self):
        return bytes(self.buf)


def render_capture(tape, start, stop, base_dt, damage=None):
    """Render a capture of tape GOPs [start, stop). ``tape`` is a list of dicts with keys
    frames/closed/broken. ``damage`` maps a tape index -> 'cc' or 'corrupt'. ``base_dt`` is the
    (y,mo,d,h,mi,s) recording time at tape index 0 (advances ~1s per GOP)."""
    cap = Capture()
    y, mo, d, h, mi, s = base_dt
    import datetime
    t0 = datetime.datetime(y, mo, d, h, mi, s)
    for idx in range(start, stop):
        cap.psi()                          # repeat PAT/PMT before every GOP, like real HDV, so a
                                           # merge that starts mid-file still carries the tables
        t = t0 + datetime.timedelta(seconds=idx)
        # tape TC: a running MM:SS:FF timecode (no hours — like the real Sony 0x63 pack), frame-
        # accurate, tracking the wall clock at a constant offset.
        tc = (t.minute, t.second, idx % 25)
        cap.aux((t.year, t.month, t.day, t.hour, t.minute, t.second), tc=tc)
        dmg = (damage or {}).get(idx)
        es = gop_es(idx, frames=tape[idx]["frames"], closed=tape[idx]["closed"],
                    broken=tape[idx]["broken"], corrupt=(dmg == "corrupt"))
        cap.gop(es, cc_break=(dmg == "cc"))
    return cap.bytes()


def simple_tape(n=40):
    return [{"frames": 4, "closed": 1, "broken": 0} for _ in range(n)]
