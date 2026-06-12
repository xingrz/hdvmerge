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
    lane instead of a bar that pretends to cover them."""
    def secs(tc):
        try:
            h, m, s, f = (int(x) for x in tc.replace(";", ":").split(":"))
        except (ValueError, AttributeError):
            return None
        return ((h * 60 + m) * 60 + s) + f / (fps or 25.0)

    segs = []
    start_tc = last_tc = None
    last_s = None
    for g in gops:
        tc = g.get("tc")
        t = secs(tc) if tc else None
        if t is None:
            continue
        if last_s is not None and (t - last_s > 1.0 or t - last_s < -0.2):
            segs.append({"tc0": start_tc, "tc1": last_tc})
            start_tc = tc
        if start_tc is None:
            start_tc = tc
        last_tc = tc
        last_s = t
    if start_tc is not None:
        segs.append({"tc0": start_tc, "tc1": last_tc})
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
        "residuals": plan.residuals,
        "divergences": plan.divergences,
        "gaps": plan.gaps,
        "lost": plan.lost,
        "unused_sources": plan.unused_sources,
    }
