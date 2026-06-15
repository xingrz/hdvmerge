"""Serialize an analysis (the alignment :class:`~hdvmerge.model.Report` + the byte-range
:class:`~hdvmerge.model.Plan`) to a JSON-ready dict.

This is the *structured* counterpart to :mod:`hdvmerge.report`'s human Markdown: a faithful dump of
hdvmerge's own model — sources, the assembled segment chain, residuals, divergences, gaps, unused
sources — so another program can consume the analysis without scraping Markdown. It is emitted by
``hdvmerge --json``; nothing here is persisted (the only on-disk artifact is still the per-capture
index). It exposes hdvmerge's model **as-is** and deliberately does NOT normalise to any external
schema — that mapping belongs to the consumer.

Kept in lock-step with the model by ``tests/test_jsonout.py`` so a future refactor of the model that
silently breaks this output (a path normal CLI use never exercises) fails the suite loudly.
"""

from . import __version__

SCHEMA = "hdvmerge.analysis/1"


def _tc_secs(tc, fps):
    """Tape TC ``"HH:MM:SS:FF"`` -> seconds (float), or None for an empty/unparseable value. The
    frame fraction uses ``fps`` so two adjacent codes order correctly; a caller testing for a session
    reset only cares about the sign and rough size of the jump."""
    try:
        h, m, s, f = (int(x) for x in tc.replace(";", ":").split(":"))
    except (ValueError, AttributeError):
        return None
    return ((h * 60 + m) * 60 + s) + f / (fps or 25.0)


def _source_damage(gops):
    """Contiguous runs of damaged GOPs (continuity break / transport error / decode error) within
    this capture, labelled by tape TC — i.e. where the *capture itself* is bad, independent of
    whether another capture covers that tape position cleanly. (The plan's residuals, by contrast,
    are only where no capture has a clean copy.) A consumer can show these on the capture's own
    lane."""
    spans = []
    cur = None
    for g in gops:
        if g["cc"] > 0 or g["tei"] > 0 or g.get("dec", 0) > 0:
            if cur is None:
                cur = {"tc0": g.get("tc"), "tc1": g.get("tc"),
                       "cc": 0, "tei": 0, "dec": 0, "ngops": 0}
            cur["tc1"] = g.get("tc")
            cur["cc"] += g["cc"]
            cur["tei"] += g["tei"]
            cur["dec"] += g.get("dec", 0)
            cur["ngops"] += 1
        elif cur is not None:
            spans.append(cur)
            cur = None
    if cur is not None:
        spans.append(cur)
    return spans


def _source_coverage(gops, fps):
    """The contiguous tape-TC runs this capture actually holds. A capture can drop content at a
    continuity break (its GOP timecodes then jump), so its coverage is NOT one solid span — split it
    wherever the TC jumps (> ~1 s forward, or backward), so a consumer can show the real gaps on the
    lane instead of a bar that pretends to cover them. Each run also carries the GOP index range
    ``[j0, j1)`` it spans, so a consumer can map it onto the physical frame axis (the TC alone can't —
    it restarts each session)."""
    segs = []
    start_tc = last_tc = None
    last_s = None
    j0 = jlast = 0
    for j, g in enumerate(gops):
        tc = g.get("tc")
        t = _tc_secs(tc, fps) if tc else None
        if t is None:
            continue
        if last_s is not None and (t - last_s > 1.0 or t - last_s < -0.2):
            segs.append({"tc0": start_tc, "tc1": last_tc, "j0": j0, "j1": jlast + 1})
            start_tc = tc
            j0 = j
        if start_tc is None:
            start_tc = tc
            j0 = j
        last_tc = tc
        last_s = t
        jlast = j
    if start_tc is not None:
        segs.append({"tc0": start_tc, "tc1": last_tc, "j0": j0, "j1": jlast + 1})
    return segs


def _source(idx, shift):
    """Per-capture summary: its place on the tape axis (``shift``), damage-flag totals, the
    recording-time / tape-TC span it covers (read from the GOP index, never extrapolated), and
    ``damage`` — the runs where this capture is itself damaged (see :func:`_source_damage`)."""
    gops = idx.gops
    recs = [g["rec"] for g in gops if g.get("rec")]
    tcs = [g["tc"] for g in gops if g.get("tc")]
    return {
        "tag": idx.tag,
        "ngops": len(gops),
        "video_pid": idx.video_pid,
        "aux_pid": idx.aux_pid,
        "fps": idx.fps,
        "decoded": idx.decoded,
        "shift": shift,
        "cc": sum(g["cc"] for g in gops),
        "tei": sum(g["tei"] for g in gops),
        "dec": sum(g.get("dec", 0) for g in gops),
        "rec0": recs[0] if recs else None,
        "rec1": recs[-1] if recs else None,
        "tc0": tcs[0] if tcs else None,
        "tc1": tcs[-1] if tcs else None,
        "damage": _source_damage(gops),
        "coverage": _source_coverage(gops, idx.fps),
    }


def _rec_curve(report, plan, target=600):
    """A sampled ``(tc, rec)`` curve of the assembled tape: the tape timecode paired with the camera
    wall-clock at that position, walked in output order. Lets a consumer map any tape position to its
    true recording time *per position* — the wall clock is non-linear (it jumps at record pauses, and
    where footage was shot on a different day) so extrapolating from one anchor is wrong. Uniformly
    downsampled to ~``target`` points to bound size; discontinuities survive at the sample resolution
    and the consumer snaps across them."""
    pts = []
    for s in plan.segments:
        idx = report.source(s.tag)
        if idx is None:
            continue
        for g in idx.gops[s.j0:s.j1]:
            tc, rec = g.get("tc"), g.get("rec")
            if tc and rec:
                pts.append((tc, rec))
    if not pts:
        return []
    step = max(1, len(pts) // target)
    out = [{"tc": pts[i][0], "rec": pts[i][1]} for i in range(0, len(pts), step)]
    if (len(pts) - 1) % step:
        out.append({"tc": pts[-1][0], "rec": pts[-1][1]})
    return out


def _axis_anchors(report, plan, target=600):
    """A sampled ``frame -> (tc, rec)`` curve along the assembled output, plus the frame positions of
    recording-session **seams** (where the record-run tape TC restarts). HDV's TC is per-session, so a
    tape that splices on later footage or partly over-records is NOT monotonic in TC: at the splice
    the TC jumps back to ~0 while physical position runs on. Such a tape must be laid out on this
    physical frame axis (TC would scatter/overlap the sessions), labelling tc/rec by interpolating
    these anchors and snapping across each seam. Mirrors dvmerge's pf-axis anchors.

    Returns ``(anchors, seams, multi_session)``: anchors are ``{frame, tc, rec}`` downsampled to
    ~``target`` points (endpoints and every seam are always kept); seams are frame positions; and
    ``multi_session`` is true iff any seam was found."""
    fps = plan.fps
    SEAM_DROP = 2.0   # a backward TC jump bigger than this (s) is a session reset, not frame jitter
    pts = []          # (frame, tc, rec) at every output GOP carrying both
    seams = []
    prev = None
    for s in plan.segments:
        idx = report.source(s.tag)
        if idx is None:
            continue
        frame = s.frame0
        for g in idx.gops[s.j0:s.j1]:
            tc, rec = g.get("tc"), g.get("rec")
            t = _tc_secs(tc, fps) if tc else None
            if tc is not None and rec is not None:
                if prev is not None and t is not None and t < prev - SEAM_DROP:
                    seams.append(frame)
                pts.append((frame, tc, rec))
            if t is not None:
                prev = t
            frame += g.get("npic", 0)
    if not pts:
        return [], [], False
    seamset = set(seams)
    step = max(1, len(pts) // target)
    anchors = [{"frame": f, "tc": tc, "rec": rec}
               for i, (f, tc, rec) in enumerate(pts)
               if i == 0 or i == len(pts) - 1 or f in seamset or i % step == 0]
    return anchors, seams, bool(seams)


def _segment(sg):
    return {
        "tag": sg.tag,
        "j0": sg.j0,
        "j1": sg.j1,
        "ngops": sg.ngops,
        "nbytes": sg.nbytes,
        "off": sg.off,
        "end": sg.end,
        "frame0": sg.frame0,
        "gap_before": sg.gap_before,
        "rec": sg.rec,
        "rec_end": sg.rec_end,
        "tc": sg.tc,
        "tc_end": sg.tc_end,
    }


def analysis(report, plan):
    """A JSON-ready dict capturing the whole analysis: how the captures align, how the tape is
    reassembled into segments, and everything still imperfect (residuals / divergences / gaps /
    unused sources). ``report`` is :func:`hdvmerge.scan.analyze`'s output, ``plan`` is
    :func:`hdvmerge.plan.build_plan`'s. Faithful to hdvmerge's model; the consumer normalises.

    ``complete`` is hdvmerge's own "nothing to re-capture" verdict — every tape GOP has a clean copy
    in the output and no source was left unplaced (no residuals, no gaps, no unused sources)."""
    axis_anchors, seams, multi_session = _axis_anchors(report, plan)
    return {
        "schema": SCHEMA,
        "version": __version__,
        "fps": plan.fps,
        "total_frames": plan.total_frames,
        "bad_seams": plan.bad_seams,
        "complete": not (plan.residuals or plan.gaps or plan.lost or plan.unused_sources),
        "chain": list(report.chain),
        "sources": [_source(report.source(t), report.shifts.get(t, 0)) for t in report.chain],
        "segments": [_segment(s) for s in plan.segments],
        "rec_curve": _rec_curve(report, plan),
        # physical-frame axis: the (frame -> tc/rec) curve and the recording-session seam positions,
        # so a multi-session tape (TC restarts) lays out by frame instead of collapsing on TC
        "anchors": axis_anchors,
        "seams": seams,
        "multi_session": multi_session,
        "residuals": plan.residuals,
        "divergences": plan.divergences,
        "gaps": plan.gaps,
        "lost": plan.lost,
        "unused_sources": plan.unused_sources,
    }
