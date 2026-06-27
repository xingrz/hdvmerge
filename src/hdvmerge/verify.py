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


def _duplicate_frames(gops):
    """Tape moments (tc+rec) RE-emitted out of tape order — the same moment appears again after the
    timeline has moved past it (a backward-TC repeat at a splice). That is a duplicated frame: a
    redundant clean copy stitched in at an already-covered position; two byte-different copies of one
    moment are the cause (same tc+rec, different hash), so the moment — not the hash — is the key.

    Only **non-contiguous** repeats count: a couple of adjacent GOPs that share one (coarse/edge) AUX
    (tc, rec) label are a timecode-attribution stall, not a re-emission, so a run of identical positions
    is ignored — only occurrences separated by other frames are a real duplicate."""
    from collections import defaultdict
    pos = defaultdict(list)
    for i, g in enumerate(gops):
        if g.get("tc") and g.get("rec"):
            pos[(g["tc"], g["rec"])].append(i)
    out = []
    for (tc, rec), idxs in pos.items():
        if idxs[-1] - idxs[0] + 1 > len(idxs):   # not a single contiguous run -> a real re-emission
            out.append({"tc": tc, "rec": rec, "copies": len(idxs)})
    return sorted(out, key=lambda d: d["tc"] or "")


def find_duplicate_frames(path):
    """Scan an already-built master and return its duplicated frames (see :func:`_duplicate_frames`).
    No plan needed — works on any exported file, so it can audit masters built by older versions."""
    from . import scan as scanmod
    return _duplicate_frames(scanmod.scan_file(path).gops)


def decode_scan(out_path, idx, plan, on_progress=None):
    """Decode ``out_path`` with ffmpeg and classify every error against ``plan``. Returns a dict with
    the ``decode_errors`` / ``unexplained_decode`` / ``seam_discontinuities`` counts plus
    ``decode_error_spots`` — the located, cause-classified, cascade-coalesced decode-error spots; or
    ``{}`` when ffmpeg is absent.

    Shared by :func:`verify_build` (the merged output against the build's plan) and the standalone
    single-file ``verify`` (a master against its own single-source plan — *conservative*: with no build
    plan to name them, a divergence cut on otherwise-clean content reads as ``unexplained``). Each spot
    is ``{frame, tc, rec, kind, count}``; ``kind`` is ``residual`` (intra-frame damage no capture had
    clean), ``stitch`` (a fresh-start island edge — a real gap or a divergence), ``transport`` (a TS
    break), or ``unexplained`` (an error on content nothing in the plan explains — the one to worry
    about). ``frame`` is the emitted frame index; ``tc``/``rec`` come from the GOP at the spot."""
    if not probe.have_ffmpeg():
        return {}
    errs, container = probe.decode_errors(out_path, idx.gops, on_progress=on_progress)
    # emitted-frame start of each GOP, so a plan position (residual/island, in emitted frames) maps to
    # the GOP ffmpeg chokes on.
    starts, f = [], 0
    for g in idx.gops:
        starts.append(f)
        f += g["npic"]
    # The three legitimate reasons a GOP may fail to decode, kept SEPARATE so each surfaced error can
    # name its cause: a TS break in the output, a planned residual (intra-frame damage no capture has
    # clean), or the first GOP of a stitched-in island (it starts fresh — across a real gap or a
    # divergence — with no prior reference frame). All located by emitted frame position.
    transport = {i for i, g in enumerate(idx.gops) if g["cc"] > 0 or g["tei"] > 0}
    residual_g, island_g = set(), set()
    for fr in [r["frame"] for r in plan.residuals]:
        k = bisect.bisect_right(starts, fr) - 1
        if 0 <= k < len(idx.gops):
            residual_g.add(k)
    for fr in [sg.frame0 for sg in plan.segments if sg.gap_before]:
        k = bisect.bisect_right(starts, fr) - 1
        if 0 <= k < len(idx.gops):
            island_g.add(k)
    known = transport | residual_g | island_g
    allowed = {i + d for i in known for d in range(-_DEC_WINDOW, _DEC_WINDOW + 1)}
    unexplained = sum(c for gi, c in errs.items() if gi not in allowed)

    # Locate every decode error: attribute it to the nearest known cause (within the cascade window) and
    # coalesce a cascade of adjacent same-cause GOPs into ONE spot carrying a tape timecode + rec time —
    # so a UI / CLI can mark exactly where a master still decodes badly and what to do: residual or
    # stitch ⇒ try another capture there (a clean copy / a pass that resolves the divergence);
    # unexplained ⇒ a real concern, an error on content nothing explains.
    def _cause(gi):
        near = lambda s: any(gi + d in s for d in range(-_DEC_WINDOW, _DEC_WINDOW + 1))
        if near(residual_g):
            return "residual"
        if near(transport):
            return "transport"
        if near(island_g):
            return "stitch"
        return "unexplained"
    spots = []
    for gi in sorted(errs):
        g = idx.gops[gi]
        kind = _cause(gi)
        if spots and kind == spots[-1]["kind"] and gi - spots[-1]["_gi"] <= _DEC_WINDOW:
            spots[-1]["count"] += errs[gi]
            spots[-1]["_gi"] = gi
        else:
            spots.append({"_gi": gi, "frame": starts[gi], "tc": g["tc"], "rec": g.get("rec"),
                          "kind": kind, "count": errs[gi]})
    for s in spots:
        del s["_gi"]

    # seam timestamp discontinuities (demuxer "Packet corrupt (dts=...)") are inherent to a byte-exact
    # merge that never rewrites PTS/DTS; report their count but never gate on them — not content damage.
    # (Their POSITION is unreliable: the output's PTS is non-monotonic across a splice — the very thing
    # being flagged — so GOP attribution can't be trusted; hence a count only, unlike the decode spots.)
    return {"decode_errors": sum(errs.values()), "unexplained_decode": unexplained,
            "seam_discontinuities": sum(container.values()), "decode_error_spots": spots}


def verify_build(out_path, plan, decode=True, on_scan_progress=None, on_decode_progress=None):
    """Post-build integrity check of the merged output. Returns ``(ok, info)``.

    The check is two full-file passes — a byte re-scan (``on_scan_progress``) then, when ``decode``
    and ffmpeg are present, an ffmpeg decode (``on_decode_progress``) — each reporting ``(done,
    total)`` so a caller can show progress for what is otherwise a long silent step.

    Because ``build`` rewrites the continuity counters, this is the net that catches any way our
    handling of MPEG-TS could be wrong:

    - **CC/TEI integrity** (always): re-scan the output; its continuity/transport breaks must equal
      what the plan *emitted* (``plan.emitted_cc``/``emitted_tei``). Re-phasing must add none at any
      seam — a higher count means the build corrupted the stream.
    - **AUX survival** (always): the recording timecode is still readable at both ends.
    - **decode integrity** (when ``decode`` and ffmpeg present): decode the whole output and require
      every genuine *decode* error (concealed/damaged picture) to coincide (within a few GOPs) with a
      *known* damaged GOP — a TS break in the output or a planned residual. An error on clean content
      means the build produced an invalid stream. On a clean merge that means zero decode errors. The
      mpegts demuxer's *timestamp* complaints (``Packet corrupt (dts=...)`` / non-monotonic DTS) are
      counted separately as ``seam_discontinuities`` and never gate the build: the byte-exact merge
      preserves the tape's own PTS/DTS, and overlapping captures carry identical timestamps, so a
      cross-capture join is usually continuous — the few remaining steps are non-monotonic points in
      the tape's own recorded timestamps (NOT one per splice), a playback-seek nuisance, not damage.
    """
    from . import scan as scanmod
    idx = scanmod.scan_file(out_path, on_progress=on_scan_progress)
    cc = sum(g["cc"] for g in idx.gops)
    tei = sum(g["tei"] for g in idx.gops)
    ok, info = verify(out_path)
    dups = _duplicate_frames(idx.gops)
    info.update(cc=cc, expected_cc=plan.emitted_cc, tei=tei, expected_tei=plan.emitted_tei,
                duplicate_frames=dups)
    ok = ok and cc == plan.emitted_cc and tei == plan.emitted_tei and not dups

    if decode:
        d = decode_scan(out_path, idx, plan, on_progress=on_decode_progress)
        if d:                                       # ffmpeg present -> a real decode pass ran
            info.update(d)
            # A clean merge must decode cleanly, so any unexplained error there is a hard failure. A
            # merge that knowingly carries damage (residuals) is inherently noisy — ffmpeg cascades from
            # the real damage onto byte-clean GOPs — so decode integrity is informational there and the
            # CC/TEI check (which proved re-phasing introduced no break) is the gate.
            info["decode_gate"] = not plan.residuals
            if not plan.residuals:
                ok = ok and d["unexplained_decode"] == 0
    return ok, info
