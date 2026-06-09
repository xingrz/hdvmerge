"""MPEG-2 video GOP splitter + content hashing — the alignment key.

Fed the video-PID payload packet by packet (in file order), it reassembles the elementary
stream just enough to cut it at GOP boundaries (``00 00 01 B8``) and emit one record per GOP:

    off     file byte offset of the GOP's first TS packet (a PUSI) — a lossless cut point
    npic    coded pictures (frames) in the GOP
    closed  closed_gop flag
    broken  broken_link flag
    pts     33-bit PTS of the GOP's first access unit (or None)
    h       16-hex blake2b of the GOP's ES bytes — identical across captures of the same tape
            GOP, so it is a frame-accurate, metadata-free coordinate for aligning files
    cc      TS continuity-counter breaks inside the GOP (dropped/garbled packets)
    tei     transport_error_indicator packets inside the GOP

Only a bounded window of the ES is held in memory (trimmed as GOPs are emitted), so multi-GB
captures stream in constant space.
"""

import hashlib
import struct

from . import GOP_START, PIC_START


def parse_pts(pes):
    """33-bit PTS from a video PES header (00 00 01 E0..EF), or None."""
    if len(pes) < 14 or pes[0] or pes[1] or pes[2] != 1:
        return None
    if (pes[6] & 0xC0) != 0x80 or (pes[7] & 0x80) == 0:
        return None
    b = pes[9:14]
    return (((b[0] >> 1) & 7) << 30 | b[1] << 22 | ((b[2] >> 1) & 0x7F) << 15
            | b[3] << 7 | ((b[4] >> 1) & 0x7F))


class GopSplitter:
    def __init__(self):
        self._es = bytearray()
        self._base = 0                  # ES position of self._es[0]
        self._segs = []                 # (es_pos, file_off, pusi, pts, tei, cc) per fed packet
        self._pend = None               # currently open GOP
        self.gops = []

    def feed(self, file_off, pusi, payload, tei=0, cc=0, pts=None):
        """Append one video packet's payload. Cheap: GOP cutting happens in :meth:`flush`,
        called once per read chunk, so the ES ``find``/``del`` work is batched not per-packet."""
        self._segs.append((self._base + len(self._es), file_off, pusi, pts, tei, cc))
        self._es += payload

    def flush(self):
        self._flush()
        self._trim()

    def finalize(self, file_size):
        self._flush()
        if self._pend is not None:
            self._emit(file_size_end=file_size)
        for k in range(len(self.gops) - 1):
            self.gops[k]["end"] = self.gops[k + 1]["off"]
        if self.gops:
            self.gops[-1]["end"] = file_size
        for g in self.gops:
            g["nbytes"] = g["end"] - g["off"]
        return self.gops

    # --- internals ---

    def _flush(self):
        sf = 0
        while True:
            nxt = self._es.find(GOP_START, sf)
            if nxt < 0:
                break
            nxt_es = self._base + nxt
            if self._pend is None:
                self._pend = self._open(nxt_es)
                sf = nxt + 4
                continue
            if nxt_es <= self._pend["st"]:
                sf = nxt + 4
                continue
            self._emit(nxt_es)
            self._pend = self._open(nxt_es)
            sf = nxt + 4

    def _open(self, st_es):
        cut_off = pts = None
        for s in reversed(self._segs):
            if s[0] <= st_es and s[2]:        # last PUSI packet at/before the GOP start
                cut_off, pts = s[1], s[3]
                break
        if cut_off is None:
            for s in reversed(self._segs):
                if s[0] <= st_es:
                    cut_off = s[1]
                    break
        p = st_es - self._base
        closed = broken = 0
        if p + 8 <= len(self._es):
            v = struct.unpack(">I", bytes(self._es[p + 4:p + 8]))[0]
            closed, broken = (v >> 6) & 1, (v >> 5) & 1
        return {"st": st_es, "off": cut_off, "pts": pts, "closed": closed, "broken": broken}

    def _emit(self, end_es=None, file_size_end=None):
        st = self._pend["st"]
        end = end_es if end_es is not None else self._base + len(self._es)
        slc = bytes(self._es[st - self._base:end - self._base])
        cc = tei = 0
        for s in self._segs:
            if st <= s[0] < end:
                tei += s[4]
                cc += s[5]
        self.gops.append({
            "i": len(self.gops), "off": self._pend["off"], "end": None,
            "npic": slc.count(PIC_START), "closed": self._pend["closed"],
            "broken": self._pend["broken"], "pts": self._pend["pts"],
            "h": hashlib.blake2b(slc, digest_size=8).hexdigest(),
            "cc": cc, "tei": tei,
        })

    def _trim(self):
        keep = self._pend["st"] if self._pend is not None else self._base + len(self._es)
        cut = keep - self._base
        if cut > 0:
            del self._es[:cut]
            self._base += cut
            j = 0
            while j + 1 < len(self._segs) and self._segs[j + 1][0] <= self._base:
                j += 1
            if j > 0:
                del self._segs[:j]
