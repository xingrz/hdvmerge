"""build — materialise a plan into one continuous file by PURE byte-level concatenation of the
planned segments. The only stage that writes output bytes; sources stay read-only.

No ffmpeg, no remux: each segment's byte range is copied verbatim, so every TS stream survives
intact — including Sony's ``0xA1`` AUX recording-timecode stream that ``ffmpeg -c copy`` would
drop. Cut points are GOP/TS-packet aligned, so the result is a valid MPEG-TS with ordinary
stream discontinuities at the (few) seams, exactly like a tape splice.
"""

import os


def build(plan, out_path, on_progress=None):
    grand = sum(s.nbytes for s in plan.segments)
    written = 0
    tmp = out_path + ".part"
    with open(tmp, "wb") as o:
        for sg in plan.segments:
            assert sg.off < sg.end, "empty/inverted segment %s" % sg.tag
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
