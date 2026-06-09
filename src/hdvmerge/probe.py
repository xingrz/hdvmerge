"""Optional decode-based damage detection (ffmpeg).

The TS-level signals in :mod:`scan` (continuity breaks, TEI) and the cross-source hash
divergence in :mod:`plan` cover most damage, but they cannot see intra-frame bitstream damage
that leaves the TS structure intact in a region only one capture covers. A real MPEG-2 decode
can. This module runs ffmpeg purely to *detect* such damage and tag the affected GOPs — ffmpeg
is **never** used to build output (that would drop Sony's private AUX stream; the merge stays
byte-level).

ffmpeg also over-reports: a decode error often cascades from a lost reference frame onto later,
byte-clean GOPs. That is harmless here — a GOP whose bytes are identical to a clean copy in
another capture is selected by hash regardless of its `dec` flag, so the only effect of a false
positive is preferring an identical copy. Genuine single-copy damage becomes a residual.
"""

import re
import shutil
import subprocess

_ERR = re.compile(r"damaged|invalid|Invalid|concealing|corrupt|forbidden|Marker", re.I)
_PTS = re.compile(r"pts_time:([0-9.]+)")


def have_ffmpeg():
    return shutil.which("ffmpeg") is not None


def _gop_of_pts(gops, pts90):
    ans = 0
    for i, g in enumerate(gops):
        p = g.get("pts")
        if p is None:
            continue
        if p <= pts90:
            ans = i
        else:
            break
    return ans


def decode_errors(path, gops):
    """Return ``{gop_index: decode_error_count}`` by decoding ``path`` with ffmpeg and
    attributing each error to the GOP whose PTS range contains the frame it occurred on."""
    if not have_ffmpeg():
        raise RuntimeError("ffmpeg not found on PATH")
    proc = subprocess.Popen(
        ["ffmpeg", "-loglevel", "info", "-i", path, "-map", "0:v:0",
         "-vf", "showinfo", "-f", "null", "-"],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    errs = {}
    pending = 0
    for line in proc.stderr:
        m = _PTS.search(line)
        if m and "showinfo" in line:
            if pending:
                gi = _gop_of_pts(gops, int(round(float(m.group(1)) * 90000)))
                errs[gi] = errs.get(gi, 0) + pending
                pending = 0
        elif _ERR.search(line) and "showinfo" not in line and "MVs not available" not in line:
            pending += 1
    proc.wait()
    return errs
