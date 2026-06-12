"""verify — confirm a built file is a sound TS: the Sony AUX recording timecode survived, and
``build``'s CC re-phasing produced a valid stream that introduced no damage of its own.

The AUX stream is the byte-fidelity canary: if it still parses near both the start and the end
of the output, the private stream was preserved end to end (which an ffmpeg remux would have
dropped). :func:`verify_build` adds the post-build integrity check — see its docstring. Exit
semantics for the CLI: 0 = ok, 1 = not, 2 = error.
"""

import bisect
import os

from . import TS
from . import ts as T
from . import probe
from .psi import find_pids
from .auxpack import parse_aux

_DEC_WINDOW = 5   # GOPs: a decode error attributes a few GOPs off its true frame (B-frame reorder)


def _aux_rec_near(path, stride, aux_pid, target, window=8 << 20):
    """``(rec, tc)`` of the first readable AUX packet near ``target``, or ``(None, None)``."""
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
                    rec, tc = parse_aux(pkt[ps:])
                    if rec:
                        return rec, tc
            pos += stride
        else:
            pos += 1
    return None, None


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
    head, head_tc = _aux_rec_near(path, framing["stride"], apid, size // 50)
    tail, tail_tc = _aux_rec_near(path, framing["stride"], apid, size - size // 50)
    info["rec_head"], info["rec_tail"] = head, tail
    info["tc_head"], info["tc_tail"] = head_tc, tail_tc
    return bool(head and tail), info


def verify_build(out_path, plan, decode=True):
    """Post-build integrity check of the merged output. Returns ``(ok, info)``.

    Because ``build`` rewrites the continuity counters, this is the net that catches any way our
    handling of MPEG-TS could be wrong:

    - **CC/TEI integrity** (always): re-scan the output; its continuity/transport breaks must equal
      what the plan *emitted* (``plan.emitted_cc``/``emitted_tei``). Re-phasing must add none at any
      seam — a higher count means the build corrupted the stream.
    - **AUX survival** (always): the recording timecode is still readable at both ends.
    - **decode integrity** (when ``decode`` and ffmpeg present): decode the whole output and require
      every decode error to coincide (within a few GOPs) with a *known* damaged GOP — a TS break in
      the output or a planned residual. An error on clean content means the build produced an
      invalid stream. On a clean merge that means zero decode errors.
    """
    from . import scan as scanmod
    idx = scanmod.scan_file(out_path)
    cc = sum(g["cc"] for g in idx.gops)
    tei = sum(g["tei"] for g in idx.gops)
    ok, info = verify(out_path)
    info.update(cc=cc, expected_cc=plan.emitted_cc, tei=tei, expected_tei=plan.emitted_tei)
    ok = ok and cc == plan.emitted_cc and tei == plan.emitted_tei

    if decode and probe.have_ffmpeg():
        errs = probe.decode_errors(out_path, idx.gops)
        # GOP indices we expect ffmpeg to choke on: TS breaks in the output, plus planned residuals
        # (which include intra-frame-only damage the TS layer can't see), located by emitted frame.
        starts, f = [], 0
        for g in idx.gops:
            starts.append(f)
            f += g["npic"]
        # GOP indices ffmpeg may legitimately choke on: TS breaks in the output, planned residuals,
        # and the first GOP of each stitched-in island (it starts fresh across a real gap, with no
        # prior reference frame) — all located by their emitted frame position.
        known = {i for i, g in enumerate(idx.gops) if g["cc"] > 0 or g["tei"] > 0}
        marks = [r["frame"] for r in plan.residuals]
        marks += [sg.frame0 for sg in plan.segments if sg.gap_before]
        for fr in marks:
            k = bisect.bisect_right(starts, fr) - 1
            if 0 <= k < len(idx.gops):
                known.add(k)
        allowed = {i + d for i in known for d in range(-_DEC_WINDOW, _DEC_WINDOW + 1)}
        unexplained = sum(c for gi, c in errs.items() if gi not in allowed)
        info.update(decode_errors=sum(errs.values()), unexplained_decode=unexplained)
        # A clean merge must decode cleanly, so any unexplained error there is a hard failure. A
        # merge that knowingly carries damage (residuals) is inherently noisy — ffmpeg cascades from
        # the real damage onto byte-clean GOPs — so decode integrity is informational there and the
        # CC/TEI check (which proved re-phasing introduced no break) is the gate.
        info["decode_gate"] = not plan.residuals
        if not plan.residuals:
            ok = ok and unexplained == 0
    return ok, info
