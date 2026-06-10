"""build — materialise a plan into one continuous file by byte-level concatenation of the planned
segments, **re-phasing the continuity counters** so the result is one seamless stream. The only
stage that writes output bytes; sources stay read-only.

No ffmpeg, no remux: every byte that carries tape content is copied verbatim — the video ES
(image), the audio, Sony's ``0xA1`` AUX recording timecode, and the PCR (the tape's real clock,
which an ``ffmpeg -c copy`` would also disturb). Cut points are GOP/TS-packet aligned.

The one field we DO rewrite is the 4-bit ``continuity_counter`` (CC). It is not tape data: it is
regenerated per capture pass (proven — two captures of the same tape GOP have byte-identical ES but
different CC, so a plain concatenation breaks CC at every seam even though the content is
tape-adjacent). So at each clean seam we add a constant per-PID offset to the incoming segment's CC
so it continues seamlessly from the outgoing segment. A constant offset preserves every internal CC
relationship (payload packets still +1, adaptation-only packets unchanged, and any real damage
break inside a residual is kept exactly), so the only thing that changes is the cross-capture phase
that was never tape-faithful anyway. CC lives in the TS header, not the ES, so GOP content hashes
are unchanged and a built file re-indexes/re-merges exactly like a raw capture.

The result is a stream with no spurious discontinuity at any seam: CC, PCR and PTS are all
continuous, so a decoder runs straight through (no reset, no leading-B-frame failure) and a re-scan
sees it as one capture. (Real discontinuities — gaps with no clean copy — are a separate concern.)
"""

import os

from . import SYNC, TS
from .ts import detect_framing


def _framing_stride(path, scan_bytes=2 << 20):
    with open(path, "rb") as f:
        head = f.read(scan_bytes)
    fr = detect_framing(head)
    if fr is None:
        raise IOError("cannot detect TS framing in %s" % path)
    return fr["stride"]


def build(plan, out_path, on_progress=None):
    segs = plan.segments
    if not segs:
        raise ValueError("empty plan")
    stride = _framing_stride(segs[0].src)
    chunk_size = stride * 65536          # whole packets per read (segment ranges are stride-aligned)
    grand = sum(s.nbytes for s in segs)
    written = 0
    running_cc = {}                      # pid -> last emitted CC nibble (continuity across segments)
    tmp = out_path + ".part"
    with open(tmp, "wb") as o:
        for si, sg in enumerate(segs):
            assert sg.off < sg.end, "empty/inverted segment %s" % sg.tag
            assert (sg.end - sg.off) % stride == 0, "segment %s not packet-aligned" % sg.tag
            delta = {}                   # pid -> CC offset for this segment (continue from running)
            first_seg = si == 0
            with open(sg.src, "rb") as f:
                f.seek(sg.off)
                rem = sg.end - sg.off
                while rem > 0:
                    chunk = bytearray(f.read(min(chunk_size, rem)))
                    if not chunk:
                        raise IOError("short read in %s at %d" % (sg.src, sg.off))
                    for base in range(0, len(chunk), stride):
                        assert chunk[base] == SYNC, "lost packet alignment in %s" % sg.tag
                        pid = ((chunk[base + 1] & 0x1F) << 8) | chunk[base + 2]
                        b3 = chunk[base + 3]
                        cc = b3 & 0x0F
                        has_payload = ((b3 >> 4) & 0x3) in (1, 3)
                        d = delta.get(pid)
                        if d is None:
                            if first_seg or pid not in running_cc:
                                d = 0                        # nothing to continue from; keep phase
                            else:
                                want = (running_cc[pid] + 1) & 0x0F if has_payload else running_cc[pid]
                                d = (want - cc) & 0x0F
                            delta[pid] = d
                        new_cc = (cc + d) & 0x0F
                        if new_cc != cc:
                            chunk[base + 3] = (b3 & 0xF0) | new_cc
                        running_cc[pid] = new_cc
                    o.write(chunk)
                    n = len(chunk)
                    rem -= n
                    written += n
                    if on_progress:
                        on_progress(written, grand)
    os.replace(tmp, out_path)
    return written
