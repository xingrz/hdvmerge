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
    }


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
        "complete": not plan.residuals and not plan.gaps and not plan.unused_sources,
        "chain": list(report.chain),
        "sources": [_source(report.source(t), report.shifts.get(t, 0)) for t in report.chain],
        "segments": [_segment(s) for s in plan.segments],
        "residuals": plan.residuals,
        "divergences": plan.divergences,
        "gaps": plan.gaps,
        "unused_sources": plan.unused_sources,
    }
