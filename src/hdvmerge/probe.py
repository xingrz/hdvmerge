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
# A subset of _ERR matches that are NOT picture damage but the mpegts *demuxer* complaining about the
# timestamp timeline. A byte-exact merge keeps each capture's own PTS/DTS base (only the CC nibble is
# rewritten), so the DTS steps at every cross-capture splice; the demuxer then emits, per splice, both
# "Packet corrupt (dts=...)" and the paired "corrupt input packet in stream N" (and on a backward step
# the muxer's "non monotonically increasing dts"). These are seam timestamp discontinuities — they
# affect some players' seeking, not the content. Genuine picture damage comes from the *decoder*
# ("[mpeg2video] ... damaged/concealing/...") and is NOT matched here, so anything unrecognised falls
# through to the decode bucket (we over-report rather than hide real damage).
_CONTAINER = re.compile(
    r"packet corrupt|corrupt input packet|non[- ]?monoton|monotonically increasing|packet mismatch",
    re.I)
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
    """Decode ``path`` with ffmpeg and return ``(decode, container)``, each
    ``{gop_index: count}`` attributing every error to the GOP whose PTS range contains the frame it
    occurred on:

    - ``decode`` — genuine MPEG-2 *decode* damage (concealed/damaged macroblocks, bad markers): the
      decoder choking on the picture bitstream. This is the real single-copy-damage signal.
    - ``container`` — mpegts *demuxer* timestamp complaints (``Packet corrupt (dts=...)``, non-
      monotonic DTS). Not picture damage — see :data:`_CONTAINER`. Reported separately so a sound
      byte-exact merge is never failed for a seam timestamp discontinuity.
    """
    if not have_ffmpeg():
        raise RuntimeError("ffmpeg not found on PATH")
    proc = subprocess.Popen(
        ["ffmpeg", "-loglevel", "info", "-i", path, "-map", "0:v:0",
         "-vf", "showinfo", "-f", "null", "-"],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    decode, container = {}, {}
    p_dec = p_con = 0
    for line in proc.stderr:
        m = _PTS.search(line)
        if m and "showinfo" in line:
            if p_dec or p_con:
                gi = _gop_of_pts(gops, int(round(float(m.group(1)) * 90000)))
                if p_dec:
                    decode[gi] = decode.get(gi, 0) + p_dec
                if p_con:
                    container[gi] = container.get(gi, 0) + p_con
                p_dec = p_con = 0
        elif _ERR.search(line) and "showinfo" not in line and "MVs not available" not in line:
            if _CONTAINER.search(line):
                p_con += 1
            else:
                p_dec += 1
    proc.wait()
    return decode, container
