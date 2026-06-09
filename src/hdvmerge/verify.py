"""verify — confirm that a built file is a valid TS whose Sony AUX recording timecode survived.

The AUX stream is the byte-fidelity canary: if it still parses near both the start and the end
of the output, the private stream was preserved end to end (which an ffmpeg remux would have
dropped). Exit semantics for the CLI: 0 = readable, 1 = not readable, 2 = error.
"""

import os

from . import TS
from . import ts as T
from .psi import find_pids
from .aux import parse_rec


def _aux_rec_near(path, stride, aux_pid, target, window=8 << 20):
    a = max(0, target - window // 2)
    with open(path, "rb") as f:
        f.seek(a)
        buf = f.read(window)
    n = len(buf)
    pos = 0
    while pos + TS <= n:
        if buf[pos] == 0x47:
            pkt = buf[pos:pos + TS]
            if T.pid(pkt) == aux_pid and T.pusi(pkt):
                ps = T.payload_start(pkt)
                if ps is not None:
                    rec = parse_rec(pkt[ps:])
                    if rec:
                        return rec
            pos += stride
        else:
            pos += 1
    return None


def verify(path):
    """Return ``(ok, info)``. ``ok`` is True iff the AUX recording timecode is readable."""
    framing, vpid, apid, _pmt = find_pids(path)
    info = {"video_pid": vpid, "aux_pid": apid,
            "framing": framing["stride"] if framing else None}
    if framing is None or vpid is None:
        info["error"] = "not a recognisable MPEG-TS"
        return False, info
    if apid is None:
        info["error"] = "no Sony AUX stream (0xA1/0xA0) present"
        return False, info
    size = os.path.getsize(path)
    head = _aux_rec_near(path, framing["stride"], apid, size // 50)
    tail = _aux_rec_near(path, framing["stride"], apid, size - size // 50)
    info["rec_head"], info["rec_tail"] = head, tail
    return bool(head and tail), info
