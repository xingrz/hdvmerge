"""build — materialise a plan into one continuous file by byte-level concatenation of the planned
segments, with a single discontinuity marker inserted at each seam. The only stage that writes
output bytes; sources stay read-only.

No ffmpeg, no remux: each segment's byte range is copied verbatim, so every TS stream survives
intact — including Sony's ``0xA1`` AUX recording-timecode stream that ``ffmpeg -c copy`` would
drop. Cut points are GOP/TS-packet aligned.

At each seam (between consecutive segments) the two captures have independent, capture-relative
continuity counters, so the video PID's CC jumps there even though the content is tape-adjacent
(the PCR/PTS, being tape-absolute, stay continuous). To signal that splice we insert one
``ts.make_disc_marker`` packet — an AFC=2, payload-less TS packet on the video PID with the
``discontinuity_indicator`` set — immediately before the incoming segment. It is purely additive:
**no source byte is modified**, and because it carries no ES payload it never changes a GOP's
content hash, so a built file can itself be re-indexed and re-merged exactly like a raw capture.
The marker tells a decoder to reset cleanly at the seam, and tells our own ``scan`` the CC jump is
signalled (not packet loss), so re-analysing a merged file no longer flags its own seams.
"""

import os

from . import ts as T


def build(plan, out_path, on_progress=None):
    marker = T.make_disc_marker(plan.video_pid) if plan.video_pid is not None else b""
    grand = sum(s.nbytes for s in plan.segments) + max(0, len(plan.segments) - 1) * len(marker)
    written = 0
    tmp = out_path + ".part"
    with open(tmp, "wb") as o:
        for i, sg in enumerate(plan.segments):
            assert sg.off < sg.end, "empty/inverted segment %s" % sg.tag
            if i > 0 and marker:
                o.write(marker)                 # signal the seam; modifies no source byte
                written += len(marker)
                if on_progress:
                    on_progress(written, grand)
            with open(sg.src, "rb") as f:
                f.seek(sg.off)
                rem = sg.end - sg.off
                while rem > 0:
                    chunk = f.read(min(16 << 20, rem))
                    if not chunk:
                        raise IOError("short read in %s at %d" % (sg.src, sg.off))
                    o.write(chunk)
                    rem -= len(chunk)
                    written += len(chunk)
                    if on_progress:
                        on_progress(written, grand)
    os.replace(tmp, out_path)
    return written
