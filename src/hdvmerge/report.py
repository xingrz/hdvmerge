"""Render a plan as the human analysis report (Markdown).

This is the report you read each round: it leads with the **re-capture list** (the damage no
capture can repair, by the camera's real recording time), then shows how the tape was
reassembled, then the output facts. It doubles as the final record once you accept the
remaining list. Internal checks (seam adjacency) only show up if something is wrong.
"""

import unicodedata

GAP_GOP_FRAMES = 12   # only for estimating a gap's duration in the report


def _w(s):
    """Terminal display width: CJK and full-width chars occupy two columns, the rest one.
    Ambiguous-width chars (en-dash, arrows) count as one, matching the default terminal."""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def _table(header, rows):
    """A GitHub-flavored Markdown table whose columns are padded to line up when read as
    plain text in a monospace terminal (CJK-aware). `header` and each row are cell lists;
    returns the table's lines. Renderers ignore the padding; raw-text readers get aligned pipes."""
    w = [_w(h) for h in header]
    for r in rows:
        for i, c in enumerate(r):
            w[i] = max(w[i], _w(c))
    w = [max(3, x) for x in w]
    pad = lambda s, i: s + " " * (w[i] - _w(s))
    out = ["| " + " | ".join(pad(header[i], i) for i in range(len(w))) + " |",
           "| " + " | ".join("-" * w[i] for i in range(len(w))) + " |"]
    for r in rows:
        out.append("| " + " | ".join(pad(r[i], i) for i in range(len(w))) + " |")
    return out


def _hms(frames, fps):
    s = frames / (fps or 25.0)
    return "%02d:%02d:%02d" % (int(s // 3600), int(s % 3600 // 60), int(s % 60))


def _date(rec):
    return rec.split(" ")[0] if rec and " " in rec else "?"


def _time(rec):
    return rec.split(" ")[1] if rec and " " in rec else (rec or "?")


def _tc(tc):
    """Tape timecode HH:MM:SS:FF (frame-accurate), or '?' when no AUX timecode was read."""
    return tc or "?"


_SEP = " -_.·"


def _title(tags):
    """A batch name for the report header, taken from the captures' shared filename prefix (e.g.
    'CLIP-A'/'CLIP-B' -> 'CLIP'), or '' if they share none. Used only as a heading — every source
    is named in full in the tables, so this assumes nothing about how files are named."""
    uniq = list(dict.fromkeys(tags))
    if not uniq:
        return ""
    if len(uniq) == 1:
        return uniq[0]
    p = uniq[0]
    for t in uniq[1:]:
        while not t.startswith(p):
            p = p[:-1]
    cut = 0
    for i, c in enumerate(p):
        if c in _SEP:
            cut = i + 1
    return p[:cut].rstrip(_SEP)


def _kinds(rows):
    k = []
    if any(r["cc"] for r in rows):
        k.append("continuity break")
    if any(r["tei"] for r in rows):
        k.append("transport error")
    if any(r.get("dec") for r in rows):
        k.append("intra-frame damage")
    return ", ".join(k) or "damage"


def render(plan):
    fps = plan.fps or 25.0
    segs = plan.segments
    tags = [s.tag for s in segs]
    title = _title(tags)
    ncaptures = len({s.tag for s in segs})
    start = segs[0].rec if segs else None
    end = segs[-1].rec_end if segs else None

    L = ["# hdvmerge — %s" % title if title else "# hdvmerge report", ""]
    span = ("Recorded %s %s–%s · " % (_date(start), _time(start), _time(end))) if start and end else ""
    L.append("%srecoverable %s from %d capture%s."
             % (span, _hms(plan.total_frames, fps), ncaptures, "" if ncaptures == 1 else "s"))
    L.append("")

    # 0) loud warning: aligned sources the walk never reached — their content is NOT in the output
    if plan.unused_sources:
        total_f = sum(u["frames"] for u in plan.unused_sources)
        L.append("## ⚠ %d source%s NOT used — ~%s of content is missing from the output"
                 % (len(plan.unused_sources), "" if len(plan.unused_sources) == 1 else "s",
                    _hms(total_f, fps)))
        L.append("")
        L.append("These aligned onto a separate stretch of tape with no overlapping capture to "
                 "bridge them to the assembled chain, so the greedy walk never reached them. Their "
                 "content is **not** in the merged file. Add an overlapping capture across the gap, "
                 "or merge them separately.")
        L.append("")
        L += _table(["source", "recording span", "tape TC span", "length"],
                    [[u["tag"],
                      "%s – %s" % (_time(u["rec0"]), _time(u["rec1"])),
                      "%s – %s" % (_tc(u["tc0"]), _tc(u["tc1"])),
                      _hms(u["frames"], fps)]
                     for u in plan.unused_sources])
        L.append("")

    # 1) the headline: re-capture list
    groups = []
    for r in plan.residuals:
        if groups and r["tag"] == groups[-1][-1]["tag"] and r["frame"] - groups[-1][-1]["frame"] <= fps * 2:
            groups[-1].append(r)
        else:
            groups.append([r])
    if not groups:
        L.append("## Nothing to re-capture — every tape GOP has a clean copy. 🎉")
    else:
        L.append("## Re-capture these — %d spot%s with no clean copy"
                 % (len(groups), "" if len(groups) == 1 else "s"))
        L.append("")
        L.append("Each ~0.5 s. **Tape TC** is the frame-accurate timecode to cue on the deck; "
                 "recording time is the camera's wall clock. Re-capture with ≥15 s of good footage "
                 "on both sides, drop the new file in, and re-run.")
        L.append("")
        L += _table(["recording time", "tape TC", "damage", "only copy"],
                    [[_time(g[0].get("rec")), _tc(g[0].get("tc")), _kinds(g), g[0]["tag"]]
                     for g in groups])
    L.append("")

    # recorded-but-unreadable-in-every-capture: the tape was recorded here (rec-run TC and wall
    # clock advance in step across it) but no pass could read it — the merge jumps across it
    if plan.lost:
        total_f = sum(l["frames"] for l in plan.lost)
        L.append("## ⚠ Lost tape — recorded but unreadable in every capture")
        L.append("")
        L.append("%d span%s, ~%s. The tape WAS recorded here (its rec-run timecode and the camera "
                 "wall clock advance together across the spot) but no capture could read it, so the "
                 "merge jumps across it. Re-capture — a different deck, a cleaning, or a slower pass "
                 "may recover it."
                 % (len(plan.lost), "" if len(plan.lost) == 1 else "s", _hms(total_f, fps)))
        L.append("")
        L += _table(["recording time", "tape TC", "length"],
                    [[_time(l["rec0"]), "%s → %s" % (_tc(l["tc0"]), _tc(l["tc1"])),
                      _hms(l["frames"], fps)] for l in plan.lost])
        L.append("")

    # genuine gaps (missing in every capture) — worse than a residual
    if plan.gaps:
        L.append("## Gaps — missing in every capture")
        L.append("")
        total = sum((b - a + 1) for a, b in plan.gaps)
        L.append("%d gap%s, ~%s total. Re-capture these regions."
                 % (len(plan.gaps), "" if len(plan.gaps) == 1 else "s",
                    _hms(total * GAP_GOP_FRAMES, fps)))
        L.append("")

    # 2) how it was assembled
    ngaps = sum(1 for s in segs if s.gap_before)
    gapnote = (", %d stitched across a tape gap (marked ⟂)" % ngaps) if ngaps else ""
    L.append("## How it was assembled — %d segment%s, %d seam%s%s%s"
             % (len(segs), "" if len(segs) == 1 else "s", max(0, len(segs) - 1),
                "" if len(segs) - 1 == 1 else "s",
                "" if not plan.bad_seams else ", ⚠ %d NOT tape-adjacent" % plan.bad_seams, gapnote))
    if ngaps:
        L.append("")
        L.append("A **⟂ gap** row marks a real tape discontinuity: an island the walk reached only "
                 "by tape TC (no overlapping bridge). Its content IS in the output — the join is "
                 "signalled (a decoder resets cleanly there), and the timeline jumps by the missing "
                 "stretch.")
    L.append("")
    rows = []
    prev_tc_end = None
    for s in segs:
        if s.gap_before:
            rows.append(["⟂ **gap**", "_(missing)_", "%s → %s" % (_tc(prev_tc_end), _tc(s.tc))])
        rows.append([s.tag,
                     "%s – %s" % (_time(s.rec), _time(s.rec_end)),
                     "%s – %s" % (_tc(s.tc), _tc(s.tc_end))])
        prev_tc_end = s.tc_end
    L += _table(["from", "recording span", "tape TC span"], rows)
    L.append("")

    # divergences worth a human glance
    if plan.divergences:
        L.append("## Divergences — review")
        L.append("")
        L.append("Two clean copies of the same tape GOP differ byte-for-byte (intra-frame damage "
                 "the TS layer can't flag). The first copy is used; review if a spot looks wrong.")
        L.append("")
        L += _table(["recording time", "tape TC", "copies"],
                    [[_time(d.get("rec")), _tc(d.get("tc")),
                      ", ".join(c["tag"] for c in d["copies"])]
                     for d in plan.divergences])
        L.append("")

    # 3) output facts
    gb = sum(s.nbytes for s in segs) / 1e9
    seams = "verified tape-adjacent" if not plan.bad_seams else "⚠ %d bad" % plan.bad_seams
    L.append("## Output")
    L.append("")
    L.append("%.2f GB · %d frames @ %g fps · seams %s." % (gb, plan.total_frames, fps, seams))
    L.append("")
    return "\n".join(L)
